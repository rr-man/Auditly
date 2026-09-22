#!/usr/bin/env python3
"""Auditly -- the HTTP server.

  http://<host>:8084/             the app
  http://<host>:8084/health       liveness, no secrets

  python3 auditly_host.py                    run
  python3 auditly_host.py --init-db          create/upgrade the database and exit
  python3 auditly_host.py --add-user EMAIL [--role admin|reviewer]
  python3 auditly_host.py --demo             fake providers + seeded sample data, no keys

This file is deliberately NOT called serve.py: other tools on this box run
`pkill -f "[p]ython3 .*serve\\.py"` (see ~/cfbuilder-serve/restart.sh).

Two rules this file keeps:
  1. to_public() is the ONLY function that turns a database row into a response
     body. It is a whitelist. recording.path, sha256 and anything in .env have no
     path out.
  2. Provider keys never enter this file's response path. /api/health reports
     key_present and key_len; nothing else.
"""
import argparse
import csv
import getpass
import hashlib
import http.server
import io
import json
import os
import re
import secrets
import sqlite3
import socketserver
import ssl
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core                      # noqa: E402
import llm                       # noqa: E402
import notify                    # noqa: E402
import pdfgen                    # noqa: E402
import rubric as rubric_mod      # noqa: E402
import rubric_sync               # noqa: E402
import coaching
import kb
import mailer
import assistant
import transcribe
import score as score_mod
import tone        # noqa: E402
import dashboard                 # noqa: E402
import worker                    # noqa: E402

APP_HTML = os.path.join(core.HERE, "auditly.html")
STARTED_AT = core.now()   # the page is re-read per request, the code is not: /health tells them apart
COOKIE = "auditly_session"
CSRF_HEADER = "X-Auditly-CSRF"
LOGIN_WINDOW_MIN = 15
LOGIN_MAX_FAILS = 8
UUID = r"[0-9a-fA-F-]{36}"
DISPUTE_TOKEN = r"[A-Za-z0-9_-]{32,64}"     # secrets.token_urlsafe(32) is 43 chars; anything else never reaches the DB
LINK_GONE = "This link has expired or is not valid."


def _id_list(v, cap=50):
    """?recording=a,b,c -> ["a","b","c"] (blanks dropped, at most cap). Focus mode sends "none" for an empty
    set, which is a list of one id that matches nothing."""
    return [x.strip() for x in (v or "").split(",") if x.strip()][:cap]


def public_base(c, host_header, secure):
    """The absolute origin the agent's dispute link is built on: AUDITLY_PUBLIC_URL when set, else the
    scheme and Host of the request in hand (behind nginx, X-Forwarded-Proto decides `secure`)."""
    base = (c.get("AUDITLY_PUBLIC_URL") or "").strip().rstrip("/")
    if base:
        return base
    host = (host_header or "").strip() or "localhost"
    return ("https://" if secure else "http://") + host


def link_days(c):
    return max(0, core.cfg_int(c, "AUDITLY_DISPUTE_LINK_DAYS", 14))


# The agent's page (0.52.0): /dispute/<token>. One template, the same nonce CSP as the app, its own styles and
# script (no style="" anywhere, nothing external, no native dialogs). It fetches /data with credentials omitted
# and posts /raise with the CSRF header; an unknown, expired or revoked token gets the same page with the
# "gone" panel and a 404. It is deliberately not auditly.html: it must not be able to call the signed-in API.
DISPUTE_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>Your QA scorecard</title>
<style nonce="__CSP_NONCE__">
:root{color-scheme:light dark;--bg:#f6f7f9;--card:#fff;--ink:#1a2233;--dim:#5b6472;--line:#dfe3ea;--ok:#1d7a4a;--bad:#a8323e;--part:#9a6a00;--btn:#1f3b73;--soft:#eef2fa}
@media (prefers-color-scheme:dark){:root{--bg:#0f1420;--card:#171e2e;--ink:#e7ebf3;--dim:#9aa4b5;--line:#2a3448;--btn:#7ea1e0;--soft:#1f2a44}}
*{box-sizing:border-box} body{margin:0;font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--bg);color:var(--ink)}
.wrap{max-width:860px;margin:0 auto;padding:24px 16px} h1{font-size:22px;margin:0 0 4px} h2{font-size:16px;margin:0 0 8px} h3{font-size:14px;margin:12px 0 4px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin-bottom:14px}
.ring{font-size:34px;font-weight:700;font-variant-numeric:tabular-nums} .pill{display:inline-block;border-radius:999px;padding:1px 9px;font-size:12px;border:1px solid var(--line);vertical-align:middle}
.pill.met{color:var(--ok);border-color:var(--ok)} .pill.missed{color:var(--bad);border-color:var(--bad)} .pill.partial{color:var(--part);border-color:var(--part)}
.pill.fail{background:var(--bad);color:#fff;border-color:var(--bad);font-weight:700}
.crit{border-top:1px solid var(--line);padding:12px 0} .crit:first-child{border-top:none;padding-top:0}
.ev{color:var(--dim);font-size:13px;margin:2px 0 0 12px} .ev b{font-variant-numeric:tabular-nums;margin-right:4px}
.btn{border:1px solid var(--btn);background:var(--btn);color:#fff;border-radius:6px;padding:6px 12px;cursor:pointer;font:inherit;font-size:13px}
.btn.sec{background:transparent;color:var(--btn)} .btn[disabled]{opacity:.5;cursor:default}
textarea{width:100%;min-height:80px;border:1px solid var(--line);border-radius:6px;padding:8px;font:inherit;background:var(--card);color:var(--ink);margin-top:8px}
.row{display:flex;gap:8px;margin-top:6px;flex-wrap:wrap} .hidden{display:none} .dim{color:var(--dim)} .small{font-size:13px} .mt8{margin-top:8px}
.toast{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);background:var(--ink);color:var(--bg);padding:8px 14px;border-radius:8px;font-size:13px;max-width:calc(100vw - 32px)}
.dsp{border-left:3px solid var(--line);padding:6px 10px;margin:8px 0;background:var(--soft);border-radius:0 6px 6px 0} .st-open{border-color:var(--part)} .st-upheld{border-color:var(--ok)} .st-rejected{border-color:var(--bad)}
ul{margin:4px 0 0;padding-left:20px} li{margin:2px 0}
</style></head><body><div class="wrap">
<div id="gone" class="card hidden"><h1>Your QA scorecard</h1><p>This link has expired or is not valid.</p><p class="dim small">Links work for a limited time and stop when the audit is reopened. Ask your QA reviewer for a new one.</p></div>
<div id="app" class="hidden">
  <div class="card"><h1 id="title">Your QA scorecard</h1><div class="small dim" id="sub"></div>
    <div class="mt8"><span class="ring" id="pct"></span> <span id="autofail" class="pill fail hidden" title="A critical item was missed, so the call fails regardless of the total">AUTO-FAIL</span></div>
    <p id="summary"></p><div id="lists"></div>
    <p class="small dim" id="expiry"></p></div>
  <div class="card"><h2>Criteria</h2><p class="small dim">Each score with the reason and the words quoted from the call. If a score is wrong, press Dispute next to it and say why.</p><div id="crits"></div>
    <div class="crit"><button class="btn sec" id="dspAll" type="button" title="Dispute the scorecard as a whole rather than one score">Dispute the whole scorecard</button><div id="formAll" class="hidden"></div></div></div>
  <div class="card"><h2>Your disputes</h2><div id="dsps"></div></div>
  <p class="small dim">Auditly v__APP_VERSION__ · This page shows your scores only: no audio, no customer details.</p>
</div></div>
<script nonce="__CSP_NONCE__">
(function () {
  var base = location.pathname.replace(/\/+$/, ""), H = { "X-Auditly-CSRF": "1" };
  function $(id) { return document.getElementById(id); }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
  function ts(s) { s = Math.max(0, Math.round(+s || 0)); return String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0"); }
  function when(iso) { var d = new Date(iso); return isNaN(d) ? (iso || "") : d.toLocaleString(); }
  function toast(m) { var t = document.createElement("div"); t.className = "toast"; t.textContent = m; document.body.appendChild(t); setTimeout(function () { t.remove(); }, 3500); }
  function gone() { $("app").classList.add("hidden"); $("gone").classList.remove("hidden"); }
  function load() {
    fetch(base + "/data", { headers: H, credentials: "omit" }).then(function (r) { if (!r.ok) { throw new Error("gone"); } return r.json(); }).then(render).catch(gone);
  }
  function sendDispute(critId, reason, btn) {
    btn.disabled = true;
    fetch(base + "/raise", { method: "POST", headers: { "X-Auditly-CSRF": "1", "Content-Type": "application/json" }, credentials: "omit", body: JSON.stringify({ criterion_id: critId, reason: reason }) })
      .then(function (r) { return r.json().then(function (j) { if (!r.ok) { throw new Error(j.error || ("Request failed (" + r.status + ")")); } return j; }); })
      .then(function (v) { toast("Your dispute was sent to the QA team."); render(v); })
      .catch(function (e) { btn.disabled = false; toast(e.message); });
  }
  function form(critId, host) {
    if (!host || host.firstChild) { return; }
    var ta = document.createElement("textarea"); ta.placeholder = "What do you dispute, and why? Say what you said or did on the call.";
    var row = document.createElement("div"); row.className = "row";
    var send = document.createElement("button"); send.className = "btn"; send.type = "button"; send.textContent = "Send dispute";
    var cancel = document.createElement("button"); cancel.className = "btn sec"; cancel.type = "button"; cancel.textContent = "Cancel";
    row.appendChild(send); row.appendChild(cancel); host.appendChild(ta); host.appendChild(row); host.classList.remove("hidden"); ta.focus();
    send.onclick = function () { var r = ta.value.trim(); if (!r) { toast("Say what you dispute and why."); return; } sendDispute(critId, r, send); };
    cancel.onclick = function () { host.innerHTML = ""; host.classList.add("hidden"); };
  }
  function render(v) {
    $("gone").classList.add("hidden"); $("app").classList.remove("hidden");
    $("title").textContent = "Your QA scorecard" + (v.agent_name ? " — " + v.agent_name : "");
    $("sub").textContent = [v.rubric_name ? "QA type " + v.rubric_name + " v" + v.version_no : "", v.call_at ? "call taken " + when(v.call_at) : "", v.submitted_at ? "audit submitted " + when(v.submitted_at) : ""].filter(Boolean).join(" · ");
    var pct = v.final_pct != null ? v.final_pct : v.overall_pct;
    $("pct").textContent = pct == null ? "—" : Number(pct).toFixed(1) + "%";
    $("autofail").classList.toggle("hidden", !v.auto_fail);
    $("summary").textContent = v.summary || "";
    var names = {}; (v.items || []).forEach(function (i) { names[i.key] = i.name; });
    function note(x) { var t = typeof x === "string" ? x : (x && x.text) || "", k = x && typeof x === "object" ? x.key : null; return "<li>" + esc(t) + (k && names[k] ? ' <span class="pill">' + esc(names[k]) + "</span>" : "") + "</li>"; }
    $("lists").innerHTML = [["Strengths", v.strengths], ["Opportunities", v.opportunities], ["Misses", v.misses]].filter(function (x) { return x[1] && x[1].length; })
      .map(function (x) { return "<h3>" + x[0] + "</h3><ul>" + x[1].map(note).join("") + "</ul>"; }).join("");
    $("expiry").textContent = v.expires_at ? "This link works until " + when(v.expires_at) + " or until the audit is reopened." : "";
    var open = {}; (v.disputes || []).forEach(function (d) { if (d.status === "open") { open[d.criterion_id || "*"] = true; } });
    $("crits").innerHTML = (v.items || []).map(function (i) {
      return '<div class="crit"><div><b>' + esc(i.name) + '</b> <span class="pill ' + esc(i.rating) + '">' + esc(i.rating) + '</span> <span class="dim">' + esc(i.final_score) + " / " + esc(i.weight) + (i.critical ? " · critical" : "") + (i.overridden ? " · corrected by the QA" : "") + "</span></div>"
        + (i.rationale ? '<div class="small">' + esc(i.rationale) + "</div>" : "")
        + (i.evidence || []).map(function (e) { return '<div class="ev"><b>' + ts(e.ts) + "</b> “" + esc(e.quote) + "”</div>"; }).join("")
        + (open[i.criterion_id] ? '<div class="small dim mt8">You have an open dispute on this score.</div>'
           : '<div class="row"><button class="btn sec" type="button" data-dsp="' + esc(i.criterion_id) + '" title="Say why this score is wrong">Dispute this score</button></div><div class="f hidden"></div>') + "</div>";
    }).join("");
    document.querySelectorAll("[data-dsp]").forEach(function (b) { b.onclick = function () { form(b.dataset.dsp, b.parentNode.nextElementSibling); }; });
    var all = $("dspAll"); all.disabled = !!open["*"]; all.onclick = function () { form(null, $("formAll")); };
    $("dsps").innerHTML = (v.disputes || []).length ? v.disputes.map(function (d) {
      return '<div class="dsp st-' + esc(d.status) + '"><b>' + esc(d.criterion_name || "Whole scorecard") + '</b> <span class="pill">' + esc(d.status) + "</span>" + (d.via === "qa" ? ' <span class="small dim">recorded by the QA</span>' : "") + "<div>" + esc(d.reason) + "</div>"
        + (d.resolution_note ? '<div class="small dim">Outcome: ' + esc(d.resolution_note) + "</div>" : "") + '<div class="small dim">' + esc(when(d.created_at)) + "</div></div>";
    }).join("") : '<p class="dim small">None yet. If a score looks wrong, press Dispute next to it.</p>';
  }
  load();
})();
</script></body></html>
"""


def log(msg):
    sys.stderr.write("[%s] %s\n" % (time.strftime("%F %T"), msg))
    sys.stderr.flush()


worker.set_logger(log)
rubric_sync.set_logger(log)


# ── the serializer ────────────────────────────────────────────────────────
def _j(s, default):
    try:
        return json.loads(s) if s else default
    except ValueError:
        return default


OPEN_ACCESS_EMAIL = "open-access@local"


def open_access(c):
    """AUDITLY_OPEN_ACCESS=1: no sign-in; every visitor acts as one built-in admin.
    Demo/LAN only -- deploy/install.sh refuses to install with it set."""
    return c.get("AUDITLY_OPEN_ACCESS") == "1"


def open_access_user(db):
    """The row every anonymous visitor becomes under open access. Created on first
    use with a random, never-disclosed password so it cannot be signed in to."""
    r = db.execute("SELECT * FROM user WHERE email=?", (OPEN_ACCESS_EMAIL,)).fetchone()
    if r:
        return r
    h, s = core.hash_password(secrets.token_urlsafe(32))
    # OR IGNORE: a fresh page fires several requests at once, and two of them can get here together
    db.execute("INSERT OR IGNORE INTO user (id,email,name,pw_hash,pw_salt,role,active,created_at)"
               " VALUES (?,?,?,?,?,'admin',1,?)",
               (core.new_id(), OPEN_ACCESS_EMAIL, "Open access", h, s, core.now()))
    db.commit()
    return db.execute("SELECT * FROM user WHERE email=?", (OPEN_ACCESS_EMAIL,)).fetchone()


PERSON_KINDS = ("agent", "qa")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


audit_name_for = core.audit_name_for      # kept as a name for callers and tests; lives in core so the worker can use it


def review_stage(job_status, rv_status, coached_at):
    """Where a call is on its way from scored to coached. None until the job is done."""
    if job_status != "done":
        return None
    if not rv_status:
        return "scored"
    if rv_status == "queued":
        return "queued"
    if rv_status == "draft":
        return "in_review"
    return "coached" if coached_at else "audited"


STAGE_LABEL = {"scored": "scored", "queued": "queued for audit", "in_review": "in review",
               "audited": "audited", "coached": "coached"}


ROLE_LABELS = {"caller": "caller", "agent": "agent", "voice_ai": "voice AI", "other": "other"}


def speakers_text(lst):
    """'S0 agent (Jordan), S1 caller (Maria)' for the exports; '-' when the scorer gave none."""
    if not isinstance(lst, list) or not lst:
        return "-"
    parts = []
    for x in lst:
        if not isinstance(x, dict):
            continue
        role = ROLE_LABELS.get(x.get("role"), x.get("role") or "?")
        parts.append("S%s %s%s" % (x.get("speaker"), role, (" (%s)" % x["name"]) if x.get("name") else ""))
    return ", ".join(parts) or "-"


def sentiment_text(s):
    """'negative -> positive (overall positive)' for exports; '-' when not classified."""
    if not isinstance(s, dict):
        return "-"
    a, b, o = s.get("start") or "?", s.get("end") or "?", s.get("overall") or "?"
    return "%s -> %s (overall %s)" % (a, b, o)


def tone_lines(t):
    """[(label, value)] for the exports; [] when there is no tone data."""
    if not isinstance(t, dict):
        return []
    out = []
    if t.get("talk_ratio_agent") is not None:
        out.append(("agent talk share", "%d%%" % round(t["talk_ratio_agent"] * 100)))
    out.append(("interruptions by agent", str(t.get("interruptions_by_agent", 0))))
    out.append(("interruptions by caller", str(t.get("interruptions_by_caller", 0))))
    out.append(("longest silence", "%ss" % t.get("longest_silence_s", 0)))
    if t.get("agent_wpm") is not None:
        out.append(("agent pace", "%d words/min" % t["agent_wpm"]))
    if t.get("agent_response_latency_s") is not None:
        out.append(("agent response time", "%ss (median)" % t["agent_response_latency_s"]))
    if t.get("caller_trend"):
        out.append(("caller sentiment trend (per line)", t["caller_trend"]))
    return out


def slug(s, n=60):
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", s or "").strip("-.")
    return s[:n].rstrip("-.") or "call"


def person_lookup(db, kind, name):
    return db.execute("SELECT * FROM person WHERE kind=? AND name=? COLLATE NOCASE AND active=1",
                      (kind, name)).fetchone()


def person_problem(db, kind, name):
    """Why a name was refused: still on file but archived, or not on file at all. Names live
    under Settings > Names (the tab is called that; an older message named it differently)."""
    what = "agent" if kind == "agent" else "QA reviewer"
    row = db.execute("SELECT active FROM person WHERE kind=? AND name=? COLLATE NOCASE", (kind, name)).fetchone()
    if row and not row["active"]:
        return "'%s' is archived — choose an active %s, or unarchive it under Settings › Names." % (name, what)
    return "No %s named '%s' — add it under Settings › Names." % (what, name)


EDIT_ROLES = ("admin", "reviewer")      # who may create, version, archive or extract a QA type


def can_edit_rubrics(u):
    """Admins and QA reviewers change QA types; any other (future) role only reads them."""
    return bool(u) and (u.get("role") if isinstance(u, dict) else u["role"]) in EDIT_ROLES


def _s(v):
    """A JSON value as a stripped string: str/int/float pass, anything else (list, dict,
    bool, None) is ''. Bodies are untrusted, and `(d.get(k) or "").strip()` 500s on a list."""
    if isinstance(v, bool) or v is None:
        return ""
    return str(v).strip() if isinstance(v, (str, int, float)) else ""


def to_public(kind, row, **extra):
    """The one place a database row becomes something a browser may see."""
    r = dict(row) if row is not None else {}

    if kind == "rubric":
        return {"id": r.get("id"), "name": r.get("name"), "archived": bool(r.get("archived")),
                "created_at": r.get("created_at"), "created_by": r.get("created_by"),
                "current_version": extra.get("current_version"),
                "current_version_id": extra.get("current_version_id"),
                "criteria_count": extra.get("criteria_count", 0),
                "updated_at": extra.get("updated_at"), "updated_by": extra.get("updated_by"),
                "sync_status": extra.get("sync_status"),
                "versions": extra.get("versions")}
    if kind == "version":
        return {"id": r.get("id"), "rubric_id": r.get("rubric_id"),
                "version_no": r.get("version_no"), "notes": r.get("notes"),
                "source_filename": r.get("source_filename"),
                "source_text": r.get("source_text") if extra.get("with_source") else None,
                "created_at": r.get("created_at"), "created_by": r.get("created_by"),
                "sync_status": r.get("sync_status") or "none", "sync_error": r.get("sync_error"),
                "sync_attempts": r.get("sync_attempts") or 0, "sync_runs": r.get("sync_runs") or 0,
                "sync_started_at": r.get("sync_started_at"), "sync_finished_at": r.get("sync_finished_at"),
                "sync_provider": r.get("sync_provider"), "sync_model": r.get("sync_model"),
                "optimised_scorecards": extra.get("optimised_scorecards"),
                "criteria": extra.get("criteria")}
    if kind == "sync":
        # the poll/precheck body for "Optimise for the scorer": status + what the button may do
        return {"version_id": r.get("id"), "rubric_id": r.get("rubric_id"), "version_no": r.get("version_no"),
                "sync_status": r.get("sync_status") or "none", "sync_error": r.get("sync_error"),
                "sync_attempts": r.get("sync_attempts") or 0, "sync_runs": r.get("sync_runs") or 0,
                "sync_started_at": r.get("sync_started_at"), "sync_finished_at": r.get("sync_finished_at"),
                "sync_provider": r.get("sync_provider"), "sync_model": r.get("sync_model"),
                "max_attempts": rubric_sync.MAX_SYNC_ATTEMPTS,
                "optimised_scorecards": extra.get("optimised_scorecards", 0),
                "runs_used": extra.get("runs_used", 0), "runs_max": extra.get("runs_max", 0),
                "today_used": extra.get("today_used", 0), "today_max": extra.get("today_max", 0),
                "can_run": bool(extra.get("can_run")), "reason": extra.get("reason"), "criteria_count": extra.get("criteria_count", 0),
                "est_usd": extra.get("est_usd", 0.0), "model": extra.get("model"), "provider": extra.get("provider")}
    if kind == "criterion":
        return {"id": r.get("id"), "seq": r.get("seq"), "key": r.get("key"), "name": r.get("name"),
                "description": r.get("description"), "weight": r.get("weight"),
                "guidance_met": r.get("guidance_met"), "guidance_partial": r.get("guidance_partial"),
                "guidance_missed": r.get("guidance_missed"), "critical": bool(r.get("critical")),
                "example_good": r.get("example_good"), "example_bad": r.get("example_bad"),
                "opt_description": r.get("opt_description"), "opt_met": r.get("opt_met"),
                "opt_partial": r.get("opt_partial"), "opt_missed": r.get("opt_missed"), "opt_na": r.get("opt_na")}
    if kind == "recording":
        return {"id": r.get("id"), "filename": r.get("filename"), "ext": r.get("ext"),
                "bytes": r.get("bytes"), "duration_s": r.get("duration_s"),
                "agent_name": r.get("agent_name"), "qa_name": r.get("qa_name"),
                "caller_company": r.get("caller_company"), "caller_name": r.get("caller_name"),
                "caller_email": r.get("caller_email"), "kind": r.get("kind") or "real",
                "caller_phone": r.get("caller_phone"), "ticket_ref": r.get("ticket_ref"),
                "audit_name": r.get("audit_name"), "call_ref": r.get("call_ref"),
                "notes": r.get("notes"), "uploaded_at": r.get("uploaded_at"),
                "uploaded_by": r.get("uploaded_by"),
                "audio_available": not r.get("audio_deleted_at") and not r.get("deleted_at"),
                "audio_url": ("/api/recordings/%s/audio" % r.get("id"))
                             if not r.get("audio_deleted_at") else None}
    if kind == "job":
        return {"id": r.get("id"), "recording_id": r.get("recording_id"),
                "rubric_version_id": r.get("rubric_version_id"),
                "rubric_name": extra.get("rubric_name"), "version_no": extra.get("version_no"),
                "stt_provider": r.get("stt_provider"), "stt_model": r.get("stt_model"),
                "scoring_provider": r.get("scoring_provider"), "scoring_model": r.get("scoring_model"),
                "status": r.get("status"), "progress": r.get("progress"), "error": r.get("error"),
                "attempts": r.get("attempts"), "created_at": r.get("created_at"),
                "started_at": r.get("started_at"), "finished_at": r.get("finished_at"),
                "created_by": r.get("created_by"), "transcript_mode": r.get("transcript_mode") or "auto",
                "usage": _j(r.get("usage_json"), None),
                "recording": extra.get("recording"),
                "scorecard_id": extra.get("scorecard_id"),
                "overall_pct": extra.get("overall_pct"), "auto_fail": extra.get("auto_fail"),
                "stage": extra.get("stage"), "review_status": extra.get("review_status"),
                "final_pct": extra.get("final_pct"), "open_disputes": extra.get("open_disputes", 0)}
    if kind == "utterance":
        return {"seq": r.get("seq"), "start_s": r.get("start_s"), "end_s": r.get("end_s"),
                "speaker": r.get("speaker"), "text": r.get("text"),
                "sentiment": r.get("sentiment"), "sentiment_score": r.get("sentiment_score")}
    if kind == "transcript":
        return {"id": r.get("id"), "provider": r.get("provider"), "model": r.get("model"),
                "language": r.get("language"), "duration_s": r.get("duration_s"),
                "cached_from": r.get("cached_from"),
                "created_at": r.get("created_at"), "utterances": extra.get("utterances", [])}
    if kind == "scorecard":
        return {"dispute_link": extra.get("dispute_link"), "id": r.get("id"), "job_id": r.get("job_id"), "recording_id": r.get("recording_id"),
                "rubric_version_id": r.get("rubric_version_id"),
                "rubric_name": extra.get("rubric_name"), "version_no": extra.get("version_no"),
                "agent_speaker": r.get("agent_speaker"),
                # every criterion 'na' -> nothing was scored; 0.0 in the row must not read as 0%
                "overall_pct": r.get("overall_pct") if (r.get("applicable_weight") or 0) > 0 else None,
                "applicable_weight": r.get("applicable_weight"), "auto_fail": bool(r.get("auto_fail")),
                "summary": r.get("summary"), "call_summary": r.get("call_summary"),
                "strengths": _j(r.get("strengths_json"), []),
                "opportunities": _j(r.get("opportunities_json"), []),
                "misses": _j(r.get("misses_json"), []), "warnings": _j(r.get("warnings_json"), []),
                "truncated": bool(r.get("truncated")), "model": r.get("model"), "prompt_version": r.get("prompt_version"),
                "guidance_mode": r.get("guidance_mode") or "authored",
                "sentiment": _j(r.get("sentiment_json"), None),
                "customer": _j(r.get("customer_json"), None),
                "call_facts": _j(r.get("call_facts_json"), None),
                "speakers": _j(r.get("speakers_json"), None),
                "transferred": (bool(r.get("transferred")) if r.get("transferred") is not None else None),
                "call_reasons": [dict(x, label=score_mod.REASON_LABELS.get(x.get("category"), x.get("category")))
                                 for x in _j(r.get("call_reasons_json"), []) if isinstance(x, dict)],
                "created_at": r.get("created_at"), "items": extra.get("items", []),
                "recording": extra.get("recording"), "transcript": extra.get("transcript"),
                "review": extra.get("review"), "stage": extra.get("stage"),
                "disputes": extra.get("disputes", []), "open_disputes": extra.get("open_disputes", 0),
                "tone": _j(r.get("tone_json"), None),
                "tone_agrees": tone.trend_agrees(_j(r.get("tone_json"), None), _j(r.get("sentiment_json"), None)),
                "kb": _j(r.get("kb_json"), []) or [], "kb_used": r.get("kb_key") is not None}
    if kind == "kb_doc":
        return {"id": r.get("id"), "title": r.get("title"), "filename": r.get("filename"), "chars": r.get("chars") or 0,
                "enabled": bool(r.get("enabled")), "rubric_id": r.get("rubric_id"), "rubric_name": extra.get("rubric_name"),
                "uploaded_by": r.get("uploaded_by"), "created_at": r.get("created_at"), "updated_at": r.get("updated_at"),
                "chunks": extra.get("chunks", 0), "text": r.get("text") if extra.get("with_text") else None,
                "used_by": extra.get("used_by", 0)}
    if kind == "ask_voice":
        return {"text": extra.get("text", ""), "duration_s": r.get("audio_s"), "provider": r.get("provider"), "model": r.get("model"),
                "chars": r.get("chars"), "created_at": r.get("created_at")}
    if kind == "ask_prompt":
        # the admin's editable half of the assistant's prompt; persona '' = a reset to the built-in text
        return {"id": r.get("id"), "version_no": r.get("version_no"), "persona": r.get("persona") or "",
                "builtin": not (r.get("persona") or "").strip(), "notes": r.get("notes"),
                "created_at": r.get("created_at"), "created_by": r.get("created_by")}
    if kind == "ask_setting":
        return {"enabled": (None if r.get("enabled") is None else bool(r.get("enabled"))), "model": r.get("model") or "",
                "max_per_day": r.get("max_per_day"), "max_per_user_per_hour": r.get("max_per_user_per_hour"),
                "updated_at": r.get("updated_at"), "updated_by": r.get("updated_by")}
    if kind == "ask_turn":
        return {"id": r.get("id"), "thread_id": r.get("thread_id"), "seq": r.get("seq"), "prompt_id": r.get("prompt_id"),
                "question": r.get("question"), "answer": r.get("answer"),
                "cites": _j(r.get("cites_json"), []) or [], "grounded": bool(r.get("grounded")),
                "prompt_version": r.get("prompt_version"), "model": r.get("model"),
                "usage": _j(r.get("usage_json"), None), "warnings": _j(r.get("warnings_json"), []) or [],
                "created_at": r.get("created_at")}
    if kind == "kb_hit":
        return {"document_id": r.get("document_id"), "title": r.get("title"), "seq": r.get("seq"), "text": r.get("text"),
                "chars": len(r.get("text") or ""), "score": r.get("score"), "terms": r.get("terms") or []}
    if kind == "dispute":
        return {"id": r.get("id"), "scorecard_id": r.get("scorecard_id"), "criterion_id": r.get("criterion_id"),
                "criterion_name": r.get("criterion_name"), "criterion_key": r.get("criterion_key"),
                "agent_name": r.get("agent_name"), "reason": r.get("reason"), "status": r.get("status") or "open",
                "raised_by": r.get("raised_by"), "via": r.get("via") or "qa", "resolution_note": r.get("resolution_note"),
                "resolved_by": r.get("resolved_by"), "resolved_at": r.get("resolved_at"),
                "created_at": r.get("created_at"), "updated_at": r.get("updated_at"),
                "audit_name": extra.get("audit_name"), "recording_id": extra.get("recording_id"),
                "final_pct": extra.get("final_pct"), "review_status": extra.get("review_status")}
    if kind == "item":
        final = r.get("override_score") if r.get("override_score") is not None else r.get("score")
        return {"criterion_id": r.get("criterion_id"), "key": r.get("key"), "name": r.get("name"),
                "description": r.get("description"), "critical": bool(r.get("critical")),
                "seq": r.get("seq"), "rating": r.get("rating"), "score": r.get("score"),
                "weight": r.get("weight"), "final_score": final, "rationale": r.get("rationale"),
                "evidence": _j(r.get("evidence_json"), []),
                "override_score": r.get("override_score"), "override_note": r.get("override_note"),
                "override_by": r.get("override_by"), "override_at": r.get("override_at")}
    if kind == "review":
        return {"id": r.get("id"), "scorecard_id": r.get("scorecard_id"), "recording_id": r.get("recording_id"),
                "reviewer_email": r.get("reviewer_email"), "status": r.get("status") or "draft",
                "final_pct": r.get("final_pct") if (r.get("applicable_weight") or 0) > 0 else None,
                "applicable_weight": r.get("applicable_weight"),
                "auto_fail": bool(r.get("auto_fail")) if r.get("auto_fail") is not None else None,
                "coaching_notes": r.get("coaching_notes"), "resolution_notes": r.get("resolution_notes"),
                "action_plan": r.get("action_plan"), "follow_up_on": r.get("follow_up_on"),
                "coached_at": r.get("coached_at"), "created_at": r.get("created_at"),
                "updated_at": r.get("updated_at"), "submitted_at": r.get("submitted_at")}
    if kind == "dashboard":
        # dashboard.aggregate() output: aggregates only, but it still goes through the whitelist
        out = {k: extra.get(k) for k in ("period", "from", "to", "tz", "source", "scope", "totals", "buckets",
                                         "series", "by_agent", "by_qa", "activity", "sentiment", "reasons",
                                         "reason_labels", "pipeline",
                                         # 0.37.0 executive layer
                                         "kpis", "bands", "sentiment_shift", "by_criterion", "disputes", "coaching",
                                         "previous", "deltas", "leaders")}
        out["kind"] = extra.get("calls_kind")      # 'kind' is this function's own first argument
        return out
    if kind == "person":
        return {"id": r.get("id"), "kind": r.get("kind"), "name": r.get("name"), "email": r.get("email"),
                "active": bool(r.get("active")), "created_at": r.get("created_at"),
                "created_by": r.get("created_by"), "uses": extra.get("uses", 0)}
    if kind == "session":
        calls = extra.get("calls", [])
        return {"id": r.get("id"), "agent_name": r.get("agent_name"), "qa_name": r.get("qa_name"),
                "title": r.get("title"), "status": r.get("status") or "planned",
                "scheduled_at": r.get("scheduled_at"), "duration_min": r.get("duration_min") or coaching.DEFAULT_MINUTES,
                "location": r.get("location"), "agenda": r.get("agenda"), "agenda_edited": bool(r.get("agenda_edited")),
                "notes": r.get("notes"), "created_by": r.get("created_by"), "created_at": r.get("created_at"),
                "updated_at": r.get("updated_at"), "done_at": r.get("done_at"), "cancelled_at": r.get("cancelled_at"),
                "calls": calls, "calls_count": len(calls), "agent_email": extra.get("agent_email"),
                "conflicts": extra.get("conflicts"), "summary": extra.get("summary"),
                "invite_sent_at": r.get("invite_sent_at"), "invite_to": r.get("invite_to"),
                "qa_email": extra.get("qa_email"), "invites": extra.get("invites", [])}
    if kind == "invite":
        return {"id": r.get("id"), "session_id": r.get("session_id"), "to_email": r.get("to_email"), "from_email": r.get("from_email"),
                "status": r.get("status"), "channel": r.get("channel") or "smtp", "error": r.get("error"), "sent_by": r.get("sent_by"), "sent_at": r.get("sent_at")}
    if kind == "session_call":
        # one audited call inside a session (or offered for one): never the path or hash
        return {"scorecard_id": r.get("scorecard_id"), "recording_id": r.get("recording_id"), "job_id": r.get("job_id"),
                "seq": r.get("seq"), "audit_name": r.get("audit_name") or r.get("filename"), "filename": r.get("filename"),
                "call_ref": r.get("call_ref"), "uploaded_at": r.get("uploaded_at"), "agent_name": r.get("agent_name"),
                "overall_pct": r.get("overall_pct") if (r.get("applicable_weight") or 0) > 0 else None,
                "final_pct": r.get("final_pct") if r.get("rv_status") == "submitted" and (r.get("rv_basis") or 0) > 0 else None,
                "review_status": r.get("rv_status"), "coached_at": r.get("coached_at"), "auto_fail": bool(r.get("auto_fail")),
                "open_disputes": r.get("open_disputes") or 0, "misses": _j(r.get("misses_json"), []),
                "in_session": r.get("in_session"), "kind": r.get("kind") or "real",
                "items": r.get("items")}          # [{key,name,weight,final,rating}] when the caller attached them
    if kind == "coach_agent":
        return {"name": r.get("name"), "email": r.get("email"), "active": bool(r.get("active", 1)),
                "to_coach": r.get("to_coach") or 0, "open_disputes": r.get("open_disputes") or 0,
                "sessions_open": r.get("sessions_open") or 0, "next_session_at": r.get("next_session_at"),
                "next_session_id": r.get("next_session_id"), "last_coached_at": r.get("last_coached_at"),
                "audited": r.get("audited") or 0}
    if kind == "user":
        return {"id": r.get("id"), "email": r.get("email"), "name": r.get("name"),
                "role": r.get("role"), "active": bool(r.get("active")),
                "created_at": r.get("created_at"), "last_login": r.get("last_login"),
                "open_access": bool(extra.get("open_access"))}
    if kind == "match":
        # an earlier upload that looks like the same call: never the path or hash
        return {"recording_id": r.get("id"), "audit_name": r.get("audit_name") or r.get("filename"),
                "filename": r.get("filename"), "uploaded_at": r.get("uploaded_at"), "kind": r.get("kind") or "real",
                "call_ref": r.get("call_ref"), "job_id": extra.get("job_id"), "match": extra.get("match"),
                "audited": bool(extra.get("audited"))}
    if kind == "audit":
        return {"at": r.get("at"), "email": r.get("email"), "action": r.get("action"),
                "target": r.get("target"), "ip": r.get("ip"), "detail": r.get("detail")}
    if kind == "branding":
        # never the blobs: only the mode, the URLs the page should use and what was uploaded
        return {"logo_mode": r.get("logo_mode") or "auditly", "updated_at": r.get("updated_at"), "updated_by": r.get("updated_by"),
                "logos": extra.get("logos"), "icons": extra.get("icons"), "custom": extra.get("custom", {}),
                "show_name": (r.get("logo_mode") or "auditly") in ("coeo", "hidden"),
                "png_hint": extra.get("png_hint", [])}
    if kind == "agent_view":
        # the agent's own page (0.52.0): scores, reasons and quotes -- never the recording, the call text, the
        # customer, the QA's name or notes, the model, the knowledge excerpts. A new key here needs a reason.
        disputes = extra.get("disputes", [])
        return {"agent_name": extra.get("agent_name"), "call_at": extra.get("call_at"), "duration_s": extra.get("duration_s"),
                "rubric_name": extra.get("rubric_name"), "version_no": extra.get("version_no"),
                "overall_pct": r.get("overall_pct") if (r.get("applicable_weight") or 0) > 0 else None,
                "applicable_weight": r.get("applicable_weight"), "auto_fail": bool(r.get("auto_fail")),
                "final_pct": extra.get("final_pct"), "submitted_at": extra.get("submitted_at"),
                "summary": r.get("summary"), "strengths": _j(r.get("strengths_json"), []),
                "opportunities": _j(r.get("opportunities_json"), []), "misses": _j(r.get("misses_json"), []),
                "items": extra.get("items", []), "disputes": disputes,
                "open_disputes": sum(1 for d in disputes if d.get("status") == "open"),
                "expires_at": extra.get("expires_at"), "version": core.app_version()}
    if kind == "agent_item":
        final = r.get("override_score") if r.get("override_score") is not None else r.get("score")
        return {"criterion_id": r.get("criterion_id"), "key": r.get("key"), "name": r.get("name"), "description": r.get("description"),
                "critical": bool(r.get("critical")), "seq": r.get("seq"), "rating": r.get("rating"), "score": r.get("score"),
                "weight": r.get("weight"), "final_score": final, "rationale": r.get("rationale"),
                "evidence": [{"ts": e.get("ts"), "quote": e.get("quote")} for e in _j(r.get("evidence_json"), []) if isinstance(e, dict)],
                "overridden": r.get("override_score") is not None}
    if kind == "agent_dispute":
        return {"id": r.get("id"), "criterion_id": r.get("criterion_id"), "criterion_name": r.get("criterion_name"),
                "reason": r.get("reason"), "status": r.get("status") or "open", "resolution_note": r.get("resolution_note"),
                "created_at": r.get("created_at"), "resolved_at": r.get("resolved_at"), "via": r.get("via") or "qa"}
    if kind == "dispute_link":
        # the QA-side status of one link row; the token hash never leaves
        return {"sent_to": r.get("sent_to"), "channel": r.get("channel"), "error": r.get("error"), "created_by": r.get("created_by"),
                "created_at": r.get("created_at"), "expires_at": r.get("expires_at"), "revoked_at": r.get("revoked_at"),
                "live": r.get("revoked_at") is None and (r.get("expires_at") or "") > core.now()}
    if kind == "notification":
        # a shared event with this reader's read mark; the actor is a user email, shown as elsewhere
        return {"id": r.get("id"), "kind": r.get("kind"), "at": r.get("at"), "title": r.get("title"), "body": r.get("body"),
                "target_kind": r.get("target_kind"), "target_id": r.get("target_id"), "agent_name": r.get("agent_name"),
                "actor": r.get("actor_email"), "read": bool(r.get("read_at")), "recording_id": r.get("recording_id")}
    raise ValueError("to_public: unknown kind %r" % kind)


# ── request plumbing ──────────────────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "auditly"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        line = re.sub(r"/dispute/[A-Za-z0-9_-]{20,}", "/dispute/<token>", fmt % a)   # the agent's link token stays out of the log
        log("%s %s" % (self.address_string(), line))

    def client_ip(self):
        xff = self.headers.get("X-Forwarded-For", "")
        return (xff.split(",")[0].strip() if xff else self.client_address[0])

    def send(self, code, body=b"", ctype=None, extra=None, nostore=True):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        if ctype:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if nostore:
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def json(self, obj, code=200, extra=None):
        self.send(code, json.dumps(obj, default=str).encode("utf-8"),
                  "application/json; charset=utf-8", extra)

    def fail(self, code, msg):
        self.json({"error": msg}, code)

    def body_json(self, limit=1_000_000):
        self._body_done = True
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        if n > limit:
            raw = self.rfile.read(min(n, limit))     # drain what we can, then refuse
            return {"__too_large__": True}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8", "replace") or "{}")
        except ValueError:
            return {}

    # -- session ----------------------------------------------------------
    def cookies(self):
        out = {}
        for part in (self.headers.get("Cookie") or "").split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    def current_user(self, db):
        u = self.session_user(db)
        if u is None and open_access(core.cfg()):
            return open_access_user(db)       # AUDITLY_OPEN_ACCESS=1: every visitor is this user
        return u

    def session_user(self, db):
        raw = self.cookies().get(COOKIE)
        if not raw:
            return None
        r = db.execute("SELECT u.*, s.expires_at FROM session s JOIN user u ON u.id=s.user_id"
                       " WHERE s.token_hash=?", (core.token_hash(raw),)).fetchone()
        if not r:
            return None
        if r["expires_at"] <= core.now() or not r["active"]:
            db.execute("DELETE FROM session WHERE token_hash=?", (core.token_hash(raw),))
            return None
        return r

    def set_session_cookie(self, raw, c):
        secure = "" if c.get("AUDITLY_INSECURE_COOKIES") == "1" else " Secure;"
        hours = core.cfg_int(c, "AUDITLY_SESSION_HOURS", 12)
        return {"Set-Cookie": "%s=%s; Path=/; HttpOnly;%s SameSite=Lax; Max-Age=%d"
                              % (COOKIE, raw, secure, hours * 3600)}

    def clear_cookie(self):
        return {"Set-Cookie": "%s=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0" % COOKIE}

    def csrf_ok(self):
        return self.headers.get(CSRF_HEADER) == "1"

    # -- routing ----------------------------------------------------------
    def do_GET(self):
        path, _, query = self.path.partition("?")
        path = urllib.parse.unquote(path.split("#", 1)[0])
        q = urllib.parse.parse_qs(query, keep_blank_values=False)
        db = core.connect()
        try:
            self.route_get(db, path, q)
        except BrokenPipeError:
            pass
        except Exception as e:                    # noqa: BLE001
            log("500 on GET %s: %r" % (path, e))
            self.fail(500, "Internal error.")
        finally:
            db.close()

    do_HEAD = do_GET

    def do_POST(self):
        path, _, query = self.path.partition("?")
        path = urllib.parse.unquote(path)
        q = urllib.parse.parse_qs(query, keep_blank_values=False)
        db = core.connect()
        self._body_done = False
        try:
            if not self.csrf_ok():
                return self.fail(403, "Missing request header.")
            self.route_post(db, path, q)
        except BrokenPipeError:
            pass
        except Exception as e:                    # noqa: BLE001
            log("500 on POST %s: %r" % (path, e))
            self.fail(500, "Internal error.")
        finally:
            db.close()
            self.drain_body()

    def drain_body(self):
        """HTTP/1.1 keep-alive: a handler that never read its body (archive, remove-demo, ...) would
        leave the bytes on the socket to be parsed as the next request line -- a 400 for the browser's
        next call. Read a small leftover; close the connection on a big one (a refused upload)."""
        if getattr(self, "_body_done", True):
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return
        if n <= 1_000_000:
            try:
                self.rfile.read(n)
            except OSError:
                self.close_connection = True
        else:
            self.close_connection = True
        self._body_done = True

    def do_PUT(self):
        self.fail(405, "Method not allowed.")

    do_DELETE = do_PATCH = do_OPTIONS = do_PUT

    def one(self, q, name, default=None):
        v = q.get(name, [None])[0]
        return v.strip() if isinstance(v, str) and v.strip() else default

    # ── GET routes ────────────────────────────────────────────────────────
    def route_get(self, db, path, q):
        c = core.cfg()
        if path == "/health":
            return self.json({"ok": True, "service": "auditly", "version": core.app_version(),
                              "time": core.now(), "started_at": STARTED_AT, "demo": c.get("AUDITLY_DEMO") == "1"})
        if path in ("/l1qa", "/l1qa/"):                  # the pre-0.30.0 LAN link keeps working
            return self.send(301, b"", "text/plain; charset=utf-8", {"Location": "/auditly/"})
        if path in ("/", "/index.html", "/auditly", "/auditly/"):
            return self.serve_app()
        if path.startswith("/static/"):
            m = re.match(r"^/static/([A-Za-z0-9._-]+)$", path)
            if not m or m.group(1) not in self.STATIC:
                return self.fail(404, "Not found.")
            return self.serve_static(m.group(1))
        if path == "/favicon.ico":               # the brand icon (dark background reads on any tab strip)
            return self.brand_asset(db, "icon_dark", favicon=True)
        if path == "/api/branding":
            return self.json(self._branding_out(db))
        m = re.match(r"^/api/branding/asset/(logo_light|logo_dark|icon_light|icon_dark)$", path)
        if m:
            return self.brand_asset(db, m.group(1))
        if path == "/api/changelog":
            entries = core.changelog_entries()
            return self.json({"version": entries[0]["version"] if entries else "",
                              "date": entries[0]["date"] if entries else "", "entries": entries})
        if path == "/api/plan":
            return self.json({"markdown": core.read_plan()})
        if path == "/api/me":
            u = self.current_user(db)
            if not u:
                return self.fail(401, "Not signed in.")
            return self.json(to_public("user", u, open_access=open_access(c)))
        m = re.match(r"^/dispute/(%s)(/data)?$" % DISPUTE_TOKEN, path)
        if m:                                    # the agent's page (0.52.0): a token, never a session
            return self.dispute_data(db, m.group(1)) if m.group(2) else self.serve_dispute(db, m.group(1))
        if path.startswith("/dispute/"):         # malformed token: the same page, the same 404
            return self.serve_dispute(db, None)

        u = self.current_user(db)
        if not u:
            return self.fail(401, "Not signed in.")

        if path == "/api/health":
            st = llm.provider_status(c)
            st.update({"ok": True, "version": core.app_version(), "time": core.now(), "started_at": STARTED_AT,
                       "max_upload_mb": core.cfg_int(c, "AUDITLY_MAX_UPLOAD_MB", 200),
                       "retention_days": core.cfg_int(c, "AUDITLY_RETENTION_DAYS", 0),
                       "ffmpeg": bool(_which("ffmpeg")), "queue": worker.JOBQ.qsize(),
                       "open_access": open_access(c), "spend": self._spend(db),
                       "ask": self._ask_status(db, c), "tls_url": self._tls_url(c),
                       "legacy_keys": core.legacy_keys(),
                       "kb_docs": db.execute("SELECT COUNT(*) c FROM kb_document WHERE enabled=1 AND deleted_at IS NULL").fetchone()["c"],
                       "mail": mailer.status(c),
                       "db": os.path.basename(core.db_path(c))})
            return self.json(st)
        if path == "/api/rubrics":
            return self.api_rubrics(db, q)
        if path == "/api/people":
            return self.api_people(db, q)
        m = re.match(r"^/api/rubrics/(%s)$" % UUID, path)
        if m:
            return self.api_rubric(db, m.group(1), q)
        m = re.match(r"^/api/rubrics/(%s)/versions/(\d+)$" % UUID, path)
        if m:
            return self.api_rubric_version(db, m.group(1), int(m.group(2)))
        m = re.match(r"^/api/rubrics/(%s)/versions/(\d+)/sync$" % UUID, path)
        if m:
            return self.api_rubric_sync(db, m.group(1), int(m.group(2)))
        if path == "/api/jobs":
            return self.api_jobs(db, q)
        m = re.match(r"^/api/jobs/(%s)$" % UUID, path)
        if m:
            return self.api_job(db, m.group(1))
        if path == "/api/recordings/lookup":
            return self.api_lookup(db, q)
        m = re.match(r"^/api/recordings/(%s)/audio$" % UUID, path)
        if m:
            return self.media(db, u, m.group(1))
        m = re.match(r"^/api/recordings/(%s)/transcript$" % UUID, path)
        if m:
            return self.api_transcript(db, m.group(1))
        m = re.match(r"^/api/scorecards/(%s)$" % UUID, path)
        if m:
            return self.api_scorecard(db, u, m.group(1))
        m = re.match(r"^/api/scorecards/(%s)/export\.(pdf|csv|json)$" % UUID, path)
        if m:
            return self.export(db, u, m.group(1), m.group(2))
        m = re.match(r"^/api/scorecards/(%s)/review$" % UUID, path)
        if m:
            return self.review_get(db, u, m.group(1))
        m = re.match(r"^/api/scorecards/(%s)/disputes$" % UUID, path)
        if m:
            return self.disputes_get(db, m.group(1))
        m = re.match(r"^/api/scorecards/(%s)/dispute-link$" % UUID, path)
        if m:
            return self.dispute_link_get(db, u, m.group(1))
        if path == "/api/disputes":
            return self.api_disputes(db, q)
        if path == "/api/kb/documents":
            return self.api_kb_docs(db, q)
        if path == "/api/ask/thread":
            return self.api_ask_thread(db, u, q)
        if path == "/api/kb/search":
            return self.api_kb_search(db, q)
        m = re.match(r"^/api/kb/documents/(%s)$" % UUID, path)
        if m:
            return self.api_kb_doc(db, m.group(1))
        if path == "/api/coaching/sessions":
            return self.api_sessions(db, q)
        if path == "/api/coaching/candidates":
            return self.api_candidates(db, q)
        if path == "/api/coaching/agents":
            return self.api_coach_agents(db, q)
        if path == "/api/coaching/calendar":
            return self.api_calendar(db, q)
        m = re.match(r"^/api/coaching/sessions/(%s)$" % UUID, path)
        if m:
            return self.session_get(db, m.group(1))
        m = re.match(r"^/api/coaching/sessions/(%s)/export\.(pdf|ics)$" % UUID, path)
        if m:
            return self.session_export(db, u, m.group(1), m.group(2))
        m = re.match(r"^/api/coaching/sessions/(%s)/export\.eml$" % UUID, path)
        if m:
            return self.session_eml(db, u, m.group(1), q)
        if path == "/api/reviews":
            return self.api_reviews(db, q)
        if path == "/api/notifications":
            return self.api_notifications(db, u, q)
        if path == "/api/dashboard":
            return self.api_dashboard(db, q)
        if path == "/api/dashboard/export.pdf":
            return self.dashboard_pdf(db, u, q)
        if path.startswith("/api/admin/"):
            if u["role"] != "admin":
                return self.fail(404, "Not found.")
            return self.admin_get(db, u, path, q)
        return self.fail(404, "Not found.")

    # ── POST routes ───────────────────────────────────────────────────────
    def route_post(self, db, path, q):
        if path == "/auth/login":
            return self.login(db)
        if path == "/auth/logout":
            return self.logout(db)
        m = re.match(r"^/dispute/(%s)/raise$" % DISPUTE_TOKEN, path)
        if m:                                    # the agent raising a dispute from their link (0.52.0)
            return self.dispute_raise_public(db, m.group(1))
        u = self.current_user(db)
        if not u:
            return self.fail(401, "Not signed in.")
        if path == "/auth/password":
            return self.change_password(db, u)
        if path == "/api/recordings":
            return self.upload(db, u, q)
        m = re.match(r"^/api/recordings/(%s)/rescore$" % UUID, path)
        if m:
            return self.rescore(db, u, m.group(1))
        m = re.match(r"^/api/recordings/(%s)/retranscribe$" % UUID, path)
        if m:
            return self.retranscribe(db, u, m.group(1))
        m = re.match(r"^/api/jobs/(%s)/(retry|delete)$" % UUID, path)
        if m:
            return self.job_action(db, u, m.group(1), m.group(2))
        m = re.match(r"^/api/scorecards/(%s)/items/(%s)/override$" % (UUID, UUID), path)
        if m:
            return self.override(db, u, m.group(1), m.group(2))
        m = re.match(r"^/api/scorecards/(%s)/agent-speaker$" % UUID, path)
        if m:
            return self.agent_speaker(db, u, m.group(1))
        m = re.match(r"^/api/scorecards/(%s)/speakers$" % UUID, path)
        if m:
            return self.speakers(db, u, m.group(1))
        m = re.match(r"^/api/scorecards/(%s)/review(?:/(submit|reopen|coached|queue|unqueue))?$" % UUID, path)
        if m:
            return self.review_post(db, u, m.group(1), m.group(2))
        if path == "/api/reviews/queue":
            return self.reviews_queue(db, u)
        if path == "/api/notifications/read":
            return self.notifications_read(db, u)
        if path == "/api/notifications/prefs":
            return self.notifications_prefs(db, u)
        m = re.match(r"^/api/scorecards/(%s)/disputes$" % UUID, path)
        if m:
            return self.dispute_raise(db, u, m.group(1))
        m = re.match(r"^/api/scorecards/(%s)/dispute-link$" % UUID, path)
        if m:
            return self.dispute_link_post(db, u, m.group(1))
        m = re.match(r"^/api/disputes/(%s)/(resolve|withdraw)$" % UUID, path)
        if m:
            return self.dispute_action(db, u, m.group(1), m.group(2))
        if path == "/api/ask":
            return self.api_ask(db, u)
        if path == "/api/ask/voice":
            return self.api_ask_voice(db, u, q)
        if path == "/api/kb/documents":
            return self.kb_upload(db, u)
        m = re.match(r"^/api/kb/documents/(%s)/(enable|disable|rename|delete|scope)$" % UUID, path)
        if m:
            return self.kb_action(db, u, m.group(1), m.group(2))
        if path == "/api/coaching/sessions":
            return self.session_create(db, u)
        m = re.match(r"^/api/coaching/sessions/(%s)/invite$" % UUID, path)
        if m:
            return self.session_invite(db, u, m.group(1))
        m = re.match(r"^/api/coaching/sessions/(%s)/invite-sent$" % UUID, path)
        if m:
            return self.session_invite_sent(db, u, m.group(1))
        m = re.match(r"^/api/coaching/sessions/(%s)(?:/(calls|schedule|unschedule|done|reopen|cancel))?$" % UUID, path)
        if m:
            return self.session_post(db, u, m.group(1), m.group(2))
        m = re.match(r"^/api/recordings/(%s)/details$" % UUID, path)
        if m:
            return self.recording_details(db, u, m.group(1))
        if path == "/api/people":
            return self.people_create(db, u)
        if path == "/api/people/remove-demo":
            return self.people_remove_demo(db, u)
        m = re.match(r"^/api/people/(%s)/(rename|archive|delete|email)$" % UUID, path)
        if m:
            return self.people_action(db, u, m.group(1), m.group(2))
        if path == "/api/rubrics":
            return self.rubric_create(db, u)
        if path == "/api/rubrics/extract":
            return self.rubric_extract(db, u)
        m = re.match(r"^/api/rubrics/(%s)/versions$" % UUID, path)
        if m:
            return self.rubric_new_version(db, u, m.group(1))
        m = re.match(r"^/api/rubrics/(%s)/versions/(\d+)/sync$" % UUID, path)
        if m:
            return self.rubric_sync_start(db, u, m.group(1), int(m.group(2)))
        m = re.match(r"^/api/rubrics/(%s)/archive$" % UUID, path)
        if m:
            return self.rubric_archive(db, u, m.group(1))
        if path.startswith("/api/admin/"):
            if u["role"] != "admin":
                return self.fail(404, "Not found.")
            if path == "/api/admin/ask":
                return self.api_admin_ask_set(db, u)
            if path == "/api/admin/ask/prompt":
                return self.api_admin_ask_prompt(db, u)
            if path == "/api/admin/ask/prompt/reset":
                return self.api_admin_ask_reset(db, u)
            if path == "/api/admin/ask/try":
                return self.api_admin_ask_try(db, u)
            if path == "/api/admin/branding":
                return self.branding_mode(db, u)
            if path == "/api/admin/mail/test":
                return self.mail_test(db, u)
            m = re.match(r"^/api/admin/branding/asset/(logo_light|logo_dark|icon_light|icon_dark)(?:/(delete))?$", path)
            if m:
                return self.branding_asset_delete(db, u, m.group(1)) if m.group(2) else self.branding_asset_upload(db, u, m.group(1), q)
            return self.admin_post(db, u, path)
        return self.fail(404, "Not found.")

    # ── auth ──────────────────────────────────────────────────────────────
    def rate_limited(self, db, email, ip):
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=LOGIN_WINDOW_MIN)) \
            .replace(microsecond=0).isoformat()
        db.execute("DELETE FROM login_attempt WHERE at < ?", (cutoff,))
        n = db.execute("SELECT COUNT(*) c FROM login_attempt WHERE at >= ? AND (email = ? OR ip = ?)",
                       (cutoff, (email or "").lower(), ip)).fetchone()["c"]
        return n >= LOGIN_MAX_FAILS

    def login(self, db):
        d = self.body_json()
        email = _s(d.get("email")).lower()
        pw = d.get("password") if isinstance(d.get("password"), str) else ""
        ip = self.client_ip()
        c = core.cfg()
        if self.rate_limited(db, email, ip):
            core.audit(db, "login.ratelimited", target=email, ip=ip)
            return self.fail(429, "Too many attempts. Try again in 15 minutes.")
        r = db.execute("SELECT * FROM user WHERE email=? COLLATE NOCASE", (email,)).fetchone()
        ok = bool(r) and r["active"] and core.verify_password(pw, r["pw_hash"], r["pw_salt"])
        if not ok:
            db.execute("INSERT INTO login_attempt (at,email,ip) VALUES (?,?,?)", (core.now(), email, ip))
            core.audit(db, "login.failed", target=email, ip=ip)
            return self.fail(401, "Email or password is incorrect.")
        raw, th = core.new_session_token()
        hours = core.cfg_int(c, "AUDITLY_SESSION_HOURS", 12)
        db.execute("INSERT INTO session (token_hash,user_id,created_at,expires_at,ip) VALUES (?,?,?,?,?)",
                   (th, r["id"], core.now(), core.session_expiry(hours), ip))
        db.execute("UPDATE user SET last_login=? WHERE id=?", (core.now(), r["id"]))
        db.execute("DELETE FROM login_attempt WHERE email=?", (email,))
        core.audit(db, "login.ok", user=r, ip=ip)
        return self.json(to_public("user", r), extra=self.set_session_cookie(raw, c))

    def logout(self, db):
        raw = self.cookies().get(COOKIE)
        if raw:
            u = self.current_user(db)
            db.execute("DELETE FROM session WHERE token_hash=?", (core.token_hash(raw),))
            core.audit(db, "logout", user=u, ip=self.client_ip())
        return self.json({"ok": True}, extra=self.clear_cookie())

    def change_password(self, db, u):
        d = self.body_json()
        if not core.verify_password(d.get("current") or "", u["pw_hash"], u["pw_salt"]):
            return self.fail(403, "Current password is incorrect.")
        new = d.get("new") or ""
        problem = core.password_problem(new)
        if problem:
            return self.fail(400, problem)
        h, s = core.hash_password(new)
        db.execute("UPDATE user SET pw_hash=?, pw_salt=? WHERE id=?", (h, s, u["id"]))
        db.execute("DELETE FROM session WHERE user_id=?", (u["id"],))
        core.audit(db, "password.changed", user=u, ip=self.client_ip())
        return self.json({"ok": True}, extra=self.clear_cookie())

    # ── rubrics ───────────────────────────────────────────────────────────
    def _rubric_summary(self, db, r):
        v = db.execute("SELECT * FROM rubric_version WHERE rubric_id=? ORDER BY version_no DESC LIMIT 1",
                       (r["id"],)).fetchone()
        n = 0
        if v:
            n = db.execute("SELECT COUNT(*) c FROM criterion WHERE rubric_version_id=?",
                           (v["id"],)).fetchone()["c"]
        return to_public("rubric", r, current_version=v["version_no"] if v else None,
                         current_version_id=v["id"] if v else None, criteria_count=n,
                         updated_at=v["created_at"] if v else r["created_at"],
                         updated_by=(v["created_by"] if v else None) or r["created_by"],
                         sync_status=(v["sync_status"] or "none") if v else None)

    def api_rubrics(self, db, q):
        rows = db.execute("SELECT * FROM rubric ORDER BY archived, name").fetchall()
        return self.json({"rubrics": [self._rubric_summary(db, r) for r in rows]})

    def api_rubric(self, db, rid, q):
        r = db.execute("SELECT * FROM rubric WHERE id=?", (rid,)).fetchone()
        if not r:
            return self.fail(404, "Not found.")
        vers = db.execute("SELECT * FROM rubric_version WHERE rubric_id=? ORDER BY version_no DESC",
                          (rid,)).fetchall()
        out = self._rubric_summary(db, r)
        out["versions"] = []
        for v in vers:
            crit = [to_public("criterion", x) for x in rubric_mod.criteria_for(db, v["id"])]
            used = db.execute("SELECT COUNT(*) c FROM scorecard WHERE rubric_version_id=?",
                              (v["id"],)).fetchone()["c"]
            pv = to_public("version", v, criteria=crit, with_source=True,
                           optimised_scorecards=rubric_sync.optimised_scorecards(db, v["id"]))
            pv["scorecards"] = used
            out["versions"].append(pv)
        return self.json(out)

    def api_rubric_version(self, db, rid, vno):
        v = db.execute("SELECT * FROM rubric_version WHERE rubric_id=? AND version_no=?",
                       (rid, vno)).fetchone()
        if not v:
            return self.fail(404, "Not found.")
        crit = [to_public("criterion", x) for x in rubric_mod.criteria_for(db, v["id"])]
        return self.json(to_public("version", v, criteria=crit, with_source=True,
                                   optimised_scorecards=rubric_sync.optimised_scorecards(db, v["id"])))

    # ── "Optimise for the scorer" ─────────────────────────────────────────
    def _version_by_no(self, db, rid, vno):
        return db.execute("SELECT v.*, r.name rubric_name FROM rubric_version v JOIN rubric r ON r.id=v.rubric_id"
                          " WHERE v.rubric_id=? AND v.version_no=?", (rid, vno)).fetchone()

    def _sync_public(self, db, c, v):
        cp = rubric_sync.caps(db, c, v)
        est = rubric_sync.estimate(db, c, v)
        used = rubric_sync.optimised_scorecards(db, v["id"])
        ready = rubric_sync.readiness(c)
        reason = cp["reason"] or ready
        if not reason and used:
            reason = ("%d call%s already scored with this version's optimised rules; save a new version to optimise again."
                      % (used, " was" if used == 1 else "s were"))
        if not reason and (v["sync_status"] or "none") in rubric_sync.LIVE:
            reason = "This version is already being optimised."
        return to_public("sync", v, optimised_scorecards=used, can_run=reason is None, reason=reason,
                         runs_used=cp["runs_used"], runs_max=cp["runs_max"], today_used=cp["today_used"],
                         today_max=cp["today_max"], est_usd=est["est_usd"], model=est["model"], provider=est["provider"],
                         criteria_count=est["criteria"])

    def api_rubric_sync(self, db, rid, vno):
        v = self._version_by_no(db, rid, vno)
        if not v:
            return self.fail(404, "Not found.")
        return self.json(self._sync_public(db, core.cfg(), v))

    def rubric_sync_start(self, db, u, rid, vno):
        """Manual, capped, paid: refuse with the reason BEFORE anything is queued."""
        if not can_edit_rubrics(u):
            return self.fail(403, "Only an admin or a QA reviewer can change QA types.")
        v = self._version_by_no(db, rid, vno)
        if not v:
            return self.fail(404, "Not found.")
        c = core.cfg()
        if (v["sync_status"] or "none") in rubric_sync.LIVE:
            return self.fail(409, "This version is already being optimised.")
        used = rubric_sync.optimised_scorecards(db, v["id"])
        if used:
            return self.fail(409, "%d call%s already scored with this version's optimised rules; save a new version to optimise again."
                             % (used, " was" if used == 1 else "s were"))
        ready = rubric_sync.readiness(c)
        if ready:
            return self.fail(400, ready)
        cp = rubric_sync.caps(db, c, v)
        if not cp["allowed"]:
            return self.fail(429, cp["reason"])
        core.audit(db, "rubric.sync.requested", user=u, target=v["id"], ip=self.client_ip(), detail="v%d" % vno)
        rubric_sync.reset_and_enqueue(db, v["id"])
        v = self._version_by_no(db, rid, vno)
        return self.json(self._sync_public(db, c, v), 202)

    def rubric_create(self, db, u):
        if not can_edit_rubrics(u):
            return self.fail(403, "Only an admin or a QA reviewer can change QA types.")
        d = self.body_json(4_000_000)
        if d.get("__too_large__"):
            return self.fail(413, "Request too large.")
        name = _s(d.get("name"))
        if not name:
            return self.fail(400, "A QA type name is required.")
        if db.execute("SELECT 1 FROM rubric WHERE name=?", (name,)).fetchone():
            return self.fail(409, "A QA type with that name already exists.")
        items = rubric_mod.normalise(d.get("criteria"))
        errs = rubric_mod.validate(items)
        if errs:
            return self.json({"error": errs[0], "errors": errs}, 400)
        rid = core.new_id()
        db.execute("INSERT INTO rubric (id,name,created_at,created_by) VALUES (?,?,?,?)",
                   (rid, name, core.now(), u["email"]))
        vid, vno = rubric_mod.create_version(db, rid, items, d.get("source_text"),
                                             d.get("source_filename"), d.get("notes"), u)
        core.audit(db, "rubric.create", user=u, target=rid, ip=self.client_ip(), detail=name)
        return self.json({"id": rid, "version_id": vid, "version_no": vno, "sync_status": "none"}, 201)

    def rubric_new_version(self, db, u, rid):
        if not can_edit_rubrics(u):
            return self.fail(403, "Only an admin or a QA reviewer can change QA types.")
        r = db.execute("SELECT * FROM rubric WHERE id=?", (rid,)).fetchone()
        if not r:
            return self.fail(404, "Not found.")
        d = self.body_json(4_000_000)
        if d.get("__too_large__"):
            return self.fail(413, "Request too large.")
        items = rubric_mod.normalise(d.get("criteria"))
        errs = rubric_mod.validate(items)
        if errs:
            return self.json({"error": errs[0], "errors": errs}, 400)
        vid, vno = rubric_mod.create_version(db, rid, items, d.get("source_text"),
                                             d.get("source_filename"), d.get("notes"), u)
        core.audit(db, "rubric.version", user=u, target=rid, ip=self.client_ip(), detail="v%d" % vno)
        return self.json({"id": rid, "version_id": vid, "version_no": vno, "sync_status": "none"}, 201)

    def rubric_archive(self, db, u, rid):
        if not can_edit_rubrics(u):
            return self.fail(403, "Only an admin or a QA reviewer can change QA types.")
        r = db.execute("SELECT * FROM rubric WHERE id=?", (rid,)).fetchone()
        if not r:
            return self.fail(404, "Not found.")
        new = 0 if r["archived"] else 1
        db.execute("UPDATE rubric SET archived=? WHERE id=?", (new, rid))
        core.audit(db, "rubric.archive" if new else "rubric.unarchive", user=u, target=rid,
                   ip=self.client_ip())
        return self.json({"ok": True, "archived": bool(new)})

    def rubric_extract(self, db, u):
        """{text} or {filename, content_b64} -> proposed criteria (not saved)."""
        if not can_edit_rubrics(u):
            return self.fail(403, "Only an admin or a QA reviewer can change QA types.")
        d = self.body_json(12_000_000)
        if d.get("__too_large__"):
            return self.fail(413, "Guideline file is too large (12 MB max).")
        text = d.get("text") or ""
        fname = d.get("filename") or ""
        if d.get("content_b64"):
            import base64
            try:
                data = base64.b64decode(d["content_b64"])
                text = rubric_mod.text_from_upload(fname, data)
            except ValueError as e:
                return self.fail(400, str(e))
            except Exception:                     # noqa: BLE001
                return self.fail(400, "Could not decode that file.")
        c = core.cfg()
        try:
            items, adjusted = rubric_mod.extract_criteria(text, llm.scorer_for(c))
        except ValueError as e:
            return self.fail(400, str(e))
        except llm.ProviderError as e:
            return self.fail(502, "The scoring model could not be reached: %s" % e.body[:200])
        core.audit(db, "rubric.extract", user=u, ip=self.client_ip(), detail=fname or "pasted")
        for it in items:
            it["critical"] = bool(it["critical"])
        return self.json({"criteria": items, "adjusted": adjusted, "source_text": text,
                          "source_filename": fname or None})

    # ── people: agents and QA reviewers ───────────────────────────────────
    def _person_uses(self, db, p):
        col = "agent_name" if p["kind"] == "agent" else "qa_name"
        return db.execute("SELECT COUNT(*) c FROM recording WHERE %s=? COLLATE NOCASE" % col,
                          (p["name"],)).fetchone()["c"]

    def api_people(self, db, q):
        kind = self.one(q, "kind")
        where, params = ["1=1"], []
        if kind in PERSON_KINDS:
            where.append("kind=?")
            params.append(kind)
        if self.one(q, "all") != "1":
            where.append("active=1")
        rows = db.execute("SELECT * FROM person WHERE %s ORDER BY kind, name COLLATE NOCASE"
                          % " AND ".join(where), params).fetchall()
        return self.json({"people": [to_public("person", p, uses=self._person_uses(db, p)) for p in rows]})

    def people_create(self, db, u):
        """{kind, names} -- names is one per line (a single {name} works too)."""
        if u["role"] != "admin":
            return self.fail(403, "Only an admin can manage names.")
        d = self.body_json(200_000)
        kind = _s(d.get("kind")).lower()
        if kind not in PERSON_KINDS:
            return self.fail(400, "kind must be 'agent' or 'qa'.")
        raw = d.get("names") if d.get("names") is not None else d.get("name") or ""
        if isinstance(raw, list):
            raw = "\n".join(str(x) for x in raw)
        names, seen = [], set()
        for line in str(raw).splitlines():
            n = " ".join(line.split())[:80]
            if n and n.lower() not in seen:
                seen.add(n.lower())
                names.append(n)
        if not names:
            return self.fail(400, "Give at least one name.")
        if len(names) > 200:
            return self.fail(400, "At most 200 names at a time.")
        created, skipped = [], []
        for n in names:
            if db.execute("SELECT 1 FROM person WHERE kind=? AND name=? COLLATE NOCASE", (kind, n)).fetchone():
                skipped.append(n)
                continue
            pid = core.new_id()
            db.execute("INSERT INTO person (id,kind,name,active,created_at,created_by) VALUES (?,?,?,1,?,?)",
                       (pid, kind, n, core.now(), u["email"]))
            created.append(to_public("person", db.execute("SELECT * FROM person WHERE id=?", (pid,)).fetchone()))
        if created:
            core.audit(db, "person.create", user=u, target=kind, ip=self.client_ip(),
                       detail=", ".join(p["name"] for p in created)[:500])
        return self.json({"created": created, "skipped": skipped}, 201)

    def people_remove_demo(self, db, u):
        """Drop the seeded names (Jordan, Priya, Marcus, Demo QA): delete when unused, archive when a call uses them."""
        if u["role"] != "admin":
            return self.fail(403, "Only an admin can manage names.")
        import demo_data
        deleted, archived = [], []
        for kind, names in (("agent", demo_data.AGENTS), ("qa", demo_data.QA_NAMES + demo_data.HISTORY_QA)):
            for n in names:
                p = db.execute("SELECT * FROM person WHERE kind=? AND name=? COLLATE NOCASE", (kind, n)).fetchone()
                if not p:
                    continue
                if self._person_uses(db, p):
                    db.execute("UPDATE person SET active=0 WHERE id=?", (p["id"],))
                    archived.append(p["name"])
                else:
                    db.execute("DELETE FROM person WHERE id=?", (p["id"],))
                    deleted.append(p["name"])
        core.audit(db, "person.remove_demo", user=u, ip=self.client_ip(),
                   detail="deleted %s; archived %s" % (", ".join(deleted) or "-", ", ".join(archived) or "-"))
        return self.json({"deleted": deleted, "archived": archived})

    def people_action(self, db, u, pid, act):
        if u["role"] != "admin":
            return self.fail(403, "Only an admin can manage names.")
        p = db.execute("SELECT * FROM person WHERE id=?", (pid,)).fetchone()
        if not p:
            return self.fail(404, "Not found.")
        col = "agent_name" if p["kind"] == "agent" else "qa_name"
        if act == "rename":
            name = " ".join((self.body_json().get("name") or "").split())[:80]
            if not name:
                return self.fail(400, "Name is required.")
            dup = db.execute("SELECT id FROM person WHERE kind=? AND name=? COLLATE NOCASE AND id<>?",
                             (p["kind"], name, pid)).fetchone()
            if dup:
                return self.fail(409, "'%s' already exists." % name)
            db.execute("UPDATE person SET name=? WHERE id=?", (name, pid))
            n = db.execute("UPDATE recording SET %s=? WHERE %s=? COLLATE NOCASE" % (col, col),
                           (name, p["name"])).rowcount
            core.audit(db, "person.rename", user=u, target=pid, ip=self.client_ip(),
                       detail="%s -> %s (%d recordings)" % (p["name"], name, n))
            return self.json({"ok": True, "name": name, "recordings_renamed": n})
        if act == "email":
            email = _s(self.body_json().get("email"))[:160]
            if email and not EMAIL_RE.match(email):
                return self.fail(400, "That does not look like an email address.")
            db.execute("UPDATE person SET email=? WHERE id=?", (email or None, pid))
            core.audit(db, "person.email", user=u, target=pid, ip=self.client_ip(), detail="%s: %s" % (p["name"], email or "(cleared)"))
            return self.json({"ok": True, "email": email or None})
        if act == "archive":
            new = 0 if p["active"] else 1
            db.execute("UPDATE person SET active=? WHERE id=?", (new, pid))
            core.audit(db, "person.unarchive" if new else "person.archive", user=u, target=pid,
                       ip=self.client_ip(), detail=p["name"])
            return self.json({"ok": True, "active": bool(new)})
        uses = self._person_uses(db, p)
        if uses:
            return self.fail(409, "In use by %d recording%s — archive it instead." % (uses, "" if uses == 1 else "s"))
        db.execute("DELETE FROM person WHERE id=?", (pid,))
        core.audit(db, "person.delete", user=u, target=pid, ip=self.client_ip(), detail=p["name"])
        return self.json({"ok": True})

    # ── upload ────────────────────────────────────────────────────────────
    def upload(self, db, u, q):
        c = core.cfg()
        filename = self.one(q, "filename", "recording")
        filename = os.path.basename(filename)[:200]
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        provider = (self.one(q, "provider") or c.get("TRANSCRIBE_PROVIDER") or "deepgram").lower()
        rvid = self.one(q, "rubric_version_id")
        run = (self.one(q, "run") or "").lower()
        kind = run if run in ("demo", "test") else "real"     # demo = fake providers; test = real providers, tagged
        if kind == "demo":
            provider = "demo"
        stt_model = self.one(q, "stt_model")
        if provider in ("deepgram", "openai"):
            if stt_model is None:
                stt_model = llm.default_stt(c)[1] if llm.default_stt(c)[0] == provider else \
                    ("nova-3" if provider == "deepgram" else "whisper-1")
        sp, sm = ("demo", "demo") if kind == "demo" else llm.default_scorer(c)
        scorer_id = self.one(q, "scorer")
        agent = self.one(q, "agent")
        qa = self.one(q, "qa")
        company = (self.one(q, "caller_company") or "")[:120] or None
        caller = (self.one(q, "caller_name") or "")[:120] or None
        email = (self.one(q, "caller_email") or "")[:160] or None
        audit = (self.one(q, "audit_name") or "")[:160] or None
        call_ref = (self.one(q, "call_ref") or "")[:80] or None
        if not call_ref:
            # the PBX names recordings after the call: 1700000000.12345.mp3 -> call ID 1700000000.12345
            stem = filename.rsplit(".", 1)[0] if "." in filename else filename
            call_ref = stem.strip()[:80] or None
        try:
            size = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            size = 0
        cap = core.cfg_int(c, "AUDITLY_MAX_UPLOAD_MB", 200) * 1024 * 1024

        # Validate everything BEFORE reading the body, so a bad request costs nothing.
        problem = None
        if ext not in core.AUDIO_EXT:
            problem = (400, "Unsupported file type '.%s'. Use %s." % (ext, ", ".join(sorted(core.AUDIO_EXT))))
        elif size <= 0:
            problem = (400, "Empty upload.")
        elif size > cap:
            problem = (413, "File is %.1f MB; the limit is %d MB." % (size / 1048576.0, cap // 1048576))
        elif provider not in ("deepgram", "openai", "demo"):
            problem = (400, "Unknown transcription provider.")
        elif provider != "demo" and llm.parse_choice(c, "stt", llm.choice_id(provider, stt_model)) is None:
            problem = (400, "Unknown transcription model '%s' for %s." % (stt_model, provider))
        elif scorer_id and kind != "demo" and llm.parse_choice(c, "scoring", scorer_id) is None:
            problem = (400, "Unknown audit model '%s'." % scorer_id)
        elif provider == "openai" and size > core.OPENAI_STT_CAP:
            problem = (400, "OpenAI transcription accepts files up to 25 MB; this one is %.1f MB. "
                            "Choose Deepgram for this recording." % (size / 1048576.0))
        elif c.get("AUDITLY_DEMO") != "1" and provider != "demo":
            st = llm.provider_status(c)
            if scorer_id:
                sp, sm = llm.parse_choice(c, "scoring", scorer_id)
            kp = llm.key_problem(c, provider) or llm.key_problem(c, sp)
            if kp:
                problem = (400, kp)                    # both keys checked here, before any spend
            elif not st["allow_spend"]:
                problem = (400, "AUDITLY_ALLOW_SPEND is not 1 on the server; uploads are paused.")
        if not problem:
            if not rvid:
                problem = (400, "Choose a QA type.")
            elif not db.execute("SELECT 1 FROM rubric_version WHERE id=?", (rvid,)).fetchone():
                problem = (400, "That QA type version does not exist.")
        if not problem and email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            problem = (400, "Customer email does not look valid.")
        if not problem and agent:
            row = person_lookup(db, "agent", agent)
            if not row:
                problem = (400, person_problem(db, "agent", agent))
            else:
                agent = row["name"]                  # canonical spelling
        if not problem and qa:
            row = person_lookup(db, "qa", qa)
            if not row:
                problem = (400, person_problem(db, "qa", qa))
            else:
                qa = row["name"]
        if problem:
            # Do not read a body we are rejecting; nginx/clients handle the reset.
            self.close_connection = True
            return self.fail(*problem)

        rec_id = core.new_id()
        dst = os.path.join(core.upload_dir(c), "%s.%s" % (rec_id, ext))
        h = hashlib.sha256()
        got = 0
        try:
            with open(dst, "wb") as f:
                os.chmod(dst, 0o600)
                while got < size:
                    chunk = self.rfile.read(min(1024 * 1024, size - got))
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
            self._body_done = got == size
        except (OSError, ConnectionResetError) as e:
            log("upload aborted: %s" % e)
            got = -1
        if got != size:
            try:
                os.unlink(dst)
            except OSError:
                pass
            self.close_connection = True
            return self.fail(400, "Upload was cut short; please try again.")

        now = core.now()
        if not audit:
            audit = audit_name_for(agent, company, caller, now[:10])
        db.execute("INSERT INTO recording (id,filename,ext,mime,bytes,sha256,path,agent_name,qa_name,"
                   "caller_company,caller_name,caller_email,audit_name,kind,call_ref,uploaded_at,uploaded_by)"
                   " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (rec_id, filename, ext, core.AUDIO_EXT[ext], size, h.hexdigest(), dst,
                    agent, qa, company, caller, email, audit, kind, call_ref, now, u["email"]))
        job_id = core.new_id()
        if scorer_id and kind != "demo":
            sp, sm = llm.parse_choice(c, "scoring", scorer_id)
        db.execute("INSERT INTO job (id,recording_id,rubric_version_id,stt_provider,stt_model,scoring_provider,"
                   "scoring_model,status,created_at,created_by) VALUES (?,?,?,?,?,?,?,'queued',?,?)",
                   (job_id, rec_id, rvid, provider, None if provider == "demo" else stt_model, sp, sm,
                    core.now(), u["email"]))
        core.audit(db, "recording.upload", user=u, target=rec_id, ip=self.client_ip(),
                   detail="%s %d bytes via %s" % (filename, size, provider))
        worker.enqueue(job_id)
        matches = self._matches(db, sha256=h.hexdigest(), call_ref=call_ref, filename=filename, nbytes=size, exclude=rec_id)
        out = {"recording_id": rec_id, "job_id": job_id, "duplicate_of": None, "duplicates": matches}
        audio = [x for x in matches if x["match"] == "audio"]
        if audio:
            out["duplicate_of"] = {"recording_id": audio[0]["recording_id"], "audit_name": audio[0]["audit_name"],
                                   "job_id": audio[0]["job_id"]}
        return self.json(out, 201)

    def _matches(self, db, sha256=None, call_ref=None, filename=None, nbytes=None, exclude=None):
        """Earlier recordings that look like the same call, newest first: identical bytes ("audio"),
        the same call ID ("call_id"), or the same file name and size ("file"). Each carries whether
        that call already has a submitted audit, so the Upload page can warn before spending."""
        seen, out = set(), []
        probes = []
        if sha256:
            probes.append(("audio", "sha256=?", (sha256,)))
        if call_ref:
            probes.append(("call_id", "call_ref=? COLLATE NOCASE", (call_ref,)))
        if filename and nbytes:
            probes.append(("file", "filename=? AND bytes=?", (filename, nbytes)))
        for label, where, params in probes:
            rows = db.execute("SELECT * FROM recording WHERE %s AND deleted_at IS NULL ORDER BY uploaded_at DESC LIMIT 5"
                              % where, params).fetchall()
            for r in rows:
                if r["id"] == exclude or r["id"] in seen:
                    continue
                seen.add(r["id"])
                dj = db.execute("SELECT id FROM job WHERE recording_id=? AND status='done' ORDER BY created_at DESC LIMIT 1",
                                (r["id"],)).fetchone()
                audited = db.execute("SELECT 1 FROM review v JOIN scorecard s ON s.id=v.scorecard_id"
                                     " WHERE s.recording_id=? AND v.status='submitted'", (r["id"],)).fetchone()
                out.append(to_public("match", r, job_id=dj["id"] if dj else None, match=label, audited=bool(audited)))
        return out

    def api_lookup(self, db, q):
        """GET /api/recordings/lookup?call_ref=&filename=&bytes= -- before uploading a file, has this
        call been uploaded (or audited) already? Bytes cannot be hashed in the browser on the plain-HTTP
        LAN link, so this matches on the call ID and on file name + size; identical audio is still
        caught server-side once the file arrives."""
        call_ref = (self.one(q, "call_ref") or "")[:80] or None
        filename = os.path.basename(self.one(q, "filename") or "")[:200] or None
        try:
            nbytes = int(self.one(q, "bytes") or 0)
        except ValueError:
            nbytes = 0
        if not call_ref and not (filename and nbytes):
            return self.fail(400, "Give call_ref, or filename and bytes.")
        return self.json({"matches": self._matches(db, call_ref=call_ref, filename=filename, nbytes=nbytes or None)})

    def _carry_source(self, db, rec_id):
        """The run whose human work a rescore should inherit: the submitted audit if any, else the
        run with a review, else the newest scored run."""
        r = db.execute("SELECT s.id FROM scorecard s JOIN job j ON j.id=s.job_id AND j.status='done'"
                       " LEFT JOIN review v ON v.scorecard_id=s.id WHERE s.recording_id=?"
                       " ORDER BY (v.status='submitted') DESC, (v.id IS NOT NULL) DESC, s.created_at DESC LIMIT 1",
                       (rec_id,)).fetchone()
        return r["id"] if r else None

    def rescore(self, db, u, rec_id):
        rec = db.execute("SELECT * FROM recording WHERE id=? AND deleted_at IS NULL", (rec_id,)).fetchone()
        if not rec:
            return self.fail(404, "Not found.")
        d = self.body_json()
        rvid = d.get("rubric_version_id")
        if not rvid or not db.execute("SELECT 1 FROM rubric_version WHERE id=?", (rvid,)).fetchone():
            return self.fail(400, "Choose a QA type version.")
        if not db.execute("SELECT 1 FROM transcript WHERE recording_id=?", (rec_id,)).fetchone():
            return self.fail(400, "This recording has no transcript yet; retry its original job instead.")
        c = core.cfg()
        sp, sm = self._scorer_choice(c, rec, d.get("scorer"))
        if sp is None:
            return self.fail(400, "Unknown audit model '%s'." % d.get("scorer"))
        if rec["kind"] != "demo" and c.get("AUDITLY_DEMO") != "1":
            kp = llm.key_problem(c, sp)
            if kp:
                return self.fail(400, kp)
        if not d.get("force"):
            tr = self._latest_transcript(db, rec_id)
            same = self._identical_audit(db, tr["id"] if tr else None, rvid, sp, sm)
            if same:
                return self.json(dict(same, cached=True))       # nothing queued, nothing billed
        job_id = core.new_id()
        carry = self._carry_source(db, rec_id) if d.get("carry_over") else None
        db.execute("INSERT INTO job (id,recording_id,rubric_version_id,stt_provider,scoring_provider,scoring_model,"
                   "transcript_mode,status,created_at,created_by,carry_from_scorecard_id)"
                   " VALUES (?,?,?,?,?,?,'auto','queued',?,?,?)",
                   (job_id, rec_id, rvid, "reuse", sp, sm, core.now(), u["email"], carry))
        core.audit(db, "job.rescore", user=u, target=rec_id, ip=self.client_ip(),
                   detail="%s · %s:%s%s" % (rvid, sp, sm, " · carry over" if carry else ""))
        worker.enqueue(job_id)
        return self.json({"job_id": job_id}, 201)

    def _spend(self, db):
        """Totals from job.usage_json plus a list-price ESTIMATE; never a bill."""
        tot = {"jobs": 0, "cached_transcripts": 0, "audio_min": 0.0, "llm_calls": 0, "prompt_tokens": 0,
               "completion_tokens": 0, "cached_tokens": 0, "est_usd": 0.0, "syncs": 0}
        rows = db.execute("SELECT stt_provider, stt_model, scoring_model, usage_json FROM job WHERE usage_json IS NOT NULL").fetchall()
        for r in rows:
            u = _j(r["usage_json"], {}) or {}
            tot["jobs"] += 1
            tot["cached_transcripts"] += 1 if u.get("cached") else 0
            audio_min = float(u.get("audio_s") or 0) / 60.0
            tot["audio_min"] += audio_min
            tot["llm_calls"] += int(u.get("calls") or 0)
            for k in ("prompt_tokens", "completion_tokens", "cached_tokens"):
                tot[k] += int(u.get(k) or 0)
            stt_rate = llm.PRICES["stt"].get("%s:%s" % (r["stt_provider"], r["stt_model"]), 0.0)
            pin, pout = llm.PRICES["llm"].get(r["scoring_model"] or "", (0.0, 0.0))
            uncached = max(0, int(u.get("prompt_tokens") or 0) - int(u.get("cached_tokens") or 0))
            tot["est_usd"] += audio_min * stt_rate + (uncached * pin + int(u.get("cached_tokens") or 0) * pin / 2
                                                      + int(u.get("completion_tokens") or 0) * pout) / 1e6
        # spoken questions to Ask Auditly are paid STT calls too (0.48.0)
        tot["voice_clips"], tot["voice_s"] = 0, 0.0
        for r in db.execute("SELECT provider, model, audio_s FROM ask_voice").fetchall():
            tot["voice_clips"] += 1
            secs = float(r["audio_s"] or 0)
            tot["voice_s"] += secs
            tot["audio_min"] += secs / 60.0
            tot["est_usd"] += (secs / 60.0) * llm.PRICES["stt"].get("%s:%s" % (r["provider"], r["model"]), 0.0)
        tot["voice_s"] = round(tot["voice_s"], 1)
        # Ask Auditly turns are paid LLM calls too (0.46.0)
        tot["asks"] = 0
        for r in db.execute("SELECT model, usage_json FROM ask_turn WHERE usage_json IS NOT NULL").fetchall():
            u = _j(r["usage_json"], {}) or {}
            tot["asks"] += 1
            tot["llm_calls"] += 1
            for k in ("prompt_tokens", "completion_tokens", "cached_tokens"):
                tot[k] += int(u.get(k) or 0)
            pin, pout = llm.PRICES["llm"].get(r["model"] or "", (0.0, 0.0))
            tot["est_usd"] += (int(u.get("prompt_tokens") or 0) * pin + int(u.get("completion_tokens") or 0) * pout) / 1e6
        # "Optimise for the scorer" runs are paid LLM calls too
        for r in db.execute("SELECT sync_model, sync_usage_json FROM rubric_version WHERE sync_usage_json IS NOT NULL").fetchall():
            u = _j(r["sync_usage_json"], {}) or {}
            tot["syncs"] += 1
            tot["llm_calls"] += int(u.get("calls") or 0)
            for k in ("prompt_tokens", "completion_tokens", "cached_tokens"):
                tot[k] += int(u.get(k) or 0)
            pin, pout = llm.PRICES["llm"].get(r["sync_model"] or "", (0.0, 0.0))
            tot["est_usd"] += (int(u.get("prompt_tokens") or 0) * pin + int(u.get("completion_tokens") or 0) * pout) / 1e6
        tot["audio_min"] = round(tot["audio_min"], 1)
        tot["est_usd"] = round(tot["est_usd"], 4)
        return tot

    def _identical_audit(self, db, transcript_id, rvid, sp, sm):
        """A finished scorecard of the same transcript, rubric version, model, prompt version AND
        guidance mode (optimised rules vs as written -- the version's current state), if any."""
        if not transcript_id:
            return None
        v = db.execute("SELECT * FROM rubric_version WHERE id=?", (rvid,)).fetchone()
        mode = score_mod.guidance_mode(v, rubric_mod.criteria_for(db, rvid)) if v else "authored"
        # the same reference material too: the worker stores kb.fingerprint() of the documents in scope
        kb_key = kb.fingerprint(kb.docs_for(db, v["rubric_id"])) if v else ""
        row = db.execute("SELECT s.id scorecard_id, s.job_id existing_job_id, s.created_at, s.overall_pct FROM scorecard s"
                         " JOIN job j ON j.id=s.job_id WHERE s.transcript_id=? AND s.rubric_version_id=?"
                         " AND j.scoring_provider=? AND j.scoring_model=? AND s.prompt_version=? AND j.status='done'"
                         " AND COALESCE(s.guidance_mode,'authored')=? AND COALESCE(s.kb_key,'')=?"
                         " ORDER BY s.created_at DESC LIMIT 1",
                         (transcript_id, rvid, sp, sm, score_mod.PROMPT_VERSION, mode, kb_key)).fetchone()
        return dict(row) if row else None

    def _scorer_choice(self, c, rec, scorer_id):
        """(provider, model) for a job on this recording; demo recordings always use the fake."""
        if rec["kind"] == "demo":
            return "demo", "demo"
        if not scorer_id:
            return llm.default_scorer(c)
        return llm.parse_choice(c, "scoring", scorer_id) or (None, None)

    def retranscribe(self, db, u, rec_id):
        """{stt: 'openai:gpt-4o-transcribe', rubric_version_id?, scorer?} -> a new job that
        transcribes the audio again with that engine, then scores. Never overwrites."""
        rec = db.execute("SELECT * FROM recording WHERE id=? AND deleted_at IS NULL", (rec_id,)).fetchone()
        if not rec:
            return self.fail(404, "Not found.")
        if rec["audio_deleted_at"] or not os.path.exists(rec["path"]):
            return self.fail(400, "The audio is no longer stored (retention), so it cannot be transcribed again.")
        d = self.body_json()
        c = core.cfg()
        rvid = d.get("rubric_version_id")
        if not rvid:
            last = db.execute("SELECT rubric_version_id FROM job WHERE recording_id=? ORDER BY created_at DESC LIMIT 1",
                              (rec_id,)).fetchone()
            rvid = last["rubric_version_id"] if last else None
        if not rvid or not db.execute("SELECT 1 FROM rubric_version WHERE id=?", (rvid,)).fetchone():
            return self.fail(400, "Choose a QA type version.")
        if rec["kind"] == "demo":
            provider, model = "demo", None
        else:
            ch = llm.parse_choice(c, "stt", d.get("stt"))
            if not ch:
                return self.fail(400, "Unknown transcription engine '%s'." % d.get("stt"))
            provider, model = ch
            if provider == "openai" and rec["bytes"] > core.OPENAI_STT_CAP:
                return self.fail(400, "OpenAI transcription accepts files up to 25 MB; this one is %.1f MB." % (rec["bytes"] / 1048576.0))
            if c.get("AUDITLY_DEMO") != "1":
                kp = llm.key_problem(c, provider)
                if kp:
                    return self.fail(400, kp)
        sp, sm = self._scorer_choice(c, rec, d.get("scorer"))
        if sp is None:
            return self.fail(400, "Unknown audit model '%s'." % d.get("scorer"))
        if rec["kind"] != "demo" and c.get("AUDITLY_DEMO") != "1":
            kp = llm.key_problem(c, sp)
            if kp:
                return self.fail(400, kp)
        if not d.get("force") and provider != "demo":
            have = db.execute("SELECT t.id, t.created_at, t.job_id FROM transcript t WHERE t.recording_id=? AND t.provider=?"
                              " AND t.model IS ? ORDER BY t.created_at DESC LIMIT 1", (rec_id, provider, model)).fetchone()
            if have:
                sc = db.execute("SELECT s.id scorecard_id, s.overall_pct, j.id existing_job_id FROM scorecard s JOIN job j ON j.id=s.job_id"
                                " WHERE s.transcript_id=? AND j.status='done' ORDER BY s.created_at DESC LIMIT 1", (have["id"],)).fetchone()
                out = {"existing_transcript": True, "transcript_id": have["id"], "created_at": have["created_at"], "cached": True}
                if sc:
                    out.update(dict(sc))
                return self.json(out)                       # nothing queued, nothing billed
        job_id = core.new_id()
        carry = self._carry_source(db, rec_id) if d.get("carry_over") else None
        db.execute("INSERT INTO job (id,recording_id,rubric_version_id,stt_provider,stt_model,scoring_provider,scoring_model,"
                   "transcript_mode,status,created_at,created_by,carry_from_scorecard_id)"
                   " VALUES (?,?,?,?,?,?,?,'fresh','queued',?,?,?)",
                   (job_id, rec_id, rvid, provider, model, sp, sm, core.now(), u["email"], carry))
        core.audit(db, "job.retranscribe", user=u, target=rec_id, ip=self.client_ip(),
                   detail="%s:%s · %s:%s" % (provider, model, sp, sm))
        worker.enqueue(job_id)
        return self.json({"job_id": job_id}, 201)

    # ── jobs ──────────────────────────────────────────────────────────────
    # scorecard.job_id and review.scorecard_id are both UNIQUE, so the joins cannot fan out
    JOB_SQL = ("SELECT j.*, r.name rubric_name, v.version_no, s.id scorecard_id, s.overall_pct, s.auto_fail,"
               " s.applicable_weight sc_basis,"
               " (SELECT COUNT(*) FROM dispute d WHERE d.scorecard_id=s.id AND d.status='open') open_disputes,"
               " rv.status rv_status, rv.final_pct rv_final_pct, rv.coached_at rv_coached_at"
               " FROM job j JOIN rubric_version v ON v.id=j.rubric_version_id"
               " JOIN rubric r ON r.id=v.rubric_id LEFT JOIN scorecard s ON s.job_id=j.id"
               " LEFT JOIN review rv ON rv.scorecard_id=s.id")

    def _job_out(self, db, j):
        rec = db.execute("SELECT * FROM recording WHERE id=?", (j["recording_id"],)).fetchone()
        return to_public("job", j, rubric_name=j["rubric_name"], version_no=j["version_no"],
                         recording=to_public("recording", rec) if rec else None,
                         scorecard_id=j["scorecard_id"],
                         overall_pct=j["overall_pct"] if (j["sc_basis"] or 0) > 0 else None,
                         auto_fail=bool(j["auto_fail"]) if j["auto_fail"] is not None else None,
                         stage=review_stage(j["status"], j["rv_status"], j["rv_coached_at"]),
                         review_status=j["rv_status"], open_disputes=j["open_disputes"] or 0,
                         final_pct=j["rv_final_pct"] if j["rv_status"] == "submitted" and (j["sc_basis"] or 0) > 0 else None)

    def api_jobs(self, db, q):
        status = self.one(q, "status")
        try:
            page = max(1, int(self.one(q, "page", "1")))
        except ValueError:
            page = 1
        size = 50
        where, params = ["1=1"], []
        if status:
            where.append("j.status=?")
            params.append(status)
        kind = self.one(q, "kind")
        if kind in ("real", "demo", "test"):
            where.append("EXISTS (SELECT 1 FROM recording rr WHERE rr.id=j.recording_id AND rr.kind=?)")
            params.append(kind)
        text = self.one(q, "q")
        if text:
            like = "%" + text + "%"
            where.append("EXISTS (SELECT 1 FROM recording rr WHERE rr.id=j.recording_id AND "
                         "(rr.filename LIKE ? OR rr.agent_name LIKE ? OR rr.call_ref LIKE ? OR rr.qa_name LIKE ?"
                         " OR rr.caller_company LIKE ? OR rr.caller_name LIKE ? OR rr.caller_email LIKE ?"
                         " OR rr.audit_name LIKE ?))")
            params += [like] * 8
        recs = _id_list(self.one(q, "recording"))
        if recs:                                 # Focus (0.53.0/0.55.0): only these recordings; an unknown id or "none" matches nothing
            where.append("j.recording_id IN (%s)" % ",".join("?" * len(recs)))
            params += recs
        base = self.JOB_SQL + " WHERE " + " AND ".join(where)
        total = db.execute("SELECT COUNT(*) c FROM (%s)" % base, params).fetchone()["c"]
        rows = db.execute(base + " ORDER BY j.created_at DESC LIMIT ? OFFSET ?",
                          params + [size, (page - 1) * size]).fetchall()
        return self.json({"jobs": [self._job_out(db, j) for j in rows], "total": total,
                          "page": page, "pages": max(1, (total + size - 1) // size)})

    def api_job(self, db, job_id):
        j = db.execute(self.JOB_SQL + " WHERE j.id=?", (job_id,)).fetchone()
        if not j:
            return self.fail(404, "Not found.")
        return self.json(self._job_out(db, j))

    def job_action(self, db, u, job_id, act):
        j = db.execute("SELECT * FROM job WHERE id=?", (job_id,)).fetchone()
        if not j:
            return self.fail(404, "Not found.")
        if act == "retry":
            if j["status"] not in ("failed",):
                return self.fail(400, "Only a failed job can be retried.")
            d = self.body_json()
            provider = (_s(d.get("provider")) or j["stt_provider"] or "").lower()
            c = core.cfg()
            allowed = ("deepgram", "openai", "reuse") + (("demo",) if c.get("AUDITLY_DEMO") == "1" else ())
            if provider not in allowed:
                return self.fail(400, "Unknown transcription provider '%s'." % provider[:40])
            if provider in ("deepgram", "openai") and c.get("AUDITLY_DEMO") != "1":
                kp = llm.key_problem(c, provider)
                if kp:
                    return self.fail(400, kp)
            db.execute("UPDATE job SET status='queued', error=NULL, progress='Queued for retry',"
                       " attempts=0, stt_provider=? WHERE id=?", (provider, job_id))
            core.audit(db, "job.retry", user=u, target=job_id, ip=self.client_ip())
            worker.enqueue(job_id)
            return self.json({"ok": True})
        # delete: the job, its scorecard, and -- if no other job needs it -- the recording + audio
        if u["role"] != "admin":
            return self.fail(403, "Only an admin can delete.")
        rec = db.execute("SELECT * FROM recording WHERE id=?", (j["recording_id"],)).fetchone()
        db.execute("DELETE FROM job WHERE id=?", (job_id,))
        others = db.execute("SELECT COUNT(*) c FROM job WHERE recording_id=?", (j["recording_id"],)).fetchone()["c"]
        if rec and others == 0:
            try:
                if os.path.exists(rec["path"]):
                    os.unlink(rec["path"])
            except OSError as e:
                log("delete: could not unlink %s: %s" % (rec["path"], e))
            db.execute("DELETE FROM recording WHERE id=?", (rec["id"],))
        db.execute("DELETE FROM notification WHERE target_kind='job' AND target_id=?", (job_id,))   # its failure line goes with it
        core.audit(db, "job.delete", user=u, target=job_id, ip=self.client_ip(),
                   detail=rec["filename"] if rec else None)
        return self.json({"ok": True})

    # ── transcript, media ─────────────────────────────────────────────────
    def _latest_transcript(self, db, rec_id):
        return db.execute("SELECT * FROM transcript WHERE recording_id=? ORDER BY created_at DESC LIMIT 1",
                          (rec_id,)).fetchone()

    def api_transcript(self, db, rec_id):
        t = self._latest_transcript(db, rec_id)
        if not t:
            return self.fail(404, "Not found.")
        utts = [to_public("utterance", x) for x in db.execute(
            "SELECT * FROM utterance WHERE transcript_id=? ORDER BY seq", (t["id"],))]
        return self.json(to_public("transcript", t, utterances=utts))

    def media(self, db, u, rec_id):
        rec = db.execute("SELECT * FROM recording WHERE id=? AND deleted_at IS NULL", (rec_id,)).fetchone()
        if not rec or rec["audio_deleted_at"] or not os.path.exists(rec["path"]):
            return self.fail(404, "Not found.")
        total = os.path.getsize(rec["path"])
        headers = {"Content-Type": rec["mime"] or "application/octet-stream",
                   "Content-Disposition": 'inline; filename="%s"' % rec["filename"].replace('"', ""),
                   "Cache-Control": "private, max-age=300", "Accept-Ranges": "bytes"}
        rng = self.headers.get("Range")
        start, end = 0, total - 1
        code = 200
        if rng:
            span = self._parse_range(rng, total)
            if span is None:
                return self.send(416, b"", "text/plain", {"Content-Range": "bytes */%d" % total})
            start, end = span
            code = 206
            headers["Content-Range"] = "bytes %d-%d/%d" % (start, end, total)
        if code == 200:
            core.audit(db, "audio.play", user=u, target=rec_id, ip=self.client_ip())
        length = end - start + 1
        self.send_response(code)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with open(rec["path"], "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(64 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    @staticmethod
    def _parse_range(header, total):
        m = re.match(r"^bytes=(\d*)-(\d*)$", (header or "").strip())
        if not m or total <= 0:
            return None
        a, b = m.group(1), m.group(2)
        if a == "" and b == "":
            return None
        if a == "":
            n = int(b)
            if n <= 0:
                return None
            return max(0, total - n), total - 1
        start = int(a)
        if start >= total:
            return None
        end = int(b) if b else total - 1
        return start, min(end, total - 1)

    # ── scorecards ────────────────────────────────────────────────────────
    ITEM_SQL = ("SELECT i.*, c.key, c.name, c.description, c.critical, c.seq FROM scorecard_item i"
                " JOIN criterion c ON c.id=i.criterion_id WHERE i.scorecard_id=? ORDER BY c.seq")

    def _scorecard_full(self, db, sc, link_just=None):
        items = [to_public("item", x) for x in db.execute(self.ITEM_SQL, (sc["id"],))]
        rec = db.execute("SELECT * FROM recording WHERE id=?", (sc["recording_id"],)).fetchone()
        t = db.execute("SELECT * FROM transcript WHERE id=?", (sc["transcript_id"],)).fetchone()
        utts = [to_public("utterance", x) for x in db.execute(
            "SELECT * FROM utterance WHERE transcript_id=? ORDER BY seq", (sc["transcript_id"],))]
        v = db.execute("SELECT v.version_no, r.name FROM rubric_version v JOIN rubric r ON r.id=v.rubric_id"
                       " WHERE v.id=?", (sc["rubric_version_id"],)).fetchone()
        rv = db.execute("SELECT * FROM review WHERE scorecard_id=?", (sc["id"],)).fetchone()
        disputes = self._disputes_of(db, sc["id"])
        return to_public("scorecard", sc, items=items,
                         recording=to_public("recording", rec) if rec else None,
                         transcript=to_public("transcript", t, utterances=utts) if t else None,
                         rubric_name=v["name"] if v else None, version_no=v["version_no"] if v else None,
                         review=to_public("review", rv) if rv else None,
                         stage=review_stage("done", rv["status"] if rv else None, rv["coached_at"] if rv else None),
                         disputes=disputes, open_disputes=sum(1 for d in disputes if d["status"] == "open"),
                         dispute_link=self._dispute_link_status(db, sc["id"], rec["agent_name"] if rec else None, link_just))

    def api_scorecard(self, db, u, sc_id):
        sc = db.execute("SELECT * FROM scorecard WHERE id=?", (sc_id,)).fetchone()
        if not sc:
            return self.fail(404, "Not found.")
        core.audit(db, "scorecard.view", user=u, target=sc_id, ip=self.client_ip())
        return self.json(self._scorecard_full(db, sc))

    @staticmethod
    def _item_missed(r):
        """After overrides: 0 counts as missed, anything above 0 does not; otherwise the model's rating."""
        if r["rating"] == "na":
            return False                    # not applicable: excluded from the total, cannot fail the call
        if r["override_score"] is not None:
            return int(r["override_score"]) == 0
        return r["rating"] == "missed"

    def _recompute(self, db, sc_id):
        rows = db.execute("SELECT i.*, c.critical FROM scorecard_item i JOIN criterion c ON c.id=i.criterion_id"
                          " WHERE i.scorecard_id=?", (sc_id,)).fetchall()
        pct, basis = score_mod.compute_overall([dict(r) for r in rows])
        auto_fail = 1 if any(r["critical"] and self._item_missed(r) for r in rows) else 0   # follows overrides
        db.execute("UPDATE scorecard SET overall_pct=?, applicable_weight=?, auto_fail=? WHERE id=?",
                   (pct, basis, auto_fail, sc_id))
        return pct

    LOCKED = "This scorecard has a submitted QA review. Reopen it to change scores."

    def _apply_override(self, db, u, sc_id, crit_id, score, note):
        """The one path that writes a human score onto a criterion (the override button and an
        upheld dispute both use it). None when applied, else (status, message) and nothing written."""
        it = db.execute("SELECT * FROM scorecard_item WHERE scorecard_id=? AND criterion_id=?",
                        (sc_id, crit_id)).fetchone()
        if not it:
            return (404, "Not found.")
        if self._review_locked(db, sc_id):
            return (409, self.LOCKED)
        if it["rating"] == "na":
            return (400, "This criterion was rated not applicable, so it has no score to override"
                         " and is left out of the total.")
        note = _s(note)
        if score is None:
            db.execute("UPDATE scorecard_item SET override_score=NULL, override_note=NULL, override_by=NULL,"
                       " override_at=NULL WHERE scorecard_id=? AND criterion_id=?", (sc_id, crit_id))
            core.audit(db, "scorecard.override.clear", user=u, target=sc_id, ip=self.client_ip(), detail=crit_id)
        else:
            try:
                s = int(score)
            except (TypeError, ValueError):
                return (400, "Score must be a whole number.")
            if s < 0 or s > it["weight"]:
                return (400, "Score must be between 0 and %d." % it["weight"])
            if not note:
                return (400, "A note explaining the override is required.")
            db.execute("UPDATE scorecard_item SET override_score=?, override_note=?, override_by=?, override_at=?"
                       " WHERE scorecard_id=? AND criterion_id=?",
                       (s, note, u["email"], core.now(), sc_id, crit_id))
            core.audit(db, "scorecard.override", user=u, target=sc_id, ip=self.client_ip(),
                       detail="%s -> %d: %s" % (crit_id, s, note[:120]))
        self._recompute(db, sc_id)
        return None

    def override(self, db, u, sc_id, crit_id):
        d = self.body_json()
        if d.get("__too_large__"):
            return self.fail(413, "Request too large.")
        problem = self._apply_override(db, u, sc_id, crit_id, d.get("score"), d.get("note"))
        if problem:
            return self.fail(*problem)
        sc = db.execute("SELECT * FROM scorecard WHERE id=?", (sc_id,)).fetchone()
        return self.json(self._scorecard_full(db, sc))

    # ── disputes: the agent's objection, recorded by the QA ───────────────
    DISPUTE_SQL = ("SELECT d.*, c.name criterion_name, c.key criterion_key FROM dispute d"
                   " LEFT JOIN criterion c ON c.id=d.criterion_id")

    def _disputes_of(self, db, sc_id):
        rows = db.execute(self.DISPUTE_SQL + " WHERE d.scorecard_id=? ORDER BY d.created_at DESC", (sc_id,)).fetchall()
        return [to_public("dispute", r) for r in rows]

    def disputes_get(self, db, sc_id):
        if not db.execute("SELECT 1 FROM scorecard WHERE id=?", (sc_id,)).fetchone():
            return self.fail(404, "Not found.")
        return self.json({"disputes": self._disputes_of(db, sc_id)})

    def api_disputes(self, db, q):
        """Every dispute, newest first; ?status= open|upheld|rejected|withdrawn|all (default open), ?agent=, ?kind=."""
        try:
            page = max(1, int(self.one(q, "page", "1")))
        except ValueError:
            page = 1
        size = 50
        where, params = ["r.deleted_at IS NULL"], []
        status = self.one(q, "status", "open")
        if status in ("open", "upheld", "rejected", "withdrawn"):
            where.append("d.status=?")
            params.append(status)
        elif status == "resolved":
            where.append("d.status IN ('upheld','rejected')")
        agent = self.one(q, "agent")
        if agent:
            where.append("d.agent_name=? COLLATE NOCASE")
            params.append(agent)
        kind = self.one(q, "kind", "real")
        if kind in ("real", "demo", "test"):
            where.append("r.kind=?")
            params.append(kind)
        base = ("SELECT d.*, c.name criterion_name, c.key criterion_key, r.audit_name, r.filename, r.id recording_id,"
                " v.final_pct rv_final, v.status rv_status, v.applicable_weight rv_basis FROM dispute d"
                " JOIN scorecard s ON s.id=d.scorecard_id JOIN recording r ON r.id=s.recording_id"
                " LEFT JOIN criterion c ON c.id=d.criterion_id LEFT JOIN review v ON v.scorecard_id=s.id"
                " WHERE " + " AND ".join(where))
        total = db.execute("SELECT COUNT(*) c FROM (%s)" % base, params).fetchone()["c"]
        rows = db.execute(base + " ORDER BY d.created_at DESC LIMIT ? OFFSET ?", params + [size, (page - 1) * size]).fetchall()
        counts = {k: db.execute("SELECT COUNT(*) c FROM dispute d JOIN scorecard s ON s.id=d.scorecard_id"
                                " JOIN recording r ON r.id=s.recording_id WHERE d.status=? AND r.deleted_at IS NULL"
                                + (" AND r.kind=?" if kind in ("real", "demo", "test") else ""),
                                (k,) + ((kind,) if kind in ("real", "demo", "test") else ())).fetchone()["c"]
                  for k in ("open", "upheld", "rejected", "withdrawn")}
        out = [to_public("dispute", d, audit_name=d["audit_name"] or d["filename"], recording_id=d["recording_id"],
                         final_pct=d["rv_final"] if d["rv_status"] == "submitted" and (d["rv_basis"] or 0) > 0 else None,
                         review_status=d["rv_status"]) for d in rows]
        return self.json({"disputes": out, "total": total, "page": page, "pages": max(1, (total + size - 1) // size),
                          "counts": counts})

    def _raise_dispute(self, db, sc, crit, reason, raised_by, actor, via):
        """One open dispute per scorecard+criterion. Shared by the QA path (via='qa', actor=the QA) and the agent's
        emailed link (via='link', actor=None, raised_by=the agent's address). Returns ((status, message), None)
        when refused, else (None, dispute_id)."""
        reason = _s(reason)[:2000]
        if not reason:
            return (400, "Say what you dispute and why." if via == "link" else "Say what the agent disputes and why."), None
        crit = _s(crit) or None
        if crit and not db.execute("SELECT 1 FROM scorecard_item WHERE scorecard_id=? AND criterion_id=?",
                                   (sc["id"], crit)).fetchone():
            return (404, "That criterion is not on this scorecard."), None
        if db.execute("SELECT 1 FROM dispute WHERE scorecard_id=? AND criterion_id IS ? AND status='open'",
                      (sc["id"], crit)).fetchone():
            what = " criterion" if crit else " scorecard"
            return (409, ("You already have an open dispute on this%s; a reviewer will answer it." % what) if via == "link"
                    else "There is already an open dispute on this%s. Resolve or withdraw it first." % what), None
        rec = db.execute("SELECT agent_name FROM recording WHERE id=?", (sc["recording_id"],)).fetchone()
        agent = rec["agent_name"] if rec else None
        now, did = core.now(), core.new_id()
        db.execute("INSERT INTO dispute (id,scorecard_id,criterion_id,agent_name,reason,status,raised_by,via,created_at,updated_at)"
                   " VALUES (?,?,?,?,?,'open',?,?,?,?)", (did, sc["id"], crit, agent, reason, raised_by, via, now, now))
        core.audit(db, "dispute.raise", user=actor, target=sc["id"], ip=self.client_ip(),
                   detail=("via link, %s: " % (agent or "agent") if via == "link" else "")
                   + ("criterion %s: " % crit if crit else "scorecard: ") + reason[:160])
        notify.emit(db, "dispute.raised", "%s disputes a %s" % (agent or "The agent", "criterion" if crit else "scorecard"), reason[:200],
                    "scorecard", sc["id"], agent, actor, recording_id=sc["recording_id"])
        return None, did

    def dispute_raise(self, db, u, sc_id):
        """{reason, criterion_id?} -> a new open dispute recorded by the QA."""
        sc = db.execute("SELECT s.* FROM scorecard s JOIN job j ON j.id=s.job_id WHERE s.id=? AND j.status='done'",
                        (sc_id,)).fetchone()
        if not sc:
            return self.fail(404, "Not found.")
        d = self.body_json()
        if d.get("__too_large__"):
            return self.fail(413, "Request too large.")
        problem, _did = self._raise_dispute(db, sc, d.get("criterion_id"), d.get("reason"), u["email"], u, "qa")
        if problem:
            return self.fail(*problem)
        return self.json(self._scorecard_full(db, sc), 201)

    # ── the agent's dispute link (0.52.0) ───────────────────────────────
    # A long random token, stored hashed, opens /dispute/<token>: a read-only copy of ONE scorecard where the
    # agent raises disputes themselves. Minted and emailed when an audit is submitted (address from Settings ›
    # Names, the SMTP relay), or by the QA from the scorecard (Email / Copy / mail app). Every unexpired link
    # works until the audit is reopened. The raw token is never audited, logged or stored.
    def _public_base(self, c):
        secure = self.headers.get("X-Forwarded-Proto", "").lower() == "https" or isinstance(self.connection, ssl.SSLSocket)
        return public_base(c, self.headers.get("Host"), secure)

    def _link_row(self, db, raw):
        """The live dispute_link row (+ recording_id) for a raw token, or None: unknown, expired, revoked, or links off."""
        if not raw or link_days(core.cfg()) <= 0:
            return None
        return db.execute("SELECT l.*, s.recording_id FROM dispute_link l JOIN scorecard s ON s.id=l.scorecard_id"
                          " WHERE l.token_hash=? AND l.revoked_at IS NULL AND l.expires_at > ?",
                          (core.token_hash(raw), core.now())).fetchone()

    def _dispute_link_mint(self, db, u, sc_id, agent_name, channel, to=None):
        """Insert a link row; returns (raw_token, expires_at). The raw token goes to the caller and nowhere else."""
        raw, h = core.new_session_token()
        exp = (datetime.now(timezone.utc) + timedelta(days=link_days(core.cfg()))).replace(microsecond=0).isoformat()
        db.execute("INSERT INTO dispute_link (token_hash,scorecard_id,agent_name,sent_to,channel,created_by,created_at,expires_at)"
                   " VALUES (?,?,?,?,?,?,?,?)", (h, sc_id, agent_name, to, channel, core.field(u, "email"), core.now(), exp))
        return raw, exp

    @staticmethod
    def _dispute_link_text(rec, pct, auto_fail, url, exp, qa_name):
        first = ((rec["agent_name"] or "").strip().split(" ") or ["there"])[0] or "there"
        score = ("%.1f%%" % pct) if pct is not None else "no score (nothing applicable)"
        return ("Hi %s,\n\nA QA audit of one of your calls (taken %s) was submitted with a final score of %s%s.\n\n"
                "Read your scorecard here:\n%s\n\n"
                "It shows the score for each criterion with the words quoted from the call. If you disagree with a score, press "
                "\"Dispute this score\" next to it (or \"Dispute the whole scorecard\") and say why; a reviewer then upholds or "
                "rejects it, and you can see the outcome on the same page.\n\n"
                "This link is for you only. It works until %s or until the audit is reopened, whichever comes first.\n\n"
                "Thanks,\n%s\n"
                % (first, fmt_when(rec["uploaded_at"]), score, " (auto-fail: a critical item was missed)" if auto_fail else "",
                   url, fmt_when(exp), qa_name or "The QA team"))

    def _dispute_link_send(self, db, u, sc, rec, c, to=None):
        """Mint and email the agent's link. Returns {"ok", "to", "why", ...}; never raises and never fails the caller.
        why: off | no_email | no_mail | failed. The row of a failed send is revoked with the redacted error."""
        if link_days(c) <= 0:
            return {"ok": False, "why": "off"}
        to = _s(to) or self._agent_email(db, rec["agent_name"]) or ""
        if not EMAIL_RE.match(to):
            return {"ok": False, "why": "no_email"}
        pb = mailer.problem(c)
        if pb:
            return {"ok": False, "why": "no_mail", "error": pb}
        raw, exp = self._dispute_link_mint(db, u, sc["id"], rec["agent_name"], "smtp", to)
        url = self._public_base(c) + "/dispute/" + raw
        qa_email = self._qa_email(db, rec, u)
        st = mailer.status(c)
        sender = qa_email if (st["send_as_qa"] and qa_email) else st["from"]
        pct = sc["overall_pct"] if (sc["applicable_weight"] or 0) > 0 else None
        subject = "Your QA scorecard — %s" % (rec["audit_name"] or fmt_when(rec["uploaded_at"]))
        msg = mailer.build_plain(to, sender, subject, self._dispute_link_text(rec, pct, sc["auto_fail"], url, exp, rec["qa_name"]),
                                 reply_to=qa_email, sender_name=rec["qa_name"] or None)
        try:
            mailer.send(c, msg)
        except RuntimeError as e:
            db.execute("UPDATE dispute_link SET revoked_at=?, error=? WHERE token_hash=?", (core.now(), str(e)[:300], core.token_hash(raw)))
            core.audit(db, "dispute.link.failed", user=u, target=sc["id"], ip=self.client_ip(), detail="to %s: %s" % (to, str(e)[:200]))
            return {"ok": False, "why": "failed", "to": to, "error": str(e)}
        core.audit(db, "dispute.link.sent", user=u, target=sc["id"], ip=self.client_ip(), detail="to %s, expires %s" % (to, exp[:10]))
        return {"ok": True, "to": to, "expires_at": exp}

    def _dispute_link_revoke(self, db, u, sc_id, why):
        n = db.execute("UPDATE dispute_link SET revoked_at=? WHERE scorecard_id=? AND revoked_at IS NULL", (core.now(), sc_id)).rowcount
        if n:
            core.audit(db, "dispute.link.revoked", user=u, target=sc_id, ip=self.client_ip(), detail="%d link(s): %s" % (n, why))
        return n

    def _dispute_link_status(self, db, sc_id, agent_name, just=None):
        """What the QA's scorecard shows about the agent's link. No token in here."""
        c = core.cfg()
        now = core.now()
        live = db.execute("SELECT COUNT(*) c FROM dispute_link WHERE scorecard_id=? AND revoked_at IS NULL AND expires_at>?", (sc_id, now)).fetchone()["c"]
        last = db.execute("SELECT * FROM dispute_link WHERE scorecard_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1", (sc_id,)).fetchone()
        return {"enabled": link_days(c) > 0, "days": link_days(c), "live": live, "mail": mailer.status(c)["configured"],
                "agent_email": self._agent_email(db, agent_name), "last": to_public("dispute_link", last) if last else None, "just": just}

    def _agent_view(self, db, sc, link):
        """The agent's copy of the scorecard: to_public("agent_view"), never _scorecard_full (that carries the call text)."""
        items = [to_public("agent_item", x) for x in db.execute(self.ITEM_SQL, (sc["id"],))]
        rec = db.execute("SELECT agent_name, uploaded_at, duration_s FROM recording WHERE id=?", (sc["recording_id"],)).fetchone()
        v = db.execute("SELECT v.version_no, r.name FROM rubric_version v JOIN rubric r ON r.id=v.rubric_id WHERE v.id=?",
                       (sc["rubric_version_id"],)).fetchone()
        rv = db.execute("SELECT status, submitted_at, final_pct, applicable_weight FROM review WHERE scorecard_id=?", (sc["id"],)).fetchone()
        disputes = [to_public("agent_dispute", r) for r in
                    db.execute(self.DISPUTE_SQL + " WHERE d.scorecard_id=? ORDER BY d.created_at DESC", (sc["id"],))]
        sub = bool(rv and rv["status"] == "submitted")
        return to_public("agent_view", sc, items=items, disputes=disputes,
                         agent_name=rec["agent_name"] if rec else None, call_at=rec["uploaded_at"] if rec else None,
                         duration_s=rec["duration_s"] if rec else None,
                         rubric_name=v["name"] if v else None, version_no=v["version_no"] if v else None,
                         submitted_at=rv["submitted_at"] if sub else None,
                         final_pct=rv["final_pct"] if sub and (rv["applicable_weight"] or 0) > 0 else None,
                         expires_at=link["expires_at"])

    def serve_dispute(self, db, raw):
        ok = bool(raw) and self._link_row(db, raw) is not None
        nonce = secrets.token_urlsafe(16)
        html = DISPUTE_HTML.replace("__CSP_NONCE__", nonce).replace("__APP_VERSION__", core.app_version()).encode("utf-8")
        csp = ("default-src 'none'; script-src 'nonce-%s'; style-src 'nonce-%s'; connect-src 'self'; "
               "form-action 'none'; base-uri 'none'; frame-ancestors 'none'" % (nonce, nonce))
        return self.send(200 if ok else 404, html, "text/html; charset=utf-8",
                         {"Content-Security-Policy": csp, "X-Robots-Tag": "noindex, nofollow, noarchive"})

    def dispute_data(self, db, raw):
        link = self._link_row(db, raw)
        if not link:
            return self.json({"error": LINK_GONE}, 404, {"X-Robots-Tag": "noindex"})
        sc = db.execute("SELECT * FROM scorecard WHERE id=?", (link["scorecard_id"],)).fetchone()
        if not sc:
            return self.json({"error": LINK_GONE}, 404, {"X-Robots-Tag": "noindex"})
        db.execute("UPDATE dispute_link SET last_used_at=?, uses=uses+1 WHERE token_hash=?", (core.now(), link["token_hash"]))
        return self.json(self._agent_view(db, sc, link), 200, {"X-Robots-Tag": "noindex"})

    def dispute_raise_public(self, db, raw):
        """The agent, from their link: {reason, criterion_id?}. Same rules as the QA path; the answer is the agent view."""
        link = self._link_row(db, raw)
        if not link:
            return self.fail(404, LINK_GONE)
        sc = db.execute("SELECT s.* FROM scorecard s JOIN job j ON j.id=s.job_id WHERE s.id=? AND j.status='done'",
                        (link["scorecard_id"],)).fetchone()
        if not sc:
            return self.fail(404, LINK_GONE)
        d = self.body_json(limit=20_000)
        if d.get("__too_large__"):
            return self.fail(413, "Request too large.")
        raised_by = self._agent_email(db, link["agent_name"]) or link["sent_to"] or ("agent:%s" % (link["agent_name"] or "unknown"))
        problem, _did = self._raise_dispute(db, sc, d.get("criterion_id"), d.get("reason"), raised_by, None, "link")
        if problem:
            return self.fail(*problem)
        db.execute("UPDATE dispute_link SET last_used_at=? WHERE token_hash=?", (core.now(), link["token_hash"]))
        return self.json(self._agent_view(db, sc, link), 201)

    def dispute_link_get(self, db, u, sc_id):
        sc = db.execute("SELECT s.id, r.agent_name FROM scorecard s JOIN recording r ON r.id=s.recording_id WHERE s.id=?", (sc_id,)).fetchone()
        if not sc:
            return self.fail(404, "Not found.")
        return self.json({"dispute_link": self._dispute_link_status(db, sc_id, sc["agent_name"])})

    def dispute_link_post(self, db, u, sc_id):
        """{action: email | link | mailto, to?}: email the agent their link, or mint one to copy / to paste into your own mail."""
        sc = db.execute("SELECT * FROM scorecard WHERE id=?", (sc_id,)).fetchone()
        if not sc:
            return self.fail(404, "Not found.")
        c = core.cfg()
        if link_days(c) <= 0:
            return self.fail(400, "Dispute links are off: set AUDITLY_DISPUTE_LINK_DAYS in .env.")
        rv = self._review_row(db, sc_id)
        if not (rv and rv["status"] == "submitted"):
            return self.fail(409, "Submit the audit first — the agent's link shows the final score.")
        rec = db.execute("SELECT * FROM recording WHERE id=?", (sc["recording_id"],)).fetchone()
        if not rec:
            return self.fail(404, "Not found.")
        d = self.body_json()
        if d.get("__too_large__"):
            return self.fail(413, "Request too large.")
        act = _s(d.get("action")).lower()
        if act == "email":
            pb = mailer.problem(c)
            if pb:
                return self.fail(400, pb)
            to = _s(d.get("to")) or self._agent_email(db, rec["agent_name"]) or ""
            if not EMAIL_RE.match(to):
                return self.fail(400, "No email for %s: add it under Settings › Names, or type one here." % (rec["agent_name"] or "the agent"))
            res = self._dispute_link_send(db, u, sc, rec, c, to)
            if not res.get("ok"):
                return self.fail(502, res.get("error") or "Could not send the link.")
            return self.json({"sent_to": to, "expires_at": res["expires_at"],
                              "dispute_link": self._dispute_link_status(db, sc_id, rec["agent_name"], res)})
        if act in ("link", "mailto"):
            raw, exp = self._dispute_link_mint(db, u, sc_id, rec["agent_name"], "copy" if act == "link" else "mail_app")
            core.audit(db, "dispute.link.copied" if act == "link" else "dispute.link.mailto", user=u, target=sc_id,
                       ip=self.client_ip(), detail="expires %s" % exp[:10])
            return self.json({"url": self._public_base(c) + "/dispute/" + raw, "expires_at": exp,
                              "agent_email": self._agent_email(db, rec["agent_name"]),
                              "dispute_link": self._dispute_link_status(db, sc_id, rec["agent_name"])})
        return self.fail(400, "action must be email, link or mailto.")

    def dispute_action(self, db, u, did, act):
        """resolve {status: upheld|rejected, note, score?} | withdraw. An upheld dispute with a score
        goes through _apply_override, so a submitted review is refused (409) until reopened and the
        dispute stays open -- the note is never half-applied."""
        dp = db.execute("SELECT * FROM dispute WHERE id=?", (did,)).fetchone()
        if not dp:
            return self.fail(404, "Not found.")
        if dp["status"] != "open":
            return self.fail(409, "This dispute is already %s." % dp["status"])
        d = self.body_json()
        if d.get("__too_large__"):
            return self.fail(413, "Request too large.")
        ip = self.client_ip()
        now = core.now()
        if act == "withdraw":
            db.execute("UPDATE dispute SET status='withdrawn', resolved_by=?, resolved_at=?, updated_at=?,"
                       " resolution_note=? WHERE id=?", (u["email"], now, now, _s(d.get("note"))[:2000] or None, did))
            core.audit(db, "dispute.withdraw", user=u, target=dp["scorecard_id"], ip=ip, detail=did)
        else:
            status = _s(d.get("status")).lower()
            if status not in ("upheld", "rejected"):
                return self.fail(400, "status must be upheld or rejected.")
            note = _s(d.get("note"))[:2000]
            if not note:
                return self.fail(400, "A resolution note is required.")
            score = d.get("score")
            if score is not None:
                if status != "upheld":
                    return self.fail(400, "A new score only goes with an upheld dispute.")
                if not dp["criterion_id"]:
                    return self.fail(400, "This dispute is about the whole scorecard; override the criteria one by one.")
                problem = self._apply_override(db, u, dp["scorecard_id"], dp["criterion_id"], score,
                                               "Dispute upheld: " + note)
                if problem:
                    return self.fail(*problem)
            db.execute("UPDATE dispute SET status=?, resolution_note=?, resolved_by=?, resolved_at=?, updated_at=? WHERE id=?",
                       (status, note, u["email"], now, now, did))
            core.audit(db, "dispute.resolve", user=u, target=dp["scorecard_id"], ip=ip,
                       detail="%s %s%s: %s" % (did, status, (" -> %s" % score) if score is not None else "", note[:120]))
        sc = db.execute("SELECT * FROM scorecard WHERE id=?", (dp["scorecard_id"],)).fetchone()
        outcome = "withdrawn" if act == "withdraw" else status
        notify.emit(db, "dispute.resolved", "%s's dispute %s" % (dp["agent_name"] or "The agent", outcome),
                    (_s(d.get("note"))[:200] or None), "scorecard", dp["scorecard_id"], dp["agent_name"], u,
                    recording_id=sc["recording_id"] if sc else None)
        return self.json(self._scorecard_full(db, sc))

    # ── knowledge base: reference documents the scorer may consult (kb.py) ──────
    KB_TEXT_CAP = kb.MAX_DOC_CHARS

    def _kb_out(self, db, d, with_text=False):
        rn = None
        if d["rubric_id"]:
            r = db.execute("SELECT name FROM rubric WHERE id=?", (d["rubric_id"],)).fetchone()
            rn = r["name"] if r else "(deleted QA type)"
        n = db.execute("SELECT COUNT(*) c FROM kb_chunk WHERE document_id=?", (d["id"],)).fetchone()["c"]
        used = db.execute("SELECT COUNT(*) c FROM scorecard WHERE kb_json LIKE ?", ('%"' + d["id"] + '"%',)).fetchone()["c"]
        return to_public("kb_doc", d, rubric_name=rn, chunks=n, with_text=with_text, used_by=used)

    def api_kb_docs(self, db, q):
        rows = db.execute("SELECT * FROM kb_document WHERE deleted_at IS NULL ORDER BY title COLLATE NOCASE").fetchall()
        return self.json({"documents": [self._kb_out(db, d) for d in rows]})

    def api_kb_doc(self, db, did):
        d = db.execute("SELECT * FROM kb_document WHERE id=? AND deleted_at IS NULL", (did,)).fetchone()
        if not d:
            return self.fail(404, "Not found.")
        return self.json(self._kb_out(db, d, with_text=True))

    # ── Ask Auditly (assistant.py): answers about one call, cites everything, writes nothing ──
    ASK_ACTION = "assistant.ask"

    def _ask_effective(self, db, c):
        """The assistant's switch, model and caps: .env defaults, overridden by whatever an admin saved
        under Settings › Server › Assistant (a NULL column there means "leave the .env value")."""
        _p, m = llm.default_scorer(c)
        eff = {"enabled": (c.get("ASK_ENABLED") or "").strip() == "1",
               "model": (c.get("ASK_MODEL") or "").strip() or m, "default_model": m,
               "max_per_day": max(0, core.cfg_int(c, "ASK_MAX_PER_DAY", 300)),
               "max_per_user_per_hour": max(0, core.cfg_int(c, "ASK_MAX_PER_USER_PER_HOUR", 40)),
               "source": "env", "updated_at": None, "updated_by": None}
        row = db.execute("SELECT * FROM ask_setting WHERE id=1").fetchone()
        if row:
            eff.update(source="settings", updated_at=row["updated_at"], updated_by=row["updated_by"])
            if row["enabled"] is not None:
                eff["enabled"] = bool(row["enabled"])
            if row["model"]:
                eff["model"] = row["model"]
            if row["max_per_day"] is not None:
                eff["max_per_day"] = max(0, int(row["max_per_day"]))
            if row["max_per_user_per_hour"] is not None:
                eff["max_per_user_per_hour"] = max(0, int(row["max_per_user_per_hour"]))
        return eff

    def _ask_prompt_current(self, db):
        """The newest saved persona, or None when there is none or the newest is a reset."""
        r = db.execute("SELECT * FROM ask_prompt ORDER BY version_no DESC LIMIT 1").fetchone()
        return r if r and (r["persona"] or "").strip() else None

    def _ask_problem(self, db, c, eff=None):
        """None when a question may be asked now, else the plain reason. Same shape as rubric_sync.readiness()."""
        eff = eff or self._ask_effective(db, c)
        if not eff["enabled"]:
            return "Ask Auditly is switched off. An admin turns it on under Settings › Server › Assistant (or ASK_ENABLED=1 in .env)."
        if (c.get("AUDITLY_DEMO") or "").strip() != "1":
            p, _m = llm.default_scorer(c)
            kp = llm.key_problem(self._ask_cfg(c), p)
            if kp:
                return "Ask Auditly needs a working scoring key on the server. " + kp
            if (c.get("AUDITLY_ALLOW_SPEND") or "").strip() != "1":
                return "Ask Auditly needs AUDITLY_ALLOW_SPEND=1 on the server; spending is off."
        if self._ask_today(db) >= eff["max_per_day"]:
            return "The server's %d questions for today are used up. Try again tomorrow (UTC)." % eff["max_per_day"]
        return None

    def _ask_today(self, db):
        midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        return db.execute("SELECT COUNT(*) c FROM audit WHERE action=? AND at>=?", (self.ASK_ACTION, midnight)).fetchone()["c"]

    @staticmethod
    def _ask_cfg(c):
        """The scorer's config with the assistant's own key swapped in when one is set: a separate
        OpenAI project key gives the assistant its own spend line and its own cap."""
        k = (c.get("ASK_API_KEY") or "").strip()
        if not k:
            return c
        c2 = dict(c)
        c2["OPENAI_API_KEY"] = k
        return c2

    def _ask_status(self, db, c):
        eff = self._ask_effective(db, c)
        return {"enabled": eff["enabled"], "problem": self._ask_problem(db, c, eff), "source": eff["source"],
                "model": eff["model"], "today": self._ask_today(db), "max_per_day": eff["max_per_day"],
                "prompt_version": assistant.ASK_PROMPT_VERSION}

    def _ask_answer(self, db, u, sc, question, history):
        """The one answer path, shared by the panel and by Settings' Try it: build the fenced context,
        ask, drop uncheckable citations. Returns (turn_fields, error_response)."""
        c = core.cfg()
        eff = self._ask_effective(db, c)
        full = self._scorecard_full(db, sc)
        hits = assistant.retrieve(db, full, question)
        context, allowed = assistant.build_context(full, hits)
        pr = self._ask_prompt_current(db)
        system, user_msg = assistant.build_messages(context, history, question, pr["persona"] if pr else None)
        model = eff["model"] if eff["model"] != eff["default_model"] else None
        scorer = llm.scorer_for(self._ask_cfg(c), None, model)
        try:
            result = scorer.complete_json(system, user_msg, assistant.ASK_SCHEMA, "ask",
                                          max_tokens=core.cfg_int(c, "ASK_MAX_TOKENS", 700))
        except llm.SpendBlocked as e:
            return None, self.fail(409, str(e))
        except llm.ProviderError as e:
            return None, self.fail(502, "The assistant could not answer: " + core.redact_secrets(str(e)))
        answer, cites, grounded, warnings = assistant.clean_answer(result, allowed)
        return {"question": question, "answer": answer, "cites": assistant.cite_labels(cites, full), "grounded": grounded,
                "warnings": warnings, "provider": scorer.name, "model": scorer.model, "usage": scorer.last_usage or {},
                "prompt_id": pr["id"] if pr else None, "about": ((full.get("recording") or {}).get("audit_name") or "this call")}, None

    def _ask_thread(self, db, u, sc_id, create=False):
        row = db.execute("SELECT * FROM ask_thread WHERE user_id=? AND scorecard_id=?", (u["id"], sc_id)).fetchone()
        if row or not create:
            return row
        tid, now = core.new_id(), core.now()
        db.execute("INSERT INTO ask_thread (id,user_id,scorecard_id,created_at,updated_at) VALUES (?,?,?,?,?)", (tid, u["id"], sc_id, now, now))
        return db.execute("SELECT * FROM ask_thread WHERE id=?", (tid,)).fetchone()

    def api_ask_thread(self, db, u, q):
        """?scorecard_id= -> this person's conversation about that call, so reopening the panel shows it."""
        sc_id = self.one(q, "scorecard_id")
        if not sc_id or not re.match("^%s$" % UUID, sc_id):
            return self.fail(400, "scorecard_id is required.")
        th = self._ask_thread(db, u, sc_id)
        turns = [] if not th else [to_public("ask_turn", r) for r in db.execute(
            "SELECT * FROM ask_turn WHERE thread_id=? ORDER BY seq", (th["id"],))]
        return self.json({"thread_id": th["id"] if th else None, "turns": turns})

    def api_ask(self, db, u):
        """{scorecard_id, question} -> one answer about that call, cited, recorded, audited. The assistant
        reads through _scorecard_full() (the same whitelist the scorecard page uses) and writes nothing
        but its own turn. Refused, before anything is spent, when it is off, unpaid, capped or throttled."""
        c = core.cfg()
        d = self.body_json(limit=20_000)
        if d.get("__too_large__"):
            return self.fail(413, "Too large.")
        sc_id, question = _s(d.get("scorecard_id")), (d.get("question") or "").strip()
        if not sc_id or not re.match("^%s$" % UUID, sc_id):
            return self.fail(400, "Open a call first.")
        qp = assistant.question_problem(question)
        if qp:
            return self.fail(400, qp)
        eff = self._ask_effective(db, c)
        problem = self._ask_problem(db, c, eff)
        if problem:
            return self.fail(409, problem)
        n = self._ask_hour_count(db, u)
        if n >= eff["max_per_user_per_hour"]:
            return self.fail(429, "That is %d questions in an hour; give it a little while." % n)
        sc = db.execute("SELECT * FROM scorecard WHERE id=?", (sc_id,)).fetchone()
        if not sc:
            return self.fail(404, "Not found.")
        th = self._ask_thread(db, u, sc_id, create=True)
        history = [dict(r) for r in db.execute("SELECT question, answer FROM ask_turn WHERE thread_id=? ORDER BY seq DESC LIMIT ?",
                                               (th["id"], assistant.MAX_TURNS))][::-1]
        t, err = self._ask_answer(db, u, sc, question, history)
        if err:
            return err
        seq = db.execute("SELECT COALESCE(MAX(seq),0)+1 n FROM ask_turn WHERE thread_id=?", (th["id"],)).fetchone()["n"]
        now, tid = core.now(), core.new_id()
        db.execute("INSERT INTO ask_turn (id,thread_id,seq,question,answer,cites_json,grounded,prompt_version,provider,model,usage_json,warnings_json,created_at,prompt_id)"
                   " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (tid, th["id"], seq, question, t["answer"], json.dumps(t["cites"]), 1 if t["grounded"] else 0, assistant.ASK_PROMPT_VERSION,
                    t["provider"], t["model"], json.dumps(t["usage"]), json.dumps(t["warnings"]), now, t["prompt_id"]))
        db.execute("UPDATE ask_thread SET updated_at=? WHERE id=?", (now, th["id"]))
        core.audit(db, self.ASK_ACTION, user=u, target=sc_id, ip=self.client_ip(), detail=question[:120])
        row = db.execute("SELECT * FROM ask_turn WHERE id=?", (tid,)).fetchone()
        return self.json(dict(to_public("ask_turn", row), thread_id=th["id"]), 201)

    # ── Settings › Server › Assistant (admin): the switch, model, caps, the editable persona, Try it ──
    def _ask_admin_out(self, db, c):
        eff = self._ask_effective(db, c)
        eff["problem"] = self._ask_problem(db, c, eff)
        row = db.execute("SELECT * FROM ask_setting WHERE id=1").fetchone()
        cur = self._ask_prompt_current(db)
        prompts = [to_public("ask_prompt", r) for r in db.execute("SELECT * FROM ask_prompt ORDER BY version_no DESC LIMIT 12")]
        usage = {"today": self._ask_today(db), "max_per_day": eff["max_per_day"], "total": 0, "prompt_tokens": 0, "completion_tokens": 0, "est_usd": 0.0}
        for r in db.execute("SELECT model, usage_json FROM ask_turn").fetchall():
            u2 = _j(r["usage_json"], {}) or {}
            usage["total"] += 1
            usage["prompt_tokens"] += int(u2.get("prompt_tokens") or 0)
            usage["completion_tokens"] += int(u2.get("completion_tokens") or 0)
            pin, pout = llm.PRICES["llm"].get(r["model"] or "", (0.0, 0.0))
            usage["est_usd"] += (int(u2.get("prompt_tokens") or 0) * pin + int(u2.get("completion_tokens") or 0) * pout) / 1e6
        usage["est_usd"] = round(usage["est_usd"], 4)
        dp, _dm = llm.default_scorer(c)
        models = [{"model": m, "label": lbl, "note": note} for (prov, m, lbl, note) in llm.scoring_choices(c) if prov == dp]
        return {"effective": eff, "settings": to_public("ask_setting", row or {}), "default_model": eff["default_model"], "models": models,
                "prompt": (to_public("ask_prompt", cur) if cur else {"id": None, "version_no": None, "persona": assistant.PERSONA_DEFAULT, "builtin": True}),
                "prompts": prompts, "rules": assistant.RULES, "code_prompt_version": assistant.ASK_PROMPT_VERSION, "usage": usage}

    def api_admin_ask_get(self, db, u):
        return self.json(self._ask_admin_out(db, core.cfg()))

    def api_admin_ask_set(self, db, u):
        """{enabled?, model?, max_per_day?, max_per_user_per_hour?}: only the keys sent change; the rest keep
        their saved value or fall through to .env. Model must be one of the scoring provider's choices; blank = default."""
        c = core.cfg()
        d = self.body_json()
        row = db.execute("SELECT * FROM ask_setting WHERE id=1").fetchone()
        vals = {k: (row[k] if row else None) for k in ("enabled", "model", "max_per_day", "max_per_user_per_hour")}
        if "enabled" in d:
            vals["enabled"] = 1 if d.get("enabled") else 0
        if "model" in d:
            m = _s(d.get("model"))
            dp, _dm = llm.default_scorer(c)
            allowed = {x[1] for x in llm.scoring_choices(c) if x[0] == dp}
            if m and m not in allowed:
                return self.fail(400, "Choose one of the scoring provider's models: " + ", ".join(sorted(allowed)) + ".")
            vals["model"] = m or None
        for k in ("max_per_day", "max_per_user_per_hour"):
            if k in d:
                try:
                    v = int(d.get(k))
                except (TypeError, ValueError):
                    return self.fail(400, "%s must be a whole number." % k)
                if not 0 <= v <= 10000:
                    return self.fail(400, "%s must be between 0 and 10000." % k)
                vals[k] = v
        now = core.now()
        db.execute("INSERT INTO ask_setting (id,enabled,model,max_per_day,max_per_user_per_hour,updated_at,updated_by) VALUES (1,?,?,?,?,?,?)"
                   " ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled, model=excluded.model, max_per_day=excluded.max_per_day,"
                   " max_per_user_per_hour=excluded.max_per_user_per_hour, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                   (vals["enabled"], vals["model"], vals["max_per_day"], vals["max_per_user_per_hour"], now, u["email"]))
        core.audit(db, "assistant.settings", user=u, ip=self.client_ip(), detail=json.dumps({k: v for k, v in d.items() if k in vals})[:160])
        return self.json(self._ask_admin_out(db, c))

    def _ask_prompt_save(self, db, u, persona, notes):
        vno = int(db.execute("SELECT COALESCE(MAX(version_no),0)+1 n FROM ask_prompt").fetchone()["n"])
        pid = core.new_id()
        db.execute("INSERT INTO ask_prompt (id,version_no,persona,notes,created_at,created_by) VALUES (?,?,?,?,?,?)",
                   (pid, vno, persona, notes or None, core.now(), u["email"]))
        core.audit(db, "assistant.prompt", user=u, target=pid, ip=self.client_ip(), detail="v%d%s" % (vno, " reset" if not persona else ""))
        return db.execute("SELECT * FROM ask_prompt WHERE id=?", (pid,)).fetchone()

    def api_admin_ask_prompt(self, db, u):
        """{persona, notes} -> a new version. The locked rules are not part of it and are never stored."""
        d = self.body_json(limit=100_000)
        persona = (d.get("persona") or "").strip()
        pp = assistant.persona_problem(persona)
        if pp:
            return self.fail(400, pp)
        if assistant.RULES[:40] in persona:
            return self.fail(400, "The rules are added automatically; leave them out of the instructions.")
        r = self._ask_prompt_save(db, u, persona, _s(d.get("notes"))[:160])
        return self.json(to_public("ask_prompt", r), 201)

    def api_admin_ask_reset(self, db, u):
        """Back to the built-in instructions, recorded as a version so the history shows it."""
        if not self._ask_prompt_current(db):
            return self.fail(409, "The built-in instructions are already in use.")
        r = self._ask_prompt_save(db, u, "", "reset to built-in")
        return self.json(to_public("ask_prompt", r), 201)

    def api_admin_ask_try(self, db, u):
        """{question} -> an answer about the newest scored call, through the real path, so an admin sees what a
        saved persona does before anyone else. Not threaded; audited and counted toward the day like any question."""
        c = core.cfg()
        d = self.body_json(limit=20_000)
        question = (d.get("question") or "").strip()
        qp = assistant.question_problem(question)
        if qp:
            return self.fail(400, qp)
        eff = self._ask_effective(db, c)
        problem = self._ask_problem(db, c, eff)
        if problem:
            return self.fail(409, problem)
        sc = db.execute("SELECT s.* FROM scorecard s JOIN job j ON j.id=s.job_id WHERE j.status='done' ORDER BY s.created_at DESC LIMIT 1").fetchone()
        if not sc:
            return self.fail(409, "There is no scored call to try it on yet.")
        t, err = self._ask_answer(db, u, sc, question, [])
        if err:
            return err
        core.audit(db, self.ASK_ACTION, user=u, target=sc["id"], ip=self.client_ip(), detail="[try] " + question[:110])
        return self.json({"question": question, "answer": t["answer"], "cites": t["cites"], "grounded": t["grounded"], "about": t["about"],
                          "model": t["model"], "prompt_id": t["prompt_id"], "usage": t["usage"]})

    def _tls_url(self, c):
        """The https:// twin of the link the page was loaded from, when the TLS listener is on (0.49.0).
        The page shows it where voice needs a secure context."""
        tport = core.cfg_int(c, "AUDITLY_TLS_PORT", 0)
        if tport <= 0:
            return None
        host = re.sub(r":\d+$", "", (self.headers.get("Host") or "").strip()) or "localhost"
        return "https://%s:%d/" % (host, tport)

    def _ask_hour_count(self, db, u):
        """Questions asked plus clips spoken by this person in the last hour: both cost money, so both count."""
        hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        n = db.execute("SELECT COUNT(*) c FROM ask_turn t JOIN ask_thread th ON th.id=t.thread_id"
                       " WHERE th.user_id=? AND t.created_at>=?", (u["id"], hour_ago)).fetchone()["c"]
        n += db.execute("SELECT COUNT(*) c FROM ask_voice WHERE user_id=? AND created_at>=?", (u["id"], hour_ago)).fetchone()["c"]
        return n

    # ── voice: a spoken question, transcribed by the desk's own STT provider, into the question box ──
    VOICE_EXT = {"webm": "audio/webm", "ogg": "audio/ogg", "m4a": "audio/mp4"}   # what MediaRecorder produces
    VOICE_MAX_BYTES = 2 * 1024 * 1024                                              # about a minute of Opus
    VOICE_DEMO_TEXT = "How did the agent do on the greeting?"

    def api_ask_voice(self, db, u, q):
        """Raw body: ?ext=webm|ogg|m4a. Every check runs from the headers before a byte is read -- the
        extension, the size, that the assistant is on and paid for, the hourly cap -- so a refused clip
        costs nothing. The clip is written to a 0600 temp file for the provider and deleted in `finally`;
        the text is returned, not stored. In demo mode the fake transcriber would hand back a whole canned
        call, so demo returns a fixed question instead and the flow stays testable with no key."""
        c = core.cfg()
        ext = (self.one(q, "ext") or "").lower()
        try:
            size = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            size = 0
        problem = None
        if ext not in self.VOICE_EXT:
            problem = (400, "Unsupported clip type '%s'. Use %s." % (ext, ", ".join(sorted(self.VOICE_EXT))))
        elif size <= 0:
            problem = (400, "Nothing was recorded.")
        elif size > self.VOICE_MAX_BYTES:
            problem = (413, "That clip is %.1f MB; keep a question under a minute." % (size / 1048576.0))
        if not problem:
            eff = self._ask_effective(db, c)
            why = self._ask_problem(db, c, eff)
            if why:
                problem = (409, why)
            elif self._ask_hour_count(db, u) >= eff["max_per_user_per_hour"]:
                problem = (429, "That is a lot of questions in an hour; give it a little while.")
        if problem:
            self.close_connection = True        # do not read a body we are refusing
            return self.fail(*problem)
        data = self.rfile.read(size)
        self._body_done = len(data) == size
        if len(data) != size:
            return self.fail(400, "The clip ended early.")
        demo = (c.get("AUDITLY_DEMO") or "").strip() == "1"
        provider = "demo" if demo else (c.get("TRANSCRIBE_PROVIDER") or "deepgram").strip().lower()
        text, dur, model = "", 0.0, "demo"
        if demo:
            text, dur = self.VOICE_DEMO_TEXT, round(max(1.0, size / 4000.0), 1)     # Opus at ~32 kbit/s
        else:
            path = os.path.join(core.upload_dir(c), "voice-%s.%s" % (core.new_id(), ext))
            try:
                with open(path, "wb") as f:
                    os.chmod(path, 0o600)
                    f.write(data)
                t = transcribe.transcriber_for(c, provider)
                t.clip = True
                try:
                    r = t.transcribe(path, self.VOICE_EXT[ext])
                except llm.SpendBlocked as e:
                    return self.fail(409, str(e))
                except (llm.ProviderError, ValueError) as e:
                    return self.fail(502, "Could not transcribe the question: " + core.redact_secrets(str(e)))
                text = (r.get("full_text") or " ".join(x.get("text", "") for x in r.get("utterances") or [])).strip()
                dur = float(r.get("duration_s") or 0)
                model = r.get("model") or ""
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        if not text:
            return self.fail(422, "Nothing was heard. Try again a little closer to the microphone.")
        text = text[:assistant.MAX_QUESTION]
        vid, now = core.new_id(), core.now()
        db.execute("INSERT INTO ask_voice (id,user_id,created_at,audio_s,provider,model,chars) VALUES (?,?,?,?,?,?,?)",
                   (vid, u["id"], now, dur, provider, model, len(text)))
        core.audit(db, "assistant.voice", user=u, ip=self.client_ip(), detail="%.1fs via %s" % (dur, provider))
        row = db.execute("SELECT * FROM ask_voice WHERE id=?", (vid,)).fetchone()
        return self.json(to_public("ask_voice", row, text=text))

    def api_kb_search(self, db, q):
        """?q=&rubric_id= -> the excerpts the scorer would be shown for that text: how a QA checks the material works."""
        text = self.one(q, "q")
        if not text:
            return self.fail(400, "q is required.")
        hits, key = kb.retrieve_for(db, self.one(q, "rubric_id") or None, text[:20000])
        return self.json({"hits": [to_public("kb_hit", h) for h in hits], "documents": len(kb.docs_for(db, self.one(q, "rubric_id") or None))})

    def kb_upload(self, db, u):
        """{title?, filename, content_b64} or {title, text}, rubric_id? -> a new enabled document, chunked."""
        if not can_edit_rubrics(u):
            return self.fail(403, "Only an admin or a QA reviewer can manage the knowledge base.")
        d = self.body_json(12_000_000)
        if d.get("__too_large__"):
            return self.fail(413, "Document is too large (12 MB max).")
        fname = _s(d.get("filename"))[:200]
        text = d.get("text") if isinstance(d.get("text"), str) else ""
        if d.get("content_b64"):
            import base64
            try:
                text = rubric_mod.text_from_upload(fname, base64.b64decode(d["content_b64"]))
            except ValueError as e:
                return self.fail(400, str(e))
            except Exception:                     # noqa: BLE001
                return self.fail(400, "Could not decode that file.")
        text = (text or "").replace("\r\n", "\n").strip()
        if len(text) < 40:
            return self.fail(400, "The document is empty or too short to be useful (40 characters at least).")
        if len(text) > self.KB_TEXT_CAP:
            return self.fail(400, "The document is over %d characters; split it." % self.KB_TEXT_CAP)
        title = _s(d.get("title"))[:160] or re.sub(r"\.[A-Za-z0-9]+$", "", fname) or "Untitled"
        rubric_id = _s(d.get("rubric_id")) or None
        if rubric_id and not db.execute("SELECT 1 FROM rubric WHERE id=?", (rubric_id,)).fetchone():
            return self.fail(400, "Unknown QA type.")
        chunks = kb.chunk(text)
        if not chunks:
            return self.fail(400, "Nothing readable in that document.")
        did, now = core.new_id(), core.now()
        db.execute("INSERT INTO kb_document (id,title,filename,chars,text,enabled,rubric_id,uploaded_by,created_at,updated_at)"
                   " VALUES (?,?,?,?,?,1,?,?,?,?)", (did, title, fname or None, len(text), text, rubric_id, u["email"], now, now))
        for i, ch in enumerate(chunks):
            db.execute("INSERT INTO kb_chunk (document_id,seq,text) VALUES (?,?,?)", (did, i, ch))
        core.audit(db, "kb.upload", user=u, target=did, ip=self.client_ip(), detail="%s · %d chars · %d chunks" % (title, len(text), len(chunks)))
        return self.json(self._kb_out(db, db.execute("SELECT * FROM kb_document WHERE id=?", (did,)).fetchone()), 201)

    def kb_action(self, db, u, did, act):
        if not can_edit_rubrics(u):
            return self.fail(403, "Only an admin or a QA reviewer can manage the knowledge base.")
        d0 = db.execute("SELECT * FROM kb_document WHERE id=? AND deleted_at IS NULL", (did,)).fetchone()
        if not d0:
            return self.fail(404, "Not found.")
        d = self.body_json()
        now = core.now()
        if act in ("enable", "disable"):
            db.execute("UPDATE kb_document SET enabled=?, updated_at=? WHERE id=?", (1 if act == "enable" else 0, now, did))
        elif act == "rename":
            title = _s(d.get("title"))[:160]
            if not title:
                return self.fail(400, "Title is required.")
            db.execute("UPDATE kb_document SET title=?, updated_at=? WHERE id=?", (title, now, did))
        elif act == "scope":
            rubric_id = _s(d.get("rubric_id")) or None
            if rubric_id and not db.execute("SELECT 1 FROM rubric WHERE id=?", (rubric_id,)).fetchone():
                return self.fail(400, "Unknown QA type.")
            db.execute("UPDATE kb_document SET rubric_id=?, updated_at=? WHERE id=?", (rubric_id, now, did))
        else:                                                   # delete: soft, admin only
            if u["role"] != "admin":
                return self.fail(403, "Only an admin can delete a document; disable it instead.")
            db.execute("UPDATE kb_document SET deleted_at=?, enabled=0, updated_at=? WHERE id=?", (now, now, did))
            db.execute("DELETE FROM kb_chunk WHERE document_id=?", (did,))
        core.audit(db, "kb." + act, user=u, target=did, ip=self.client_ip(), detail=d0["title"])
        row = db.execute("SELECT * FROM kb_document WHERE id=?", (did,)).fetchone()
        return self.json(self._kb_out(db, row) if act != "delete" else {"ok": True})

    # ── coaching sessions: an agent's audited calls, grouped, scheduled, delivered ──
    # one row per audited call: the scorecard plus its review, disputes and whether a live session holds it
    CALL_SQL = ("SELECT s.id scorecard_id, s.recording_id, s.job_id, s.overall_pct, s.applicable_weight, s.auto_fail, s.misses_json,"
                " r.audit_name, r.filename, r.call_ref, r.uploaded_at, r.agent_name, r.kind,"
                " v.status rv_status, v.final_pct, v.applicable_weight rv_basis, v.coached_at, v.coaching_notes, v.action_plan,"
                " (SELECT COUNT(*) FROM dispute d WHERE d.scorecard_id=s.id AND d.status='open') open_disputes,"
                " (SELECT csc.session_id FROM coaching_session_call csc JOIN coaching_session cs ON cs.id=csc.session_id"
                "  WHERE csc.scorecard_id=s.id AND cs.status<>'cancelled' LIMIT 1) in_session"
                " FROM scorecard s JOIN job j ON j.id=s.job_id AND j.status='done'"
                " JOIN recording r ON r.id=s.recording_id AND r.deleted_at IS NULL"
                " LEFT JOIN review v ON v.scorecard_id=s.id")

    def _session_row(self, db, sid):
        return db.execute("SELECT * FROM coaching_session WHERE id=?", (sid,)).fetchone()

    def _attach_items(self, db, rows):
        """Every scored criterion of these calls, so a selection can be added up per criterion."""
        rows = [dict(r) for r in rows]
        ids = [r["scorecard_id"] for r in rows]
        by = {i: [] for i in ids}
        for k in range(0, len(ids), 400):
            chunk = ids[k:k + 400]
            for it in db.execute("SELECT i.scorecard_id, c.key, c.name, i.weight, i.rating, COALESCE(i.override_score, i.score) final"
                                 " FROM scorecard_item i JOIN criterion c ON c.id=i.criterion_id WHERE i.scorecard_id IN (%s) ORDER BY c.seq"
                                 % ",".join("?" * len(chunk)), chunk):
                by[it["scorecard_id"]].append({"key": it["key"], "name": it["name"], "weight": it["weight"], "rating": it["rating"], "final": it["final"]})
        for r in rows:
            r["items"] = by.get(r["scorecard_id"], [])
        return rows

    def _target(self):
        return dashboard.target_of(core.cfg().get("AUDITLY_TARGET_PCT"))

    def _session_calls(self, db, sid):
        rows = db.execute(self.CALL_SQL + " JOIN coaching_session_call c2 ON c2.scorecard_id=s.id AND c2.session_id=?"
                          " ORDER BY c2.seq", (sid,)).fetchall()
        return self._attach_items(db, rows)

    def _agent_email(self, db, name):
        p = db.execute("SELECT email FROM person WHERE kind='agent' AND name=? COLLATE NOCASE", (name or "",)).fetchone()
        return (p["email"] or None) if p else None

    def _qa_email(self, db, s, u=None):
        """Who the invite is from / replies go to: the QA of record's email (Settings › Names), else the
        signed-in reviewer's account email (never the open-access placeholder)."""
        if s["qa_name"]:
            p = db.execute("SELECT email FROM person WHERE kind='qa' AND name=? COLLATE NOCASE", (s["qa_name"],)).fetchone()
            if p and p["email"]:
                return p["email"]
        if u and u["email"] and not u["email"].endswith("@local") and EMAIL_RE.match(u["email"]):
            return u["email"]
        return None

    def _session_out(self, db, s, conflicts=None, u=None):
        calls = [to_public("session_call", r) for r in self._session_calls(db, s["id"])]
        invites = [to_public("invite", r) for r in db.execute("SELECT * FROM coaching_invite WHERE session_id=? ORDER BY sent_at DESC, rowid DESC LIMIT 5", (s["id"],))]
        return to_public("session", s, calls=calls, agent_email=self._agent_email(db, s["agent_name"]), conflicts=conflicts,
                         summary=coaching.summarise(calls, self._target()), qa_email=self._qa_email(db, s, u), invites=invites)

    def session_invite(self, db, u, sid):
        """Email the calendar invite (method=REQUEST) to the agent. {to?, qa_email?}: defaults are the agent's
        email from Settings › Names and the QA of record's. Records every attempt in coaching_invite."""
        s = self._session_row(db, sid)
        if not s:
            return self.fail(404, "Not found.")
        if s["status"] != "scheduled" or not s["scheduled_at"]:
            return self.fail(409, "Schedule the session first; the invite needs a time.")
        c = core.cfg()
        pb = mailer.problem(c)
        if pb:
            return self.fail(400, pb)
        d = self.body_json()
        to = _s(d.get("to")) or self._agent_email(db, s["agent_name"]) or ""
        if not EMAIL_RE.match(to):
            return self.fail(400, "No email for %s: add it under Settings › Names, or type one here." % s["agent_name"])
        qa_email = _s(d.get("qa_email")) or self._qa_email(db, s, u)
        if qa_email and not EMAIL_RE.match(qa_email):
            return self.fail(400, "The QA email does not look like an address.")
        st = mailer.status(c)
        sender = qa_email if (st["send_as_qa"] and qa_email) else st["from"]
        body = self._invite_text(s, u)
        ics = coaching.ics(dict(s), agent_email=to, organizer_email=qa_email or sender)
        msg = mailer.build_invite(dict(s), to, qa_email, s["qa_name"] or (u["name"] or None), ics, body, sender, reply_to=qa_email)
        now, iid = core.now(), core.new_id()
        try:
            mailer.send(c, msg)
        except RuntimeError as e:
            db.execute("INSERT INTO coaching_invite (id,session_id,to_email,from_email,status,channel,error,sent_by,sent_at) VALUES (?,?,?,?,'failed','smtp',?,?,?)",
                       (iid, sid, to, sender, str(e)[:300], u["email"], now))
            core.audit(db, "coaching.invite.failed", user=u, target=sid, ip=self.client_ip(), detail=str(e)[:200])
            return self.fail(502, str(e))
        db.execute("INSERT INTO coaching_invite (id,session_id,to_email,from_email,status,channel,sent_by,sent_at) VALUES (?,?,?,?,'sent','smtp',?,?)",
                   (iid, sid, to, sender, u["email"], now))
        db.execute("UPDATE coaching_session SET invite_sent_at=?, invite_to=?, updated_at=? WHERE id=?", (now, to, now, sid))
        core.audit(db, "coaching.invite", user=u, target=sid, ip=self.client_ip(), detail="to %s from %s" % (to, sender))
        return self.json(self._session_out(db, self._session_row(db, sid), u=u))

    def _invite_text(self, s, u):
        return ("Hi %s,\n\nLet's meet for a coaching session on %s (%d min)%s.\n\n%s\n\nAccept the calendar invite to add it to your calendar.\n\nThanks,\n%s"
                % (s["agent_name"], fmt_when(s["scheduled_at"]), s["duration_min"] or coaching.DEFAULT_MINUTES, (" at " + s["location"]) if s["location"] else "",
                   s["agenda"] or "", s["qa_name"] or (u["name"] if u["name"] else "QA")))

    def session_eml(self, db, u, sid, q):
        """The invite as an .eml DRAFT for the QA's own mail app (Outlook opens X-Unsent mail as a new
        message from the user's account, calendar attached). ?to= and ?qa= override the defaults; a blank
        To is allowed -- the QA fills it in the app. Nothing is sent or recorded as sent here."""
        s = self._session_row(db, sid)
        if not s:
            return self.fail(404, "Not found.")
        if s["status"] != "scheduled" or not s["scheduled_at"]:
            return self.fail(409, "Schedule the session first; the invite needs a time.")
        to = self.one(q, "to", "") or self._agent_email(db, s["agent_name"]) or ""
        if to and not EMAIL_RE.match(to):
            return self.fail(400, "The To address does not look like an email.")
        qa_email = self.one(q, "qa", "") or self._qa_email(db, s, u) or ""
        if qa_email and not EMAIL_RE.match(qa_email):
            return self.fail(400, "The QA email does not look like an address.")
        ics = coaching.ics(dict(s), agent_email=to or None, organizer_email=qa_email or None)
        msg = mailer.build_invite(dict(s), to, qa_email, s["qa_name"] or (u["name"] or None), ics, self._invite_text(s, u),
                                  qa_email or None, reply_to=None, draft=True)
        core.audit(db, "coaching.invite.draft", user=u, target=sid, ip=self.client_ip(), detail="to %s" % (to or "(blank)"))
        name = "coaching-invite-%s-%s" % ((s["scheduled_at"] or "")[:10], slug(s["agent_name"]))
        return self.send(200, msg.as_bytes(), "message/rfc822", {"Content-Disposition": 'attachment; filename="%s.eml"' % name})

    def session_invite_sent(self, db, u, sid):
        """{to}: the QA sent the invite from their own mail app -- record it so the ✉ mark and the weekly
        summary are true. Nothing is sent here."""
        s = self._session_row(db, sid)
        if not s:
            return self.fail(404, "Not found.")
        if s["status"] != "scheduled" or not s["scheduled_at"]:
            return self.fail(409, "Schedule the session first.")
        d = self.body_json()
        to = _s(d.get("to")) or self._agent_email(db, s["agent_name"]) or ""
        if not EMAIL_RE.match(to):
            return self.fail(400, "Say who the invite went to (an email address).")
        now = core.now()
        db.execute("INSERT INTO coaching_invite (id,session_id,to_email,from_email,status,channel,sent_by,sent_at) VALUES (?,?,?,?,'sent','mail_app',?,?)",
                   (core.new_id(), sid, to, self._qa_email(db, s, u), u["email"], now))
        db.execute("UPDATE coaching_session SET invite_sent_at=?, invite_to=?, updated_at=? WHERE id=?", (now, to, now, sid))
        core.audit(db, "coaching.invite.manual", user=u, target=sid, ip=self.client_ip(), detail="to %s (mail app)" % to)
        return self.json(self._session_out(db, self._session_row(db, sid), u=u))

    def mail_test(self, db, u):
        """Admin: send a plain test message to prove the relay works. {to?} defaults to the admin's own address."""
        c = core.cfg()
        pb = mailer.problem(c)
        if pb:
            return self.fail(400, pb)
        d = self.body_json()
        to = _s(d.get("to")) or (u["email"] if not u["email"].endswith("@local") else "")
        if not EMAIL_RE.match(to):
            return self.fail(400, "Give an address to send the test to.")
        st = mailer.status(c)
        msg = mailer.build_plain(to, st["from"], "Auditly mail test",
                                 "This is a test from Auditly (%s). If you can read this, coaching invites and the agents' dispute links will send.\n" % core.app_version())
        try:
            mailer.send(c, msg)
        except RuntimeError as e:
            core.audit(db, "mail.test.failed", user=u, ip=self.client_ip(), detail=str(e)[:200])
            return self.fail(502, str(e))
        core.audit(db, "mail.test", user=u, ip=self.client_ip(), detail=to)
        return self.json({"ok": True, "to": to, "from": st["from"]})

    def _draft(self, db, agent, sid):
        calls = []
        for r in self._session_calls(db, sid):
            dl = [d["reason"] for d in db.execute("SELECT reason FROM dispute WHERE scorecard_id=? AND status='open'", (r["scorecard_id"],))]
            calls.append({"audit_name": r["audit_name"] or r["filename"], "uploaded_at": r["uploaded_at"],
                          "final_pct": r["final_pct"] if r["rv_status"] == "submitted" and (r["rv_basis"] or 0) > 0 else None,
                          "overall_pct": r["overall_pct"] if (r["applicable_weight"] or 0) > 0 else None, "review_status": r["rv_status"],
                          "auto_fail": r["auto_fail"], "open_disputes": r["open_disputes"], "items": r.get("items") or [],
                          "misses": [m.get("text") if isinstance(m, dict) else str(m) for m in (_j(r["misses_json"], []) or [])],
                          "coaching_notes": r["coaching_notes"], "action_plan": r["action_plan"], "disputes": dl})
        return coaching.draft_agenda(agent, calls, self._target())

    def _live_session_of(self, db, sc_id, exclude=None):
        row = db.execute("SELECT cs.id FROM coaching_session_call csc JOIN coaching_session cs ON cs.id=csc.session_id"
                         " WHERE csc.scorecard_id=? AND cs.status<>'cancelled'" + (" AND cs.id<>?" if exclude else ""),
                         (sc_id,) + ((exclude,) if exclude else ())).fetchone()
        return row["id"] if row else None

    def _check_calls(self, db, agent, ids, sid):
        """Can these scorecards join a session of this agent? (status, message) or None. Reads only --
        the connection autocommits, so every refusal must happen before anything is written."""
        if not isinstance(ids, list):
            return (400, "scorecard_ids must be a list.")
        for sc_id in ids:
            if not isinstance(sc_id, str) or not re.match("^%s$" % UUID, sc_id):
                return (400, "Not a scorecard id: %s" % str(sc_id)[:40])
            row = db.execute(self.CALL_SQL + " WHERE s.id=?", (sc_id,)).fetchone()
            if not row:
                return (404, "Scorecard %s is not a scored call." % sc_id[:8])
            if (row["agent_name"] or "").strip().lower() != (agent or "").strip().lower():
                return (400, "'%s' is %s's call, not %s's." % (row["audit_name"] or row["filename"], row["agent_name"] or "nobody", agent))
            other = self._live_session_of(db, sc_id, exclude=sid)
            if other:
                return (409, "'%s' is already in another coaching session (%s)." % (row["audit_name"] or row["filename"], other[:8]))
        return None

    def _add_calls(self, db, s, ids):
        """Attach already-checked scorecards to a session (skips ones it already holds)."""
        seq = db.execute("SELECT COALESCE(MAX(seq),0) m FROM coaching_session_call WHERE session_id=?", (s["id"],)).fetchone()["m"]
        for sc_id in ids:
            if db.execute("SELECT 1 FROM coaching_session_call WHERE session_id=? AND scorecard_id=?", (s["id"], sc_id)).fetchone():
                continue
            seq += 1
            db.execute("INSERT INTO coaching_session_call (session_id,scorecard_id,seq) VALUES (?,?,?)", (s["id"], sc_id, seq))

    def api_sessions(self, db, q):
        """?agent= &status= planned|scheduled|done|cancelled|open|all (default open) &from= &to= (local dates via &tz=) &page="""
        try:
            page = max(1, int(self.one(q, "page", "1")))
        except ValueError:
            page = 1
        size = 50
        where, params = ["1=1"], []
        status = self.one(q, "status", "open")
        if status in coaching.STATUSES:
            where.append("cs.status=?")
            params.append(status)
        elif status == "open":
            where.append("cs.status IN ('planned','scheduled')")
        agent = self.one(q, "agent")
        if agent:
            where.append("cs.agent_name=? COLLATE NOCASE")
            params.append(agent)
        base = "SELECT cs.*, (SELECT COUNT(*) FROM coaching_session_call c WHERE c.session_id=cs.id) n_calls FROM coaching_session cs WHERE " + " AND ".join(where)
        total = db.execute("SELECT COUNT(*) c FROM (%s)" % base, params).fetchone()["c"]
        order = "cs.scheduled_at IS NULL, cs.scheduled_at ASC, cs.created_at DESC" if status in ("open", "planned", "scheduled") else "COALESCE(cs.done_at, cs.cancelled_at, cs.scheduled_at, cs.created_at) DESC"
        rows = db.execute(base + " ORDER BY " + order + " LIMIT ? OFFSET ?", params + [size, (page - 1) * size]).fetchall()
        counts = {k: db.execute("SELECT COUNT(*) c FROM coaching_session WHERE status=?" + (" AND agent_name=? COLLATE NOCASE" if agent else ""),
                                (k,) + ((agent,) if agent else ())).fetchone()["c"] for k in coaching.STATUSES}
        return self.json({"sessions": [self._session_out(db, s) for s in rows], "total": total, "page": page,
                          "pages": max(1, (total + size - 1) // size), "counts": counts})

    def session_get(self, db, sid):
        s = self._session_row(db, sid)
        if not s:
            return self.fail(404, "Not found.")
        return self.json(self._session_out(db, s))

    CANDIDATE_SCOPES = {"waiting": " AND ((v.status='submitted' AND v.coached_at IS NULL)"
                                   "  OR EXISTS (SELECT 1 FROM dispute d WHERE d.scorecard_id=s.id AND d.status='open'))",
                        "audited": " AND v.status='submitted'",
                        "all": ""}

    def api_candidates(self, db, q):
        """Calls the QA may put in a coaching session for ?agent=, newest scorecard per recording, with
        every scored criterion attached so a selection can be added up. ?scope= waiting (default:
        audited and not yet coached, or with an open dispute) | audited (every submitted review, coached
        or not) | all (every scored call). in_session names the live session already holding a call."""
        agent = self.one(q, "agent")
        if not agent:
            return self.fail(400, "agent is required.")
        scope = self.one(q, "scope", "waiting")
        if scope not in self.CANDIDATE_SCOPES:
            return self.fail(400, "scope must be waiting, audited or all.")
        kind = self.one(q, "kind", "real")
        rows = db.execute(self.CALL_SQL + " WHERE r.agent_name=? COLLATE NOCASE AND " + self.REVIEW_ONE
                          + (" AND r.kind=?" if kind in ("real", "demo", "test") else "") + self.CANDIDATE_SCOPES[scope]
                          + " ORDER BY r.uploaded_at DESC LIMIT 200", (agent,) + ((kind,) if kind in ("real", "demo", "test") else ())).fetchall()
        return self.json({"calls": [to_public("session_call", r) for r in self._attach_items(db, rows)], "scope": scope,
                          "target": self._target()})

    def api_coach_agents(self, db, q):
        """Per agent: audited calls waiting for coaching, open disputes, live sessions, next session, last coached."""
        kind = self.one(q, "kind", "real")
        kf = " AND r.kind=?" if kind in ("real", "demo", "test") else ""
        kp = (kind,) if kind in ("real", "demo", "test") else ()
        names = {}
        for p in db.execute("SELECT name, email, active FROM person WHERE kind='agent' ORDER BY name COLLATE NOCASE"):
            names[p["name"].lower()] = {"name": p["name"], "email": p["email"], "active": p["active"]}
        for r in db.execute("SELECT DISTINCT agent_name FROM recording WHERE agent_name IS NOT NULL AND trim(agent_name)<>'' AND deleted_at IS NULL"):
            names.setdefault(r["agent_name"].lower(), {"name": r["agent_name"], "email": None, "active": 0})
        out = []
        now = core.now()
        for key in sorted(names, key=lambda k: names[k]["name"].lower()):
            a = dict(names[key])
            row = db.execute("SELECT SUM(CASE WHEN v.status='submitted' THEN 1 ELSE 0 END) audited,"
                             " SUM(CASE WHEN v.status='submitted' AND v.coached_at IS NULL AND NOT EXISTS (SELECT 1 FROM coaching_session_call csc"
                             "   JOIN coaching_session cs ON cs.id=csc.session_id WHERE csc.scorecard_id=s.id AND cs.status<>'cancelled') THEN 1 ELSE 0 END) to_coach,"
                             " MAX(v.coached_at) last_coached_at"
                             " FROM scorecard s JOIN job j ON j.id=s.job_id AND j.status='done' JOIN recording r ON r.id=s.recording_id AND r.deleted_at IS NULL"
                             " LEFT JOIN review v ON v.scorecard_id=s.id WHERE r.agent_name=? COLLATE NOCASE" + kf, (a["name"],) + kp).fetchone()
            a.update({"audited": row["audited"] or 0, "to_coach": row["to_coach"] or 0, "last_coached_at": row["last_coached_at"]})
            a["open_disputes"] = db.execute("SELECT COUNT(*) c FROM dispute d JOIN scorecard s ON s.id=d.scorecard_id JOIN recording r ON r.id=s.recording_id"
                                            " WHERE d.status='open' AND r.agent_name=? COLLATE NOCASE AND r.deleted_at IS NULL" + kf, (a["name"],) + kp).fetchone()["c"]
            a["sessions_open"] = db.execute("SELECT COUNT(*) c FROM coaching_session WHERE agent_name=? COLLATE NOCASE AND status IN ('planned','scheduled')", (a["name"],)).fetchone()["c"]
            nx = db.execute("SELECT id, scheduled_at FROM coaching_session WHERE agent_name=? COLLATE NOCASE AND status='scheduled' AND scheduled_at>=?"
                            " ORDER BY scheduled_at LIMIT 1", (a["name"], now)).fetchone()
            a["next_session_at"] = nx["scheduled_at"] if nx else None
            a["next_session_id"] = nx["id"] if nx else None
            if a["active"] or a["audited"] or a["open_disputes"] or a["sessions_open"]:
                out.append(to_public("coach_agent", a))
        return self.json({"agents": out})

    def api_calendar(self, db, q):
        """?from=&to= (YYYY-MM-DD, local) &tz= &agent= -> sessions by local day, plus review follow-up dates."""
        tz = dashboard.clamp_tz(self.one(q, "tz", "0"))
        try:
            from_d = dashboard.parse_date(self.one(q, "from"))
            to_d = dashboard.parse_date(self.one(q, "to"))
        except ValueError as e:
            return self.fail(400, str(e))
        if from_d > to_d or (to_d - from_d).days > 62:
            return self.fail(400, "The range must run forwards and cover at most 62 days.")
        agent = self.one(q, "agent")
        days = {}
        for s in db.execute("SELECT * FROM coaching_session WHERE scheduled_at IS NOT NULL AND status<>'cancelled'"
                            + (" AND agent_name=? COLLATE NOCASE" if agent else "") + " ORDER BY scheduled_at", ((agent,) if agent else ())):
            d = dashboard.local_date(s["scheduled_at"], tz)
            if d is None or d < from_d or d > to_d:
                continue
            n = db.execute("SELECT COUNT(*) c FROM coaching_session_call WHERE session_id=?", (s["id"],)).fetchone()["c"]
            days.setdefault(d.isoformat(), {"date": d.isoformat(), "sessions": [], "follow_ups": []})["sessions"].append(
                to_public("session", s, calls=[None] * n, agent_email=None))
        for r in db.execute("SELECT v.follow_up_on, v.scorecard_id, r.agent_name, r.audit_name, r.filename FROM review v"
                            " JOIN recording r ON r.id=v.recording_id AND r.deleted_at IS NULL WHERE v.follow_up_on IS NOT NULL"
                            + (" AND r.agent_name=? COLLATE NOCASE" if agent else ""), ((agent,) if agent else ())):
            try:
                d = dashboard.parse_date(r["follow_up_on"])
            except ValueError:
                continue
            if d < from_d or d > to_d:
                continue
            days.setdefault(d.isoformat(), {"date": d.isoformat(), "sessions": [], "follow_ups": []})["follow_ups"].append(
                {"scorecard_id": r["scorecard_id"], "agent_name": r["agent_name"], "audit_name": r["audit_name"] or r["filename"]})
        for d in days.values():
            for s in d["sessions"]:
                s["calls"] = []
        return self.json({"from": from_d.isoformat(), "to": to_d.isoformat(), "tz": tz,
                          "days": [days[k] for k in sorted(days)]})

    def session_create(self, db, u):
        """{agent_name, qa_name?, title?, scorecard_ids?} -> a planned session with a drafted agenda."""
        d = self.body_json()
        if d.get("__too_large__"):
            return self.fail(413, "Request too large.")
        name = " ".join(_s(d.get("agent_name")).split())
        if not name:
            return self.fail(400, "Choose the agent.")
        p = db.execute("SELECT * FROM person WHERE kind='agent' AND name=? COLLATE NOCASE", (name,)).fetchone()
        if p:
            name = p["name"]
        elif not db.execute("SELECT 1 FROM recording WHERE agent_name=? COLLATE NOCASE", (name,)).fetchone():
            return self.fail(400, person_problem(db, "agent", name))
        qa = None
        if d.get("qa_name"):
            _, qa, problem = self._review_fields(db, {"qa_name": d.get("qa_name")})
            if problem:
                return self.fail(400, problem)
        ids = d.get("scorecard_ids") or []
        problem = self._check_calls(db, name, ids, None)
        if problem:
            return self.fail(*problem)
        now = core.now()
        sid = core.new_id()
        db.execute("INSERT INTO coaching_session (id,agent_name,qa_name,title,status,duration_min,created_by,created_at,updated_at)"
                   " VALUES (?,?,?,?,'planned',?,?,?,?)",
                   (sid, name, qa or None, _s(d.get("title"))[:160] or None, coaching.DEFAULT_MINUTES, u["email"], now, now))
        s = self._session_row(db, sid)
        self._add_calls(db, s, ids)
        db.execute("UPDATE coaching_session SET agenda=? WHERE id=?", (self._draft(db, name, sid), sid))
        core.audit(db, "coaching.create", user=u, target=sid, ip=self.client_ip(),
                   detail="%s · %d calls" % (name, len(d.get("scorecard_ids") or [])))
        return self.json(self._session_out(db, self._session_row(db, sid)), 201)

    SESSION_TEXT = (("title", 160), ("agenda", 20000), ("notes", 20000), ("location", 200))

    def session_post(self, db, u, sid, act):
        s = self._session_row(db, sid)
        if not s:
            return self.fail(404, "Not found.")
        d = self.body_json()
        if d.get("__too_large__"):
            return self.fail(413, "Request too large.")
        ip, now = self.client_ip(), core.now()
        closed = s["status"] in ("done", "cancelled")
        if act is None:                                   # edit title / agenda / notes / location / QA
            sets, vals = [], []
            for k, cap in self.SESSION_TEXT:
                if k in d:
                    v = _s(d.get(k))[:cap] or None
                    sets.append("%s=?" % k)
                    vals.append(v)
                    if k == "agenda":
                        sets.append("agenda_edited=1")
            if "qa_name" in d:
                _, qa, problem = self._review_fields(db, {"qa_name": d.get("qa_name")})
                if problem:
                    return self.fail(400, problem)
                sets.append("qa_name=?")
                vals.append(qa or None)
            if sets:
                db.execute("UPDATE coaching_session SET %s, updated_at=? WHERE id=?" % ", ".join(sets), vals + [now, sid])
            core.audit(db, "coaching.update", user=u, target=sid, ip=ip, detail=", ".join(k for k, _ in self.SESSION_TEXT if k in d))
        elif act == "calls":
            if closed:
                return self.fail(409, "This session is %s; reopen it to change its calls." % s["status"])
            rem = d.get("remove") or []
            add = d.get("add") or []
            problem = self._check_calls(db, s["agent_name"], add, sid)
            if problem:
                return self.fail(*problem)
            for sc_id in rem:
                if isinstance(sc_id, str):
                    db.execute("DELETE FROM coaching_session_call WHERE session_id=? AND scorecard_id=?", (sid, sc_id))
            self._add_calls(db, s, add)
            if not s["agenda_edited"]:
                db.execute("UPDATE coaching_session SET agenda=? WHERE id=?", (self._draft(db, s["agent_name"], sid), sid))
            db.execute("UPDATE coaching_session SET updated_at=? WHERE id=?", (now, sid))
            core.audit(db, "coaching.calls", user=u, target=sid, ip=ip, detail="+%d -%d" % (len(d.get("add") or []), len(rem)))
        elif act == "schedule":
            if closed:
                return self.fail(409, "This session is %s." % s["status"])
            try:
                when = coaching.parse_when(d.get("scheduled_at"), d.get("tz"))
                mins = coaching.minutes(d.get("duration_min") or s["duration_min"] or coaching.DEFAULT_MINUTES)
            except ValueError as e:
                return self.fail(400, str(e))
            qa = s["qa_name"]
            others = db.execute("SELECT id, scheduled_at, duration_min, agent_name FROM coaching_session WHERE id<>? AND status='scheduled'"
                                " AND scheduled_at IS NOT NULL AND (agent_name=? COLLATE NOCASE" + (" OR qa_name=? COLLATE NOCASE" if qa else "") + ")",
                                (sid, s["agent_name"]) + ((qa,) if qa else ())).fetchall()
            hits = coaching.overlaps(when, mins, [(o["id"], o["scheduled_at"], o["duration_min"]) for o in others])
            if hits and not d.get("force"):
                conf = [self._session_out(db, self._session_row(db, h)) for h in hits]
                return self.json({"error": "That time overlaps another coaching session (%s). Pick another time, or schedule anyway."
                                  % ", ".join("%s %s" % (c["agent_name"], fmt_when(c["scheduled_at"])) for c in conf),
                                  "conflicts": conf}, 409)
            loc = _s(d.get("location"))[:200] if "location" in d else s["location"]
            db.execute("UPDATE coaching_session SET status='scheduled', scheduled_at=?, duration_min=?, location=?, updated_at=? WHERE id=?",
                       (coaching.iso(when), mins, loc or None, now, sid))
            core.audit(db, "coaching.schedule", user=u, target=sid, ip=ip, detail="%s · %d min" % (coaching.iso(when), mins))
            # s is the row before the UPDATE, so a reschedule can say where it moved from (0.51.0)
            new_txt = "%s · %d min%s" % (fmt_when(coaching.iso(when)), mins, (" · " + loc) if loc else "")
            if s["status"] == "scheduled" and s["scheduled_at"] and s["scheduled_at"] != coaching.iso(when):
                notify.emit(db, "coaching.changed", "Coaching with %s rescheduled" % s["agent_name"],
                            "Was %s → now %s" % (fmt_when(s["scheduled_at"]), new_txt), "session", sid, s["agent_name"], u)
            elif s["status"] != "scheduled":
                notify.emit(db, "coaching.scheduled", "Coaching with %s scheduled" % s["agent_name"], new_txt, "session", sid, s["agent_name"], u)
        elif act == "unschedule":
            if s["status"] != "scheduled":
                return self.fail(409, "This session is not scheduled.")
            db.execute("UPDATE coaching_session SET status='planned', scheduled_at=NULL, updated_at=? WHERE id=?", (now, sid))
            core.audit(db, "coaching.unschedule", user=u, target=sid, ip=ip)
            notify.emit(db, "coaching.changed", "Coaching with %s unscheduled" % s["agent_name"],
                        "Unscheduled (was %s)" % fmt_when(s["scheduled_at"]), "session", sid, s["agent_name"], u)
        elif act == "done":
            if closed:
                return self.fail(409, "This session is already %s." % s["status"])
            ids = [r["scorecard_id"] for r in db.execute("SELECT scorecard_id FROM coaching_session_call WHERE session_id=?", (sid,))]
            if not ids:
                return self.fail(400, "Add at least one call before marking the session delivered.")
            marks = ", ".join("?" * len(ids))
            # coached_at = done_at on every SUBMITTED review that was not coached yet, so reopen can undo exactly this
            n = db.execute("UPDATE review SET coached_at=?, updated_at=? WHERE scorecard_id IN (%s) AND status='submitted' AND coached_at IS NULL" % marks,
                           [now, now] + ids).rowcount
            skipped = [r["scorecard_id"] for r in db.execute(
                "SELECT c.scorecard_id FROM coaching_session_call c LEFT JOIN review v ON v.scorecard_id=c.scorecard_id"
                " WHERE c.session_id=? AND (v.id IS NULL OR v.status<>'submitted')", (sid,))]
            db.execute("UPDATE coaching_session SET status='done', done_at=?, updated_at=? WHERE id=?", (now, now, sid))
            if "notes" in d:
                db.execute("UPDATE coaching_session SET notes=? WHERE id=?", (_s(d.get("notes"))[:20000] or None, sid))
            core.audit(db, "coaching.done", user=u, target=sid, ip=ip, detail="%d marked coached, %d not submitted" % (n, len(skipped)))
            out = self._session_out(db, self._session_row(db, sid))
            out["coached"] = n
            out["skipped"] = skipped
            return self.json(out)
        elif act == "reopen":
            if s["status"] != "done":
                return self.fail(409, "Only a delivered session can be reopened.")
            ids = [r["scorecard_id"] for r in db.execute("SELECT scorecard_id FROM coaching_session_call WHERE session_id=?", (sid,))]
            if ids:
                db.execute("UPDATE review SET coached_at=NULL, updated_at=? WHERE scorecard_id IN (%s) AND coached_at=?" % ", ".join("?" * len(ids)),
                           [now] + ids + [s["done_at"]])
            db.execute("UPDATE coaching_session SET status=?, done_at=NULL, updated_at=? WHERE id=?",
                       ("scheduled" if s["scheduled_at"] else "planned", now, sid))
            core.audit(db, "coaching.reopen", user=u, target=sid, ip=ip)
        elif act == "cancel":
            if s["status"] == "done":
                return self.fail(409, "A delivered session cannot be cancelled; reopen it first.")
            if s["status"] == "cancelled":
                return self.fail(409, "Already cancelled.")
            db.execute("UPDATE coaching_session SET status='cancelled', cancelled_at=?, updated_at=? WHERE id=?", (now, now, sid))
            core.audit(db, "coaching.cancel", user=u, target=sid, ip=ip)
            notify.emit(db, "coaching.changed", "Coaching with %s cancelled" % s["agent_name"],
                        "Cancelled" + ((" (was %s)" % fmt_when(s["scheduled_at"])) if s["scheduled_at"] else ""), "session", sid, s["agent_name"], u)
        return self.json(self._session_out(db, self._session_row(db, sid), u=u))

    def session_export(self, db, u, sid, fmt):
        s = self._session_row(db, sid)
        if not s:
            return self.fail(404, "Not found.")
        out = self._session_out(db, s)
        core.audit(db, "coaching.export." + fmt, user=u, target=sid, ip=self.client_ip())
        name = "auditly-coaching-%s-%s" % ((s["scheduled_at"] or s["created_at"] or "")[:10], slug(s["agent_name"]))
        if fmt == "ics":
            if not s["scheduled_at"]:
                return self.fail(409, "Schedule the session first; the calendar event needs a time.")
            body = coaching.ics(dict(s), agent_email=out.get("agent_email"),
                                organizer_email=None if open_access(core.cfg()) else u["email"])
            return self.send(200, body, "text/calendar; charset=utf-8", {"Content-Disposition": 'attachment; filename="%s.ics"' % name})
        p = pdfgen.Pdf("Coaching session - %s" % s["agent_name"])
        p.text("Coaching session: %s" % s["agent_name"], 16, bold=True, gap_after=2)
        meta = [("status " + (s["status"] or "planned"))]
        if s["scheduled_at"]:
            meta.append("%s · %d min" % (fmt_when(s["scheduled_at"]), s["duration_min"] or coaching.DEFAULT_MINUTES))
        if s["location"]:
            meta.append(s["location"])
        if s["qa_name"]:
            meta.append("QA %s" % s["qa_name"])
        p.text("  ·  ".join(meta), 9.5, color=(0.35, 0.35, 0.35), gap_after=6)
        if s["title"]:
            p.text(s["title"], 12, bold=True, gap_after=4)
        tot = out.get("summary") or {}
        if tot.get("calls"):
            p.rule()
            p.text("Totals", 11, bold=True, gap_after=2)
            line = "%d call%s" % (tot["calls"], "" if tot["calls"] == 1 else "s")
            if tot.get("avg_pct") is not None:
                line += "  ·  average %.1f%%" % tot["avg_pct"]
                if tot.get("scored", 0) > 1:
                    line += " (lowest %.1f%%, highest %.1f%%)" % (tot["min_pct"], tot["max_pct"])
                line += "  ·  %d below the %d%% target" % (tot["below_target"], tot["target"])
            line += "  ·  %d misses  ·  %d auto-fail%s  ·  %d open dispute%s" % (tot.get("misses", 0), tot.get("auto_fails", 0), "" if tot.get("auto_fails") == 1 else "s",
                                                                                  tot.get("open_disputes", 0), "" if tot.get("open_disputes") == 1 else "s")
            p.text(line, 9.5, color=(0.3, 0.3, 0.3), gap_after=2)
            for k in [x for x in tot.get("by_criterion") or [] if x.get("lost_total")][:6]:
                p.text("%s: %s points lost across the calls (%s per call)  ·  missed %d, partial %d" % (k["name"], k["lost_total"], k["lost_points"], k["missed"], k["partial"]),
                       9.5, indent=10, color=(0.3, 0.3, 0.3), gap_after=1)
            p.space(4)
        p.rule()
        p.text("Agenda", 11, bold=True, gap_after=2)
        for line in (s["agenda"] or "(none)").split("\n"):
            p.text(line or " ", 10, gap_after=0)
        p.space(8)
        p.rule()
        p.text("Calls (%d)" % len(out["calls"]), 11, bold=True, gap_after=3)
        for c in out["calls"]:
            fp = ("%.1f%%" % c["final_pct"]) if c.get("final_pct") is not None else (("AI %.1f%% (not audited)" % c["overall_pct"]) if c.get("overall_pct") is not None else "n/a")
            p.text("%s  -  %s%s" % (c["audit_name"], fp, "  ·  AUTO-FAIL" if c.get("auto_fail") else ""), 10, bold=True, gap_after=1)
            p.text("uploaded %s%s%s" % (fmt_when(c.get("uploaded_at")), "  ·  coached %s" % fmt_when(c["coached_at"]) if c.get("coached_at") else "",
                                       "  ·  %d open dispute(s)" % c["open_disputes"] if c.get("open_disputes") else ""), 9, color=(0.35, 0.35, 0.35), gap_after=1)
            for m in (c.get("misses") or [])[:6]:
                p.text("- %s" % (m.get("text") if isinstance(m, dict) else m), 9.5, indent=10, color=(0.25, 0.25, 0.25))
            p.space(4)
        if s["notes"]:
            p.rule()
            p.text("Session notes", 11, bold=True, gap_after=2)
            for line in s["notes"].split("\n"):
                p.text(line or " ", 10, gap_after=0)
        return self.send(200, p.build(), "application/pdf", {"Content-Disposition": 'attachment; filename="%s.pdf"' % name})

    def speakers(self, db, u, sc_id):
        """{speakers:[{speaker, role, name}]} -- the reviewer's word on who is who. The single
        'agent' role becomes agent_speaker (the part that is scored)."""
        sc = db.execute("SELECT * FROM scorecard WHERE id=?", (sc_id,)).fetchone()
        if not sc:
            return self.fail(404, "Not found.")
        if self._review_locked(db, sc_id):
            return self.fail(409, self.LOCKED)
        d = self.body_json()
        lst = d.get("speakers")
        if not isinstance(lst, list):
            return self.fail(400, "speakers must be a list.")
        out, seen = [], set()
        for sp in lst:
            if not isinstance(sp, dict):
                return self.fail(400, "Each speaker must be an object.")
            try:
                n = int(sp.get("speaker"))
            except (TypeError, ValueError):
                return self.fail(400, "speaker must be a number.")
            role = _s(sp.get("role")).lower()
            if role not in score_mod.ROLES:
                return self.fail(400, "role must be one of %s." % ", ".join(score_mod.ROLES))
            if n in seen:
                continue
            seen.add(n)
            out.append({"speaker": n, "role": role, "name": _s(sp.get("name"))[:80] or None})
        agents = [x["speaker"] for x in out if x["role"] == "agent"]
        agent_speaker = agents[0] if len(agents) == 1 else (sc["agent_speaker"] if not agents else agents[0])
        db.execute("UPDATE scorecard SET speakers_json=?, agent_speaker=? WHERE id=?", (json.dumps(out), agent_speaker, sc_id))
        self._retone(db, sc_id)
        core.audit(db, "scorecard.speakers", user=u, target=sc_id, ip=self.client_ip(),
                   detail=", ".join("S%d=%s" % (x["speaker"], x["role"]) for x in out)[:300])
        sc = db.execute("SELECT * FROM scorecard WHERE id=?", (sc_id,)).fetchone()
        return self.json(self._scorecard_full(db, sc))

    def agent_speaker(self, db, u, sc_id):
        sc = db.execute("SELECT * FROM scorecard WHERE id=?", (sc_id,)).fetchone()
        if not sc:
            return self.fail(404, "Not found.")
        if self._review_locked(db, sc_id):
            return self.fail(409, self.LOCKED)
        d = self.body_json()
        sp = d.get("speaker")
        if sp is not None:
            try:
                sp = int(sp)
            except (TypeError, ValueError):
                return self.fail(400, "Speaker must be a number or null.")
        db.execute("UPDATE scorecard SET agent_speaker=? WHERE id=?", (sp, sc_id))
        self._retone(db, sc_id)
        core.audit(db, "scorecard.agent_speaker", user=u, target=sc_id, ip=self.client_ip(), detail=str(sp))

    def _retone(self, db, sc_id):
        """Tone & delivery follows who the agent is: recompute after a speaker swap."""
        sc = db.execute("SELECT * FROM scorecard WHERE id=?", (sc_id,)).fetchone()
        utts = [dict(r) for r in db.execute("SELECT * FROM utterance WHERE transcript_id=? ORDER BY seq", (sc["transcript_id"],))]
        t = tone.compute(utts, sc["agent_speaker"], _j(sc["speakers_json"], None))
        db.execute("UPDATE scorecard SET tone_json=? WHERE id=?", (json.dumps(t) if t else None, sc_id))
        sc = db.execute("SELECT * FROM scorecard WHERE id=?", (sc_id,)).fetchone()
        return self.json(self._scorecard_full(db, sc))

    # ── reviews: the human QA audit of a scorecard ────────────────────────
    REVIEW_TEXT = ("coaching_notes", "resolution_notes", "action_plan")
    REVIEW_SQL = ("SELECT s.*, r.uploaded_at FROM scorecard s JOIN job j ON j.id=s.job_id AND j.status='done'"
                  " JOIN recording r ON r.id=s.recording_id AND r.deleted_at IS NULL"
                  " LEFT JOIN review v ON v.scorecard_id=s.id")
    # one row per recording: the reviewed scorecard if any, else the newest done one
    REVIEW_ONE = ("s.id = (SELECT s2.id FROM scorecard s2 JOIN job j2 ON j2.id=s2.job_id AND j2.status='done'"
                  " LEFT JOIN review v2 ON v2.scorecard_id=s2.id WHERE s2.recording_id=s.recording_id"
                  " ORDER BY (v2.status='submitted') DESC, (v2.id IS NOT NULL) DESC, s2.created_at DESC LIMIT 1)")

    def _review_row(self, db, sc_id):
        return db.execute("SELECT * FROM review WHERE scorecard_id=?", (sc_id,)).fetchone()

    def _review_locked(self, db, sc_id):
        r = self._review_row(db, sc_id)
        return bool(r and r["status"] == "submitted")

    def _review_fields(self, db, d):
        """Validated fields from a request body -> (fields, qa_name | False, problem).
        qa_name False = not sent; '' = clear."""
        out = {}
        for k in self.REVIEW_TEXT:
            if k in d:
                v = d.get(k)
                out[k] = (str(v).strip()[:4000] or None) if v is not None else None
        if "follow_up_on" in d:
            v = d.get("follow_up_on")
            if v in (None, ""):
                out["follow_up_on"] = None
            else:
                try:
                    dashboard.parse_date(v)
                except ValueError:
                    return None, False, "Follow-up date must be YYYY-MM-DD."
                out["follow_up_on"] = v
        qa = False
        if "qa_name" in d:
            name = " ".join(str(d.get("qa_name") or "").split())
            if name:
                row = person_lookup(db, "qa", name)
                if not row:
                    return None, False, person_problem(db, "qa", name)
                qa = row["name"]
            else:
                qa = ""
        return out, qa, None

    def _review_upsert(self, db, u, sc, fields, qa, keep_reviewer=False):
        """keep_reviewer: leave reviewer_email alone (marking coaching delivered on a submitted
        audit must not re-credit the audit to whoever ticked the box)."""
        now = core.now()
        if not self._review_row(db, sc["id"]):
            db.execute("INSERT INTO review (id,scorecard_id,recording_id,reviewer_email,status,created_at,updated_at)"
                       " VALUES (?,?,?,?,'draft',?,?)", (core.new_id(), sc["id"], sc["recording_id"], u["email"], now, now))
        sets = "".join("%s=?, " % k for k in fields)
        if keep_reviewer:
            db.execute("UPDATE review SET %supdated_at=? WHERE scorecard_id=?" % sets,
                       list(fields.values()) + [now, sc["id"]])
        else:
            db.execute("UPDATE review SET %sreviewer_email=?, updated_at=? WHERE scorecard_id=?" % sets,
                       list(fields.values()) + [u["email"], now, sc["id"]])
        if qa is not False:
            db.execute("UPDATE recording SET qa_name=? WHERE id=?", (qa or None, sc["recording_id"]))
        return self._review_row(db, sc["id"])

    # The Audit tab's two sub-tabs and the narrower filter inside each. Anything else, including
    # no status at all, means "to audit": the tab never lists a call nobody started an audit on.
    # 'none' (never started) stays for scripts and tests; the UI does not offer it.
    REVIEW_STATUS_SQL = {"todo": "v.status IN ('queued','draft')", "queued": "v.status='queued'", "draft": "v.status='draft'",
                         "done": "v.status='submitted'", "submitted": "v.status='submitted'",
                         "audited": "v.status='submitted' AND v.coached_at IS NULL",
                         "coached": "v.status='submitted' AND v.coached_at IS NOT NULL", "none": "v.id IS NULL",
                         "disputed": "v.status='submitted' AND EXISTS (SELECT 1 FROM dispute d WHERE d.scorecard_id=s.id AND d.status='open')"}

    # ── notifications (0.51.0): shared events, this person's kinds and read marks ──
    def _notif_out(self, db, u, limit=30):
        items, unread, off = notify.feed(db, u, limit)
        return {"items": [to_public("notification", r) for r in items], "unread": unread,
                "kinds": notify.kinds_for(off), "failing_rule": notify.failing_rule(self._target())}

    def api_notifications(self, db, u, q):
        try:
            limit = int(self.one(q, "limit", "30"))
        except ValueError:
            return self.fail(400, "limit must be a number.")
        return self.json(self._notif_out(db, u, limit))

    def notifications_read(self, db, u):
        """{ids: [...]} or {all: true}; answers with the feed as it now stands."""
        d = self.body_json()
        if d.get("__too_large__"):
            return self.fail(413, "Request too large.")
        ids = d.get("ids")
        if not d.get("all") and not isinstance(ids, list):
            return self.fail(400, "Send ids (a list) or all: true.")
        notify.mark_read(db, u["id"], ids=ids if isinstance(ids, list) else None, all_=bool(d.get("all")))
        return self.json(self._notif_out(db, u))

    def notifications_prefs(self, db, u):
        """{off: [kind, ...]} -- the kinds this person mutes; unknown ids are refused by name."""
        d = self.body_json()
        if d.get("__too_large__"):
            return self.fail(413, "Request too large.")
        off = d.get("off")
        if not isinstance(off, list) or not all(isinstance(k, str) for k in off):
            return self.fail(400, "off must be a list of notification kinds.")
        try:
            notify.prefs_set(db, u["id"], off)
        except ValueError as e:
            return self.fail(400, str(e))
        core.audit(db, "notification.prefs", user=u, ip=self.client_ip(), detail=(",".join(sorted(set(off))) or "all on")[:160])
        return self.json(self._notif_out(db, u))

    def api_reviews(self, db, q):
        """Calls someone has started an audit on, with their review state; plus the two sub-tab counts."""
        try:
            page = max(1, int(self.one(q, "page", "1")))
        except ValueError:
            page = 1
        size = 50
        where, params = [self.REVIEW_ONE], []
        kind = self.one(q, "kind", "real")
        if kind in ("real", "demo", "test"):
            where.append("r.kind=?")
            params.append(kind)
        status = self.one(q, "status")
        if status not in self.REVIEW_STATUS_SQL:
            status = "todo"
        for col, key in (("agent_name", "agent"), ("qa_name", "qa")):
            val = self.one(q, key)
            if val:
                where.append("r.%s=? COLLATE NOCASE" % col)
                params.append(val)
        text = self.one(q, "q")
        if text:
            like = "%" + text + "%"
            where.append("(r.filename LIKE ? OR r.agent_name LIKE ? OR r.call_ref LIKE ? OR r.qa_name LIKE ?"
                         " OR r.caller_company LIKE ? OR r.caller_name LIKE ? OR r.caller_email LIKE ?"
                         " OR r.audit_name LIKE ?)")
            params += [like] * 8
        recs = _id_list(self.one(q, "recording"))
        if recs:                                 # Focus (0.53.0/0.55.0)
            where.append("r.id IN (%s)" % ",".join("?" * len(recs)))
            params += recs
        # sub-tab badges: same call filters, before the status narrows them
        counts = {}
        for key in ("todo", "done"):
            counts[key] = db.execute("SELECT COUNT(*) c FROM (%s)" % (self.REVIEW_SQL + " WHERE " + " AND ".join(where + [self.REVIEW_STATUS_SQL[key]])),
                                     params).fetchone()["c"]
        where.append(self.REVIEW_STATUS_SQL[status])
        base = self.REVIEW_SQL + " WHERE " + " AND ".join(where)
        total = db.execute("SELECT COUNT(*) c FROM (%s)" % base, params).fetchone()["c"]
        order = ("v.submitted_at DESC, " if status in ("done", "submitted", "audited", "coached", "disputed") else "") + "r.uploaded_at DESC, s.created_at DESC"
        rows = db.execute(base + " ORDER BY " + order + " LIMIT ? OFFSET ?",
                          params + [size, (page - 1) * size]).fetchall()
        out = []
        for sc in rows:
            rec = db.execute("SELECT * FROM recording WHERE id=?", (sc["recording_id"],)).fetchone()
            v = db.execute("SELECT v.version_no, r.name FROM rubric_version v JOIN rubric r ON r.id=v.rubric_id"
                           " WHERE v.id=?", (sc["rubric_version_id"],)).fetchone()
            rv = self._review_row(db, sc["id"])
            nd = db.execute("SELECT COUNT(*) c FROM dispute WHERE scorecard_id=? AND status='open'", (sc["id"],)).fetchone()["c"]
            out.append(to_public("scorecard", sc, items=[], transcript=None,
                                 recording=to_public("recording", rec) if rec else None,
                                 rubric_name=v["name"] if v else None, version_no=v["version_no"] if v else None,
                                 review=to_public("review", rv) if rv else None, open_disputes=nd,
                                 stage=review_stage("done", rv["status"] if rv else None, rv["coached_at"] if rv else None)))
        return self.json({"reviews": out, "total": total, "page": page, "pages": max(1, (total + size - 1) // size), "counts": counts})

    def review_get(self, db, u, sc_id):
        if not db.execute("SELECT 1 FROM scorecard WHERE id=?", (sc_id,)).fetchone():
            return self.fail(404, "Not found.")
        rv = self._review_row(db, sc_id)
        return self.json({"review": to_public("review", rv) if rv else None})

    def review_post(self, db, u, sc_id, act):
        sc = db.execute("SELECT * FROM scorecard WHERE id=?", (sc_id,)).fetchone()
        if not sc:
            return self.fail(404, "Not found.")
        d = self.body_json()
        if d.get("__too_large__"):
            return self.fail(413, "Request too large.")
        rv = self._review_row(db, sc_id)
        submitted = bool(rv and rv["status"] == "submitted")
        ip = self.client_ip()
        link_just = None
        if act in (None, "submit"):
            if submitted:
                return self.fail(409, "This review is submitted — reopen it first." if act is None
                                 else "This review is already submitted.")
            if act == "submit" and db.execute(
                    "SELECT 1 FROM review v JOIN scorecard s ON s.id=v.scorecard_id"
                    " WHERE s.recording_id=? AND v.status='submitted' AND v.scorecard_id<>?",
                    (sc["recording_id"], sc_id)).fetchone():
                return self.fail(409, "This call already has a submitted audit on another run — reopen it first.")
            fields, qa, problem = self._review_fields(db, d)
            if problem:
                return self.fail(400, problem)
            self._review_upsert(db, u, sc, fields, qa)
            db.execute("UPDATE review SET status='draft' WHERE scorecard_id=? AND status='queued'", (sc_id,))
            if act == "submit":
                # the final score is the scorecard after overrides, frozen now; _recompute() keeps
                # scorecard.overall_pct current, so this is a copy, not a calculation
                db.execute("UPDATE review SET status='submitted', final_pct=?, applicable_weight=?, auto_fail=?,"
                           " submitted_at=?, reviewer_email=?, updated_at=? WHERE scorecard_id=?",
                           (sc["overall_pct"] if (sc["applicable_weight"] or 0) > 0 else None,
                            sc["applicable_weight"], sc["auto_fail"], core.now(), u["email"],
                            core.now(), sc_id))
                core.audit(db, "review.submit", user=u, target=sc_id, ip=ip, detail="%.1f%%" % (sc["overall_pct"] or 0))
                # a submitted audit changes the value the Dashboard counts: re-check the agent's standing (0.51.0)
                rec_n = db.execute("SELECT * FROM recording WHERE id=?", (sc["recording_id"],)).fetchone()
                if rec_n and (rec_n["kind"] or "real") == "real" and rec_n["agent_name"]:
                    notify.check_failing(db, rec_n["agent_name"], "real", recording_id=sc["recording_id"], actor=u, target=self._target())
                # the agent's own copy, with their Dispute buttons (0.52.0); mail never fails the submit
                if rec_n:
                    try:
                        link_just = self._dispute_link_send(db, u, sc, rec_n, core.cfg())
                    except Exception as e:           # noqa: BLE001
                        log("dispute link: %r" % e)
                        link_just = {"ok": False, "why": "failed", "error": "Could not prepare the link."}
            else:
                core.audit(db, "review.save", user=u, target=sc_id, ip=ip)
        elif act == "reopen":
            if not submitted:
                return self.fail(409, "This review is not submitted.")
            db.execute("UPDATE review SET status='draft', final_pct=NULL, applicable_weight=NULL, auto_fail=NULL,"
                       " submitted_at=NULL, updated_at=? WHERE scorecard_id=?", (core.now(), sc_id))
            core.audit(db, "review.reopen", user=u, target=sc_id, ip=ip)
            self._dispute_link_revoke(db, u, sc_id, "review reopened")    # the agent's link showed a score that no longer stands
        elif act == "coached":
            # coaching is often delivered after the audit is submitted, so this is never locked
            coached = bool(d.get("coached"))
            self._review_upsert(db, u, sc, {}, False, keep_reviewer=submitted)
            db.execute("UPDATE review SET coached_at=?, updated_at=? WHERE scorecard_id=?",
                       (core.now() if coached else None, core.now(), sc_id))
            db.execute("UPDATE review SET status='draft' WHERE scorecard_id=? AND status='queued'", (sc_id,))
            core.audit(db, "review.coached", user=u, target=sc_id, ip=ip, detail="yes" if coached else "no")
        elif act == "queue":
            qa = None
            if d.get("qa_name"):
                _, qa, problem = self._review_fields(db, {"qa_name": d.get("qa_name")})
                if problem:
                    return self.fail(400, problem)
            problem = self._queue_one(db, u, sc, qa or None, ip)
            if problem:
                return self.fail(*problem)
        elif act == "unqueue":
            if not rv:
                return self.fail(409, "This call is not in the audit queue.")
            if rv["status"] != "queued":
                return self.fail(409, "Already in review — open it in Audit.")
            db.execute("DELETE FROM review WHERE scorecard_id=? AND status='queued'", (sc_id,))
            core.audit(db, "review.unqueue", user=u, target=sc_id, ip=ip)
        sc = db.execute("SELECT * FROM scorecard WHERE id=?", (sc_id,)).fetchone()
        return self.json(self._scorecard_full(db, sc, link_just))

    def _queue_one(self, db, u, sc, qa, ip):
        """Send one scorecard to audit. qa = canonical QA name or None (keep the recording's).
        Returns None when queued (or already in audit), else (status, message)."""
        if self._review_row(db, sc["id"]):
            return None
        other = db.execute("SELECT 1 FROM review v JOIN scorecard s ON s.id=v.scorecard_id"
                           " WHERE s.recording_id=? AND v.status='submitted'", (sc["recording_id"],)).fetchone()
        if other:
            return (409, "This call already has a submitted audit — reopen it first.")
        rec = db.execute("SELECT qa_name FROM recording WHERE id=?", (sc["recording_id"],)).fetchone()
        if not qa and not (rec and rec["qa_name"]):
            return (400, "Choose the QA of record to start this audit.")
        now = core.now()
        db.execute("INSERT INTO review (id,scorecard_id,recording_id,reviewer_email,status,created_at,updated_at)"
                   " VALUES (?,?,?,?,'queued',?,?)", (core.new_id(), sc["id"], sc["recording_id"], u["email"], now, now))
        if qa:
            db.execute("UPDATE recording SET qa_name=? WHERE id=?", (qa, sc["recording_id"]))
        core.audit(db, "review.queue", user=u, target=sc["id"], ip=ip, detail=qa or rec["qa_name"])
        return None

    def reviews_queue(self, db, u):
        """Bulk 'Send to audit' from the Calls list."""
        d = self.body_json()
        ids = d.get("scorecard_ids")
        if not isinstance(ids, list) or not ids:
            return self.fail(400, "scorecard_ids must be a non-empty list.")
        if len(ids) > 200:
            return self.fail(400, "At most 200 calls at a time.")
        qa = None
        if d.get("qa_name"):
            _, qa, problem = self._review_fields(db, {"qa_name": d.get("qa_name")})
            if problem:
                return self.fail(400, problem)
        ip = self.client_ip()
        queued, skipped = [], []
        for sc_id in ids:
            if not isinstance(sc_id, str) or not re.match("^%s$" % UUID, sc_id):
                skipped.append({"id": str(sc_id)[:40], "reason": "not a scorecard id"})
                continue
            sc = db.execute("SELECT s.* FROM scorecard s JOIN job j ON j.id=s.job_id WHERE s.id=? AND j.status='done'",
                            (sc_id,)).fetchone()
            if not sc:
                skipped.append({"id": sc_id, "reason": "not found or not scored"})
                continue
            if self._review_row(db, sc_id):
                skipped.append({"id": sc_id, "reason": "already in audit"})
                continue
            problem = self._queue_one(db, u, sc, qa or None, ip)
            if problem:
                skipped.append({"id": sc_id, "reason": problem[1]})
            else:
                queued.append(sc_id)
        return self.json({"queued": queued, "skipped": skipped})

    DETAIL_CAPS = (("agent_name", 80), ("qa_name", 80), ("caller_name", 120), ("caller_company", 120),
                   ("caller_email", 160), ("call_ref", 80), ("audit_name", 160),
                   ("caller_phone", 40), ("ticket_ref", 80))                       # 0.61.0

    def recording_details(self, db, u, rec_id):
        """Fix who took the call and who it was for, after upload. Key absent = unchanged,
        '' = clear. Names are validated only when they change, so an archived current
        name never blocks a save. Not a score, so a submitted review does not lock it --
        except the QA of record, which the Dashboard credits."""
        rec = db.execute("SELECT * FROM recording WHERE id=? AND deleted_at IS NULL", (rec_id,)).fetchone()
        if not rec:
            return self.fail(404, "Not found.")
        d = self.body_json()
        upd = {}
        for k, cap in self.DETAIL_CAPS:
            if k in d:
                v = d.get(k)
                upd[k] = (" ".join(str(v).split())[:cap] or None) if v is not None else None
        if upd.get("caller_email") and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", upd["caller_email"]):
            return self.fail(400, "Customer email does not look valid.")
        def changed(k):
            return k in upd and (upd[k] or "").lower() != (rec[k] or "").lower()
        for k, kind in (("agent_name", "agent"), ("qa_name", "qa")):
            if k in upd and not changed(k):
                upd.pop(k)                           # same name, maybe other casing: keep the stored spelling
            elif upd.get(k):
                row = person_lookup(db, kind, upd[k])
                if not row:
                    return self.fail(400, person_problem(db, kind, upd[k]))
                upd[k] = row["name"]
        if changed("qa_name") and db.execute("SELECT 1 FROM review v JOIN scorecard s ON s.id=v.scorecard_id"
                                             " WHERE s.recording_id=? AND v.status='submitted'", (rec_id,)).fetchone():
            return self.fail(409, "This call has a submitted audit; reopen it before changing the QA of record.")
        new = dict(rec)
        new.update(upd)
        day = (rec["uploaded_at"] or "")[:10]
        old_auto = audit_name_for(rec["agent_name"], rec["caller_company"], rec["caller_name"], day)
        if not new.get("audit_name") or ("audit_name" not in upd and (rec["audit_name"] or "") == old_auto):
            upd["audit_name"] = audit_name_for(new.get("agent_name"), new.get("caller_company"), new.get("caller_name"), day) or None
        diff = ["%s: %s -> %s" % (k, rec[k] or "-", v or "-") for k, v in upd.items() if (rec[k] or "") != (v or "")]
        if diff:
            db.execute("UPDATE recording SET %s WHERE id=?" % ", ".join("%s=?" % k for k in upd),
                       list(upd.values()) + [rec_id])
            core.audit(db, "recording.details", user=u, target=rec_id, ip=self.client_ip(), detail="; ".join(diff)[:300])
        rec = db.execute("SELECT * FROM recording WHERE id=?", (rec_id,)).fetchone()
        return self.json({"recording": to_public("recording", rec)})

    # ── dashboard ─────────────────────────────────────────────────────────
    DASH_SQL = dashboard.ROWS_SQL      # owned by dashboard.py since 0.51.0: notify.check_failing runs the same query

    # one row per recording with the Flow's milestones, for the Dashboard's pipeline strip
    PIPE_SQL = ("SELECT r.id, r.uploaded_at, r.agent_name, r.qa_name,"
                " EXISTS(SELECT 1 FROM transcript t WHERE t.recording_id=r.id) transcribed,"
                " EXISTS(SELECT 1 FROM scorecard s JOIN job j ON j.id=s.job_id AND j.status='done' WHERE s.recording_id=r.id) scored,"
                " EXISTS(SELECT 1 FROM review v JOIN scorecard s2 ON s2.id=v.scorecard_id WHERE s2.recording_id=r.id AND v.status='submitted') audited,"
                " EXISTS(SELECT 1 FROM review v2 JOIN scorecard s3 ON s3.id=v2.scorecard_id WHERE s3.recording_id=r.id AND v2.coached_at IS NOT NULL) coached"
                " FROM recording r WHERE r.deleted_at IS NULL%s")

    # every scored criterion, for the executive "where calls lose points" table (filtered to the picked cards in Python)
    ITEMS_DASH_SQL = ("SELECT i.scorecard_id sc_id, c.key, c.name, i.weight, i.rating, COALESCE(i.override_score, i.score) final"
                      " FROM scorecard_item i JOIN criterion c ON c.id=i.criterion_id")

    def _dashboard_data(self, db, q):
        """(payload, None) or (None, (status, message)) -- shared by the JSON route and the PDF export."""
        period = self.one(q, "period", "week")
        if period not in dashboard.PERIODS:
            return None, (400, "period must be one of %s." % ", ".join(dashboard.PERIODS))
        kind = self.one(q, "kind", "real")          # real by default: demo/test never leak into real totals
        if kind not in ("real", "demo", "test", "all"):
            return None, (400, "kind must be real, demo, test or all.")
        source = self.one(q, "source", "scored")
        if source not in dashboard.SOURCES:
            return None, (400, "source must be scored or audited.")
        tz = dashboard.clamp_tz(self.one(q, "tz", "0"))
        today = dashboard.local_dt(core.now(), tz).date()
        d_from, d_to = dashboard.default_range(period, today)
        try:
            if self.one(q, "from"):
                d_from = dashboard.parse_date(self.one(q, "from"))
            if self.one(q, "to"):
                d_to = dashboard.parse_date(self.one(q, "to"))
        except ValueError as e:
            return None, (400, str(e))
        kf, kp = ("" if kind == "all" else " AND r.kind=?"), (() if kind == "all" else (kind,))
        rows = [dict(r) for r in db.execute(self.DASH_SQL % kf, kp).fetchall()]
        items = [dict(r) for r in db.execute(self.ITEMS_DASH_SQL).fetchall()]
        disputes = [dict(r) for r in db.execute("SELECT scorecard_id sc_id, status FROM dispute").fetchall()]
        agent = self.one(q, "agent")
        sessions = [dict(r) for r in db.execute("SELECT agent_name, status, scheduled_at, done_at FROM coaching_session"
                                                + (" WHERE agent_name=? COLLATE NOCASE" if agent else ""), ((agent,) if agent else ())).fetchall()]
        target = dashboard.target_of(core.cfg().get("AUDITLY_TARGET_PCT"))
        try:
            out = dashboard.aggregate(rows, period, d_from, d_to, tz=tz, source=source, agent=agent, qa=self.one(q, "qa"),
                                      items=items, disputes=disputes, sessions=sessions, target=target, now_iso=core.now())
        except ValueError as e:
            return None, (400, str(e))
        out["calls_kind"] = kind
        out["reason_labels"] = dict(score_mod.REASON_LABELS)
        prow = [dict(r) for r in db.execute(self.PIPE_SQL % kf, kp).fetchall()]
        out["pipeline"] = dashboard.pipeline(prow, d_from, d_to, tz=tz, agent=agent, qa=self.one(q, "qa"))
        return to_public("dashboard", None, **out), None

    def api_dashboard(self, db, q):
        out, problem = self._dashboard_data(db, q)
        if problem:
            return self.fail(*problem)
        return self.json(out)

    def dashboard_pdf(self, db, u, q):
        """The executive summary as a one-page PDF, same filters as the Dashboard."""
        d, problem = self._dashboard_data(db, q)
        if problem:
            return self.fail(*problem)
        core.audit(db, "dashboard.export.pdf", user=u, ip=self.client_ip(), detail="%s..%s %s" % (d["from"], d["to"], d["kind"]))
        k, t, dl, pv = d["kpis"] or {}, d["totals"] or {}, d.get("deltas") or {}, d.get("previous") or {}

        def pc(v):
            return "n/a" if v is None else "%.1f%%" % v

        def dv(v, unit="%"):
            if v is None:
                return ""
            return "  (%s%s%s vs %s..%s)" % ("+" if v > 0 else "", ("%.1f" % v) if isinstance(v, float) else v, unit if isinstance(v, float) else "", pv.get("from", "?"), pv.get("to", "?"))
        p = pdfgen.Pdf("Auditly executive summary %s to %s" % (d["from"], d["to"]))
        scope = d.get("scope") or {}
        p.text("QA executive summary", 16, bold=True, gap_after=2)
        p.text("%s to %s  ·  %s calls  ·  %s  ·  %s%s" % (d["from"], d["to"], {"real": "real", "demo": "demo", "test": "test", "all": "all"}.get(d["kind"], d["kind"]),
                                                       "audited only" if d["source"] == "audited" else "all scored",
                                                       ("agent " + scope["agent"]) if scope.get("agent") else ("QA " + scope["qa"]) if scope.get("qa") else "site-wide",
                                                       "  ·  target %d%%" % k.get("target", 90)), 9.5, color=(0.35, 0.35, 0.35), gap_after=8)
        p.text("Headline", 12, bold=True, gap_after=3)
        for label, val in (("Calls scored", "%d%s" % (t.get("calls", 0), dv(dl.get("calls"), ""))),
                           ("Average QA score", pc(k.get("avg_pct")) + dv(dl.get("avg_pct"))),
                           ("Pass rate (at or above target)", pc(k.get("pass_rate")) + dv(dl.get("pass_rate"))),
                           ("Critical checks passed", pc(None if k.get("auto_fail_rate") is None else round(100 - k["auto_fail_rate"], 1))
                            + dv(None if dl.get("auto_fail_rate") is None else -dl["auto_fail_rate"])),
                           ("Audit coverage (scored calls with a final score)", pc(k.get("audit_coverage")) + dv(dl.get("audit_coverage"))),
                           ("Coaching coverage (audited calls coached)", pc(k.get("coaching_coverage")) + dv(dl.get("coaching_coverage")))):
            p.text("%s: %s" % (label, val), 10, gap_after=1)
        p.space(6)
        b = d.get("bands") or {}
        p.text("Score bands: under 60: %d  ·  60-79: %d  ·  80-89: %d  ·  90+: %d" % (b.get("lt60", 0), b.get("60_79", 0), b.get("80_89", 0), b.get("ge90", 0)), 10, gap_after=2)
        sh = d.get("sentiment_shift") or {}
        p.text("Customer sentiment start to end: improved %d  ·  same %d  ·  worsened %d  ·  not classified %d"
               % (sh.get("improved", 0), sh.get("same", 0), sh.get("worsened", 0), sh.get("unclassified", 0)), 10, gap_after=8)
        p.rule()
        p.text("Where calls earn points (criteria, strongest first)", 12, bold=True, gap_after=3)
        for c in sorted((d.get("by_criterion") or []), key=lambda x: -(x.get("attainment_pct") or 0))[:8]:
            p.text("%s: %s attainment  ·  met %d / partial %d / missed %d%s  ·  %s"
                   % (c["name"], pc(c.get("attainment_pct")), c["met"], c["partial"], c["missed"], ("  /  n/a %d" % c["na"]) if c.get("na") else "",
                      ("%.1f points per call to gain" % c["lost_points"]) if c.get("lost_points") else "full marks"),
                   9.5, gap_after=1)
        if not d.get("by_criterion"):
            p.text("No scored criteria in this range.", 9.5, color=(0.35, 0.35, 0.35))
        p.space(6)
        p.rule()
        ld = d.get("leaders") or {}
        p.text("Agents (at least %d calls)" % ld.get("min_n", 3), 12, bold=True, gap_after=3)
        for title, lst in (("Leading", ld.get("top") or []), ("Most room to grow", ld.get("bottom") or [])):
            if lst:
                p.text(title, 10.5, bold=True, gap_after=1)
                for a in lst:
                    p.text("%s: %s over %d calls%s" % (a["name"], pc(a.get("avg")), a.get("n", 0), ("  (%s%.1f)" % ("+" if a["delta"] > 0 else "", a["delta"])) if a.get("delta") is not None else ""), 9.5, indent=10, gap_after=1)
        if not (ld.get("top") or ld.get("bottom")):
            p.text("Not enough calls per agent yet.", 9.5, color=(0.35, 0.35, 0.35))
        p.space(6)
        p.rule()
        ds, co = d.get("disputes") or {}, d.get("coaching") or {}
        p.text("Disputes and coaching", 12, bold=True, gap_after=3)
        p.text("Disputes: %d open  ·  %d upheld  ·  %d rejected  ·  upheld rate %s" % (ds.get("open", 0), ds.get("upheld", 0), ds.get("rejected", 0), pc(ds.get("upheld_rate"))), 10, gap_after=1)
        p.text("Coaching: %d audited calls awaiting coaching  ·  %d sessions scheduled  ·  %d planned  ·  %d delivered  ·  %s days audit to coaching (avg)"
               % (co.get("awaiting_coaching", 0), co.get("sessions_upcoming", 0), co.get("sessions_planned", 0), co.get("sessions_done", 0),
                  ("%.1f" % co["avg_days_audit_to_coached"]) if co.get("avg_days_audit_to_coached") is not None else "n/a"), 10, gap_after=1)
        name = "auditly-executive-%s-%s" % (d["from"], d["to"])
        return self.send(200, p.build(), "application/pdf", {"Content-Disposition": 'attachment; filename="%s.pdf"' % name})

    # ── export ────────────────────────────────────────────────────────────
    def export(self, db, u, sc_id, fmt):
        sc = db.execute("SELECT * FROM scorecard WHERE id=?", (sc_id,)).fetchone()
        if not sc:
            return self.fail(404, "Not found.")
        full = self._scorecard_full(db, sc)
        core.audit(db, "scorecard.export." + fmt, user=u, target=sc_id, ip=self.client_ip())
        rec = full.get("recording") or {}
        stamp = (rec.get("uploaded_at") or "")[:10] or "call"
        tail = rec.get("audit_name") or ""
        if not tail or tail == stamp:                 # audit name is just the date: avoid "date-date"
            tail = rec.get("call_ref") or sc_id[:8]
        name = "auditly-scorecard-%s-%s" % (stamp, slug(tail))

        if fmt == "json":
            return self.send(200, json.dumps(full, indent=2, default=str), "application/json; charset=utf-8",
                             {"Content-Disposition": 'attachment; filename="%s.json"' % name})
        if fmt == "csv":
            buf = io.StringIO()
            w = csv.writer(buf)
            for label, key in (("Audit name", "audit_name"), ("File", "filename"), ("Agent", "agent_name"),
                               ("QA reviewer", "qa_name"), ("Customer name", "caller_name"),
                               ("Customer company", "caller_company"), ("Customer email", "caller_email"),
                               ("Call ID", "call_ref"), ("Uploaded", "uploaded_at"), ("Kind", "kind")):
                w.writerow([label, rec.get(key) or ""])
            cust = full.get("customer") or {}
            w.writerow(["Customer phone", rec.get("caller_phone") or cust.get("phone") or ""])
            w.writerow(["Ticket number", rec.get("ticket_ref") or cust.get("reference") or ""])
            cf = full.get("call_facts") or {}
            for k in score_mod.FACT_IDS:                                   # the facts as heard (0.61.0)
                if (cf.get("identifiers") or {}).get(k):
                    w.writerow(["Fact: " + score_mod.FACT_LABELS[k], cf["identifiers"][k]])
            if cf.get("troubleshooting"):
                w.writerow(["Troubleshooting", " | ".join("%d. %s%s" % (i + 1, s.get("text"), (" -- " + s["result"]) if s.get("result") else "")
                                                          for i, s in enumerate(cf["troubleshooting"]))])
            if cf.get("discussed"):
                w.writerow(["Discussed", " | ".join(s.get("text") or "" for s in cf["discussed"])])
            if cf.get("outcome"):
                w.writerow(["Outcome", cf["outcome"]])
            w.writerow(["QA type", "%s v%s" % (full.get("rubric_name") or "", full.get("version_no") or "")])
            w.writerow(["Speakers", speakers_text(full.get("speakers")) + ("  ·  transferred call" if full.get("transferred") else "")])
            w.writerow(["Call summary", full.get("call_summary") or ""])
            w.writerow(["Customer sentiment", sentiment_text(full.get("sentiment"))])
            w.writerow(["Call reasons", " | ".join("%s: %s" % (x.get("label"), x.get("detail") or "")
                                                   for x in full.get("call_reasons") or [])])
            for label, val in tone_lines(full.get("tone")):
                w.writerow(["Delivery: " + label, val])
            w.writerow([])
            w.writerow(["Criterion", "Weight", "Rating", "AI score", "Final score", "Critical", "Rationale",
                        "Override note", "Evidence"])
            for it in full["items"]:
                w.writerow([it["name"], it["weight"], it["rating"], it["score"], it["final_score"],
                            "yes" if it["critical"] else "", it["rationale"] or "", it["override_note"] or "",
                            " | ".join("[%s] %s" % (e.get("ts"), e.get("quote")) for e in it["evidence"])])
            w.writerow([])
            w.writerow(["Overall %", full["overall_pct"] if full["overall_pct"] is not None else "n/a",
                        "Auto-fail" if full["auto_fail"] else ""])
            rv = full.get("review") or {}
            w.writerow(["Final QA score", rv.get("final_pct") if rv.get("status") == "submitted" else ""])
            w.writerow(["Review status", rv.get("status") or "not started"])
            w.writerow(["Reviewed by", rv.get("reviewer_email") or ""])
            w.writerow(["Submitted", rv.get("submitted_at") or ""])
            w.writerow(["Coaching notes", rv.get("coaching_notes") or ""])
            w.writerow(["Resolution", rv.get("resolution_notes") or ""])
            w.writerow(["Action plan", rv.get("action_plan") or ""])
            w.writerow(["Follow-up", rv.get("follow_up_on") or ""])
            w.writerow(["Coaching delivered", rv.get("coached_at") or ""])
            w.writerow(["Summary", full["summary"]])
            for o in full["opportunities"]:
                w.writerow(["Opportunity", o.get("text")])
            for m in full["misses"]:
                w.writerow(["Miss", m.get("text")])
            return self.send(200, buf.getvalue().encode("utf-8"), "text/csv; charset=utf-8",
                             {"Content-Disposition": 'attachment; filename="%s.csv"' % name})

        p = pdfgen.Pdf("Auditly scorecard")
        p.text("AUDITLY", 9, bold=True, color=(0.45, 0.45, 0.45), gap_after=2)
        p.text(rec.get("audit_name") or "Call scorecard", 18, bold=True, gap_after=4)
        caller = ", ".join(x for x in (rec.get("caller_name"), rec.get("caller_company"), rec.get("caller_email")) if x) or "-"
        tag = {"demo": "DEMO RUN  ·  ", "test": "TEST CALL  ·  "}.get(rec.get("kind") or "real", "")
        p.text("%s%s  ·  agent %s  ·  QA %s  ·  customer %s  ·  call ID %s  ·  %s"
               % (tag, rec.get("filename") or "-", rec.get("agent_name") or "-", rec.get("qa_name") or "-",
                  caller, rec.get("call_ref") or "-", fmt_when(rec.get("uploaded_at"))),
               9.5, color=(0.35, 0.35, 0.35), gap_after=2)
        p.text("QA type: %s v%s  ·  scored by %s  ·  %s" % (full.get("rubric_name"), full.get("version_no"),
                                                          full.get("model") or "-", fmt_when(full.get("created_at"))),
               9.5, color=(0.35, 0.35, 0.35), gap_after=6)
        p.rule()
        headline = ("Overall: %.1f%%" % full["overall_pct"]) if full["overall_pct"] is not None \
            else "Overall: n/a (no criterion applied)"
        if full["auto_fail"]:
            names = [it["name"] for it in full["items"] if it["critical"]
                     and (int(it["override_score"]) == 0 if it["override_score"] is not None else it["rating"] == "missed")]
            headline += "   AUTO-FAIL (critical: %s)" % (", ".join(names) or "criterion missed")
        p.text(headline, 15, bold=True, gap_after=4)
        if full.get("call_summary"):
            p.text("Call summary", 11, bold=True, gap_after=2)
            p.text(full["call_summary"], 10, gap_after=4)
        cf = full.get("call_facts") or {}
        facts = [(score_mod.FACT_LABELS[k], (cf.get("identifiers") or {}).get(k)) for k in score_mod.FACT_IDS if (cf.get("identifiers") or {}).get(k)]
        if facts or cf.get("troubleshooting"):
            p.text("Call details (as heard)", 11, bold=True, gap_after=2)
            if facts:
                p.text("  ·  ".join("%s %s" % f for f in facts), 9.5, color=(0.3, 0.3, 0.3), gap_after=2)
            for i, s in enumerate(cf.get("troubleshooting") or []):
                p.text("%d. %s%s" % (i + 1, s.get("text") or "", (" -- " + s["result"]) if s.get("result") else ""), 9.5, color=(0.3, 0.3, 0.3), gap_after=1)
            if cf.get("outcome"):
                p.text("Outcome: %s" % cf["outcome"], 9.5, color=(0.3, 0.3, 0.3), gap_after=1)
            p.space(4)
        if full.get("speakers") or full.get("transferred"):
            p.text("Speakers: %s%s" % (speakers_text(full.get("speakers")), "  ·  transferred call" if full.get("transferred") else ""),
                   9.5, color=(0.3, 0.3, 0.3), gap_after=2)
        if full.get("sentiment") or full.get("call_reasons"):
            p.text("Customer sentiment: %s" % sentiment_text(full.get("sentiment")), 9.5, color=(0.3, 0.3, 0.3), gap_after=1)
            for x in full.get("call_reasons") or []:
                p.text("Reason: %s - %s" % (x.get("label"), x.get("detail") or ""), 9.5, color=(0.3, 0.3, 0.3), gap_after=1)
            p.space(5)
        tl = tone_lines(full.get("tone"))
        if tl:
            p.text("Tone & delivery (from the timings; for coaching, not scored)", 11, bold=True, gap_after=2)
            p.text("  ·  ".join("%s %s" % (k, v) for k, v in tl), 9.5, color=(0.3, 0.3, 0.3), gap_after=6)
        if full.get("summary"):
            p.text("QA summary", 11, bold=True, gap_after=2)
            p.text(full["summary"], 10, gap_after=8)
        rv = full.get("review")
        if rv:
            p.rule()
            if rv.get("status") == "submitted":
                p.text("Final QA score: %s   submitted %s by %s" % (("%.1f%%" % rv["final_pct"]) if rv.get("final_pct") is not None else "n/a",
                                                                        fmt_when(rv.get("submitted_at")),
                                                                        rv.get("reviewer_email") or "-"), 13, bold=True, gap_after=4)
            else:
                p.text("Queued for QA audit - score not yet final" if rv.get("status") == "queued"
                       else "QA review in progress (draft) - score not yet final", 11, bold=True, gap_after=4)
            for title, key in (("Coaching notes", "coaching_notes"), ("Resolution", "resolution_notes"),
                               ("Action plan", "action_plan")):
                if rv.get(key):
                    p.text(title, 10.5, bold=True, gap_after=1)
                    p.text(rv[key], 10, gap_after=4)
            extra = []
            if rv.get("follow_up_on"):
                extra.append("follow-up %s" % rv["follow_up_on"])
            extra.append("coaching delivered %s" % fmt_when(rv["coached_at"]) if rv.get("coached_at") else "coaching not yet delivered")
            p.text("  ·  ".join(extra), 9.5, color=(0.35, 0.35, 0.35), gap_after=6)
        p.rule()
        p.text("Criteria", 11, bold=True, gap_after=3)
        for it in full["items"]:
            flag = "  [override: %s]" % it["override_note"] if it["override_score"] is not None else ""
            p.text("%s  -  %s/%s  (%s)%s%s" % (it["name"], it["final_score"], it["weight"], it["rating"],
                                                "  CRITICAL" if it["critical"] else "", flag),
                   10, bold=True, gap_after=1)
            if it["rationale"]:
                p.text(it["rationale"], 9.5, indent=10, color=(0.25, 0.25, 0.25))
            for e in it["evidence"]:
                p.text("[%s] \"%s\"" % (e.get("ts"), e.get("quote")), 9, indent=18, color=(0.4, 0.4, 0.4))
            p.space(5)
        p.rule()
        for title, lst, key in (("Strengths", full["strengths"], None),
                                ("Opportunities", full["opportunities"], "text"),
                                ("Misses", full["misses"], "text")):
            if lst:
                p.text(title, 11, bold=True, gap_after=3)
                for x in lst:
                    p.text("- " + (x.get(key) if key else x), 10, indent=6)
                p.space(6)
        return self.send(200, p.build(), "application/pdf",
                         {"Content-Disposition": 'attachment; filename="%s.pdf"' % name})

    # ── admin ─────────────────────────────────────────────────────────────
    def admin_get(self, db, u, path, q):
        if path == "/api/admin/ask":
            return self.api_admin_ask_get(db, u)
        if path == "/api/admin/users":
            rows = db.execute("SELECT * FROM user ORDER BY role, email")
            return self.json({"users": [to_public("user", r) for r in rows]})
        if path == "/api/admin/audit":
            rows = db.execute("SELECT * FROM audit ORDER BY at DESC, id DESC LIMIT 300")
            return self.json({"audit": [to_public("audit", r) for r in rows]})
        if path == "/api/admin/demo-data":
            import demo_data
            return self.json({"count": demo_data.history_count(db)})
        return self.fail(404, "Not found.")

    def admin_post(self, db, u, path):
        d = self.body_json()
        ip = self.client_ip()
        if path == "/api/admin/demo-data/load":
            import demo_data
            n = demo_data.history_count(db)
            if n:
                return self.fail(409, "Demo data is already loaded (%d calls). Remove it first." % n)
            ver = db.execute("SELECT v.id FROM rubric_version v JOIN rubric r ON r.id=v.rubric_id WHERE r.archived=0"
                             " ORDER BY v.created_at DESC LIMIT 1").fetchone()
            if not ver:
                return self.fail(400, "Create a QA type first (Settings › QA types) or run --seed-rubric.")
            db.execute("BEGIN IMMEDIATE")            # ~1,500 inserts: one commit, and no half-load survives a failure
            try:
                demo_data._seed_people(db, u["email"])
                n = demo_data.seed_history(db, core.cfg(), "demo", ver["id"], u["email"])
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
            core.audit(db, "demo.load", user=u, ip=ip, detail="%d calls" % n)
            return self.json({"loaded": n})
        if path == "/api/admin/demo-data/remove":
            import demo_data
            db.execute("BEGIN IMMEDIATE")
            try:
                n = demo_data.remove_history(db)
                for name in demo_data.HISTORY_QA:      # the extra QA name goes too, unless a real call took it
                    p = db.execute("SELECT * FROM person WHERE kind='qa' AND name=? COLLATE NOCASE", (name,)).fetchone()
                    if p and not self._person_uses(db, p):
                        db.execute("DELETE FROM person WHERE id=?", (p["id"],))
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
            core.audit(db, "demo.remove", user=u, ip=ip, detail="%d calls" % n)
            return self.json({"removed": n})
        if path == "/api/admin/users":
            email = (d.get("email") or "").strip().lower()
            role = d.get("role") or "reviewer"
            pw = d.get("password") or ""
            if "@" not in email:
                return self.fail(400, "A valid email address is required.")
            if role not in ("admin", "reviewer"):
                return self.fail(400, "Unknown role.")
            problem = core.password_problem(pw)
            if problem:
                return self.fail(400, problem)
            if db.execute("SELECT 1 FROM user WHERE email=? COLLATE NOCASE", (email,)).fetchone():
                return self.fail(409, "That email already has an account.")
            h, s = core.hash_password(pw)
            uid = core.new_id()
            db.execute("INSERT INTO user (id,email,name,pw_hash,pw_salt,role,active,created_at)"
                       " VALUES (?,?,?,?,?,?,1,?)",
                       (uid, email, (d.get("name") or "").strip() or None, h, s, role, core.now()))
            core.audit(db, "user.create", user=u, target=email, ip=ip, detail=role)
            return self.json({"id": uid, "email": email}, 201)
        m = re.match(r"^/api/admin/users/(%s)/(disable|enable|password)$" % UUID, path)
        if m:
            uid, act = m.group(1), m.group(2)
            t = db.execute("SELECT * FROM user WHERE id=?", (uid,)).fetchone()
            if not t:
                return self.fail(404, "Not found.")
            if act in ("disable", "enable"):
                if act == "disable" and t["id"] == u["id"]:
                    return self.fail(400, "You cannot disable your own account.")
                db.execute("UPDATE user SET active=? WHERE id=?", (0 if act == "disable" else 1, uid))
                if act == "disable":
                    db.execute("DELETE FROM session WHERE user_id=?", (uid,))
                core.audit(db, "user." + act, user=u, target=t["email"], ip=ip)
                return self.json({"ok": True})
            pw = d.get("password") or ""
            problem = core.password_problem(pw)
            if problem:
                return self.fail(400, problem)
            h, s = core.hash_password(pw)
            db.execute("UPDATE user SET pw_hash=?, pw_salt=? WHERE id=?", (h, s, uid))
            db.execute("DELETE FROM session WHERE user_id=?", (uid,))
            core.audit(db, "user.password_reset", user=u, target=t["email"], ip=ip)
            return self.json({"ok": True})
        return self.fail(404, "Not found.")

    # ── the app ───────────────────────────────────────────────────────────
    # Vendored third-party code, served same-origin and loaded with the page nonce (invariant 6: nothing
    # external). Cached as immutable, so a newer Mermaid must ship under a new file name.
    STATIC = {"mermaid.min.js": "application/javascript; charset=utf-8",
              "coeo_light.svg": "image/svg+xml", "coeo_dark.svg": "image/svg+xml",           # the COEO logo, light/dark
              # the Auditly brand: built-in SVGs; a PNG of the real artwork dropped in static/ under the
              # same stem (auditly_light.png ...) is preferred when present (see _brand_urls)
              "auditly_light.svg": "image/svg+xml", "auditly_dark.svg": "image/svg+xml",
              # the same wordmark without the stamp animation, for a visitor who asked for reduced motion
              "auditly_light_still.svg": "image/svg+xml", "auditly_dark_still.svg": "image/svg+xml",
              "auditly_icon_light.svg": "image/svg+xml", "auditly_icon_dark.svg": "image/svg+xml",
              # the square mark on the Ask Auditly launcher, lower right. The suffix is the theme the
              # file is shown in, as above — the launcher inverts (navy button in light, cream in dark),
              # so auditly_mark_light.svg carries the cream ink. _still: same, without the stamp.
              "auditly_mark_light.svg": "image/svg+xml", "auditly_mark_dark.svg": "image/svg+xml",
              "auditly_mark_light_still.svg": "image/svg+xml", "auditly_mark_dark_still.svg": "image/svg+xml",
              "auditly_light.png": "image/png", "auditly_dark.png": "image/png",
              "auditly_icon_light.png": "image/png", "auditly_icon_dark.png": "image/png"}

    # ── branding (white label) ──────────────────────────────────────────
    BRAND_SLOTS = ("logo_light", "logo_dark", "icon_light", "icon_dark")
    BRAND_MAX_BYTES = 512 * 1024
    BRAND_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "svg": "image/svg+xml",
                  "webp": "image/webp", "gif": "image/gif", "ico": "image/x-icon"}

    def _branding_row(self, db):
        return db.execute("SELECT * FROM branding WHERE id=1").fetchone()

    def _brand_urls(self, db):
        """Per slot: the uploaded asset, else the real PNG artwork in static/ when present, else the
        built-in SVG. The two logo slots also follow logo_mode (coeo | hidden)."""
        row = self._branding_row(db)
        mode = (row["logo_mode"] if row else None) or "auditly"
        # The built-in files keep their names from release to release but are served immutable for a
        # year, so a browser that fetched an older build would go on drawing it however many times the
        # artwork changed (0.43.1: the stand-in wordmark outlived its replacement on real machines).
        # The build version in the query is what retires the old copy — uploaded assets already do this
        # with their updated_at below.
        ver = core.app_version()
        custom = {r["slot"]: r for r in db.execute("SELECT slot, name, bytes, mime, updated_at FROM brand_asset")}
        urls, hint = {}, []
        for slot in self.BRAND_SLOTS:
            kind, theme = slot.split("_")
            stem = "auditly_%s%s" % ("icon_" if kind == "icon" else "", theme)
            if slot in custom and (kind == "icon" or mode == "custom"):
                urls[slot] = "/api/branding/asset/%s?v=%s" % (slot, re.sub(r"[^0-9]", "", custom[slot]["updated_at"] or ""))
            elif kind == "logo" and mode == "hidden":
                urls[slot] = None
            elif kind == "logo" and mode == "coeo":
                urls[slot] = "/static/coeo_%s.svg?v=%s" % (theme, ver)
            elif os.path.exists(os.path.join(core.HERE, "static", stem + ".png")):
                urls[slot] = "/static/%s.png?v=%s" % (stem, ver)
            else:
                urls[slot] = "/static/%s.svg?v=%s" % (stem, ver)
                hint.append(stem + ".png")
        return mode, urls, {k: {"name": v["name"], "bytes": v["bytes"], "updated_at": v["updated_at"]} for k, v in custom.items()}, hint

    def _branding_out(self, db):
        row = self._branding_row(db)
        mode, urls, custom, hint = self._brand_urls(db)
        return to_public("branding", row or {"logo_mode": mode},
                         logos={"light": urls["logo_light"], "dark": urls["logo_dark"]},
                         icons={"light": urls["icon_light"], "dark": urls["icon_dark"]}, custom=custom, png_hint=hint)

    def brand_asset(self, db, slot, favicon=False):
        """The bytes behind a brand URL: an uploaded blob, else the static file the URL map points at."""
        row = db.execute("SELECT blob, mime FROM brand_asset WHERE slot=?", (slot,)).fetchone()
        if row:
            body, mime = bytes(row["blob"]), row["mime"]
        else:
            if not favicon:
                return self.fail(404, "Not found.")
            _, urls, _, _ = self._brand_urls(db)
            name = (urls[slot] or "").rsplit("/", 1)[-1].partition("?")[0]   # the URL carries ?v=<build>
            try:
                with open(os.path.join(core.HERE, "static", name), "rb") as f:
                    body = f.read()
            except OSError:
                return self.fail(404, "Not found.")
            mime = self.STATIC.get(name, "application/octet-stream")
        extra = {"Cache-Control": "public, max-age=3600" if favicon else "public, max-age=31536000, immutable"}
        if mime == "image/svg+xml":
            extra["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'"   # an SVG opened directly runs no script
        return self.send(200, body, mime, extra, nostore=False)

    def branding_mode(self, db, u):
        d = self.body_json()
        mode = _s(d.get("logo_mode")).lower()
        if mode not in ("auditly", "coeo", "hidden", "custom"):
            return self.fail(400, "logo_mode must be auditly, coeo, hidden or custom.")
        if mode == "custom" and not db.execute("SELECT 1 FROM brand_asset WHERE slot IN ('logo_light','logo_dark')").fetchone():
            return self.fail(400, "Upload a logo first (light, dark or both), then choose Custom.")
        now = core.now()
        db.execute("INSERT INTO branding (id,logo_mode,updated_at,updated_by) VALUES (1,?,?,?)"
                   " ON CONFLICT(id) DO UPDATE SET logo_mode=excluded.logo_mode, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                   (mode, now, u["email"]))
        core.audit(db, "branding.mode", user=u, ip=self.client_ip(), detail=mode)
        return self.json(self._branding_out(db))

    @staticmethod
    def _image_problem(ext, data):
        """Magic-byte check so a renamed file cannot be stored as an image; SVG must be markup without script."""
        sig = {"png": (b"\x89PNG\r\n\x1a\n",), "jpg": (b"\xff\xd8\xff",), "jpeg": (b"\xff\xd8\xff",), "gif": (b"GIF87a", b"GIF89a"),
               "ico": (b"\x00\x00\x01\x00",)}
        if ext == "webp":
            return None if data[:4] == b"RIFF" and data[8:12] == b"WEBP" else "That is not a WebP image."
        if ext == "svg":
            head = data[:4000].decode("utf-8", "replace").lower()
            if "<svg" not in head:
                return "That is not an SVG image."
            low = data.decode("utf-8", "replace").lower()
            if "<script" in low or "javascript:" in low or re.search(r"\bon[a-z]+\s*=", low) or "<foreignobject" in low:
                return "SVG logos may not contain script or event handlers."
            return None
        return None if any(data.startswith(s) for s in sig.get(ext, ())) else "The file's contents do not match its extension."

    def branding_asset_upload(self, db, u, slot, q):
        """Raw body upload (like recordings): ?filename= names the type; every check runs before the body is read."""
        fname = os.path.basename(self.one(q, "filename", ""))[:200]
        ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
        try:
            size = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            size = 0
        problem = None
        if ext not in self.BRAND_MIME:
            problem = (400, "Logo must be a .png, .jpg, .svg, .webp, .gif or .ico file.")
        elif size <= 0:
            problem = (400, "Empty upload.")
        elif size > self.BRAND_MAX_BYTES:
            problem = (413, "Logo files are capped at 512 KB.")
        if problem:
            self.close_connection = True
            return self.fail(*problem)
        data = self.rfile.read(size)
        if len(data) != size:
            return self.fail(400, "Upload ended early.")
        bad = self._image_problem(ext, data)
        if bad:
            return self.fail(400, bad)
        now = core.now()
        db.execute("INSERT INTO brand_asset (slot,blob,mime,name,bytes,updated_at,updated_by) VALUES (?,?,?,?,?,?,?)"
                   " ON CONFLICT(slot) DO UPDATE SET blob=excluded.blob, mime=excluded.mime, name=excluded.name, bytes=excluded.bytes,"
                   " updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                   (slot, sqlite3.Binary(data), self.BRAND_MIME[ext], fname, size, now, u["email"]))
        if slot.startswith("logo_"):                         # uploading a logo means: use it
            db.execute("INSERT INTO branding (id,logo_mode,updated_at,updated_by) VALUES (1,'custom',?,?)"
                       " ON CONFLICT(id) DO UPDATE SET logo_mode='custom', updated_at=excluded.updated_at, updated_by=excluded.updated_by", (now, u["email"]))
        core.audit(db, "branding.asset", user=u, ip=self.client_ip(), detail="%s %s (%d bytes)" % (slot, fname, size))
        return self.json(self._branding_out(db), 201)

    def branding_asset_delete(self, db, u, slot):
        db.execute("DELETE FROM brand_asset WHERE slot=?", (slot,))
        if slot.startswith("logo_") and not db.execute("SELECT 1 FROM brand_asset WHERE slot IN ('logo_light','logo_dark')").fetchone():
            db.execute("UPDATE branding SET logo_mode='auditly', updated_at=?, updated_by=? WHERE id=1 AND logo_mode='custom'", (core.now(), u["email"]))
        core.audit(db, "branding.asset.delete", user=u, ip=self.client_ip(), detail=slot)
        return self.json(self._branding_out(db))

    def serve_static(self, name):
        try:
            with open(os.path.join(core.HERE, "static", name), "rb") as f:
                body = f.read()
        except OSError:
            return self.fail(404, "Not found.")
        return self.send(200, body, self.STATIC[name], {"Cache-Control": "public, max-age=31536000, immutable"}, nostore=False)

    def serve_app(self):
        try:
            with open(APP_HTML, "rb") as f:
                html = f.read()
        except OSError:
            return self.send(503, b"The app is unavailable.\n", "text/plain")
        nonce = secrets.token_urlsafe(16)
        html = html.replace(b"__CSP_NONCE__", nonce.encode())
        html = html.replace(b"__APP_VERSION__", core.app_version().encode())
        csp = ("default-src 'none'; script-src 'nonce-%s'; style-src 'nonce-%s'; "
               "img-src 'self' data:; media-src 'self'; connect-src 'self'; "
               "form-action 'none'; base-uri 'none'; frame-ancestors 'none'" % (nonce, nonce))
        return self.send(200, html, "text/html; charset=utf-8", {"Content-Security-Policy": csp})


def fmt_when(iso):
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return d.strftime("%d %b %Y, %H:%M UTC")
    except (ValueError, TypeError):
        return str(iso or "")


def _which(name):
    for p in os.environ.get("PATH", "").split(os.pathsep):
        if os.path.isfile(os.path.join(p, name)):
            return os.path.join(p, name)
    return None


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Plain http:// opened against the TLS port, a port scanner, a tab closed mid-response: one quiet
        # line each, not a traceback per connection. Anything else keeps the default report.
        et, ev = sys.exc_info()[:2]
        if et and issubclass(et, (ssl.SSLError, ConnectionResetError, BrokenPipeError, TimeoutError)):
            log("connection from %s dropped: %s" % (client_address[0], (str(ev) or et.__name__)[:90]))
            return
        super().handle_error(request, client_address)


def start_tls_listener(c, bind):
    """0.49.0: a second, encrypted listener in the same process when AUDITLY_TLS_PORT is set.

    Browsers open a microphone only on a secure page and the LAN link is plain HTTP; this gives it an
    https:// twin with no nginx and no root. Same Handler, same database, same job queue. The certificate
    is whatever AUDITLY_TLS_CERT/KEY point at -- restart.sh generates a self-signed pair when none exists,
    and a real one drops in over it with no code change.

    Deliberately NO Strict-Transport-Security anywhere in this process: HSTS is per host and ignores the
    port, so one such header from :8444 would make browsers refuse http://host:8084 for a year. The nginx
    config in deploy/ does send it, correctly, because under nginx the plain link no longer exists."""
    tport = core.cfg_int(c, "AUDITLY_TLS_PORT", 0)
    if tport <= 0:
        return None
    cert = core._abs(c.get("AUDITLY_TLS_CERT") or "tls/auditly.crt")
    key = core._abs(c.get("AUDITLY_TLS_KEY") or "tls/auditly.key")
    if not (os.path.exists(cert) and os.path.exists(key)):
        log("TLS: AUDITLY_TLS_PORT=%d but %s or %s is missing -- ./restart.sh generates a self-signed pair; "
            "serving plain HTTP only" % (tport, cert, key))
        return None
    # A bad certificate or a taken port costs the secure link, never the app: log it and keep serving plain HTTP.
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(cert, key)
        srv = Server((bind, tport), Handler)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    except (OSError, ssl.SSLError) as e:
        log("TLS: could not listen on %s:%d (%s) -- serving plain HTTP only" % (bind, tport, core.redact_paths(str(e))))
        return None
    threading.Thread(target=srv.serve_forever, name="auditly-tls", daemon=True).start()
    log("Auditly v%s serving https://%s:%d/  (certificate %s)" % (core.app_version(), bind, tport, cert))
    return srv


# ── CLI ───────────────────────────────────────────────────────────────────
def add_user(email, role):
    db = core.init_db()
    email = email.strip().lower()
    pw = getpass.getpass("Password for %s: " % email)
    if pw != getpass.getpass("Confirm: "):
        sys.exit("Passwords do not match.")
    problem = core.password_problem(pw)
    if problem:
        sys.exit(problem)
    h, s = core.hash_password(pw)
    r = db.execute("SELECT id FROM user WHERE email=? COLLATE NOCASE", (email,)).fetchone()
    if r:
        db.execute("UPDATE user SET pw_hash=?,pw_salt=?,role=?,active=1 WHERE id=?", (h, s, role, r["id"]))
        db.execute("DELETE FROM session WHERE user_id=?", (r["id"],))
        print("Reset the password for existing user %s (%s)." % (email, role))
    else:
        db.execute("INSERT INTO user (id,email,pw_hash,pw_salt,role,active,created_at) VALUES (?,?,?,?,?,1,?)",
                   (core.new_id(), email, h, s, role, core.now()))
        print("Created %s %s." % (role, email))
    core.audit(db, "user.bootstrap", target=email, detail="via CLI, role " + role)


def main():
    ap = argparse.ArgumentParser(description="Auditly server")
    ap.add_argument("--add-user", metavar="EMAIL")
    ap.add_argument("--role", choices=("admin", "reviewer"), default="reviewer")
    ap.add_argument("--init-db", action="store_true")
    ap.add_argument("--demo", action="store_true", help="fake providers + seeded data; no keys")
    ap.add_argument("--seed-rubric", action="store_true",
                    help="seed the sample rubric and the agent/QA name lists into the real database, then exit")
    ap.add_argument("--load-demo-data", action="store_true",
                    help="load ~44 past calls tagged DEMO (with reviews) so the Dashboard has trends, then exit")
    ap.add_argument("--remove-demo-data", action="store_true", help="delete the calls --load-demo-data added, then exit")
    ap.add_argument("--optimise-version", metavar="RUBRIC_VERSION_ID",
                    help="run 'Optimise for the scorer' on one QA type version now (honours the caps and spend gate), then exit")
    ap.add_argument("--eval-determinism", metavar="TRANSCRIPT_ID|latest",
                    help="score one transcript N times with the guidance as written and N times with the optimised"
                         " rules, in memory (nothing written), and print agreement, spread and cost; then exit")
    ap.add_argument("--version-id", metavar="RUBRIC_VERSION_ID", help="with --eval-determinism; default: current version of the newest QA type")
    ap.add_argument("--runs", type=int, default=3, help="with --eval-determinism; runs per mode (default 3)")
    ap.add_argument("--scorer", metavar="provider:model", help="with --eval-determinism; default: the .env scorer")
    a = ap.parse_args()

    if a.demo:
        os.environ["AUDITLY_DEMO"] = "1"
        os.environ.setdefault("AUDITLY_DB", "./auditly-demo.db")
    if a.add_user:
        return add_user(a.add_user, a.role)
    c = core.cfg()
    db = core.init_db()
    if a.init_db:
        db.close()
        print("Database ready at %s" % core.db_path(c))
        return
    if a.optimise_version:
        v = db.execute("SELECT * FROM rubric_version WHERE id=?", (a.optimise_version,)).fetchone()
        if not v:
            sys.exit("No QA type version with id %s." % a.optimise_version)
        if (v["sync_status"] or "none") in rubric_sync.LIVE:
            sys.exit("That version is already being optimised (status %s)." % v["sync_status"])
        if rubric_sync.optimised_scorecards(db, v["id"]):
            sys.exit("Calls were already scored with this version's optimised rules; save a new version instead.")
        ready = rubric_sync.readiness(c)
        if ready:
            sys.exit(ready)
        cp = rubric_sync.caps(db, c, v)
        if not cp["allowed"]:
            sys.exit(cp["reason"])
        core.audit(db, "rubric.sync.requested", target=v["id"], detail="v%d · CLI" % v["version_no"])
        rubric_sync.reset_and_enqueue(db, v["id"])
        rubric_sync.SYNCQ.get_nowait()                     # no thread here: run it in this process
        db.close()
        rubric_sync.set_logger(log)
        rubric_sync.run(v["id"], c)
        db = core.connect()
        v = db.execute("SELECT * FROM rubric_version WHERE id=?", (v["id"],)).fetchone()
        print("v%d: %s%s" % (v["version_no"], v["sync_status"], (" -- " + v["sync_error"]) if v["sync_error"] else ""))
        db.close()
        return 0 if v["sync_status"] == "done" else 1
    if a.eval_determinism:
        rc = rubric_sync.evaluate(db, c, a.eval_determinism, a.version_id, a.runs, a.scorer, print)
        db.close()
        return rc
    if a.seed_rubric:
        import demo_data
        vid, _ = demo_data.seed_for_real(db)
        db.close()
        print("Seeded into %s: rubric 'L1 Support v1' (version %s). Add agents and QA reviewers under Settings > Names." % (core.db_path(c), vid[:8]))
        return
    if a.load_demo_data or a.remove_demo_data:
        import demo_data
        if a.remove_demo_data:
            n = demo_data.remove_history(db)
            print("Removed %d demo call(s) from %s." % (n, core.db_path(c)))
        else:
            if demo_data.history_count(db):
                sys.exit("Demo data is already loaded; run --remove-demo-data first.")
            ver = db.execute("SELECT v.id FROM rubric_version v JOIN rubric r ON r.id=v.rubric_id WHERE r.archived=0"
                             " ORDER BY v.created_at DESC LIMIT 1").fetchone()
            if not ver:
                sys.exit("No rubric yet: run --seed-rubric first.")
            demo_data._seed_people(db, "setup")
            n = demo_data.seed_history(db, c, "demo", ver["id"], "setup")
            print("Loaded %d demo call(s) into %s (tagged DEMO; the Dashboard's Real/Demo switch shows them)." % (n, core.db_path(c)))
        db.close()
        return

    if a.demo:
        import demo_data
        demo_data.seed(db, c)
        db = core.connect()
        log("DEMO MODE: providers are fakes, nothing leaves this machine.")
        log("Sign in as %s / %s" % (demo_data.DEMO_EMAIL, demo_data.DEMO_PASSWORD))
    if c.get("AUDITLY_INSECURE_COOKIES") == "1":
        log("WARNING: AUDITLY_INSECURE_COOKIES=1 -- cookies are sent without the Secure flag. "
            "Acceptable on localhost only.")
    if open_access(c):
        log("OPEN ACCESS: AUDITLY_OPEN_ACCESS=1 -- no sign-in; every visitor acts as %s (admin). "
            "Not for real recordings." % OPEN_ACCESS_EMAIL)
    st = llm.provider_status(c)
    if not st["demo"]:
        if not st["allow_spend"]:
            log("AUDITLY_ALLOW_SPEND is not 1: uploads will be refused until it is set.")
        if not st["deepgram"]["key_present"] and not st["openai"]["key_present"]:
            log("no transcription key in .env (DEEPGRAM_API_KEY / OPENAI_API_KEY).")
    n = worker.requeue_unfinished(db)
    if n:
        log("re-queued %d unfinished job(s)" % n)
    m = rubric_sync.requeue_unfinished(db)
    if m:
        log("marked %d interrupted QA-type optimisation(s) failed; press Optimise to run again" % m)
    db.close()
    worker.start(c)
    bind, port = c.get("AUDITLY_BIND") or "127.0.0.1", core.cfg_int(c, "AUDITLY_PORT", 8084)
    with Server((bind, port), Handler) as srv:
        log("Auditly v%s serving http://%s:%d/" % (core.app_version(), bind, port))
        tls = start_tls_listener(c, bind)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            if tls:
                tls.shutdown()
                tls.server_close()


if __name__ == "__main__":
    sys.exit(main())
