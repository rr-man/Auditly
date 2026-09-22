-- Auditly schema. Applied with executescript on every start; every
-- statement is IF NOT EXISTS so it is safe to re-run.
--
-- Boundaries worth knowing before you add a column:
--   * A rubric_version's AUTHORED criteria are IMMUTABLE once any scorecard
--     references it. Editing a rubric always creates a new version, so an old
--     scorecard can always be read against the exact criteria that produced it.
--     criterion.opt_* ("Optimise for the scorer") is DERIVED from the authored
--     text and may only be (re)written while no scorecard on that version has
--     guidance_mode = 'optimised'.
--   * recording.path and recording.sha256 never leave the server; to_public()
--     in auditly_host.py is the whitelist that decides what a browser sees.
--   * scorecard_item.score is the model's (clamped) number. override_score is a
--     human's. The UI and exports show COALESCE(override_score, score).
--   * IF NOT EXISTS cannot add a column to a table that already exists. When you
--     add a column here, also add it to core._migrate(), which ALTERs live DBs.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS user (
  id          TEXT PRIMARY KEY,
  email       TEXT NOT NULL UNIQUE COLLATE NOCASE,
  name        TEXT,
  pw_hash     BLOB,
  pw_salt     BLOB,
  role        TEXT NOT NULL CHECK (role IN ('admin','reviewer')),
  active      INTEGER NOT NULL DEFAULT 1,
  created_at  TEXT NOT NULL,
  last_login  TEXT
);

CREATE TABLE IF NOT EXISTS session (
  token_hash  BLOB PRIMARY KEY,
  user_id     TEXT NOT NULL REFERENCES user(id) ON DELETE CASCADE,
  created_at  TEXT NOT NULL,
  expires_at  TEXT NOT NULL,
  ip          TEXT
);

CREATE TABLE IF NOT EXISTS login_attempt (
  at     TEXT NOT NULL,
  email  TEXT,
  ip     TEXT
);

CREATE TABLE IF NOT EXISTS rubric (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,
  archived    INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT NOT NULL,
  created_by  TEXT
);

CREATE TABLE IF NOT EXISTS rubric_version (
  id               TEXT PRIMARY KEY,
  rubric_id        TEXT NOT NULL REFERENCES rubric(id) ON DELETE CASCADE,
  version_no       INTEGER NOT NULL,
  source_text      TEXT,              -- the guidelines this version was built from
  source_filename  TEXT,
  notes            TEXT,
  created_at       TEXT NOT NULL,
  created_by       TEXT,
  -- "Optimise for the scorer": manual, none -> pending -> running -> done | failed.
  sync_status      TEXT NOT NULL DEFAULT 'none',
  sync_error       TEXT,               -- final error of the last run; redacted, shown in the UI
  sync_attempts    INTEGER NOT NULL DEFAULT 0,   -- tries inside the current run (max 3)
  sync_runs        INTEGER NOT NULL DEFAULT 0,   -- completed runs; the per-version cap
  sync_started_at  TEXT,
  sync_finished_at TEXT,
  sync_provider    TEXT,
  sync_model       TEXT,
  sync_usage_json  TEXT,               -- {prompt_tokens, completion_tokens, cached_tokens, calls}
  UNIQUE (rubric_id, version_no)
);

CREATE TABLE IF NOT EXISTS criterion (
  id                 TEXT PRIMARY KEY,
  rubric_version_id  TEXT NOT NULL REFERENCES rubric_version(id) ON DELETE CASCADE,
  seq                INTEGER NOT NULL,
  key                TEXT NOT NULL,     -- slug; the enum the scoring schema is built from
  name               TEXT NOT NULL,
  description        TEXT,
  weight             INTEGER NOT NULL CHECK (weight BETWEEN 0 AND 100),
  guidance_met       TEXT,
  guidance_partial   TEXT,
  guidance_missed    TEXT,
  example_good       TEXT,              -- 0.61.0: sample wording that earns met (calibration for the scorer, never a rule)
  example_bad        TEXT,              -- 0.61.0: sample wording that earns missed
  critical           INTEGER NOT NULL DEFAULT 0,   -- 1: missing it auto-fails the call
  -- Decision rules written by "Optimise for the scorer"; NULL = the authored text is in use.
  opt_description    TEXT,
  opt_met            TEXT,
  opt_partial        TEXT,
  opt_missed         TEXT,
  opt_na             TEXT,
  UNIQUE (rubric_version_id, key)
);
-- Application-level rule enforced on save: SUM(weight) per rubric_version = 100.

-- Managed name lists shown as dropdowns on Upload. Names are copied onto the
-- recording as text; renaming a person also renames matching recordings.
CREATE TABLE IF NOT EXISTS person (
  id          TEXT PRIMARY KEY,
  kind        TEXT NOT NULL CHECK (kind IN ('agent','qa')),
  name        TEXT NOT NULL,
  active      INTEGER NOT NULL DEFAULT 1,  -- 0 = archived: hidden from Upload, kept on past calls
  email       TEXT,                        -- optional; agents: the coaching invite's mailto / ATTENDEE
  created_at  TEXT NOT NULL,
  created_by  TEXT,
  UNIQUE (kind, name COLLATE NOCASE)
);

CREATE TABLE IF NOT EXISTS recording (
  id                TEXT PRIMARY KEY,
  filename          TEXT NOT NULL,
  ext               TEXT NOT NULL,
  mime              TEXT,
  bytes             INTEGER NOT NULL,
  sha256            TEXT,
  path              TEXT NOT NULL,      -- server-side only
  duration_s        REAL,
  agent_name        TEXT,                -- from the person table (kind='agent')
  qa_name           TEXT,                -- from the person table (kind='qa'); required at upload
  caller_company    TEXT,                -- shown as "Customer" in the UI
  caller_name       TEXT,
  caller_email      TEXT,
  caller_phone      TEXT,                -- 0.61.0: captured from the call by the scorer, editable
  ticket_ref        TEXT,                -- 0.61.0: the ticket / case / reference number quoted on the call, editable
  audit_name        TEXT,                -- "Agent — Company — Caller — YYYY-MM-DD" unless edited
  kind              TEXT NOT NULL DEFAULT 'real',  -- real | demo (fake providers) | test (real providers, practice).
                                                   -- Totals default to kind='real'; the Dashboard's Real/Demo/All
                                                   -- switch is the one deliberate exception. Loaded demo history
                                                   -- rows also carry uploaded_by='demo-history@local'.
  call_ref          TEXT,
  notes             TEXT,
  uploaded_at       TEXT NOT NULL,
  uploaded_by       TEXT,
  audio_deleted_at  TEXT,               -- set by the retention sweep or a delete
  deleted_at        TEXT
);

CREATE TABLE IF NOT EXISTS job (
  id                  TEXT PRIMARY KEY,
  recording_id        TEXT NOT NULL REFERENCES recording(id) ON DELETE CASCADE,
  rubric_version_id   TEXT NOT NULL REFERENCES rubric_version(id),
  stt_provider        TEXT NOT NULL,
  stt_model           TEXT,
  scoring_provider    TEXT,
  scoring_model       TEXT,               -- the model the reviewer asked for; scorecard.model is what ran
  transcript_mode     TEXT NOT NULL DEFAULT 'auto',  -- auto = reuse an existing transcript; fresh = transcribe again
  usage_json          TEXT,               -- {prompt_tokens, completion_tokens, cached_tokens, audio_s, cached, calls}
  carry_from_scorecard_id TEXT,           -- rescore/audit again: copy overrides (same rubric version) + audit notes from this run
  status              TEXT NOT NULL CHECK (status IN ('queued','transcribing','scoring','done','failed')),
  progress            TEXT,
  error               TEXT,
  attempts            INTEGER NOT NULL DEFAULT 0,
  created_at          TEXT NOT NULL,
  started_at          TEXT,
  finished_at         TEXT,
  created_by          TEXT
);
CREATE INDEX IF NOT EXISTS job_created ON job(created_at DESC);
CREATE INDEX IF NOT EXISTS job_status ON job(status);

CREATE TABLE IF NOT EXISTS transcript (
  id            TEXT PRIMARY KEY,
  recording_id  TEXT NOT NULL REFERENCES recording(id) ON DELETE CASCADE,
  job_id        TEXT,
  provider      TEXT NOT NULL,
  model         TEXT,
  language      TEXT,
  full_text     TEXT,
  duration_s    REAL,
  cached_from   TEXT,                    -- transcript id this one was copied from (identical audio, same engine): no charge
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS recording_sha256 ON recording(sha256);

CREATE TABLE IF NOT EXISTS utterance (
  transcript_id  TEXT NOT NULL REFERENCES transcript(id) ON DELETE CASCADE,
  seq            INTEGER NOT NULL,
  start_s        REAL NOT NULL,
  end_s          REAL NOT NULL,
  speaker        INTEGER,               -- provider label; NULL when not diarized
  text           TEXT NOT NULL,
  confidence     REAL,
  sentiment      TEXT,                  -- negative|neutral|positive from the STT provider's text sentiment (Deepgram, opt-in)
  sentiment_score REAL,                 -- -1..1, same source; NULL when not requested
  PRIMARY KEY (transcript_id, seq)
);

CREATE TABLE IF NOT EXISTS scorecard (
  id                 TEXT PRIMARY KEY,
  job_id             TEXT NOT NULL UNIQUE REFERENCES job(id) ON DELETE CASCADE,
  recording_id       TEXT NOT NULL REFERENCES recording(id) ON DELETE CASCADE,
  transcript_id      TEXT NOT NULL REFERENCES transcript(id),
  rubric_version_id  TEXT NOT NULL REFERENCES rubric_version(id),
  agent_speaker      INTEGER,           -- which utterance.speaker is the agent (scorer's call, reviewer can swap)
  overall_pct        REAL NOT NULL,
  applicable_weight  INTEGER NOT NULL,  -- 100 minus the weight of 'na' criteria
  auto_fail          INTEGER NOT NULL DEFAULT 0,
  summary            TEXT,               -- the QA summary (judgement)
  call_summary       TEXT,               -- what the call was about, no judgement
  strengths_json     TEXT NOT NULL DEFAULT '[]',
  opportunities_json TEXT NOT NULL DEFAULT '[]',
  misses_json        TEXT NOT NULL DEFAULT '[]',
  warnings_json      TEXT NOT NULL DEFAULT '[]',
  truncated          INTEGER NOT NULL DEFAULT 0,
  model              TEXT,
  prompt_version     INTEGER,            -- score.PROMPT_VERSION that produced it; part of the audit-cache key
  created_at         TEXT NOT NULL,
  sentiment_json     TEXT,               -- {"start","end","overall"}, each negative|neutral|positive (prompt v4+)
  call_reasons_json  TEXT,               -- [{"category","detail"}] 1-4, category from score.REASON_KEYS
  customer_json      TEXT,               -- {"name","company","email","phone","reference"} as stated on the call (prompt v5+; v8: reference = ticket/case number by either party)
  speakers_json      TEXT,               -- [{"speaker","role","name"}] role caller|agent|voice_ai|other (prompt v5+)
  transferred        INTEGER,            -- 1 when the caller was handed to another person or system (prompt v5+)
  guidance_mode      TEXT,               -- 'optimised' | 'authored' (NULL = authored; rows before 0.28.0)
  tone_json          TEXT,               -- tone.compute(): talk ratio, interruptions, silences, pace, latency (0.33.0)
  kb_json            TEXT,               -- knowledge-base excerpts shown to the scorer: [{document_id,title,seq,chars,text}] (0.36.0)
  kb_key             TEXT,               -- kb.fingerprint() of the documents in scope at scoring time; '' = none; part of the audit-cache key
  call_facts_json    TEXT                -- {"identifiers":{phone,extension,mac_address,ticket,account,device,address},"troubleshooting":[{text,result,key}],
                                         --  "discussed":[{text,key}],"outcome"} -- checked against the transcript by score._facts (prompt v8, 0.61.0)
);

CREATE TABLE IF NOT EXISTS scorecard_item (
  scorecard_id    TEXT NOT NULL REFERENCES scorecard(id) ON DELETE CASCADE,
  criterion_id    TEXT NOT NULL REFERENCES criterion(id),
  rating          TEXT NOT NULL CHECK (rating IN ('met','partial','missed','na')),
  score           INTEGER NOT NULL,     -- model's, clamped to [0, weight]
  weight          INTEGER NOT NULL,     -- copied so the row is self-describing
  rationale       TEXT,
  evidence_json   TEXT NOT NULL DEFAULT '[]',
  override_score  INTEGER,
  override_note   TEXT,
  override_by     TEXT,
  override_at     TEXT,
  PRIMARY KEY (scorecard_id, criterion_id)
);

-- The human QA review of one scorecard: coaching, resolution, action plan and the
-- FINAL score. A call is SENT to audit from Calls (status 'queued'), becomes 'draft'
-- once the QA saves anything, and 'submitted' freezes final_pct (a snapshot of
-- scorecard.overall_pct after overrides); while submitted, overrides and speaker swaps
-- are refused. The QA of record is recording.qa_name; reviewer_email is who was signed
-- in. Changing the CHECK below needs core._rebuild_review(). (`audit` further down is
-- the security log, unrelated.)
CREATE TABLE IF NOT EXISTS review (
  id                TEXT PRIMARY KEY,
  scorecard_id      TEXT NOT NULL UNIQUE REFERENCES scorecard(id) ON DELETE CASCADE,
  recording_id      TEXT NOT NULL REFERENCES recording(id) ON DELETE CASCADE,
  reviewer_email    TEXT,
  status            TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('queued','draft','submitted')),
  final_pct         REAL,
  applicable_weight INTEGER,
  auto_fail         INTEGER,
  coaching_notes    TEXT,
  resolution_notes  TEXT,
  action_plan       TEXT,
  follow_up_on      TEXT,                -- YYYY-MM-DD
  coached_at        TEXT,                -- set when the QA marks the coaching as delivered
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  submitted_at      TEXT
);
CREATE INDEX IF NOT EXISTS review_recording ON review(recording_id);

-- An agent's dispute of a scorecard (or of one criterion when criterion_id is set). Agents have
-- no login: since 0.52.0 they raise it themselves from an emailed link (dispute_link, below), or the
-- QA records it on their behalf; any reviewer/admin resolves it.
-- open -> upheld | rejected | withdrawn. Upholding may apply an override through the normal
-- override path, so a submitted review must be reopened first (final_pct stays frozen).
CREATE TABLE IF NOT EXISTS dispute (
  id                TEXT PRIMARY KEY,
  scorecard_id      TEXT NOT NULL REFERENCES scorecard(id) ON DELETE CASCADE,
  criterion_id      TEXT,                 -- NULL = the whole scorecard
  agent_name        TEXT,                 -- copied from the recording when raised
  reason            TEXT NOT NULL,
  status            TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','upheld','rejected','withdrawn')),
  raised_by         TEXT,                 -- the QA's account email, or the agent's address from the link
  via               TEXT,                 -- 'link' = raised by the agent from their emailed link; NULL/'qa' = recorded by the QA
  resolution_note   TEXT,
  resolved_by       TEXT,
  resolved_at       TEXT,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS dispute_scorecard ON dispute(scorecard_id);
CREATE INDEX IF NOT EXISTS dispute_status ON dispute(status);

-- 0.52.0: the agent's emailed link to a read-only copy of ONE scorecard, on which they raise disputes
-- themselves. Only the sha256 of the token is stored (like session); the raw token lives in the email
-- or the QA's clipboard and nowhere else. Every unexpired row is valid until the audit is reopened
-- (revoked_at) or expires_at passes. Rows go with the scorecard. New table: IF NOT EXISTS suffices.
CREATE TABLE IF NOT EXISTS dispute_link (
  token_hash    BLOB PRIMARY KEY,
  scorecard_id  TEXT NOT NULL REFERENCES scorecard(id) ON DELETE CASCADE,
  agent_name    TEXT,
  sent_to       TEXT,                 -- the address it was emailed to; NULL when copied / mail app
  channel       TEXT NOT NULL,        -- smtp | copy | mail_app
  error         TEXT,                 -- the redacted relay error when an smtp send failed (row is revoked)
  created_by    TEXT,                 -- the QA's email (submit hook or the scorecard's Dispute link action)
  created_at    TEXT NOT NULL,
  expires_at    TEXT NOT NULL,
  revoked_at    TEXT,
  last_used_at  TEXT,
  uses          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS dispute_link_sc ON dispute_link(scorecard_id, revoked_at, expires_at);

-- A coaching session: the QA groups an agent's audited calls, drafts the agenda from their
-- reviews, schedules it (scheduled_at is UTC; the browser supplies its offset) and marks it
-- delivered, which stamps review.coached_at = done_at on every included submitted review.
-- planned -> scheduled -> done | cancelled. A scorecard sits in at most one live (not cancelled) session.
CREATE TABLE IF NOT EXISTS coaching_session (
  id             TEXT PRIMARY KEY,
  agent_name     TEXT NOT NULL,
  qa_name        TEXT,
  title          TEXT,
  status         TEXT NOT NULL DEFAULT 'planned' CHECK (status IN ('planned','scheduled','done','cancelled')),
  scheduled_at   TEXT,
  duration_min   INTEGER NOT NULL DEFAULT 30,
  location       TEXT,                    -- room, Teams link, phone
  agenda         TEXT,
  agenda_edited  INTEGER NOT NULL DEFAULT 0,   -- 0 = still the auto-draft; re-drafted when calls change
  notes          TEXT,                    -- what was said in the session
  created_by     TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  done_at        TEXT,
  cancelled_at   TEXT,
  invite_sent_at TEXT,                    -- last emailed calendar invite (mailer.py); the calendar's ✉ mark
  invite_to      TEXT
);
CREATE INDEX IF NOT EXISTS coaching_session_agent ON coaching_session(agent_name, status);
-- every invite attempt, for the audit trail the calendar mark summarises
CREATE TABLE IF NOT EXISTS coaching_invite (
  id          TEXT PRIMARY KEY,
  session_id  TEXT NOT NULL REFERENCES coaching_session(id) ON DELETE CASCADE,
  to_email    TEXT NOT NULL,
  from_email  TEXT,
  status      TEXT NOT NULL CHECK (status IN ('sent','failed')),
  channel     TEXT,                       -- smtp (the server sent it) | mail_app (the QA sent it from their own app and marked it)
  error       TEXT,
  sent_by     TEXT,
  sent_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS coaching_session_when ON coaching_session(scheduled_at);

CREATE TABLE IF NOT EXISTS coaching_session_call (
  session_id    TEXT NOT NULL REFERENCES coaching_session(id) ON DELETE CASCADE,
  scorecard_id  TEXT NOT NULL REFERENCES scorecard(id) ON DELETE CASCADE,
  seq           INTEGER NOT NULL,
  PRIMARY KEY (session_id, scorecard_id)
);
CREATE INDEX IF NOT EXISTS coaching_session_call_sc ON coaching_session_call(scorecard_id);

-- Knowledge base: reference documents (.txt/.md/.docx) the scorer may consult to judge whether
-- what the agent SAID was accurate. Chunked on upload; searched lexically (kb.py) with the
-- transcript as the query; the excerpts used are kept on the scorecard (kb_json) and the set of
-- documents in scope is fingerprinted (kb_key) so "the same audit" means the same material too.
-- rubric_id NULL = every QA type. Delete is soft (deleted_at) so old scorecards keep their titles.
CREATE TABLE IF NOT EXISTS kb_document (
  id           TEXT PRIMARY KEY,
  title        TEXT NOT NULL,
  filename     TEXT,
  chars        INTEGER NOT NULL DEFAULT 0,
  text         TEXT NOT NULL,
  enabled      INTEGER NOT NULL DEFAULT 1,
  rubric_id    TEXT,
  uploaded_by  TEXT,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  deleted_at   TEXT
);
-- White label: which logo the header shows (auditly | coeo | hidden | custom) and any uploaded brand
-- assets (logo_light, logo_dark, icon_light, icon_dark) kept as blobs so no file path exists to leak.
-- GET /api/branding is public (the sign-in page shows the logo too); writes are admin-only.
CREATE TABLE IF NOT EXISTS branding (
  id          INTEGER PRIMARY KEY CHECK (id = 1),
  logo_mode   TEXT NOT NULL DEFAULT 'auditly' CHECK (logo_mode IN ('auditly','coeo','hidden','custom')),
  updated_at  TEXT,
  updated_by  TEXT
);
CREATE TABLE IF NOT EXISTS brand_asset (
  slot        TEXT PRIMARY KEY CHECK (slot IN ('logo_light','logo_dark','icon_light','icon_dark')),
  blob        BLOB NOT NULL,
  mime        TEXT NOT NULL,
  name        TEXT,
  bytes       INTEGER NOT NULL,
  updated_at  TEXT NOT NULL,
  updated_by  TEXT
);

CREATE TABLE IF NOT EXISTS kb_chunk (
  document_id  TEXT NOT NULL REFERENCES kb_document(id) ON DELETE CASCADE,
  seq          INTEGER NOT NULL,
  text         TEXT NOT NULL,
  PRIMARY KEY (document_id, seq)
);
CREATE INDEX IF NOT EXISTS review_submitted ON review(status, submitted_at);
CREATE INDEX IF NOT EXISTS recording_kind_uploaded ON recording(kind, uploaded_at);

-- Ask Auditly: one thread per (user, call); every turn keeps what produced the answer -- the prompt
-- version, the model, the citations and the token usage -- so a bad answer is traceable afterwards.
-- The assistant never writes anywhere else. Threads are swept by ASK_RETENTION_DAYS. New tables, so
-- IF NOT EXISTS suffices; no core._migrate() entry is needed.
CREATE TABLE IF NOT EXISTS ask_thread (
  id            TEXT PRIMARY KEY,
  user_id       TEXT,
  scorecard_id  TEXT REFERENCES scorecard(id) ON DELETE CASCADE,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ask_thread_user_sc ON ask_thread(user_id, scorecard_id);

CREATE TABLE IF NOT EXISTS ask_turn (
  id              TEXT PRIMARY KEY,
  thread_id       TEXT NOT NULL REFERENCES ask_thread(id) ON DELETE CASCADE,
  seq             INTEGER NOT NULL,
  question        TEXT NOT NULL,
  answer          TEXT NOT NULL,
  cites_json      TEXT NOT NULL DEFAULT '[]',
  grounded        INTEGER NOT NULL DEFAULT 1,
  prompt_version  INTEGER NOT NULL,
  provider        TEXT,
  model           TEXT,
  usage_json      TEXT,
  warnings_json   TEXT,
  created_at      TEXT NOT NULL,
  prompt_id       TEXT                  -- 0.47.0: the ask_prompt version behind this answer; NULL = built-in persona (also in core._migrate())
);
CREATE INDEX IF NOT EXISTS ask_turn_thread ON ask_turn(thread_id, seq);

-- 0.47.0: Settings › Server › Assistant. One row of admin choices that override the ASK_* keys in
-- .env when present (NULL column = fall back to .env), and the editable half of the prompt as
-- immutable versions -- every save is a new row, like rubric_version, so any answer can be traced to
-- the exact text that produced it. persona='' records a reset to the built-in text. The locked rules
-- (assistant.RULES) are never stored here. New tables: IF NOT EXISTS suffices.
CREATE TABLE IF NOT EXISTS ask_setting (
  id                     INTEGER PRIMARY KEY CHECK (id = 1),
  enabled                INTEGER,
  model                  TEXT,
  max_per_day            INTEGER,
  max_per_user_per_hour  INTEGER,
  updated_at             TEXT,
  updated_by             TEXT
);
CREATE TABLE IF NOT EXISTS ask_prompt (
  id          TEXT PRIMARY KEY,
  version_no  INTEGER NOT NULL UNIQUE,
  persona     TEXT NOT NULL,
  notes       TEXT,
  created_at  TEXT NOT NULL,
  created_by  TEXT
);
-- 0.48.0: a spoken question to Ask Auditly, transcribed by the desk's own STT provider. The clip is
-- deleted the moment it is transcribed and the text is not kept; this row exists so the seconds show
-- in Settings' spend and count toward the per-person hourly cap. New table: IF NOT EXISTS suffices.
CREATE TABLE IF NOT EXISTS ask_voice (
  id          TEXT PRIMARY KEY,
  user_id     TEXT,
  created_at  TEXT NOT NULL,
  audio_s     REAL NOT NULL DEFAULT 0,
  provider    TEXT,
  model       TEXT,
  chars       INTEGER NOT NULL DEFAULT 0
);

-- 0.51.0: in-app notifications. Events are shared (one row per event); which kinds a person wants
-- and what they have read are per user. recording_id is set whenever a call is behind the event so
-- deleting the call (or removing demo data) takes its lines with it; coaching events have none and
-- are swept after 90 days (notify.prune). dedupe_key stops the same event landing twice --
-- agent.failing carries the agent and the Dashboard week. New tables: IF NOT EXISTS suffices.
CREATE TABLE IF NOT EXISTS notification (
  id            TEXT PRIMARY KEY,
  kind          TEXT NOT NULL,
  at            TEXT NOT NULL,
  title         TEXT NOT NULL,
  body          TEXT,
  target_kind   TEXT,                 -- scorecard | session | agent | job
  target_id     TEXT,
  agent_name    TEXT,
  actor_email   TEXT,
  recording_id  TEXT REFERENCES recording(id) ON DELETE CASCADE,
  dedupe_key    TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS notification_at ON notification(at DESC);
CREATE TABLE IF NOT EXISTS notification_pref (
  user_id     TEXT PRIMARY KEY REFERENCES user(id) ON DELETE CASCADE,
  off_json    TEXT NOT NULL DEFAULT '[]',   -- kinds this person muted; anything not listed is on
  updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notification_read (
  user_id          TEXT NOT NULL REFERENCES user(id) ON DELETE CASCADE,
  notification_id  TEXT NOT NULL REFERENCES notification(id) ON DELETE CASCADE,
  read_at          TEXT NOT NULL,
  PRIMARY KEY (user_id, notification_id)
);

CREATE TABLE IF NOT EXISTS audit (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  at       TEXT NOT NULL,
  user_id  TEXT,
  email    TEXT,
  action   TEXT NOT NULL,
  target   TEXT,
  ip       TEXT,
  detail   TEXT
);
