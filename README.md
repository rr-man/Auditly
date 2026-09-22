# Auditly

**Version:** 0.63.1 · 2026-09-22

An internal tool for the Level 1 support QA team. A reviewer uploads a call
recording; the tool transcribes it (Deepgram or OpenAI), scores it against the
team's own QA guidelines (a **QA type**) on a 100-point basis, and shows a scorecard with
per-criterion scores, verbatim evidence, **Opportunities** and **Misses**. The
reviewer can override any score with a note, and export the result as PDF, CSV
or JSON. The **Audit** tab is where the QA writes up the coaching, resolution
and action plan and submits the **final QA score**; the **Dashboard** shows QA
trends site-wide, per agent and per QA by day, week, month, quarter or year,
with customer sentiment and call reasons.

> **Data leaves this machine.** Recordings are sent to the transcription
> provider and transcripts to the scoring model. This is the point of the tool,
> but it is a deliberate departure from the sibling tools on this box, which
> keep everything local. Audio is deleted after `AUDITLY_RETENTION_DAYS`;
> transcripts and scorecards are kept.

## Quick start

```bash
cd Auditly
python3 auditly_host.py --demo            # no keys, fake providers, seeded sample call
# open http://127.0.0.1:8084/  and sign in as demo@example.com / demo-demo-demo

```

Demo mode transcribes nothing: every upload gets the canned sample call.

For real use (this is what the LAN link runs):

```bash
cp .env.example .env && chmod 600 .env   # fill in DEEPGRAM_API_KEY and OPENAI_API_KEY,
                                         # set AUDITLY_ALLOW_SPEND=1, generate SESSION_SECRET
python3 auditly_host.py --seed-rubric       # sample rubric into auditly.db (names: Settings › Names)
python3 auditly_host.py --add-user you@example.com --role admin   # unless AUDITLY_OPEN_ACCESS=1
./restart.sh                             # -> http://<this host>:8084/auditly/  (LAN, plain HTTP)
                                         #    https://<this host>:8444/         (same app; self-signed, so
                                         #    each browser warns once: Advanced -> Proceed. Voice input needs this one)
```

Stdlib Python 3 only. No pip, no build, no node.

## How a call is scored

1. **Upload** — pick the QA type and the agent, then drop an
   mp3/wav/m4a/ogg/webm/flac on the Upload tab. That is the whole form: the
   scoring model, the QA reviewer, the audit's name and the Real / Demo / Test
   switch already have working defaults and wait under **More options**.
   A **Next step** bar under the tabs says what to do now with the call you are
   on and has one button that does it, all the way from "still processing" to
   "coached"; `×` hides it and Help brings it back. Each dropped file
   waits in "This session's uploads" with its own transcription engine
   (Deepgram nova-3, OpenAI whisper-1 or gpt-4o-transcribe) until you press
   Start; "Audit with" picks the scoring LLM (gpt-4o-mini by default). A
   **Help** button at the top opens a six-step guide to the app with Go there links (once by
   itself after the first sign-in); **Show me around** beside it is a guided walk with a spotlight and a
   callout at nine stops over the real screens — including a call being processed and Ask Auditly — on sample data wherever it can (on a real server it
   offers to load the fictional demo calls first rather than open a customer's call); the **Flow** button beside it opens the eight-step flowchart (Mermaid, zoomable):
   QA type set-up, recording, transcription, scoring, scorecard (with the
   re-audit paths and Ask Auditly beside it), QA audit, coaching and Dashboard.
   An audit name is built from those (editable). The **Run as** toggle offers
   Real call and Test call (real audio, not counted as an audit); in demo mode
   a Demo run (a fake scorecard at no cost) is offered too. Demo and test calls
   are tagged and filtered out of the Calls list by default. The upload returns immediately;
   a background worker does the rest and the Calls tab shows progress — an arrow while
   uploading, a sound wave while transcribing, thinking dots while the AI pre-audits, the
   Auditly tick as each stage finishes, and the score ring drawing itself in when the call lands.
2. **Transcribe** — Deepgram `nova-3` (default; speaker-separated, files up to
   200 MB) or OpenAI `whisper-1` (no speaker separation; 25 MB cap, enforced
   at upload).
3. **Score** — the scoring model receives the rubric (criteria, weights,
   met/partial/missed guidance) and the timestamped transcript, and must return
   strict JSON: a rating and score per criterion, verbatim evidence quotes,
   which speaker is the agent, strengths, opportunities and misses. The server
   clamps every score to `[0, weight]`, drops any quote it cannot find in the
   transcript, and **recomputes the overall itself** — the model's arithmetic
   is never trusted.
4. **Review** — the scorecard shows an overall ring, the per-criterion table
   with evidence you can click to seek the audio (⟲20 ⟲10 ⟳10 ⟳20 and a speed menu sit beside
   the player; the recording keeps playing when you switch tabs or save a score — Settings › **Preferences**
   can make it stop instead, and switches animations on or off), a short call summary with a **Details**
   toggle (the facts as heard — extension, phone, ticket, MAC address, the troubleshooting steps and what was
   discussed — each kept only if it occurs in the transcript, with the criterion it speaks to as a pill), the QA
   summary, Misses, Opportunities and Strengths, and a **Customer information (Captured)** card: name, company,
   email, phone, ticket number and call ID as the scorer heard them, editable; ✎ by the title renames the call. Override a score with a note; the overall recomputes and the
   change is audited. A criterion marked *critical* auto-fails the call when
   missed. Doubt the result? "Audit again with…" rescores with another LLM and
   "Transcribe again with…" redoes the transcript with another engine; each adds
   a new run beside the old one. Identical work is never billed twice: the same
   audio through the same engine reuses its transcript, and an identical audit
   is offered to open instead of re-run. Settings shows the spend so far.
5. **Start audit** — from Calls (per row or on the scorecard) a scored call
   is given a QA of record, goes to the Audit queue and opens in the Audit tab
   so the review begins at once; tick several to queue them together. Calls
   shows each call's stage: scored · queued for audit · in review · audited ·
   coached. **Focus** — the chip at the top, or the button on any row or
   scorecard — hides every call on Calls, Audit and Coaching except the ones you
   bring into focus (as many as you like; search to find them while everything
   is hidden); Show all brings the rest back; your browser remembers it.
6. **Audit** — the Audit tab lists only calls an audit was started on, under
   two sub-tabs: **To audit** (queued + in review) and **Completed** (final
   score submitted). The QA opens a call, fixes the call
   details if needed (agent, QA, customer, call ID), corrects any score,
   writes the coaching notes, resolution and action plan, marks the coaching
   delivered, and submits. Submitting freezes the final QA score (the
   scorecard after overrides) and locks the scores until the review is
   reopened, and emails the agent a link to a read-only copy of the scorecard
   where they can raise a **dispute** themselves (their address lives under
   Settings › Names; **Dispute link** on the scorecard sends or copies it again).
   The QA can also record a dispute for them (⚑ on a criterion, or Dispute for
   the whole card); a reviewer upholds it with a corrected score, rejects or
   withdraws it, and Completed can filter to disputed calls. The Dashboard trends those scores site-wide, per agent and per QA,
   and counts audits and coaching sessions per QA per period. Settings › Names
   › Demo data loads (and removes) sample history so the Dashboard can be seen
   before real calls are audited; the Dashboard's Real / Demo / All switch
   keeps it out of real totals.
   The **Coaching** tab then lets the QA tick any of an agent's scored calls
   into a coaching session, with a running tally of what they add up to; the
   session shows Totals and the agenda is drafted from the reviews, **Schedule** sets the time
   (**Email invite** opens the calendar invite as a draft in the QA's own
   mail app, or sends it from the server; an `.ics` file too; agent emails live under Settings
   › Names), the **Calendar** sub-tab shows the month, and **Mark delivered**
   stamps every included call as coached.
   Settings › **Knowledge** holds reference documents (procedures, product
   facts); the excerpts that match a call are shown to the scorer to check what
   the agent said — never as evidence of what happened.
   The Dashboard opens on an **Executive** view: the headline numbers against the
   target (`AUDITLY_TARGET_PCT`) with the change since the previous period, where
   calls earn points (strongest first, with what is still there to gain), score
   bands, coaching and dispute health, who is leading and who has most room to
   grow, and an Export PDF; **Detail** keeps every trend chart and table.
   Settings › **Server** has four panes — Status, Providers, Assistant, Branding — and remembers the
   one you were on.
   White label: Settings › Server › **Branding** shows the Auditly wordmark by
   default (it is stamped on, letter by letter, each time the page loads) and lets an admin hide the logo, switch to COEO, or upload their own
   logo and tab icon for light and dark mode; a choice takes effect only when **Apply** is pressed, and
   hiding the logo hides the Ask button too.
   The **notification** button in the header turns red with a count when something you have not
   seen happened — a coaching session scheduled or moved, a dispute raised or resolved, an agent
   below the Dashboard's pass target, a call that failed to process; click a line to go to it.
   Settings › **Notifications** chooses which of those each person gets.
   **Ask Auditly** is the round mark in the lower right. With a call open in Calls or Audit it
   answers questions about that call — why a criterion was rated as it was, what the evidence says,
   what the QA wrote — only from the call's own record, citing what it used under each reply, and
   saying so when the answer is not there. It never changes anything. An admin switches it on,
   picks the model and caps, and **edits its instructions** under Settings › Server › **Assistant**; the
   six safety rules beneath the instructions are always added and cannot be edited away. Every save is
   a version, and a Try it box shows the effect before reviewers see it. A **microphone** button lets
   you speak the question; it is transcribed by the desk's own provider and lands in the box for you to
   check before Ask. Browsers only open a microphone on HTTPS, so voice works on the app's own
   `https://…:8444/` link (the plain link shows that address instead). The design and its limits are
   in `docs/ASSISTANT_DESIGN.md`.

## Guidelines → QA type

Under **Settings › QA types**, paste the team's QA guidelines or drop a `.txt`, `.md` or
`.docx`. *Extract criteria* asks the model to propose weighted criteria that
total 100; you edit them and save. Each criterion can carry a **good sample** and a **bad sample** — a
line that earns Met and one that earns Missed — which the scorer calibrates against (illustrations, never
required wording). Weights that do not total exactly 100 cannot
be saved. Open a QA type to **Edit** it (saves as the next version, with a note of what
changed, the time and the editor) or **Save as new QA type**; only admins and QA reviewers can.
Saving always creates a new **version**; scorecards remember which
version scored them, so old results stay readable after the QA type changes.

The call ID is the recording's file name without its extension (the PBX names files after the
call), and the scorer reads the customer's name, company and contact details from the call
itself; both can be corrected on the call's details. A file that was uploaded before is flagged
amber on the Upload tab before anything is sent. The scorer also labels each speaker — caller,
live agent, voice AI (`AUDITLY_VOICE_AI_NAME`, default Emma) or other — scores only the live
agent, and flags transferred calls.

## Files

| File | Role |
|---|---|
| `auditly_host.py` | HTTP server, routes, upload, auth, exports, CLI. Not called `serve.py` on purpose. |
| `core.py` | config (`.env` re-read per call), SQLite, scrypt, sessions, audit, changelog parser |
| `worker.py` | background job pipeline, re-queue on restart, audio retention sweep |
| `transcribe.py` | Deepgram / OpenAI / Demo transcribers → one normalised shape |
| `llm.py` | the one outbound HTTP helper; OpenAI / Anthropic / Demo scorers |
| `score.py` | prompt, strict JSON schema, validation, `compute_overall()`, sentiment + call-reason lists |
| `dashboard.py` | trend aggregation: buckets, one value per recording, per agent / per QA series |
| `rubric.py` | guideline text extraction, criteria normalisation, weights-must-be-100 |
| `pdfgen.py` | zero-dependency PDF writer (from coeo-transcripts) |
| `demo_data.py`, `fixtures/` | demo rubric, sample call, deterministic scorer output |
| `assistant.py` | Ask Auditly: the call's record as fenced context, the cited answer, the citation check |
| `auditly.html` | the entire front end, one file, no external assets |
| `schema.sql` | database |
| `docs/` | `CHANGELOG.md` (source of the version chip), `PLAN.md`, `PRD_Auditly.md`, `RUNBOOK.md`, `VERIFY.md` |
| `deploy/` | nginx site + systemd unit + self-verifying installer |
| `tests/test_auditly.py` | end-to-end tests against a real server in demo mode |

## Verify

```bash
python3 tests/test_auditly.py
```

See `docs/VERIFY.md` for the browser walk-through.
