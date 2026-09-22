#!/usr/bin/env python3
"""Background job pipeline: queued -> transcribing -> scoring -> done | failed.

One daemon thread per AUDITLY_WORKERS pulls job ids off a queue. Each state change
is its own short transaction so the UI can poll progress. Anything unfinished at
boot is re-queued; after 3 attempts a job stays failed until a human retries it.
Also owns the retention sweep that deletes old audio (never transcripts or
scorecards).
"""
import json
import os
import queue
import threading
import time
import traceback
from datetime import datetime, timezone

import core
import kb
import llm
import notify
import rubric as rubric_mod
import rubric_sync
import score as score_mod
import tone
import transcribe

JOBQ = queue.Queue()
MAX_ATTEMPTS = 3
_log = None


def set_logger(fn):
    global _log
    _log = fn


def log(msg):
    if _log:
        _log(msg)


def enqueue(job_id):
    JOBQ.put(job_id)


# ── the pipeline ──────────────────────────────────────────────────────────
def _set(db, job_id, **kw):
    cols = ", ".join("%s=?" % k for k in kw)
    db.execute("UPDATE job SET %s WHERE id=?" % cols, list(kw.values()) + [job_id])


def _notify_failed(db, job_id, job, rec, msg):
    """A failed job is an event people want to see (0.51.0); it must never itself fail the job."""
    try:
        name = (rec["audit_name"] or rec["filename"]) if rec else None
        notify.emit(db, "job.failed", "Processing failed: %s" % (name or job_id[:8]), msg[:200], "job", job_id,
                    rec["agent_name"] if rec else None, None, (job["recording_id"] if job else None))
    except Exception as e:                       # noqa: BLE001
        log("notify: %r" % e)


def process(job_id, c=None):
    """Run one job to completion. Safe to call synchronously (demo, tests)."""
    c = c or core.cfg()
    db = core.connect()
    job = rec = None
    try:
        job = db.execute("SELECT * FROM job WHERE id=?", (job_id,)).fetchone()
        if not job or job["status"] in ("done",):
            return
        if job["attempts"] >= MAX_ATTEMPTS and job["status"] == "failed":
            return
        rec = db.execute("SELECT * FROM recording WHERE id=?", (job["recording_id"],)).fetchone()
        ver = db.execute("SELECT v.*, r.name rubric_name FROM rubric_version v JOIN rubric r ON r.id=v.rubric_id"
                         " WHERE v.id=?", (job["rubric_version_id"],)).fetchone()
        crit = rubric_mod.criteria_for(db, job["rubric_version_id"])
        if not rec or not ver or not crit:
            _set(db, job_id, status="failed", error="Recording or QA type no longer exists.",
                 finished_at=core.now())
            core.audit(db, "job.failed", target=job_id, detail="Recording or QA type no longer exists.")
            _notify_failed(db, job_id, job, rec, "Recording or QA type no longer exists.")
            return
        _set(db, job_id, status="transcribing", started_at=core.now(), error=None,
             attempts=job["attempts"] + 1, progress="Preparing…")

        # 1. transcript -- reuse the newest one for this recording if present
        #    (retry after a scoring failure, or a rescore) so we never bill STT twice.
        tr = db.execute("SELECT * FROM transcript WHERE recording_id=? ORDER BY created_at DESC LIMIT 1",
                        (rec["id"],)).fetchone()
        fresh = ("transcript_mode" in job.keys()) and job["transcript_mode"] == "fresh"
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "audio_s": 0.0, "cached": False, "calls": 0}
        if tr and not fresh:
            _set(db, job_id, progress="Reusing existing transcript")
            transcript_id = tr["id"]
        else:
            if rec["audio_deleted_at"] or not os.path.exists(rec["path"]):
                raise RuntimeError("The audio file is no longer available (retention or delete).")
            t = transcribe.transcriber_for(c, job["stt_provider"], job["stt_model"])
            _set(db, job_id, stt_model=job["stt_model"] or t.model)
            # Cache by content: the same bytes through the same engine give the same
            # transcript, so copy it instead of paying again (identical audio may sit
            # on another recording -- the same call uploaded twice).
            src = _cached_transcript(db, rec["sha256"], t.name, t.model)
            if src is not None:
                _set(db, job_id, progress="Reused transcript of identical audio (no charge)")
                transcript_id = _copy_transcript(db, src, rec["id"], job_id)
                usage["cached"] = True
            else:
                _set(db, job_id, progress="Uploading to %s…" % t.name)
                result = t.transcribe(rec["path"], rec["mime"] or "application/octet-stream")
                transcript_id = _store_transcript(db, rec["id"], job_id, result)
                usage["audio_s"] = float(result.get("duration_s") or rec["duration_s"] or 0)

        # 2. score
        utts = [dict(r) for r in db.execute(
            "SELECT * FROM utterance WHERE transcript_id=? ORDER BY seq", (transcript_id,))]
        _set(db, job_id, status="scoring", progress="Scoring %d criteria…" % len(crit))
        # 2a. someone may have just pressed "Optimise for the scorer" on this QA type: give the
        #     rewrite a moment to land so this audit uses the same rules as the next one will.
        limit, waited = core.cfg_int(c, "AUDITLY_SYNC_WAIT_S", 120), 0
        while waited < limit:
            st = db.execute("SELECT sync_status FROM rubric_version WHERE id=?", (ver["id"],)).fetchone()
            if not st or st["sync_status"] not in rubric_sync.LIVE:
                break
            if not waited:
                _set(db, job_id, progress="Waiting for the QA type to finish optimising…")
            time.sleep(2)
            waited += 2
        ver = db.execute("SELECT v.*, r.name rubric_name FROM rubric_version v JOIN rubric r ON r.id=v.rubric_id"
                         " WHERE v.id=?", (job["rubric_version_id"],)).fetchone()
        crit = rubric_mod.criteria_for(db, ver["id"])            # re-read: opt_* may have landed
        mode = score_mod.guidance_mode(ver, crit)
        _set(db, job_id, progress="Scoring %d criteria (%s)…"
             % (len(crit), "optimised rules" if mode == "optimised" else "guidance as written"))
        scorer = llm.scorer_for(c, job["scoring_provider"], job["scoring_model"])
        # The job keeps what was ASKED for (the audit-cache key); scorecard.model records what ran.
        _set(db, job_id, scoring_provider=job["scoring_provider"] or scorer.name, scoring_model=job["scoring_model"] or scorer.model)
        text, truncated = score_mod.format_transcript(
            utts, core.cfg_int(c, "AUDITLY_MAX_TRANSCRIPT_CHARS", 120000))
        diarized = any(u["speaker"] is not None for u in utts)
        # 2b. reference material: the enabled knowledge-base documents in scope, searched with the
        #     transcript; nothing paid, and nothing added when no chunk clearly matches
        _set(db, job_id, progress="Finding reference material…")
        kb_hits, kb_key = kb.retrieve_for(db, ver["rubric_id"], text)
        _set(db, job_id, progress="Scoring %d criteria (%s%s)…"
             % (len(crit), "optimised rules" if mode == "optimised" else "guidance as written",
                ", %d reference excerpt%s" % (len(kb_hits), "" if len(kb_hits) == 1 else "s") if kb_hits else ""))
        system, user = score_mod.build_prompt(ver["rubric_name"], ver["version_no"], crit, text, diarized,
                                             voice_ai_name=(c.get("AUDITLY_VOICE_AI_NAME") or "").strip() or None,
                                             use_optimised=(mode == "optimised"), kb_excerpts=kb_hits)
        schema = score_mod.schema_for([x["key"] for x in crit])
        max_tokens = core.cfg_int(c, "SCORING_MAX_TOKENS", 4000)
        def ask(u):
            out = scorer.complete_json(system, u, schema, "scorecard", max_tokens)
            lu = scorer.last_usage or {}
            for k in ("prompt_tokens", "completion_tokens", "cached_tokens"):
                usage[k] += int(lu.get(k) or 0)
            usage["calls"] += 1
            return out
        try:
            raw = ask(user)
        except llm.ProviderError as e:
            if "not JSON" in (e.body or "") and not isinstance(e, llm.SpendBlocked):
                raw = ask(user + "\n\nReturn only the JSON object.")
            else:
                raise
        missing = score_mod.missing_keys(raw, crit)
        if missing:                                   # belt and braces: the schema should prevent this
            _set(db, job_id, progress="Scoring %d criteria… (asking again for %d left out)" % (len(crit), len(missing)))
            try:
                again = ask(user + "\n\nYour previous answer left out these criteria: %s. "
                            "Score ALL %d criteria this time." % (", ".join(missing), len(crit)))
                if len(score_mod.missing_keys(again, crit)) < len(missing):
                    raw = again
            except llm.ProviderError:
                pass
        clean, warnings = score_mod.validate_result(raw, crit, text)
        pct, basis = score_mod.compute_overall(clean["items"])
        # tone & delivery from the timings, now that the scorer has said who the agent is
        tone_d = tone.compute(utts, clean["agent_speaker"], clean.get("speakers"))

        sc_id = core.new_id()
        db.execute("DELETE FROM scorecard WHERE job_id=?", (job_id,))
        db.execute("INSERT INTO scorecard (id,job_id,recording_id,transcript_id,rubric_version_id,"
                   "agent_speaker,overall_pct,applicable_weight,auto_fail,summary,call_summary,strengths_json,"
                   "opportunities_json,misses_json,warnings_json,truncated,model,prompt_version,created_at,"
                   "sentiment_json,call_reasons_json,customer_json,speakers_json,transferred,guidance_mode,tone_json,kb_json,kb_key,call_facts_json)"
                   " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (sc_id, job_id, rec["id"], transcript_id, ver["id"], clean["agent_speaker"],
                    pct, basis, 1 if clean["auto_fail"] else 0, clean["summary"], clean.get("call_summary") or None,
                    json.dumps(clean["strengths"]), json.dumps(clean["opportunities"]),
                    json.dumps(clean["misses"]), json.dumps(warnings), 1 if truncated else 0,
                    scorer.model, score_mod.PROMPT_VERSION, core.now(),
                    json.dumps(clean["sentiment"]) if clean.get("sentiment") else None,
                    json.dumps(clean.get("call_reasons") or []),
                    json.dumps(clean["customer"]) if clean.get("customer") else None,
                    json.dumps(clean.get("speakers") or []) if clean.get("speakers") else None,
                    1 if clean.get("transferred") else 0, mode, json.dumps(tone_d) if tone_d else None,
                    json.dumps(kb.public_hits(kb_hits)) if kb_hits else None, kb_key,
                    json.dumps(clean["call_facts"]) if clean.get("call_facts") else None))
        _set(db, job_id, usage_json=json.dumps(usage))
        for it in clean["items"]:
            db.execute("INSERT INTO scorecard_item (scorecard_id,criterion_id,rating,score,weight,"
                       "rationale,evidence_json) VALUES (?,?,?,?,?,?,?)",
                       (sc_id, it["criterion_id"], it["rating"], it["score"], it["weight"],
                        it["rationale"], json.dumps(it["evidence"])))
        _fill_customer(db, rec, clean.get("customer"))
        carried = _carry_over(db, job, sc_id, ver["id"])
        _set(db, job_id, status="done", progress=carried or None, finished_at=core.now())
        core.audit(db, "job.done", target=job_id, detail="%.1f%% · %s" % (pct, mode))
        log("job %s done: %.1f%%" % (job_id[:8], pct))
        # a new value the Dashboard counts: is this agent now below target? (real calls only; 0.51.0)
        try:
            if (rec["kind"] or "real") == "real" and rec["agent_name"]:
                notify.check_failing(db, rec["agent_name"], "real", recording_id=rec["id"])
        except Exception as e:                   # noqa: BLE001 -- a notification never fails a done job
            log("notify: %r" % e)
    except Exception as e:                       # noqa: BLE001 -- the job must record its failure
        # job.error is shown in the UI, so a path from open() on a vanished upload must not
        # survive (invariant 1: recording.path never leaves the server)
        msg = core.redact_paths(str(e), c)[:600] or e.__class__.__name__
        log("job %s failed: %s" % (job_id[:8], msg))
        if not isinstance(e, (llm.ProviderError, RuntimeError, ValueError)):
            log(traceback.format_exc())
        _set(db, job_id, status="failed", error=msg, progress=None, finished_at=core.now())
        core.audit(db, "job.failed", target=job_id, detail=msg[:200])
        _notify_failed(db, job_id, job, rec, msg)
    finally:
        db.close()


def _fill_customer(db, rec, cust):
    """Copy what the call gave (name, company, email, phone, ticket number) onto the recording -- only into blank
    columns, so anything a reviewer typed wins -- and rebuild the audit name when it is still the
    automatic one (agent — date) so the customer's company and name appear in it."""
    if not cust:
        return
    cur = db.execute("SELECT * FROM recording WHERE id=?", (rec["id"],)).fetchone()
    if not cur:
        return
    sets = {}
    for col, key in (("caller_name", "name"), ("caller_company", "company"), ("caller_email", "email"),
                     ("caller_phone", "phone"), ("ticket_ref", "reference")):        # 0.61.0: phone and ticket too
        if not (cur[col] or "").strip() and cust.get(key):
            sets[col] = cust[key]
    if not sets:
        return
    date = (cur["uploaded_at"] or "")[:10]
    auto_before = core.audit_name_for(cur["agent_name"], cur["caller_company"], cur["caller_name"], date)
    if not (cur["audit_name"] or "").strip() or cur["audit_name"] == auto_before:
        sets["audit_name"] = core.audit_name_for(cur["agent_name"], sets.get("caller_company", cur["caller_company"]),
                                                 sets.get("caller_name", cur["caller_name"]), date)
    db.execute("UPDATE recording SET %s WHERE id=?" % ", ".join("%s=?" % k for k in sets),
               list(sets.values()) + [rec["id"]])
    core.audit(db, "recording.customer_from_call", target=rec["id"],
               detail="; ".join("%s: %s" % (k, v) for k, v in sets.items())[:300])


def _carry_over(db, job, sc_id, rubric_version_id):
    """Rescore / audit again with carry_over: copy the reviewer's overrides (same rubric version
    only -- criteria differ otherwise) and the audit notes as a DRAFT review onto the new run.
    Never the submitted status or the final score: those belong to the run they were given on.
    Returns a progress note, or None."""
    src_id = job["carry_from_scorecard_id"] if "carry_from_scorecard_id" in job.keys() else None
    if not src_id:
        return None
    src = db.execute("SELECT * FROM scorecard WHERE id=?", (src_id,)).fetchone()
    if not src:
        return None
    n = 0
    if src["rubric_version_id"] == rubric_version_id:
        for it in db.execute("SELECT * FROM scorecard_item WHERE scorecard_id=? AND override_score IS NOT NULL", (src_id,)):
            cur = db.execute("UPDATE scorecard_item SET override_score=?, override_note=?, override_by=?, override_at=?"
                             " WHERE scorecard_id=? AND criterion_id=?",
                             (min(int(it["override_score"]), int(it["weight"])), it["override_note"], it["override_by"],
                              it["override_at"], sc_id, it["criterion_id"]))
            n += cur.rowcount
        if n:
            rows = [dict(r) for r in db.execute("SELECT * FROM scorecard_item WHERE scorecard_id=?", (sc_id,))]
            pct, basis = score_mod.compute_overall(rows)
            crit_ids = {r["criterion_id"] for r in rows}
            critical = {r["id"] for r in db.execute("SELECT id FROM criterion WHERE rubric_version_id=? AND critical=1", (rubric_version_id,))}
            auto_fail = 1 if any(r["criterion_id"] in critical and r["rating"] != "na"
                                 and (int(r["override_score"]) == 0 if r["override_score"] is not None else r["rating"] == "missed")
                                 for r in rows) else 0
            db.execute("UPDATE scorecard SET overall_pct=?, applicable_weight=?, auto_fail=? WHERE id=?", (pct, basis, auto_fail, sc_id))
    rv = db.execute("SELECT * FROM review WHERE scorecard_id=?", (src_id,)).fetchone()
    notes = False
    if rv and any((rv[k] or "").strip() for k in ("coaching_notes", "resolution_notes", "action_plan") if k in rv.keys()):
        if not db.execute("SELECT 1 FROM review WHERE scorecard_id=?", (sc_id,)).fetchone():
            now = core.now()
            db.execute("INSERT INTO review (id,scorecard_id,recording_id,reviewer_email,status,coaching_notes,resolution_notes,"
                       "action_plan,follow_up_on,created_at,updated_at) VALUES (?,?,?,?,'draft',?,?,?,?,?,?)",
                       (core.new_id(), sc_id, src["recording_id"], rv["reviewer_email"], rv["coaching_notes"],
                        rv["resolution_notes"], rv["action_plan"], rv["follow_up_on"], now, now))
            notes = True
    if not n and not notes:
        return None
    parts = []
    if n:
        parts.append("%d override%s" % (n, "" if n == 1 else "s"))
    if notes:
        parts.append("the audit notes (as a draft)")
    note = "Carried over %s from the run of %s" % (" and ".join(parts), (src["created_at"] or "")[:10])
    core.audit(db, "scorecard.carry_over", target=sc_id, detail=note[:300])
    return note


def _cached_transcript(db, sha256, provider, model):
    """The newest finished transcript of the same audio bytes by the same engine, on any recording."""
    if not sha256:
        return None
    return db.execute("SELECT t.* FROM transcript t JOIN recording r ON r.id=t.recording_id"
                      " WHERE r.sha256=? AND t.provider=? AND t.model IS ? ORDER BY t.created_at DESC LIMIT 1",
                      (sha256, provider, model)).fetchone()


def _copy_transcript(db, src, recording_id, job_id):
    tid = core.new_id()
    db.execute("INSERT INTO transcript (id,recording_id,job_id,provider,model,language,full_text,duration_s,"
               "cached_from,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
               (tid, recording_id, job_id, src["provider"], src["model"], src["language"], src["full_text"],
                src["duration_s"], src["cached_from"] or src["id"], core.now()))
    db.execute("INSERT INTO utterance (transcript_id,seq,start_s,end_s,speaker,text,confidence,sentiment,sentiment_score)"
               " SELECT ?,seq,start_s,end_s,speaker,text,confidence,sentiment,sentiment_score FROM utterance WHERE transcript_id=?",
               (tid, src["id"]))
    if src["duration_s"] is not None:
        db.execute("UPDATE recording SET duration_s=? WHERE id=? AND duration_s IS NULL", (src["duration_s"], recording_id))
    return tid


def _store_transcript(db, recording_id, job_id, result):
    tid = core.new_id()
    utts = result.get("utterances") or []
    dur = result.get("duration_s")
    if dur is None and utts:
        dur = max(float(u.get("end_s") or 0) for u in utts)
    db.execute("INSERT INTO transcript (id,recording_id,job_id,provider,model,language,full_text,"
               "duration_s,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
               (tid, recording_id, job_id, result.get("provider") or "?", result.get("model"),
                result.get("language"), result.get("full_text"), dur, core.now()))
    for i, u in enumerate(utts):
        sent = u.get("sentiment") if u.get("sentiment") in ("negative", "neutral", "positive") else None
        db.execute("INSERT INTO utterance (transcript_id,seq,start_s,end_s,speaker,text,confidence,sentiment,sentiment_score)"
                   " VALUES (?,?,?,?,?,?,?,?,?)",
                   (tid, i, float(u.get("start_s") or 0), float(u.get("end_s") or 0),
                    u.get("speaker"), (u.get("text") or "").strip(), u.get("confidence"), sent,
                    u.get("sentiment_score") if isinstance(u.get("sentiment_score"), (int, float)) else None))
    if dur is not None:
        db.execute("UPDATE recording SET duration_s=? WHERE id=? AND duration_s IS NULL",
                   (dur, recording_id))
    return tid


# ── threads ───────────────────────────────────────────────────────────────
class Worker(threading.Thread):
    daemon = True

    def run(self):
        while True:
            job_id = JOBQ.get()
            try:
                process(job_id)
            except Exception as e:                # noqa: BLE001
                log("worker crashed on %s: %r" % (job_id, e))
            finally:
                JOBQ.task_done()


def requeue_unfinished(db):
    n = 0
    for r in db.execute("SELECT id FROM job WHERE status IN ('queued','transcribing','scoring')"
                        " AND attempts < ? ORDER BY created_at", (MAX_ATTEMPTS,)):
        db.execute("UPDATE job SET status='queued', progress='Re-queued after restart' WHERE id=?",
                   (r["id"],))
        enqueue(r["id"])
        n += 1
    # A job interrupted on its last attempt would otherwise stay 'transcribing'/'scoring'
    # forever: not re-queued here, and Retry only accepts 'failed'. Fail it so Retry works.
    stuck = db.execute("SELECT j.id, j.recording_id, r.agent_name, r.audit_name, r.filename FROM job j"
                       " LEFT JOIN recording r ON r.id=j.recording_id"
                       " WHERE j.status IN ('queued','transcribing','scoring') AND j.attempts >= ?", (MAX_ATTEMPTS,)).fetchall()
    for r in stuck:                               # otherwise nobody ever learns (0.51.0)
        _notify_failed(db, r["id"], r, r if r["recording_id"] else None,
                       "Interrupted by a server restart after %d attempts. Use Retry to run it again." % MAX_ATTEMPTS)
    db.execute("UPDATE job SET status='failed', progress=NULL, finished_at=?,"
               " error='Interrupted by a server restart after %d attempts. Use Retry to run it again.'"
               " WHERE status IN ('queued','transcribing','scoring') AND attempts >= ?"
               % MAX_ATTEMPTS, (core.now(), MAX_ATTEMPTS))
    return n


def _uploaded_epoch(iso):
    """'2026-09-08T15:49:46+00:00' -> epoch seconds, or None if unparsable."""
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.timestamp()
    except (TypeError, ValueError):
        return None


def retention_sweep(db, days):
    """Delete audio files older than `days`. Rows, transcripts and scorecards stay."""
    if not days or days <= 0:
        return 0
    cutoff = time.time() - days * 86400
    n = 0
    for r in db.execute("SELECT id, path, uploaded_at FROM recording WHERE audio_deleted_at IS NULL"):
        try:
            if not os.path.exists(r["path"]):
                continue
            # age from the upload record, not the file's mtime: a restore or rsync of
            # uploads/ resets every mtime and would keep customer audio indefinitely
            age_ref = _uploaded_epoch(r["uploaded_at"])
            if age_ref is None:
                age_ref = os.path.getmtime(r["path"])
            if age_ref < cutoff:
                os.unlink(r["path"])
                db.execute("UPDATE recording SET audio_deleted_at=? WHERE id=?", (core.now(), r["id"]))
                core.audit(db, "audio.retention_delete", target=r["id"])
                n += 1
        except OSError as e:
            log("retention: could not delete %s: %s" % (r["path"], e))
    return n


class Sweeper(threading.Thread):
    daemon = True

    def run(self):
        while True:
            try:
                c = core.cfg()
                db = core.connect()
                try:
                    n = retention_sweep(db, core.cfg_int(c, "AUDITLY_RETENTION_DAYS", 0))
                    if n:
                        log("retention sweep deleted %d audio file(s)" % n)
                    if notify.prune(db):
                        log("swept notifications older than %d days" % notify.KEEP_DAYS)
                finally:
                    db.close()
            except Exception as e:                # noqa: BLE001
                log("retention sweep error: %r" % e)
            time.sleep(3600)


def start(c):
    n = max(1, core.cfg_int(c, "AUDITLY_WORKERS", 1))
    for _ in range(n):
        Worker().start()
    Sweeper().start()
    rubric_sync.start()
    return n
