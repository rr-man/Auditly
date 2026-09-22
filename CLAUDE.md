# CLAUDE.md — working notes for this repo

An internal QA tool: upload a support-call recording, transcribe it, score it
against a weighted rubric, show a scorecard with Opportunities and Misses.
**`README.md`** is the user-facing description; **`docs/PRD_Auditly.md`**
is the rationale; **`docs/RUNBOOK.md`** is operations. This file is only about
how to change the code without breaking it.

Stdlib Python 3 only. No `package.json`, no build step, no dependencies.

## Layout

```
auditly_host.py    HTTP server, routes, to_public(), upload, exports, CLI   (~900 lines)
core.py         .env loader, SQLite, scrypt, sessions, audit, parse_changelog
worker.py       job pipeline: queued → transcribing → scoring → done|failed; retention
transcribe.py   Deepgram / OpenAI / Demo → {utterances:[{start_s,end_s,speaker,text}]}
llm.py          the ONE outbound HTTP helper + OpenAI/Anthropic/Demo scorers
score.py        prompt, JSON schema, validate_result(), compute_overall(); SENTIMENTS, REASONS
dashboard.py    pure aggregation for /api/dashboard: buckets, one value per recording, series
rubric.py       text_from_upload (.txt/.md/.docx), normalise(), validate(), rescale();
                OPTIMISE_SYSTEM + optimise_criteria() + validate_optimised() ("Optimise for the scorer")
rubric_sync.py  the manual optimisation runner: readiness()/caps() gate, one queue + thread,
                none → pending → running → done|failed, 3 retries per run; evaluate() for --eval-determinism
pdfgen.py       copied verbatim from ~/coeo-transcripts
tone.py         tone & delivery from utterance timings (+ Deepgram per-line sentiment, opt-in); never scored
coaching.py     coaching sessions: agenda draft, .ics event, local-time parsing, overlap check (pure)
mailer.py       the ONE place SMTP is spoken: coaching invites (text/calendar; method=REQUEST) and the agent's
                dispute link (build_plain) via the .env relay; password never in a message, log or exception
assistant.py    Ask Auditly: one call's record as fenced context, a cited answer through llm.complete_json
                (ASK_SCHEMA), uncheckable citations dropped; its OWN ASK_PROMPT_VERSION -- never score's, which
                is in the audit-reuse key. Answers only, never writes. Design: docs/ASSISTANT_DESIGN.md
                The prompt is PERSONA_DEFAULT (admin-editable, versioned in ask_prompt via Settings › Server ›
                Assistant) + RULES (locked, always appended by system_prompt()); ask_setting overrides ASK_* .env keys
kb.py           knowledge base: paragraph chunks + BM25 retrieval with the transcript as query; excerpts go
                to the scorer under SYSTEM rule 13; kb.fingerprint() is part of the identical-audit key
notify.py       in-app notifications: shared event rows (emit), per-user muted kinds + read marks (feed, prefs,
                mark_read); "agent below target" = dashboard.agent_standing() over dashboard.ROWS_SQL -- never a
                second threshold; runs plain statements on the caller's connection, no transactions, no mail
demo_data.py    --demo seed (sample call through the REAL pipeline); --seed-rubric for real mode
schema.sql      read the comments at the top before adding a column; `review` = the human QA
                audit (coaching, final score), `audit` = the security log — do not confuse them; `dispute` = the
                agent's objection (raised from their emailed link, or recorded by the QA); `dispute_link` = the
                hashed token behind /dispute/<token>; `coaching_session(_call)`; `kb_document/kb_chunk`
auditly.html   the whole front end
fixtures/       demo_call.json (with per-line sentiment), demo_rubric.md, demo_scorecard.json, demo_kb.md, tone.wav
static/         mermaid.min.js (Mermaid 11.4.1, MIT) — the only third-party code — the COEO logo
                (coeo_light.svg / coeo_dark.svg) and the Auditly artwork: the wordmark
                animates on load (auditly_light/dark.svg, CSS keyframes inside the file), with a motionless
                twin (*_still.svg) the page picks under prefers-reduced-motion, and static tab icons cut
                from the same paths; the square mark on the Ask Auditly launcher (auditly_mark_<theme>.svg,
                same stamp, same *_still twin); a PNG under the same stem wins; uploads live in brand_asset.
                The suffix is the THEME the file is SHOWN IN, not the background it was drawn on — the
                launcher inverts, so auditly_mark_light.svg carries cream ink for its navy button. Served
                same-origin at /static/ with a long immutable cache, loaded with the page nonce.
                **Changing the artwork in place is not enough**: /static/ is served
                `public, max-age=31536000, immutable`, so a browser that has been here before never asks
                again. Every built-in brand URL therefore carries `?v=<build>` (`_brand_urls()` and the
                page's `AV`), which is what retires the old copy — 0.43.1 was that fix, after the real
                logo sat unseen on machines that had loaded an earlier build. Anything that looks a file
                up from one of those URLs must read past the query (see `brand_asset`).
tests/test_auditly.py   700 checks against a real server, no keys
tests/tour_record.js    records Help › Show me around to a .webm with Playwright (not part of the suite; node + playwright needed)
deploy/         nginx + systemd + install.sh (run by a human with sudo)
```

## Invariants

1. **`to_public()` is the only thing that turns a row into a response body.**
   It is a whitelist. `recording.path`, `sha256` and every `.env` value have no
   path out. A hand-rolled `json.dumps(dict(row))` anywhere in the request
   path is a bug. The test `no secret, path or hash in any response body` is
   the backstop. The agent's page `/dispute/<token>` is `DISPUTE_HTML` served by
   `serve_dispute` with the same nonce CSP; its data is `to_public("agent_view")` and
   never `_scorecard_full`, which carries the call text.
2. **Keys never leave the server.** `/api/health` reports `key_present` and
   `key_len` only. `llm.request()` puts keys in headers, never URLs, and never
   includes them in an exception message.
3. **The model's arithmetic is never trusted.** `score.validate_result()`
   clamps every score to `[0, weight]`, forces met→weight and missed/na→0,
   drops quotes it cannot find in the transcript, and `compute_overall()` is
   the single place an overall percentage is calculated — it runs again after
   every override. `applicable_weight` = 100 minus the weight of `na` items.
4. **A rubric version's authored criteria are immutable.** Editing creates a new `rubric_version`;
   `criterion.opt_*` (the optimised rules) is derived and may only be rewritten while no
   scorecard on that version has `guidance_mode='optimised'`; `POST …/sync` refuses otherwise.
   Optimisation is manual and capped (`AUDITLY_SYNC_MAX_RUNS_PER_*`); nothing runs at boot or on save.
   `scorecard.rubric_version_id` pins the criteria that produced it.
5. **Weights sum to exactly 100** — `rubric.validate()` refuses otherwise, and
   the UI disables Save. `rescale()` is only used on the LLM's *proposal*.
6. **Nothing external in the HTML.** No CDN, no web font, no remote image. The
   CSP served with the page forbids it; a test asserts it. A plain `<a href>` to
   another site is navigation, not an asset, and is fine (the footer credit is one).
   Third-party code is vendored under `static/` and served from this server
   (Mermaid for the flowchart). The CSP is nonce-only for styles too, so a
   library's `<style>`/`style=""` output is refused as parsed. The flowchart
   keeps Mermaid's own look by re-creating its `<style>` as a script-made
   element carrying the page nonce and re-applying each `style=""` through the
   CSSOM (`el.style.cssText`) — both allowed; nothing is fetched.
7. **No native dialogs.** Never `alert`, `confirm` or `prompt` — every
   confirmation is drawn in-page (`modal()`). Tested.
8. **`AUDITLY_ALLOW_SPEND=1` gates every paid call.** `--demo` never needs it.
   Uploads are refused (400) when it is not set so the queue cannot fill with
   jobs that will fail.
9. **Uploads are raw bodies, not multipart.** Python 3.13+ removed `cgi`; the
   browser sends `file` as the body with metadata in the query string. Every
   validation happens *before* the body is read.
10. **`AUDITLY_OPEN_ACCESS=1` makes every visitor `open-access@local` (admin)** with
    no sign-in; `deploy/install.sh` refuses to install with it set. Demo/LAN only.
    A real session still wins in `current_user()`.
11. **Never send Strict-Transport-Security from Python.** Since 0.49.0 the app serves its own HTTPS
    twin on `AUDITLY_TLS_PORT` (restart.sh: 8444, self-signed into `tls/`) beside plain 8084. HSTS is per
    host and ignores the port, so one such header would make browsers refuse `http://host:8084` for a
    year. nginx (`deploy/`) sends it, correctly, because under nginx the plain link is gone. Tested.
12. **Never `pkill -f serve\.py`, and never name anything here `serve.py`.**
    That pattern took a neighbouring tool down for 19 hours on 2026-08-19.
    Also: any shell whose command line contains the literal text
    `auditly_host.py --demo` will match a `pkill -f` for it — kill by port
    (`fuser -k 8084/tcp`) or via systemd instead.

## Providers

- OpenAI STT has a 25 MB cap, enforced at upload; the endpoints and parameters
  are in `transcribe.py` and `llm.py`.
- `AUDITLY_DEMO=1` swaps every provider for the Demo fakes.

The scorer identifies the agent (`agent_speaker`); there is no greeting
heuristic. The reviewer can swap it in the UI.

## Verifying a change

```bash
python3 tests/test_auditly.py
```

Boots the server in demo mode on 8395 with a temp DB and upload dir. If you
touch scoring, the `scoring maths` block is where to add a case; if you touch
routes or `to_public()`, the serialization backstop at the end.

**Rendering cannot be checked headlessly** — there is no browser on this
host. Run `python3 auditly_host.py --demo`, open it in a browser and exercise
the path you touched. If you did not, **say so** rather than implying it was
verified. `docs/VERIFY.md` has the walk-through.

## Always update the changelog

Every user-facing change ships with its own new version and its own dated
entry, in the same edit. Bump the patch/minor number from the top entry of
`docs/CHANGELOG.md` (minor for a feature, patch for a fix), write bullets with
a real em dash, and update the `**Version:**` line in `README.md:3` and the
header of `docs/PRD_Auditly.md`. The version chip reads the changelog
at request time, so there is no fourth place. Changes to this file are the
exception.

**The Flow chart is part of the UI it describes.** Any change to a pipeline step, to a menu the
chart reads (`stt_choices`, `scoring_choices`), or to a QA-type or scorecard action (save,
optimise, re-run) must update `flowSource()` in `auditly.html` in the same edit, along with
the strings pinned by the flowchart checks in `tests/test_auditly.py` (they cross-check the chart
against the menus, the re-run buttons, the Settings sub-tab and the step count in the docs).

## Secrets

`.env` holds provider keys and the session secret; `auditly.db` holds password
hashes, live session tokens and customer call content; `uploads/` holds the
audio. `.gitignore` covers all three, but an archive is covered by nothing
except attention — this is the kind of folder people zip up and hand over.
