#!/usr/bin/env python3
"""In-app notifications (0.51.0): shared event rows, per-user preferences and read state.

Events are written by the host (coaching, disputes, an audit submitted) and the worker (a job done
or failed) at the moment they happen; nothing here sends mail. "Agent below target" is
dashboard.agent_standing() -- the Dashboard's own rows, per-recording pick, value, default range,
pass target and LEADER_MIN_N -- so no second threshold exists anywhere. Like core.audit(), every
function here runs plain statements on the connection it is handed and never opens a transaction
of its own (the demo seeder calls it inside BEGIN IMMEDIATE; the worker on its own connection).
"""
import json
from datetime import datetime, timedelta, timezone

import core
import dashboard

KINDS = (   # id, label, description, default on
    ("coaching.scheduled", "Coaching session scheduled", "A session got a date and time.", True),
    ("coaching.changed", "Coaching session changed", "Rescheduled (old and new time), unscheduled or cancelled.", True),
    ("dispute.raised", "Dispute raised", "A QA recorded an agent's dispute on a scorecard or a criterion.", True),
    ("dispute.resolved", "Dispute resolved", "A dispute was upheld, rejected or withdrawn.", True),
    ("agent.failing", "Agent below target",
     "An agent's average over the Dashboard's default range fell below the pass target with at least %d scored calls;"
     " at most once per agent per week." % dashboard.LEADER_MIN_N, True),
    ("job.failed", "Processing failed", "A call could not be transcribed or scored.", True),
)
KIND_IDS = tuple(k[0] for k in KINDS)
KEEP_DAYS = 90
FEED_MAX = 100


def kinds_for(off):
    """The registry as the page shows it, with this person's muted kinds switched off."""
    off = set(off or ())
    return [{"id": k, "label": label, "description": desc, "on": k not in off} for k, label, desc, _ in KINDS]


def emit(db, kind, title, body=None, target_kind=None, target_id=None, agent_name=None,
         actor=None, recording_id=None, dedupe_key=None):
    """One shared event. Returns the new id, or None when dedupe_key already exists."""
    if kind not in KIND_IDS:
        raise ValueError("unknown notification kind %r" % kind)
    nid = core.new_id()
    cur = db.execute("INSERT OR IGNORE INTO notification (id,kind,at,title,body,target_kind,target_id,agent_name,"
                     "actor_email,recording_id,dedupe_key) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (nid, kind, core.now(), (title or "")[:160], (body[:600] if body else None), target_kind, target_id,
                      agent_name, core.field(actor, "email") or None, recording_id, dedupe_key))
    return nid if cur.rowcount else None


def prefs_get(db, user_id):
    """The kinds this person muted (a set); anything not listed is on."""
    r = db.execute("SELECT off_json FROM notification_pref WHERE user_id=?", (user_id,)).fetchone()
    try:
        off = json.loads(r["off_json"]) if r else []
    except (ValueError, TypeError):
        off = []
    return set(k for k in off if isinstance(k, str) and k in KIND_IDS)


def prefs_set(db, user_id, off):
    off = sorted(set(off or ()))
    bad = [k for k in off if k not in KIND_IDS]
    if bad:
        raise ValueError("Unknown notification kind: %s" % ", ".join(bad))
    db.execute("INSERT INTO notification_pref (user_id,off_json,updated_at) VALUES (?,?,?)"
               " ON CONFLICT(user_id) DO UPDATE SET off_json=excluded.off_json, updated_at=excluded.updated_at",
               (user_id, json.dumps(off), core.now()))
    return set(off)


def _enabled(db, user):
    off = prefs_get(db, core.field(user, "id"))
    return [k for k in KIND_IDS if k not in off], off


def unread_count(db, user, on=None):
    """Unread lines of the kinds this person has on, from their account's creation onwards --
    someone added today does not open to last month's red badge."""
    if on is None:
        on, _ = _enabled(db, user)
    if not on:
        return 0
    # The open-access account (…@local) is created on its first visit, after the demo seed has already
    # written events, so for it the bound would hide everything at boot: count from the beginning.
    since = "" if (core.field(user, "email") or "").endswith("@local") else (core.field(user, "created_at") or "")
    marks = ",".join("?" * len(on))
    return db.execute("SELECT COUNT(*) FROM notification n LEFT JOIN notification_read r"
                      " ON r.notification_id=n.id AND r.user_id=? WHERE n.kind IN (%s) AND r.read_at IS NULL AND n.at >= ?" % marks,
                      [core.field(user, "id")] + on + [since]).fetchone()[0]


def feed(db, user, limit=30):
    """(items newest first with this person's read_at, unread count, muted kinds)."""
    on, off = _enabled(db, user)
    if not on:
        return [], 0, off
    try:
        limit = max(1, min(FEED_MAX, int(limit or 30)))
    except (TypeError, ValueError):
        limit = 30
    marks = ",".join("?" * len(on))
    rows = [dict(r) for r in db.execute(
        "SELECT n.*, r.read_at FROM notification n LEFT JOIN notification_read r ON r.notification_id=n.id AND r.user_id=?"
        " WHERE n.kind IN (%s) ORDER BY n.at DESC, n.rowid DESC LIMIT ?" % marks, [core.field(user, "id")] + on + [limit])]
    return rows, unread_count(db, user, on), off


def mark_read(db, user_id, ids=None, all_=False):
    """Read marks are inserted from the notification table itself, so an id that does not exist is ignored."""
    now = core.now()
    if all_:
        return db.execute("INSERT OR IGNORE INTO notification_read (user_id,notification_id,read_at)"
                          " SELECT ?, id, ? FROM notification", (user_id, now)).rowcount
    n = 0
    for i in list(ids or ())[:200]:
        if isinstance(i, str) and i:
            n += db.execute("INSERT OR IGNORE INTO notification_read (user_id,notification_id,read_at)"
                            " SELECT ?, id, ? FROM notification WHERE id=?", (user_id, now, i)).rowcount
    return n


def failing_rule(target):
    """What the Settings pane states about agent.failing -- read from the same constants the check uses."""
    return {"target": target, "min_n": dashboard.LEADER_MIN_N, "period": "week"}


def check_failing(db, agent_name, kind="real", recording_id=None, actor=None, target=None):
    """Emit agent.failing when the Dashboard's own numbers put this agent below target; once per agent per
    Dashboard week (the dedupe key carries the range start). Returns the id emitted, else None."""
    name = (agent_name or "").strip()
    if not name:
        return None
    if target is None:
        target = dashboard.target_of(core.cfg().get("AUDITLY_TARGET_PCT"))
    rows = [dict(r) for r in db.execute(dashboard.ROWS_SQL % " AND r.kind=?", (kind,)).fetchall()]
    st = dashboard.agent_standing(rows, name, target, datetime.now(timezone.utc).date())
    if not st["failing"]:
        return None
    body = "Average %.1f%% over %d scored call%s since %s; the target is %d%%.%s" % (
        st["avg"], st["n"], "" if st["n"] == 1 else "s", st["from"], target, "" if kind == "real" else " (%s calls)" % kind)
    return emit(db, "agent.failing", "%s is below target" % name, body, "agent", name, name, actor, recording_id,
                "agent.failing:%s:%s:%s" % (kind, name.lower(), st["from"]))


def prune(db, days=KEEP_DAYS):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()
    return db.execute("DELETE FROM notification WHERE at < ?", (cutoff,)).rowcount
