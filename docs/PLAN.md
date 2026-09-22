# Auditly — v1 Plan (built as "L1 Support QA"; renamed in 0.30.0)

**Plan version:** 1.0 · 2026-09-01 · Status: approved, v0.1.0 built against it
**App version at launch:** v0.1.0 (changelog entry dated 2026-09-01)
## 1. Context

The L1 support QA team reviews call recordings by hand. Goal: an internal web tool where a QA reviewer
**uploads a recording → it is transcribed (Deepgram or OpenAI) → scored against the team's own uploaded QA
guidelines on a 100-point basis → a scorecard shows per-criterion scores, Opportunities and Misses.** The app
carries a clickable version chip that opens a dated changelog, and ships with a `.env` plan.

`~/Auditly/` exists and is empty — greenfield, but four sibling projects set firm conventions
this plan follows rather than reinvents.

### 1.1 What exploration found — reuse vs build

| Reusable (copy from sibling) | Source |
|---|---|
| `.env` loader re-read per call; SQLite connect + 0600 restrict; scrypt users; token-hash sessions; CSRF header; login rate-limit; audit log | `~/coeo-transcripts/core.py` (`env()` :37, `connect()` :71, `_restrict()` :88, `audit()` :300), `serve.py` (:273 cookie, :281 CSRF, :403 rate-limit) |
| Zero-dep PDF writer → scorecard PDF export | `~/coeo-transcripts/pdfgen.py` (`Pdf` :75) |
| Keep-a-Changelog parser → version badge + "What's new" (single source of truth) | `~/coeo-transcripts/serve.py` `parse_changelog()` :61, `app_version()` :97 |
| CSP-with-nonce page serving, `__APP_VERSION__` substitution | `serve.py` `serve_app()` :895 |
| Audio relay with Range support (scrubbing in `<audio>`) | `serve.py` `media()` :556, `_parse_range()` :657 |
| Version chip → changelog dialog markup; drop-zone + FileReader size guard; `:root` theme tokens incl. `--good/--warn/--bad`; KPI tiles; `.seg` tabs; Auto/Light/Dark toggle | `~/voice.ai.calculator/index.html` :129-152, :193-210, :9-37, :909-921 |
| Whitelist static host; `restart.sh` matching by absolute path; host filename avoiding `serve.py` | `~/voice.ai.calculator/vaicalc_host.py`, `~/cfbuilder-serve/cfbuilder_host.py` (`env()` :59, `ALLOW_WRITE` :48) |
| nginx `include` common conf + dotfile deny + no `default_server`; self-verifying idempotent `install.sh`; systemd units | `~/coeo-transcripts/deploy/`, `~/cfbuilder-deploy/install.sh` |
| Seed rubric prose (written resolution definition; excluded metrics with reasons) | `~/voice-ai-training/templates/eval_scorecard.md` §1, §8 |
| Test harness: `tests/test_*.py` boots a real server on a temp DB, no deps | `~/coeo-transcripts/tests/test_portal.py` (`MediaStub` :81) |

**Must be built new** (no sibling has it): binary upload handling, Deepgram/OpenAI clients (existing
`Upstream._req` is GET-only), async job worker, rubric storage + guideline→criteria extraction, LLM scoring
layer, scorecard UI.

### 1.2 House conventions honoured

- Stdlib-only Python (`http.server` + `ThreadingTCPServer` + `sqlite3` + `urllib`). No pip deps, no build.
- Single-file vanilla HTML/JS frontend; no CDN/framework; light/dark via CSS vars; **no native `alert/confirm/prompt`**.
- Secrets never reach the browser; `/api/health` reports `key_present`/`key_len` only. `.env` mode 600; `.env.example` committed; `.gitignore` pre-emptive.
- Host filename must not contain `serve.py` (2026-08-19 `pkill` incident). Ports 8081/8082/8083 taken → **8084**.
- Every user-facing change = new `## [x.y.z] - YYYY-MM-DD` entry; `README.md:3` carries `**Version:**`; PRD header carries version.
- Project layout mirrors coeo-transcripts (backend + `deploy/` inside the project, since the backend is the app, not an add-on).

### 1.3 Host facts (checked 2026-09-01)

- Python **3.14.4** — `import cgi` fails (removed in 3.13) → raw-body upload (D3) is required, not a preference.
- **No `ffmpeg`** on PATH → OpenAI STT for files > 25 MB fails with a clear message until someone installs
  ffmpeg (`sudo apt install ffmpeg` — outside the project folder, so a human decision). Deepgram default unaffected.
- Listening: 22, 80 (nginx, site `snet-ai-apps`), 8081, 8082, 8083, <LAN IP>:8090. **8084 is free.**
- 4 CPU / 7.4 GB RAM — fine for a threaded stdlib server with one worker thread.

### 1.4 Trade-off to state explicitly

Sibling tools promise "nothing leaves this machine". This tool **necessarily sends customer call audio and
transcripts to third parties** (Deepgram/OpenAI for STT, OpenAI for scoring). README + PRD say so plainly; a
retention setting deletes audio after N days; provider choice stays an env decision. Deliberate departure.

---

## 2. Decisions (defaults chosen — change any before approving)

| # | Decision | Rationale |
|---|---|---|
| D1 | **Default STT = Deepgram `nova-3`** (diarize + utterances). OpenAI STT (`whisper-1`, `verbose_json` segments) selectable per upload. | Deepgram returns speaker turns natively and accepts up to 2 GB; OpenAI STT caps at 25 MB, and `whisper-1` is the OpenAI model that returns timestamps (`gpt-4o-transcribe*` variants can be tried via `OPENAI_TRANSCRIBE_MODEL`). |
| D2 | **Scoring LLM = OpenAI chat completions with `json_schema` strict output** (`OPENAI_SCORING_MODEL`, default `gpt-4o-mini` — confirm current id when setting). Anthropic is an optional second provider (`SCORING_PROVIDER=anthropic`, forced tool-use schema). | User asked for OpenAI; one key covers STT + scoring. Strict schema kills JSON drift. |
| D2b | **Speaker → role is decided by the scorer**: the prompt asks "which speaker label is the agent?" and returns `agent_speaker`; UI relabels turns and offers a one-click swap. No greeting-regex heuristic. | Cheaper and more robust than a heuristic; works for both Deepgram speakers and unlabeled OpenAI segments (`agent_speaker: null` → plain turns). |
| D3 | **Uploads are raw binary `POST /api/recordings`** (`Content-Type: audio/*`, `X-Filename` header), streamed to disk with a byte cap. Not multipart. | Python 3.13 removed `cgi`; a hand-rolled multipart parser is avoidable complexity. Browser `fetch(file)` sends the raw body trivially. |
| D4 | **Guidelines accepted as `.txt`, `.md`, `.docx` (zipfile + XML strip, stdlib) or pasted text.** PDF → v1.1 (no stdlib text extraction). | Keeps zero-dep rule. QA guidelines are usually Word/Confluence exports. |
| D5 | Guidelines → **LLM proposes structured criteria with weights summing to 100 → reviewer edits/approves → saved as an immutable rubric version.** Manual criteria entry also supported. | The "100% basis" is enforced at save time; the LLM only proposes. |
| D6 | **Per-criterion reviewer override (score + note) is in v1.** | QA is human-in-the-loop; LLM score is a first draft. Cheap: one PATCH route + two columns. |
| D7 | **Login required** (reuse coeo scrypt users/sessions; `--add-user` CLI). Single role in v1. | Recordings are customer PII; the code already exists to copy. |
| D8 | **Version/changelog single source = `docs/CHANGELOG.md`**, parsed server-side (`parse_changelog`), served at `/api/changelog`; chip text substituted from top entry. | coeo pattern: "no third place to keep in sync". Avoids CFB's byte-identical twin-copy burden. |
| D9 | **`--demo` mode** seeds a fixture transcript + rubric + scorecard and stubs both providers, so the UI and tests run with no keys. | Tests must not spend money or need network. |
| D10 | OpenAI STT files > 25 MB are **refused at upload time (HTTP 400)** naming the cap and suggesting Deepgram; the UI shows "≤ 25 MB" beside the OpenAI option. No transcode/chunking in v1 (host has no ffmpeg — §1.3). | Failing fast beats a job that dies minutes later; Deepgram sidesteps the cap entirely. v1.1 candidate: ffmpeg transcode if installed. |
| D11 | **`docs/PLAN.md` (this plan, trimmed) is served at `/api/plan` and shown as a second tab inside the version/changelog modal.** | Satisfies "show v1 plan" inside the app itself, next to the dated changelog. |

---

## 3. Repository layout

```
~/Auditly/
  CLAUDE.md                 working notes (invariants, layout, changelog rule)
  README.md                 line 3: **Version:** 0.1.0 · 2026-09-01 ; Files table; data-leaves-machine notice
  .gitignore                .env, .env.*, !.env.example, *.db*, uploads/, *.log
  .env.example              every variable, commented, no values (see §7)
  auditly_host.py              HTTP server: routes, auth, upload, job worker thread   (NOT "serve.py")
  core.py                   env(), connect(), init_db(), scrypt, sessions, audit()  ← copied from coeo, renamed constants
  transcribe.py             Deepgram + OpenAI STT clients → normalised Transcript
  rubric.py                 rubric validation (weights == 100), .docx/.txt/.md text extraction, LLM criteria extraction
  score.py                  prompt builder, JSON schema, LLM call, validation/clamping → Scorecard
  llm.py                    the ONE outbound HTTP helper: POST JSON / POST binary / POST multipart, Bearer/Token auth, retries
  pdfgen.py                 copied verbatim from coeo
  demo_data.py              fixture call transcript, fixture rubric, fixture scorecard; provider stubs
  schema.sql                see §4
  auditly.html        entire front end
  fixtures/demo_call.json   diarized utterances for the fixture call (demo mode + tests)
  fixtures/demo_rubric.md   an L1 rubric in prose (extraction test input)
  fixtures/demo_scorecard.json  deterministic DemoScorer output
  fixtures/tone.wav         ~2 s silent WAV so the upload path has a real file
  docs/CHANGELOG.md         ## [0.1.0] - 2026-09-01  (source of truth for the version chip)
  docs/PLAN.md              the v1 plan shown in-app (served at /api/plan)
  docs/PRD_Auditly.md living doc; numbered sections; ends with Build snapshot
  docs/RUNBOOK.md           install, .env, add-user, restart, logs, rotation
  docs/VERIFY.md            test map (headless / live / browser-by-hand)
  tests/test_auditly.py        boots real server on temp DB with stubbed providers
  deploy/nginx-auditly.conf    listen 8084 + server_name auditly.example.com ; include auditly-common.conf
  deploy/auditly-common.conf   proxy_pass 127.0.0.1:8084 ; client_max_body_size 250m ; proxy_request_buffering off ; dotfile deny
  deploy/auditly.service       systemd unit (User=rmangune, WorkingDirectory=project, ExecStart=python3 auditly_host.py)
  deploy/install.sh         idempotent; pre-flight port/server_name checks; nginx -t rollback; post-install self-verify (.env → 404)
  deploy/uninstall.sh
  uploads/                  runtime, gitignored, 0700: <recording_id>.<ext>
```

---

## 4. Data model (`schema.sql`)

```sql
user(id, email UNIQUE NOCASE, name, pw_hash, pw_salt, role CHECK IN ('admin','reviewer'), active, created_at, last_login)
session(token_hash PK, user_id, created_at, expires_at, ip)        -- from coeo
login_attempt(at, email, ip)                                       -- from coeo (rate limit)
audit(id, at, user_id, email, action, target, ip, detail)          -- from coeo

rubric(id, name, created_at, created_by)
rubric_version(id, rubric_id, version INT, source_text, source_filename,
               created_at, created_by, UNIQUE(rubric_id, version))  -- immutable once a scorecard references it
criterion(id, rubric_version_id, position, key /*slug, UNIQUE per version; used as the schema enum*/, name, description,
          weight INT CHECK(weight BETWEEN 0 AND 100),
          guidance_met, guidance_partial, guidance_missed,          -- what full/partial/zero looks like
          critical INT DEFAULT 0)                                   -- 1 = auto-fail flag if missed
  -- app-level check on save: SUM(weight) over a rubric_version == 100

recording(id, filename, ext, bytes, sha256, duration_s, uploaded_at, uploaded_by,
          agent_name, call_ref, notes, deleted_at)                  -- agent_name/call_ref optional metadata typed at upload
job(id, recording_id, rubric_version_id, stt_provider, stt_model, scoring_provider, scoring_model,
    status CHECK IN ('queued','transcribing','scoring','done','failed'),
    error, created_at, started_at, finished_at, attempts)
transcript(id, recording_id, job_id, provider, model, language, raw_json, created_at)
utterance(transcript_id, seq, start_s, end_s, speaker INT /*provider label, NULL for OpenAI*/, text, confidence,
          PRIMARY KEY(transcript_id, seq))
scorecard(id, job_id UNIQUE, transcript_id, rubric_version_id,
          agent_speaker INT /*from scorer, D2b; reviewer can swap*/,
          overall_pct REAL, applicable_weight INT /*100 minus 'na' weights*/, auto_fail INT,
          summary, strengths_json, opportunities_json, misses_json, warnings_json, truncated INT, model, created_at)
scorecard_item(id, scorecard_id, criterion_id, ai_score REAL, ai_rating CHECK IN ('met','partial','missed','na'),
               rationale, evidence_json,                            -- [{quote, start_s, speaker}]
               override_score REAL, override_note, override_by, override_at)
  -- final_score = COALESCE(override_score, ai_score); overall recomputed on override
```

---

## 5. API routes (`auditly_host.py`)

All JSON routes require session cookie + `X-Auditly-CSRF: 1` header (coeo pattern). Cross-user data is not a
concern in v1 (single team), but every row still goes out through one whitelist serializer `to_public()`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/`, `/auditly/` | app HTML with CSP nonce + `__APP_VERSION__` substituted |
| GET | `/api/health` | `{version, db_ok, ffmpeg, deepgram:{key_present,key_len}, openai:{…}, anthropic:{…}, default_stt, default_scoring}` |
| GET | `/api/changelog` | `parse_changelog(docs/CHANGELOG.md)` → `[{version,date,bullets}]` |
| GET | `/api/plan` | `{markdown}` from `docs/PLAN.md` (whitelisted single file) — rendered in the "Plan" tab of the version modal |
| POST | `/api/login` · `/api/logout` · GET `/api/me` | coeo auth |
| POST | `/api/recordings?filename=&provider=&rubric_version_id=&agent=&call_ref=` | raw body streamed in 1 MB chunks to `uploads/<id>.<ext>` while hashing; checks: `Content-Length ≤ AUDITLY_MAX_UPLOAD_MB` (413), ext ∈ {mp3,wav,m4a,ogg,webm} (400), provider key present (400), **provider=openai and size > 25 MB → 400**; short read → partial file unlinked; creates `recording` + `job(queued)`; returns `{recording_id, job_id}` |
| GET | `/api/jobs?status=&limit=` | job list joined with recording + rubric name |
| GET | `/api/jobs/<id>` | status, timings, error; UI polls every 3 s while not terminal |
| POST | `/api/jobs/<id>/retry` | requeue a failed job (optionally with a different provider) |
| POST | `/api/recordings/<id>/rescore` | new job with `rubric_version_id`, reuses existing transcript (skips STT) |
| GET | `/api/recordings/<id>/audio` | Range-capable relay from `uploads/` (coeo `media()` adapted to local file) |
| GET | `/api/recordings/<id>/transcript` | utterances |
| PATCH | `/api/scorecards/<id>/agent-speaker` | `{speaker}` — swap which speaker label is the agent (fixes the scorer's guess, D2b); audit |
| GET | `/api/scorecards/<id>` | full scorecard + items + criteria |
| PATCH | `/api/scorecards/<id>/items/<item_id>` | `{override_score, override_note}` → recompute `overall_pct`; audit |
| GET | `/api/scorecards/<id>/export.pdf` | `pdfgen` scorecard (header, overall %, table, opportunities, misses, evidence quotes) |
| GET | `/api/scorecards/<id>/export.csv` | one row per criterion |
| GET | `/api/rubrics` · POST `/api/rubrics` | list / create named rubric |
| GET | `/api/rubrics/<id>/versions/<v>` | criteria |
| POST | `/api/rubrics/<id>/versions` | `{criteria:[…]}` → validate weights==100 → new immutable version |
| POST | `/api/rubrics/extract` | `{text}` or raw `.docx/.txt/.md` body → LLM-proposed criteria (not saved) |
| DELETE | `/api/recordings/<id>` | soft-delete row + unlink audio file; audit |

Worker: `threading.Thread(daemon=True)` consuming `queue.Queue` of job ids; on startup, any job in
`queued/transcribing/scoring` is re-queued (`attempts+1`, give up at 3). Pipeline per job:
`transcribe.run(job) → utterances → score.run(job) → scorecard`. Each stage updates `job.status` so the UI
shows "Transcribing… / Scoring…". Retention sweep: hourly thread deletes audio older than `AUDITLY_RETENTION_DAYS`
(keeps transcript + scorecard).

---

## 6. Provider clients

### 6.1 `llm.py` — one outbound helper
```python
def post_json(url, headers, payload, timeout=120) -> dict
def post_bytes(url, headers, data: bytes|file, content_type, timeout=600) -> dict
def post_multipart(url, headers, fields: dict, file_field, filename, file_bytes, content_type) -> dict
# Bearer/Token in Authorization header only (never URL). 2 retries on 429/5xx with backoff. Raises ProviderError(status, snippet)
```

### 6.2 `transcribe.py`
```python
@dataclass Utterance(speaker:str, start_s:float, end_s:float, text:str)
@dataclass Transcript(provider, model, language, utterances:list[Utterance], raw:dict, duration_s)

def deepgram(path, model) -> Transcript
  # POST https://api.deepgram.com/v1/listen?model=nova-3&diarize=true&utterances=true&smart_format=true&punctuate=true
  # Authorization: Token <DEEPGRAM_API_KEY>; body = raw audio bytes; Content-Type from ext
  # utterances[] → Utterance(speaker=f"Speaker {u.speaker}", start, end, transcript)
def openai(path, model) -> Transcript
  # (>25 MB already refused at upload) POST https://api.openai.com/v1/audio/transcriptions multipart:
  #   file, model=whisper-1, response_format=verbose_json, timestamp_granularities[]=segment
  # segments[] → Utterance(speaker=None, …)  — no diarization; scorer is told speakers are unlabeled
class DemoTranscriber            # returns fixtures/demo_call.json after sleep(0.2); used by --demo and tests
def transcriber_for(name, cfg)   # dispatch on job.stt_provider (deepgram | openai | demo)
```

### 6.3 `rubric.py`
```python
def text_from_upload(filename, data) -> str      # .txt/.md passthrough; .docx: zipfile→word/document.xml→strip tags, paragraphs→\n
def validate(criteria) -> list[str]              # errors: weights not summing to 100, empty names, weight<0, duplicate names
def extract_criteria(text, provider) -> list[dict]  # LLM: "turn these QA guidelines into ≤12 weighted criteria summing to 100" (schema below)
```

### 6.4 `score.py` — prompt + schema
System prompt (fixed): *"You are a strict but fair QA reviewer for a Level 1 technical support desk. Score one call
transcript against the rubric. (1) Use only what is in the transcript; never assume. (2) For each criterion return a
rating met/partial/missed/na and a score 0..weight — met = full weight, partial ≈ half, missed = 0, na = 0 and the
criterion is excluded; use na only when it genuinely could not apply. (3) Every rating except na needs ≥1 evidence quote
copied verbatim with its [mm:ss] timestamp; never invent quotes. (4) Identify which speaker label is the agent.
(5) Misses = things the rubric required that the agent did not do. Opportunities = coaching points that would have
made the call better even where the criterion was met. Be specific and actionable. (6) If a criterion marked critical
is missed, set auto_fail=true. (7) Return only JSON matching the schema."*
User message: rubric name/version, each criterion as `key | name | weight | critical | description | met: … | partial: … | missed: …`,
then `TRANSCRIPT:` as `[mm:ss] S0: …` lines (middle-truncated at `AUDITLY_MAX_TRANSCRIPT_CHARS`, flagged `truncated`).

Output JSON schema (`strict: true`, `additionalProperties: false`, all required; `key` enum = this rubric version's criterion keys):
```json
{ "agent_speaker": string|null, "auto_fail": boolean, "summary": string,
  "items": [ { "key": string, "rating": "met|partial|missed|na", "score": integer, "rationale": string,
               "evidence": [ { "ts": string, "speaker": string|null, "quote": string } ] } ],
  "strengths": [string],
  "opportunities": [ { "text": string, "key": string|null } ],
  "misses":        [ { "text": string, "key": string|null } ] }
```
Validation after the call: every key present exactly once (missing → `missed, 0, "not addressed by model"` + warning);
`met` forced to weight, `missed`/`na` forced to 0, `partial` clamped to `[0, weight]`;
**`overall_pct = Σscore / Σweight(non-na) × 100`, recomputed server-side** — the model's arithmetic is never
trusted; evidence quotes fuzzy-matched against the transcript, unmatched quotes dropped and listed in `warnings_json`;
non-JSON reply → one retry with "Return only the JSON object", then `failed` with the first 300 chars stored.
`final_score = COALESCE(override_score, score)`; overrides re-run the same `compute_overall()`.

---

## 7. `.env` plan (`.env.example`)

```bash
# Copy to .env, fill in, then: chmod 600 .env
# Keys are read server-side and NEVER sent to the browser. /api/health reports only key_present / key_len.

# ── server ──
AUDITLY_PORT=8084                # 8081 cfbuilder, 8082 coeo, 8083 calculator are taken
AUDITLY_BIND=127.0.0.1           # 0.0.0.0 only for LAN dev without nginx
AUDITLY_DB=./auditly.db
AUDITLY_UPLOAD_DIR=./uploads
AUDITLY_MAX_UPLOAD_MB=200
AUDITLY_RETENTION_DAYS=90        # audio deleted after N days; transcripts + scorecards kept. 0 = never
AUDITLY_INSECURE_COOKIES=0       # 1 only on plain-http localhost dev
SESSION_SECRET=               # python3 -c "import secrets;print(secrets.token_urlsafe(48))"

# ── transcription ──
TRANSCRIBE_PROVIDER=deepgram  # deepgram | openai  (per-upload override in UI)
DEEPGRAM_API_KEY=
DEEPGRAM_MODEL=nova-3
OPENAI_API_KEY=               # used for OpenAI STT and (default) scoring
OPENAI_TRANSCRIBE_MODEL=whisper-1   # returns verbose_json timestamps; 25 MB file cap enforced at upload
OPENAI_API_BASE=https://api.openai.com/v1

# ── scoring ──
SCORING_PROVIDER=openai       # openai | anthropic
OPENAI_SCORING_MODEL=gpt-4o-mini    # confirm current id in OpenAI docs when you set it
ANTHROPIC_API_KEY=            # optional
ANTHROPIC_MODEL=claude-sonnet-5     # confirm current id in Anthropic docs when you set it
SCORING_TEMPERATURE=0
SCORING_MAX_TOKENS=4000
AUDITLY_MAX_TRANSCRIPT_CHARS=120000
AUDITLY_WORKERS=1
AUDITLY_SESSION_HOURS=12

# ── safety interlocks ──
AUDITLY_ALLOW_SPEND=0            # must be exactly 1 for any provider call; --demo works without it
AUDITLY_DEMO=0                   # 1 = seed fixtures + stub providers
```

---

## 8. Front end (`auditly.html`)

Header: brand · **version chip** (`<button class="chip" id="verBtn">v__APP_VERSION__</button>` → `showLog()` opens an
in-page modal with two tabs: **Changelog** — every `## [x.y.z] - YYYY-MM-DD` entry with its bullets from
`/api/changelog`, newest first; **Plan** — `docs/PLAN.md` from `/api/plan` rendered by the same ~15-line
markdown-subset renderer as CFB's `showLog()`) · theme Auto/Light/Dark `.seg` · user/logout.

Tabs (`.seg` control): **Upload · Calls · Guidelines · Settings**

1. **Upload** — drop zone (+ click), multiple files; per-file: rubric select (default = most recent version of
   default rubric), STT provider select (default from `/api/health`), optional Agent name / Call ref; progress
   bar via `XMLHttpRequest.upload.onprogress`; on 201 → switches to Calls with the job highlighted.
2. **Calls** — table: uploaded, filename, agent, rubric vN, status pill (queued / transcribing / scoring /
   done / failed w/ retry), overall % badge coloured `--good ≥90 / --warn 70–89 / --bad <70`, AUTO-FAIL tag.
   Click → **Scorecard view**: left = overall % ring + summary + strengths; per-criterion table
   (name · weight · AI score · rating · final · ✎ override); **Opportunities** list and **Misses** list
   (each links to its criterion, hover shows evidence); right = `<audio controls preload="none">` + transcript
   pane with speaker turns (agent highlighted per `agent_speaker`), timestamps clickable to seek, "Swap agent/customer"
   button; Export PDF / CSV / JSON buttons; Rescore-with-rubric dropdown. Evidence quotes are clickable → seek audio + scroll transcript.
3. **Guidelines** — rubric list; new rubric: paste text or drop `.txt/.md/.docx` → "Extract criteria" → editable
   grid (name, description, weight, met/partial/missed guidance, critical checkbox) with a live **"Total: 100 / 100"**
   indicator that blocks Save while ≠ 100 and offers "Normalise to 100"; version history read-only.
4. **Settings** — read-only health card: providers key present/length, ffmpeg found, default models, DB path,
   retention; "Add users via `python3 auditly_host.py --add-user`" hint.

Invariants: no external assets; CSP nonce; in-page modals only; WCAG-AA token pairs in both themes.

---

## 9. Milestones

Build order rule: **write the Demo providers before the real ones** so worker + UI are finished end-to-end with no keys;
real clients are then a swap behind `transcriber_for()` / `scorer_for()`. M3 and M4 are independent of each other.

**M1 — Skeleton, auth, version chip + plan tab, demo mode** (files: `core.py`, `auditly_host.py`, `schema.sql`,
`auditly.html`, `demo_data.py`, `fixtures/*`, `docs/CHANGELOG.md` v0.1.0, `docs/PLAN.md`, `README.md`, `CLAUDE.md`, `.env.example`, `.gitignore`)
- Copy coeo `core.py`/auth/CSP/`parse_changelog`; `/api/health`, `/api/changelog`, `/api/plan`; header + chip + changelog/plan modal + theme; `--init-db`, `--add-user`, `--demo`.
- Test: server boots, login works, `/api/changelog[0].version == "0.1.0"` and equals `README.md:3`, HTML contains no `http(s)://` asset URL.

**M2 — Upload + job queue + audio relay** (`auditly_host.py` upload route, worker thread, `uploads/`)
- Raw-body upload with cap, sha256, `job(queued)`; Calls tab polling; Range relay; retention sweep; delete.
- Test: 3 MB fake upload → 201; > cap → 413; job reaches `failed` cleanly when provider stub raises.

**M3 — Transcription** (`llm.py`, `transcribe.py`)
- Deepgram + OpenAI clients behind `Transcriber` interface, transcript pane + seek; `job.progress` text ("Uploading to Deepgram…").
- Test: Deepgram JSON fixture → N utterances with speaker ints; OpenAI fixture → speakers NULL; `provider=openai` + 26 MB `Content-Length` → 400 mentioning 25 MB.

**M4 — Guidelines / rubric** (`rubric.py`, Guidelines tab)
- `.docx/.txt/.md` extraction, LLM criteria proposal, editable grid, weights==100 gate, immutable versions.
- Test: docx fixture → text; weights 95 → 400 with message; save v1 then v2, v1 unchanged.

**M5 — Scoring + scorecard + overrides + export** (`score.py`, `pdfgen.py`, Scorecard view)
- Prompt, strict schema, validation/clamp/recompute, opportunities/misses, override PATCH, PDF/CSV, rescore.
- Test: fixture LLM response with out-of-range score → clamped, overall recomputed; override changes overall; PDF starts with `%PDF`.

**M6 — Deploy + docs** (`deploy/*`, `docs/RUNBOOK.md`, `docs/VERIFY.md`, `docs/PRD_Auditly.md`)
- nginx 8084 + `client_max_body_size 250m`, systemd unit, self-verifying `install.sh` (asserts `/.env` → 404), runbook.

Version bumps: each milestone that changes user-visible behaviour after v0.1.0 gets its own `## [0.x.0]`
entry (minor for features, patch for fixes); v1.0.0 when M6 is installed and the QA team has scored 10 real calls.

---

## 10. Risks

| Risk | Mitigation |
|---|---|
| OpenAI STT 25 MB cap / no diarization | Deepgram default (D1); refused at upload with specific message (D10); `agent_speaker: null` → plain turns |
| Space in the project path (was `L1 Support QA`; gone since the 0.30.0 rename to `Auditly`) | every script quotes `"$SELF"`; systemd unit quotes `WorkingDirectory`; tests use `os.path.join` |
| Long jobs (10-min call ≈ 30–90 s STT + 20 s scoring) | async queue + status polling; never block the upload response |
| LLM JSON drift / hallucinated quotes | strict `json_schema`; server recomputes overall; quotes fuzzy-verified against transcript |
| Wrong speaker→role guess | heuristic + one-click role toggle; scoring prompt receives roles as labelled |
| Weights not summing to 100 | rejected at save (400); UI blocks Save; "Normalise" helper |
| Customer PII leaves the machine | stated in README/PRD; retention sweep; `AUDITLY_ALLOW_SPEND` interlock; audit log of every play/export |
| Cost runaway | `AUDITLY_ALLOW_SPEND=1` required; per-job token/duration logged in `job` |
| `pkill -f serve\.py` incident repeat | host file named `auditly_host.py`; systemd unit, not nohup |
| Python 3.13 stdlib changes (`cgi` removed) | raw-body upload (D3), no `cgi` import |

---

## 11. Verification

```bash
cd "~/Auditly"
python3 -c "import sys; print(sys.version)"          # confirm ≥3.10, note whether 3.13 (cgi absent)
python3 tests/test_auditly.py                            # boots server on temp DB, providers stubbed, no keys, ~60 checks
python3 auditly_host.py --demo                           # seeds fixture call + rubric + scorecard; open http://127.0.0.1:8084/
grep -n 'src="http\|href="http' auditly.html    # must return nothing
```
Browser by hand (no browser on host — publish via nginx or port-forward, and **say so if not done**): upload a
real ~1-min mp3 with `AUDITLY_ALLOW_SPEND=1` → watch status → scorecard renders → override one criterion → PDF
export → click version chip → changelog shows `0.1.0 — 2026-09-01`.
Deploy: `sudo deploy/install.sh` then `curl -sI http://127.0.0.1:8084/.env | head -1` → 404.


## 12. Deferred to v1.1 (explicitly out of v1)

PDF guideline extraction · ffmpeg transcode/chunking for OpenAI STT · per-agent trend dashboards · calibration
(two reviewers score the same call) · bulk CSV export of all scorecards · webhook/PBX auto-ingest of recordings.
