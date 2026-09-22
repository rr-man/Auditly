# Changelog

All notable changes to Auditly (built as L1 Support QA). Format follows Keep a Changelog.
The top entry's version is what the app shows in its version chip.

## [0.63.2] - 2026-09-22

### Added
- **MIT licence.** `LICENSE` (Copyright (c) 2026 COEO (SNET Connect) — Ron Mangune and contributors) and
  `THIRD_PARTY_NOTICES.md` — Mermaid 11.4.1 under its own MIT notice, the COEO logo as the company's
  trademark artwork outside the code grant, and a statement that the sample data is fictional. The README
  points at both; the repository-hygiene tests check they are present.

## [0.63.1] - 2026-09-22

### Changed
- **Ready for a public repository.** Internal hostnames, the LAN address and absolute home paths are gone from
  the files that ship: `restart.sh` finds its own folder and names the host from `hostname -f` (or
  `AUDITLY_HOST`), the systemd unit carries `__APPDIR__` / `__USER__` placeholders that `deploy/install.sh`
  fills in, the nginx site and the docs use `qa-server.example.com` / `auditly.example.com`, and the call-ID
  example is a made-up PBX file name. `.gitignore` also catches two stray names an old mis-pasted `.env` line
  created (a second uploads folder and an empty database file) and the sample tour clip. The test suite now
  checks all of it: no key-shaped string, internal hostname, LAN address or home path in any committable file,
  the ignore rules present, and — once the folder is a git repository — every secret and data path ignored
  and none tracked. Nothing on the machine moved: `.env`, the database, the recordings and `tls/` stay where
  they are, unversioned.

## [0.63.0] - 2026-09-22

### Added
- **Optimising, animated.** While a QA type is being optimised for the scorer the status card shows the same
  motifs a call does — the rising arrow while queued, the thinking dots while the rules are being written —
  and when the page watches it land, the Auditly tick stamps in beside "Optimised in …" with the caption
  *optimised for the scorer*.
- **Coaching delivered, animated.** Mark delivered stamps the session's status with the tick (*coaching
  delivered*) and each call that was just marked coached draws its own tick in, one after another; the
  Audit tab's coaching-delivered mark draws its tick too. All of it stands still under Settings › Preferences ›
  Animations Off or the system's reduce-motion setting.

### Fixed
- The walk's Ask Auditly stop reopens the sample scorecard behind the dock when the Calls list was showing.

## [0.62.0] - 2026-09-22

### Added
- **Ask Auditly on the map.** The Flow chart has an *Ask Auditly* node beside the scorecard — asked from the
  scorecard and from the Audit workspace — with what it does and its one limit: it never changes anything.
  Show me around gains a stop after the scorecard: the walk opens the dock over the sample call, explains what
  it answers and from where, suggests a first question, and says so when the assistant is switched off on the
  server. Nine stops now; the Help dialog's Good-to-know list names it too.

## [0.61.0] - 2026-09-22

### Added
- **Customer information (Captured).** The scorecard's Customer card is now *Customer information (Captured)*:
  name, company, email, **phone**, **ticket number** and call ID, all captured from the call by the scorer and
  all editable in place and on the Audit tab's Call details card. The row that read "Reference" is the
  **Ticket number**; the scorer now takes the ticket, case or reference number quoted by either party — the
  agent reading one out counts — and the recording remembers phone and ticket like the other details, so a
  correction sticks. CSV and PDF exports say "Ticket number".
- **Call summary › Details.** A details toggle under the call summary lists the checkable facts of the call
  as heard — extension, phone, MAC address, ticket, account, device, address — the troubleshooting steps in
  order with what each showed, what was discussed or agreed, and the outcome. Deterministic by construction:
  the server keeps an identifier only when its characters occur in the transcript (case and separators
  ignored) and drops the rest with a note, so nothing is invented. An item that speaks to a criterion carries
  that criterion's name as a highlighted pill; click it to jump to the row. The facts are in the CSV and PDF
  and Ask Auditly can answer "what was the extension?" from them.
- **Rename a call** — ✎ beside the title on the scorecard (Calls and Audit) renames the call everywhere;
  blank rebuilds the name from agent, company, customer and date.
- **Good and bad samples per criterion.** The QA type editor has a second line under Met / Partial / Missed:
  a *good sample* (a line that earns Met) and a *bad sample* (a line that earns Missed). The scorer reads them
  as calibration — a turn that matches the good sample in substance is met, one that matches the bad sample is
  missed — never as required wording; Extract criteria copies sample phrases the guidelines give, the optimiser
  reads them so its rules agree, and the versions view shows them. The sample QA type carries samples on
  greeting, verification, troubleshooting and closing.

### Changed
- Scoring prompt **v8**: the ticket rule, the call facts and the samples. An audit is only reusable when it was
  produced by the same prompt version, so the first re-run of an older call scores afresh.

## [0.60.0] - 2026-09-22

### Added
- **One player for the page.** The recording's `<audio>` is no longer rebuilt with every scorecard: the page has
  one, born in a playback bar at the foot of the window, and the open scorecard borrows it. Saving an override,
  a review, a dispute or the call's details — anything that redraws the scorecard — parks it for a moment and
  hands it straight back, so **playback is never interrupted by an audit action**. Leaving the call (another
  tab, back to the list) hands it to the bar, where it **keeps playing** with the call's name, ⟲10 / ⟳10, an
  Open call button and ×; or it stops, if you prefer. Opening a different call's scorecard gives the player to
  that call.
- **Rewind and forward** — ⟲20 ⟲10 ⟳10 ⟳20 beside the player, a speed menu (1× to 2×), and the keep-playing
  switch right there.
- **Settings › Preferences** — yours alone in this browser, never sent to the server: **Animations** Auto (follow
  the system's reduce-motion setting) / On / Off — Off stills the processing motifs, the score ring, the ticks,
  the wordmark, the shimmer and the walk's spotlight glide — and **Recording playback** keep playing / stop.
  Every reduced-motion rule now reads the switch beside the system setting.

## [0.59.1] - 2026-09-22

### Fixed
- **The Show me around callout never sits on the spotlight.** When the spotlit element filled the window — the
  Audit table, the scorecard header — the card fell back to the lower right corner, which was on top of the very
  thing it was describing: on the Processing stop it hid the score ring, so the scorecard looked as if it had not
  come back, and on Audit it covered the last rows and their buttons. The card now tries below, above, beside on
  the right and beside on the left, and when none of those is clear of the target it docks along the bottom of
  the window, the spotlight ends above it and the page gains that much room at the foot so everything can still
  be scrolled above the card. Small windows, which always docked, get the same clipping and room.
- **Processing stop, landing** — when the sample call's scorecard arrives the walk scrolls the ring into view and
  the callout says so ("There it is — the ring drew itself in as the score arrived…") instead of still asking
  you to watch.

## [0.59.0] - 2026-09-22

- **Show me around now shows a call being processed.** A new third stop, Processing, sits between Calls
  and the scorecard: the tracker walks through Uploaded, Transcribing (the sound wave) and Scoring (the
  thinking dots), then the real scorecard lands with its ring drawing in and the "pre-audited by AI"
  tick. The sample pipeline finishes in a blink, so the walk stages the tracker itself with the same
  code the Calls page uses; leaving the stop or the walk stops the staging at once. Eight stops now.
- **A quieter finished row on Upload.** When a call is done its row says the one thing that matters —
  "Transcript reused", or "Uploaded before" while a file is waiting — with the earlier audit, the no-charge
  note and the open-it link behind a details toggle instead of a paragraph, and the
  transcription-engine pill is gone from finished rows (it stays while a call is still processing).

## [0.58.0] - 2026-09-22

- **Sample data near the target.** The 44 fictional calls now score the way a team close to its goal
  scores: on the demo Dashboard the pass rate and coaching coverage sit near the 90 % target instead of at
  half, Jordan leads, Marcus is lowest and climbing but still a little under, one or two calls miss a
  critical check, and some calls still wait in the audit queue or in draft. A test pins the numbers.
- **A call's journey, drawn.** While a call is processed the tracker and the Upload rows show what is
  happening rather than one spinner: an arrow rising while uploading, a sound wave while transcribing,
  three thinking dots while the AI pre-audits, and the Auditly tick drawing in on each finished stage.
  When a call the page was watching lands, its score ring draws itself in and the number counts up, with
  a small "pre-audited by AI" tick beside it for a moment.
- **The audit is done.** When a QA submits the final score, the review card's ring draws in, the number
  counts up and the mark's orange tick stamps beside it. Under reduced motion nothing moves: values
  appear at once and the tick is simply there.

## [0.57.0] - 2026-09-22

- **The Dashboard leads with what is going well.** The same numbers, framed as progress: the
  **Auto-fail rate** tile is now **Critical checks passed** (green at 100 %); **Where calls lose points**
  is **Where calls earn points**, strongest criterion first, each bar saying what is still there to gain
  (or "full marks"), with the note that the lowest bars are where coaching pays off most; **Agents to
  watch** is **Agents**, with **Leading** and **Most room to grow** instead of Top and Needs attention,
  and the largest rise marked **most improved**; score bands and sentiment shift list the best segment
  first; "Awaiting coaching" reads **Ready for coaching**; a downward change is shown in amber, not red.
  Score colours themselves are unchanged. The one-page PDF, the tour and the Help guide use the same words.
- **The walk keeps Coaching to the made-up agents.** On a real server the Show me around walk's Coaching
  stop listed the whole live team with zero counts beside Jordan, Priya and Marcus; it now shows only the
  agents of the sample data, and only their sessions and calendar entries.

## [0.56.0] - 2026-09-22

- **Show me around is a button at the top**, beside Help, as well as inside the Help dialog.
- **The walk shows sample data, not customers.** On a demo server everything already is sample data. On
  a real server the walk looks for loaded demo calls: if they are there it switches Calls, Audit,
  Coaching and the Dashboard to Demo for its duration and opens a demo call at the scorecard stop,
  putting the filters back when you finish; if they are not, it asks first — an admin can load the
  sample data (44 fictional calls: made-up agents, callers and companies, no audio, tagged Demo, out of
  real totals, removable under Settings › Names) and start, and anyone can choose to continue with real
  calls or cancel. A new check pins that the sample data holds no email outside example.com and no
  phone number. Focus mode, if on, is left as it is during the walk.

## [0.55.0] - 2026-09-22

- **Focus mode.** A **Focus** chip at the top hides every call on Calls, Audit and Coaching at once;
  the calls you bring into focus — with the Focus button on any row or on the scorecard, as many as
  you like — are the only ones that show, and the bar under the tabs lists them with a × each and
  **Show all**. While the set is empty the lists are blank and the bar says how to add a call: type in
  the search box (results show while you search), press Focus on the one you want, clear the search.
  Coaching shows only the focused calls' agents and their sessions; the Dashboard stays a whole-team
  view. Pressing Focus on a call with the mode off switches it on with that call, as 0.53.0 did. It is
  remembered in your browser.

## [0.54.0] - 2026-09-22

- **Show me around.** Help now offers a guided walk through the real screens: a spotlight and a short
  callout at seven stops — Upload, Calls, the sample call's scorecard, Audit, Coaching, Dashboard,
  Settings — then a card that says what to do first. It uses whatever is on screen (in demo mode the
  sample call DEMO-0001 and the loaded history; on a real server with nothing in it each stop says
  "Nothing here yet" and what will appear), opens every tab through the same buttons you would press,
  and leaves you on the last screen when you finish or press Escape. Back, Next, Skip, ← → and Escape;
  the callout keeps clear of the sticky header and docks to the bottom on a small window; no motion
  under reduced-motion. The first-sign-in Help dialog leads with it; nothing ever starts it by itself.
- **Do we need a clip?** Not to learn the app: the walk runs on the live screens and never goes stale,
  while a recorded video would be wrong within a release. `tests/tour_record.js` (node + Playwright, not
  part of the suite) plays the tour and writes a `.webm`, so a shareable clip can be produced from the
  app itself whenever one is wanted — none ships in this release.

## [0.53.0] - 2026-09-22

- **Focus on one call.** Every row on Calls and Audit, and the scorecard's action row, has a **Focus**
  button: press it and both lists show only that call — every run of it, whatever its kind — with a bar
  under the tabs naming it and a **Show all** to go back. It is remembered in your browser, so it
  survives a reload and a change of tab; the search box and the status filter still apply on top of it.
  Coaching and the Dashboard are agent-level views and are left as they are.

## [0.52.0] - 2026-09-21

- **Agents can dispute their own scores.** When a QA submits an audit, the agent is emailed a link to a
  read-only copy of their scorecard — the score for each criterion with the words quoted from the call,
  the summary, strengths, opportunities and misses — where they press **Dispute this score** (or
  **Dispute the whole scorecard**) and say why. No login: the link carries a long random token bound to
  that one scorecard, works for `AUDITLY_DISPUTE_LINK_DAYS` (14; 0 switches the feature off) and stops
  the moment the audit is reopened; resubmitting sends a fresh one. The page never shows audio, the
  call text, customer details, the QA's name or notes, and unknown, expired and revoked links all get
  the same "expired or not valid" page.
- The agent's dispute lands where the QA-recorded ones do — the same Disputes card, counts, filters and
  notification — marked "raised by the agent from the emailed link", with the agent's address as who
  raised it. Uphold, reject and withdraw are unchanged, and the agent sees the outcome on their page.
- **Dispute link** on the scorecard emails it again, copies it, or opens it in your own mail app — for
  agents without an address under Settings › Names or desks without a relay. The address the link
  points at is `AUDITLY_PUBLIC_URL`, or the one you reached the app by. The submit message says whether
  the link went out and, if not, why.
- The mail test now shares its message builder with the link email (`mailer.build_plain`).

## [0.51.0] - 2026-09-21

- **A notification button at the top.** It turns red with a count when something you have not seen
  happened — a coaching session scheduled, moved, unscheduled or cancelled (with the old and the new
  time), a dispute raised or resolved, an agent falling below the Dashboard's pass target, a call that
  failed to process. Click a line to go to it: the call, the audit, the session, or the Dashboard
  filtered to that agent. Events are shared by the team; what you have read is yours. It refreshes once
  a minute and whenever you come back to the tab.
- **Settings › Notifications** lets each person choose which of those events they get; choices apply
  on Save, and Mark everything read is there too. "Agent below target" is the Dashboard's own rule —
  an average under `AUDITLY_TARGET_PCT` with at least three scored real calls in the Dashboard's default
  range — at most once per agent per week. There is no second threshold anywhere: the Dashboard's row
  query now lives in `dashboard.py` and the check runs it.
- Demo mode and Load demo data light the button with the agents the seeded history really puts below
  target; removing the demo data takes those lines away again, and deleting a call or a job takes its
  lines with it. Notifications older than 90 days are swept. A job the boot re-queue has to give up on
  now reports itself too, instead of failing silently.

## [0.50.1] - 2026-09-21

- **The header logo waits for Apply.** Settings › Server › Branding used to save the moment a logo mode
  was clicked, so a pass through the options — Auditly, COEO, Hidden — could leave the deployment on
  Hidden without anyone meaning it; and Hidden also takes the Ask Auditly button away, which is exactly
  what happened this afternoon for two hours. Clicking now only picks: the header, the sign-in page and
  everyone else's screen keep the logo in use until **Apply** is pressed, **Cancel** drops the pick, and
  a line under the control says what is pending and, for Hidden, that the assistant's button goes with it.
  Leaving Settings and coming back drops an unapplied pick too. Auditly remains the default.
- The Ask Auditly button's tooltip still said the assistant was not switched on yet, wording from before
  it answered; it now says what the button does.

## [0.50.0] - 2026-09-21

- **Settings › Server is four panes.** It had grown into one long page: sixteen status tiles, then the
  Branding card, then the Assistant card with its instructions and history. It now has its own row —
  **Status** (version, mode, secure link, queue, database, upload cap, retention, ffmpeg, menus), **Providers**
  (the three keys, the default engines, the spend estimate, the mail relay and its Send test),
  **Assistant** and **Branding**. Nothing inside the cards moved; the row remembers your last pane in
  this browser. Reviewers see Status and Providers; the two admin panes are theirs alone.

## [0.49.0] - 2026-09-21

- **A secure link, from the app itself.** Auditly now serves HTTPS on a second port beside the plain
  one: `https://qa-server.example.com:8444/` next to `http://…:8084/`. Same server, same database, same
  everything — but a secure page, which is what browsers require before they will open a microphone. On
  the https link the Ask Auditly microphone works; on the plain link the panel now shows the https
  address to open instead. No nginx, no root: `./restart.sh` generates a self-signed certificate into
  `tls/` the first time and starts both listeners; `--no-tls` opts out.
- **What the team will see once.** A self-signed certificate makes each browser show "Your connection
  is not private" on the first visit. Advanced → Proceed, once per browser, and it is done. When IT
  issues a real certificate for the host, its two files replace `tls/auditly.crt` and `tls/auditly.key`
  (or `AUDITLY_TLS_CERT` / `AUDITLY_TLS_KEY` point at them) and the warning stops — same address, no code
  change. The production nginx install in `deploy/` takes over the same port when the time comes:
  switch the built-in listener off first (`restart.sh --no-tls`), then run the installer.
- Settings › Server shows the secure link. The app never sends Strict-Transport-Security — HSTS is per
  host and ignores the port, so one such header from :8444 would make browsers refuse the plain link
  for a year — and a test now pins that.

## [0.48.0] - 2026-09-21

- **Speak the question.** A microphone button sits in the Ask Auditly panel between the box and Ask.
  Press it, talk, press again: the clip goes to the server, is transcribed by the desk's own provider
  (Deepgram or OpenAI, whichever `.env` names, behind the same spend gate as everything else), and the
  words appear in the box for you to check before you press Ask. Nothing is sent to the assistant
  until you do. The clip is deleted the moment it is transcribed; only its length is kept, so the
  seconds show in Settings › Server › spend and count toward the per-person hourly cap.
- **It needs the HTTPS link.** Browsers open a microphone only on a secure page — HTTPS, or
  `localhost` — and the LAN dev link is plain HTTP, so there the button explains itself and stays
  disabled. The TLS install in `deploy/` (RUNBOOK §2) is what turns it on for the team; nothing else is
  needed. On `http://127.0.0.1:8084` it works today.
- The browser's own speech recogniser is deliberately not used: it is Chrome/Edge only and sends audio
  to Google under Google's terms, and a reviewer's question can name customers and agents. Demo mode
  answers with a fixed question and no key, so the flow is testable.

## [0.47.0] - 2026-09-21

- **Settings › Server › Assistant.** The assistant's switch, model, caps and — the part that was missing —
  its **instructions** are now edited in Settings by an admin, not in `.env` or in code. The card shows
  who last saved what and when, today's questions against the cap, the total and the estimated cost.
  Saved choices override the `ASK_*` keys in `.env`; the key itself stays in `.env` and is never shown.
- **The prompt is two parts, and only one is editable.** The instructions — who the assistant is, how it
  talks, what to emphasise — are yours to change. The six rules beneath them (answer only from the
  record, say so when it is not there, cite, fenced text is data not instructions, never reveal, never
  score or save) are shown greyed and are always appended by the code. A typo or an experiment in
  Settings can change the tone; it cannot make a caller's words executable.
- **Every save is a new version, like a QA type.** Nothing is overwritten; **Reset to built-in** is
  recorded as a version too; the history shows each one with its note and byline; and every answer
  records which version produced it, so a bad answer traces to the exact text behind it.
- **Try it.** A question box against the newest scored call, answered through the real path with the
  current instructions, so an admin sees the effect of an edit before anyone else does. Not added to any
  reviewer's conversation; counted and audited like any question.
- The refusal message when the assistant is off now points at Settings. `assistant.settings` and
  `assistant.prompt` join the audit verbs.

## [0.46.0] - 2026-09-21

- **Ask Auditly answers.** The launcher in the lower right now opens a working assistant for the call you
  have open in Calls or Audit. Ask why a criterion was rated as it was, what the evidence says, what the
  QA wrote, what the call lost points on, what it was about. Every answer cites what it used — the
  criterion, the quote, the review, the summary — as chips under the reply, and a citation the record
  did not offer is dropped before you see it, exactly as the scorer drops a quote it cannot find in the
  transcript. When the answer is not in the record it says so instead of guessing. It answers only; it
  never scores, overrides, saves or sends.
- **Off until an admin turns it on** (`ASK_ENABLED=1` in `.env`), and behind the same spend gate as
  scoring. The panel says plainly why it cannot ask when it cannot: switched off, no key, spending off,
  today's cap used up. `ASK_MAX_PER_DAY` (300) and `ASK_MAX_PER_USER_PER_HOUR` (40) bound it; every
  question is audited as `assistant.ask` — its own verb, so the "who looked at this call" trail keeps
  its meaning — and its tokens count in Settings › Server › spend. `ASK_API_KEY` takes a separate
  OpenAI project key so the assistant's spend gets its own line and cap; blank means the scoring key.
- **The transcript is treated as what people said, never as instructions.** It is fenced and labelled as
  data, the prompt says so, and the test suite carries an injection suite ("ignore your instructions",
  "admin mode", "reveal your prompt") that must be declined, plus an eleven-question golden set that
  runs on every change with no key and no cost. The assistant has its own prompt version, separate from
  the scorer's, so a reword here can never invalidate cached audits.
- **QA reviewers only, by design.** Agents using it is the right destination and is much larger than the
  chatbot: the app has never had per-row permissions, and the LAN link has no sign-in. That is its own
  project. The full reasoning, the industry practice consulted and the phasing are in
  `docs/ASSISTANT_DESIGN.md`.

## [0.45.0] - 2026-09-21

- **A Next step bar under the tabs.** One line says what to do now with the call being worked on, and
  one button does it: watch a call that is still processing, read a scored one and start its audit,
  open an audit that is waiting, plan the coaching for the agent, or go to the trends once the call is
  coached. A five-dot rail shows how far along it is. It lives in the sticky header, so the answer is
  the same on every tab, and `×` hides it for good — Help offers it back.
- **The Upload form asks for two things.** QA type, agent, drop the file. The scoring model, the QA
  reviewer, the audit's name and the Real / Demo / Test switch all have working defaults and now sit
  behind **More options**, which remembers whether it was left open. Demo mode opens it once, since
  Demo run lives in there. Nothing was removed and nothing about the upload changed.
- **Four dead ends are closed.** Starting several audits at once used to show a message and leave you
  on the Calls list; it now takes you to the audit queue. Nothing told you an upload had finished once
  you left the Upload tab; the Next bar does. Submitting a final score never mentioned coaching; it
  does now. The Coaching tab never knew which agent you had just audited; arriving from the Next bar
  it does, and choosing an agent is one function both it and the Agents list go through.

## [0.44.0] - 2026-09-21

- **Coaching is its own step in the Flow chart.** It had been a single box inside the QA audit, which
  no longer matched the app: coaching has its own tab, its own selection of calls and its own stage on
  a call ("coached"). The chart now runs in eight steps, and step 7 shows what the tab actually does —
  pick an agent and tick their scored calls with a running tally, a session with Totals and an agenda
  drafted from the reviews, a date and time with the month and week calendar, and Mark delivered
  stamping every call in the session. The email invite hangs off the schedule as an optional branch,
  since a session can be booked without one.
- The Dashboard becomes step 8 and still closes the chart.

## [0.43.1] - 2026-09-21

- **Fixed: the old logo kept showing.** The built-in brand files keep their names from release to
  release, but they are served with a year-long `immutable` cache, so a browser that had loaded the
  app before 20 September went on drawing the stand-in wordmark I had drawn — the real artwork was on
  the server and never fetched. Every built-in logo, tab icon and launcher mark URL now carries the
  build (`?v=0.43.1`), which retires the cached copy on the next ordinary page load. No hard refresh
  and no cache clearing needed, and the same applies to every future change to the artwork.
- Uploaded branding already worked this way, versioned by its upload time; the favicon route reads
  past the new query when it looks the file up on disk.

## [0.43.0] - 2026-09-20

- **Ask Auditly** — the square Auditly mark now sits in a round button in the lower right of every
  screen, and stamps itself the same way the wordmark does: the "i" stem lands, then the orange tick
  drops and is pressed home. Pressing it opens a small panel that says plainly that the assistant is
  not switched on yet and names what it will answer from — the scorecard and QA audit for the call
  you are on, the QA type it was scored against, and the documents under Settings › Knowledge.
  Nothing is sent anywhere: there is no model call and no new outbound traffic behind it.
- The button is the place the assistant will take, so the panel is the shell it will fill. It appears
  only once someone is signed in, closes on Escape or a click outside, and hands focus back to the
  button.
- The launcher follows the theme and inverts with it: brand navy carrying the cream mark in light
  mode, brand cream carrying the navy mark in dark. A visitor who asks for reduced motion gets the
  motionless twin (`auditly_mark_light_still.svg` / `auditly_mark_dark_still.svg`), chosen by the
  page, since that preference never reaches an SVG drawn inside an `<img>`.
- Hiding the logo under Settings › Server › Branding now hides the launcher too, so a white-labelled
  deployment does not get the Auditly mark in the corner instead.

## [0.42.0] - 2026-09-20

- Logo — the real Auditly artwork replaces the stand-in marks I drew, and the wordmark now **animates on
  every page load and refresh**: the letters land one at a time, then the orange tick drops and is stamped
  with a ring. It plays on the sign-in page too, and again on a theme switch, which swaps light for dark.
  The animation lives inside the SVG as CSS keyframes, so nothing was added to the page's script and the
  nonce-only CSP is untouched.
- Logo — a visitor who has asked their system for reduced motion gets a motionless twin of the same
  wordmark (`auditly_light_still.svg` / `auditly_dark_still.svg`). The page chooses it, because
  `prefers-reduced-motion` does not reach an SVG drawn inside an `<img>` and the media query inside the
  file cannot be relied on.
- Tab icon — the favicon is now cut from the same artwork: the "i" stem and the tick on a solid navy
  square. Icons stay still, since a tab icon that starts blank is no icon at all.

## [0.41.0] - 2026-09-20

- Coaching invites from the QA's own email app — **Email invite** on a scheduled session now opens one
  dialog with three ways, the first recommended: **Open in my email app** downloads the invite as an
  `.eml` draft (`X-Unsent: 1`, calendar request attached) that Outlook opens as a new message from the
  QA's own account, ready to check and send; **Mark invite as sent** then records it so the session and
  the calendar show ✉ (channel `mail_app`). **Send from the server** stays for teams that set the `SMTP_*`
  keys (channel `smtp`). A plain `mailto:` link remains as the no-attachment fallback, labelled as such.
  The header's separate Email invite link and server-only Send invite button are gone; the ✉ pill says
  which way it went. `GET /api/coaching/sessions/{id}/export.eml?to=&qa=`,
  `POST /api/coaching/sessions/{id}/invite-sent`.

## [0.40.0] - 2026-09-20

- Coaching invites by email — **Send invite** on a scheduled session emails the agent a real calendar
  request (text/calendar, method REQUEST, .ics attached) so Outlook and Google show Accept / Decline.
  Sent through a mailbox named in `.env` (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`,
  `SMTP_FROM`, `SMTP_STARTTLS`; new `mailer.py`, stdlib smtplib). The QA of record is the organiser
  and Reply-To (their email now lives under Settings › Names too, next to the agents'); From is the
  server mailbox unless `SMTP_SEND_AS_QA=1`, which needs Send As rights on the relay. Every attempt is
  recorded (`coaching_invite`); the session shows **✉ invite sent … to …**. Without SMTP settings the
  button is not offered and the .ics download and Email invite link remain. Settings › Server shows a
  **Mail** tile with **Send test** (admin) to prove the relay works.
- Coaching calendar — a **Week** view beside Month: a Calendly-style time grid (08:00–18:00, half-hour
  slots, Monday first); click a free slot to book a session at that time. Both views mark sessions
  with **✉** (invite emailed) and **✓** (delivered). The week view opens with "This week: N sessions ·
  ✉ N invites sent · ✓ N delivered · N awaiting invite"; the month view ends with a **By week** table
  of the same, a row per week, "all invited" or "N awaiting invite" — click a row to open that week.
- Fixed — the header logo showed as a broken image when the page was newer than the running server:
  the page now falls back to the built-in mark, then to the name. (The running LAN server also needed
  its restart to pick up 0.39.0's routes.)

## [0.39.0] - 2026-09-20

- Branding (white label) — the header now carries the **Auditly** wordmark (light and dark) and the
  browser tab the Auditly icon, instead of the COEO logo and no favicon. Under Settings › Server ›
  **Branding** an admin chooses the header logo: **Auditly** · **COEO** (with the app name beside it) ·
  **Hidden** (name only) · **Custom logo**, and can upload their own logo and tab icon for light and dark
  mode (PNG, JPG, SVG, WebP, GIF or ICO, 512 KB each; checked by magic bytes, SVGs refused when they
  carry script or handlers and served with a no-script CSP). Uploads live in the database
  (`brand_asset`), so no file path exists to leak; `GET /api/branding` and the asset URLs are public
  because the sign-in page shows the logo too. The built-in Auditly marks are SVG recreations; the
  real artwork, dropped as `auditly_light.png`, `auditly_dark.png`, `auditly_icon_light.png` and
  `auditly_icon_dark.png` into `static/`, is used automatically (the Branding card lists the names).
- Favicon — `/favicon.ico` serves the current dark-background tab icon (an uploaded one when present)
  instead of answering 204.

## [0.38.0] - 2026-09-20

- Coaching — the QA picks the calls, from any of the agent's scored calls. **New session** and
  **Add calls** gain a scope switch — **Waiting for coaching** (audited, not yet coached, or with an
  open dispute; the default and the old behaviour) · **Audited** (coached or not, for a refresher or a
  comparison) · **All scored** (un-audited calls show their AI score, are labelled, and are never
  stamped coached) — plus **Select all / none**. Calls already in another live session stay greyed out.
- Coaching — it adds up. A tally under the list updates as calls are ticked: count, average and
  lowest score, calls below the target, misses, and the criteria that lost the most points; the
  Create / Add buttons show the count. The session view gains a **Totals** card (calls, average with
  lowest and highest, below target, misses and auto-fails, open disputes, points lost per criterion
  across the calls and per call) and an Average footer on the calls table; the drafted agenda opens
  with the same totals and the worst criteria; the coaching-pack PDF has a Totals block. One value per
  call: the final score where the audit is submitted, else the AI score. `/api/coaching/candidates`
  takes `scope=` and returns each call's scored criteria; a session returns `summary`.

## [0.37.0] - 2026-09-20

- Executive dashboard — the Dashboard now opens on an **Executive** view (Detail keeps every chart
  and table that was there; the choice is remembered in the browser). Six headline numbers with the
  change since the previous period of the same length: average QA score, pass rate against the
  target, calls, auto-fail rate, audit coverage (scored calls with a final score) and coaching
  coverage (audited calls coached). Then the trend against the target, **Where calls lose points**
  (attainment per criterion, worst first, with points lost per call), score bands (under 60 · 60–79 ·
  80–89 · 90+), the customer-sentiment shift start → end, **Coaching and disputes** health (awaiting
  coaching, sessions scheduled, days from audit to coaching, open disputes, upheld rate) and **Agents
  to watch** (top and lowest averages among agents with three or more calls, with their change).
  **Export PDF** writes the same summary as a one-page PDF for the same range and filters.
- Target — `AUDITLY_TARGET_PCT` (default 90) sets the pass-rate threshold and the dotted target line
  on both views.
- Flow — the Dashboard step names the executive KPIs.
- Fixed — the QA score trend line was drawn as a filled area: the series colour class set `fill` on the
  path as well as on the dots. The line is a line again on both views.

## [0.36.0] - 2026-09-20

- Knowledge base — Settings › **Knowledge** holds the desk's reference documents (procedures,
  product facts, escalation rules): paste text or upload `.txt` / `.md` / `.docx`, scope a document to
  every QA type or one, enable/disable, rename, delete (admin). Each document is split into
  paragraph chunks; at scoring time the transcript is the query and the best-matching chunks (BM25,
  pure Python — no embeddings, no provider call, no cost) are given to the scorer as
  **KNOWLEDGE BASE EXCERPTS** before the transcript, under a new rule 13: use them only to judge
  whether what the agent SAID was accurate and complete; they are never evidence of what happened
  on the call. A call that matches nothing gets no excerpts. The scorecard shows a **Reference
  material** card with the excerpts the scorer saw (`scorecard.kb_json`); **Try a search** on the
  Knowledge tab shows exactly what a given text would retrieve. `/api/health` reports `kb_docs`
  and the Upload tab says when the scorer will read the knowledge base.
- Scoring — prompt version 7 (rule 13 and the excerpt slot). An audit is "the same request" only
  when the reference material was the same too (`scorecard.kb_key`), so changing the knowledge
  base makes "Audit again" run rather than reuse; cards scored before 0.36.0 are re-run once.
- Demo — `--demo` seeds a sample document (desk-phone procedures) so the demo call's scorecard
  shows the excerpts; an existing demo database's old card gains none until it is audited again.
- Flow — step 4 gains the Knowledge base node feeding the scorer.

## [0.35.0] - 2026-09-20

- Coaching calendar — **Schedule** on a session picks the date, time (your time zone), length
  (15–180 min) and place; the server stores UTC and warns of an overlap with the same agent's or the
  same QA's other session (409, with the clashes listed; **Schedule anyway** forces it). **Unschedule**
  goes back to planned. The Coaching tab's **Calendar** sub-tab is a month grid (Monday first) of
  scheduled sessions and review follow-up dates (⏰), with ‹ Today › and a "Coming up" list; click a day
  to plan a session on it, a session to open it, a follow-up to open its audit.
- Invites without a mail server — **Add to calendar (.ics)** downloads an RFC 5545 event (UTC start
  and end, the agenda as description, the place, the QA as organiser and the agent as attendee when
  their email is on file) for Outlook or Google Calendar; **Email invite** opens the QA's mail client
  with the invite drafted to the agent. Agent emails are kept under Settings › Names (agents only,
  optional, `person.email`). A Calendly-style "the agent picks a slot" would need agents to sign in,
  which they cannot yet; scheduling is the QA's.

## [0.34.0] - 2026-09-20

- Coaching tab — a new top-level tab between Audit and Dashboard. The left card lists every agent
  with audited calls waiting for coaching, open disputes and their next session; the right card lists
  sessions under **Upcoming · Delivered · All**. **New session** picks the agent and ticks the calls
  to coach on — audited (submitted) calls not yet marked coached, plus any call with an open dispute;
  a call sits in at most one live session — and the agenda is drafted from their reviews (final score,
  misses, coaching notes, action plan, open disputes, recurring themes). The session view edits the
  title, place, agenda and notes, adds or removes calls, opens a call in the Audit tab and exports a
  coaching-pack PDF. **Mark delivered** stamps `coached_at` on every included submitted review (calls
  whose audit is not submitted are reported and left alone); **Reopen** removes exactly the marks that
  session made; **Cancel** frees the calls. Every action is in the audit log (`coaching.*`).
- Flow — step 6 now runs Submit → Coaching tab → Dashboard.

## [0.33.0] - 2026-09-20

- Tone & delivery — a new card on the scorecard, computed from the transcript timings alone: the
  agent's talk share, interruptions (agent / caller), the longest silence and how many gaps ran
  over 5 s, the agent's pace in words per minute and their median response time. Coaching
  material — it is never part of the score, and the card says so. Also in the CSV and PDF.
- Per-line sentiment (opt-in) — `AUDITLY_STT_SENTIMENT=1` asks Deepgram for sentiment on every
  line (`utterance.sentiment`, `sentiment_score`); the transcript gets a coloured dot per line and
  a caller / agent sentiment strip over the call, and the card shows the caller's trend and whether
  it agrees with the scorer's start → end verdict. Off by default: it is billed per token on top of
  the audio minutes, English only, and — the finding behind this feature — it is derived from the
  **words** after recognition, not from the voice. Neither provider exposes acoustic tone
  (pitch, volume), OpenAI's transcription returns nothing tonal, and this host cannot decode the
  audio itself, so tone of voice stays out of reach; the timings are what the call gives for free.
  The demo call carries per-line sentiment so the feature can be seen without keys.
- Tone follows a speaker swap: changing who the agent is recomputes the card.

## [0.32.0] - 2026-09-20

- Disputes — an agent can now dispute a score. Agents have no sign-in, so the QA records the
  dispute on the agent's behalf: **⚑** beside any criterion, or **Dispute** on the scorecard header
  for the card as a whole, with the agent's reason in their words. A Disputes card on the scorecard
  lists them; any reviewer or admin can **Uphold** (with a corrected score when it is about one
  criterion — applied through the normal override path, so a submitted audit must be reopened
  first and the final score stays frozen until then), **Reject** or **Withdraw**, each with a note.
  Open disputes show as a ⚑ count on Calls, Audit and the scorecard; Audit › Completed has a
  "Disputed by the agent (open)" filter; `/api/disputes` lists them for the coming Coaching tab.
  Every action is in the audit log (`dispute.raise/resolve/withdraw`).
- Flow — step 6 gains the dispute loop: Submit → "agent disagrees?" → Agent dispute → back to fixing
  scores.

## [0.31.1] - 2026-09-15

- Help — the About tab now opens with who made Auditly and who contributed, before who it is
  for and the problem it solves.

## [0.31.0] - 2026-09-15

- Help — the dialog now has two tabs. **How to use** is the six-step guide as before. **About**
  says who made Auditly and who contributed (with LinkedIn links), who it is for (the Level 1
  support desk at COEO / SNET Connect: reviewers, admins, agents), the problem it solves (hand-scored
  spreadsheets, small samples, reviewers disagreeing), what it stands for, how it works in one
  breath, and how data and cost are handled.

## [0.30.0] - 2026-09-15

- Renamed — the product is now **Auditly**. The page title, header, Help guide, PDF exports
  (masthead and file name `auditly-scorecard-…`), logs and documents all carry the new name.
  The folder is `~/Auditly`, the server file `auditly_host.py`, the page
  `auditly.html`, the tests `tests/test_auditly.py`, and the settings in `.env` are `AUDITLY_*`.
- Renamed — nothing breaks for the team: the LAN link is now
  `http://qa-server.example.com:8084/auditly/` and the old `/l1qa/` link redirects to it; the old
  `L1QA_*` names in `.env` still work (the Server tab lists any still in use so they can be
  renamed); the theme and the "guide already shown" choice carry over; the seeded QA type keeps
  its name "L1 Support v1" because it describes the team's rubric, not the product.

## [0.29.0] - 2026-09-15

- Help — a **Help** button at the top, beside Flow, opens a short guide: what the app does, the
  six steps from setting up a QA type to reading the Dashboard, each with a **Go there** button,
  and a Good to know list (demo mode is free, what leaves the machine, identical work is never
  billed twice, every save is a new version). It opens once by itself after the first sign-in in
  each browser and can be reopened from Help at any time; **Show the Flow chart** hands over to
  the flowchart.
- Tooltips — every remaining control now explains itself on hover: Light/Dark and Sign out, the
  QA type box and the drop zone, the Calls filters, pager, column headings, Retry and Delete, the
  scorecard's exports, override fields and evidence quotes, the audit form and call details, the
  Dashboard period and range buttons, users and passwords, every field of the QA-type editor, the
  Optimise panel, and the archived, critical and status pills (queued · transcribing · scoring ·
  done · failed).
- Flow — step 1's Optimise box now says the question is asked after every save (Optimise or Not
  now) and step 4 says scoring uses the optimised rules or the guidance as written. A test now
  cross-checks the chart against the menus, the re-run buttons, the Settings sub-tab and the
  seven-step count in the docs, so the chart cannot go stale without the suite saying so.

## [0.28.1] - 2026-09-15

- QA types — after every Save (a new QA type, an edit, or Save as new QA type) the app now
  asks straight away whether to optimise the version for the scorer — **Optimise** or
  **Not now** — naming the model, the estimated cost and the runs left. When it cannot run
  (spending off, key missing, limit reached) a notice says why instead. Optimising is still
  the reviewer's decision and still counts against the limits; Not now leaves the Optimise
  button on the card and the "not optimised" pill on the list.
- Fixed — the Optimise dialog and the "Already done — reuse it?" dialog showed two Cancel
  buttons; one each now.

## [0.28.0] - 2026-09-15

- QA types — **Optimise for the scorer**. Each version now has an Optimise button
  (after Save, in the list and on the version). Pressing it asks the scoring model
  once to rewrite every criterion's met / partial / missed guidance into explicit
  decision rules — "met = ALL of: …", what counts as an attempt, what "absent"
  means, and exactly when a criterion does not apply — grounded in the uploaded
  guidelines, so repeated audits of the same call land on the same ratings. The
  criteria, names, weights and the guidance as written are never changed; the rules
  are stored alongside and shown under "Show what changed". It is manual: saving
  never optimises, and versions from before this release stay as they are.
- QA types — limits and guardrails: one optimisation run per version and five per
  day across the server (`L1QA_SYNC_MAX_RUNS_PER_VERSION`, `L1QA_SYNC_MAX_RUNS_PER_DAY`
  in `.env`), shown with the cost estimate in the confirm dialog. A run retries
  itself up to three times if the model fails or returns rules that do not pass the
  server's checks; retries do not use up runs. When spending is off or the scoring
  key is missing the button is refused with the reason and nothing is queued.
- QA types — while it runs the card shows Saved → Optimising → Ready with a progress
  bar; when it finishes, a toast, a green "optimised" pill on the list and the version,
  and an audit-log entry. A version that is not optimised shows an amber "not optimised"
  pill and a banner on the QA types list; the Upload tab notes it under the QA type box.
- Scoring — a scorecard records whether it was scored with the optimised rules or the
  guidance as written ("optimised rules" pill next to "scored by"), and "Audit again"
  only reuses an earlier result produced the same way. An audit queued while an
  optimisation is running waits for it (up to `L1QA_SYNC_WAIT_S`, default 120 s).
  Prompt version 6: the scorer is told to apply listed conditions literally.
- Scoring — OpenAI audits send a fixed `seed` (`SCORING_SEED`) next to temperature 0 on
  the models that accept it — best effort, one less source of run-to-run drift.
- Check — `python3 l1qa_host.py --eval-determinism latest --runs 3` scores one transcript
  several times with the guidance as written and several times with the optimised
  rules, in memory, and prints per-criterion agreement, overall-% spread, cost and a
  verdict (MORE / EQUALLY / LESS consistent) — so an optimisation is confirmed before it
  is relied on. `--optimise-version <id>` runs one optimisation from the command line.
  Both refuse to run unless spending is allowed (or in demo).
- Flow — step 1 gains "Optimise for the scorer" after "Edit and save".
- Fixed — `L1QA_VOICE_AI_NAME` in `.env` was read but silently ignored, so the scorer
  always called the voice AI "Emma". It is honoured now.

## [0.27.3] - 2026-09-10

- Fixed — flowchart labels ran past their boxes again. Mermaid measures each
  label while it draws, and the page's security policy refused its stylesheet
  and inline styles at that moment, so labels were measured unstyled and drawn
  styled. While the chart is rendered its styles are now let through under the
  page's own nonce, so measured and drawn text agree; every box also has more
  padding and every label line is short with explicit breaks. Verified in a
  headless browser this time.
- Flow — **Start** and **End** are circles sized to their word. There is one
  Start: the QA type is set up first, "then, per call" the recording is
  processed; the rubric is shown "scored against" the LLM with a dotted line.
- Flow — the "new run" notes no longer lie across other boxes; they moved into
  the two re-run boxes. Shorter step titles so none wraps onto its boxes.

## [0.27.2] - 2026-09-10

- Flow — **Start** is drawn in a green shade and **End** in a red shade, in
  tones that suit the light or dark theme.

## [0.27.1] - 2026-09-10

- Flow — the chart now has a **Start** (leading into the QA type set-up and
  the recording) and an **End** after the Dashboard.

## [0.27.0] - 2026-09-10

- Flow — the chart now starts with the **QA type** set-up (Settings › QA types:
  guidelines → Extract criteria → edit, weights must total 100 → saved as a
  version) feeding the scorer; the Scorecard step shows the three re-audit
  paths — **Audit again with…** (another LLM), **Rescore with QA type…**
  (another version) and **Transcribe again with…** (another engine) — looping
  back as new runs beside the old one; and the **Dashboard** is the final step
  after the QA audit. Steps are numbered 1–7.

## [0.26.4] - 2026-09-10

- Header — the logo header and the section tabs now stick to the top as one
  block; the tab bar no longer slides under the header while scrolling.

## [0.26.3] - 2026-09-10

- Footer — always sits at the bottom of the window on short pages and after
  the content on long ones, at any screen size or zoom level.

## [0.26.2] - 2026-09-10

- Footer — the LinkedIn badge is now LinkedIn blue and each name is in the
  normal text colour, in both light and dark themes; only “Created by” and
  “Contributors:” stay muted.

## [0.26.1] - 2026-09-10

- Footer — Amy Gallo and Anah Aquino are credited as contributors beside the
  creator credit, each linking to their LinkedIn profile.

## [0.26.0] - 2026-09-09

- The section tabs — Upload · Calls · Audit · Dashboard · Settings — moved out of
  the logo header into their own full-width bar beneath it: larger, bolder, the
  active tab filled in COEO blue, and the bar stays at the top while you scroll.

## [0.25.0] - 2026-09-09

- The theme switch offers **Light** and **Dark** only; the Auto option is gone. A
  browser that had Auto keeps the mode it was showing.

## [0.24.0] - 2026-09-09

- The app wears the COEO colours: navy text and blue accents in light mode, the
  deep-navy background with pale text in dark mode, taken from the logo files.
  The animated COEO logo sits in the header and swaps with the theme. Served
  from this server (`static/`); nothing external.

## [0.23.0] - 2026-09-09

- Dashboard: a **pipeline strip** above the tiles mirrors the Flow chart —
  Recorded → Transcribed → AI scored → Awaiting audit → Audited → Coached — with
  the count for each step in the selected range and filters. Click a step to open
  the matching list (Calls, Audit › To audit, Audit › Completed).
- API: `/api/dashboard` gains `pipeline`.

## [0.22.4] - 2026-09-09

- The flowchart's step 2 is titled simply "2 · Transcription"; the long title
  wrapped over the engine boxes. The caption still says which engine the next
  file will use.

## [0.22.3] - 2026-09-09

- Fixed — flowchart labels still ran past their boxes. Mermaid lays the boxes out
  with its top-level font size (16) but drew the labels at the theme's 18 px set in
  0.22.1, so text was about an eighth wider than the box measured for it. Both
  settings now carry the same size and family.

## [0.22.2] - 2026-09-09

- Fixed — flowchart labels ran past their boxes. Mermaid measures text before it
  draws, in a scratch element where the page's security policy blocked its font
  settings, so boxes were sized for a smaller font than the one drawn. The page
  now pins the same font and size for measuring and drawing.

## [0.22.1] - 2026-09-09

- Fixed — the Flow dialog was capped at 860 px by the generic dialog rule, so the
  chart sat in a small box in the middle of the screen. It now uses 98 % of the
  window, has a **Full screen** toggle (the F key too), and Mermaid draws the
  labels larger.

## [0.22.0] - 2026-09-09

- The flowchart is drawn in **Mermaid's own look** — its standard boxes, arrows
  and labels, and its dark theme when the page is dark — instead of the app's
  colours. The transcription engine the next file will use keeps its heavier
  outline. Still rendered from this server's copy of Mermaid; nothing external.

## [0.21.2] - 2026-09-09

- Fixed — Fit on the Flow dialog filled the window's height and so cut the flow
  off after step 2 with a scrollbar. Fit now shows the **whole flow** at once,
  and the dialog is a rectangle shaped to the chart (no empty band under it);
  + / − and Ctrl+wheel zoom in and pan, Fit returns to the full view. In a
  narrow or portrait window the flow is drawn top-to-bottom instead.

## [0.21.1] - 2026-09-09

- Fixed — the Flow dialog drew the flowchart at the dialog's width, so on most
  screens it was a thin strip with tiny text. **Fit** now fills the window's
  height (pan sideways for the rest) and re-fits when the window is resized or
  the browser zoom changes; + / − and Ctrl+wheel still zoom by hand, and Fit
  returns to the fitted size. The version chip's Flow tab is sized the same way.

## [0.21.0] - 2026-09-08

- A QA type can be **edited** from its view — **Edit** saves the changes as the
  next version of the same QA type, **Save as new QA type** starts a new one from
  its criteria — and every version now records **what changed**, when and by
  whom. The list and the view show "Last edited <date> by <who>". Only an admin
  or a QA reviewer can create, version, archive or extract QA types; the server
  refuses any other role.

## [0.20.0] - 2026-09-08

- Rescoring keeps the reviewer's work — **Rescore with QA type…**, **Audit again
  with…** and **Transcribe again with…** now offer *Carry over my overrides and
  audit notes to the new run* (on by default). Overrides are copied when the QA
  type version is the same (criteria match), the coaching, resolution and action
  plan are copied as a **draft** review — never the submitted status or final
  score, which stay on the run they were given on. The job's progress line says
  what was carried. Nothing was ever deleted by a rescan; each run is its own row.
- **Edit QA type** link in the scorecard header opens that QA type in Settings ›
  QA types to create a new version.

## [0.19.0] - 2026-09-08

- What was called a *rubric* on screen is now a **QA type**: the Upload label,
  the Calls column, the scorecard header, the rescore menu, the Settings tab
  (now **QA types**), the editor and its messages, the PDF line and a new CSV row.
  Element ids, API paths and export field names are unchanged.

## [0.18.0] - 2026-09-08

- The scorer now names every speaker's role — **Caller**, **Agent** (the live
  person), **Voice AI** (the assistant that answers first; its name comes from
  `L1QA_VOICE_AI_NAME`, default *Emma*) or **Other** (e.g. the second agent after
  a transfer) — and flags a **Transferred call**. Only the live agent's handling
  is scored; the voice AI's turns are context. The transcript colours each role;
  a Speakers legend above the transcript lets the reviewer correct a role, which
  also sets who is scored (replaces the Swap agent/customer button). Exports list
  the speakers.

## [0.17.0] - 2026-09-08

- Customer sentiment is a colour-coded banner at the top of the scorecard —
  *Start → End*, the overall verdict and a plain reading such as "started
  unhappy, ended satisfied" — instead of a small pill.

## [0.16.0] - 2026-09-08

- The scorer reads the customer's details from the call itself — name, company,
  email, phone and any reference number the caller states — and fills the
  recording's Customer fields where they are blank, rebuilding the audit name to
  "Agent — Company — Caller — date". Anything a reviewer typed is never
  overwritten; the Customer card stays editable. Phone and reference show on the
  card and in the CSV. The scoring prompt is now version 5, so earlier audits
  are re-run rather than reused when scored again.

## [0.15.0] - 2026-09-08

- Upload tab: the Customer name, company, email and Call ID boxes are gone. The
  **call ID is the file name** without its extension (`1700000000.12345.mp3` →
  `1700000000.12345`), editable later on the call's details. A file that matches
  an earlier upload — same call ID, or same name and size — is flagged **amber**
  as soon as it is dropped, naming the earlier audit and whether it was already
  audited, with a link to open it; Start stays available. Identical audio is
  still detected once uploaded. **Remove** takes a file off the list before it is
  sent; Start all asks before starting flagged files. Recordings uploaded before
  this version get their call ID filled in from the file name at start-up (blank
  ones only), so the check also finds older calls.
- API: `GET /api/recordings/lookup?call_ref=&filename=&bytes=`; the upload
  response gains `duplicates` (how each earlier call matched and whether it is
  audited).

## [0.14.13] - 2026-09-08

- Fixed — a request body whose fields were not text (a number or list where a
  name, email, kind or provider was expected) was answered 500 "Internal
  error"; it is now a 400 that names the problem. A body over 1 MB sent to an
  override or review no longer parses as "no score" and clears the override —
  it is refused as too large (413).
- RUNBOOK §8 no longer implies the `@reboot` crontab line is already present;
  it says how to check and how to add it.

## [0.14.12] - 2026-09-08

- Fixed — a quoted `.env` value followed by a comment
  (`SESSION_SECRET="…"   # rotate quarterly`) kept its quotes, so the key or
  secret was wrong by two characters. Quoted values now end at the closing
  quote and the comment is dropped.
- Fixed — audio retention was measured from the file's modification time, so
  restoring or rsync-ing `uploads/` reset the clock and customer audio was kept
  indefinitely. It is now measured from the recording's upload time.

## [0.14.11] - 2026-09-08

- Fixed — a failed job's error message could carry the server's absolute path
  to the audio file (the error from opening a recording that retention removed
  moments earlier). Paths are redacted before the message is stored or shown.
- Fixed — a job interrupted by a restart on its last attempt stayed
  "transcribing" or "scoring" forever, with Retry refused. It is now marked
  failed at start-up with a note to use Retry.
- Fixed — Retry accepted any provider name and an unknown one silently ran on
  Deepgram; it now refuses names that are not Deepgram or OpenAI (400) and
  checks the key first. An unknown provider name can no longer fall back to a
  paid engine anywhere.
- Fixed — a provider that accepted the request but timed out while answering
  produced a bare traceback (and a 500 from Extract criteria) instead of the
  usual "provider unreachable" message. Such a timeout is not retried, so a
  transcription that was already accepted is never paid for twice.
- Fixed — a model answer that was a JSON list instead of an object crashed the
  scorer with a traceback; it is now a clear "returned a list" failure.

## [0.14.10] - 2026-09-08

- Fixed — Dashboard › QA activity counted an audit only if the *call* was
  uploaded inside the date range. An audit submitted (or coaching delivered)
  inside the range on a call uploaded before it is now counted on the day the
  work was done, as the tab's description says.

## [0.14.9] - 2026-09-08

- Fixed — ticking "coaching delivered" on a submitted audit re-credited the
  audit to whoever ticked the box; the CSV "Reviewed by" and the PDF then named
  the wrong QA. The submitter stays the author.
- Fixed — a second run of the same call (rescore, audit again, transcribe
  again) could be submitted while the first run's audit was already submitted,
  leaving two final scores for one call. Submit now answers 409 "already has a
  submitted audit on another run — reopen it first", as Start audit already did.

## [0.14.8] - 2026-09-08

- Fixed — a criterion the model rated *not applicable* accepted an override; the
  score showed on the card, CSV and PDF but was never part of the total, and
  overriding a critical n/a item to 0 wrongly set AUTO-FAIL. Overrides on an
  n/a item are now refused with an explanation, and n/a items can never fail a
  call.
- Fixed — a call where every criterion was n/a was recorded as a real 0.0 %, frozen
  into the final QA score and averaged into the Dashboard. It now shows as
  "n/a" on the scorecard, exports and job list, and is left out of averages.

## [0.14.7] - 2026-09-08

- Fixed — in demo mode, Retry on a failed Demo-run upload after switching the
  run back to Real still sent the file to the fake engine. Retry restores the
  default transcription engine.

## [0.14.6] - 2026-09-08

- Fixed — evidence timestamps the model returned with brackets (`[02:18]`,
  about one in eight) were shown as `[[02:18]]` and clicking them did not move
  the player. Timestamps are stored bare (`h:mm:ss` is converted) and the
  player accepts either form for scorecards saved before this fix.

## [0.14.5] - 2026-09-08

- Fixed — the Dashboard tiles' big numbers rendered at body size and the
  average-score / auto-fail colouring never applied (a selector mismatch).
- Fixed — the score on a finished upload row had no colour; it now uses the
  same green / amber / red pill as the Calls table.
- Fixed — the 90 % target line on the trend chart drew as an ordinary grid line;
  it is now dotted amber, as the legend says.

## [0.14.4] - 2026-09-08

- Fixed — the page's CSP is nonce-only for styles, and a nonce never applies to
  an inline `style=""` attribute, so every one of the 41 in the page was refused
  by the browser: the upload progress bar never moved, the criteria table's
  column widths were lost, the Rescore / Audit again / Transcribe again menus
  took a full row each, and a dozen spacings were off. All inline styles are
  now classes; the progress width is set through the CSSOM, which the CSP
  allows. The test suite now refuses any `style=` in the served page.

## [0.14.3] - 2026-09-04

- This session's uploads: each row is a colour-coded card — blue while the
  job is working, green when done, red when failed — the transcription engine
  and the job status are coloured pills matching the Calls table, the file
  name is bold, Open is the primary action on a finished row, and the
  "transcript reused — no transcription charge" note is a green callout.
  Status pills everywhere gained a tinted background.

## [0.14.2] - 2026-09-04

- Fixed — the flowchart showed its source and "Parse error on line 6" instead
  of drawing: the highlighted transcription engine was given two class names
  in Mermaid's one-name `:::` shorthand. The highlight is now a separate
  `class` line. The test suite parses every variant of the diagram with the
  vendored Mermaid in Node (`tests/mermaid_parse.js`), so a diagram that would
  fail in the browser fails the build first.

## [0.14.1] - 2026-09-04

- The flowchart no longer sits on the Upload page; it opens from the **Flow**
  button (and the changelog dialog's Flow view) in a near-full-width dialog
  with zoom — − / Fit / + buttons, Ctrl+wheel, and a scrolling canvas to pan.
- Each step has its own colour — recording blue, transcription teal, AI
  scoring violet, scorecard green, QA audit orange — in both light and dark
  themes; the transcription engine the next file will use keeps a heavier
  outline.

## [0.14.0] - 2026-09-04

- The flowchart is drawn with Mermaid from one text source in the page —
  edit the diagram as text, not as SVG coordinates. Mermaid 11.4.1 is
  vendored under `static/` and served from this server (nothing external);
  its own styling is refused by the page's nonce-only CSP, so the drawing is
  coloured with the theme tokens after render and follows light/dark mode.
- A **Flow** button next to the version chip opens the flowchart from any
  tab. The copy on the Upload page has **Hide** / **Show**, remembered per
  browser; the changelog dialog's Flow view uses the same drawing.

## [0.13.3] - 2026-09-04

- The "How a call is processed" flowchart shows the whole workflow: a fifth
  step, **QA audit — a person** (Start audit in Calls → Audit › To audit →
  submit the final score → Completed), and step 3 is now called "AI scoring"
  so that "audit" means the human review everywhere. Submitting says the call
  moved to Completed, reopening says it is back under To audit, and the back
  button in the Audit workspace reads "‹ Audit".

## [0.13.2] - 2026-09-04

- Start audit offers only active QA reviewers from Settings › Names — the
  live production list. A call whose QA of record is archived shows it as
  archived and asks for an active reviewer instead of offering the archived
  name and failing. An admin can add a QA reviewer from inside the dialog
  (+ Add a QA reviewer), and the new name is selected at once.
- The "Unknown QA name … Settings › Agents & QA reviewers" messages now say
  whether the name is archived or not on file, and point at Settings › Names,
  which is what the tab is called.

## [0.13.1] - 2026-09-04

- Fixed — a POST whose handler never read its JSON body (Remove demo names,
  archive or delete a name, load or remove demo data, and others) left the
  body on the keep-alive connection, so the browser's next request on that
  connection was answered 400. The server now drains an unread body after
  every POST (and closes the connection when it is too large to drain, as
  after a refused upload). Covered by a keep-alive test.

## [0.13.0] - 2026-09-04

- Production carries nothing demo. `--seed-rubric` no longer seeds the
  placeholder names Jordan, Priya, Marcus and Demo QA — the team's agents and
  QA reviewers are added under Settings › Names. Outside demo mode the Upload
  "Demo run" option, the Load demo data button and the "Demo runs" filters are
  hidden (Remove demo data stays while demo rows exist). Demo mode is
  unchanged. Settings › Names › Remove demo names remains for a database
  seeded before this version.

## [0.12.2] - 2026-09-04

- Start audit no longer defaults to "keep the call's own QA" when the call has
  none — which was most calls, so it skipped with "Choose the QA of record".
  The dialog now preselects the call's QA of record, or the only QA name when
  there is just one, and otherwise asks for a choice; Start audit stays
  disabled until there is one, and a single call that could not be started
  shows the reason plainly. With no QA names at all, the dialog offers a jump
  to Settings › Names.

## [0.12.1] - 2026-09-04

- A 404 from the API now says the server may be running an older build than
  the page, with the restart hint; `/health` and `/api/health` report
  `started_at`. Prompted by a LAN server that had been up since before the
  Audit hand-off existed: it served the new page from disk but answered
  Start audit with a bare "Not found."

## [0.12.0] - 2026-09-04

- **Start audit** replaces "Send to audit" in Calls. On a call row or its
  scorecard it asks for the QA of record, queues the call and opens it in the
  Audit tab so the review begins at once; ticking several calls queues them
  together as before.
- The Audit tab shows only calls an audit was started on, under two sub-tabs —
  **To audit** (queued + in review) and **Completed** (final score submitted,
  coached or not) — each with a count and a narrower state filter (Queued / In
  review; Audited / Coached). The "Not sent to audit" and "All scored calls"
  options are gone; Completed is ordered by submission date.
- API: `/api/reviews` defaults to `status=todo`, accepts `done`, `audited` and
  `coached`, and returns `counts` for the two sub-tabs. `status=none` (never
  started) remains for scripts; the UI no longer offers it.

## [0.11.1] - 2026-09-04

- "Call reference" is now "Call ID" everywhere — upload form, Calls table
  column, scorecard header and Customer card, Call details card, search hints,
  CSV and PDF exports — with a tooltip explaining it is the ID of the call in
  the PBX call reports. The API and export field name `call_ref` is unchanged.

## [0.11.0] - 2026-09-04

- The Customer card on a scorecard is editable in place — ✎ in the card header
  opens Name, Company, Email and Call reference for editing; Save writes them
  through the same details endpoint the Audit tab's Call details card uses, so
  the audit name and header line update and the change lands in the audit log.
  Enter saves, Escape or Cancel restores the read-only view.

## [0.10.0] - 2026-09-04

- Calls → Audit is now an explicit hand-off. Every scored call shows its stage
  in Calls — scored · queued for audit · in review · audited · coached — and a
  **Send to audit** action puts it in the Audit queue with a QA of record: per
  row, from the scorecard, or tick several and send them together. The Audit
  queue defaults to "To do" (queued + in review); a queued call can be taken
  back out until someone writes in it. Saving anything moves it to "in review";
  submitting freezes the final score as before. A call whose audit is already
  submitted cannot be queued again on another scorecard. The QA review card has
  "Open call in Calls"; a scorecard in Calls has "Send to audit" / "Open review".
- Call details are editable in the Audit workspace — agent, QA of record,
  customer name, company and email, call reference and audit name — so calls
  uploaded without an agent can be attributed and appear per agent on the
  Dashboard. Names are validated against Settings › Names when they change;
  a blank audit name is rebuilt; the QA of record is locked while the audit is
  submitted. The audit log records each change.
- Demo data on any server — Settings › Names › Demo data: **Load demo data**
  adds ~44 past calls across Jordan, Priya and Marcus with reviews, sentiment
  and call reasons, tagged DEMO with no audio; **Remove demo data** deletes them
  again (also `--load-demo-data` / `--remove-demo-data`). The Dashboard gains a
  Calls switch — Real (default) · Demo · All — so demo data is never mixed into
  real totals unless asked for. Fixed — the demo-mode history rows pointed at
  the sample call's audio file; they now carry no file at all.
- The `review` table's status list gained "queued"; a database created with
  0.9.0 is rebuilt in place on the next start, keeping its rows.

## [0.9.0] - 2026-09-03

- Audit tab — the human QA review lives here. The queue lists every scored call
  with its review state (not audited · draft · submitted), agent, QA of record,
  sentiment and call reasons. Open a call to correct scores with ✎, write the
  coaching notes, resolution and action plan, set a follow-up date, tick
  "coaching delivered", and Submit. Submit freezes the **final QA score** (the
  scorecard after your overrides) and locks overrides and speaker swaps until
  the review is reopened; the audit log records who submitted and who reopened.
- Dashboard tab — QA score trend lines site-wide, per agent and per QA of
  record, by day, week, month, quarter or year, with presets (30 days, 90 days,
  12 months, year to date) or a custom range; tiles for calls, average, audited,
  coaching delivered, auto-fails; audits submitted and coaching sessions
  delivered per QA per period; customer sentiment at the start, end and
  overall; what customers call about; per-agent and per-QA tables. Hover any
  point for the value. One value per recording (a rescored call counts once —
  the reviewed scorecard wins, else the newest); "Audited only" restricts to
  submitted reviews. Real calls only (0.10.0 added a Real / Demo / All switch).
- Sentiment and call reasons — the scorer now returns the customer's mood at
  the start, the end and overall, and 1–4 call reasons from a fixed list (no
  service, call quality, voicemail, call routing, provisioning, network,
  billing, porting, hardware, account access, how-to, outage, other) each with
  a one-line detail. Shown as pills on the scorecard and in the Audit queue,
  counted on the Dashboard, and in the CSV and PDF. Prompt version 4: calls
  scored earlier show "not classified" until audited again, and "Audit again"
  with the same model on such a call runs again rather than offering the old
  result.
- Exports carry the review: final QA score, status, reviewer, coaching notes,
  resolution, action plan, follow-up, coaching delivered (CSV rows; a "QA
  review" block in the PDF).
- Demo mode seeds ~44 past calls across the three demo agents and two QA names
  with reviews on most, so the Dashboard and the Audit queue have something to
  show. `L1QA_DEMO_HISTORY=0` turns that off (the test-suite does). Their audio
  is not stored.
- Renamed scorecard rendering into a builder plus a binder so the same
  scorecard draws in Calls and in the Audit workspace; a scorecard in Calls now
  has an "Audit this call" button.

## [0.8.1] - 2026-09-02

- Auto-fail explained — the scorecard names the critical criterion behind an
  AUTO-FAIL and quotes the rule (skipping it fails the call regardless of the
  rest); the PDF headline names it too; the override dialog says a score above
  0 clears the flag.
- Fixed — overriding a critical criterion did not recompute the auto-fail: a
  reviewer who corrected "missed" to a partial score still saw AUTO-FAIL. The
  flag now follows overrides (0 keeps it, anything above 0 clears it, clearing
  the override restores the model's view).
- Scorer told that "missed" means the behaviour was absent and any attempt is
  "partial", and to rate a critical criterion missed only when the transcript
  shows no attempt at all. Prompt version 3.

## [0.8.0] - 2026-09-02

- Never pay twice for the same audio — a recording whose bytes were already
  transcribed by the same engine (the same call uploaded again, or "Transcribe
  again" with an engine already used) gets that transcript copied, free. The
  uploads panel says so and links to the earlier call; the scorecard header
  shows a "reused" pill.
- Never pay twice for the same audit — Audit again / Rescore first check for a
  finished scorecard of the same transcript, rubric version, model and prompt
  version, and offer "Open existing" or "Run again anyway" before anything is
  billed.
- Spend visibility — every job records the tokens (including cached ones) and
  audio minutes it actually used; Settings › Server shows totals and a
  list-price estimate. Anthropic calls mark the system prompt cacheable.

## [0.7.2] - 2026-09-02

- Start before spending — Rescore with rubric…, Audit again with… and Transcribe
  again with… now open a box that names the choice, what will happen and the
  cost, with Start and Cancel. Nothing is queued until Start.
- Fixed — the Criteria table no longer clips or spills: it sits full-width
  below the summary/transcript row with fixed column widths, so it can never be
  wider than its card; long quotes wrap. On phones it stacks into blocks.
- Small screens — rubric editor rows and the uploads panel stack below 800 px
  and 560 px; tables inside pop-ups scroll within the pop-up.

## [0.7.1] - 2026-09-02

- Fixed — auditing with gpt-5.6-luna failed: OpenAI's newer models reject the
  `max_tokens` parameter (and a custom temperature). The scorer now sends
  `max_completion_tokens`, which every current model accepts, and sets a
  temperature only for the gpt-4 family.

## [0.7.0] - 2026-09-02

- Transcribe again — on a scorecard, "Transcribe again with…" re-runs the audio
  through another engine (Deepgram nova-3, OpenAI whisper-1 or OpenAI
  gpt-4o-transcribe) and scores it; the per-file menu on Upload offers the same
  three. A new run is added; the old one stays. Only Deepgram labels speakers;
  gpt-4o-transcribe returns no timestamps.
- Audit with another LLM — "Audit again with…" rescores the same transcript
  with gpt-4o-mini (default), gpt-5.6-luna or gpt-4.1-mini; Anthropic Claude
  appears when ANTHROPIC_API_KEY is set. The Upload form has an "Audit with"
  choice for new calls. Settings › Server shows which menu entries have a key.
- The job now records the engine and model that were asked for; the scorecard
  header shows both.

## [0.6.2] - 2026-09-02

- Fixed — the Criteria table could run out of its card and under the transcript
  pane. The two-column layout now lets each column shrink to its track, and the
  table scrolls inside its card if a screen is narrower than it needs.
- Loading animation — while a call is transcribing or scoring the job page shows
  a four-step tracker (Uploaded · Transcribing · Scoring · Scorecard) with a
  spinner and a moving bar, then a "Loading scorecard…" state before the
  scorecard appears. The uploads panel follows each file live (queued →
  transcribing → scoring → done with its score) instead of saying "queued" until
  you click Open, and busy rows in Calls carry the spinner too. Animations are
  turned off for browsers that ask for reduced motion.

## [0.6.1] - 2026-09-02

- Fixed — the job page could sit on "Scoring…" forever: opening a job from the
  uploads panel let the Calls list cancel the job page's refresh timer. The two
  now have separate timers, a failed refresh retries with backoff instead of
  giving up, a late response for a job you already left is ignored, and the
  page shows how long ago the job started plus a Refresh now button. Polling
  pauses while the browser tab is hidden and resumes when it is shown.

## [0.6.0] - 2026-09-02

- Transcription provider per file — dropped files now wait in "This session's
  uploads" as Ready, each with a Deepgram / OpenAI choice and a Start button
  (Start all for a batch). Nothing is sent until you press Start; a failed row
  offers Retry. The form's Transcription dropdown is gone. QA scoring is always
  OpenAI, and the panel says so.
- Flowchart — "How a call is processed" on the Upload tab and a Flow tab under
  the version chip: Recording → Transcription (your choice, per file) → QA
  scoring (OpenAI, always) → Scorecard. The provider the next file will use is
  highlighted. Inline SVG in theme colours; nothing external.

## [0.5.3] - 2026-09-02

- Fixed — the scorer could skip criteria: on the first real call gpt-4o-mini
  scored 1 of 8 and the rest were recorded as missed (10 %, auto-fail). The
  schema now names every criterion as a required field, so the model cannot
  leave one out; the prompt says so too, and if anything is still missing the
  scorer is asked once more before the result is accepted. The QA summary is
  now required to be a verdict, distinct from the call summary.
- Fixed — PDF filename no longer repeats the date when the audit name is only a date.

## [0.5.2] - 2026-09-02

- Fixed — a pasted key that does not look like one (an OpenAI key missing its
  `sk-` prefix, a Deepgram key that is not 40 hex characters) is now caught at
  upload, before the transcription is paid for, with a message naming the
  variable and what a right key looks like. Settings › Server and the Upload
  banner flag it too. Previously the job failed only at scoring, after the
  Deepgram call. The scoring key is now also checked at upload, not just the
  transcription key.

## [0.5.1] - 2026-09-02

- Run as — the Demo run / Test call checkboxes on Upload are now one toggle:
  Real call · Demo run · Test call.

## [0.5.0] - 2026-09-02

- Settings with tabs — Guidelines moved off the top bar into Settings, which now
  has Guidelines · Names · Server · Account. The top bar is Upload · Calls · Settings.
- Remove demo names — one button under Settings › Names deletes the seeded
  Jordan, Priya, Marcus and Demo QA; any already used by a call is archived instead.
- Demo run and Test call — two options on Upload. Demo run uses the fake
  transcriber and scorer (no keys, no cost, the canned sample transcript); Test
  call transcribes and scores the real audio. Both are tagged DEMO / TEST in
  Calls and on the scorecard and PDF, and the Calls list shows real calls only
  by default (filter: Real · All · Demo runs · Test calls). Anything that ever
  totals scores must use real calls only.

## [0.4.0] - 2026-09-02

- Real transcription on the LAN link — `restart.sh` now runs real mode, so each
  upload is transcribed (Deepgram) and scored (OpenAI) from its own audio. Demo
  mode returned the same canned sample call for every file; it stays available
  behind `--demo`. New `--seed-rubric` puts the sample rubric and the agent/QA
  name lists into the real database so a fresh install can upload at once. The
  Upload tab says so when no transcription key is set yet.
- Customer section — Customer name, company and email on Upload (email checked
  for shape), a Customer card on the scorecard, and both in the PDF and CSV.
- Call summary — the scorer now also writes two or three plain sentences on what
  the call was about (who called, what for, what was done, how it ended), shown
  above the QA summary on the scorecard and in the PDF and CSV.
- Fixed — an `.env` line with an empty value followed by a comment
  (`OPENAI_API_KEY=   # …`) was read as the comment text, so Settings claimed a
  key was present. It now reads as empty.

## [0.3.2] - 2026-09-02

- Footer — "Created by Ron Mangune" with a LinkedIn glyph, linking to the profile
  in a new tab. Inline SVG, so the page still loads nothing external.

## [0.3.1] - 2026-09-02

- QA reviewer on Upload is optional — leave it at "— none —" and the call simply
  carries no QA name. When chosen it must still be one of the managed names.

## [0.3.0] - 2026-09-02

- Agents and QA reviewers — managed under Settings: add one name or paste or
  import a list (.txt/.csv), rename (past calls are renamed too), archive, and
  delete while unused. Upload picks the agent from a dropdown and requires a QA
  reviewer; both appear on the Calls table, the scorecard and the PDF/CSV exports.
- Audit name — every call gets a title built as Agent — Caller's company —
  Caller's name — date, filled in live on Upload and editable. It heads the
  Calls table, the scorecard and the PDF, and names the downloaded file. New
  optional Caller's company and Caller's name fields.
- Tooltips — every Upload field and every name-management control explains
  itself on hover.

## [0.2.0] - 2026-09-02

- Open access — `L1QA_OPEN_ACCESS=1` removes sign-in: every visitor acts as the
  built-in `open-access@local` admin, and the Sign out, Users and password
  cards disappear. Set on the LAN demo link. The TLS installer refuses to
  install while it is set, so real recordings stay behind a login.

## [0.1.2] - 2026-09-02

- Fixed — the page rendered blank: two mismatched quotes in the front-end
  script stopped it from running, so neither the sign-in form nor the app ever
  appeared. The test suite now syntax-checks the script when `node` is
  installed on the host.
- Fixed — `/favicon.ico` answers 204 instead of logging a 401 on every load.

## [0.1.1] - 2026-09-02

- Dev link — `./restart.sh` serves the demo at
  `http://qa-server.example.com:8084/l1qa/`: plain HTTP on the LAN in the same
  pattern as the sibling tools on this box, restarted at boot by one crontab
  line. The TLS installer in `deploy/` remains the production route.
- Fixed — the `.env` loader now strips inline `# comments`, so `cp .env.example .env`
  yields clean settings. Previously `L1QA_BIND` was read as `0.0.0.0   # …` and the
  server could not bind. A `#` inside a token or a quoted value is kept.

## [0.1.0] - 2026-09-01

First working version.

- Upload and transcribe — a reviewer drops a call recording (mp3, wav, m4a,
  ogg, webm) onto the Upload tab and it is transcribed in the background by
  Deepgram (default, with speaker separation) or OpenAI, chosen per upload.
- Guidelines to rubric — QA guidelines can be pasted or uploaded as .txt, .md
  or .docx; the tool proposes weighted criteria that sum to 100, the reviewer
  edits and saves them as a versioned rubric. Weights that do not total 100
  cannot be saved.
- Scorecard — each call is scored per criterion on a 100-point basis with
  verbatim evidence quotes and timestamps, an overall percentage, Strengths,
  Opportunities and Misses. A criterion marked critical auto-fails the call
  when missed.
- Reviewer overrides — any criterion score can be overridden with a note; the
  overall recomputes and the change is audited.
- Exports — scorecard as PDF, CSV or JSON.
- Version chip and changelog — the version in the top bar opens this changelog
  and the v1 plan, drawn in-page.
- Demo mode — `--demo` seeds a sample rubric and a scored sample call with no
  API keys and no network.
