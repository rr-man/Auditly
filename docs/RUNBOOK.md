# RUNBOOK — Auditly

## §1 Configuration

```bash
cd /path/to/Auditly
cp .env.example .env && chmod 600 .env
python3 -c "import secrets;print(secrets.token_urlsafe(48))"   # → SESSION_SECRET
```

Set at least one transcription key (`DEEPGRAM_API_KEY` or `OPENAI_API_KEY`),
`OPENAI_API_KEY` for scoring (or `ANTHROPIC_API_KEY` + `SCORING_PROVIDER=anthropic`),
and `AUDITLY_ALLOW_SPEND=1`. Every variable is documented in `.env.example`.
`.env` is re-read on every request — no restart after editing it.

## §2 Install as a service (production)

```bash
sudo deploy/install.sh --cert /etc/ssl/certs/snetcom-wildcard.pem --key /etc/ssl/private/snetcom-wildcard.key
```

Installs: `/etc/nginx/sites-available/auditly` (+ enabled symlink), `/etc/nginx/auditly-common.conf`,
`/etc/nginx/conf.d/auditly-limits.conf`, `/etc/systemd/system/auditly.service`; opens 8444/tcp if ufw is active.
Refuses without TLS, refuses if `AUDITLY_INSECURE_COOKIES=1` or `AUDITLY_OPEN_ACCESS=1`, refuses if 8084/8444 are taken by
something else, verifies the neighbouring nginx sites are byte-identical before and after,
and rolls back if `nginx -t` fails. Re-run to redeploy. `sudo deploy/uninstall.sh` reverses it.

URLs: `https://qa-server.example.com:8444/` and `https://auditly.example.com/` (once DNS exists).

Voice input to Ask Auditly (the microphone button, 0.48.0) works only on an HTTPS link or on
`http://127.0.0.1:8084`: browsers open a microphone only in a secure context. Until this install is
done, §2a below gives the LAN link an https:// twin from the app itself.

**Handover.** The app's built-in TLS listener (§2a) and this nginx install both want 8444. Before
running `install.sh`, stop the built-in one — `./restart.sh --no-tls`, or `AUDITLY_TLS_PORT=0` — so the
port is free; the address the team uses does not change.

## 2a. Self-signed TLS from the app (no root, no nginx) — 0.49.0

`./restart.sh` generates `tls/auditly.crt` and `tls/auditly.key` (EC P-256, 825 days, SANs for
the host's name, its short name, `localhost`, its LAN IP, `127.0.0.1`) the first time it runs,
then starts the app on both `http://…:8084/` and `https://…:8444/`. The pair is 0600 inside a 0700
folder and git-ignored.

Because the certificate is self-signed, **each browser shows "Your connection is not private" on its
first visit**: Advanced → Proceed to qa-server.example.com, once. After that the page is a secure
context and the microphone works. To stop the warning, obtain a certificate for the host (or
`*.example.com`) from IT and either overwrite the two files in `tls/` with the fullchain and key, or set
`AUDITLY_TLS_CERT` / `AUDITLY_TLS_KEY` in `.env` to their paths; then `./restart.sh`. No code changes.

The app never sends `Strict-Transport-Security`: HSTS is per host and ignores the port, so one such
header from :8444 would make browsers refuse `http://…:8084` for a year. nginx (§2) does send it,
correctly, because under nginx there is no plain link left.

## §3 Users

```bash
python3 auditly_host.py --add-user someone@example.com --role admin      # or reviewer
```

Admins can also add users under Settings. Passwords: 12+ characters. Disabling a
user ends their sessions.

## §4 Day to day

| Task | How |
|---|---|
| Status | `systemctl status auditly`; `curl -s http://127.0.0.1:8084/health` |
| Logs | `journalctl -u auditly -f` |
| Restart | `sudo systemctl restart auditly` — unfinished jobs re-queue automatically |
| Stop the dev server | `fuser -k 8084/tcp` — **never** `pkill -f` a pattern that other tools share |
| Rotate a key | edit `.env`; takes effect on the next request |
| Sign everyone out | rotate `SESSION_SECRET` (or delete rows from `session`) |
| Free disk | lower `AUDITLY_RETENTION_DAYS`; the hourly sweep deletes old audio, keeps transcripts and scorecards |
| Per-line sentiment wanted | set `AUDITLY_STT_SENTIMENT=1` (Deepgram only, English, billed per token on top of the minutes). It is text-derived, not tone of voice; the Tone & delivery card's timing metrics need nothing |
| Scorer should know the desk's procedures | Settings › Knowledge: add .txt/.md/.docx (or paste). Retrieval is keyword-based and free; each call adds at most ~1.3k tokens of excerpts to the scoring prompt |
| Different QA target | set `AUDITLY_TARGET_PCT` (1–100, default 90); the Dashboard's pass rate and target line follow at the next load |
| White label / other logo | Settings › Server › Branding (admin): hide, switch to COEO, or upload logo + tab icon (light/dark). Stored in the DB. Or drop `auditly_*.png` artwork into `static/` |
| Coaching invites | No settings needed: **Email invite › Open in my email app** downloads an `.eml` draft Outlook opens as a new message from the QA's account with the calendar attached; the QA presses Send and then **Mark invite as sent**. Optional server sending: set the `SMTP_*` keys (see `.env.example`; Microsoft 365 needs SMTP AUTH on the mailbox), then Settings › Server › Mail › **Send test**. QA emails go under Settings › Names so replies reach the QA. Failures are shown in the app and logged as `coaching.invite.failed` |
| Agents' dispute links | Sent by the same relay when an audit is submitted, to the agent's address under Settings › Names; the submit message says if it did not go out and why. **Dispute link** on the scorecard emails, copies or opens it in your mail app. Failures are logged as `dispute.link.failed`. Switch off with `AUDITLY_DISPUTE_LINK_DAYS=0` |
| Agent link points at the wrong host or http | set `AUDITLY_PUBLIC_URL=https://qa.company.com` in `.env` (no trailing slash) and restart; without it the link takes the scheme and host of the QA's own request (nginx passes `X-Forwarded-Proto`) |

## §5 When a job fails

The Calls tab shows the error. Common ones:

- `AUDITLY_ALLOW_SPEND is not 1` — set it in `.env`.
- `DEEPGRAM_API_KEY is not set` / `OPENAI_API_KEY is not set`.
- `… does not look like a valid key` — the paste lost part of the key. OpenAI keys start with `sk-`
  (usually `sk-proj-`, about 160 characters); Deepgram keys are 40 hex characters. Re-paste, then Retry —
  the transcript is reused, so only scoring runs again.
- `OpenAI accepts at most 25 MB` — retry the job with Deepgram (Retry button, or re-upload).
- `model reply was not JSON` — the scorer retried once; retry the job. If it persists, lower `SCORING_MAX_TOKENS`
  is *not* the fix — check the model id in `.env`.
- `The audio file is no longer available` — retention deleted it; rescore is still possible from the stored transcript.
- Garbled words in the transcript — on the scorecard use **Transcribe again with…** (another engine) and compare; a doubtful
  score — **Audit again with…** (another LLM). Each adds a new run; nothing is overwritten. If the identical run already
  exists the box offers **Open existing** first; identical audio uploaded again reuses its transcript free.
- Spend — Settings › Server › Spend shows audio minutes, tokens and a list-price estimate from recorded usage.

Retry re-uses an existing transcript, so a scoring failure never bills transcription twice.

## §6 Backups

Back up `auditly.db` (WAL mode: copy `auditly.db`, `-wal` and `-shm` together, or use `sqlite3 auditly.db ".backup out.db"`)
and, if audio must be kept, `uploads/`. Both hold customer content — keep the backup owner-only.

## §7 Demo mode

```bash
python3 auditly_host.py --demo
```

Uses `./auditly-demo.db`, fake providers, and seeds `demo@example.com / demo-demo-demo` with one scored call
plus ~44 past calls (agents Jordan, Priya, Marcus; QA names Demo QA and Alex Reyes) with reviews on most, so
the Dashboard and the Audit queue are populated. Those past calls carry no audio. `AUDITLY_DEMO_HISTORY=0`
seeds only the one call. Nothing leaves the machine — and nothing is transcribed: every upload gets the
canned sample call.

The same history can be loaded into the **real** database to preview the Dashboard before real calls are
audited: Settings › Names › Demo data › Load demo data (admin), or

```bash
python3 auditly_host.py --load-demo-data      # ~44 calls tagged DEMO, no audio, reviews included
python3 auditly_host.py --remove-demo-data    # deletes exactly those again; real calls untouched
```

They are excluded from the Dashboard unless its Calls switch is set to Demo or All, and from the Calls and
Audit lists unless the kind filter includes demo runs. After upgrading, restart the LAN link (`./restart.sh`,
§8) so it picks up the new code; the database migrates itself on start. The page and the version chip are
read from disk on every request, so they update without a restart — the Python does not. A server started
before an upgrade therefore shows the new page but answers its newer actions with **Not found.**;
`/api/health` reports `started_at` so a stale process can be spotted.

## §8 Plain-HTTP LAN link (dev)

```bash
./restart.sh                 # -> http://qa-server.example.com:8084/auditly/
```

The same pattern as the sibling tools on this box (`~/cfbuilder-serve/restart.sh`): the
Python host binds `0.0.0.0:8084` directly, no nginx, started with `setsid nohup`, restarted
at boot by one `@reboot` line in rmangune's crontab.

**Real mode (current).** `MODE=""` in `restart.sh`. Each upload is transcribed by Deepgram
and scored by OpenAI from its own audio. Needs in `.env`: `DEEPGRAM_API_KEY`,
`OPENAI_API_KEY`, `AUDITLY_ALLOW_SPEND=1` (`.env` is re-read per request — paste a key and
the next upload works). First time: `python3 auditly_host.py --seed-rubric` seeds the sample
QA type "L1 Support v1" into `auditly.db`; the team's agents and QA reviewers are added under
Settings › Names before the first upload. Until a key is set the
Upload tab shows a banner and uploads are refused with the key's name.

**Demo mode.** `MODE="--demo"` (and `--demo` on the crontab line): fake providers, the
canned sample call for *every* upload, `auditly-demo.db`, nothing leaves the machine.
Demo runs, Load demo data and the "Demo runs" filters exist only in this mode. In real mode
use **Run as: Test call** to score real audio without it counting as a real audit (tagged
TEST). Calls shows real calls only unless the filter is changed.

**Nothing demo in production.** Since 0.13.0 `--seed-rubric` seeds no names; before that it
added Jordan, Priya, Marcus and Demo QA as placeholders. A database seeded earlier: Settings ›
Names › **Remove demo names** deletes them (archives any already used by a call), and
**rename** on an archived placeholder rewrites the calls credited to it to the real person.

Requires in `.env`: `AUDITLY_BIND=0.0.0.0` and `AUDITLY_INSECURE_COOKIES=1`. Browsers drop a
`Secure` cookie over `http://`, so without the second setting login silently fails.
Passwords and session cookies therefore cross the LAN unencrypted.

**No sign-in:** `AUDITLY_OPEN_ACCESS=1` (set on this host) makes every visitor act as the
built-in `open-access@local` admin — anyone who can reach port 8084 can upload,
override, delete and export. The Sign out, Users and password cards are hidden; audit
rows read `open-access@local`. A real login still works and takes precedence. Set it
back to `0` (takes effect on the next request, no restart) to require sign-in again.

Acceptable for demo data; for real recordings use §2 (TLS), which refuses to install
while `AUDITLY_INSECURE_COOKIES=1` or `AUDITLY_OPEN_ACCESS=1`, and while something other
than `auditly.service` holds 8084 — so stop this link first: `fuser -k 8084/tcp`.

Logs: `server.log` in the project folder. If the link is unreachable from another machine
but `curl http://127.0.0.1:8084/health` works, the firewall is blocking 8084
(`sudo ufw allow 8084/tcp`, a human decision).

The `@reboot` crontab line (present on this host since 2026-09-10). Check with
`crontab -l | grep auditly_host`; if nothing prints, add the line below with `crontab -e`,
otherwise a reboot leaves the link down until someone runs `./restart.sh`:

    @reboot cd /path/to/Auditly && setsid nohup python3 "/path/to/Auditly/auditly_host.py" >> server.log 2>&1 < /dev/null &

(add `--demo` after `auditly_host.py"` for demo mode; it must match `MODE` in `restart.sh`.)

## Publishing or forking the code

The repository carries placeholders, never this site's names: `qa-server.example.com` / `auditly.example.com`
in the nginx site and the docs, `__APPDIR__` / `__USER__` in `deploy/auditly.service` (filled in by
`deploy/install.sh` from the checkout's own path and owner), and `restart.sh` names the host from
`hostname -f` unless `AUDITLY_HOST` is set. `.gitignore` keeps `.env`, every `*.db*`, `uploads*/`, `tls/`
and `*.log` out; the test suite's *repository hygiene* checks refuse a key-shaped string, an internal
hostname, a LAN address or a home path in any committable file. Before a push: `python3 tests/test_auditly.py`
and `git ls-files | grep -E '\.env|\.db|uploads|tls/|\.log'` (only `.env.example` may appear).
