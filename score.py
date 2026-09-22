#!/usr/bin/env python3
"""Scoring: rubric + transcript -> prompt -> strict JSON -> validated scorecard.

The model's arithmetic is never trusted. compute_overall() is the only place an
overall percentage is calculated, and it runs again after every human override.
"""
import difflib
import re

import core
import kb

# Bump when SYSTEM, the schema or build_prompt change: an audit is only "the same
# request" (and reusable) when it was produced by the same prompt version.
PROMPT_VERSION = 8

SYSTEM = (
    "You are a strict but fair quality-assurance reviewer for a Level 1 technical support "
    "desk. You score ONE call transcript against the rubric provided.\n"
    "Rules:\n"
    "1. Use only what is in the transcript. Never assume something happened off-transcript.\n"
    "2. Score EVERY criterion -- items has one entry per criterion key, none may be left out. "
    "For each return a rating of met, partial, missed or na, and a score from 0 "
    "to that criterion's weight: met = the full weight, partial = roughly half, missed = 0, "
    "na = 0 and the criterion is excluded from the basis. missed means the behaviour was "
    "absent from the call; any attempt, however incomplete, is partial. Use na only when the "
    "criterion genuinely could not apply to this call.\n"
    "3. Every rating except na needs at least one evidence quote copied verbatim from the "
    "transcript with its [mm:ss] timestamp. Never invent or paraphrase a quote.\n"
    "4. Speakers: give every speaker label a role in speakers -- caller (the customer), agent "
    "(the live human support agent), voice_ai (an automated assistant; the voice AI that answers "
    "calls first at this desk is named {voice_ai}), or other (a third party, e.g. a second agent "
    "after a transfer) -- with the person's name when it is stated, else null. Return the live "
    "agent's label as agent_speaker (the label string such as 'S0', or null if you cannot tell). "
    "Score ONLY the live agent's handling: the voice AI's turns are context, never scored. When "
    "the voice AI did the initial greeting, judge greeting and identification criteria on the live "
    "agent's own opening when they join. Set transferred to true when the caller is handed to "
    "another person or system during the call.\n"
    "5. misses = things the rubric required that the agent did not do. opportunities = "
    "specific, actionable coaching points that would have made the call better, including "
    "on criteria that were met. strengths = what the agent did well.\n"
    "6. If a criterion marked CRITICAL is missed, set auto_fail to true. Rate a CRITICAL "
    "criterion missed only when the transcript shows no attempt at all; an incomplete attempt "
    "is partial and does not fail the call.\n"
    "7. call_summary = 2-3 plain sentences: who called and from where, what they needed, "
    "what the agent did, how it ended. No judgement there. summary = your QA verdict in 2-3 "
    "sentences: how well the call was handled, the main strength and the main gap. The two "
    "must not repeat each other.\n"
    "8. sentiment = the CUSTOMER's mood judged from the customer's own words only: at the start "
    "of the call, at the end, and overall. Each is negative, neutral or positive.\n"
    "9. call_reasons = why the customer called, as 1 to 4 entries from the fixed category list, "
    "most important first, each with a one-line plain-English detail. A call often has more than "
    "one reason; list each. Use other only when nothing fits. Never repeat a category.\n"
    "10. customer = details the CALLER states about themselves: name, company, email and phone, copied "
    "as said (null when not stated -- never guess or infer from the agent's words). reference = the ticket, "
    "case or reference number quoted on the call by EITHER party -- the agent reading one out counts -- "
    "copied exactly, digits and letters as spoken; null when none was given.\n"
    "11. Where a criterion lists explicit conditions (met: ALL of ...; na: ...), apply them literally: "
    "met only when every met-condition is observable in the live agent's turns; partial when at least "
    "one but not all is, or when a listed partial form occurs; missed when none is; na exactly when the "
    "na-condition holds and never otherwise.\n"
    "12. Return only JSON matching the schema.\n"
    "13. KNOWLEDGE BASE EXCERPTS, when present, are the desk's own reference material (procedures, "
    "product facts). Use them only to judge whether what the live agent SAID was accurate and complete "
    "-- for example a troubleshooting step given in the wrong order, or advice the procedure contradicts. "
    "They are never evidence that anything happened on the call; rules 1 and 3 still govern every rating "
    "and every quote. If the excerpts are irrelevant to this call, ignore them.\n"
    "14. call_facts = the checkable facts of the call, nothing general. identifiers: phone, extension, "
    "mac_address, ticket, account, device, address -- each copied character for character as spoken (a MAC "
    "as its hex pairs, a number as its digits), null when not stated; never infer, never tidy up. "
    "troubleshooting = the steps actually taken, in order, each as {text, result, key}: what was done, what "
    "it showed, and the key of the criterion it speaks to (null when none). discussed = what was explained, "
    "agreed or promised, each as {text, key}. outcome = one sentence on how the matter stands at the end. "
    "Be specific -- 'reseated the network cable at the wall jack; the phone re-registered' -- never "
    "'basic troubleshooting'.\n"
    "15. A criterion may carry a good sample and a bad sample: wording that earns met and wording that earns "
    "missed. Use them to calibrate the rating -- a turn that matches the good sample in substance is met, one "
    "that matches the bad sample is missed -- but they are illustrations, never required phrasing.")

# call_facts.identifiers (prompt v8): the keys the scorer fills, and how the page and the exports label them
FACT_IDS = ("phone", "extension", "mac_address", "ticket", "account", "device", "address")
FACT_LABELS = {"phone": "Phone", "extension": "Extension", "mac_address": "MAC address", "ticket": "Ticket number",
               "account": "Account", "device": "Device", "address": "Address"}
FACT_CODES = ("phone", "extension", "mac_address", "ticket", "account")   # must appear in the transcript, character for character

DEFAULT_VOICE_AI_NAME = "Emma"
ROLES = ("caller", "agent", "voice_ai", "other")

RATINGS = ("met", "partial", "missed", "na")
SENTIMENTS = ("negative", "neutral", "positive")
# Fixed so the dashboard can count them. Keys are stored; labels are for display.
REASONS = (("no_service", "No service / phone offline"),
           ("call_quality", "Call quality (audio, drops, one-way)"),
           ("voicemail", "Voicemail"),
           ("call_routing", "Call routing / auto-attendant / ring group"),
           ("provisioning", "Provisioning (new user, device, extension)"),
           ("network", "Network / internet / firewall"),
           ("billing", "Billing / invoice"),
           ("porting", "Number porting"),
           ("hardware", "Hardware fault / replacement"),
           ("account_access", "Portal or account access"),
           ("how_to", "How-to / feature question"),
           ("outage", "Outage (site or carrier)"),
           ("other", "Other"))
REASON_KEYS = tuple(k for k, _ in REASONS)
REASON_LABELS = dict(REASONS)
MAX_REASONS = 4


def format_transcript(utts, max_chars):
    """'[mm:ss] S0: text' lines. Middle-truncated so the opening and the close,
    which most rubric items depend on, always survive."""
    lines = []
    for u in utts:
        sp = "S%d" % u["speaker"] if u.get("speaker") is not None else "S?"
        lines.append("[%s] %s: %s" % (core.fmt_ts(u["start_s"]), sp, (u.get("text") or "").strip()))
    text = "\n".join(lines)
    if max_chars and len(text) > max_chars:
        half = max_chars // 2
        text = text[:half] + "\n[... transcript truncated for length ...]\n" + text[-half:]
        return text, True
    return text, False


def guidance_mode(ver, criteria):
    """'optimised' only when the version's optimisation finished AND every criterion carries all
    five opt_* fields; anything else scores with the guidance as written."""
    if (core.field(ver, "sync_status") or "") != "done" or not criteria:
        return "authored"
    for c in criteria:
        for f in ("opt_description", "opt_met", "opt_partial", "opt_missed", "opt_na"):
            if not (core.field(c, f) or "").strip():
                return "authored"
    return "optimised"


def build_prompt(rubric_name, version_no, criteria, transcript_text, diarized, voice_ai_name=None,
                 use_optimised=False, kb_excerpts=None):
    """use_optimised: read criterion.opt_* (the 'Optimise for the scorer' rules) instead of the
    authored text, falling back per field when one is empty. Works on Row or dict.
    kb_excerpts: knowledge-base hits (kb.retrieve_for) placed before the transcript; None/[] adds nothing."""
    system = SYSTEM.replace("{voice_ai}", (voice_ai_name or DEFAULT_VOICE_AI_NAME).strip() or DEFAULT_VOICE_AI_NAME)

    def g(c, opt, authored):
        v = core.field(c, opt) if use_optimised else None
        return (v if v else core.field(c, authored)) or ""

    parts = ["RUBRIC: %s (version %d). Weights sum to 100. Score all %d criteria: %s.%s\n"
             % (rubric_name, version_no, len(criteria), ", ".join(c["key"] for c in criteria),
                " Guidance is written as decision rules; apply rule 11 literally." if use_optimised else "")]
    for c in criteria:
        line = ("- key=%s | %s | weight %d%s\n  %s\n  met: %s\n  partial: %s\n  missed: %s"
                % (c["key"], c["name"], c["weight"],
                   " | CRITICAL" if c["critical"] else "",
                   g(c, "opt_description", "description"), g(c, "opt_met", "guidance_met"),
                   g(c, "opt_partial", "guidance_partial"), g(c, "opt_missed", "guidance_missed")))
        na = core.field(c, "opt_na") if use_optimised else None
        if na:
            line += "\n  na: %s" % na
        # the authored samples (0.61.0) ride along in both modes: calibration, not rules (SYSTEM rule 15)
        good, bad = core.field(c, "example_good") or "", core.field(c, "example_bad") or ""
        if good:
            line += "\n  good sample (earns met): \"%s\"" % " ".join(str(good).split())
        if bad:
            line += "\n  bad sample (earns missed): \"%s\"" % " ".join(str(bad).split())
        parts.append(line)
    if diarized:
        parts.append("\nSpeakers are labelled S0, S1... by the transcription service. "
                     "Work out which one is the live agent, which the caller, and whether one is the voice AI.")
    else:
        parts.append("\nThe transcript is NOT speaker-separated (every line is S?). "
                     "Infer who is speaking from context; set agent_speaker to null and speakers to [].")
    if kb_excerpts:
        parts.append(kb.excerpt_block(kb_excerpts))
    parts.append("\nTRANSCRIPT:\n" + transcript_text)
    return system, "\n".join(parts)


def schema_for(keys):
    ev = {"type": "object", "additionalProperties": False,
          "properties": {"ts": {"type": "string"},
                         "speaker": {"type": ["string", "null"]},
                         "quote": {"type": "string"}},
          "required": ["ts", "speaker", "quote"]}
    item = {"type": "object", "additionalProperties": False,
            "properties": {"rating": {"type": "string", "enum": list(RATINGS)},
                           "score": {"type": "integer"},
                           "rationale": {"type": "string"},
                           "evidence": {"type": "array", "items": ev}},
            "required": ["rating", "score", "rationale", "evidence"]}
    # One required property per criterion: with strict output the model cannot
    # skip a criterion (on the first real call gpt-4o-mini scored 1 of 8 when
    # items was a free-length array).
    items_obj = {"type": "object", "additionalProperties": False,
                 "properties": {k: item for k in keys},
                 "required": list(keys)}
    note = {"type": "object", "additionalProperties": False,
            "properties": {"text": {"type": "string"},
                           "key": {"type": ["string", "null"]}},
            "required": ["text", "key"]}
    mood = {"type": "string", "enum": list(SENTIMENTS)}
    sentiment = {"type": "object", "additionalProperties": False,
                 "properties": {"start": mood, "end": mood, "overall": mood},
                 "required": ["start", "end", "overall"]}
    reason = {"type": "object", "additionalProperties": False,
              "properties": {"category": {"type": "string", "enum": list(REASON_KEYS)},
                             "detail": {"type": "string"}},
              "required": ["category", "detail"]}
    opt = {"type": ["string", "null"]}
    customer = {"type": "object", "additionalProperties": False,
                "properties": {"name": opt, "company": opt, "email": opt, "phone": opt, "reference": opt},
                "required": ["name", "company", "email", "phone", "reference"]}
    speaker = {"type": "object", "additionalProperties": False,
               "properties": {"label": {"type": "string"},
                              "role": {"type": "string", "enum": list(ROLES)},
                              "name": opt},
               "required": ["label", "role", "name"]}
    identifiers = {"type": "object", "additionalProperties": False,
                   "properties": {k: opt for k in FACT_IDS}, "required": list(FACT_IDS)}
    step = {"type": "object", "additionalProperties": False,
            "properties": {"text": {"type": "string"}, "result": opt, "key": {"type": ["string", "null"]}},
            "required": ["text", "result", "key"]}
    call_facts = {"type": "object", "additionalProperties": False,
                  "properties": {"identifiers": identifiers,
                                 "troubleshooting": {"type": "array", "items": step},
                                 "discussed": {"type": "array", "items": note},
                                 "outcome": opt},
                  "required": ["identifiers", "troubleshooting", "discussed", "outcome"]}
    return {"type": "object", "additionalProperties": False,
            "properties": {"agent_speaker": {"type": ["string", "null"]},
                           "speakers": {"type": "array", "items": speaker},
                           "transferred": {"type": "boolean"},
                           "customer": customer,
                           "auto_fail": {"type": "boolean"},
                           "summary": {"type": "string"},
                           "call_summary": {"type": "string"},
                           "items": items_obj,
                           "strengths": {"type": "array", "items": {"type": "string"}},
                           "opportunities": {"type": "array", "items": note},
                           "misses": {"type": "array", "items": note},
                           "sentiment": sentiment,
                           "call_reasons": {"type": "array", "items": reason},
                           "call_facts": call_facts},
            "required": ["agent_speaker", "speakers", "transferred", "customer", "auto_fail", "summary",
                         "call_summary", "items", "strengths", "opportunities", "misses", "sentiment",
                         "call_reasons", "call_facts"]}


def clamp(score, weight):
    try:
        s = int(round(float(score)))
    except (TypeError, ValueError):
        s = 0
    return max(0, min(int(weight), s))


def normalise_items(items):
    """The schema keys items by criterion ({key: {...}}); older callers and the
    fixtures use a list of {key, ...}. Return the list form either way."""
    if isinstance(items, dict):
        return [dict(v, key=k) for k, v in items.items() if isinstance(v, dict)]
    return list(items or [])


def missing_keys(result, criteria):
    """Criterion keys the model left out of its answer."""
    if not isinstance(result, dict):
        raise ValueError("The model returned %s instead of a JSON object." % type(result).__name__)
    got = {i.get("key") for i in normalise_items(result.get("items")) if isinstance(i, dict)}
    return [c["key"] for c in criteria if c["key"] not in got]


_TS_RE = re.compile(r"^\[?\s*(?:(\d{1,2}):)?(\d{1,3}):(\d{2})\s*\]?$")


def normalise_ts(ts):
    """'[01:13]' / '01:13' / '1:01:13' -> 'mm:ss'; anything else -> ''. The model copies the
    transcript's [mm:ss] prefix, brackets and all, about one time in eight, and the UI's
    click-to-seek needs the bare form."""
    m = _TS_RE.match(str(ts or "").strip())
    if not m:
        return ""
    h, mi, se = int(m.group(1) or 0), int(m.group(2)), int(m.group(3))
    if se > 59:
        return ""
    return "%02d:%02d" % (h * 60 + mi, se)


def validate_result(result, criteria, transcript_text=""):
    """Coerce the model's answer into rows. Returns (clean, warnings)."""
    if not isinstance(result, dict):
        raise ValueError("The model returned %s instead of a JSON object." % type(result).__name__)
    warnings = []
    by_key = {c["key"]: c for c in criteria}
    seen, items = {}, []
    for raw in normalise_items(result.get("items")):
        if not isinstance(raw, dict):
            continue
        k = raw.get("key")
        if k not in by_key:
            warnings.append("Model returned unknown criterion '%s'; dropped." % k)
            continue
        if k in seen:
            warnings.append("Model returned '%s' twice; kept the first." % k)
            continue
        seen[k] = raw
    for c in criteria:
        raw = seen.get(c["key"])
        w = int(c["weight"])
        if raw is None:
            warnings.append("Model did not score '%s'; recorded as missed." % c["name"])
            items.append({"criterion_id": c["id"], "key": c["key"], "rating": "missed",
                          "score": 0, "weight": w, "rationale": "Not addressed by the model.",
                          "evidence": []})
            continue
        rating = str(raw.get("rating") or "").lower()
        if rating not in RATINGS:
            warnings.append("Bad rating '%s' on '%s'; treated as partial." % (rating, c["name"]))
            rating = "partial"
        if rating == "met":
            score = w
        elif rating in ("missed", "na"):
            score = 0
        else:
            score = clamp(raw.get("score"), w)
            if score == w and w > 0:
                score = max(0, w - 1) if w > 1 else 0   # partial cannot silently be full marks
        ev = []
        for e in (raw.get("evidence") or [])[:6]:
            if not isinstance(e, dict):
                continue
            q = str(e.get("quote") or "").strip()
            if not q:
                continue
            if transcript_text and not _quote_ok(q, transcript_text):
                warnings.append("Evidence quote on '%s' not found in transcript; dropped: \"%s\""
                                % (c["name"], q[:80]))
                continue
            ev.append({"ts": normalise_ts(e.get("ts")), "speaker": e.get("speaker"), "quote": q})
        items.append({"criterion_id": c["id"], "key": c["key"], "rating": rating,
                      "score": score, "weight": w,
                      "rationale": str(raw.get("rationale") or "").strip(), "evidence": ev})

    auto_fail = bool(result.get("auto_fail"))
    crit_missed = [i for i in items if i["rating"] == "missed" and by_key[i["key"]]["critical"]]
    if crit_missed and not auto_fail:
        warnings.append("A critical criterion was missed; auto_fail set by the server.")
        auto_fail = True
    if auto_fail and not crit_missed:
        auto_fail = False       # the model may not fail a call the rubric does not
        warnings.append("Model set auto_fail without a missed critical criterion; cleared.")

    def notes(lst):
        out = []
        for n in (lst or [])[:20]:
            if isinstance(n, dict):
                t = str(n.get("text") or "").strip()
                k = n.get("key") if n.get("key") in by_key else None
            else:
                t, k = str(n or "").strip(), None
            if t:
                out.append({"text": t, "key": k})
        return out

    speakers = _speakers(result.get("speakers"), warnings)
    agent_speaker = _speaker_int(result.get("agent_speaker"))
    agents = [sp["speaker"] for sp in speakers if sp["role"] == "agent"]
    if agent_speaker is None and len(agents) == 1:
        agent_speaker = agents[0]
    facts = _facts(result.get("call_facts"), by_key, transcript_text, warnings)
    customer = _customer(result.get("customer"))
    # one ticket number, whichever field the model put it in: the facts feed the Customer information card
    ticket = (facts or {}).get("identifiers", {}).get("ticket") if facts else None
    if ticket and not (customer or {}).get("reference"):
        customer = dict(customer or {k: None for k in CUSTOMER_CAPS}, reference=ticket)
    clean = {"agent_speaker": agent_speaker,
             "speakers": speakers,
             "transferred": bool(result.get("transferred")),
             "customer": customer,
             "call_facts": facts,
             "auto_fail": auto_fail,
             "summary": str(result.get("summary") or "").strip(),
             "call_summary": str(result.get("call_summary") or "").strip()[:1200],
             "strengths": [str(s).strip() for s in (result.get("strengths") or [])[:20] if str(s).strip()],
             "opportunities": notes(result.get("opportunities")),
             "misses": notes(result.get("misses")),
             "sentiment": _sentiment(result.get("sentiment")),
             "call_reasons": _reasons(result.get("call_reasons"), warnings),
             "items": items}
    return clean, warnings


CUSTOMER_CAPS = {"name": 120, "company": 120, "email": 160, "phone": 40, "reference": 80}
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _customer(v):
    """{name, company, email, phone, reference} as stated by the caller; every field trimmed and
    capped, a malformed email dropped; None when the model gave nothing usable."""
    if not isinstance(v, dict):
        return None
    out = {}
    for k, cap in CUSTOMER_CAPS.items():
        val = v.get(k)
        val = " ".join(str(val).split())[:cap] if isinstance(val, (str, int, float)) else ""
        if k == "email" and val and not _EMAIL_RE.match(val):
            val = ""
        out[k] = val or None
    return out if any(out.values()) else None


def _alnum(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _facts(v, by_key, transcript_text, warnings):
    """call_facts (prompt v8), kept deterministic: an identifier survives only when its characters occur in
    the transcript (case and separators ignored, so 'AA:BB:CC' matches 'AA BB CC' and '101' matches
    'extension 101'); device and address must be found the way a quote is; a step's key must be a real
    criterion; lists are capped. None when nothing usable remains."""
    if not isinstance(v, dict):
        return None
    raw_ids = v.get("identifiers") if isinstance(v.get("identifiers"), dict) else {}
    tnorm = _alnum(transcript_text) if transcript_text else ""
    ids = {}
    for k in FACT_IDS:
        val = raw_ids.get(k)
        val = " ".join(str(val).split())[:80] if isinstance(val, (str, int, float)) else ""
        if val and transcript_text:
            ok = (len(_alnum(val)) >= 2 and _alnum(val) in tnorm) if k in FACT_CODES else _quote_ok(val, transcript_text)
            if not ok:
                warnings.append("%s '%s' was not heard on the call; dropped." % (FACT_LABELS[k], val[:40]))
                val = ""
        ids[k] = val or None

    def steps(lst, with_result):
        out = []
        for s in (lst or [])[:8]:
            if not isinstance(s, dict):
                continue
            t = " ".join(str(s.get("text") or "").split())[:200]
            if not t:
                continue
            e = {"text": t, "key": s.get("key") if s.get("key") in by_key else None}
            if with_result:
                r = s.get("result")
                e["result"] = (" ".join(str(r).split())[:200] or None) if isinstance(r, str) else None
            out.append(e)
        return out
    ts, ds = steps(v.get("troubleshooting"), True), steps(v.get("discussed"), False)
    outcome = v.get("outcome")
    outcome = (" ".join(str(outcome).split())[:300] or None) if isinstance(outcome, str) else None
    if not any(ids.values()) and not ts and not ds and not outcome:
        return None
    return {"identifiers": ids, "troubleshooting": ts, "discussed": ds, "outcome": outcome}


def _speakers(lst, warnings):
    """[{speaker:int, role, name}] -- one entry per label, roles restricted to ROLES."""
    out, seen = [], set()
    for sp in (lst or []):
        if not isinstance(sp, dict):
            continue
        n = _speaker_int(sp.get("label") if sp.get("label") is not None else sp.get("speaker"))
        role = str(sp.get("role") or "").strip().lower()
        if n is None or n in seen:
            continue
        if role not in ROLES:
            warnings.append("Model returned unknown speaker role '%s' for S%d; treated as other." % (role, n))
            role = "other"
        seen.add(n)
        name = sp.get("name")
        name = " ".join(str(name).split())[:80] if isinstance(name, str) and name.strip() else None
        out.append({"speaker": n, "role": role, "name": name})
    return out


def _sentiment(v):
    """{start,end,overall} with unknown values dropped; None when nothing usable (pre-v4 answers)."""
    if not isinstance(v, dict):
        return None
    out = {}
    for k in ("start", "end", "overall"):
        m = str(v.get(k) or "").strip().lower()
        out[k] = m if m in SENTIMENTS else None
    return out if any(out.values()) else None


def _reasons(lst, warnings):
    """Known categories only, one entry per category, at most MAX_REASONS, detail trimmed."""
    out, seen = [], set()
    for r in (lst or []):
        if not isinstance(r, dict):
            continue
        cat = str(r.get("category") or "").strip().lower()
        if cat not in REASON_KEYS:
            warnings.append("Model returned unknown call reason '%s'; dropped." % cat)
            continue
        if cat in seen:
            continue
        seen.add(cat)
        out.append({"category": cat, "detail": str(r.get("detail") or "").strip()[:200]})
        if len(out) >= MAX_REASONS:
            break
    return out


def compute_overall(items):
    """items carry score, weight, rating and optional override_score.
    Returns (pct rounded to 1dp, applicable_weight)."""
    got, basis = 0, 0
    for it in items:
        if it.get("rating") == "na":
            continue
        w = int(it.get("weight") or 0)
        s = it.get("override_score")
        if s is None:
            s = it.get("score") or 0
        got += max(0, min(w, int(s)))
        basis += w
    if basis <= 0:
        return 0.0, 0
    return round(got * 100.0 / basis, 1), basis


def _quote_ok(quote, transcript):
    q = " ".join(quote.lower().split())
    t = " ".join(transcript.lower().split())
    if q in t:
        return True
    # tolerate small punctuation/smart-format differences
    if len(q) < 12:
        return False
    m = difflib.SequenceMatcher(None, q, t[:200000], autojunk=False).find_longest_match(0, len(q), 0, min(len(t), 200000))
    return m.size >= max(12, int(len(q) * 0.7))


def _speaker_int(v):
    if v is None:
        return None
    s = str(v).strip().upper()
    if s.startswith("S"):
        s = s[1:]
    try:
        return int(s)
    except ValueError:
        return None
