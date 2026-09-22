#!/usr/bin/env python3
"""Shared core for Auditly: config, database, hashing, sessions, audit,
and the changelog parser that is the single source of the app version.

Stdlib only, deliberately -- same convention as ~/coeo-transcripts/core.py,
from which the env loader, scrypt, session and audit helpers are lifted.
No provider (Deepgram / OpenAI / Anthropic) code lives here; that is llm.py.
"""
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, ".env")
SCHEMA_PATH = os.path.join(HERE, "schema.sql")
CHANGELOG_PATH = os.path.join(HERE, "docs", "CHANGELOG.md")
PLAN_PATH = os.path.join(HERE, "docs", "PLAN.md")

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1

# Every key the app reads. A key not listed here is not configuration -- it is
# ignored, so a typo in .env fails loudly in /api/health rather than silently.
DEFAULTS = {
    "AUDITLY_PORT": "8084",
    "AUDITLY_BIND": "127.0.0.1",
    # 0.49.0: a second, encrypted listener from the same process (browsers open a microphone only on a
    # secure page). 0 = off. restart.sh sets 8444 and generates a self-signed pair into tls/ when absent;
    # a real certificate's two files can replace them at any time. Paths are relative to the app folder.
    "AUDITLY_TLS_PORT": "0",
    "AUDITLY_TLS_CERT": "tls/auditly.crt",
    "AUDITLY_TLS_KEY": "tls/auditly.key",
    "AUDITLY_DB": "./auditly.db",
    "AUDITLY_UPLOAD_DIR": "./uploads",
    "AUDITLY_MAX_UPLOAD_MB": "200",
    "AUDITLY_RETENTION_DAYS": "90",
    "AUDITLY_INSECURE_COOKIES": "0",
    "AUDITLY_SESSION_HOURS": "12",
    "AUDITLY_WORKERS": "1",
    "AUDITLY_MAX_TRANSCRIPT_CHARS": "120000",
    "AUDITLY_ALLOW_SPEND": "0",
    "AUDITLY_DEMO": "0",
    "AUDITLY_OPEN_ACCESS": "0",
    "AUDITLY_DEMO_HISTORY": "1",
    "SESSION_SECRET": "",
    "TRANSCRIBE_PROVIDER": "deepgram",
    "DEEPGRAM_API_KEY": "",
    "DEEPGRAM_MODEL": "nova-3",
    "OPENAI_API_KEY": "",
    "OPENAI_TRANSCRIBE_MODEL": "whisper-1",
    "OPENAI_API_BASE": "https://api.openai.com/v1",
    "SCORING_PROVIDER": "openai",
    "OPENAI_SCORING_MODEL": "gpt-4o-mini",
    "ANTHROPIC_API_KEY": "",
    "ANTHROPIC_MODEL": "claude-sonnet-5",
    "SCORING_TEMPERATURE": "0",
    "SCORING_MAX_TOKENS": "4000",
    "SCORING_SEED": "20260915",              # OpenAI gpt-4 family only; best-effort repeatability
    "AUDITLY_VOICE_AI_NAME": "Emma",            # the voice AI that answers first; never scored
    "AUDITLY_SYNC_MAX_RUNS_PER_VERSION": "1",   # "Optimise for the scorer" runs allowed per QA type version
    "AUDITLY_SYNC_MAX_RUNS_PER_DAY": "5",       # ... and across the server per UTC day
    "AUDITLY_SYNC_WAIT_S": "120",               # a job waits this long for an in-flight optimisation
    "AUDITLY_STT_SENTIMENT": "0",               # 1 = ask Deepgram for per-line sentiment (text-derived, English, billed per token)
    "AUDITLY_TARGET_PCT": "90",                 # the QA score the Dashboard measures pass rate and the target line against
    # coaching invites by email (mailer.py): unset SMTP_HOST = no sending, .ics download + mailto only
    "SMTP_HOST": "",
    "SMTP_PORT": "587",
    "SMTP_USER": "",
    "SMTP_PASSWORD": "",
    "SMTP_FROM": "",                            # the sending mailbox; the QA is Reply-To and the .ics organiser
    "SMTP_STARTTLS": "1",
    "SMTP_SEND_AS_QA": "0",                     # 1 = From: the QA's address (needs Send As rights on the relay)
    # the agent's dispute link (0.52.0): emailed when an audit is submitted, or copied by the QA from the scorecard
    "AUDITLY_DISPUTE_LINK_DAYS": "14",          # how long a link works; 0 switches the feature off (existing links stop too)
    "AUDITLY_PUBLIC_URL": "",                   # the address agents reach the app at, e.g. https://qa.company.com; blank = scheme+Host of the QA's request
    # Ask Auditly (assistant.py): off until an admin turns it on; the same spend gate as scoring
    "ASK_ENABLED": "0",
    "ASK_API_KEY": "",                          # a separate OpenAI project key for the assistant; blank = OPENAI_API_KEY
    "ASK_MODEL": "",                            # blank = the scoring model (OPENAI_SCORING_MODEL)
    "ASK_MAX_TOKENS": "700",                    # the answer's ceiling
    "ASK_MAX_PER_DAY": "300",                   # questions per UTC day, server-wide
    "ASK_MAX_PER_USER_PER_HOUR": "40",          # per signed-in person
    "ASK_RETENTION_DAYS": "90",                 # conversations older than this are deleted; 0 = never
}
KNOWN_KEYS = tuple(DEFAULTS)
PREFIX, LEGACY_PREFIX = "AUDITLY_", "L1QA_"      # the product was L1 Support QA until 0.30.0
OPENAI_STT_CAP = 25 * 1024 * 1024        # OpenAI's documented audio upload limit
AUDIO_EXT = {"mp3": "audio/mpeg", "wav": "audio/wav", "m4a": "audio/mp4",
             "ogg": "audio/ogg", "webm": "audio/webm", "mp4": "audio/mp4",
             "flac": "audio/flac"}


# ── config ────────────────────────────────────────────────────────────────
def env(path=None):
    """Re-read .env on each call so editing it takes effect without a restart.

    Lifted from cfbuilder-serve/cfbuilder_host.py:env() -- same format, same reason."""
    out = {}
    try:
        with open(path or ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                t = line.strip()
                if not t or t.startswith("#"):
                    continue
                if t.startswith("export "):
                    t = t[7:]
                i = t.find("=")
                if i < 1:
                    continue
                k, v = t[:i].strip(), t[i + 1:].strip()
                if len(v) > 1 and v[0] in "\"'" and v.find(v[0], 1) > 0:
                    # quoted value; anything after the closing quote (a comment) is dropped
                    v = v[1:v.find(v[0], 1)]
                elif v.startswith("#"):
                    v = ""                   # `KEY=   # comment` -- empty value, not the comment
                else:
                    # `KEY=value   # comment` is how .env.example is written, so
                    # `cp .env.example .env` must yield clean values. Only
                    # whitespace-then-# starts a comment; a bare # inside a
                    # token (a key, a URL fragment) is kept.
                    for j in range(1, len(v)):
                        if v[j] == "#" and v[j - 1].isspace():
                            v = v[:j].rstrip()
                            break
                out[k] = v
    except OSError:
        pass                      # no .env yet is a normal state for --demo
    return out


def cfg():
    """Defaults, overlaid by .env, overlaid by the process environment.

    The process environment wins so tests and systemd can pin a port or a
    database path without editing the file a human maintains."""
    c = dict(DEFAULTS)
    e = env()
    for k, v in e.items():
        if k in DEFAULTS:
            c[k] = v
    for k in KNOWN_KEYS:
        if os.environ.get(k) is not None:
            c[k] = os.environ[k]
    # 0.30.0 rename: a .env or a shell still using the old L1QA_* names keeps working. A new
    # name always wins; the old one only fills a key that was not set under its new name.
    for k in KNOWN_KEYS:
        old = LEGACY_PREFIX + k[len(PREFIX):]
        v = os.environ.get(old)
        if v is None:
            v = e.get(old)
        if v is not None and k not in e and os.environ.get(k) is None:
            c[k] = v
    return c


def legacy_keys():
    """The L1QA_* names still in use (in .env or the process environment) that should be
    renamed to AUDITLY_*; shown on the Server tab. Empty once the rename is complete."""
    e = env()
    out = []
    for k in KNOWN_KEYS:
        old = LEGACY_PREFIX + k[len(PREFIX):]
        if old in e or os.environ.get(old) is not None:
            out.append(old)
    return out


def cfg_int(c, key, default=0):
    try:
        return int(str(c.get(key, default)).strip())
    except (TypeError, ValueError):
        return default


def _abs(p):
    return p if os.path.isabs(p) else os.path.join(HERE, p)


def db_path(c=None):
    return _abs((c or cfg()).get("AUDITLY_DB") or DEFAULTS["AUDITLY_DB"])


def upload_dir(c=None):
    d = _abs((c or cfg()).get("AUDITLY_UPLOAD_DIR") or DEFAULTS["AUDITLY_UPLOAD_DIR"])
    if not os.path.isdir(d):
        os.makedirs(d, mode=0o700, exist_ok=True)
    try:
        if os.stat(d).st_mode & 0o077:
            os.chmod(d, 0o700)        # recordings are customer PII
    except OSError:
        pass
    return d


_QUOTED_PATH_RE = re.compile(r"(['\"])(/[^'\"]*)\1")        # '/any path/with spaces/file.mp3'
_BARE_PATH_RE = re.compile(r"(?<![\w.])/(?:home|tmp|var|srv|opt|mnt|root|Users)(?:/[^\s'\"]+)+")


def audit_name_for(agent, company, caller, date):
    """'<Agent> — <Caller's company> — <Caller's name> — <YYYY-MM-DD>', blank parts dropped.
    Mirrors buildAuditName() in the page; used when the upload sends none, and again by the
    worker once the scorer has read the customer's name and company from the call."""
    return " — ".join(p.strip() for p in (agent, company, caller, date) if p and p.strip())


def redact_paths(text, c=None):
    """Strip absolute paths from a message before it is stored or shown: an exception
    from open() on a missing upload carries the full path, and job.error is public.
    A path under the upload dir keeps its file name; any other path becomes <path>."""
    if not text:
        return text
    text = str(text)
    try:
        up = upload_dir(c).rstrip("/") + "/"
    except Exception:                      # noqa: BLE001 -- redaction must never raise
        up = None
    if up:
        text = text.replace(up, "uploads/")

    def _q(m):
        return m.group(1) + ("uploads/" + m.group(2).split("/uploads/", 1)[1] if "/uploads/" in m.group(2)
                             else "<path>") + m.group(1)
    text = _QUOTED_PATH_RE.sub(_q, text)
    return _BARE_PATH_RE.sub("<path>", text)


_SECRET_RE = re.compile(r"sk-[A-Za-z0-9_\-]{12,}")


def redact_secrets(text):
    """A provider's 401 body can echo a masked key prefix; nothing that looks like an
    API key survives into a stored, public error message."""
    return _SECRET_RE.sub("sk-…", str(text)) if text else text

def key_status(value):
    """What /api/health may say about a secret: whether it exists, and how long.
    The value itself never leaves the server."""
    v = (value or "").strip()
    return {"key_present": bool(v), "key_len": len(v)}


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_id():
    return str(uuid.uuid4())


def fmt_ts(seconds):
    """73.4 -> '01:13'. Used for transcript lines and evidence timestamps."""
    try:
        s = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return "--:--"
    return "%02d:%02d" % (s // 60, s % 60)


def slug(name, fallback="c"):
    s = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return (s or fallback)[:40]


# ── database ──────────────────────────────────────────────────────────────
def connect(path=None):
    db = sqlite3.connect(path or db_path(), timeout=15,
                         detect_types=0, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 15000")
    return db


def init_db(path=None):
    path = path or db_path()
    db = connect(path)
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        db.executescript(f.read())
    _migrate(db)
    _restrict(path)
    return db


def _migrate(db):
    """executescript + IF NOT EXISTS cannot add a column to a table that already
    exists, so columns added after a database was created are ALTERed in here."""
    for table, cols in (("recording", {"qa_name": "TEXT", "caller_company": "TEXT", "caller_name": "TEXT",
                                       "caller_email": "TEXT", "audit_name": "TEXT",
                                       "kind": "TEXT NOT NULL DEFAULT 'real'",
                                       "caller_phone": "TEXT", "ticket_ref": "TEXT"}),                   # 0.61.0
                        ("scorecard", {"call_summary": "TEXT", "prompt_version": "INTEGER",
                                       "sentiment_json": "TEXT", "call_reasons_json": "TEXT",
                                       "customer_json": "TEXT", "speakers_json": "TEXT", "transferred": "INTEGER",
                                       "guidance_mode": "TEXT", "tone_json": "TEXT", "kb_json": "TEXT", "kb_key": "TEXT",
                                       "call_facts_json": "TEXT"}),                                        # 0.61.0
                        # 0.28.0: "Optimise for the scorer". Existing versions get 'none' (not
                        # optimised, authored guidance in use) and nothing is queued at boot.
                        ("rubric_version", {"sync_status": "TEXT NOT NULL DEFAULT 'none'", "sync_error": "TEXT",
                                            "sync_attempts": "INTEGER NOT NULL DEFAULT 0",
                                            "sync_runs": "INTEGER NOT NULL DEFAULT 0",
                                            "sync_started_at": "TEXT", "sync_finished_at": "TEXT",
                                            "sync_provider": "TEXT", "sync_model": "TEXT", "sync_usage_json": "TEXT"}),
                        ("criterion", {"opt_description": "TEXT", "opt_met": "TEXT", "opt_partial": "TEXT",
                                       "opt_missed": "TEXT", "opt_na": "TEXT",
                                       "example_good": "TEXT", "example_bad": "TEXT"}),                    # 0.61.0
                        ("transcript", {"cached_from": "TEXT"}),
                        # 0.33.0: per-line sentiment (Deepgram, opt-in) and the tone & delivery metrics
                        ("utterance", {"sentiment": "TEXT", "sentiment_score": "REAL"}),
                        ("person", {"email": "TEXT"}),                       # 0.34.0: coaching invites
                        ("coaching_session", {"invite_sent_at": "TEXT", "invite_to": "TEXT"}),   # 0.40.0: emailed invite mark
                        ("coaching_invite", {"channel": "TEXT"}),                           # 0.41.0: smtp | mail_app
                        ("dispute", {"via": "TEXT"}),                                       # 0.52.0: link | qa
                        ("ask_turn", {"prompt_id": "TEXT"}),                                # 0.47.0: the Settings prompt version behind an answer
                        ("job", {"transcript_mode": "TEXT NOT NULL DEFAULT 'auto'", "usage_json": "TEXT",
                                 "carry_from_scorecard_id": "TEXT"})):
        have = {r["name"] for r in db.execute("PRAGMA table_info(%s)" % table).fetchall()}
        for col, decl in cols.items():
            if col not in have:
                db.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, decl))
    # 0.15.0: the call ID is the file name without its extension. Recordings uploaded before
    # that (or with the box left blank) get theirs filled in, so "was this call uploaded
    # already?" also finds the older ones. Blank ones only; a typed call ID is never touched.
    db.execute("UPDATE recording SET call_ref = substr(CASE WHEN ext IS NOT NULL AND ext <> ''"
               " AND length(filename) > length(ext) + 1 AND lower(substr(filename, -length(ext) - 1)) = '.' || lower(ext)"
               " THEN substr(filename, 1, length(filename) - length(ext) - 1) ELSE filename END, 1, 80)"
               " WHERE (call_ref IS NULL OR trim(call_ref) = '') AND filename IS NOT NULL AND trim(filename) <> ''")
    _rebuild_review(db)


def _rebuild_review(db):
    """ALTER cannot change a CHECK constraint. A `review` table created before 'queued'
    was allowed is rebuilt from the current schema.sql definition: copy, drop, rename,
    re-create its indexes (they die with the DROP and IF NOT EXISTS already ran).
    Nothing references `review`, so foreign keys need no juggling. One transaction:
    connections autocommit, and a crash between DROP and RENAME must not lose the table."""
    row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='review'").fetchone()
    if not row or "'queued'" in row["sql"]:
        return
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        m = re.search(r"CREATE TABLE IF NOT EXISTS review \((.*?)\n\);", f.read(), re.S)
    body = m.group(1)
    cols = [ln.strip().split()[0] for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("--")]
    have = {r["name"] for r in db.execute("PRAGMA table_info(review)").fetchall()}
    common = ",".join(c for c in cols if c in have)
    # Rows are copied verbatim; a stale row whose parent is already gone must not abort
    # the rebuild. The pragma is a no-op inside a transaction, so it goes first.
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("CREATE TABLE review_new (%s\n)" % body)
        db.execute("INSERT INTO review_new (%s) SELECT %s FROM review" % (common, common))
        db.execute("DROP TABLE review")
        db.execute("ALTER TABLE review_new RENAME TO review")
        db.execute("CREATE INDEX IF NOT EXISTS review_recording ON review(recording_id)")
        db.execute("CREATE INDEX IF NOT EXISTS review_submitted ON review(status, submitted_at)")
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    finally:
        db.execute("PRAGMA foreign_keys=ON")


def _restrict(path):
    """Keep the database owner-only: it holds password hashes, live session
    tokens and customer call content. SQLite creates it with the process umask,
    which on this box is 022 -- so without this it lands world-readable."""
    for p in (path, path + "-wal", path + "-shm"):
        try:
            if os.path.exists(p) and (os.stat(p).st_mode & 0o077):
                os.chmod(p, 0o600)
        except OSError as e:
            sys.stderr.write("could not restrict %s: %s\n" % (p, e))


# ── passwords ─────────────────────────────────────────────────────────────
def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                       n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32,
                       maxmem=64 * 1024 * 1024)
    return h, salt


def verify_password(password, pw_hash, pw_salt):
    if not pw_hash or not pw_salt:
        return False
    h, _ = hash_password(password, bytes(pw_salt))
    return hmac.compare_digest(h, bytes(pw_hash))


def password_problem(pw):
    """Return a complaint string, or None if acceptable. Length over
    character-class rules (current NIST guidance)."""
    if len(pw or "") < 12:
        return "Password must be at least 12 characters."
    if len(pw) > 200:
        return "Password must be under 200 characters."
    if pw.strip() != pw:
        return "Password cannot start or end with a space."
    return None


# ── sessions ──────────────────────────────────────────────────────────────
def new_session_token():
    """Return (raw_token, sha256_of_token). Only the hash is ever stored."""
    raw = secrets.token_urlsafe(32)
    return raw, hashlib.sha256(raw.encode("ascii")).digest()


def token_hash(raw):
    return hashlib.sha256((raw or "").encode("ascii", "ignore")).digest()


def session_expiry(hours=12):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)) \
        .replace(microsecond=0).isoformat()


# ── rows and audit ────────────────────────────────────────────────────────
def field(row, name):
    """Read one column from a sqlite3.Row, a dict, or None."""
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(name)
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


def audit(db, action, user=None, target=None, ip=None, detail=None):
    db.execute(
        "INSERT INTO audit (at,user_id,email,action,target,ip,detail)"
        " VALUES (?,?,?,?,?,?,?)",
        (now(), field(user, "id"), field(user, "email"),
         action, target, ip, detail))


# ── changelog: the single source of the version ───────────────────────────
_CLOG_HEAD = re.compile(r"^##\s*\[([^\]]+)\]\s*-\s*(.+?)\s*$")


def parse_changelog(text):
    """Parse Keep-a-Changelog markdown into [{version, date, bullets:[...]}].
    Bullets that wrap across several indented lines are rejoined into one."""
    entries, cur, in_bullet = [], None, False
    for line in text.splitlines():
        m = _CLOG_HEAD.match(line)
        if m:
            cur = {"version": m.group(1), "date": m.group(2), "bullets": []}
            entries.append(cur)
            in_bullet = False
            continue
        if cur is None:
            continue
        s = line.strip()
        if s.startswith("- "):
            cur["bullets"].append(s[2:].strip())
            in_bullet = True
        elif not s:
            in_bullet = False
        elif in_bullet and cur["bullets"]:
            cur["bullets"][-1] += " " + s
        elif s:
            cur["bullets"].append(s)
            in_bullet = False
    return entries


def changelog_entries():
    try:
        with open(CHANGELOG_PATH, "r", encoding="utf-8") as f:
            return parse_changelog(f.read())
    except OSError:
        return []


def app_version():
    e = changelog_entries()
    return e[0]["version"] if e else "?"


def read_plan():
    """docs/PLAN.md, and only that file -- shown in the version modal's Plan tab."""
    try:
        with open(PLAN_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""
