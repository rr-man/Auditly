#!/usr/bin/env python3
"""'Optimise for the scorer': rewrite a QA-type version's authored guidance into literal
decision rules (criterion.opt_*) so repeated audits land on the same ratings.

Manual only -- a reviewer presses the button; nothing runs at boot or on save. Derived
data on the same version: the authored criteria are never touched. One daemon thread
pulls version ids off a queue; every state change is its own autocommit UPDATE so the
page can poll.  none -> pending -> running -> done | failed.

Guardrails: a run retries itself up to MAX_SYNC_ATTEMPTS times on provider trouble or a
rewrite that fails validation; a run only counts against the caps (per version, per UTC
day) once it reaches done or failed; spending and key checks happen BEFORE anything is
queued (rubric_sync.readiness), so a mis-configured server refuses instead of failing.
"""
import json
import math
import queue
import threading
import time
import traceback
from datetime import datetime, timezone

import core
import llm
import rubric as rubric_mod
import score as score_mod

SYNCQ = queue.Queue()
MAX_SYNC_ATTEMPTS = 3
BACKOFF_S = (2, 6)                 # between attempt 1->2 and 2->3
STATES = ("none", "pending", "running", "done", "failed")
LIVE = ("pending", "running")
_log = None


def set_logger(fn):
    global _log
    _log = fn


def log(msg):
    if _log:
        _log(msg)


def enqueue(version_id):
    SYNCQ.put(version_id)


def _set(db, vid, **kw):
    cols = ", ".join("%s=?" % k for k in kw)
    db.execute("UPDATE rubric_version SET %s WHERE id=?" % cols, list(kw.values()) + [vid])


# ── what the button may do right now ──────────────────────────────────────
def readiness(c):
    """None when an optimisation could be run, else the plain reason it cannot. Demo never
    spends. Checked in the POST handler so nothing is queued on a server that cannot pay."""
    if (c.get("AUDITLY_DEMO") or "").strip() == "1":
        return None
    p, _m = llm.default_scorer(c)
    kp = llm.key_problem(c, p)
    if kp:
        return "Optimisation needs a working scoring key on the server. " + kp
    if (c.get("AUDITLY_ALLOW_SPEND") or "").strip() != "1":
        return "Optimisation needs AUDITLY_ALLOW_SPEND=1 on the server; spending is off, so the guidance as written is in use."
    return None


def caps(db, c, ver):
    """Per-version and per-day limits. A run counts once it reaches done or failed
    (sync_runs); retries inside a run and interrupted runs do not."""
    per_v = max(0, core.cfg_int(c, "AUDITLY_SYNC_MAX_RUNS_PER_VERSION", 1))
    per_d = max(0, core.cfg_int(c, "AUDITLY_SYNC_MAX_RUNS_PER_DAY", 5))
    used_v = int(core.field(ver, "sync_runs") or 0)
    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    used_d = db.execute("SELECT COUNT(*) c FROM audit WHERE action='rubric.sync.requested' AND at>=?",
                        (midnight,)).fetchone()["c"]
    reason = None
    if used_v >= per_v:
        reason = ("This version has used its %d optimisation run%s. Save a new version to optimise again."
                  % (per_v, "" if per_v == 1 else "s"))
    elif used_d >= per_d:
        reason = "The server's %d optimisation runs for today are used up. Try again tomorrow (UTC)." % per_d
    return {"runs_used": used_v, "runs_max": per_v, "today_used": used_d, "today_max": per_d,
            "allowed": reason is None, "reason": reason}


def estimate(db, c, ver, crit=None):
    """A rough cost so the confirm dialog can show it: prompt chars / 4 + ~700 output tokens
    per criterion, at the default scorer's list price. 0 in demo."""
    crit = crit if crit is not None else rubric_mod.criteria_for(db, ver["id"])
    demo = (c.get("AUDITLY_DEMO") or "").strip() == "1"
    p, m = ("demo", "demo") if demo else llm.default_scorer(c)
    user = rubric_mod.optimise_user(core.field(ver, "rubric_name") or "", ver["version_no"], crit, ver["source_text"])
    in_tok = (len(rubric_mod.OPTIMISE_SYSTEM) + len(user)) / 4.0
    out_tok = 700.0 * len(crit)
    pin, pout = llm.PRICES["llm"].get(m, (0.0, 0.0))
    return {"provider": p, "model": m, "criteria": len(crit),
            "est_usd": round((in_tok * pin + out_tok * pout) / 1e6, 4)}


def optimised_scorecards(db, vid):
    return db.execute("SELECT COUNT(*) c FROM scorecard WHERE rubric_version_id=? AND guidance_mode='optimised'",
                      (vid,)).fetchone()["c"]


def reset_and_enqueue(db, vid):
    """Back to a clean pending row (opt_* cleared, run counter kept) and onto the queue."""
    db.execute("UPDATE criterion SET opt_description=NULL, opt_met=NULL, opt_partial=NULL, opt_missed=NULL,"
               " opt_na=NULL WHERE rubric_version_id=?", (vid,))
    _set(db, vid, sync_status="pending", sync_error=None, sync_attempts=0, sync_started_at=None,
         sync_finished_at=None, sync_provider=None, sync_model=None, sync_usage_json=None)
    enqueue(vid)


# ── the run ───────────────────────────────────────────────────────────────
def _claim(db, vid):
    """Atomic pending -> running; False when another runner has it or it is not pending."""
    cur = db.execute("UPDATE rubric_version SET sync_status='running', sync_started_at=?, sync_finished_at=NULL,"
                     " sync_error=NULL, sync_attempts=1 WHERE id=? AND sync_status='pending'", (core.now(), vid))
    return cur.rowcount == 1


def _version(db, vid):
    return db.execute("SELECT v.*, r.name rubric_name FROM rubric_version v JOIN rubric r ON r.id=v.rubric_id"
                      " WHERE v.id=?", (vid,)).fetchone()


def _once(db, vid, c, usage):
    ver = _version(db, vid)
    if not ver:
        raise RuntimeError("QA type version no longer exists.")
    crit = rubric_mod.criteria_for(db, vid)
    if not crit:
        raise RuntimeError("This version has no criteria.")
    scorer = llm.scorer_for(c)
    _set(db, vid, sync_provider=scorer.name, sync_model=scorer.model)
    clean, calls = rubric_mod.optimise_criteria(crit, scorer, ver["rubric_name"], ver["version_no"],
                                                ver["source_text"], usage)
    # opt_* and 'done' land together: a reader never sees done with half the criteria filled
    db.execute("BEGIN IMMEDIATE")
    try:
        for row in crit:
            o = clean[row["key"]]
            db.execute("UPDATE criterion SET opt_description=?, opt_met=?, opt_partial=?, opt_missed=?, opt_na=?"
                       " WHERE id=?", (o["description"], o["met"], o["partial"], o["missed"], o["na"], row["id"]))
        db.execute("UPDATE rubric_version SET sync_status='done', sync_error=NULL, sync_finished_at=?,"
                   " sync_runs=sync_runs+1, sync_usage_json=? WHERE id=?", (core.now(), json.dumps(usage), vid))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    core.audit(db, "rubric.sync.done", target=vid,
               detail="v%d · %s:%s · %d call(s)" % (ver["version_no"], scorer.name, scorer.model, calls))
    log("sync %s done (v%d, %s)" % (vid[:8], ver["version_no"], scorer.model))


def _finish(db, vid, msg, c):
    msg = core.redact_secrets(core.redact_paths(msg, c))[:600] or "Unknown error"
    db.execute("UPDATE rubric_version SET sync_status='failed', sync_error=?, sync_finished_at=?,"
               " sync_runs=sync_runs+1, sync_usage_json=NULL WHERE id=?", (msg, core.now(), vid))
    core.audit(db, "rubric.sync.failed", target=vid, detail=msg[:200])
    log("sync %s failed: %s" % (vid[:8], msg))


def run(vid, c=None):
    """Run one optimisation to a terminal state. Safe to call synchronously (CLI, tests)."""
    c = c or core.cfg()
    db = core.connect()
    try:
        if not _claim(db, vid):
            return
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "calls": 0}
        last = None
        for i in range(MAX_SYNC_ATTEMPTS):
            if i:
                _set(db, vid, sync_attempts=i + 1)
            try:
                _once(db, vid, c, usage)
                return
            except llm.SpendBlocked as e:               # config changed under us: nothing to retry
                last = e
                break
            except (llm.ProviderError, ValueError) as e:  # provider trouble, or a rewrite that will not validate
                last = e
                log("sync %s attempt %d failed: %s" % (vid[:8], i + 1, str(e)[:200]))
                if i < MAX_SYNC_ATTEMPTS - 1:
                    time.sleep(BACKOFF_S[i])
                    continue
            except Exception as e:                       # noqa: BLE001 -- a bug is recorded, not retried
                last = e
                log(traceback.format_exc())
                break
        _finish(db, vid, str(last) or last.__class__.__name__, c)
    finally:
        db.close()


def requeue_unfinished(db):
    """At boot: a run interrupted by a restart is failed with a plain reason and does NOT
    count as a used run. Nothing is re-queued automatically -- optimisation is manual."""
    cur = db.execute("UPDATE rubric_version SET sync_status='failed', sync_finished_at=?, sync_error=?"
                     " WHERE sync_status IN ('pending','running')",
                     (core.now(), "Interrupted by a server restart. Press Optimise to run it again."))
    return cur.rowcount


class Syncer(threading.Thread):
    daemon = True

    def run(self):
        while True:
            vid = SYNCQ.get()
            try:
                run(vid)
            except Exception as e:                        # noqa: BLE001 -- the thread must survive
                log("syncer crashed on %s: %r" % (vid, e))
            finally:
                SYNCQ.task_done()


def start():
    Syncer().start()


# ── evaluate: does the optimisation make scoring more repeatable? ─────────
def _pick_transcript(db, ref):
    if ref and ref != "latest":
        return db.execute("SELECT * FROM transcript WHERE id=?", (ref,)).fetchone()
    return db.execute("SELECT t.* FROM transcript t JOIN recording r ON r.id=t.recording_id"
                      " WHERE r.deleted_at IS NULL ORDER BY t.created_at DESC LIMIT 1").fetchone()


def _pick_version(db, vid):
    if vid:
        return _version(db, vid)
    r = db.execute("SELECT v.*, r.name rubric_name FROM rubric_version v JOIN rubric r ON r.id=v.rubric_id"
                   " WHERE r.archived=0 ORDER BY r.created_at DESC, v.version_no DESC LIMIT 1").fetchone()
    return r


def _stddev(xs):
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def evaluate(db, c, transcript_ref, version_id, runs, scorer_id, out):
    """Score one transcript `runs` times with the authored guidance and `runs` times with the
    optimised rules, in memory, and print agreement + spread. SELECT-only: no scorecard, job
    or audit row is written. Returns a process exit code."""
    demo = (c.get("AUDITLY_DEMO") or "").strip() == "1"
    if scorer_id:
        pm = llm.parse_choice(c, "llm", scorer_id)
        if not pm:
            out("Unknown scorer %r. Use provider:model from the Settings menu." % scorer_id)
            return 2
        prov, model = pm
    else:
        prov, model = llm.default_scorer(c)
    if not demo:
        kp = llm.key_problem(c, prov)
        if kp:
            out(kp)
            return 2
        if (c.get("AUDITLY_ALLOW_SPEND") or "").strip() != "1":
            out("AUDITLY_ALLOW_SPEND is not 1; refusing to call a paid provider. Use --demo or set it in .env.")
            return 2
    tr = _pick_transcript(db, transcript_ref)
    if not tr:
        out("No transcript found.")
        return 2
    ver = _pick_version(db, version_id)
    if not ver:
        out("No QA type version found.")
        return 2
    crit = rubric_mod.criteria_for(db, ver["id"])
    utts = [dict(r) for r in db.execute("SELECT * FROM utterance WHERE transcript_id=? ORDER BY seq", (tr["id"],))]
    text, _trunc = score_mod.format_transcript(utts, core.cfg_int(c, "AUDITLY_MAX_TRANSCRIPT_CHARS", 120000))
    diarized = any(u["speaker"] is not None for u in utts)
    scorer = llm.scorer_for(c, prov, model)
    schema = score_mod.schema_for([x["key"] for x in crit])
    max_tokens = core.cfg_int(c, "SCORING_MAX_TOKENS", 4000)
    runs = max(1, int(runs or 3))
    out("Determinism check — transcript %s… (%d utterances) · %s v%d (%d criteria) · %s:%s · %d runs per mode"
        % (tr["id"][:8], len(utts), ver["rubric_name"], ver["version_no"], len(crit), scorer.name, scorer.model, runs))
    out("Nothing is written: no scorecard, job or audit row.\n")

    modes = [("authored", False)]
    if ver["sync_status"] == "done" and rubric_mod.opt_complete(crit):
        modes.append(("optimised", True))
    else:
        out("optimised: skipped — this version is '%s' (press Optimise first).\n" % ver["sync_status"])
    pin, pout = llm.PRICES["llm"].get(scorer.model, (0.0, 0.0))
    results = {}
    for mode, use_opt in modes:
        system, user = score_mod.build_prompt(ver["rubric_name"], ver["version_no"], crit, text, diarized,
                                             voice_ai_name=(c.get("AUDITLY_VOICE_AI_NAME") or "").strip() or None,
                                             use_optimised=use_opt)
        pcts, ratings, fails, ptok, ctok, fps = [], [], 0, 0, 0, []
        for i in range(runs):
            raw = scorer.complete_json(system, user, schema, "scorecard", max_tokens)
            lu = scorer.last_usage or {}
            ptok += int(lu.get("prompt_tokens") or 0)
            ctok += int(lu.get("completion_tokens") or 0)
            fps.append(str(lu.get("fingerprint") or "?"))
            clean, _w = score_mod.validate_result(raw, crit, text)
            pct, _basis = score_mod.compute_overall(clean["items"])
            pcts.append(pct)
            fails += 1 if clean["auto_fail"] else 0
            ratings.append({it["key"]: it["rating"] for it in clean["items"]})
            out("  %-9s run %d/%d: %.1f%%" % (mode, i + 1, runs, pct))
        results[mode] = {"pcts": pcts, "ratings": ratings, "fails": fails, "ptok": ptok, "ctok": ctok, "fps": fps,
                         "usd": (ptok * pin + ctok * pout) / 1e6}
    out("")
    out("%-10s %4s  %-11s %-6s %-6s %-7s %-7s %-9s %-11s %-14s %s"
        % ("mode", "runs", "overall %", "min", "max", "spread", "stddev", "auto-fail", "prompt tok", "completion tok", "est USD"))
    agree = {}
    for mode, r in results.items():
        p = r["pcts"]
        out("%-10s %4d  %-11s %-6.1f %-6.1f %-7.1f %-7.2f %-9s %-11s %-14s %.4f"
            % (mode, runs, "%.1f/%.1f" % (min(p), max(p)), min(p), max(p), max(p) - min(p), _stddev(p),
               "%d/%d" % (r["fails"], runs), "{:,}".format(r["ptok"]), "{:,}".format(r["ctok"]), r["usd"]))
        per = []
        for cr in crit:
            vals = [rt.get(cr["key"]) for rt in r["ratings"]]
            modal = max(set(vals), key=vals.count)
            per.append(vals.count(modal) / float(len(vals)))
        agree[mode] = sum(per) / len(per) if per else 1.0
    out("\nper-criterion agreement (share of runs giving the modal rating)")
    out("%-22s %s" % ("key", "   ".join("%-30s" % m for m in results)))
    for cr in crit:
        cells = []
        for mode, r in results.items():
            vals = [rt.get(cr["key"]) for rt in r["ratings"]]
            counts = {}
            for v in vals:
                counts[v] = counts.get(v, 0) + 1
            modal = max(counts, key=counts.get)
            desc = " ".join("%s ×%d" % (k, n) for k, n in sorted(counts.items(), key=lambda kv: -kv[1]))
            cells.append("%-30s" % ("%s  %3d%%" % (desc, round(100.0 * counts[modal] / len(vals)))))
        out("%-22s %s" % (cr["key"][:22], "   ".join(cells)))
    out("\nsystem_fingerprint: " + " · ".join("%s %s" % (m, ", ".join(sorted(set(r["fps"])))) for m, r in results.items()))
    if "optimised" in results:
        a, o = results["authored"], results["optimised"]
        sa, so = max(a["pcts"]) - min(a["pcts"]), max(o["pcts"]) - min(o["pcts"])
        ga, go = agree["authored"], agree["optimised"]
        if go > ga + 1e-9 or (abs(go - ga) < 1e-9 and so < sa - 1e-9):
            verdict = "MORE"
        elif go < ga - 1e-9 or (abs(go - ga) < 1e-9 and so > sa + 1e-9):
            verdict = "LESS"
        else:
            verdict = "EQUALLY"
        out("\nVerdict: optimised guidance is %s consistent (mean agreement %d%% vs %d%%, spread %.1f vs %.1f).%s"
            % (verdict, round(100 * go), round(100 * ga), so, sa,
               "  Do not deploy this version's optimisation." if verdict == "LESS" else ""))
    else:
        out("\nVerdict: only the authored mode ran (mean agreement %d%%); optimise the version and run again."
            % round(100 * agree["authored"]))
    return 0
