# VERIFY — the test map

## 1. Headless, no keys (run this every time)

```bash
python3 tests/test_auditly.py
```

Boots `auditly_host.py --demo` on 8395 with a temp DB and upload dir (387 checks at v0.31.0). Covers:
public surface (health, changelog = README version, plan, CSP nonce, no external
asset, no native dialog), auth (401/403/429, rate limit, roles), the seeded job
(90.0 %, 8 items, evidence verified, agent speaker), overrides (recompute,
bounds, note required, clear), exports (PDF/CSV/JSON), media (200/206/416),
rubrics (weights≠100 rejected, immutable versions, extract sums to 100, docx,
pdf refused), uploads (201, bad ext, empty, 413 over cap, OpenAI > 25 MB, no
orphan files, dir 0700), rescore/retry/delete, reviews (queue filters, draft,
submit freezes 90.0, 409 on override/swap/edit while submitted, reopen,
resubmit, coached, CSV/PDF carry it), the hand-off (queue / un-queue / bulk,
stage on jobs, todo filter), call details (validation, audit-name rebuild, QA
lock while submitted), demo data (load 44 tagged DEMO, dashboard kind switch,
remove, reviewer 404), the review-table rebuild migration, sentiment + call reasons (schema, prompt
v4, validation), dashboard (buckets = series, distinct real calls, filters,
400s, `dashboard.py` unit cases), scoring maths unit cases, "Optimise for the
scorer" (save leaves a version un-optimised; precheck shape and caps; POST → done in
one demo attempt with five `opt_*` fields per criterion and the authored fields
untouched; second run 429; a rescore against it records `guidance_mode`
optimised and prompt v6; audit-again cache honours the mode; daily cap 429;
`validate_optimised` rejections; `build_prompt` fallback; DemoScorer optimise
branch; seed only where temperature is; pre-0.28.0 migration reads 'none';
`--eval-determinism` smoke; in the real-mode suite a bad key is refused before
anything is queued and the key value never appears), admin, audit, and
the serialization backstop (no secret/path/hash in any body).

```bash
grep -n 'src="http\|href="http' auditly.html     # must print nothing
```

The suite also runs `node --check` on the page's inline script when `node` is on
the PATH (it is on the dev box). A syntax error there shows as a blank page in the
browser and nothing else can catch it on a host without a browser. If node is
absent the check is skipped and the suite says so.

## 2. Live smoke (keys present, costs money)

1. `.env`: `AUDITLY_ALLOW_SPEND=1`, a Deepgram key, an OpenAI key.
2. `./restart.sh` (real mode) or `python3 auditly_host.py`, upload a ~1-minute real mp3 with Deepgram.
3. Watch the Calls tab go queued → transcribing → scoring → done; open the scorecard.
4. Repeat with OpenAI on a < 25 MB file; confirm the transcript has no speaker labels and the scorecard still renders.
5. Settings shows `key set (N chars)` and never a key.
6. Settings › QA types → press **Optimise** on the current version (one call, ~1¢), then
   `python3 auditly_host.py --eval-determinism latest --runs 3` (6 calls, a few cents, nothing
   written). Keep relying on the optimisation only if the verdict is MORE or EQUALLY consistent.

## 3. Browser walk-through (cannot be done headlessly on this host)

- Drag two files onto the drop zone; both appear as Ready with a provider select; pick OpenAI on one, Start all;
  both progress bars complete; both appear in Calls. The flowchart highlights the provider last chosen; the
  version chip pop-up has a Flow tab with the same diagram. Flow: all eight steps are visible at once in a dialog
  shaped to the chart (no empty band); resize the window or press Ctrl+/− and it re-fits; a narrow or portrait
  window draws it top-to-bottom; after + / − the Fit button returns to the full view.
  The chart has Mermaid's standard look (lavender boxes, grey arrows) and switches to Mermaid's dark theme with
  the Dark toggle; the browser console shows no CSP errors when the dialog opens.
  The dialog spans almost the whole window; Full screen (or F) fills it edge to edge and Fit re-runs.
  Every label sits inside its box (none clipped at the right edge); the step titles do not wrap over the boxes.
- Version chip opens the modal; Changelog tab lists `0.1.0 — 2026-09-01`; Plan tab renders tables.
- The section tabs sit in their own bar under the logo header, large, active one filled blue, sticky on scroll.
- Theme Light/Dark toggles and persists on reload (no Auto); the COEO logo in the header swaps with the theme;
  colours are the COEO navy/blue palette.
- Dashboard: the pipeline strip shows Recorded → … → Coached with counts; clicking Awaiting audit opens Audit › To audit.
- Scorecard: the Criteria table stays inside its card at 1400 px and 1100 px wide (scrolls inside the card if
  narrower); while a job runs, the job page shows the step tracker, spinner and moving bar, then "Loading
  scorecard…"; the uploads panel row moves queued → transcribing → scoring → done with the score.
- Scorecard: "Audit again with…" and "Transcribe again with…" open a Start/Cancel box naming the choice and cost;
  Cancel queues nothing; Start adds a new run and opens it; the header names the engine and the model.
- Upload the same file twice: the second row says "same audio as … — transcript reused" and finishes without a
  transcription call; Audit again with the same model offers "Open existing"; Settings › Server shows the Spend tile.
- Scorecard with AUTO-FAIL: the header explains which critical criterion caused it; overriding that criterion above 0
  clears the flag and recomputes the overall.
- Scorecard: the Criteria card sits full-width below the summary/transcript row with no sideways scroll at 1400 px
  and 1100 px; at phone width the rows stack.
- Scorecard: ring colour matches band; clicking an evidence quote seeks the audio and highlights the utterance;
  the Speakers legend re-labels the transcript; override dialog validates 0–weight and requires a note; PDF downloads.
- Settings has inner tabs QA types · Names · Knowledge · Server · Notifications · Account; nothing rubric-related is in the top bar.
- Notifications (0.51.0): signed in, the circle-with-exclamation chip sits between the theme switch and your name; in
  demo it is red with a count (the seeded history puts an agent below target). Press it → the dropdown lists the
  events, newest first; Escape and a click outside close it. Click the agent line → the Dashboard opens filtered to
  that agent and the count drops by one. On Coaching, schedule a session → the chip count rises (at once, and within
  a minute on another browser); reschedule it → the new line says "Was … → now …". Settings › Notifications: untick
  Coaching session changed, Save → those lines leave the dropdown; a second browser signed in as a reviewer keeps
  its own count and its own ticks. Mark all read → the chip goes grey.
- Upload: drop a file whose name was uploaded before — the row turns amber with one line, "Uploaded before"; its
  details toggle names the earlier audit, says whether it was audited and offers *open it*; Remove takes it off the
  list; Start still works. A finished row that reused a transcript says so in one line with the same toggle, and
  shows no engine pill.
- Scorecard: the sentiment banner under the head is coloured by the overall mood; the Speakers legend
  lists S0/S1 with role selects (Caller · Agent · Voice AI · Other) and changing the agent role changes
  who is scored; a Transferred call pill appears when flagged; the Customer card shows what the scorer
  read from the call and stays editable; **Edit QA type** opens the editor.
- Rescore with *Carry over* ticked: the new run shows the same override and a draft review with the notes.
- Upload with **Run as** set to Demo run completes with no keys and shows a DEMO pill; Calls hides it until the
  filter is set to Demo runs or All; Settings › Names › Remove demo names empties the seeded names.
- Settings › Names: add a name, it appears in the Upload dropdown; hovering a `?` shows
  its tooltip; the Audit name fills from the agent and today's date and gains the customer's company and
  name once the call is scored; the downloaded PDF is named after the audit.
- Help › About lists the creator and contributors with LinkedIn links, who it is for, the problem it solves, the
  principles, how it works and data/cost; switching back to How to use restores the Go there buttons, which still jump.
- Help chip opens the guide over any tab; each Go there lands on its tab (QA type → Settings › QA types); Show the Flow
  chart closes it and opens the flowchart; in a browser that has never signed in it opens by itself once after sign-in and
  never on the login screen (clear site data to see it again); Escape and Close dismiss it.
- Hover Light/Dark, Sign out, the QA type box, the drop zone, a status pill, an archived pill, a critical pill, a Dashboard
  period button, a QA-type editor field and the Optimise button: each shows a one-sentence tooltip; plain Close and Cancel
  buttons show none.
- Settings › QA types: paste `fixtures/demo_rubric.md`, Extract, total shows 100 in green; change a weight, Save disables;
  Normalise to 100 re-enables; Save creates v1; open the QA type → Edit creates v2 with a "What changed" note;
  the list shows "Last edited" with your email; Save as new QA type pre-fills the criteria with a blank name.
- Optimise for the scorer (demo mode is free): Save a version → the card stays and shows "Saved vN — not yet
  optimised" with the **Optimise for the scorer** button, runs left (1 of 1 · 5 of 5) and ≈ $0.000; the button reads
  Saving… and is disabled during the save, and **the confirm pop-up opens by itself** ("vN saved — optimise it for the
  scorer now?", Optimise / Not now). Not now → the card with the button; pressing Optimise later, or Optimise from the
  list or the version modal, opens the same dialog but does not auto-pop. With spend off the pop-up is a notice saying
  why it cannot run. Optimise → the dialog names the model, cost and runs left → the strip
  shows ✓ Saved → ⟳ Optimising → Ready with a moving bar and a shimmering "Rewriting N criteria…" line with a seconds
  counter → within a moment it goes green, a toast says it is optimised, "Show what changed" opens a two-column As
  written / Optimised table (text with `<`, `&`, quotes shows literally). Back: the list shows a green "optimised" pill;
  a second version shows amber "not optimised" plus the list banner and an Optimise button; pressing Optimise twice on
  one version is refused with the 1-run message. Open a version → pill and (for none/failed) an Optimise / Retry button.
  Upload tab: a muted note under the QA type box when the chosen version is not optimised. Scorecard of a call audited
  against an optimised version shows an "optimised rules" pill next to "scored by". Real mode with `AUDITLY_ALLOW_SPEND=0`:
  the button is disabled with the reason and POST is refused; reduce-motion: no spinner, static bar, plain grey text;
  Tab reaches every button; switching tabs stops the 1.5 s polls (Network tab quiet).
- Coaching › Email invite: Open in my email app downloads coaching-invite-<date>-<agent>.eml; on a Windows PC with Outlook,
  double-click → a new unsent message from your account with coaching.ics attached and the agenda as text; Send it, back in
  Auditly press Mark invite as sent → the session and the calendar show ✉ (from your email app). Cannot be exercised on
  the server host (no Outlook): the file's headers are checked by the test suite instead.
- Coaching › Calendar › Week: a time grid; click a free slot → New session opens and, once created, the Schedule dialog
  is prefilled with that day and time; the session shows ✉ after Send invite (with SMTP set; Settings › Server › Mail ›
  Send test first) and ✓ after Mark delivered; the week summary and the month view's By week table follow.
- Header logo: reload any page — the letters of "auditly" land one by one, then the orange tick drops and is
  stamped (about 2.4 s). Switch Light/Dark and it replays in the other colourway; the sign-in page shows it too.
  With the OS set to reduce motion the finished wordmark appears at once, with no movement at all.
- Next step bar: under the tabs, one sentence and one button. On Upload with nothing in flight it says to drop a
  recording; open a scored call and it offers to start the audit, and the button opens that call; start the audit
  and it offers the audit queue; submit the score and it offers to coach that agent, landing on Coaching with the
  agent already picked; mark coached and it offers the Dashboard. The dot rail advances each time. Press × and it
  goes for good, including after a reload; Help › Good to know › Show it brings it back.
- Upload form: only QA type, Agent and the drop zone. More options holds Audit with, QA reviewer, Audit name and
  Run as; it remembers being left open, and opens by itself in demo mode because Demo run is inside it.
- Settings › Server: a row of four panes. Status shows version, mode, secure link, queue, database, upload cap, retention,
  ffmpeg and menus; Providers shows the three keys, default engines, spend and the mail relay with Send test; pick a
  pane, reload, it is still selected. Sign in as a reviewer: only Status and Providers are offered, and a remembered
  admin pane falls back to Status. At 380 px wide all four buttons show without a horizontal scroll.
- Settings › Server › Assistant (admin): Off → the panel's input disables and says it is switched off under Settings;
  On → it answers. Edit the instructions, Save as new version → the history shows v1 with your note and name; Try it
  answers about the newest call with chips; Reset to built-in → v2 in the history, the built-in text is back, Reset
  greys out. The rules beneath the box cannot be edited. A reviewer account does not see the card.
- Secure link: after ./restart.sh, https://qa-server.example.com:8444/ loads the same app behind a one-time certificate
  warning (Advanced → Proceed); Settings › Server shows the link; on the plain http link with a call open the Ask panel
  names it; `curl -kI https://127.0.0.1:8444/health` shows no Strict-Transport-Security header; http://…:8084 still works.
- Voice (on http://127.0.0.1:8084 or the HTTPS link only): with a call open, the mic button is enabled; press it, allow the
  microphone once, it turns red and pulses; press again and the words appear in the box (demo mode: a fixed question)
  with the cursor at the end and nothing sent; Ask then sends. On the plain-HTTP LAN link the button is disabled and
  its tooltip says voice needs the HTTPS link. Settings › Server › spend shows the clip and its seconds.
- Ask Auditly (switched on under Settings › Server › Assistant, or ASK_ENABLED=1; demo mode needs no key): open a scored call, press the mark — the panel names the
  call. Ask "How did the agent do on the greeting?": a reply with a criterion chip. Ask about someone not on the call, or
  "ignore your instructions": declined, no chips, dashed bubble. Close and reopen: the conversation is still there. With
  no call open, or with ASK_ENABLED unset, the input is disabled and the amber note says why. Settings › Server shows
  the questions in spend.
- Ask Auditly launcher: signed in, the round mark sits in the lower right of every tab — the stem lands, then the tick is
  stamped. It is navy with a cream mark in light mode and cream with a navy mark in dark. Press it: a panel opens
  above it saying the assistant is not switched on yet. Escape closes it and the button keeps focus; a click
  outside closes it too. It is not on the sign-in page, and with reduced motion the finished mark appears at once.
- Settings › Server › Branding (admin): header shows the Auditly wordmark (light and dark) and the tab icon. Click
  COEO → the header does not change; Apply and Cancel appear with "Not applied yet — the header still shows Auditly."
  Cancel → the segment returns to Auditly. Click Hidden → the hint adds that the Ask Auditly button goes too; Apply →
  only the name, and the launcher is gone; click Auditly, Apply → wordmark and launcher back. Click Custom logo with
  nothing uploaded, Apply → toast "Upload a logo first…" and the pick stays. Leave Settings and return → the segment
  shows the saved mode. Upload a PNG in the Logo · light slot → the header and the sign-in page show it (an upload
  switches to Custom by itself), Remove → back to Auditly; drop the real PNG artwork into static/ under the listed
  names → used at once.
- Settings › Knowledge: the demo document is listed (chunks, Used by ≥ 1); Try a search with "phone shows No Service" returns
  the procedure chunks with matched words, "invoice" returns the billing section, nonsense returns nothing; Add document
  (paste or .docx) → appears, toggle Off → excluded from the search; the demo scorecard's Reference material card lists
  the excerpts the scorer saw. An existing demo DB's old card has no card until audited again.
- Coaching tab: Agents lists Jordan, Priya and Marcus with To coach counts; New session → pick Jordan, switch the scope
  to All scored (un-audited calls appear with a grey AI score), tick two calls → the tally under the list shows the count,
  average, lowest, misses and the criteria that lost most points and the button reads "Create session · 2 calls"; the
  session opens with a Totals card and an Average footer, and the agenda opens with the same totals; Schedule → tomorrow 10:00, 45 min → the row shows the local time, Add to calendar (.ics)
  downloads a file that opens in a calendar app, Email invite appears once Jordan has an email under Settings › Names;
  Calendar sub-tab shows the session on its day and ⏰ on follow-up dates; Mark delivered → the calls show coached in
  Audit and the Dashboard's coaching count rises; Reopen undoes it. Scorecard: ⚑ on a criterion records a dispute,
  the Disputes card offers Uphold / Reject / Withdraw; **Dispute link** (0.52.0, submitted audits only) → Create and
  copy link → open it in a private window: the agent's page shows the scores, reasons and quotes, no call text or
  customer; Dispute this score → say why → Send → the app's Disputes card says "from the emailed link" and the bell
  lights; Reopen the audit → the same page says "expired or not valid"; resubmit with an address under Settings ›
  Names and a relay → the agent is emailed a fresh link and the submit toast says so; the Tone & delivery card shows the timing tiles and the
  caller/agent sentiment strips above the transcript (demo call).
- **Customer information, Details, rename, samples (0.61.0)**: open the demo call — the card reads Customer information
  (Captured) with Phone and Ticket number rows (— on the demo call); ✎ edits all six rows and Save keeps them; ✎ beside the
  title renames the call and the Calls row follows. Under Call summary, Details opens to Extension 101, Device front desk
  phone, four troubleshooting steps and two discussed items with Troubleshooting / Resolution / Closing pills; a pill
  scrolls to that criterion's row and highlights it. Settings › QA types › L1 Support › Edit: every criterion has a
  Good sample / Bad sample line (greeting, verification, troubleshooting and closing filled); Save as a new version keeps
  them and the versions view shows them.
- **Playback (0.60.0)**: open the demo call, press play, click a Calls-tab button (Audit, Dashboard…) — the recording
  keeps playing in the bar at the foot of the page with the call's name; ⟲10 / ⟳10 move it; Open call returns to the
  scorecard with the player back in the Recording card, still playing; × stops it. Settings › Preferences › Recording
  playback › Stop, then the same tab change pauses it. In Audit, open a review, play, save an override and Submit —
  the audio never stops. ⟲20 ⟲10 ⟳10 ⟳20 beside the player skip; the speed menu changes the rate.
- **Optimise and coaching animations (0.63.0)**: Settings › QA types › Edit › Save → Optimise: the card shows the thinking dots
  beside the shimmer while it runs and, when it lands, the tick stamps in beside "Optimised in …" (optimised for the scorer).
  Coaching › a session with an audited call › Mark delivered: the status line gets the tick (coaching delivered) and each
  coached row draws its tick in turn; Audit › tick Coaching delivered → a tick beside "yes". Animations Off → none of it moves.
- **Animations (0.60.0)**: Settings › Preferences › Animations Off → open a call from Calls: no motif, the ring is
  drawn at once, no tick stamps; the wordmark at the top is the still one after a reload. On → back. Auto follows the
  OS setting.
- **Show me around** on a real server (0.56.0): with no demo calls loaded the walk opens on "Use sample data?"; as an
  admin, Load sample data and start → the Calls, Audit and Coaching kind filters read Demo and the Dashboard's Calls
  switch is on Demo, every row carries the Demo pill, the scorecard stop opens a demo call; Done → the filters are
  back to Real. As a reviewer the card says to ask an admin; Continue with real calls walks the real data. With demo
  calls already loaded the walk starts at once on Demo.
- Help › **Show me around** (0.54.0), in light then dark: Help closes and Upload's card is spotlit with the callout
  beneath; Next walks Calls, Processing (the tracker goes Transcribing → Scoring by itself, then the scorecard
  lands with its ring and tick, scrolled into view, and the callout says "There it is"), the DEMO-0001 scorecard, Ask Auditly (the dock
  opens over the sample call with "About: …"; Next closes it), Audit › To audit, Coaching's Agents
  card, the Dashboard KPIs in Executive view, Settings' row, then the finish card; ← → mirror Back and Next; Escape
  leaves the tour on the screen it reached and focus returns to Help; nothing under the overlay reacts to clicks;
  the callout is never over the spotlit element — on Audit and the scorecard it docks along the bottom, the
  spotlight ends above it and the page can be scrolled so the last rows come up above the card (0.59.1);
  resize the window mid-stop and the spotlight follows; narrower than 700 px the callout docks to the bottom; with
  the notification or Ask panel open, starting the tour closes them; on a real server with no calls each stop says
  "Nothing here yet" and the Scorecard stop is a centred card; with reduced motion on, nothing glides. Nothing starts
  by itself: clear site data, sign in, and the first-sign-in Help only offers it. To record it:
  `node tests/tour_record.js http://127.0.0.1:8084 --shots` against a demo server with Playwright installed;
  regenerate per release if a clip is wanted.
- Focus mode (0.55.0): press the Focus chip at the top with nothing in focus → Calls, Audit and Coaching are empty and
  the bar says to search for a call; type in the Calls search → rows appear; Focus two of them; clear the search → only
  those two remain, the bar lists both with ×; Coaching shows only their agents and sessions; reload → still so; × on
  one chip drops it; Show all → everything back and the chip goes plain.
- Focus (0.53.0): on Calls press Focus on a row → the list shows only that call ("1 call · focused"), a bar under the
  tabs names it; switch to Audit → only its reviews; reload → still focused; the scorecard's Focus button reads Show all;
  Show all on the bar → every call returns. A focused demo run stays visible with the Real filter set.
- Audit tab: the queue lists the demo history calls with sentiment and reason pills and a review state; filters
  by state / agent / QA work; open a call — the QA review card sits above the scorecard with a provisional ring;
  Save draft keeps the notes; Submit final score asks first, then the ring says "final", ✎ and Swap are disabled
  with a tooltip, and the pill reads "audited · final N%"; Reopen asks first and unlocks them; ticking Coaching
  delivered works while submitted; the Calls scorecard shows the same pills and an "Audit this call" button.
- Motion (0.58.0): on Upload, drop a file with Run as Demo run and Start → the row shows the rising arrow, then the
  sound wave, then the thinking dots, then the tick; open it in Calls while it runs → the tracker shows the same motifs
  and when it lands the ring draws in with a "pre-audited by AI" tick for a few seconds; in Audit, Submit → the final
  ring draws in and the orange tick stamps beside it. With reduced motion on, nothing moves and every value is simply there.
- Dashboard › Executive on the demo history: pass rate and coaching coverage near 90 %, Jordan leading, Marcus lowest.
- Dashboard › Executive (default): six KPI tiles with ▲/▼ change since the previous period, the trend against the
  target, Where calls earn points (bars coloured by band, strongest first, "pts/call to gain" on the right), score bands
  (90 and above first), sentiment shift, Coaching and disputes tiles (Ready for coaching), Agents with Leading and Most
  room to grow (demo history: three agents, Marcus lowest, the largest rise marked most improved); Critical checks
  passed instead of an auto-fail rate; a dip shows amber; Export PDF downloads a one-page summary
  for the same filters; Detail switches back and is remembered on reload.
- Dashboard › Detail: tiles, a line chart with a dotted 90 % target, hover a point for the value; By agent shows three
  lines plus the dashed site average and the By agent table; By QA swaps the table; Day/Week/Month/Quarter/Year
  and the range presets redraw; Custom dates work; Audited only lowers the counts; the sentiment stack, reasons
  bars and the audits-per-QA bars render in both themes with no hard-coded colour.
- Calls: tick two scored calls → "Start audit · 2 calls" appears → pick a QA → both rows show "queued for
  audit" under their status and the Audit tab's To audit sub-tab has them, count 2; a row's Start audit
  button does one and lands in its review in the Audit tab; Open review jumps to the Audit workspace; the
  3-second poll while a job runs does not untick boxes.
- Audit sub-tabs: To audit offers All / Queued / In review; submit a review → the call leaves To audit and
  appears under Completed (count 1), whose filter offers All / Audited / Coached; mark coached → Coached
  lists it, Audited does not; Reopen → back under To audit. No option anywhere lists a call that was never
  started.
- Audit workspace: "Open call in Calls" lands on the job; Call details → change the agent → Save details →
  the header and the Calls row show the new agent, the audit name rebuilt; a blank audit name is rebuilt;
  while submitted, changing the QA of record is refused with a message. Remove from queue works on a queued
  row and disappears once anything is saved.
- Settings › Names › Demo data (admin): Load → count shows 44 → Dashboard Calls: Demo shows three agents
  and the Audit tab (kind Demo runs) has queued/in-review calls under To audit; Real stays as before; Remove → gone.
- No native dialog appears anywhere.

If step 3 was not performed, say so.
