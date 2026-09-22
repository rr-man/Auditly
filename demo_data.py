#!/usr/bin/env python3
"""Seed a demo rubric, a demo user and one fully scored sample call.

Runs with no API keys and no network: the transcriber and scorer are the Demo
fakes in transcribe.py / llm.py, but the call goes through the real worker
pipeline, so what you see in demo mode is what the real thing produces.
"""
import hashlib
import json
import os
import random
import shutil
from datetime import datetime, timedelta, timezone

import core
import notify
import rubric as rubric_mod
import score as score_mod
import worker

DEMO_EMAIL = "demo@example.com"
DEMO_PASSWORD = "demo-demo-demo"          # 14 chars; passes password_problem()

CRITERIA = [
    ("greeting", "Greeting and identification", 10, 0,
     "Company name, agent name and an offer of help in the opening.",
     "All three present in the first line.", "Two of the three.", "Generic 'hello' or none."),
    ("verification", "Caller verification", 10, 1,
     "Confirms the caller's identity against account details before discussing the account.",
     "Account number, PIN or authorised contact confirmed.",
     "Business name only, or verification after account details were already discussed.",
     "No attempt to verify."),
    ("issue_discovery", "Issue discovery and active listening", 15, 0,
     "Restates the problem, confirms scope, asks what changed.",
     "Problem restated and scope confirmed with the caller.", "Restated but scope not confirmed.",
     "Jumped straight to steps without confirming the problem."),
    ("troubleshooting", "Troubleshooting", 20, 0,
     "Logical simplest-first path; explains why; confirms each step's result.",
     "Ordered steps, each explained and confirmed.", "Steps taken but unordered or unexplained.",
     "Random steps, or none."),
    ("communication", "Communication", 10, 0,
     "Plain language, one instruction at a time, sets expectations, avoids dead air.",
     "Clear throughout with expectations set.", "Mostly clear; some jargon or unexplained waits.",
     "Confusing or long unexplained silences."),
    ("empathy", "Empathy and professionalism", 10, 0,
     "Acknowledges impact, stays calm, uses the caller's name, never blames.",
     "Impact acknowledged and tone professional throughout.", "Polite but transactional.",
     "Dismissive, blaming or curt."),
    ("resolution", "Resolution or escalation", 15, 0,
     "Fixes and proves it, or escalates with ticket, owner and callback time.",
     "Resolved and verified, or escalated with all three details.",
     "Resolved but unverified, or escalated with details missing.",
     "Neither resolved nor properly escalated."),
    ("closing", "Closing", 10, 0,
     "Recap, 'anything else?', reference number, thanks.",
     "Recap plus offer of further help plus reference.", "Polite close missing recap or offer.",
     "Abrupt or no close."),
]


# 0.61.0: sample wording per criterion -- a line that earns met and one that earns missed. Calibration for the
# scorer (SYSTEM rule 15), shown in the editor as "Good sample" / "Bad sample".
SAMPLES = {
    "greeting": ("Thank you for calling SNET Connect support, this is Jordan. How can I help you today?", "Hello? Yeah?"),
    "verification": ("Before I open the account, can I take the account number or the PIN on file?",
                     "(the account is discussed and noted with no identity question at all)"),
    "troubleshooting": ("Could you unplug the network cable from the back of the phone, wait ten seconds, and plug it back in? That rules out the handset.",
                        "Try turning it off and on, and call back if it still does not work."),
    "closing": ("So we found the wall-jack cable loose and extension 101 is back in service. Your reference is 48213. Anything else I can help with today?",
                "Okay, bye."),
}


def _seed_rubric(db, name, by):
    """The sample L1 rubric from fixtures/demo_rubric.md; returns the version id. Idempotent."""
    r = db.execute("SELECT id FROM rubric WHERE name=?", (name,)).fetchone()
    if r:
        ver = db.execute("SELECT id FROM rubric_version WHERE rubric_id=? ORDER BY version_no DESC LIMIT 1",
                         (r["id"],)).fetchone()
        return ver["id"]
    rid = core.new_id()
    db.execute("INSERT INTO rubric (id,name,created_at,created_by) VALUES (?,?,?,?)",
               (rid, name, core.now(), by))
    items = rubric_mod.normalise([
        {"key": k, "name": n, "weight": w, "critical": bool(cr), "description": d,
         "guidance_met": gm, "guidance_partial": gp, "guidance_missed": gx,
         "example_good": SAMPLES.get(k, ("", ""))[0], "example_bad": SAMPLES.get(k, ("", ""))[1]}
        for k, n, w, cr, d, gm, gp, gx in CRITERIA])
    errs = rubric_mod.validate(items)
    assert not errs, errs
    src = open(os.path.join(core.HERE, "fixtures", "demo_rubric.md"), encoding="utf-8").read()
    vid, _ = rubric_mod.create_version(db, rid, items, source_text=src,
                                       source_filename="demo_rubric.md",
                                       notes="Seeded by %s" % by)
    return vid


AGENTS = ("Jordan", "Priya", "Marcus")
QA_NAMES = ("Demo QA",)
HISTORY_QA = ("Alex Reyes",)              # second QA name, only with the history seed
HISTORY_BY = "demo-history@local"        # recording.uploaded_by sentinel: how loaded demo calls are found and removed
# 0.58.0: the history's shape. Per-criterion "met" probability per agent (Marcus climbs with recency:
# start + slope * recent), how often a critical criterion escapes a miss, and the share of submitted
# audits already coached. Tuned so the demo Dashboard's pass rate and coaching coverage sit near the 90 %
# target over the default 90-day window while Marcus stays lowest and a little under it and one or two
# calls still miss a critical check -- see the test "demo history: pass rate and coaching coverage".
HISTORY_BASES = {"Jordan": 0.97, "Priya": 0.94, "Marcus": (0.64, 0.24)}   # simulated 2026-09-22: pass 87.1 %, coaching 86.4 %, Marcus 81 %
HISTORY_CRIT_GUARD = 0.30      # a critical criterion that is not met escapes a miss this often (one auto-fail in 44)
HISTORY_COACHED = 0.90


def _seed_people(db, by):
    """Agents + QA reviewers for the Upload dropdowns. Idempotent; returns how many were added."""
    added = 0
    for kind, names in (("agent", AGENTS), ("qa", QA_NAMES)):
        for n in names:
            if not db.execute("SELECT 1 FROM person WHERE kind=? AND name=? COLLATE NOCASE", (kind, n)).fetchone():
                db.execute("INSERT INTO person (id,kind,name,active,created_at,created_by) VALUES (?,?,?,1,?,?)",
                           (core.new_id(), kind, n, core.now(), by))
                added += 1
    return added


def seed_for_real(db):
    """`--seed-rubric`: the sample rubric into the REAL database -- no demo user, no fake call
    and, since 0.13.0, no placeholder names: production carries nothing demo. The team's
    agents and QA reviewers are typed in under Settings > Names."""
    vid = _seed_rubric(db, "L1 Support v1", "setup")
    return vid, 0


def seed(db, c):
    """Idempotent: re-running --demo does not duplicate anything."""
    # user
    if not db.execute("SELECT 1 FROM user WHERE email=?", (DEMO_EMAIL,)).fetchone():
        h, s = core.hash_password(DEMO_PASSWORD)
        db.execute("INSERT INTO user (id,email,name,pw_hash,pw_salt,role,active,created_at)"
                   " VALUES (?,?,?,?,?,'admin',1,?)",
                   (core.new_id(), DEMO_EMAIL, "Demo Reviewer", h, s, core.now()))

    vid = _seed_rubric(db, "L1 Support v1 (demo)", DEMO_EMAIL)

    _seed_people(db, DEMO_EMAIL)
    # a demo DB created before these columns existed gets the values filled in
    db.execute("UPDATE recording SET qa_name=COALESCE(qa_name,'Demo QA'),"
               " caller_company=COALESCE(caller_company,'Northwind Traders'),"
               " caller_name=COALESCE(caller_name,'Sam Lee'),"
               " audit_name=COALESCE(audit_name,'Jordan — Northwind Traders — Sam Lee — ' || substr(uploaded_at,1,10)),"
               " kind='demo'"
               " WHERE call_ref='DEMO-0001'")

    seed_kb(db, DEMO_EMAIL)                      # before the call, so its scorecard shows the excerpts

    job_id = None
    if not db.execute("SELECT 1 FROM recording WHERE call_ref='DEMO-0001'").fetchone():
        job_id = _seed_call(db, c, vid)          # closes db; runs the real pipeline
    else:
        db.close()
    # a spread of past calls so the Dashboard and the Audit queue have something to show.
    # AUDITLY_DEMO_HISTORY=0 turns it off (the test-suite does: its counts assume one call).
    if core.cfg_int(c, "AUDITLY_DEMO_HISTORY", 1) == 1:
        db2 = core.connect()
        try:
            seed_history(db2, c, "real", vid, DEMO_EMAIL)
        finally:
            db2.close()
    return job_id


DEMO_KB_FILE = "demo_kb.md"


def seed_kb(db, by):
    """The sample knowledge-base document (desk-phone procedures matching the demo call), once."""
    if db.execute("SELECT 1 FROM kb_document WHERE filename=? AND deleted_at IS NULL", (DEMO_KB_FILE,)).fetchone():
        return None
    import kb
    text = open(os.path.join(core.HERE, "fixtures", DEMO_KB_FILE), encoding="utf-8").read()
    did, now = core.new_id(), core.now()
    db.execute("INSERT INTO kb_document (id,title,filename,chars,text,enabled,rubric_id,uploaded_by,created_at,updated_at)"
               " VALUES (?,?,?,?,?,1,NULL,?,?,?)", (did, "L1 desk reference — desk phones (demo)", DEMO_KB_FILE, len(text), text, by, now, now))
    for i, ch in enumerate(kb.chunk(text)):
        db.execute("INSERT INTO kb_chunk (document_id,seq,text) VALUES (?,?,?)", (did, i, ch))
    return did


def _seed_call(db, c, vid):
    """The one sample call, scored synchronously through the real pipeline."""
    src = os.path.join(core.HERE, "fixtures", "tone.wav")
    rec_id = core.new_id()
    dst = os.path.join(core.upload_dir(c), rec_id + ".wav")
    shutil.copyfile(src, dst)
    os.chmod(dst, 0o600)
    with open(dst, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    now = core.now()
    db.execute("INSERT INTO recording (id,filename,ext,mime,bytes,sha256,path,agent_name,qa_name,"
               "caller_company,caller_name,audit_name,kind,call_ref,uploaded_at,uploaded_by)"
               " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'demo',?,?,?)",
               (rec_id, "harbor-dental-ext101-no-service.wav", "wav", "audio/wav",
                os.path.getsize(dst), digest, dst, "Jordan", "Demo QA", "Northwind Traders", "Sam Lee",
                "Jordan — Northwind Traders — Sam Lee — " + now[:10], "DEMO-0001", now, DEMO_EMAIL))
    job_id = core.new_id()
    db.execute("INSERT INTO job (id,recording_id,rubric_version_id,stt_provider,scoring_provider,"
               "status,created_at,created_by) VALUES (?,?,?,?,?,'queued',?,?)",
               (job_id, rec_id, vid, "demo", "demo", core.now(), DEMO_EMAIL))
    db.close()
    worker.process(job_id, dict(c, AUDITLY_DEMO="1"))
    return job_id


# ── history: past calls for the dashboard ─────────────────────────────────
CALLERS = (("Harbor Dental", "Maria Lopez"), ("Northwind Traders", "Sam Lee"), ("Bluebird Bakery", "Tom Nguyen"),
           ("Summit Legal", "Dana Whitfield"), ("Riverside Vet", "Chris Okafor"), ("Pinecrest Realty", "Ana Souza"),
           ("Metro Physio", "Lee Chang"), ("Oakline Insurance", "Ravi Nair"))
REASON_WEIGHTS = (("no_service", 30), ("call_quality", 22), ("voicemail", 14), ("call_routing", 12),
                  ("provisioning", 8), ("network", 8), ("hardware", 6), ("how_to", 6), ("billing", 3),
                  ("account_access", 3), ("outage", 2), ("porting", 1))
REASON_DETAIL = {"no_service": "Desk phone showed No Service", "call_quality": "Choppy audio on outbound calls",
                 "voicemail": "Voicemail greeting would not save", "call_routing": "Ring group skipped one extension",
                 "provisioning": "New handset needed an extension", "network": "Phones dropped after a router change",
                 "hardware": "Handset display dead after power flicker", "how_to": "How to set a holiday schedule",
                 "billing": "Question about a line on the invoice", "account_access": "Portal password reset",
                 "outage": "Whole site could not make calls", "porting": "Status of a number port"}
COACHING = {
    "greeting": "Open with your name and the company every time; the first ten seconds set the tone.",
    "verification": "Verify the caller against account details before making any change or note.",
    "issue_discovery": "Restate the problem in your own words and ask one clarifying question before troubleshooting.",
    "troubleshooting": "Isolate first (one phone or all?), then change one thing at a time and say why.",
    "communication": "Narrate what you are doing during silences so the caller never hears dead air.",
    "empathy": "Acknowledge the business impact before diving into steps.",
    "resolution": "Confirm the fix with a test call and tell the caller what happens next.",
    "closing": "Ask 'anything else?' and give a reference number before ending the call.",
}


def _iso(dt):
    return dt.replace(microsecond=0).isoformat()


def history_count(db):
    return db.execute("SELECT COUNT(*) FROM recording WHERE uploaded_by=?", (HISTORY_BY,)).fetchone()[0]


def remove_history(db):
    """Delete every loaded demo call. One statement: job, transcript, utterances, scorecard,
    items and review all cascade from recording (schema.sql). No audio files exist for them."""
    return db.execute("DELETE FROM recording WHERE uploaded_by=?", (HISTORY_BY,)).rowcount


def seed_history(db, c, kind, rubric_version_id, by):
    """~44 calls of the given kind over the last 120 days across the three demo agents, with
    reviews on most (a few queued or in draft so the Audit queue has work). Direct inserts:
    the demo scorer would give 44 identical 90 % cards. The transcript is built once from
    fixtures/demo_call.json and copied; no audio file exists (path '', audio_deleted_at set),
    sha256 is NULL so a real upload can never reuse a demo transcript. Idempotent."""
    if history_count(db):
        return 0
    ver = db.execute("SELECT id FROM rubric_version WHERE id=?", (rubric_version_id,)).fetchone()
    if not ver:
        return 0
    crit = db.execute("SELECT * FROM criterion WHERE rubric_version_id=? ORDER BY seq", (ver["id"],)).fetchall()
    for n in HISTORY_QA:
        if not db.execute("SELECT 1 FROM person WHERE kind='qa' AND name=? COLLATE NOCASE", (n,)).fetchone():
            db.execute("INSERT INTO person (id,kind,name,active,created_at,created_by) VALUES (?,?,?,1,?,?)",
                       (core.new_id(), "qa", n, core.now(), by))
    with open(os.path.join(core.HERE, "fixtures", "demo_scorecard.json"), "r", encoding="utf-8") as f:
        fx = json.load(f)
    with open(os.path.join(core.HERE, "fixtures", "demo_call.json"), "r", encoding="utf-8") as f:
        call = json.load(f)
    size = os.path.getsize(os.path.join(core.HERE, "fixtures", "tone.wav"))
    src_t = None
    rng = random.Random(20260903)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    made = 0
    for i in range(44):
        days_ago = rng.randint(1, 120)
        at = now - timedelta(days=days_ago, hours=rng.randint(1, 9), minutes=rng.randint(0, 59))
        agent = rng.choices(AGENTS, weights=(40, 35, 25))[0]
        qa = rng.choice(QA_NAMES + HISTORY_QA)
        company, caller = rng.choice(CALLERS)
        recent = 1.0 - days_ago / 120.0                       # 0 = oldest, 1 = newest
        b = HISTORY_BASES[agent]
        base = b if isinstance(b, float) else b[0] + b[1] * recent
        items, lowest = [], None
        for cr in crit:
            w, roll = int(cr["weight"]), rng.random()
            if roll < base:
                rating, score = "met", w
            elif roll < base + (1 - base) * 0.65 or (cr["critical"] and rng.random() < HISTORY_CRIT_GUARD):
                rating, score = "partial", rng.randint(1, w - 1) if w > 1 else 0
            else:
                rating, score = "missed", 0
            items.append({"criterion_id": cr["id"], "key": cr["key"], "rating": rating, "score": score, "weight": w,
                          "critical": cr["critical"]})
            if score < w and (lowest is None or score * 1.0 / w < lowest[0]):
                lowest = (score * 1.0 / w, cr["key"])
        pct, basis = score_mod.compute_overall(items)
        auto_fail = 1 if any(it["rating"] == "missed" and it["critical"] for it in items) else 0
        uploaded, finished = _iso(at), _iso(at + timedelta(minutes=rng.randint(3, 12)))
        rec_id, job_id, sc_id = core.new_id(), core.new_id(), core.new_id()
        db.execute("INSERT INTO recording (id,filename,ext,mime,bytes,sha256,path,agent_name,qa_name,"
                   "caller_company,caller_name,audit_name,kind,call_ref,uploaded_at,uploaded_by,audio_deleted_at)"
                   " VALUES (?,?,?,?,?,NULL,'',?,?,?,?,?,?,?,?,?,?)",
                   (rec_id, "call-%s-%03d.wav" % (agent.lower(), i + 1), "wav", "audio/wav", size,
                    agent, qa, company, caller, " — ".join((agent, company, caller, uploaded[:10])), kind,
                    "H-%04d" % (i + 1), uploaded, HISTORY_BY, uploaded))
        db.execute("INSERT INTO job (id,recording_id,rubric_version_id,stt_provider,stt_model,scoring_provider,"
                   "scoring_model,status,attempts,created_at,started_at,finished_at,created_by)"
                   " VALUES (?,?,?,'demo','demo','demo','demo','done',1,?,?,?,?)",
                   (job_id, rec_id, ver["id"], uploaded, uploaded, finished, by))
        if src_t is None:
            tid = worker._store_transcript(db, rec_id, job_id, call)
            src_t = db.execute("SELECT * FROM transcript WHERE id=?", (tid,)).fetchone()
        else:
            tid = worker._copy_transcript(db, src_t, rec_id, job_id)
        s_start = rng.choices(("negative", "neutral", "positive"), weights=(55, 35, 10))[0]
        s_end = rng.choices(("negative", "neutral", "positive"), weights=(10, 25, 65))[0] if pct >= 60 \
            else rng.choices(("negative", "neutral", "positive"), weights=(45, 40, 15))[0]
        s_all = s_end if s_end != "neutral" else s_start
        cats, reasons = set(), []
        for _ in range(rng.choices((1, 2, 3), weights=(55, 35, 10))[0]):
            cat = rng.choices([k for k, _ in REASON_WEIGHTS], weights=[w for _, w in REASON_WEIGHTS])[0]
            if cat not in cats:
                cats.add(cat)
                reasons.append({"category": cat, "detail": REASON_DETAIL[cat]})
        db.execute("INSERT INTO scorecard (id,job_id,recording_id,transcript_id,rubric_version_id,agent_speaker,"
                   "overall_pct,applicable_weight,auto_fail,summary,call_summary,strengths_json,opportunities_json,"
                   "misses_json,warnings_json,truncated,model,prompt_version,created_at,sentiment_json,call_reasons_json)"
                   " VALUES (?,?,?,?,?,0,?,?,?,?,?,?,?,?,'[]',0,'demo',?,?,?,?)",
                   (sc_id, job_id, rec_id, tid, ver["id"], pct, basis, auto_fail,
                    fx["summary"].replace("The agent", agent), fx["call_summary"].replace("Jordan", agent),
                    json.dumps(fx["strengths"]), json.dumps(fx["opportunities"]), json.dumps(fx["misses"]),
                    score_mod.PROMPT_VERSION, finished,
                    json.dumps({"start": s_start, "end": s_end, "overall": s_all}), json.dumps(reasons)))
        for it in items:
            db.execute("INSERT INTO scorecard_item (scorecard_id,criterion_id,rating,score,weight,rationale,evidence_json)"
                       " VALUES (?,?,?,?,?,?,'[]')",
                       (sc_id, it["criterion_id"], it["rating"], it["score"], it["weight"],
                        "Demo history: %s." % it["rating"]))
        roll = rng.random()
        if roll < 0.18 and days_ago <= 45:
            # sent to audit, nobody has opened it yet
            db.execute("INSERT INTO review (id,scorecard_id,recording_id,reviewer_email,status,created_at,updated_at)"
                       " VALUES (?,?,?,?,'queued',?,?)", (core.new_id(), sc_id, rec_id, by, finished, finished))
        elif roll < 0.80 or days_ago > 60:
            sub = at + timedelta(days=rng.randint(1, 3), hours=rng.randint(0, 8))
            if sub < now:
                coached = None
                if rng.random() < HISTORY_COACHED:
                    cd = sub + timedelta(days=rng.randint(0, 5), hours=rng.randint(0, 6))
                    coached = _iso(cd) if cd < now else None
                key = lowest[1] if lowest else "closing"
                follow = (sub + timedelta(days=14)).date().isoformat() if rng.random() < 0.4 else None
                db.execute("INSERT INTO review (id,scorecard_id,recording_id,reviewer_email,status,final_pct,"
                           "applicable_weight,auto_fail,coaching_notes,resolution_notes,action_plan,follow_up_on,"
                           "coached_at,created_at,updated_at,submitted_at) VALUES (?,?,?,?,'submitted',?,?,?,?,?,?,?,?,?,?,?)",
                           (core.new_id(), sc_id, rec_id, by, pct, basis, auto_fail,
                            COACHING.get(key, COACHING["closing"]),
                            "Reviewed the call with %s; agreed on the gap and the fix." % agent,
                            "Practise the %s step on the next five calls; QA to spot-check one." % key.replace("_", " "),
                            follow, coached, _iso(sub), _iso(sub), _iso(sub)))
        elif roll < 0.93:
            db.execute("INSERT INTO review (id,scorecard_id,recording_id,reviewer_email,status,coaching_notes,"
                       "created_at,updated_at) VALUES (?,?,?,?,'draft',?,?,?)",
                       (core.new_id(), sc_id, rec_id, by, "Draft — listen again to the troubleshooting section.",
                        finished, finished))
        made += 1
    # 0.51.0: light the notification button honestly -- the Dashboard's own rule over the seeded
    # history, tied to each agent's latest seeded call so removing the history takes the lines away
    for agent in AGENTS:
        rid = db.execute("SELECT id FROM recording WHERE uploaded_by=? AND agent_name=? COLLATE NOCASE"
                         " ORDER BY uploaded_at DESC LIMIT 1", (HISTORY_BY, agent)).fetchone()
        try:
            notify.check_failing(db, agent, kind, recording_id=rid["id"] if rid else None)
        except Exception:                        # noqa: BLE001 -- the seed must not fail over a notification
            pass
    return made
