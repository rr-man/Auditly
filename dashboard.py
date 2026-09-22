#!/usr/bin/env python3
"""Dashboard aggregation: rows from the scorecard/recording/review join -> trend series.

Pure functions, no HTTP and no SQL -- except the one query string ROWS_SQL, which the host runs
for /api/dashboard and notify.check_failing() runs for "agent below target", so that rule reads
exactly the Dashboard's rows. The tests import this module like score.py.

Rules the numbers follow:
  * One value per recording. A call can have several done scorecards (rescore, "Audit
    again", "Transcribe again"); the one with a submitted review wins, else the newest.
  * The value is review.final_pct when a review is submitted, else scorecard.overall_pct.
    Nothing here recomputes a percentage from items -- score.compute_overall() did that.
  * Score trends are bucketed by the call (recording.uploaded_at); QA activity by when
    the review was submitted / the coaching marked delivered.
  * Which recording kinds are counted (real by default; demo/test/all on request) is
    decided by the caller's SQL, not here.
  * Times in the database are UTC ISO strings; `tz` is the browser's
    getTimezoneOffset() in minutes (positive west of UTC) so buckets are local days.
"""
import json
from datetime import date, datetime, timedelta, timezone

PERIODS = ("day", "week", "month", "quarter", "year")
SOURCES = ("scored", "audited")
SENTIMENTS = ("negative", "neutral", "positive")
MAX_BUCKETS = 800
DEFAULT_TARGET = 90
BANDS = (("lt60", 0, 60), ("60_79", 60, 80), ("80_89", 80, 90), ("ge90", 90, 101))   # [lo, hi)
LEADER_MIN_N = 3

# Every done scorecard with its call and review, the raw material of aggregate(). The %s takes the
# caller's kind filter (" AND r.kind=?" or ""). Owned here since 0.51.0 so the host and notify.py
# cannot drift apart.
ROWS_SQL = ("SELECT s.id sc_id, s.recording_id, s.overall_pct, s.applicable_weight, s.auto_fail, s.created_at sc_created_at,"
            " s.sentiment_json, s.call_reasons_json, r.agent_name, r.qa_name, r.uploaded_at,"
            " v.status rv_status, v.final_pct, v.submitted_at, v.coached_at"
            " FROM scorecard s JOIN job j ON j.id=s.job_id AND j.status='done'"
            " JOIN recording r ON r.id=s.recording_id AND r.deleted_at IS NULL%s"
            " LEFT JOIN review v ON v.scorecard_id=s.id")


def target_of(v):
    """AUDITLY_TARGET_PCT -> int in 1..100, else the default."""
    try:
        t = int(str(v).strip())
    except (TypeError, ValueError):
        return DEFAULT_TARGET
    return t if 1 <= t <= 100 else DEFAULT_TARGET


def previous_range(from_d, to_d):
    """The range of the same length that ends the day before from_d."""
    n = (to_d - from_d).days + 1
    return from_d - timedelta(days=n), from_d - timedelta(days=1)
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# ── time ──────────────────────────────────────────────────────────────────
def clamp_tz(v):
    try:
        tz = int(str(v).strip())
    except (TypeError, ValueError):
        return 0
    return max(-840, min(840, tz))


def local_dt(iso, tz_min):
    """UTC ISO string -> naive local datetime for the given browser offset."""
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt - timedelta(minutes=tz_min)


def local_date(iso, tz_min):
    try:
        return local_dt(iso, tz_min).date()
    except (TypeError, ValueError):
        return None


def parse_date(s):
    """YYYY-MM-DD -> date; ValueError otherwise."""
    if not isinstance(s, str) or len(s) != 10:
        raise ValueError("Dates must be YYYY-MM-DD.")
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise ValueError("Dates must be YYYY-MM-DD.")


def bucket_start(d, period):
    if period == "day":
        return d
    if period == "week":
        return d - timedelta(days=d.weekday())            # Monday
    if period == "month":
        return d.replace(day=1)
    if period == "quarter":
        return d.replace(month=((d.month - 1) // 3) * 3 + 1, day=1)
    if period == "year":
        return d.replace(month=1, day=1)
    raise ValueError("period must be one of %s." % ", ".join(PERIODS))


def bucket_key(d, period):
    """Sortable text key. Week = the Monday's date (sidesteps the ISO-week year trap),
    quarter = 2026-Q3."""
    s = bucket_start(d, period)
    if period in ("day", "week"):
        return s.isoformat()
    if period == "month":
        return "%04d-%02d" % (s.year, s.month)
    if period == "quarter":
        return "%04d-Q%d" % (s.year, (s.month - 1) // 3 + 1)
    return "%04d" % s.year


def bucket_label(key, period):
    if period == "day":
        d = date.fromisoformat(key)
        return "%d %s" % (d.day, MONTHS[d.month - 1])
    if period == "week":
        d = date.fromisoformat(key)
        return "Wk %d %s" % (d.day, MONTHS[d.month - 1])
    if period == "month":
        return "%s %s" % (MONTHS[int(key[5:7]) - 1], key[:4])
    if period == "quarter":
        return "%s %s" % (key[5:], key[:4])
    return key


def next_start(d, period):
    if period == "day":
        return d + timedelta(days=1)
    if period == "week":
        return d + timedelta(days=7)
    if period == "month":
        return date(d.year + (d.month // 12), d.month % 12 + 1, 1)
    if period == "quarter":
        m = d.month + 3
        return date(d.year + (m - 1) // 12, (m - 1) % 12 + 1, 1)
    return date(d.year + 1, 1, 1)


def bucket_range(from_d, to_d, period):
    """Every bucket key from the one holding from_d to the one holding to_d, inclusive,
    empty ones included so a chart keeps its x-axis. ValueError above MAX_BUCKETS."""
    keys = []
    d = bucket_start(from_d, period)
    while d <= to_d:
        keys.append(bucket_key(d, period))
        if len(keys) > MAX_BUCKETS:
            raise ValueError("That range has more than %d %ss; pick a coarser period." % (MAX_BUCKETS, period))
        d = next_start(d, period)
    return keys


def default_range(period, today):
    if period == "day":
        return today - timedelta(days=29), today
    if period == "week":
        return bucket_start(today, "week") - timedelta(weeks=12), today
    if period == "month":
        m = today.month - 11
        return date(today.year + (m - 1) // 12, (m - 1) % 12 + 1, 1), today
    if period == "quarter":
        q = bucket_start(today, "quarter")
        for _ in range(7):
            m = q.month - 3
            q = date(q.year + (m - 1) // 12, (m - 1) % 12 + 1, 1)
        return q, today
    return date(today.year - 4, 1, 1), today


# ── rows ──────────────────────────────────────────────────────────────────
def _same(a, b):
    return (a or "").strip().lower() == (b or "").strip().lower()


def pick_per_recording(rows):
    """One row per recording_id: a submitted review beats no review, then newest wins."""
    best = {}
    for r in rows:
        rid = r.get("recording_id")
        rank = (1 if r.get("rv_status") == "submitted" else 0,
                r.get("submitted_at") or "", r.get("sc_created_at") or "")
        cur = best.get(rid)
        if cur is None or rank > cur[0]:
            best[rid] = (rank, r)
    return [v[1] for v in best.values()]


def pipeline(rows, from_d, to_d, tz=0, agent=None, qa=None):
    """The Flow's steps as counts for the Dashboard strip. rows: one per recording with uploaded_at,
    agent_name, qa_name and 0/1 flags transcribed, scored, audited, coached. Filtered by the upload's
    local date and the agent/QA scope; awaiting_audit = scored but not audited."""
    out = {"recorded": 0, "transcribed": 0, "scored": 0, "awaiting_audit": 0, "audited": 0, "coached": 0}
    for r in rows:
        d = local_date(r.get("uploaded_at") or "", tz)
        if d is None or d < from_d or d > to_d:
            continue
        if (agent and not _same(r.get("agent_name"), agent)) or (qa and not _same(r.get("qa_name"), qa)):
            continue
        out["recorded"] += 1
        if r.get("transcribed"):
            out["transcribed"] += 1
        if r.get("scored"):
            out["scored"] += 1
            if not r.get("audited"):
                out["awaiting_audit"] += 1
        if r.get("audited"):
            out["audited"] += 1
        if r.get("coached"):
            out["coached"] += 1
    return out


def _value(r):
    """The call's score for averaging: the final QA score where audited, else the AI score.
    None when every criterion was 'na' (applicable_weight 0): nothing was scored, and a
    0.0 must not drag an average down."""
    if "applicable_weight" in r and not (r.get("applicable_weight") or 0):
        return None
    if r.get("rv_status") == "submitted" and r.get("final_pct") is not None:
        return float(r["final_pct"])
    return float(r.get("overall_pct") or 0)


def _avg(vals):
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def _series(rows, keys):
    by = {k: [] for k in keys}
    for r in rows:
        if r["_key"] in by:
            by[r["_key"]].append(r["_value"])
    return [{"key": k, "n": len(by[k]), "avg": _avg(by[k])} for k in keys]


def _loads(s, default):
    try:
        return json.loads(s) if s else default
    except ValueError:
        return default


def _pct(num, den):
    return round(num * 100.0 / den, 1) if den else None


def _delta(a, b):
    return round(a - b, 1) if a is not None and b is not None else None


def executive(rows_f, items, disputes, sessions, target, now_iso, tz):
    """The executive layer over the filtered per-recording rows: KPIs against the target, score bands,
    sentiment shift, per-criterion loss, disputes and coaching health. items: [{sc_id, key, name, weight,
    rating, final}] for any scorecard (filtered here); disputes: [{sc_id, status}]; sessions:
    [{agent_name, status, scheduled_at, done_at}] already narrowed to the agent filter by the caller."""
    ids = {r["sc_id"] for r in rows_f}
    vals = [r["_value"] for r in rows_f if r.get("_value") is not None]
    audited = [r for r in rows_f if r.get("rv_status") == "submitted"]
    coached = [r for r in audited if r.get("coached_at")]
    kpis = {"target": target, "avg_pct": _avg(vals), "scored": len(vals),
            "pass_rate": _pct(sum(1 for v in vals if v >= target), len(vals)),
            "auto_fail_rate": _pct(sum(1 for r in rows_f if r.get("auto_fail")), len(rows_f)),
            "audit_coverage": _pct(len(audited), len(rows_f)),
            "coaching_coverage": _pct(len(coached), len(audited))}
    bands = {k: 0 for k, _, _ in BANDS}
    for v in vals:
        for k, lo, hi in BANDS:
            if lo <= v < hi:
                bands[k] += 1
                break
    shift = {"improved": 0, "same": 0, "worsened": 0, "unclassified": 0}
    order = {"negative": 0, "neutral": 1, "positive": 2}
    for r in rows_f:
        s = _loads(r.get("sentiment_json"), None)
        a, b = (order.get(s.get("start")), order.get(s.get("end"))) if isinstance(s, dict) else (None, None)
        if a is None or b is None:
            shift["unclassified"] += 1
        else:
            shift["improved" if b > a else "worsened" if b < a else "same"] += 1
    crit = {}
    for it in items or []:
        if it.get("sc_id") not in ids:
            continue
        c = crit.setdefault(it.get("key"), {"key": it.get("key"), "name": it.get("name"), "n": 0, "met": 0, "partial": 0,
                                             "missed": 0, "na": 0, "_got": 0, "_basis": 0})
        c["n"] += 1
        rating = it.get("rating") if it.get("rating") in ("met", "partial", "missed", "na") else "partial"
        c[rating] += 1
        if rating != "na":
            w = int(it.get("weight") or 0)
            got = it.get("final")
            got = int(got) if got is not None else 0
            c["_got"] += max(0, min(w, got))
            c["_basis"] += w
    by_criterion = []
    for c in crit.values():
        got, basis, applicable = c.pop("_got"), c.pop("_basis"), c["n"] - c["na"]
        c["attainment_pct"] = _pct(got, basis) if basis else None
        c["lost_points"] = round((basis - got) / applicable, 1) if applicable and basis else 0.0   # per applicable call
        by_criterion.append(c)
    by_criterion.sort(key=lambda c: ((c["attainment_pct"] if c["attainment_pct"] is not None else 101), c["name"] or ""))
    dsp = {"open": 0, "upheld": 0, "rejected": 0, "withdrawn": 0}
    for d in disputes or []:
        if d.get("sc_id") in ids and d.get("status") in dsp:
            dsp[d["status"]] += 1
    resolved = dsp["upheld"] + dsp["rejected"]
    dsp["total"] = sum(dsp.values())
    dsp["upheld_rate"] = _pct(dsp["upheld"], resolved)
    dsp["disputed_calls"] = len({d["sc_id"] for d in disputes or [] if d.get("sc_id") in ids})
    days = []
    for r in coached:
        a, b = local_date(r.get("submitted_at") or "", tz), local_date(r.get("coached_at") or "", tz)
        if a and b and b >= a:
            days.append((b - a).days)
    coaching = {"sessions_upcoming": sum(1 for s in sessions or [] if s.get("status") == "scheduled" and (s.get("scheduled_at") or "") >= (now_iso or "")),
                "sessions_planned": sum(1 for s in sessions or [] if s.get("status") == "planned"),
                "sessions_done": sum(1 for s in sessions or [] if s.get("status") == "done"),
                "avg_days_audit_to_coached": round(sum(days) / len(days), 1) if days else None,
                "awaiting_coaching": len(audited) - len(coached)}
    return kpis, bands, shift, by_criterion, dsp, coaching


def leaders(by_agent, prev_by_agent, min_n=LEADER_MIN_N):
    """Top and bottom agents by average (at least min_n calls), each with the change since the previous range."""
    prev = {a["name"]: a for a in prev_by_agent or []}
    rows = [dict(a, delta=_delta(a.get("avg"), (prev.get(a["name"]) or {}).get("avg"))) for a in by_agent
            if a.get("avg") is not None and a.get("n", 0) >= min_n]
    rows.sort(key=lambda a: (-a["avg"], a["name"].lower()))
    keep = ("name", "n", "avg", "delta", "auto_fails", "coached", "audited")
    top = [{k: a.get(k) for k in keep} for a in rows[:3]]
    bottom = [{k: a.get(k) for k in keep} for a in rows[-3:][::-1] if a not in rows[:3]] if len(rows) > 3 else []
    return {"top": top, "bottom": bottom, "min_n": min_n, "ranked": len(rows)}


def agent_standing(rows, agent, target, today, period="week", tz=0, min_n=LEADER_MIN_N):
    """One agent's Dashboard numbers over the default range of `period`: {n, avg, from, to, target, failing}.
    failing = at least min_n scored calls and an average below the target -- the same rows, per-recording
    pick, value and range aggregate() shows, so no second threshold exists anywhere (0.51.0)."""
    d_from, d_to = default_range(period, today)
    out = aggregate(rows, period, d_from, d_to, tz=tz, agent=agent, target=target, with_previous=False)
    a = next((x for x in out["by_agent"] if _same(x["name"], agent)), None)
    n, avg = (a["n"], a["avg"]) if a else (0, None)
    return {"n": n, "avg": avg, "from": d_from.isoformat(), "to": d_to.isoformat(), "target": target,
            "failing": bool(avg is not None and n >= min_n and avg < target)}


def aggregate(rows, period, from_d, to_d, tz=0, source="scored", agent=None, qa=None,
              items=None, disputes=None, sessions=None, target=DEFAULT_TARGET, now_iso=None, with_previous=True):
    """rows: dicts with sc_id, recording_id, overall_pct, auto_fail, sc_created_at,
    sentiment_json, call_reasons_json, agent_name, qa_name, uploaded_at, rv_status,
    final_pct, submitted_at, coached_at. Raises ValueError for a bad period/range.
    items / disputes / sessions / target feed the executive layer (see executive()); with_previous
    adds the same totals for the preceding range of equal length and the deltas."""
    if period not in PERIODS:
        raise ValueError("period must be one of %s." % ", ".join(PERIODS))
    if source not in SOURCES:
        raise ValueError("source must be scored or audited.")
    if from_d > to_d:
        raise ValueError("The from date is after the to date.")
    keys = bucket_range(from_d, to_d, period)
    key_set = set(keys)

    site = []
    for r in pick_per_recording(rows):
        d = local_date(r.get("uploaded_at") or "", tz)
        if d is None or d < from_d or d > to_d:
            continue
        if source == "audited" and r.get("rv_status") != "submitted":
            continue
        r = dict(r, _key=bucket_key(d, period), _value=_value(r))
        site.append(r)
    rows_f = [r for r in site
              if (not agent or _same(r.get("agent_name"), agent)) and (not qa or _same(r.get("qa_name"), qa))]

    def name_groups(field, rs):
        out = {}
        for r in rs:
            out.setdefault((r.get(field) or "").strip() or "(unassigned)", []).append(r)
        return out

    by_agent_rows = name_groups("agent_name", rows_f)
    by_qa_rows = name_groups("qa_name", rows_f)

    by_agent = []
    for name, rs in sorted(by_agent_rows.items(), key=lambda kv: kv[0].lower()):
        by_agent.append({"name": name, "n": len(rs), "avg": _avg([r["_value"] for r in rs]),
                         "audited": sum(1 for r in rs if r.get("rv_status") == "submitted"),
                         "coached": sum(1 for r in rs if r.get("coached_at")),
                         "auto_fails": sum(1 for r in rs if r.get("auto_fail")),
                         "last_at": max(r.get("uploaded_at") or "" for r in rs) or None})
    by_qa = []
    for name, rs in sorted(by_qa_rows.items(), key=lambda kv: kv[0].lower()):
        by_qa.append({"name": name, "n": len(rs), "avg": _avg([r["_value"] for r in rs]),
                      "audits": sum(1 for r in rs if r.get("rv_status") == "submitted"),
                      "coached": sum(1 for r in rs if r.get("coached_at")),
                      "pending": sum(1 for r in rs if r.get("rv_status") != "submitted")})

    # QA activity: when the audit was submitted / the coaching delivered, per QA per bucket.
    # Dated by the submission, not the upload: a call uploaded before the range and audited
    # inside it is work done inside the range, so start from the unfiltered per-recording
    # rows (agent/QA narrowed, not date-narrowed) and let the bucket key do the date test.
    act_rows = [r for r in pick_per_recording(rows)
                if (not agent or _same(r.get("agent_name"), agent)) and (not qa or _same(r.get("qa_name"), qa))]
    act = {}
    for r in act_rows:
        name = (r.get("qa_name") or "").strip() or "(unassigned)"
        for col, field in (("submitted_at", "audits"), ("coached_at", "coached")):
            if not r.get(col):
                continue
            d = local_date(r[col], tz)
            if d is None:
                continue
            k = bucket_key(d, period)
            if k not in key_set:
                continue
            slot = act.setdefault((k, name), {"key": k, "qa": name, "audits": 0, "coached": 0})
            slot[field] += 1
    activity = sorted(act.values(), key=lambda a: (a["key"], a["qa"].lower()))

    sentiment = {"start": dict.fromkeys(SENTIMENTS, 0), "end": dict.fromkeys(SENTIMENTS, 0),
                 "overall": dict.fromkeys(SENTIMENTS, 0), "unclassified": 0}
    reasons = {}
    for r in rows_f:
        s = _loads(r.get("sentiment_json"), None)
        if isinstance(s, dict) and any(s.get(k) in SENTIMENTS for k in ("start", "end", "overall")):
            for k in ("start", "end", "overall"):
                if s.get(k) in SENTIMENTS:
                    sentiment[k][s[k]] += 1
        else:
            sentiment["unclassified"] += 1
        for x in _loads(r.get("call_reasons_json"), []) or []:
            if isinstance(x, dict) and x.get("category"):
                reasons[x["category"]] = reasons.get(x["category"], 0) + 1
    reasons = [{"category": k, "n": n} for k, n in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))]

    audited = sum(1 for r in rows_f if r.get("rv_status") == "submitted")
    totals = {"calls": len(rows_f), "avg_pct": _avg([r["_value"] for r in rows_f]),
              "audited": audited, "coached": sum(1 for r in rows_f if r.get("coached_at")),
              "auto_fails": sum(1 for r in rows_f if r.get("auto_fail")),
              "pending_audit": len(rows_f) - audited,
              "site_calls": len(site), "site_avg_pct": _avg([r["_value"] for r in site]),
              "agents": len(by_agent_rows), "qas": len(by_qa_rows)}

    kpis, bands, shift, by_criterion, dsp, coaching = executive(rows_f, items, disputes, sessions, target, now_iso, tz)
    out = {"period": period, "from": from_d.isoformat(), "to": to_d.isoformat(), "tz": tz, "source": source,
           "scope": {"agent": agent or None, "qa": qa or None},
           "totals": totals,
           "buckets": [{"key": k, "label": bucket_label(k, period)} for k in keys],
           "series": {"site": _series(site, keys),
                      "by_agent": {n: _series(rs, keys) for n, rs in by_agent_rows.items()},
                      "by_qa": {n: _series(rs, keys) for n, rs in by_qa_rows.items()}},
           "by_agent": by_agent, "by_qa": by_qa, "activity": activity,
           "sentiment": sentiment, "reasons": reasons,
           "kpis": kpis, "bands": bands, "sentiment_shift": shift, "by_criterion": by_criterion,
           "disputes": dsp, "coaching": coaching}
    prev = None
    if with_previous:
        pf, pt = previous_range(from_d, to_d)
        try:
            prev = aggregate(rows, period, pf, pt, tz=tz, source=source, agent=agent, qa=qa, items=items,
                             disputes=disputes, sessions=None, target=target, now_iso=now_iso, with_previous=False)
        except ValueError:
            prev = None
    if prev:
        out["previous"] = {"from": prev["from"], "to": prev["to"], "totals": prev["totals"], "kpis": prev["kpis"]}
        out["deltas"] = {"calls": (totals["calls"] - prev["totals"]["calls"]),
                         "avg_pct": _delta(kpis["avg_pct"], prev["kpis"]["avg_pct"]),
                         "pass_rate": _delta(kpis["pass_rate"], prev["kpis"]["pass_rate"]),
                         "auto_fail_rate": _delta(kpis["auto_fail_rate"], prev["kpis"]["auto_fail_rate"]),
                         "audit_coverage": _delta(kpis["audit_coverage"], prev["kpis"]["audit_coverage"]),
                         "coaching_coverage": _delta(kpis["coaching_coverage"], prev["kpis"]["coaching_coverage"]),
                         "auto_fails": totals["auto_fails"] - prev["totals"]["auto_fails"]}
        out["leaders"] = leaders(by_agent, prev["by_agent"])
    else:
        out["previous"], out["deltas"], out["leaders"] = None, None, leaders(by_agent, [])
    return out
