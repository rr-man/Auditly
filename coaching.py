"""Coaching sessions: the QA groups an agent's audited calls into a session, drafts the agenda
from their reviews, schedules it, and marks it delivered (which stamps review.coached_at on every
included call). Pure helpers live here -- agenda drafting, the .ics calendar event, time parsing
and conflict detection -- so they can be unit-tested; the SQL is in auditly_host.py.

Scheduling is QA-driven: there is no mail server and agents have no login, so the invite is an
.ics download plus a mailto: link (the agent's email is optional, kept on the person row)."""

from datetime import datetime, timedelta, timezone

STATUSES = ("planned", "scheduled", "done", "cancelled")
OPEN = ("planned", "scheduled")
MIN_MINUTES, MAX_MINUTES, DEFAULT_MINUTES = 15, 180, 30


def parse_when(s, tz_min=0):
    """'2026-09-22T14:30' (browser-local, with the browser's getTimezoneOffset minutes) or an ISO
    string with an offset/Z -> aware UTC datetime. ValueError when it cannot be read."""
    if not isinstance(s, str) or not s.strip():
        raise ValueError("A date and time are required.")
    s = s.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise ValueError("The date and time must be like 2026-09-22T14:30.")
    if dt.tzinfo is None:
        try:
            tz = max(-840, min(840, int(tz_min or 0)))
        except (TypeError, ValueError):
            tz = 0
        dt = dt.replace(tzinfo=timezone.utc) + timedelta(minutes=tz)
    return dt.astimezone(timezone.utc).replace(microsecond=0, second=0)


def iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def as_dt(s):
    """Stored ISO (core.now() style) -> aware UTC datetime, or None."""
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def minutes(v):
    try:
        m = int(v)
    except (TypeError, ValueError):
        raise ValueError("Duration must be a whole number of minutes.")
    if m < MIN_MINUTES or m > MAX_MINUTES:
        raise ValueError("Duration must be between %d and %d minutes." % (MIN_MINUTES, MAX_MINUTES))
    return m


def overlaps(start, mins, others):
    """others: [(id, scheduled_at iso, duration_min)] -> the ids whose window overlaps [start, start+mins)."""
    end = start + timedelta(minutes=mins)
    hit = []
    for oid, when, dur in others:
        st = as_dt(when)
        if not st:
            continue
        en = st + timedelta(minutes=int(dur or DEFAULT_MINUTES))
        if st < end and en > start:
            hit.append(oid)
    return hit


def call_value(c):
    """One number per call for the tally: the final score where the audit is submitted, else the AI score."""
    v = c.get("final_pct")
    if v is None and c.get("review_status") != "submitted":
        v = c.get("overall_pct")
    return v


def summarise(calls, target=90):
    """Add up a selection of calls: [{final_pct, overall_pct, review_status, auto_fail, open_disputes,
    misses:[...], items:[{key, name, weight, final, rating}]}] -> totals the QA sees while ticking
    and on the session. Points lost per criterion count only applicable (non-na) ratings."""
    vals = [call_value(c) for c in calls or []]
    vals = [float(v) for v in vals if v is not None]
    crit = {}
    for c in calls or []:
        for it in c.get("items") or []:
            if it.get("rating") == "na":
                continue
            k = crit.setdefault(it.get("key"), {"key": it.get("key"), "name": it.get("name"), "n": 0, "missed": 0, "partial": 0, "_lost": 0.0, "_basis": 0})
            w = int(it.get("weight") or 0)
            got = it.get("final")
            got = int(got) if got is not None else 0
            k["n"] += 1
            k["_lost"] += max(0, w - max(0, min(w, got)))
            k["_basis"] += w
            if it.get("rating") == "missed" or got == 0:
                k["missed"] += 1
            elif it.get("rating") == "partial":
                k["partial"] += 1
    by = []
    for k in crit.values():
        lost, basis = k.pop("_lost"), k.pop("_basis")
        k["lost_points"] = round(lost / k["n"], 1) if k["n"] else 0.0
        k["lost_total"] = round(lost, 1)
        k["attainment_pct"] = round((basis - lost) * 100.0 / basis, 1) if basis else None
        by.append(k)
    by.sort(key=lambda k: (-k["lost_total"], k["name"] or ""))
    return {"calls": len(calls or []), "scored": len(vals),
            "avg_pct": round(sum(vals) / len(vals), 1) if vals else None,
            "min_pct": min(vals) if vals else None, "max_pct": max(vals) if vals else None,
            "below_target": sum(1 for v in vals if v < target), "target": target,
            "auto_fails": sum(1 for c in calls or [] if c.get("auto_fail")),
            "misses": sum(len(c.get("misses") or []) for c in calls or []),
            "open_disputes": sum(int(c.get("open_disputes") or 0) for c in calls or []),
            "audited": sum(1 for c in calls or [] if c.get("review_status") == "submitted"),
            "coached": sum(1 for c in calls or [] if c.get("coached_at")),
            "by_criterion": by}


def draft_agenda(agent, calls, target=90):
    """calls: [{audit_name, uploaded_at, final_pct, overall_pct, review_status, misses:[text],
    coaching_notes, action_plan, disputes:[reason], items:[...]}] -> a plain-text agenda the QA can edit."""
    lines = ["Coaching session with %s" % (agent or "the agent"), ""]
    if not calls:
        lines.append("No calls selected yet.")
        return "\n".join(lines)
    tot = summarise(calls, target)
    head = "Calls: %d" % tot["calls"]
    if tot["avg_pct"] is not None:
        head += "  ·  average score %.1f%%" % tot["avg_pct"]
        if tot["scored"] > 1:
            head += " (lowest %.1f%%, highest %.1f%%)" % (tot["min_pct"], tot["max_pct"])
        if tot["below_target"]:
            head += "  ·  %d below the %d%% target" % (tot["below_target"], target)
    if tot["misses"]:
        head += "  ·  %d misses" % tot["misses"]
    lines.append(head)
    worst = [k for k in tot["by_criterion"] if k["lost_total"] > 0][:5]
    if worst:
        lines.append("Points lost per criterion (worst first): " + "  ·  ".join(
            "%s %s" % (k["name"], ("-%g" % k["lost_total"]) if k["lost_total"] == int(k["lost_total"]) else "-%.1f" % k["lost_total"]) for k in worst))
    lines.append("")
    for i, c in enumerate(calls, 1):
        head = "%d. %s" % (i, c.get("audit_name") or "call")
        if c.get("final_pct") is not None:
            head += " — %.1f%%" % c["final_pct"]
        elif call_value(c) is not None:
            head += " — AI %.1f%% (not audited)" % call_value(c)
        lines.append(head)
        for m in (c.get("misses") or [])[:4]:
            lines.append("   - Miss: %s" % m)
        if c.get("coaching_notes"):
            lines.append("   - Coaching: %s" % c["coaching_notes"].strip().replace("\n", " "))
        if c.get("action_plan"):
            lines.append("   - Action plan: %s" % c["action_plan"].strip().replace("\n", " "))
        for d in c.get("disputes") or []:
            lines.append("   - Open dispute: %s" % d)
        lines.append("")
    # recurring misses across the selected calls, most frequent first
    counts = {}
    for c in calls:
        for m in c.get("misses") or []:
            k = (m or "").strip().lower()
            if k:
                counts[k] = counts.get(k, 0) + 1
    rec = [k for k, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])) if n > 1]
    if rec:
        lines.append("Themes (seen on more than one call):")
        for k in rec[:5]:
            lines.append("   - %s" % k)
        lines.append("")
    lines.append("Agreed next steps:")
    lines.append("   - ")
    return "\n".join(lines)


def _ics_text(s):
    return (str(s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\n").replace("\n", "\\n"))


def _fold(line):
    """RFC 5545: lines at most 75 octets, continuation lines start with a space."""
    out, b = [], line.encode("utf-8")
    while len(b) > 75:
        cut = 75
        while cut > 0 and (b[cut] & 0xC0) == 0x80:      # do not split a UTF-8 sequence
            cut -= 1
        out.append(b[:cut].decode("utf-8"))
        b = b" " + b[cut:]
    out.append(b.decode("utf-8"))
    return "\r\n".join(out)


def ics(session, agent_email=None, organizer_email=None, url=None):
    """One VEVENT for a scheduled session; '' when it has no time."""
    start = as_dt(session.get("scheduled_at"))
    if not start:
        return ""
    end = start + timedelta(minutes=int(session.get("duration_min") or DEFAULT_MINUTES))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    title = session.get("title") or ("Coaching: %s" % (session.get("agent_name") or "agent"))
    desc = session.get("agenda") or ""
    if url:
        desc = (desc + "\n\n" if desc else "") + url
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Auditly//Coaching//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
             "BEGIN:VEVENT", "UID:%s@auditly" % session.get("id"), "DTSTAMP:%s" % stamp,
             "DTSTART:%s" % start.strftime("%Y%m%dT%H%M%SZ"), "DTEND:%s" % end.strftime("%Y%m%dT%H%M%SZ"),
             "SUMMARY:%s" % _ics_text(title), "DESCRIPTION:%s" % _ics_text(desc)]
    if session.get("location"):
        lines.append("LOCATION:%s" % _ics_text(session["location"]))
    if organizer_email:
        lines.append("ORGANIZER;CN=%s:mailto:%s" % (_ics_text(session.get("qa_name") or "QA"), organizer_email))
    if agent_email:
        lines.append("ATTENDEE;CN=%s;ROLE=REQ-PARTICIPANT;RSVP=TRUE:mailto:%s" % (_ics_text(session.get("agent_name") or "Agent"), agent_email))
    lines += ["STATUS:CONFIRMED", "END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_fold(l) for l in lines) + "\r\n"
