#!/usr/bin/env python3
"""Rubrics: guideline text in, weighted criteria out, weights that must sum to 100.

The LLM only PROPOSES criteria (extract_criteria). A human edits and saves; the
save path (normalise + validate) is what enforces the 100-point basis.
"""
import io
import re
import zipfile

import core
import llm

WEIGHT_TOTAL = 100
MAX_CRITERIA = 25

EXTRACT_SYSTEM = (
    "You convert a call-quality guideline document for a Level 1 technical support desk "
    "into a scoring rubric. Return only JSON matching the schema. Produce between 4 and 12 "
    "criteria. Weights are integers that sum to exactly 100; if the document states weights "
    "or points, use them, otherwise distribute by the emphasis the document gives each area. "
    "Each criterion needs a one-line definition of what 'met', 'partial' and 'missed' look "
    "like on a call. When the document gives sample wording -- a model greeting, a required phrase, an "
    "example of what not to say -- copy it into example_good (a line that earns met) and example_bad (a line "
    "that earns missed); leave them empty strings when it gives none. Mark a criterion critical only if the "
    "document says failing it fails the whole call (for example a data-protection or identity-verification rule).")

EXTRACT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "criteria": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "weight": {"type": "integer"},
                "guidance_met": {"type": "string"},
                "guidance_partial": {"type": "string"},
                "guidance_missed": {"type": "string"},
                "example_good": {"type": "string"},
                "example_bad": {"type": "string"},
                "critical": {"type": "boolean"},
            },
            "required": ["name", "description", "weight", "guidance_met",
                         "guidance_partial", "guidance_missed", "example_good", "example_bad", "critical"]}}},
    "required": ["criteria"],
}


# ── "Optimise for the scorer": authored guidance -> literal decision rules ──
# Derived text on the same version (criterion.opt_*). The authored criteria, keys,
# names, weights and critical flags are never touched; validate_optimised() is the
# guardrail that makes sure the model could not change them even if it tried.
OPT_FIELDS = ("description", "met", "partial", "missed", "na")   # JSON keys; columns are opt_<field>
OPT_MAX_LEN = 600
OPT_MIN_LEN = 10

OPTIMISE_SYSTEM = (
    "You rewrite the scoring guidance of a call-quality rubric for a Level 1 technical support desk "
    "so that a scoring model applies it the same way every time. Return only JSON matching the "
    "schema: one object per criterion key, each with description, met, partial, missed and na.\n"
    "Rules:\n"
    "1. Ground truth is the SOURCE GUIDELINES and the authored criterion text. Never add a requirement "
    "that is not stated or clearly implied there; never drop one that is. Do not change what a "
    "criterion is about.\n"
    "2. Write observable, testable conditions about what the live agent SAYS on the call, not about "
    "intent, effort or tone in the abstract. Each condition must be checkable by reading the transcript.\n"
    "3. met = 'ALL of:' followed by a numbered list of conditions, for example 'ALL of: (1) the agent "
    "states the company name; (2) the agent states their own name; (3) the agent offers help -- all "
    "within the agent's first two turns.'\n"
    "4. partial = what counts as an attempt: 'At least one met-condition is present but not all' plus "
    "the specific incomplete forms the guidelines describe (for example 'verification asked for after "
    "account details were already discussed').\n"
    "5. missed = what 'absent' means: 'None of the met-conditions is present in the agent's turns' plus "
    "any behaviour the guidelines name as a failure of this criterion.\n"
    "6. na = the exact situation in which the criterion cannot apply to a call (for example 'the caller "
    "hangs up before the agent can close'). If it always applies write exactly: 'Never; this criterion "
    "applies to every call.'\n"
    "7. description = one or two sentences saying what the criterion measures, for a reader who has not "
    "seen the guidelines.\n"
    "8. Never mention points, scores, weights, percentages or how much a rating is worth; the server "
    "computes all arithmetic. Never refer to another criterion.\n"
    "9. Plain English, present tense, no hedging words (usually, generally, may). Each field is at most "
    "600 characters; aim for 150 to 400.\n"
    "10. Keep the criterion keys exactly as given; do not add, remove or rename any.")


def optimise_schema(keys):
    """Strict: one required object per criterion key, exactly the five text fields."""
    field = {"type": "string"}
    per = {"type": "object", "additionalProperties": False,
           "properties": {f: field for f in OPT_FIELDS}, "required": list(OPT_FIELDS)}
    return {"type": "object", "additionalProperties": False,
            "properties": {k: per for k in keys}, "required": list(keys)}


def optimise_user(rubric_name, version_no, criteria, source_text):
    """The user turn. The '### key=' / 'field:' delimiters are load-bearing: llm.DemoScorer
    parses this text offline to answer deterministically."""
    parts = ["RUBRIC: %s (version %d). Rewrite the guidance of all %d criteria: %s."
             % (rubric_name, version_no, len(criteria), ", ".join(c["key"] for c in criteria))]
    src = (source_text or "").strip()
    parts.append("\nSOURCE GUIDELINES:\n<<<\n%s\n>>>"
                 % (src[:60000] if src else "(none supplied; use the authored criterion text only)"))
    parts.append("\nCRITERIA (authored):")
    for c in criteria:
        parts.append("### key=%s | %s%s\ndescription: %s\nmet: %s\npartial: %s\nmissed: %s"
                     % (c["key"], c["name"], " | CRITICAL" if c["critical"] else "",
                        core.field(c, "description") or "", core.field(c, "guidance_met") or "",
                        core.field(c, "guidance_partial") or "", core.field(c, "guidance_missed") or ""))
        # the authored samples (0.61.0): the rules written here must agree with them
        if core.field(c, "example_good"):
            parts.append("good sample (earns met): %s" % core.field(c, "example_good"))
        if core.field(c, "example_bad"):
            parts.append("bad sample (earns missed): %s" % core.field(c, "example_bad"))
    return "\n".join(parts)


_SCORE_TALK = re.compile(r"\d+(?:\.\d+)?\s*(?:points?|pts?|marks?|percent)\b|\d+(?:\.\d+)?\s*%|\bweight(?:s|ed|ing)?\b",
                         re.I)


def validate_optimised(result, criteria):
    """(clean, problems). clean = {key: {field: text}} for every criterion; problems is a list of
    human-readable strings, empty when the result is usable. Never trusts the model: the key set
    must match exactly, every field must be real text within the length caps, nothing may talk
    about scoring, and 'met' is normalised to the literal 'ALL of:' rule the scorer is told to apply."""
    problems, clean = [], {}
    if not isinstance(result, dict):
        return {}, ["The model returned %s instead of a JSON object." % type(result).__name__]
    want = [c["key"] for c in criteria]
    extra = sorted(set(result) - set(want))
    if extra:
        problems.append("Unknown criterion keys: %s." % ", ".join(extra))
    for c in criteria:
        raw = result.get(c["key"])
        if not isinstance(raw, dict):
            problems.append("Criterion '%s' (key=%s) is missing." % (c["name"], c["key"]))
            continue
        out = {}
        for f in OPT_FIELDS:
            v = raw.get(f)
            v = " ".join(str(v).split()) if isinstance(v, str) else ""
            if len(v) < OPT_MIN_LEN:
                problems.append("key=%s: '%s' is empty or too short." % (c["key"], f))
            elif len(v) > OPT_MAX_LEN:
                problems.append("key=%s: '%s' is %d characters; the limit is %d." % (c["key"], f, len(v), OPT_MAX_LEN))
            m = _SCORE_TALK.search(v)
            if m:
                problems.append("key=%s: '%s' mentions scoring ('%s'); describe behaviour only." % (c["key"], f, m.group(0)))
            out[f] = v
        # The scorer is told to apply "met: ALL of: ..." literally. A sound answer that skipped the
        # literal prefix (seen on gpt-4o-mini, 2026-09-15) is normalised, not rejected: rejecting
        # burned a whole run over wording.
        met = out["met"]
        if met and not met.lower().startswith("all of"):
            out["met"] = "ALL of: " + (met if "(1)" in met else "(1) " + met)
        clean[c["key"]] = out
    return clean, problems


def optimise_criteria(criteria, scorer, rubric_name, version_no, source_text, usage=None):
    """One optimisation pass: ask, validate, re-ask once with the problems, validate again.
    Returns (clean, calls). Raises ValueError with the remaining problems, or llm.ProviderError.
    `usage`, when given, accumulates the scorer's token counts."""
    keys = [c["key"] for c in criteria]
    system = OPTIMISE_SYSTEM
    user = optimise_user(rubric_name, version_no, criteria, source_text)
    schema = optimise_schema(keys)
    max_tokens = min(16000, 1500 + 900 * len(criteria))     # gpt-4o-mini's output cap is 16,384
    calls = [0]

    def ask(u):
        out = scorer.complete_json(system, u, schema, "optimised_guidance", max_tokens)
        calls[0] += 1
        if usage is not None:
            lu = scorer.last_usage or {}
            for k in ("prompt_tokens", "completion_tokens", "cached_tokens"):
                usage[k] = usage.get(k, 0) + int(lu.get(k) or 0)
            usage["calls"] = usage.get("calls", 0) + 1
        return out

    try:
        raw = ask(user)
    except llm.ProviderError as e:
        if "not JSON" in (e.body or "") and not isinstance(e, llm.SpendBlocked):
            raw = ask(user + "\n\nReturn only the JSON object.")
        else:
            raise
    clean, problems = validate_optimised(raw, criteria)
    if problems:
        again = ask(user + "\n\nYour previous answer had these problems; fix ALL of them and return the "
                    "whole object again:\n- " + "\n- ".join(problems[:20]))
        clean, problems = validate_optimised(again, criteria)
        if problems:
            raise ValueError("Optimised guidance failed validation: " + "; ".join(problems[:6]))
    return clean, calls[0]


def opt_complete(criteria):
    """True when every criterion carries all five optimised fields (Row or dict)."""
    return bool(criteria) and all(
        (core.field(c, "opt_" + f) or "").strip() for c in criteria for f in OPT_FIELDS)


# ── uploaded guideline text ───────────────────────────────────────────────
def text_from_upload(filename, data):
    """.txt/.md pass through; .docx is a zip whose word/document.xml we strip.
    PDF is not supported in v1 (no stdlib text extraction)."""
    name = (filename or "").lower()
    if name.endswith(".docx"):
        return _docx_text(data)
    if name.endswith(".pdf"):
        raise ValueError("PDF guidelines are not supported yet. Paste the text, or save "
                         "the document as .docx or .txt and upload that.")
    return data.decode("utf-8", "replace").replace("\r\n", "\n")


def _docx_text(data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError):
        raise ValueError("That .docx could not be read.")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab/>", "\t", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    text = (text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&apos;", "'"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ── criteria shape ────────────────────────────────────────────────────────
def normalise(items):
    """Coerce a posted criteria list into rows: seq, key, name, description,
    weight(int), guidance_*, critical(0/1). Keys are slugs, made unique."""
    out, seen = [], set()
    for i, raw in enumerate(items or []):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        key = core.slug(raw.get("key") or name, "c%d" % (i + 1))
        base, n = key, 2
        while key in seen:
            key, n = "%s_%d" % (base, n), n + 1
        seen.add(key)
        try:
            weight = int(round(float(raw.get("weight") or 0)))
        except (TypeError, ValueError):
            weight = -1
        out.append({"seq": i + 1, "key": key, "name": name,
                    "description": str(raw.get("description") or "").strip(),
                    "weight": weight,
                    "guidance_met": str(raw.get("guidance_met") or "").strip(),
                    "guidance_partial": str(raw.get("guidance_partial") or "").strip(),
                    "guidance_missed": str(raw.get("guidance_missed") or "").strip(),
                    # 0.61.0: sample wording that earns met / missed; calibration for the scorer, never a rule
                    "example_good": " ".join(str(raw.get("example_good") or "").split()),
                    "example_bad": " ".join(str(raw.get("example_bad") or "").split()),
                    "critical": 1 if raw.get("critical") in (1, True, "1", "true") else 0})
    return out


SAMPLE_MAX = 300   # characters per good/bad sample


def validate(items):
    """Return a list of human-readable problems; empty means saveable."""
    errs = []
    if not items:
        return ["At least one criterion is required."]
    if len(items) > MAX_CRITERIA:
        errs.append("At most %d criteria." % MAX_CRITERIA)
    for it in items:
        if not it["name"]:
            errs.append("Criterion %d has no name." % it["seq"])
        if it["weight"] < 0 or it["weight"] > 100:
            errs.append("'%s' has an invalid weight." % (it["name"] or it["key"]))
        for f, label in (("example_good", "good sample"), ("example_bad", "bad sample")):
            if len(it.get(f) or "") > SAMPLE_MAX:
                errs.append("'%s': the %s is over %d characters." % (it["name"] or it["key"], label, SAMPLE_MAX))
    names = [it["name"].lower() for it in items if it["name"]]
    if len(names) != len(set(names)):
        errs.append("Two criteria share the same name.")
    total = sum(max(0, it["weight"]) for it in items)
    if total != WEIGHT_TOTAL:
        errs.append("Weights sum to %d; they must sum to exactly %d." % (total, WEIGHT_TOTAL))
    return errs


def rescale(items):
    """Largest-remainder rescale so weights sum to 100. Returns (items, changed)."""
    total = sum(max(0, it["weight"]) for it in items)
    if total == WEIGHT_TOTAL or total <= 0:
        return items, False
    exact = [max(0, it["weight"]) * WEIGHT_TOTAL / total for it in items]
    floors = [int(x) for x in exact]
    short = WEIGHT_TOTAL - sum(floors)
    order = sorted(range(len(items)), key=lambda i: exact[i] - floors[i], reverse=True)
    for i in order[:short]:
        floors[i] += 1
    for it, w in zip(items, floors):
        it["weight"] = w
    return items, True


# ── LLM proposal ──────────────────────────────────────────────────────────
def extract_criteria(text, scorer):
    text = (text or "").strip()
    if len(text) < 20:
        raise ValueError("The guidelines are too short to work with.")
    user = "GUIDELINES:\n\n" + text[:60000]
    d = scorer.complete_json(EXTRACT_SYSTEM, user, EXTRACT_SCHEMA, "rubric", 3000)
    items = normalise(d.get("criteria") or [])
    items, adjusted = rescale(items)
    return items, adjusted


# ── persistence ───────────────────────────────────────────────────────────
def create_version(db, rubric_id, items, source_text=None, source_filename=None,
                   notes=None, user=None):
    """Insert an immutable new version. Caller has already run validate()."""
    r = db.execute("SELECT COALESCE(MAX(version_no),0) n FROM rubric_version WHERE rubric_id=?",
                   (rubric_id,)).fetchone()
    vno = int(r["n"]) + 1
    vid = core.new_id()
    db.execute("INSERT INTO rubric_version (id,rubric_id,version_no,source_text,source_filename,"
               "notes,created_at,created_by) VALUES (?,?,?,?,?,?,?,?)",
               (vid, rubric_id, vno, source_text, source_filename, notes, core.now(),
                core.field(user, "email")))
    for it in items:
        db.execute("INSERT INTO criterion (id,rubric_version_id,seq,key,name,description,weight,"
                   "guidance_met,guidance_partial,guidance_missed,critical,example_good,example_bad)"
                   " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (core.new_id(), vid, it["seq"], it["key"], it["name"], it["description"],
                    it["weight"], it["guidance_met"], it["guidance_partial"],
                    it["guidance_missed"], it["critical"], it.get("example_good") or None, it.get("example_bad") or None))
    return vid, vno


def criteria_for(db, version_id):
    return db.execute("SELECT * FROM criterion WHERE rubric_version_id=? ORDER BY seq",
                      (version_id,)).fetchall()
