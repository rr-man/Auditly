# PRD — Auditly

**Version:** 1.96 · 2026-09-23
**Status:** Living document
**Reflects application build:** v0.64.0

## 1. Overview

A web tool for the Level 1 support QA team: upload a call recording, get a
transcript and a scorecard against the team's own guidelines, with per-criterion
scores on a 100-point basis, verbatim evidence, Opportunities and Misses, and a
reviewer override on every line.

## 2. Goals

1. A reviewer uploads a recording and does nothing else until the scorecard is ready.
2. The team's QA guidelines — not a vendor's — define the QA type (the rubric), and it is versioned.
3. Every score is explainable: a rating, a rationale and quotes with timestamps.
4. The number is trustworthy: weights total 100, scores are clamped, the overall is computed server-side, and a human can override any line.
5. Nothing costs money by accident; nothing runs without keys in demo mode.

## 3. Background and problem

QA reviewers listen to calls end to end and fill in a spreadsheet by hand. It is
slow, coverage is a small sample, and two reviewers score the same call
differently. Per-call scoring against a written rubric with cited evidence
gives consistent first-draft scores in minutes and leaves the reviewer's time
for judgement and coaching.

## 4. Target users

- **Reviewer** — uploads calls, reads scorecards, overrides, exports; audits
  (coaching, resolution, action plan, final score) and reads the Dashboard.
- **Admin** — the above, plus users, rubric archiving, deletes.
- **L1 agent** — no login. Since 0.52.0 they receive an expiring, single-scorecard link by email when an audit is submitted and raise
  disputes on it themselves; the QA can still record disputes on their behalf, schedules their coaching (invite by .ics / mail) and shows
  them their Dashboard view (By agent) or the PDF.

## 5. Current capabilities (v0.1.0)

- Upload mp3/wav/m4a/ogg/webm/flac up to `AUDITLY_MAX_UPLOAD_MB` (200); several at once; each file waits as Ready with its own transcription provider until Start; progress bar. Flowchart of the pipeline behind the **Flow** button at the top and under the version chip; a **Help** button beside it opens the six-step guide to the app.
- Transcription: Deepgram `nova-3` with speaker separation (default) or OpenAI `whisper-1` (25 MB cap enforced at upload).
- Rubrics: paste or upload `.txt/.md/.docx`, LLM-proposed criteria, editable grid, weights must total 100, critical flag, immutable versions, archive.
- Scoring: strict-JSON scorer, per-criterion met/partial/missed/na, evidence verified against the transcript, server-side overall, auto-fail on missed critical criterion, Strengths / Opportunities / Misses.
- Review: overall ring, criteria table, click-to-seek evidence and transcript, agent/customer swap, override with note, rescore with another rubric, retry failed jobs.
- Names: agents and QA reviewers managed under Settings (add/import, rename with cascade, archive, delete when unused); Upload uses dropdowns; both optional, but a chosen name must be a managed one.
- Audit name per call (Agent — Customer company — Customer name — date, editable) heads the Calls table, scorecard and PDF.
- Customer information (Captured) (0.61.0; the Customer card before it): the scorer extracts name, company, email, phone and the ticket / case / reference number quoted by either party (prompt v5, v8); every row is written to the recording and editable on the card and in the Audit tab's Call details; in PDF/CSV as "Ticket number". Nothing customer-related is typed on Upload.
- Call ID = the recording's file name without extension; the Upload tab flags a file whose call ID or name+size was uploaded before (amber, names the earlier audit, says if audited; Remove before sending).
- Speaker roles: caller · agent (live) · voice AI (`AUDITLY_VOICE_AI_NAME`, default Emma) · other; only the live agent is scored; transferred calls flagged; roles editable on the scorecard.
- Sentiment: colour-coded banner at the top of the scorecard.
- Tone & delivery: talk share, interruptions, silences, agent pace and response time from the transcript timings, on the scorecard and in exports; never scored. Per-line sentiment from Deepgram (`AUDITLY_STT_SENTIMENT=1`, text-derived, English) draws a dot per transcript line and a caller/agent strip, with the caller's trend checked against the scorer's verdict.
- Dashboard pipeline strip: the Flow's steps with counts for the range/filters, clickable. COEO colour scheme (light/dark) and the animated logo in the header; theme switch is Light · Dark.
- Rescore / audit again / transcribe again can carry over overrides (same QA type version) and audit notes (as a draft) to the new run.
- Call summary: the scorer writes a plain 2–3 sentence account of the call, separate from the QA summary.
- Call facts (0.61.0, prompt v8): under the call summary a Details toggle lists identifiers as heard (extension, phone, MAC, ticket, account, device, address — each kept only if its characters occur in the transcript, `score._facts`), the troubleshooting steps with results, what was discussed and the outcome; items keyed to a criterion carry its name as a pill that jumps to the row. In CSV/PDF and in Ask Auditly's context.
- Good / bad samples per criterion (0.61.0): `criterion.example_good/example_bad`, authored in the editor, proposed by Extract, read by the scorer as calibration (SYSTEM rule 15) and by the optimiser; immutable per version like the rest of the authored text.
- Settings tabs: QA types · Names (with Remove demo names) · Server · Account. A QA type's view offers Edit (next version) and Save as new QA type; versions carry notes, time and author; editing is limited to admin and reviewer roles (server-enforced).
- Recording kind: real | demo (fake providers, no cost) | test (real providers, practice). Calls defaults to real; any future totals use `kind='real'`.
- Hand-off: **Start audit** from Calls (per row or from the scorecard) gives a scored call a QA of record, queues it and opens it in the Audit tab; ticking several queues them. Calls shows the stage (scored · queued for audit · in review · audited · coached). The Audit tab lists only calls an audit was started on, under two sub-tabs — To audit (queued + in review) and Completed (submitted, audited or coached) — each with a narrower state filter and a count; a queued call can be un-queued until written in.
- Audit: a review per scorecard — coaching notes, resolution, action plan, follow-up date, coaching-delivered mark, and the final QA score frozen at submit (the scorecard after overrides); submitted reviews lock overrides and speaker swaps until reopened. Call details (agent, QA of record, customer, call ID, audit name) are editable in the workspace.
- Disputes: the QA records an agent's dispute of a criterion (⚑) or of the whole scorecard, in the agent's words; a reviewer upholds (optionally with a corrected score through the override path — a submitted audit is reopened first), rejects or withdraws it. Open disputes are flagged on Calls, Audit and the scorecard and filterable under Audit › Completed.
- Coaching tab: the QA ticks any of an agent's scored calls into a coaching session (scope: waiting for coaching — submitted, not yet coached, or with an open dispute — · audited · all scored; one live session per call) with a live tally of the selection (count, average, lowest, below target, misses, points lost per criterion); the session shows Totals; agenda drafted from the reviews and totals, editable; Mark delivered stamps `coached_at` on every included submitted review, Reopen undoes it, Cancel frees the calls; coaching-pack PDF.
- Coaching calendar: sessions scheduled with date, time, length and place (UTC stored, browser offset supplied), overlap warning per agent/QA, month grid with review follow-up dates, `.ics` download (agent as attendee when `person.email` is set) and a `mailto:` invite. No mail server; agents cannot self-book (no login).
- Knowledge base: reference documents (.txt/.md/.docx or pasted; scoped to every QA type or one) chunked and searched lexically (BM25, `kb.py`) with the transcript as the query; the top excerpts go to the scorer as reference material under rule 13 (accuracy of what the agent said, never evidence); shown on the scorecard; `kb_key` makes the audit cache material-aware. Prompt v7.
- Motion that says what is happening: stage motifs while a call uploads, transcribes and is pre-audited, the score ring drawing in when it lands, and the mark's tick stamping beside the final ring when a QA submits; all still under `prefers-reduced-motion`. The demo history is tuned so the sample Dashboard sits near the target (pass rate and coaching coverage 80–95 %, Marcus lowest and under it).
- Executive dashboard: default Dashboard view with KPIs against `AUDITLY_TARGET_PCT` (average, pass rate, calls, auto-fail rate, audit and coaching coverage) and their change since the previous period, trend vs target, attainment per criterion (strongest first, with the points still to gain), score bands, sentiment shift, coaching and dispute health, the agents leading and those with most room to grow (largest rise marked most improved), critical checks passed rather than an auto-fail rate, dips in amber; one-page PDF export with the same words. Detail view keeps the full charts.
- Branding (white label): the Auditly wordmark and tab icon by default; Settings › Server › Branding switches the header logo (Auditly · COEO · Hidden · Custom; a pick takes effect only on Apply, Cancel drops it) and takes uploads for logo and icon in light and dark mode, stored as blobs, validated by magic bytes, SVG script refused; `/api/branding` public for the sign-in page.
- The Auditly wordmark is animated (CSS inside the SVG) and is stamped on at every page load; a motionless twin is served when the visitor prefers reduced motion, chosen by the page because the media query does not reach an SVG inside an `<img>`.
- **Next step bar** (0.45.0): a strip under the tabs naming the one thing to do now with the call in hand and a button that does it, driven by the call's stage (processing → scored → queued/in review → audited → coached). Dismissible, restored from Help. The Upload form keeps only the QA type, the agent and the drop zone; the four defaulted choices moved behind a More options disclosure. No new endpoint and no change to what is uploaded.
- **Ask Auditly** (0.43.0 launcher, 0.46.0 assistant): questions about the open call, answered only from its record — criteria, evidence, review, summary, transcript, knowledge-base excerpts — with every answer cited and uncheckable citations dropped. Answers only; never writes. Managed under Settings › Server › Assistant (0.47.0): on/off, model, caps, usage, and the editable half of the prompt as immutable versions with the safety rules locked and always appended; Try it answers through the real path. Settings override the `ASK_*` `.env` keys; the key stays in `.env`. Same spend gate as scoring, own prompt versions (code and admin), own audit verbs, daily and per-person caps, own optional project key. Voice input (0.48.0): the clip is transcribed by the configured STT provider server-side, deleted at once, and the text is placed in the box for the reviewer to send; requires a secure context (HTTPS or localhost), so it depends on the TLS deployment. QA reviewers only: agent access needs the per-row authorisation this app has never had (see `docs/ASSISTANT_DESIGN.md`).
- Coaching invites by email: a scheduled session's invite is sent as a calendar request (method REQUEST) through the `.env` SMTP relay from the server mailbox with the QA as organiser/Reply-To; every attempt recorded; ✉ / ✓ marks and a per-week summary (sent · delivered · awaiting invite) in the Month and the new Week (time-slot) calendar views; Settings › Server › Mail with Send test.
- Coaching invites from the QA's own mail app: an `.eml` draft (`X-Unsent: 1`, calendar REQUEST attached) the QA opens in Outlook and sends from their account; Mark invite as sent records channel `mail_app` for the ✉ mark. Server SMTP optional; mailto fallback without attachment.
- Demo data on any server: admin Load / Remove under Settings › Names (and CLI flags); loaded calls are tagged DEMO, carry no audio, and the Dashboard's Real / Demo / All switch decides whether they show.
- Dashboard: QA score trend by day/week/month/quarter/year, site-wide, per agent, per QA; audits and coaching per QA per period; sentiment and call-reason counts; per-agent and per-QA tables; one value per recording; "Audited only" source.
- Sentiment and call reasons from the scorer (prompt v4): customer mood start/end/overall; 1–4 reasons from a fixed category list with a detail line.
- Exports: PDF, CSV, JSON — including the review, sentiment and reasons.
- Ops: login (scrypt, sessions, CSRF header, rate limit), audit log, audio retention sweep, `--demo` mode, version chip → changelog + plan.

## 6. Functional requirements

| # | Requirement | Where |
|---|---|---|
| F1 | Upload returns a job id immediately; work is asynchronous | `auditly_host.py:upload`, `worker.py` |
| F2 | Transcription engine selectable per file (Deepgram nova-3, whisper-1, gpt-4o-transcribe) and audit LLM per call (gpt-4o-mini, gpt-5.6-luna, gpt-4.1-mini, Claude if keyed); both re-runnable from the scorecard as new runs | Upload tab, scorecard header, `llm.STT_CHOICES` / `scoring_choices()` |
| F3 | QA type weights must total exactly 100 to save | `rubric.validate()` |
| F4 | Per-criterion score ∈ [0, weight]; overall = Σscore / Σweight(non-na) × 100 | `score.validate_result()`, `compute_overall()` |
| F5 | Evidence quotes must exist in the transcript or are dropped with a note | `score._quote_ok()` |
| F6 | Missed critical criterion ⇒ auto-fail flag | `score.validate_result()` |
| F7 | Override per criterion with mandatory note; overall recomputes; audited | `auditly_host.py:override` |
| F8 | Old scorecards keep the QA type version that scored them | `rubric_version` immutability |
| F23 | One `<audio>` for the page (0.60.0): the open scorecard borrows it and every re-render parks and remounts it, so an override, a review or a details save never stops playback; leaving the call keeps it playing in a foot bar (or stops it, per Settings › Preferences); ⟲20 ⟲10 ⟳10 ⟳20 and a speed menu; Preferences also switches animations Auto / On / Off beside the system's reduce-motion setting, per browser | `mountPlayer()`, `parkPlayer()`, `applyMotion()`, `#playBar`, `#set-prefs` |
| F9 | Version chip opens dated changelog and the plan; Help opens the in-app guide (How to use · About) and **Show me around** (also a header button), a nine-stop guided tour over the real screens (with a staged Processing stop and an Ask Auditly stop that opens the dock), user-started only, on sample data wherever it can — a real server without demo calls is asked to load them (fictional, no audio, tagged Demo) or to continue with real calls | `#verBtn`, `/api/changelog`, `/api/plan`, `tourStart()` |
| F10 | Identical work is never billed twice: same audio + same engine reuses the transcript; same transcript + rubric + model + prompt version + guidance mode offers the existing audit; usage recorded per job | `worker._cached_transcript()`, `auditly_host._identical_audit()`, `job.usage_json` |
| F11 | **Optimise for the scorer** (manual, capped): a reviewer rewrites a QA type version's guidance into literal decision rules via one LLM call; stored as `criterion.opt_*` beside the authored text, which is never changed. The scorecard records `guidance_mode` (`optimised` / `authored`). Limits: 1 run per version, 5 per day; 3 automatic retries inside a run; refused up front when spending or the key is missing. every Save asks straight away (Optimise / Not now). `--eval-determinism` confirms the effect before it is relied on | `rubric_sync.py`, `rubric.optimise_criteria()`, `score.build_prompt(use_optimised)` |
| F12 | Agent dispute per criterion or scorecard, raised by the agent from their emailed link (`dispute.via='link'`) or recorded by the QA; one open dispute per target; upheld/rejected/withdrawn with a note; an upheld score goes through the override path (409 while the audit is submitted) | `auditly_host.py:dispute_raise`, `dispute_action`, `_apply_override` |
| F13 | Coaching session per agent over ≥0 audited calls; a scorecard in at most one live session; done ⇒ `review.coached_at = done_at` on submitted reviews only; reopen clears exactly those | `auditly_host.py:session_create`, `session_post`, `coaching.draft_agenda` |
| F14 | Schedule with browser-local time + offset → UTC; 15–180 min; overlap per agent/QA is 409 unless forced; `.ics` (RFC 5545) and calendar feed by local day | `coaching.parse_when`, `coaching.overlaps`, `coaching.ics`, `auditly_host.py:api_calendar` |
| F15 | Knowledge-base retrieval is lexical and free; ≤ 6 chunks / 5 000 chars per call; a chunk needs ≥ 2 informative query terms and ≥ half the best score; `kb_key` in the identical-audit key | `kb.py`, `score.build_prompt(kb_excerpts)`, `auditly_host.py:_identical_audit` |
| F16 | Executive KPIs computed in `dashboard.executive()` over the same one-value-per-recording rows; previous period = same length ending the day before; leaders need ≥ 3 calls; target from env, 1–100 else 90 | `dashboard.aggregate(with_previous)`, `auditly_host.py:_dashboard_data`, `dashboard_pdf` |
| F17 | Brand assets ≤ 512 KB, checked before the body is read (extension, size) and after (magic bytes / SVG without script); uploaded logo ⇒ mode custom; deleting the last logo ⇒ mode auditly; favicon = dark icon | `auditly_host.py:branding_asset_upload`, `_brand_urls`, `brand_asset` |
| F18 | Invite = EmailMessage with text + text/calendar;method=REQUEST + .ics attachment; refused 409 unscheduled, 400 without SMTP or agent email; relay errors are redacted and returned as 502; `coaching_invite` audit trail; `SMTP_SEND_AS_QA` optional | `mailer.py`, `auditly_host.py:session_invite`, `mail_test` |
| F19 | `.eml` draft = same message as the SMTP path with `X-Unsent: 1`, `From` only when a QA address is known, blank `To` allowed; 409 unless scheduled; `invite-sent` needs an address and writes `coaching_invite.channel='mail_app'` | `mailer.build_invite(draft=True)`, `auditly_host.py:session_eml`, `session_invite_sent` |
| F20 | In-app notifications: shared event rows (coaching scheduled/changed, dispute raised/resolved, agent below target, job failed), per-user muted kinds and read marks, unread counted from the account's creation; "agent below target" = `dashboard.agent_standing()` over `dashboard.ROWS_SQL` (target, `LEADER_MIN_N`, default week range), deduped per agent per week; call-backed rows cascade with the recording, job rows go with the job, 90-day sweep | `notify.py`, `auditly_host.py:api_notifications`, `worker.py:_notify_failed` |
| F21 | Emailed dispute link: 32-byte token, sha256 stored (`dispute_link`), one scorecard, `AUDITLY_DISPUTE_LINK_DAYS` (14; 0 = off, existing links refused too), minted and mailed at submit when the agent has an address and a relay is set, revoked at reopen, every unexpired link valid; the agent page is `to_public("agent_view")` — no audio, call text, customer, QA name or notes; manual Email / Copy / mail app from the scorecard; unknown, expired and revoked tokens are the same 404; the token never enters the audit log or the access log; `AUDITLY_PUBLIC_URL` or scheme+Host builds the URL | `auditly_host.py:serve_dispute`, `dispute_data`, `dispute_raise_public`, `_dispute_link_send`, `public_base`, `mailer.build_plain` |
| F22 | Focus mode: a header chip hides every call on Calls, Audit and Coaching; a set of recordings (`?recording=a,b` on `/api/jobs` and `/api/reviews`, `none` = empty set) is the only thing shown, Coaching filtered to their agents on the client; the search box overrides the mode while it has text so calls can be found and added; remembered in the browser (`auditly-focus` `{on, items}`, the 0.53.0 shape migrated) | `auditly_host.py:_id_list`, `api_jobs`, `api_reviews`, `auditly.html:focusToggle` |

## 7. Non-functional requirements

- Stdlib only; single HTML file; no external assets (CSP-enforced).
- Keys never reach the browser; `.env` mode 600; DB and uploads owner-only.
- `AUDITLY_ALLOW_SPEND=1` required for any paid call.
- A 10-minute call: ~30–90 s transcription + ~20 s scoring on current providers.

## 8. Architecture and constraints

Threaded stdlib HTTP server (`auditly_host.py`) + SQLite + one worker thread
(`worker.py`). Raw-body uploads (Python 3.14 has no `cgi`). The host has no
ffmpeg, so OpenAI STT is capped at 25 MB rather than transcoded. Everything the
browser sees passes through `to_public()`.

## 9. Deployment modes

- **Dev/demo:** `python3 auditly_host.py --demo` on 127.0.0.1:8084.
- **LAN link (real mode):** `./restart.sh` — real providers, bound to 0.0.0.0:8084, plain HTTP, no nginx, no sign-in (`AUDITLY_OPEN_ACCESS=1`), `@reboot` crontab; `http://qa-server.example.com:8084/auditly/`. `--seed-rubric` for first use seeds the rubric only; names come from Settings › Names, and nothing demo (demo runs, demo data, demo filters) is offered outside demo mode. Runbook §8.
- **Production:** systemd unit + nginx TLS reverse proxy on 8444 / `auditly.example.com`, installed by `deploy/install.sh` (human-run, sudo). Runbook §2.

## 10. Out of scope (v1)

PDF guideline extraction; audio transcoding/chunking; agent self-service
login; two-reviewer calibration; bulk export; automatic ingest from the PBX.

## 11. Roadmap

- **Shareable tour clip** — `tests/tour_record.js` records Show me around to a `.webm`; bundle it under `static/` (a `Handler.STATIC` entry with `video/webm` and Range support in `serve_static`) if the team wants a clip to send around. Decided 2026-09-22: the in-app tour teaches; a clip only travels.
- **v0.2** — ffmpeg transcode branch when installed; PDF guidelines via a human-approved dependency.
- **v0.3** — ~~per-agent trend view~~ (shipped in 0.9.0 as the Dashboard); bulk CSV export.
- **v0.10** — agent login: an `agent` role linked to an agent name, seeing only their own trend and the site average; classify-only backfill of sentiment and reasons for pre-v4 scorecards.
- **Assistant for agents** — Phase 3 of `docs/ASSISTANT_DESIGN.md`: the `agent` role (table rebuild), a user↔agent link, a session-derived scope on every read, narrowed fields, open access off and real accounts. Then Ask Auditly follows for agents. Phase 1 (QA, one call) shipped in 0.46.0.
- **v1.0** — after the QA team has scored real calls for two weeks and the schema stops moving.

## 12. Success metrics

- Reviewer time per call down from listen-length to < 5 minutes of review.
- Override rate < 20 % of criteria after the rubric's second version.
- Zero scorecards with unverifiable evidence (warnings list empty).

## 13. Risks and open questions

| Risk | Mitigation |
|---|---|
| Customer PII leaves the machine | Stated in README and UI; retention sweep; audit on play/export; TLS-only install |
| LLM inconsistency | Strict schema, temperature 0 + fixed seed (OpenAI gpt-4 family), optional "Optimise for the scorer" decision rules, server-side clamps, human override; `--eval-determinism` measures run-to-run agreement |
| "Tone of voice" expected from the transcript | Not available: Deepgram sentiment is computed from the transcript text after recognition, OpenAI STT returns nothing tonal, and the host cannot decode audio (no ffmpeg). Shipped instead (0.33.0): delivery metrics from the timings and opt-in per-line text sentiment, both labelled as coaching material, never scored |
| Reference material biases the scorer | Rule 13 confines it to accuracy of what was said; quotes still verified against the transcript; excerpts shown on the scorecard; retrieval capped and empty when nothing matches |
| OpenAI 25 MB cap | Deepgram default; refuse early with a clear message |
| Cost | Spend interlock; one scoring call per job; transcript char cap |

Open: Anthropic as a first-class second scorer? PDF guidelines needed? Hostname and TLS cert for production.

## 14. Build snapshot

- **Version:** 0.8.1 (2026-09-02)
- **Python:** 3.14, stdlib only
- **Files:** 12 modules/pages, 4 docs, 1 test suite, 5 deploy files, `restart.sh`
- **Tests:** `python3 tests/test_auditly.py` — 181 checks, all passing at v0.8.1
- **Ports:** 8084 app (0.0.0.0 for the LAN dev link; loopback under nginx); 8444 TLS — served by the app itself with a self-signed certificate since 0.49.0 (`AUDITLY_TLS_PORT`), to be taken over by nginx with a real certificate when `deploy/install.sh` is run
