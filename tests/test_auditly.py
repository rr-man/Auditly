#!/usr/bin/env python3
"""End-to-end tests. No framework, no dependencies, no API keys:

    python3 tests/test_auditly.py

Boots the real server in --demo mode on a loopback port with a temporary
database and upload directory, then exercises routing, cookies, uploads, the
job pipeline, scoring maths, overrides, exports and serialization. Provider
calls are the Demo fakes, so nothing leaves the machine and nothing is billed.
"""
import http.client
import http.cookiejar
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

PORT = int(os.environ.get("AUDITLY_TEST_PORT", "8395"))
BASE = "http://127.0.0.1:%d" % PORT
PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + str(detail)) if detail and not cond else ""))


class Client:
    def __init__(self, base=BASE):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def raw(self, path, method="GET", body=None, headers=None, data=None, csrf=True):
        h = {"X-Auditly-CSRF": "1"} if csrf else {}
        h.update(headers or {})
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with self.op.open(req, timeout=30) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def json(self, path, method="GET", body=None, **kw):
        s, b, _ = self.raw(path, method, body, **kw)
        try:
            return s, json.loads(b or b"{}")
        except ValueError:
            return s, {}

    def login(self, email, pw):
        return self.json("/auth/login", "POST", {"email": email, "password": pw})


def wait_up(timeout=25):
    for _ in range(int(timeout * 10)):
        try:
            with urllib.request.urlopen(BASE + "/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.1)
    return False


def wait_job(c, job_id, timeout=15):
    for _ in range(int(timeout * 5)):
        s, j = c.json("/api/jobs/" + job_id)
        if j.get("status") in ("done", "failed"):
            return j
        time.sleep(0.2)
    return j


class FakeSMTP(threading.Thread):
    """A minimal SMTP relay for the tests: EHLO, MAIL, RCPT, DATA, QUIT; no TLS, no auth. Captures every message."""
    daemon = True

    def __init__(self):
        super().__init__()
        self.sock = socket.socket(); self.sock.bind(("127.0.0.1", 0)); self.sock.listen(5)
        self.port = self.sock.getsockname()[1]; self.messages = []

    def run(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.serve, args=(conn,), daemon=True).start()

    def serve(self, conn):
        f = conn.makefile("rb"); w = conn.makefile("wb")
        def say(t): w.write((t + "\r\n").encode()); w.flush()
        say("220 fake.test ESMTP")
        env = {"from": None, "to": []}
        while True:
            line = f.readline()
            if not line:
                break
            l = line.decode("utf-8", "replace").rstrip("\r\n")
            up = l.upper()
            if up.startswith("EHLO") or up.startswith("HELO"):
                say("250-fake.test"); say("250 SIZE 10485760")
            elif up.startswith("MAIL FROM:"):
                env["from"] = l.split(":", 1)[1].strip(); say("250 OK")
            elif up.startswith("RCPT TO:"):
                env["to"].append(l.split(":", 1)[1].strip()); say("250 OK")
            elif up == "DATA":
                say("354 go ahead"); buf = []
                while True:
                    ln = f.readline()
                    if not ln or ln.rstrip(b"\r\n") == b".":
                        break
                    buf.append(ln)
                self.messages.append({"from": env["from"], "to": list(env["to"]), "data": b"".join(buf).decode("utf-8", "replace")})
                env = {"from": None, "to": []}; say("250 queued")
            elif up == "QUIT":
                say("221 bye"); break
            elif up == "RSET":
                env = {"from": None, "to": []}; say("250 OK")
            else:
                say("250 OK")
        conn.close()


SMTP = None


def main():
    tmp = tempfile.mkdtemp(prefix="auditly-test-")
    env = dict(os.environ, AUDITLY_DB=os.path.join(tmp, "t.db"), AUDITLY_UPLOAD_DIR=os.path.join(tmp, "up"),
               AUDITLY_PORT=str(PORT), AUDITLY_BIND="127.0.0.1", AUDITLY_INSECURE_COOKIES="1", AUDITLY_DEMO="1",
               AUDITLY_MAX_UPLOAD_MB="30", AUDITLY_OPEN_ACCESS="0",   # pinned: the host's .env may set it
               AUDITLY_DEMO_NOTE="sandbox note for the banner",
               AUDITLY_DEMO_HISTORY="0",                           # counts below assume the one seeded call
               ASK_ENABLED="1")                                    # the assistant answers here; the real-mode server leaves it off
    for k in ("DEEPGRAM_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        env[k] = "test-secret-%s-should-never-appear" % k.lower()
    log = open(os.path.join(tmp, "server.log"), "w")
    global SMTP
    SMTP = FakeSMTP(); SMTP.start()
    env.update({"SMTP_HOST": "127.0.0.1", "SMTP_PORT": str(SMTP.port), "SMTP_STARTTLS": "0", "SMTP_FROM": "auditly@test.local", "SMTP_USER": "", "SMTP_PASSWORD": "",
                "AUDITLY_PUBLIC_URL": "https://qa.test.local", "AUDITLY_DISPUTE_LINK_DAYS": "14"})
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "auditly_host.py"), "--demo"],
                            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        check("server boots in demo mode", wait_up())
        run(tmp, env)
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        if FAILS:
            print("\n--- server log ---")
            print(open(os.path.join(tmp, "server.log")).read()[-4000:])
        shutil.rmtree(tmp, ignore_errors=True)
    open_access_suite()
    local_access_suite()
    seed_rubric_suite()
    print("\n%d passed, %d failed" % (len(PASSES), len(FAILS)))
    sys.exit(1 if FAILS else 0)


def seed_rubric_suite():
    """`--seed-rubric` puts the sample rubric and the name lists into a REAL (non-demo) database."""
    import sqlite3
    tmp = tempfile.mkdtemp(prefix="auditly-seed-")
    dbp = os.path.join(tmp, "real.db")
    env = dict(os.environ, AUDITLY_DB=dbp, AUDITLY_UPLOAD_DIR=os.path.join(tmp, "up"), AUDITLY_DEMO="0")
    try:
        r1 = subprocess.run([sys.executable, os.path.join(ROOT, "auditly_host.py"), "--seed-rubric"], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=60)
        r2 = subprocess.run([sys.executable, os.path.join(ROOT, "auditly_host.py"), "--seed-rubric"], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=60)
        db = sqlite3.connect(dbp)
        n_rub = db.execute("SELECT COUNT(*) FROM rubric").fetchone()[0]
        n_crit = db.execute("SELECT COUNT(*) FROM criterion").fetchone()[0]
        n_people = db.execute("SELECT COUNT(*) FROM person").fetchone()[0]
        n_rec = db.execute("SELECT COUNT(*) FROM recording").fetchone()[0]
        n_user = db.execute("SELECT COUNT(*) FROM user").fetchone()[0]
        seed_sync = db.execute("SELECT sync_status FROM rubric_version").fetchone()[0]
        db.close()
        check("--seed-rubric: one rubric with criteria, no names, no call, no demo user, idempotent, not optimised",
              r1.returncode == 0 and r2.returncode == 0 and n_rub == 1 and n_crit >= 5 and n_people == 0 and n_rec == 0 and n_user == 0
              and "L1 Support v1" in r1.stdout and seed_sync == "none", (r1.stdout, r1.stderr[-300:], n_rub, n_crit, n_people, n_rec, n_user, seed_sync))
        # real mode with a malformed OpenAI key: refused before anything is sent or billed
        port = PORT + 2
        base = "http://127.0.0.1:%d" % port
        env2 = dict(env, AUDITLY_PORT=str(port), AUDITLY_BIND="127.0.0.1", AUDITLY_INSECURE_COOKIES="1", AUDITLY_OPEN_ACCESS="1",
                    AUDITLY_ALLOW_SPEND="1", DEEPGRAM_API_KEY="3ee5a9" + "0" * 34, OPENAI_API_KEY="JeIQM_mp" + "x" * 148)
        # 0.49.0: the app's own HTTPS listener, with a throwaway self-signed pair (skipped if openssl is absent)
        tls_port, crt, key = port + 1, os.path.join(tmp, "t.crt"), os.path.join(tmp, "t.key")
        tls_ok = shutil.which("openssl") is not None and subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-days", "2",
             "-keyout", key, "-out", crt, "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
            capture_output=True, timeout=60).returncode == 0
        if tls_ok:
            env2.update(AUDITLY_TLS_PORT=str(tls_port), AUDITLY_TLS_CERT=crt, AUDITLY_TLS_KEY=key)
        log = open(os.path.join(tmp, "server.log"), "w")
        proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "auditly_host.py")], cwd=ROOT, env=env2,
                                stdout=log, stderr=subprocess.STDOUT)
        try:
            up = False
            for _ in range(250):
                try:
                    with urllib.request.urlopen(base + "/health", timeout=2) as r:
                        up = r.status == 200
                        break
                except Exception:
                    time.sleep(0.1)
            a = Client(base)
            s, rubs = a.json("/api/rubrics")
            rvid = rubs["rubrics"][0]["current_version_id"] if s == 200 and rubs.get("rubrics") else ""
            wav = open(os.path.join(ROOT, "fixtures", "tone.wav"), "rb").read()
            s, e = a.json("/api/recordings?filename=x.wav&provider=deepgram&rubric_version_id=%s" % rvid, "POST", data=wav, headers={"Content-Type": "audio/wav"})
            check("real mode: malformed OPENAI_API_KEY refused at upload, naming sk-", up and s == 400 and "OPENAI_API_KEY" in e.get("error", "") and "sk-" in e.get("error", ""), (up, s, e))
            s, e = a.json("/api/ask", "POST", {"scorecard_id": "00000000-0000-4000-8000-000000000000", "question": "hello"})
            check("assistant off by default: a question is refused with the reason, naming ASK_ENABLED, before any lookup", s == 409 and "ASK_ENABLED" in e.get("error", ""), (s, e))
            s, hh = a.json("/api/health")
            check("health says the assistant is off and why", s == 200 and (hh.get("ask") or {}).get("enabled") is False and "ASK_ENABLED" in (hh["ask"].get("problem") or ""), hh.get("ask"))
            s, e, _ = a.raw("/api/ask/voice?ext=webm", "POST", data=b"\x1aE\xdf\xa3" + b"\x00" * 100, headers={"Content-Type": "audio/webm"})
            check("real mode, assistant off: a spoken question is refused before the body is read, nothing transcribed (409)", s == 409 and b"switched off" in e, (s, e[:80]))
            # ── the HTTPS twin (0.49.0) ──
            if tls_ok:
                import ssl
                ctx = ssl._create_unverified_context()
                hv, hdrs = None, {}
                for _ in range(40):
                    try:
                        with urllib.request.urlopen("https://127.0.0.1:%d/health" % tls_port, timeout=2, context=ctx) as r:
                            hv, hdrs = json.loads(r.read().decode()).get("version"), dict(r.headers)
                            break
                    except Exception:
                        time.sleep(0.1)
                check("the same app answers over HTTPS on AUDITLY_TLS_PORT with the same version", hv == json.loads(a.raw("/health")[1].decode())["version"], hv)
                check("neither listener sends Strict-Transport-Security (HSTS is per host and would break the plain link for a year)",
                      not any(k.lower() == "strict-transport-security" for k in hdrs) and not any(k.lower() == "strict-transport-security" for k in a.raw("/health")[2]), list(hdrs))
                plain_fail = False
                try:
                    urllib.request.urlopen("http://127.0.0.1:%d/health" % tls_port, timeout=3)
                except Exception:
                    plain_fail = True
                check("plain HTTP against the TLS port is refused, not served", plain_fail)
                s, hh2 = a.json("/api/health")
                check("health tells the page where its secure twin is", s == 200 and hh2.get("tls_url") == "https://127.0.0.1:%d/" % tls_port, hh2.get("tls_url"))
            else:
                print("       (openssl not found; the HTTPS listener was not exercised)")
            s, pp = a.json("/api/people")
            lists = [v for v in pp.values() if isinstance(v, list)] if isinstance(pp, dict) else []
            check("real mode starts with no agent or QA names (nothing demo in production)", s == 200 and lists and all(len(v) == 0 for v in lists), pp)
            s, cr = a.json("/api/people", "POST", {"kind": "qa", "name": "Live QA"})
            s2, pp2 = a.json("/api/people")
            check("production: a QA reviewer added under Settings › Names is the live dropdown's source",
                  s == 201 and s2 == 200 and [p["name"] for p in pp2.get("people", [])] == ["Live QA"] and pp2["people"][0]["active"] is True, (s, cr, pp2))
            s, h = a.json("/api/health")
            check("health flags the bad key shape without revealing it", s == 200 and h["openai"]["shape_ok"] is False and h["deepgram"]["shape_ok"] is True
                  and "JeIQM" not in json.dumps(h))
            check("menus: 3 audit choices without an Anthropic key; OpenAI ones unavailable while its key is bad",
                  len(h.get("scoring_choices", [])) == 3 and all(x["available"] is False for x in h["scoring_choices"])
                  and [x["available"] for x in h.get("stt_choices", [])] == [True, False, False], h.get("stt_choices"))
            s, e = a.json("/api/recordings?filename=x.wav&provider=deepgram&rubric_version_id=%s&run=demo" % rvid, "POST", data=wav, headers={"Content-Type": "audio/wav"})
            check("real mode: demo run still works with a bad key", s == 201, e)
            # "Optimise for the scorer" on a server that cannot pay: refused up front, nothing queued, no key value shown
            rid0 = rubs["rubrics"][0]["id"]
            s, sy = a.json("/api/rubrics/%s/versions/1/sync" % rid0)
            check("real mode, bad key: sync precheck says it cannot run, names the key variable, never the key",
                  s == 200 and sy.get("sync_status") == "none" and sy.get("can_run") is False and "OPENAI_API_KEY" in (sy.get("reason") or "")
                  and "JeIQM" not in json.dumps(sy) and sy.get("model") == "gpt-4o-mini", sy)
            s, e2 = a.json("/api/rubrics/%s/versions/1/sync" % rid0, "POST", {})
            s2, sy2 = a.json("/api/rubrics/%s/versions/1/sync" % rid0)
            check("real mode, bad key: POST sync is 400 with the reason and the version stays un-optimised (nothing queued, nothing billed)",
                  s == 400 and "OPENAI_API_KEY" in e2.get("error", "") and "JeIQM" not in json.dumps(e2) and sy2.get("sync_status") == "none" and sy2.get("runs_used") == 0, (s, e2, sy2))
            jd = wait_job(a, e.get("job_id", ""), 20)
            s, scd = a.json("/api/scorecards/%s" % jd.get("scorecard_id", ""))
            check("a demo run against an un-optimised version scores with the guidance as written", jd.get("status") == "done" and scd.get("guidance_mode") == "authored", (jd.get("status"), scd.get("guidance_mode")))
            r_ev = subprocess.run([sys.executable, os.path.join(ROOT, "auditly_host.py"), "--eval-determinism", "latest"], cwd=ROOT, env=env2, capture_output=True, text=True, timeout=60)
            check("--eval-determinism refuses in real mode with a bad key, before any call", r_ev.returncode == 2 and "OPENAI_API_KEY" in r_ev.stdout and "JeIQM" not in r_ev.stdout, (r_ev.returncode, r_ev.stdout))
            r_ov = subprocess.run([sys.executable, os.path.join(ROOT, "auditly_host.py"), "--optimise-version", rvid], cwd=ROOT, env=env2, capture_output=True, text=True, timeout=60)
            check("--optimise-version refuses in real mode with a bad key, before any call", r_ov.returncode != 0 and "OPENAI_API_KEY" in (r_ov.stderr + r_ov.stdout) and "JeIQM" not in (r_ov.stderr + r_ov.stdout), (r_ov.returncode, r_ov.stderr[-300:]))
        finally:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
            log.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def local_access_suite():
    """AUDITLY_OPEN_ACCESS unset (0.64.0): a loopback bind signs its own machine in without a password, a proxied
    request or a network bind does not."""
    def boot(port, bind, tag):
        base = "http://127.0.0.1:%d" % port
        tmp = tempfile.mkdtemp(prefix="auditly-%s-" % tag)
        env = dict(os.environ)
        env.update(AUDITLY_OPEN_ACCESS="",              # an empty variable overrides whatever the host's .env says: "unset" for the server
                   AUDITLY_DB=os.path.join(tmp, "t.db"), AUDITLY_UPLOAD_DIR=os.path.join(tmp, "up"), AUDITLY_PORT=str(port),
                   AUDITLY_BIND=bind, AUDITLY_INSECURE_COOKIES="1", AUDITLY_DEMO="1", AUDITLY_DEMO_HISTORY="0", AUDITLY_TLS_PORT="0")
        log = open(os.path.join(tmp, "server.log"), "w")
        proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "auditly_host.py"), "--demo"], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        up = False
        for _ in range(250):
            try:
                with urllib.request.urlopen(base + "/health", timeout=2) as r:
                    up = r.status == 200
                    break
            except Exception:
                time.sleep(0.1)
        return proc, tmp, base, up
    proc, tmp, base, up = boot(PORT + 5, "127.0.0.1", "local")
    try:
        check("local-access server boots (open access unset, bound to 127.0.0.1)", up)
        a = Client(base)
        s, me = a.json("/api/me")
        check("unset + loopback: this machine's browser is the built-in admin without a password", s == 200 and me.get("email") == "open-access@local" and me.get("open_access") is True, me)
        s, h = a.json("/api/health")
        check("health reports the mode as local", s == 200 and h.get("open_access_mode") == "local" and h.get("open_access") is True, h.get("open_access_mode"))
        req = urllib.request.Request(base + "/api/me", headers={"X-Forwarded-For": "203.0.113.9"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        check("unset + loopback: a request that came through a proxy must sign in", code == 401, code)
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except Exception:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)
    proc, tmp, base, up = boot(PORT + 6, "0.0.0.0", "net")
    try:
        check("network-bound server boots (open access unset, 0.0.0.0)", up)
        a = Client(base)
        s, me = a.json("/api/me")
        s2, h = a.json("/health")                      # the public probe; /api/health itself needs a signed-in user here
        check("unset + network bind: sign-in required (the only anonymous answer is 401)", s == 401 and s2 == 200 and "email" not in me, (s, s2))
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except Exception:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


def open_access_suite():
    """AUDITLY_OPEN_ACCESS=1 on a second server: no sign-in, every visitor is open-access@local."""
    port = PORT + 1
    base = "http://127.0.0.1:%d" % port
    tmp = tempfile.mkdtemp(prefix="auditly-open-")
    env = dict(os.environ, AUDITLY_DB=os.path.join(tmp, "t.db"), AUDITLY_UPLOAD_DIR=os.path.join(tmp, "up"),
               AUDITLY_PORT=str(port), AUDITLY_BIND="127.0.0.1", AUDITLY_INSECURE_COOKIES="1", AUDITLY_DEMO="1",
               AUDITLY_OPEN_ACCESS="1", AUDITLY_DEMO_HISTORY="0")
    log = open(os.path.join(tmp, "server.log"), "w")
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "auditly_host.py"), "--demo"],
                            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        up = False
        for _ in range(250):
            try:
                with urllib.request.urlopen(base + "/health", timeout=2) as r:
                    up = r.status == 200
                    break
            except Exception:
                time.sleep(0.1)
        check("open-access server boots", up)
        a = Client(base)
        s, me = a.json("/api/me")
        check("open access: /api/me is 200 anonymous", s == 200 and me.get("email") == "open-access@local", me)
        check("open access: visitor is admin and flagged", me.get("role") == "admin" and me.get("open_access") is True)
        s, jobs = a.json("/api/jobs")
        check("open access: /api/jobs is 200 anonymous", s == 200 and len(jobs.get("jobs", [])) >= 1)
        s, h = a.json("/api/health")
        check("open access: health reports open_access", s == 200 and h.get("open_access") is True)
        s, _ = a.json("/auth/logout", "POST", {})
        s2, me2 = a.json("/api/me")
        check("open access: logout is harmless", s == 200 and s2 == 200 and me2.get("email") == "open-access@local")
        s, br = a.json("/api/branding")
        check("open access: branding is readable", s == 200 and br.get("logo_mode") == "auditly")
        s, b, _ = a.raw("/api/admin/users")
        check("open access: no hash or salt in any body", s == 200 and b"pw_hash" not in b and b"pw_salt" not in b)
        s, _ = a.json("/auth/login", "POST", {"email": "open-access@local", "password": "x" * 20})
        check("open access: built-in user cannot be signed in to", s == 401)
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        if FAILS:
            print("\n--- open-access server log ---")
            print(open(os.path.join(tmp, "server.log")).read()[-3000:])
        shutil.rmtree(tmp, ignore_errors=True)


def run(tmp, env):
    import core
    import score as score_mod
    import dashboard
    import rubric as rubric_mod
    anon = Client()

    # ── public surface ────────────────────────────────────────────────
    s, h = anon.json("/health")
    check("health is public", s == 200 and h.get("ok") and h.get("demo") is True)
    s, cl = anon.json("/api/changelog")
    check("changelog is public", s == 200 and cl.get("entries"))
    readme = open(os.path.join(ROOT, "README.md")).read().splitlines()[2]
    check("changelog version matches README line 3", cl.get("version") in readme, readme)
    s, plan = anon.json("/api/plan")
    check("plan is public and non-empty", s == 200 and len(plan.get("markdown", "")) > 500)
    s, b, hd = anon.raw("/")
    html = b.decode()
    check("app served with CSP nonce", s == 200 and "nonce-" in hd.get("Content-Security-Policy", ""))
    check("version substituted into chip", ("v" + cl["version"]) in html and "__APP_VERSION__" not in html)
    check("product is Auditly: title, header, guide heading, no old name left in the page",
          "<title>Auditly</title>" in html and '<h1 id="appName" class="hidden">Auditly</h1>' in html and "Six steps from setting up a QA type" in html
          and html.count("L1 Support QA") == 1 and "renamed Auditly in 0.30.0" in html)   # the one mention is About's history line
    hc0 = http.client.HTTPConnection("127.0.0.1", PORT); hc0.request("GET", "/l1qa/"); r_old = hc0.getresponse(); loc_old = r_old.getheader("Location"); r_old.read(); hc0.close()
    s_new, b_new, _ = anon.raw("/auditly/")
    s_h, b_h, _ = anon.raw("/health")
    check("old /l1qa/ link redirects (301) to /auditly/, which serves the page; /health names the service auditly",
          r_old.status == 301 and loc_old == "/auditly/" and s_new == 200 and b"<title>Auditly</title>" in b_new
          and s_h == 200 and json.loads(b_h.decode()).get("service") == "auditly", (r_old.status, loc_old, s_new, s_h))
    check("no placeholder left in HTML", "__CSP_NONCE__" not in html)
    check("no external asset in HTML", not re.search(r'src="https?://', html) and not re.search(r'<link[^>]+href="https?://', html))
    check("Guidelines lives under Settings (inner tab), not the top nav", 'data-tab="guidelines"' not in html and 'data-set="guidelines"' in html)
    check("criteria table cannot overflow its grid column; loading animation honours reduced motion",
          ".split>*{min-width:0}" in html and "@keyframes spin" in html and "prefers-reduced-motion" in html and "S.upT" in html and 'class="steps"' in html)
    check("job view and calls list poll on separate timers", "S.pollT" not in html and "S.listT" in html and "S.jobT" in html and "visibilitychange" in html)
    check("Optimise for the scorer: own timer, panel, pills, shimmer honours reduced motion, Save no longer navigates away, notices",
          "S.syncT" in html and "function stopSyncPoll" in html and "function pollSync" in html and "function openSyncPanel" in html
          and "function startSync" in html and "e.status === 409" in html and "stopUploadPoll(); stopSyncPoll();" in html
          and all(x in html for x in (".pill.sync-none", ".pill.sync-running", ".pill.sync-done", ".pill.sync-failed"))
          and "@keyframes shimmer" in html and ".shimmer{animation:none}" in html
          and 'id="syncGo"' in html and 'id="syncBack"' in html and 'id="rubricSyncBanner"' in html and 'id="upRubricNote"' in html
          and '$("edBack").click()' not in html and 'textContent = "Saving…"' in html and "function confirmRun" in html
          and 'guidance_mode === "optimised"' in html and "Retry optimisation" in html
          and "openSyncPanel(E.rubric ? E.rubric.id : d.id, d.version_no, name, { prompt: true })" in html
          and "Not now" in html and "k.prompted" in html and "cannot be optimised right now" in html and "startSync(k, $(\"syncGo\"), true)" in html)
    check("provider is chosen per file in the uploads list, not in the form", 'id="upProvider"' not in html and 'id="startAll"' in html and "data-prov=" in html)
    check("scorecard offers rerun menus and the upload form an Audit with choice", "Audit again with" in html and "Transcribe again with" in html and 'id="upScorer"' in html)
    check("auto-fail is explained on the scorecard and in the override dialog", "<b>Auto-fail:</b>" in html and "This criterion is critical:" in html)
    check("executive dashboard: view switch, KPI strip, criterion bars, leaders and the PDF link; the detail view reads the configured target",
          'id="dbView"' in html and "function renderExec" in html and "function pctBars" in html and 'id="dbLeaders"' in html and 'id="dbPdf"' in html
          and "auditly-dbview" in html and "target: tgt" in html and "target: 90" not in html)
    check("trend line is a line, not a filled area (path.ln outranks the series fill)", ".chart .ln,.chart path.ln{fill:none" in html)
    for nm in ("auditly_light_still.svg", "auditly_dark_still.svg"):
        s_a, b_a, h_a = anon.raw("/static/" + nm)
        if not (s_a == 200 and b'd="M251.0 438.5' in b_a and b"@keyframes" not in b_a and b"animation" not in b_a):
            check("motionless twin served: " + nm, False, (s_a, b"@keyframes" in b_a))
            break
    else:
        check("a motionless twin of each wordmark is served for reduced motion", True)
    check("the page picks the motionless twin itself (the media query never reaches an SVG in an <img>)",
          'noMotion()) url = url.replace(".svg", "_still.svg")' in html)
    check("Ask Auditly: a launcher in the lower right, hidden until sign-in, with a panel that says the assistant is not on yet",
          'id="askBtn"' in html and 'id="askDock"' in html and 'function applyAsk' in html
          and ".askfab{position:fixed;right:18px;bottom:18px" in html
          and 'id="askForm"' in html and 'id="askQ"' in html and 'id="askLog"' in html and "function askRefresh" in html)
    ask_js = html.split("function applyAsk")[1][:1600]
    check("the launcher follows the theme and reduced motion, plays its stamp once per load, and goes when the logo is hidden",
          '"/static/auditly_mark_" + t + ".svg" + AV' in ask_js and 'url.replace(".svg", "_still.svg")' in ask_js
          and 'if (img.getAttribute("src") !== url) img.src = url;' in ask_js and 'b.logo_mode === "hidden"' in ask_js)
    brand_js = html.split('$("brandMode").onclick')[1].split('$("brandApply").onclick')[0]   # the pick handler alone
    check("Branding: picking a header logo is not a save — Apply posts it, Cancel drops the pick, the hint names the launcher (0.50.1)",
          'id="brandApply"' in html and 'id="brandCancel"' in html and 'id="brandPending"' in html
          and 'api("/api/admin/branding"' not in brand_js and "S.brandPick = btn.dataset.v" in brand_js
          and 'api("/api/admin/branding", { method: "POST", body: { logo_mode: S.brandPick } })' in html
          and "Hidden also takes the Ask Auditly button away." in html)
    set_tab_js = html.split("function setTab(")[1].split("function setSettingsTab")[0]
    check("notifications: the header alert button, its dock, the Settings pane and a poll that setTab leaves alone (0.51.0)",
          'id="notifBtn"' in html and 'id="notifDock"' in html and 'id="notifN"' in html and 'data-set="notifications"' in html
          and 'id="set-notifications"' in html and "function loadNotifs" in html and "function scheduleNotifPoll" in html
          and '"notifications"' in html.split("function setSettingsTab")[1][:600]
          and "stopNotifPoll" not in set_tab_js and "stopNotifPoll" in html.split('addEventListener("visibilitychange"')[1][:300]
          and ".chip.notif.unread{" in html and "Settings › Notifications chooses which" in html
          and 'api("/api/notifications/prefs"' in html.split('$("ntfSave").onclick')[1][:400])
    check("Focus mode (0.53.0/0.55.0): the header chip, the bar with its chips, Focus buttons on rows and the scorecard, remembered per browser",
          'id="focusBar"' in html and 'id="focusClear"' in html and 'id="focusBtn"' in html and 'id="focusChips"' in html and 'data-focus="' in html and 'data-focusrec="1"' in html
          and '"auditly-focus"' in html and all(f in html for f in ("function focusLoad", "function focusToggle", "function focusAdd", "function focusDrop", "function focusIds", "function focusOn", "function renderFocusBar", "function bindFocus", "function focusAgents"))
          and html.count("bindFocus(tb);") == 2 and '"&recording=" + (ids.length ? ids.join(",") : "none")' in html
          and "nothing in focus yet" in html and html.count("applyAsk(); renderNext(); renderFocusBar();") == 2
          and "focusAgents()" in html[html.index("function loadCoachAgents"):html.index("function loadCoachAgents") + 1500]
          and "focusAgents()" in html[html.index("function loadSessions"):html.index("function loadSessions") + 1200])
    check("the header logo plays the stamp once per load: src is assigned only when it changes",
          'if (url && img.getAttribute("src") !== url) img.src = url;' in html and "plays on every page load and refresh" in html
          and "border-radius:6px}" not in html.split(".bar .logo{")[1][:120])
    check("white label: favicon link, brand-aware logo, Branding card with the four slots and mode switch",
          '<link rel="icon" id="favIcon" href="/favicon.ico">' in html and "function applyLogo" in html and "function loadBrand" in html
          and 'id="brandCard"' in html and 'id="brandMode"' in html and 'id="brandSlots"' in html and 'id="appName"' in html and 'alt="COEO"' not in html)
    check("coaching calendar: month and week views, invite and delivered marks, weekly summary, Email invite dialog",
          'data-v="week"' in html and "function weekSummary" in html and "function inviteDialog" in html and 'id="csInvite"' in html and "awaiting invite" in html and "function evMark" in html)
    check("invite dialog: the QA's own mail app first (.eml draft + Mark invite as sent), server send optional, plain mailto as fallback; no bare mailto button in the header",
          "/export.eml?to=" in html and 'id="invDraft"' in html and 'id="invMark"' in html and "invite-sent" in html and 'id="invMailto"' in html and "Open in my email app" in html
          and "Email invite</a>" not in html)
    check("coaching: the QA picks calls with a scope switch, select all/none and a live tally; the session shows Totals",
          "function candidatePanel" in html and "function tallyOf" in html and 'Tally" title=' in html and ">Waiting for coaching</button>" in html
          and ">All scored</button>" in html and "<h2>Totals " in html and "Points lost per criterion" in html)
    check("knowledge base: Settings sub-tab, reference-material card on the scorecard, try-a-search box",
          'data-set="knowledge"' in html and ">Knowledge</button>" in html and "function kbCard" in html and 'id="kbHits"' in html and "never evidence" in html)
    check("tone & delivery card, sentiment strip and per-line dots are on the scorecard and explained as unscored",
          "Tone &amp; delivery" in html and "function sentimentStrip" in html and 'class="sd sent-' in html and "never part of the score" in html)
    check("criteria table has a fixed full-width layout and re-runs confirm first",
          "table-layout:fixed" in html and 'class="crit-table"><colgroup>' in html and "function confirmRun" in html and "td.rat{min-width" not in html)
    flow = html[html.index("function flowSource"):html.index("function renderFlowInto")]
    check("flowchart: Mermaid source in one place, behind the Flow button only, zoomable, Mermaid's own look under the nonce",
          "How a call is processed" in html and 'data-p="flow"' in html and 'id="flowBtn"' in html and 'id="flowZoomIn"' in html and 'id="flowCard"' not in html
          and '"/static/mermaid.min.js"' in html and not re.search(r"#[0-9a-fA-F]{3,6}\b", flow) and ".mm .node rect" not in html
          and 'ns.className = "mmstyle"; ns.nonce = NONCE' in html and "function mermaidTheme" in html and "classDef pick" in flow)
    check("flowchart: QA type set-up feeds the scorer, re-audit paths loop back, Dashboard is the last step",
          all(t in flow for t in ("Settings › QA types", "Extract criteria", "weights must total 100", "Audit again with", "Rescore with QA type",
                                  "Transcribe again with", ".-> S3", ".-> s2", "8 · Dashboard", "Q3 --> C1", "C4 --> D --> EN",
                                  "Agent dispute", "from the emailed link", 'Q3 -. "agent disagrees?" .-> Q4 -.-> Q2', "tone & delivery",
                                  "Knowledge base", "Settings › Knowledge", 'K -. "reference material" .-> S3',
                              "Ask Auditly", "never changes anything", 'R -. "questions?" .-> AA', "Q2 -.-> AA",
                                  "ST((Start))", "ST --> G", 'O -- "then, per call" --> A', 'O -. "scored against" .-> S3', "Optimise for the scorer",
                                  "Optimise or Not now", "optimised rules or as written", "EN((End))", "D --> EN",
                                  "classDef go fill:honeydew", "classDef go fill:darkgreen", "classDef stop fill:mistyrose", "classDef stop fill:darkred",
                                  ":::go", ":::stop")) and ":::end" not in flow and "ST --> G & A" not in flow)
    # the chart must describe the UI it stands for: engines/scorers come from the menus, every re-run action and the
    # Settings sub-tab are named word for word, the post-save Optimise question is there, and the docs agree on the step count
    readme_txt = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    verify_txt = open(os.path.join(ROOT, "docs", "VERIFY.md"), encoding="utf-8").read()
    check("flowchart matches the UI it describes: engines and scorers are read from the menus, not typed in",
          "sttChoices()" in flow and "stts.forEach" in flow and "scoring_choices" in flow
          and not re.search(r"mmLabel\(\[[^\]]*(nova-3|whisper|gpt-4o-transcribe)", flow) and "stt_choices" in html)
    check("flowchart names every re-run action the scorecard offers, word for word",
          all(('<option value="">' + lbl + "</option>") in html and lbl.rstrip("…") in flow
              for lbl in ("Audit again with…", "Rescore with QA type…", "Transcribe again with…")))
    check("flowchart names the Settings sub-tab and the post-save Optimise question as the UI does",
          'data-set="guidelines"' in html and ">QA types</button>" in html and "Settings › QA types" in flow
          and "Not now" in html and "Optimise or Not now" in flow and "as written" in flow and "asked after every save" in flow)
    # ── repository hygiene (0.63.1): what a public checkout must never carry ──
    ROOT_FILES = []
    for dp, dn, fn in os.walk(ROOT):
        dn[:] = [d for d in dn if d not in (".git", "__pycache__", "tls", "node_modules") and not d.startswith("uploads")]
        for f in fn:
            if f.endswith((".py", ".html", ".md", ".sh", ".sql", ".json", ".js", ".conf", ".service", ".example")) and "mermaid.min" not in f:
                ROOT_FILES.append(os.path.join(dp, f))
    leaks = []
    for f in ROOT_FILES:
        try: txt = open(f, encoding="utf-8", errors="ignore").read()
        except OSError: continue
        for pat in (r"sk-(?:proj-)?[A-Za-z0-9_-]{32,}", r"\bAKIA[0-9A-Z]{16}\b", r"\bghp_[A-Za-z0-9]{30,}\b", r"sntcom\.net", r"172\.16\.\d+\.\d+", r"/home/[a-z]+/Auditly"):
            for m in re.finditer(pat, txt):
                if "abcdefghijklmnopqrstuvwxyz1234" in m.group(0): continue     # the fake key this file uses
                leaks.append((os.path.relpath(f, ROOT), m.group(0)[:40]))
    check("repository hygiene: no committable file carries a key-shaped string, an internal hostname, a LAN address or a home path", not leaks, leaks[:6])
    lic = open(os.path.join(ROOT, "LICENSE"), encoding="utf-8").read() if os.path.exists(os.path.join(ROOT, "LICENSE")) else ""
    tpn = open(os.path.join(ROOT, "THIRD_PARTY_NOTICES.md"), encoding="utf-8").read() if os.path.exists(os.path.join(ROOT, "THIRD_PARTY_NOTICES.md")) else ""
    check("licence: MIT with the agreed copyright line, and the third-party notices name Mermaid under MIT (0.63.2)",
          lic.startswith("MIT License") and "Copyright (c) 2026 COEO (SNET Connect) — Ron Mangune and contributors" in lic
          and "Mermaid" in tpn and "MIT" in tpn and "mermaid.min.js" in tpn and "LICENSE" in readme_txt and "THIRD_PARTY_NOTICES.md" in readme_txt)
    gi = open(os.path.join(ROOT, ".gitignore"), encoding="utf-8").read()
    check("repository hygiene: .gitignore keeps the keys, the databases, the recordings, the certificate and the logs out -- including the two stray names",
          all(p_ in gi for p_ in (".env", "!.env.example", "*.db", "uploads/", "/uploads*/", "/auditly*.db*", "tls/", "*.key", "*.log", "__pycache__/")))
    if shutil.which("git") and os.path.isdir(os.path.join(ROOT, ".git")):
        probe = [".env", "auditly.db", "auditly.db-wal", "uploads/x.wav", "uploads       # created mode 700/x.wav", "auditly.db               ", "tls/auditly.key", "server.log", "docs/auditly-tour.webm"]
        not_ign = [x for x in probe if subprocess.run(["git", "-C", ROOT, "check-ignore", "-q", x]).returncode != 0]
        tracked = subprocess.run(["git", "-C", ROOT, "ls-files"], capture_output=True, text=True).stdout.split("\n")
        badt = [t for t in tracked if re.search(r"(^|/)\.env(?!\.example)|\.db|^uploads|^tls/|\.log$|\.webm$|\.key$|\.pem$", t)]
        check("repository hygiene: git ignores every secret and data path, and none is tracked", not not_ign and not badt, (not_ign, badt))
    # ── the public demo (0.65.0): container, blueprint, sandbox note ──
    dk = open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8").read()
    ry = open(os.path.join(ROOT, "render.yaml"), encoding="utf-8").read()
    di = open(os.path.join(ROOT, ".dockerignore"), encoding="utf-8").read()
    check("public demo: the Dockerfile runs demo mode with open access forced, small uploads, one-day retention, as a non-root user; the Render blueprint is a free docker web service with a health check",
          "FROM python:3.13-slim" in dk and '"--demo"' in dk and "AUDITLY_OPEN_ACCESS=1" in dk and "AUDITLY_MAX_UPLOAD_MB=10" in dk and "AUDITLY_RETENTION_DAYS=1" in dk
          and "AUDITLY_DEMO_NOTE=" in dk and "USER auditly" in dk and "EXPOSE 10000" in dk
          and "runtime: docker" in ry and "plan: free" in ry and "healthCheckPath: /health" in ry and 'value: "1"' in ry and "name: auditly-demo" in ry
          and all(p_ in di for p_ in (".env", "*.db", "uploads", "tls", "*.key", ".git")) and "!.env.example" in di)
    idx = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
    check("the repository's static front door (0.66.0): index.html is a landing page for GitHub Pages -- links to the live demo and the code, nothing external, and it is not the app",
          os.path.exists(os.path.join(ROOT, "auditly.html")) and "auditly-demo.onrender.com" in idx and "github.com/rr-man/auditly" in idx
          and "<script" not in idx and 'href="http' not in idx.replace('href="https://auditly-demo.onrender.com/"', "").replace('href="https://github.com/rr-man/auditly"', "").replace('href="https://github.com/rr-man/auditly#readme"', "")
          and "GitHub Pages serves files only" in idx and "__APP_VERSION__" not in idx)
    check("flowchart has eight steps and the docs say so",
          flow.count("subgraph ") == 8 and "eight-step flowchart" in readme_txt and "all eight steps" in verify_txt, flow.count("subgraph "))
    # coaching is a step of its own (0.44.0), not a node in the audit: the chart names what the tab does
    check("flowchart: coaching is its own step, between the audit and the Dashboard, and names what the tab does",
          all(t in flow for t in ("7 · Coaching", "Coaching tab", "a running tally", "Totals and an agenda",
                                  "Mark delivered", "is stamped coached", "Email invite",
                                  'C3 -. "invite the agent" .-> C5')) and "Q5[" not in flow)
    check("flowchart: labels are measured where they are drawn (container passed to render), with room in every box",
          "withMermaidStyles(function () { return mm.render(" in html and ", src, el); }); })" in html and "padding: 16, wrappingWidth: 280 }" in html
          and "this.style.cssText = String(value)" in html and 'sa.call(node, "nonce", NONCE)' in html)
    s, js, hd = anon.raw("/static/mermaid.min.js")
    check("vendored Mermaid served same-origin as JavaScript, cached immutable",
          s == 200 and "javascript" in hd.get("Content-Type", "") and "immutable" in hd.get("Cache-Control", "") and len(js) > 1_000_000, (s, hd.get("Content-Type"), len(js)))
    s, _, _ = anon.raw("/static/nope.js")
    check("nothing else under /static is served", s == 404)
    node = shutil.which("node")
    if node:
        r = subprocess.run([node, os.path.join(ROOT, "tests", "mermaid_parse.js"), os.path.join(ROOT, "auditly.html"),
                            os.path.join(ROOT, "static", "mermaid.min.js")], capture_output=True, text=True, timeout=120)
        check("flowchart source parses with the vendored Mermaid (node), every variant", r.returncode == 0 and "FAIL" not in r.stdout, (r.stdout[-400:], r.stderr[-300:]))
    else:
        print("  skip flowchart parse check: node is not installed")
    check("footer credit links to LinkedIn safely", re.search(r'<footer[^>]*>.*Created by.*<a href="https://www\.linkedin\.com/in/ronmangune/"[^>]*rel="noopener noreferrer"[^>]*>.*Ron Mangune</a>.*</footer>', html, re.S) is not None)
    check("footer credits Amy Gallo on LinkedIn safely", re.search(r'<footer[^>]*>.*Contributors:.*<a href="https://www\.linkedin\.com/in/amy-gallo-18b47669/"[^>]*rel="noopener noreferrer"[^>]*>.*Amy Gallo</a>.*</footer>', html, re.S) is not None)
    check("footer credits Anah Aquino on LinkedIn safely", re.search(r'<footer[^>]*>.*Contributors:.*<a href="https://www\.linkedin\.com/in/anah-jaaziel-aquino/"[^>]*rel="noopener noreferrer"[^>]*>.*Anah Aquino</a>.*</footer>', html, re.S) is not None)
    check("footer is pushed to the bottom of the window (flex column body)", re.search(r"body\{[^}]*display:flex;flex-direction:column", html) is not None and re.search(r"\.foot\{[^}]*margin-top:auto", html) is not None)
    check("header and tab bar share one sticky wrapper (no overlap on scroll)", re.search(r"\.top\{[^}]*position:sticky", html) is not None and re.search(r'<div class="top">\s*<header class="bar">', html) is not None and not re.search(r"header\.bar\{[^}]*sticky", html, re.S) and not re.search(r"\.tabs\{[^}]*sticky", html))
    check("no native dialogs in HTML", not re.search(r"\b(alert|confirm|prompt)\(", html))
    check("Help chip opens the six-step guide with Go there links, Good to know, Flow hand-off and a once-per-browser flag",
          'id="helpBtn"' in html and "function guideDialog" in html and "Good to know" in html
          and "auditly-guide-seen" in html and "guideDialog(true)" in html and 'data-x="flow"' in html
          and "You can reopen this any time from Help at the top." in html
          and all((', "%s")' % g) in html for g in ("qa", "upload", "calls", "audit", "dashboard")) and html.count("+ step(") == 6
          and "function showApp" in html and html.index("auditly-guide-seen") > html.index("function showApp") and html.index("auditly-guide-seen") < html.index("function showLogin") + 5000)
    check("Help has two tabs: How to use and About (creator, contributors with LinkedIn, audience, problem, principles, data and cost)",
          'id="helpSeg"' in html and 'data-p="how"' in html and 'data-p="about"' in html and 'id="helpBody"' in html
          and "Who made it" in html and "The problem it solves" in html and "Who it is for" in html and "What it stands for" in html and "Data and cost" in html
          and "COEO (SNET Connect)" in html and html.count("Ron Mangune") >= 2 and html.count("linkedin.com/in/amy-gallo") == 2
          and html.count("linkedin.com/in/anah-jaaziel-aquino") == 2 and html.count("linkedin.com/in/ronmangune") == 2
          and 'data-tab-how="1"' in html and "function show(p)" in html and "guideDialog(first, tab)" in html
          and html.index("<h3>Who made it</h3>") < html.index("<h3>Who it is for</h3>"))
    # 0.54.0 -- Show me around: the guided tour over the real screens
    tour_js = html[html.index("var TOUR = ["):html.index("// \u2500\u2500 calls list")]
    check("Show me around: overlay ids, both Help entry points, marker key, never auto-started",
          all(('id="%s"' % i) in html for i in ("tourWrap", "tourHole", "tourCard", "tourTitle", "tourText", "tourStep", "tourBack", "tourNext", "tourSkip", "tourUpload", "tourGuide"))
          and all(f in html for f in ("function tourStart", "function tourEnd", "function tourGo", "function tourPlace", "function waitFor", "function tourSampleJob"))
          and "auditly-tour-done" in html and html.count('data-x="tour" title="') == 2 and "New here?" in html and html.count("Show me around") >= 2
          and "tourStart(" not in html[html.index("function showApp"):html.index("function setTab")]
          and "if (S.tour) tourEnd();" in html[html.index("function showLogin"):html.index("function showApp")])
    stops = re.findall(r'\bsel: "([^"]+)"', tour_js)
    check("tour: nine stops, each spotlighting an element the page really has",
          len(stops) == 9 and html.count("+ step(") == 6
          and all(re.search(r'\bid="%s"' % re.match(r"#([\w-]+)", s_).group(1), html) for s_ in stops if s_.startswith("#")), stops)
    check("tour: keyboard, focus trap, reduced motion, sticky-header offset, clamped card, docks in a small window, modals and docks closed first",
          all(k in tour_js for k in ('e.key === "Escape"', '"ArrowRight"', '"ArrowLeft"', '"Tab"', 'querySelectorAll(".modalwrap")', "closeAsk(); closeNotif();",
                                     'querySelector(".top")', "getBoundingClientRect", "requestAnimationFrame", 'behavior: "auto"', "vw < 700 || vh < 560", "S.tour === t && t.seq === seq"))
          and "#tourHole{" in html and "z-index:55" in html and "#tourWrap.dock #tourCard{" in html
          and '#tourHole{transition:none}' in html and "data-go" not in tour_js)
    check("Show me around sits in the header and runs on sample data (0.56.0): chip hidden until sign-in, the gate card, the Demo filters and their restore",
          'id="tourBtn"' in html and '$("tourBtn").onclick' in html and 'id="tourLoad"' in html and 'id="tourReal"' in html
          and '$("tourBtn").classList.remove("hidden")' in html[html.index("function showApp"):html.index("function setTab")]
          and '$("tourBtn").classList.add("hidden")' in html[html.index("function showLogin"):html.index("function showApp")]
          and all(k in tour_js for k in ("function tourPrepare", "function tourGate", "function tourUseDemo", "function tourRestore", "/api/admin/demo-data/load",
                                         "Use sample data?", "S.tour.demo", "kind=demo", '$("jobKind").value = "demo"', 'segOn("dbKind", t.prev.dbKind)')))
    pii_re = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}")
    phone_re = re.compile(r"(?<!\d)(?:\+?\d{1,3}[ -]?)?\(?\d{3}\)?[ -]?\d{3}[ -]?\d{4}(?!\d)")
    pii_bad = []
    for fn in ("demo_data.py", "fixtures/demo_call.json", "fixtures/demo_scorecard.json", "fixtures/demo_kb.md"):
        txt = open(os.path.join(ROOT, fn), encoding="utf-8").read()
        pii_bad += [(fn, m) for m in pii_re.findall(txt) if not (m.endswith("@example.com") or m.endswith("@local"))]
        pii_bad += [(fn, m) for m in phone_re.findall(txt)]
    check("sample data is fictional: no email outside example.com/@local and no phone number in the demo seed or its fixtures", not pii_bad, pii_bad)
    check("the Dashboard leads with what is going well (0.57.0): earn points strongest first, Critical checks passed, Leading / Most room to grow, amber dips, Ready for coaching",
          "Where calls earn points" in html and "Critical checks passed" in html and ">Leading<" in html and ">Most room to grow<" in html
          and "most improved" in html and "Ready for coaching" in html and ".kpi .d.down{color:var(--warn)}" in html
          and 'BL = [["ge90", "90 and above"]' in html and "pts/call to gain" in html and "where coaching pays off most" in html
          and not any(w in html for w in ("Where calls lose points", "Agents to watch", "Needs attention", "Auto-fail rate", "Awaiting coaching", "10 weakest")))
    check("the walk on sample data keeps Coaching to the made-up agents (0.56.1)",
          "S.tour.agents" in html[html.index("function loadCoachAgents"):html.index("function loadCoachAgents") + 2000]
          and "S.tour.agents" in html[html.index("function loadSessions"):html.index("function loadSessions") + 1400]
          and "Sample data has no coaching yet." in html and "if (S.tour && S.tour.demo) p.then(lists); else lists();" in html)
    check("processing and audit-done animations (0.58.0): stage motifs, the ring drawing in, the AI and QA stamps, all still under reduced motion",
          all(k in html for k in ("@keyframes wave", "@keyframes think", "@keyframes rise", "@keyframes draw", "@keyframes stampin", "function ringDraw", "function celebrate", "function motif",
                                  "function noMotion", "pre-audited by AI", "final score in", 'var TICK_SVG', '"397,348 469,420 615,256"', "S.jobLive[id] = true;", 'if (path === "/review/submit") celebrate($("auditView"));'))
          and ".mot.wave i," in html and ".mot.tick polyline{stroke-dashoffset:0}" in html
          and "--tick:#F4821F" in html and ".stamp.qa{color:var(--tick)}" in html
          and 'cls === "done" ? motif("done") : cls === "on" ? motif(st[0])' in html)
    check("the walk's Processing stop stages the tracker with the Calls page's own renderer, on timers the walk clears (0.59.0)",
          'title: "Processing"' in tour_js and "function tourStage" in tour_js and "renderJobStatus(" in tour_js and "t.timers.push(" in tour_js
          and "(t.timers || []).forEach(clearTimeout)" in tour_js and tour_js.index('title: "Calls"') < tour_js.index('title: "Processing"') < tour_js.index('title: "Scorecard"'))
    ask_click = html[html.index('document.addEventListener("click", function (e) {'):html.index("// \u2500\u2500 notifications")]
    check("Ask Auditly is in the walk (0.62.0): a stop after the scorecard opens the dock, the walk closes it on every other stop and at the end, the click-outside closer stays quiet meanwhile",
          'sel: "#askDock"' in tour_js and 'title: "Ask Auditly"' in tour_js and tour_js.index('title: "Scorecard"') < tour_js.index('title: "Ask Auditly"') < tour_js.index('title: "Audit"')
          and "function openAsk" in html and "openAsk();" in tour_js and tour_js.count("closeAsk();") >= 3 and "if (S.tour) return;" in ask_click
          and "S.health.ask.problem" in tour_js and "The round button in the lower right is Ask Auditly" not in tour_js)
    check("the callout never sits on the spotlight (0.59.1): below, above, right, left, else docked along the bottom under a clipped hole with room to scroll; Processing says it landed",
          "h.left - GAP - cw >= M" in tour_js and "else docked = true;" in tour_js and "h.bottom = Math.min(h.bottom, cardTop - GAP)" in tour_js
          and '$("app").style.paddingBottom = (18 + vh - cardTop + M)' in tour_js and '$("app").style.paddingBottom = "";' in tour_js
          and "function tourLimit" in tour_js and "tourScrollTo(el, tourLimit())" in tour_js and "lower right, over the dimmed page" not in tour_js
          and "docked = small || !!t.docked" in tour_js and "t.docked = false" in tour_js
          and "#tourWrap.dock #tourCard{left:12px;right:12px;bottom:12px;top:auto;width:auto;max-width:720px;margin:0 auto}" in html
          and 'landed: "There it is' in tour_js and "TOUR[t.i].landed" in tour_js)
    # 0.60.0: one <audio> for the whole page, borrowed by the open scorecard and parked in the bar; rewind; Preferences
    rsc = html[html.index("function renderScorecard"):html.index("function normTs")]
    rau = html[html.index("function renderAudit"):html.index("// \u2500\u2500 dashboard")]
    stb = html[html.index("function setTab"):html.index("function setSettingsTab")]
    check("one player for the page (0.60.0): born in the playback bar, mounted into the open scorecard, parked -- never re-created -- on every re-render and tab change",
          html.count("<audio id=") == 1 and html.index('id="playBar"') < html.index('<audio id="player"') < html.index('id="playCtl"')
          and "function mountPlayer" in html and "function parkPlayer" in html and "function playSkip" in html and 'class="playerSlot"' in html
          and rsc.index('parkPlayer("render", root)') < rsc.index("root.innerHTML") < rsc.index('mountPlayer(root, sc, "calls")')
          and rau.index('parkPlayer("render", root)') < rau.index("root.innerHTML") < rau.index('mountPlayer(root, sc, "audit")')
          and 'parkPlayer("leave")' in stb and 'parkPlayer("leave", $("auditView"))' in html and 'parkPlayer("leave", $("jobView"))' in html
          and html.count('parkPlayer("render", $("jobView"))') == 2 and 'if (reason === "render") return;' in html
          and "slot.dataset.rec === p.dataset.rec" in html and 'root.querySelector("#player")' not in html)
    check("Customer information (Captured), ticket number, Details and rename (0.61.0)",
          "Customer information <span" in html and "(Captured)" in html and '["Ticket number", r.ticket_ref || cust.reference]' in html and '"Reference"' not in html
          and "function factsHtml" in html and '<details class="upd facts">' in html and 'data-critrow="' in html and 'class="pill fact"' in html
          and 'data-rename="1"' in html and 'id="rnName"' in html and 'id="dtPhone"' in html and 'id="dtTicket"' in html and '"custPhone", "tel", 40' in html and '"custTicket", "text", 80' in html
          and 'data-f="example_good"' in html and 'data-f="example_bad"' in html and "Good sample" in html and "Bad sample" in html)
    sync_js = html[html.index("function renderSyncPanel"):html.index("function syncDiffHtml")]
    check("animation while optimising and when coaching is delivered (0.63.0): motif on the live card, the tick stamps the done line when the page watched it land, Mark delivered stamps the session and ticks its calls, the coached mark draws its tick",
          "function stampEl" in html and "function stampIn" in html and "function celebrateSession" in html and "var st = stampEl(kind, caption); wrap.appendChild(st);" in html
          and 'motif("queued") : motif("scoring")' in sync_js and 'id="syncDoneLine"' in sync_js
          and 'stampIn($("syncDoneLine"), "ai", "optimised for the scorer")' in html and 'celebrateSession($("csView"))' in html
          and 'stampIn(head, "qa", "coaching delivered")' in html and 'rv.coached_at ? motif("done") + "yes' in html and ".stamp.inline{" in html)
    check("rewind and forward (0.60.0): 10 and 20 s either way on the scorecard, 10 s on the bar, a speed menu, and the keep-playing switch on both",
          all(('data-skip="%s"' % v) in html for v in ("-20", "-10", "10", "20")) and 'data-speed="1"' in html and 'data-keepchk="1"' in html
          and '"auditly-play-keep"' in html and "function playKeepSet" in html and 'localStorage.getItem("auditly-play-keep") !== "0"' in html
          and 'id="playOpen"' in html and 'id="playClose"' in html and "if (live && S.playKeep)" in html)
    rm_blocks = re.findall(r"@media \(prefers-reduced-motion:reduce\)\{", html)
    check("Settings › Preferences (0.60.0): Animations Auto / On / Off on <html> beside the system setting, per browser; every reduced-motion rule is guarded and has an Off twin",
          'data-set="prefs"' in html and ">Preferences</button>" in html and 'id="set-prefs"' in html and 'id="prefMotion"' in html and 'id="prefKeep"' in html
          and "function applyMotion" in html and '"auditly-motion"' in html and 'document.documentElement.setAttribute("data-motion", m)' in html
          and 'S.motion === "off" || (S.motion !== "on" &&' in html
          and len(rm_blocks) == 4 and html.count(':root[data-motion="off"] ') >= 4 and html.count(':root:not([data-motion="on"]) ') >= html.count(':root[data-motion="off"] ')
          and '"prefs", "account"]' in html and 'S.motion = (savedMotion === "on" || savedMotion === "off") ? savedMotion : "auto"' in html
          and html.index("S.motion = (savedMotion") < html.index("applyTheme(saved);"))
    up_js = html[html.index("function renderUploads"):html.index("function renderUploads") + 9000]
    check("a quieter finished upload row (0.59.0): one-line notes behind details, no engine pill once done",
          '<details class="upd ok">' in up_js and '<details class="upd warn">' in up_js and "Transcript reused" in up_js and "Uploaded before" in up_js
          and '(u.status === "done" ? "" : ' in up_js and "upnote" not in html)
    check("tab loaders are awaitable (the tour waits for them)",
          all(x in html for x in ("return loadJobs(); }", "return backToQueue(); }", "return loadPeople().then(loadCoaching); }", "return loadDashboard(); }); }"))
          and '$("auditList").classList.remove("hidden"); return loadReviews(); }' in html)
    # tooltips: a title on the same tag as the id, in either attribute order; works for static markup and JS-built strings
    def titled(ident):
        return re.search(r'<[a-z]+\b[^>]*\bid="%s"[^>]*\btitle="' % ident, html) is not None \
            or re.search(r'<[a-z]+\b[^>]*\btitle="[^"]*"[^>]*\bid="%s"' % ident, html) is not None
    TITLED_IDS = ("helpBtn", "flowBtn", "verBtn", "logoutBtn", "upRubric", "drop", "refreshJobs", "jobStatus", "prevPage", "nextPage", "backBtn", "retryBtn",
                  "custSave", "custCancel", "ovScore", "ovNote", "refreshReviews", "rvPrev", "rvNext", "rvFollow", "rvCoached", "rvCoaching",
                  "rvResolution", "rvPlan", "dtCaller", "dtCompany", "dtEmail", "dtAudit", "dtSave", "dtPhone", "dtTicket", "rnName", "sqQa", "sqNew", "addUserBtn", "pwCur", "pwNew",
                  "nuEmail", "nuRole", "nuName", "nuPw", "npw", "edBack", "edName", "edNotes", "edSource", "edFile", "edExtract", "edAdd", "edNorm", "edSave",
                  "syncGo", "syncBack", "syncBackTop", "syncOpen", "syncRefresh", "dspReason", "dspNote", "dspScore",
                  "coachKind", "csNew", "csRefresh", "csPrev", "csNext", "nsAgent", "nsQa", "nsTitle", "csBack", "csTitle", "csLoc", "csAgenda",
                  "csNotes", "csSave", "schDate", "schTime", "schDur", "schLoc", "calPrev", "calToday", "calNext", "pemail", "csInvite", "invTo", "invQa", "mailTest",
                  "invDraft", "invMark", "invMailto",
                  "kbAddBtn", "kbQ", "kbQRubric", "kbQGo", "kbTitle", "kbRubric", "kbText", "kbPick", "kbHint", "askBtn", "askClose", "nextGo", "nextHide", "upMoreSum", "askQ", "askSend",
                  "askAdmModel", "askAdmDay", "askAdmHour", "askAdmSave", "askAdmPersona", "askAdmNote", "askAdmSavePrompt", "askAdmReset", "askAdmTryQ", "askAdmTryGo", "askMic", "brandApply", "brandCancel",
                  "notifBtn", "notifAll", "notifPrefs", "notifClose", "ntfSave", "ntfReadAll", "dlTo", "dlSend", "dlCopy", "dlUrl", "dlMailto", "focusClear", "focusBtn", "playOpen", "playClose", "tourBack", "tourNext", "tourSkip", "tourUpload", "tourGuide", "tourBtn", "tourLoad", "tourReal")
    missing = [i for i in TITLED_IDS if not titled(i)]
    check("every control in the tooltip inventory carries a title", not missing, missing)
    TITLED_ATTRS = ('data-theme="light"', 'data-theme="dark"', 'data-v="day"', 'data-v="year"', 'data-v="30d"', 'data-v="ytd"', 'data-v="custom"',
                    'data-p="cl"', 'data-p="plan"', 'data-f="weight"', 'data-f="guidance_met"', 'data-f="guidance_missed"', 'data-x="clear"',
                    'data-dsp-all="1"', 'data-dsplink="1"', 'data-focusrec="1"', 'data-focus="', 'data-x="tour"', 'data-st="upheld"', 'data-st="rejected"', 'data-v="exec"', 'data-v="detail"',
                    'data-v="waiting"', 'data-v="audited"', 'data-pick="all"', 'data-pick="none"', 'data-v="auditly"', 'data-v="coeo"', 'data-v="hidden"', 'data-v="custom"', 'data-set="notifications"',
                    'data-v="month"', 'data-v="week"', 'data-v="on"', 'data-v="off"',
                    'data-v="status"', 'data-v="providers"', 'data-v="assistant"', 'data-v="branding"',
                    'data-skip="', 'data-keepchk="', 'data-speed="', 'data-motion="', 'data-keep="', 'data-set="prefs"',
                    'data-f="example_good"', 'data-f="example_bad"', 'data-rename="1"', 'data-crit="')
    missing = [a for a in TITLED_ATTRS if not re.search(re.escape(a) + r'[^>]*\btitle="', html)]
    check("segment, criterion and override buttons carry titles", not missing, missing)
    check("row actions, pills and status pills carry titles",
          all(re.search(r'%s="[^"]*"[^>]*\btitle="' % a, html) for a in ("data-retry", "data-del", "data-act", "data-arch", "data-sync", "data-opt"))
          and re.search(r'<th[^>]*title="[^"]*">Uploaded</th>', html) is not None and re.search(r'<th[^>]*title="[^"]*">Provider</th>', html) is not None
          and 'class="ev" data-ts="' in html and "var STATUS_TIP" in html and "function statusPill" in html and html.count("statusPill(") == 4
          and all(k in html for k in ("transcribing:", "scoring:", "failed:")) and 'class="pill crit" title="' in html
          and '<summary title="' in html and '<span class="pill">archived' not in html)
    check("Audit and Dashboard tabs present", 'data-tab="audit"' in html and 'data-tab="dashboard"' in html
          and 'id="tab-audit"' in html and 'id="tab-dashboard"' in html)
    check("Calls has Start audit, Audit has To audit/Completed sub-tabs, Dashboard a Real/Demo switch, Settings a Demo data card",
          'id="sendSel"' in html and 'id="selAll"' in html and "Start audit" in html and 'id="rvTabs"' in html and 'data-v="done"' in html
          and 'id="dbKind"' in html and 'id="demoDataCard"' in html and "function sendToAudit" in html)
    chart_css = html[html.index(".chart{"):html.index("</style>")]
    check("chart styles use theme tokens only (no hex)", not re.search(r"#[0-9a-fA-F]{3,6}\b", chart_css) and "function lineChart" in html)
    # style-src is nonce-only and a nonce never applies to a style attribute, so every
    # style="" in the page (static or built by JS) is refused by the browser (0.14.4)
    check("no inline style attribute anywhere in the page (CSP refuses them)", 'style="' not in html and "style='" not in html)
    # 0.45.0 — the upload form asks for two things; everything with a working default is behind a disclosure
    check("Upload asks only for the QA type, the agent and a file; the defaulted choices are behind More options",
          'id="upMore"' in html and '<summary id="upMoreSum"' in html
          and all(html.index('id="upMore"') < html.index(i) for i in ('id="upScorer"', 'id="upQa"', 'id="upAudit"', 'id="runSeg"'))
          and html.index('id="upAgent"') < html.index('id="upMore"') and html.index('id="drop"') < html.index('id="upMore"')
          and "auditly-upmore" in html and 'if (h.demo && S.upMoreSaved == null) $("upMore").open = true;' in html)
    # the Next step bar: one answer to "what now?", and the four dead ends it closes
    nx = html[html.index("function nextPlan"):html.index("function nextShow")]
    check("Next step bar: a stage-driven sentence and one button, on every tab, dismissible and restorable",
          'id="nextBar"' in html and 'id="nextGo"' in html and 'id="nextHide"' in html and "function renderNext" in html
          and "auditly-next-off" in html and "function nextShow" in html and 'data-x="nextshow"' in html
          and "data-go" not in nx                                   # data-go already belongs to the guide and the pipeline strip
          and all(st in nx for st in ('n.status === "failed"', 'n.status !== "done"', 'n.stage === "scored"',
                                      'n.stage === "queued" || n.stage === "in_review"', 'n.stage === "audited"', 'n.stage === "coached"')))
    check("Next step takes the user to the work: the call, the audit queue, the agent's coaching, the trends",
          all(a in nx for a in ('setTab("calls"); openJobById(n.id);', 'setRvTab("todo"); setTab("audit"); if (n.sc) openAudit(n.sc);',
                                'S.coach.agent = n.agent;', 'setTab("dashboard")')))
    check("starting several audits at once now lands in the audit queue instead of a toast on Calls",
          'if ((d.queued || []).length) { setRvTab("todo"); setTab("audit"); return; }' in html)
    check("one place chooses the coaching agent, so the Next step bar and the Agents row agree",
          "function pickCoachAgent" in html and "pickCoachAgent(S.coach.agent === tr.dataset.agent ? null : tr.dataset.agent)" in html
          and html.count("S.coach.agent = S.coach.agent ===") == 0)
    check("Upload tab: customer/call-ID inputs gone, Remove button present",
          all(x not in html for x in ('id="upRef"', 'id="upCaller"', 'id="upCompany"', 'id="upEmail"')) and "data-remove" in html)
    check("scorecard has the sentiment banner, speaker roles and carry-over checkbox",
          "sentbar" in html and "data-role=" in html and 'id="carryOver"' in html and 'id="editRubricLink"' in html)
    check("rubric is called QA type on screen", "QA types" in html and "<th>Rubric</th>" not in html and "Score against rubric" not in html)
    check("dashboard pipeline strip, header logo, and a Light/Dark-only theme switch",
          'id="dbPipe"' in html and 'id="logoImg"' in html and '/static/auditly_light.svg' in html
          and 'data-theme="auto"' not in html and 'data-theme="light"' in html and 'data-theme="dark"' in html)
    s_l, b_l, hd_l = anon.raw("/static/coeo_light.svg")
    check("COEO logo served same-origin as SVG with a long cache", s_l == 200 and hd_l.get("Content-Type", "").startswith("image/svg+xml")
          and "immutable" in hd_l.get("Cache-Control", "") and b_l.startswith(b"<svg"), (s_l, hd_l.get("Content-Type")))
    check("QA type view has Edit, Save as new QA type, What changed and the Last edited stamp",
          'data-x="saveas"' in html and 'data-x="edit"' in html and 'id="edNotes"' in html and "Last edited" in html and ">New version<" not in html)
    # A syntax error in the inline script leaves both views hidden (a blank page,
    # 2026-09-02). node is not a dependency of the app; the test uses it if present.
    js = re.search(r"<script[^>]*>(.*?)</script>", html, re.S)
    node = shutil.which("node")
    if js and node:
        jsf = os.path.join(tempfile.mkdtemp(prefix="auditly-js-"), "app.js")
        with open(jsf, "w", encoding="utf-8") as fh:
            fh.write(js.group(1))
        r = subprocess.run([node, "--check", jsf], capture_output=True, text=True)
        check("front-end script parses (node --check)", r.returncode == 0, r.stderr.strip()[:300])
    else:
        check("front-end script parses (node --check)", js is not None, "node not installed: parse not checked")
        if not node:
            print("       (node not found; front-end syntax check skipped)")
    s, fb, fh = anon.raw("/favicon.ico")
    check("/favicon.ico serves the brand tab icon, cached an hour", s == 200 and fh.get("Content-Type", "").startswith("image/") and b"<svg" in fb and "max-age=3600" in fh.get("Cache-Control", ""))
    # ── branding (white label): public read, admin write ─────────────
    s, br = anon.json("/api/branding")
    ver = json.loads(anon.raw("/health")[1].decode())["version"]
    check("branding is public and defaults to the Auditly marks, light and dark, with tab icons; the app name is hidden beside a wordmark",
          s == 200 and br.get("logo_mode") == "auditly" and br["logos"] == {"light": "/static/auditly_light.svg?v=" + ver, "dark": "/static/auditly_dark.svg?v=" + ver}
          and br["icons"]["dark"] == "/static/auditly_icon_dark.svg?v=" + ver and br.get("show_name") is False and br.get("custom") == {} and len(br.get("png_hint") or []) == 4, br)
    # the built-in files keep their names release to release and are served immutable for a year: without
    # the build in the URL a browser goes on drawing the artwork it first saw (0.43.1)
    check("every built-in brand URL carries the build, so a new release retires the cached copy",
          all(u.endswith("?v=" + ver) for u in list(br["logos"].values()) + list(br["icons"].values())) and ver != "",
          (br["logos"], ver))
    s, _, _ = anon.raw("/api/branding/asset/logo_light")
    check("no uploaded asset yet -> 404", s == 404)
    brand = {}
    for nm in ("auditly_light.svg", "auditly_dark.svg", "auditly_icon_light.svg", "auditly_icon_dark.svg"):
        s_a, b_a, h_a = anon.raw("/static/" + nm)
        brand[nm] = b_a
        if not (s_a == 200 and h_a.get("Content-Type", "").startswith("image/svg") and b"<svg" in b_a and b"<script" not in b_a
                and b"immutable" in h_a.get("Cache-Control", "").encode()):
            check("brand asset served: " + nm, False, (s_a, h_a.get("Content-Type")))
            break
    else:
        check("the four built-in brand SVGs are served, immutable, script-free", True)
    # the real artwork, not a stand-in: the wordmarks carry the lettering paths and the stamp animation,
    # the icons the "i" stem and the tick on a solid square (a tab icon must never start out blank)
    wm_l, wm_d = brand["auditly_light.svg"], brand["auditly_dark.svg"]
    ic_l, ic_d = brand["auditly_icon_light.svg"], brand["auditly_icon_dark.svg"]
    check("wordmarks are the supplied artwork and animate on load, with a reduced-motion fall-back",
          all(b'd="M251.0 438.5' in w and b"#F4821F" in w and b"@keyframes" in w and b"prefers-reduced-motion" in w
              and b'class="l l6"' in w and b"<rect" not in w for w in (wm_l, wm_d))
          and b"fill:#0A2342" in wm_l and b"fill:#F5F0E8" in wm_d,
          (b"@keyframes" in wm_l, b"<rect" in wm_l, b"fill:#0A2342" in wm_l, b"fill:#F5F0E8" in wm_d))
    check("tab icons are cut from the same artwork, static, on a solid square",
          all(b'd="M753.6 434.2' in i and b"M755.5 244.0" in i and b"<rect" in i and b"@keyframes" not in i for i in (ic_l, ic_d))
          and b'fill="#0A2342"' in ic_d and b'fill="#F5F0E8"' in ic_l, (b"<rect" in ic_d, b"@keyframes" in ic_d))
    # the Ask Auditly launcher mark: the same stamp on a square canvas, transparent, with the ink
    # inverted because the button inverts — the file suffix is the theme the file is SHOWN IN
    mark = {}
    for nm in ("auditly_mark_light.svg", "auditly_mark_dark.svg", "auditly_mark_light_still.svg", "auditly_mark_dark_still.svg"):
        s_a, b_a, h_a = anon.raw("/static/" + nm)
        mark[nm] = b_a
        if not (s_a == 200 and h_a.get("Content-Type", "").startswith("image/svg") and b"<svg" in b_a and b"<script" not in b_a
                and b"immutable" in h_a.get("Cache-Control", "").encode()):
            check("launcher mark served: " + nm, False, (s_a, h_a.get("Content-Type")))
            break
    else:
        check("the four Ask Auditly launcher marks are served, immutable, script-free", True)
    mk_l, mk_d = mark["auditly_mark_light.svg"], mark["auditly_mark_dark.svg"]
    check("launcher marks are the supplied artwork: the stem, the orange tick, no background square",
          all(b'x="434" y="465"' in m and b"#F4821F" in m and b'viewBox="0 0 960 960"' in m and b'<rect width="960"' not in m
              for m in (mk_l, mk_d))
          and b'fill="#F5F0E8"' in mk_l and b'fill="#0A2342"' in mk_d,      # cream on the navy button, navy on the cream one
          (b'fill="#F5F0E8"' in mk_l, b'fill="#0A2342"' in mk_d))
    check("the launcher mark stamps itself, and its reduced-motion rule stops the squash too (the supplied file left it running)",
          all(b"@keyframes" in m and b".s,.drop,.squash{animation:none" in m for m in (mk_l, mk_d)))
    check("a motionless twin of each launcher mark is served for reduced motion",
          all(b"@keyframes" not in mark[n] and b"animation" not in mark[n] and b'x="434" y="465"' in mark[n]
              for n in ("auditly_mark_light_still.svg", "auditly_mark_dark_still.svg")))
    for p in ("/api/jobs", "/api/rubrics", "/api/health", "/api/me"):
        s, _ = anon.json(p)
        check("%s is 401 anonymous" % p, s == 401)
    s, _ = anon.json("/api/nothing")
    check("unknown api path is 401 (not 404 leak) anonymous", s == 401)
    s, _, _ = anon.raw("/api/jobs", "PUT")
    check("PUT is 405", s == 405)

    # ── auth ──────────────────────────────────────────────────────────
    c = Client()
    s, _ = c.login("demo@example.com", "wrong-password-xx")
    check("bad password 401", s == 401)
    s, _ = c.login("nobody@example.com", "wrong-password-xx")
    check("unknown user same 401", s == 401)
    s, me = c.login("demo@example.com", "demo-demo-demo")
    check("demo login ok", s == 200 and me.get("role") == "admin")
    s, me2 = c.json("/api/me")
    check("/api/me after login", s == 200 and me2.get("email") == "demo@example.com")
    s, _, _ = c.raw("/api/jobs/x/retry", "POST", body={}, csrf=False)
    check("POST without CSRF header is 403", s == 403)
    s, hh = c.json("/api/health")
    check("health lists the legacy L1QA_* names still in use (a list; empty once .env is renamed)", isinstance(hh.get("legacy_keys"), list), hh.get("legacy_keys"))
    check("health hides key values", "test-secret" not in json.dumps(hh))
    check("health reports key presence + length", hh["deepgram"]["key_present"] and hh["deepgram"]["key_len"] > 0)
    check("health reports 25 MB cap", hh["openai"]["stt_cap_mb"] == 25)

    # ── seeded demo job ───────────────────────────────────────────────
    s, jobs = c.json("/api/jobs")
    check("jobs list", s == 200 and jobs.get("total", 0) >= 1)
    demo = [j for j in jobs["jobs"] if (j.get("recording") or {}).get("call_ref") == "DEMO-0001"]
    check("seeded demo job present and done", demo and demo[0]["status"] == "done", jobs)
    demo = demo[0]
    check("demo job overall is 90.0", demo.get("overall_pct") == 90.0, demo.get("overall_pct"))
    check("job serialization hides path/sha256", "sha256" not in json.dumps(demo) and "uploads" not in json.dumps(demo))

    s, sc = c.json("/api/scorecards/" + demo["scorecard_id"])
    check("scorecard loads", s == 200 and sc.get("id") == demo["scorecard_id"])
    check("one item per criterion (8)", len(sc["items"]) == 8)
    check("every score within [0, weight]", all(0 <= i["score"] <= i["weight"] for i in sc["items"]))
    check("weights sum to 100", sum(i["weight"] for i in sc["items"]) == 100)
    pct, basis = score_mod.compute_overall(sc["items"])
    check("overall equals server-side recompute", pct == sc["overall_pct"] and basis == sc["applicable_weight"])
    check("agent speaker identified", sc["agent_speaker"] == 0)
    check("scorer extracted the customer from the call (prompt v5)", (sc.get("customer") or {}).get("company") == "Harbor Dental"
          and sc["customer"].get("name") == "Maria" and sc["customer"].get("email") is None, sc.get("customer"))
    check("scorer labelled the speakers and the transfer flag", [ (x["speaker"], x["role"]) for x in sc.get("speakers") or [] ] == [(0, "agent"), (1, "caller")]
          and sc.get("transferred") is False, (sc.get("speakers"), sc.get("transferred")))
    check("misses and opportunities present", len(sc["misses"]) == 2 and len(sc["opportunities"]) >= 3)
    check("critical criterion partial -> no auto-fail", sc["auto_fail"] is False)
    check("evidence quotes verified (no warnings)", sc["warnings"] == [], sc["warnings"])
    check("seeded demo scorecard was scored with the guidance as written (optimisation is manual)", sc.get("guidance_mode") == "authored", sc.get("guidance_mode"))
    # ── knowledge base: the demo document's phone-fault chunks were shown to the scorer ──
    import kb as kb_mod
    kbx = sc.get("kb") or []
    check("demo scorecard carries the knowledge-base excerpts that matched the call (phone procedure, not billing) and prompt v8",
          sc.get("kb_used") is True and 1 <= len(kbx) <= 3 and all(x["title"].startswith("L1 desk reference") for x in kbx)
          and any("No Service" in (x.get("text") or "") for x in kbx) and not any("invoice" in (x.get("text") or "").lower() for x in kbx)
          and sc.get("prompt_version") == 8, [(x.get("title"), x.get("seq"), x.get("score")) for x in kbx])
    kt = open(os.path.join(ROOT, "fixtures", "demo_kb.md"), encoding="utf-8").read()
    kch = kb_mod.chunk(kt)
    kidx = kb_mod.Index([{"document_id": "d", "title": "T", "seq": i, "text": x} for i, x in enumerate(kch)])
    hit_phone = kidx.search("the front desk phone shows No Service, the screen is lit, the cable goes into the wall jack, it registered after thirty seconds")
    hit_bill = kidx.search("I have a question about my invoice, there is a billing line I dispute")
    check("kb maths: paragraph chunks with overlap; the phone fault ranks the procedure first; a billing question finds the billing section; nothing for an unrelated text",
          3 <= len(kch) <= 6 and hit_phone and "No Service" in hit_phone[0]["text"] and hit_bill and "invoice" in hit_bill[0]["text"].lower()
          and kidx.search("the weather in Paris is lovely in spring and the croissants are excellent") == []
          and kb_mod.tokens("The phones were rebooting; cables clicked") == ["phone", "reboot", "cable", "click"], (len(kch), [h["seq"] for h in hit_phone], [h["seq"] for h in hit_bill]))
    check("kb fingerprint: '' with no documents, stable otherwise", kb_mod.fingerprint([]) == "" and kb_mod.fingerprint([{"id": "a", "updated_at": "t"}]) == kb_mod.fingerprint([{"id": "a", "updated_at": "t"}]) != "")
    sys_p, usr_p = score_mod.build_prompt("R", 1, [{"key": "k", "name": "N", "weight": 100, "critical": 0, "description": "", "guidance_met": "", "guidance_partial": "", "guidance_missed": ""}], "[00:00] S0: hi", True,
                                          kb_excerpts=[{"title": "Doc", "seq": 0, "text": "Reseat the cable."}])
    _, usr_none = score_mod.build_prompt("R", 1, [{"key": "k", "name": "N", "weight": 100, "critical": 0, "description": "", "guidance_met": "", "guidance_partial": "", "guidance_missed": ""}], "[00:00] S0: hi", True)
    check("prompt: rule 13 in SYSTEM; the excerpt block sits before the transcript only when excerpts are given",
          "13. KNOWLEDGE BASE EXCERPTS" in sys_p and "KNOWLEDGE BASE EXCERPTS" in usr_p and usr_p.index("[Doc §1]") < usr_p.index("TRANSCRIPT:")
          and "KNOWLEDGE BASE" not in usr_none)
    check("transcript utterances included", len(sc["transcript"]["utterances"]) == 19)
    cf = sc.get("call_facts") or {}
    check("scorecard carries the call facts (0.61.0): extension 101 as heard, no phone / MAC / ticket invented, steps keyed to criteria, an outcome",
          (cf.get("identifiers") or {}).get("extension") == "101" and cf["identifiers"].get("phone") is None and cf["identifiers"].get("mac_address") is None
          and cf["identifiers"].get("ticket") is None and len(cf.get("troubleshooting") or []) == 4
          and all(st.get("key") in ("issue_discovery", "troubleshooting", "resolution") for st in cf["troubleshooting"])
          and any(d.get("key") == "closing" for d in cf.get("discussed") or []) and "extension 101" in (cf.get("outcome") or ""), cf)
    check("recording exposes phone and ticket number (0.61.0), empty on the demo call", "caller_phone" in sc["recording"] and "ticket_ref" in sc["recording"]
          and sc["recording"]["ticket_ref"] is None)
    check("audio url present", (sc["recording"] or {}).get("audio_url"))

    # ── tone & delivery: timings + per-line sentiment (demo fixture carries it) ──
    import tone as tone_mod
    import transcribe as transcribe_mod
    tn = sc.get("tone") or {}
    check("scorecard carries tone & delivery from the timings",
          tn.get("agent_turns", 0) > 0 and tn.get("caller_turns", 0) > 0 and 0 < (tn.get("talk_ratio_agent") or 0) < 1
          and tn.get("agent_wpm") and tn.get("longest_silence_s") is not None and tn.get("agent_response_latency_s") is not None, tn)
    check("per-line sentiment reaches the browser and the caller trend agrees with the scorer",
          sc["transcript"]["utterances"][1].get("sentiment") == "negative" and tn.get("caller_trend") == "improving"
          and tn.get("sentiment_lines") == 19 and sc.get("tone_agrees") is True, (sc["transcript"]["utterances"][1], tn.get("caller_trend"), sc.get("tone_agrees")))
    tu = [{"start_s": 0, "end_s": 4, "speaker": 0, "text": "one two three four five six"}, {"start_s": 4.5, "end_s": 9, "speaker": 1, "text": "a b c", "sentiment_score": -0.6},
          {"start_s": 9.2, "end_s": 15, "speaker": 0, "text": "w " * 12}, {"start_s": 14.5, "end_s": 18, "speaker": 1, "text": "ok", "sentiment_score": 0.1},
          {"start_s": 25, "end_s": 30, "speaker": 0, "text": "x y z"}, {"start_s": 30.2, "end_s": 33, "speaker": 1, "text": "bye", "sentiment_score": 0.7}]
    tt = tone_mod.compute(tu, 0, [{"speaker": 0, "role": "agent"}, {"speaker": 1, "role": "caller"}])
    check("tone maths: talk share, one caller interruption, 7 s silence, median latency, pace, improving trend",
          tt["talk_ratio_agent"] == 0.578 and tt["interruptions_by_caller"] == 1 and tt["interruptions_by_agent"] == 0
          and tt["longest_silence_s"] == 7.0 and tt["silences_over_5s"] == 1 and tt["agent_response_latency_s"] == 3.6
          and tt["agent_wpm"] == 85.0 and tt["caller_trend"] == "improving" and tt["caller_sentiment_curve"] == [-0.6, 0.1, 0.7], tt)
    check("tone: too short, not diarized, or no agent -> None",
          tone_mod.compute(tu[:3], 0, None) is None and tone_mod.compute([dict(u, speaker=None) for u in tu], 0, None) is None
          and tone_mod.compute(tu, None, None) is None)
    check("tone: agent-speaker swap flips the talk share; voice_ai is never the caller",
          abs(tone_mod.compute(tu, 1, [{"speaker": 0, "role": "agent"}, {"speaker": 1, "role": "caller"}])["talk_ratio_agent"] + tt["talk_ratio_agent"] - 1) < 0.01
          and tone_mod.compute(tu, 1, [{"speaker": 0, "role": "voice_ai"}, {"speaker": 1, "role": "agent"}]) is None)
    check("tone: per-line trend vs the scorer's verdict", tone_mod.trend_agrees(tt, {"start": "negative", "end": "positive"}) is True
          and tone_mod.trend_agrees(tt, {"start": "positive", "end": "negative"}) is False and tone_mod.trend_agrees(None, {}) is None)
    # Deepgram sentiment segments (start_word/end_word into the word list) mapped onto utterances by time overlap
    words = [{"word": "w%d" % i, "start": i * 1.0, "end": i * 1.0 + 0.9} for i in range(10)]
    segs = [{"text": "a", "start_word": 0, "end_word": 4, "sentiment": "negative", "sentiment_score": -0.5},
            {"text": "b", "start_word": 5, "end_word": 9, "sentiment": "positive", "sentiment_score": 0.6}]
    mu = [{"start_s": 0.0, "end_s": 4.9, "speaker": 0, "text": "a"}, {"start_s": 5.0, "end_s": 9.9, "speaker": 1, "text": "b"},
          {"start_s": 4.0, "end_s": 6.0, "speaker": 0, "text": "c"}, {"start_s": 50.0, "end_s": 51.0, "speaker": 1, "text": "d"}]
    transcribe_mod.map_sentiments(words, segs, mu)
    check("Deepgram sentiment mapper: label of the covering segment, overlap-weighted score, untouched when no overlap",
          mu[0]["sentiment"] == "negative" and mu[0]["sentiment_score"] == -0.5 and mu[1]["sentiment"] == "positive" and mu[1]["sentiment_score"] == 0.6
          and mu[2]["sentiment"] in ("negative", "positive") and -0.5 < mu[2]["sentiment_score"] < 0.6 and "sentiment" not in mu[3], mu)
    check("Deepgram sentiment mapper: bad indexes or empty lists are a no-op",
          transcribe_mod.map_sentiments(words, [{"start_word": 40, "end_word": 50}], [dict(mu[3])])[0].get("sentiment") is None
          and transcribe_mod.map_sentiments([], segs, [dict(mu[3])])[0].get("sentiment") is None)
    check("Deepgram asks for sentiment only when AUDITLY_STT_SENTIMENT=1 (default off)",
          core.DEFAULTS.get("AUDITLY_STT_SENTIMENT") == "0" and 'params["sentiment"] = "true"' in open(os.path.join(ROOT, "transcribe.py"), encoding="utf-8").read())

    # ── override ──────────────────────────────────────────────────────
    crit = [i for i in sc["items"] if i["key"] == "troubleshooting"][0]
    s, sc2 = c.json("/api/scorecards/%s/items/%s/override" % (sc["id"], crit["criterion_id"]), "POST",
                    {"score": 10, "note": "PoE not checked"})
    check("override accepted", s == 200 and sc2["overall_pct"] == 80.0, sc2.get("overall_pct"))
    it2 = [i for i in sc2["items"] if i["key"] == "troubleshooting"][0]
    check("override recorded with author", it2["final_score"] == 10 and it2["override_by"] == "demo@example.com")
    s, e = c.json("/api/scorecards/%s/items/%s/override" % (sc["id"], crit["criterion_id"]), "POST",
                  {"score": 25, "note": "x"})
    check("override above weight rejected", s == 400)
    s, e = c.json("/api/scorecards/%s/items/%s/override" % (sc["id"], crit["criterion_id"]), "POST",
                  {"score": 3, "note": ""})
    check("override without note rejected", s == 400)
    s, sc3 = c.json("/api/scorecards/%s/items/%s/override" % (sc["id"], crit["criterion_id"]), "POST",
                    {"score": None})
    check("override cleared restores 90.0", s == 200 and sc3["overall_pct"] == 90.0)
    ver_it = [i for i in sc["items"] if i["key"] == "verification"][0]
    s, a1 = c.json("/api/scorecards/%s/items/%s/override" % (sc["id"], ver_it["criterion_id"]), "POST", {"score": 0, "note": "no verification attempted at all"})
    check("override of a critical item to 0 sets auto-fail", s == 200 and a1["auto_fail"] is True and a1["overall_pct"] == 85.0, (a1.get("auto_fail"), a1.get("overall_pct")))
    s, a2 = c.json("/api/scorecards/%s/items/%s/override" % (sc["id"], ver_it["criterion_id"]), "POST", {"score": 5, "note": "asked name and company"})
    check("override above 0 clears auto-fail", s == 200 and a2["auto_fail"] is False)
    s, a3 = c.json("/api/scorecards/%s/items/%s/override" % (sc["id"], ver_it["criterion_id"]), "POST", {"score": None})
    check("clearing the override restores the model's auto-fail state", s == 200 and a3["auto_fail"] is False and a3["overall_pct"] == 90.0)
    # an 'na' criterion has no score to override and cannot fail the call (0.14.8)
    dbo = core.connect(env["AUDITLY_DB"])
    dbo.execute("UPDATE scorecard_item SET rating='na', score=0 WHERE scorecard_id=? AND criterion_id=?", (sc["id"], ver_it["criterion_id"]))
    dbo.commit()
    s, e = c.json("/api/scorecards/%s/items/%s/override" % (sc["id"], ver_it["criterion_id"]), "POST", {"score": 0, "note": "n/a"})
    check("override on a not-applicable item refused (400)", s == 400 and "not applicable" in e.get("error", ""), (s, e))
    s, na1 = c.json("/api/scorecards/%s/items/%s/override" % (sc["id"], crit["criterion_id"]), "POST", {"score": 10, "note": "PoE not checked"})
    check("recompute with a critical 'na' item: excluded from the basis, no auto-fail",
          s == 200 and na1["auto_fail"] is False and na1["applicable_weight"] == 100 - ver_it["weight"], (na1.get("auto_fail"), na1.get("applicable_weight")))
    dbo.execute("UPDATE scorecard_item SET rating=?, score=? WHERE scorecard_id=? AND criterion_id=?",
                (ver_it["rating"], ver_it["score"], sc["id"], ver_it["criterion_id"]))
    dbo.commit(); dbo.close()
    s, na2 = c.json("/api/scorecards/%s/items/%s/override" % (sc["id"], crit["criterion_id"]), "POST", {"score": None})
    check("restored: 90.0 on a 100 basis", s == 200 and na2["overall_pct"] == 90.0 and na2["applicable_weight"] == 100, (na2.get("overall_pct"), na2.get("applicable_weight")))
    s, sc4 = c.json("/api/scorecards/%s/agent-speaker" % sc["id"], "POST", {"speaker": 1})
    check("agent speaker swap", s == 200 and sc4["agent_speaker"] == 1)
    check("tone follows the agent-speaker swap", (sc4.get("tone") or {}).get("talk_ratio_agent") is not None
          and abs(sc4["tone"]["talk_ratio_agent"] + sc["tone"]["talk_ratio_agent"] - 1) < 0.01, (sc4.get("tone") or {}).get("talk_ratio_agent"))
    c.json("/api/scorecards/%s/agent-speaker" % sc["id"], "POST", {"speaker": 0})
    s, sp1 = c.json("/api/scorecards/%s/speakers" % sc["id"], "POST",
                    {"speakers": [{"speaker": 0, "role": "voice_ai", "name": "Emma"}, {"speaker": 1, "role": "agent", "name": None}]})
    check("speaker roles saved; the single agent role becomes agent_speaker", s == 200 and sp1["agent_speaker"] == 1
          and sp1["speakers"][0] == {"speaker": 0, "role": "voice_ai", "name": "Emma"}, (s, sp1.get("agent_speaker"), sp1.get("speakers")))
    s, e = c.json("/api/scorecards/%s/speakers" % sc["id"], "POST", {"speakers": [{"speaker": 0, "role": "boss", "name": None}]})
    check("unknown speaker role 400", s == 400 and "role" in e.get("error", ""), e)
    s, sp2 = c.json("/api/scorecards/%s/speakers" % sc["id"], "POST",
                    {"speakers": [{"speaker": 0, "role": "agent", "name": "Jordan"}, {"speaker": 1, "role": "caller", "name": "Maria"}]})
    check("speaker roles restored", s == 200 and sp2["agent_speaker"] == 0)
    check("tone: no caller left (voice AI + agent) -> none; restored roles bring it back",
          sp1.get("tone") is None and (sp2.get("tone") or {}).get("talk_ratio_agent") == sc["tone"]["talk_ratio_agent"], (sp1.get("tone"), (sp2.get("tone") or {}).get("talk_ratio_agent")))

    # ── exports ───────────────────────────────────────────────────────
    check("scorecard carries a call summary", bool(sc.get("call_summary")) and "Harbor Dental" in sc["call_summary"], sc.get("call_summary"))
    s, b, hd = c.raw("/api/scorecards/%s/export.pdf" % sc["id"])
    check("PDF export", s == 200 and b.startswith(b"%PDF") and "attachment" in hd.get("Content-Disposition", ""))
    check("PDF carries the Auditly masthead, producer and file name", b"AUDITLY" in b and b"/Producer (Auditly)" in b
          and "auditly-scorecard-" in hd.get("Content-Disposition", "") and b"L1 SUPPORT QA" not in b, hd.get("Content-Disposition"))
    s, b, hd = c.raw("/api/scorecards/%s/export.csv" % sc["id"])
    check("CSV export: call metadata block, then header + 8 rows", s == 200 and b.startswith(b"Audit name,")
          and b"\r\nCriterion," in b and b.decode().count("\n") >= 20)
    check("CSV metadata has customer email and call summary", b"Customer email," in b and b"Call summary," in b and b"Harbor Dental" in b)
    check("CSV carries the ticket number row and the facts as heard (0.61.0)", b"Ticket number," in b and b"Fact: Extension,101" in b and b"Troubleshooting," in b and b"Customer reference" not in b)
    check("CSV names the QA type and the speakers", b"QA type,L1 Support v1 (demo) v1" in b and b'Speakers,"S0 agent (Jordan), S1 caller (Maria)"' in b, b[:600])
    s, b, hd = c.raw("/api/scorecards/%s/export.json" % sc["id"])
    check("JSON export", s == 200 and json.loads(b)["overall_pct"] == 90.0)

    # ── reviews (the Audit tab) ───────────────────────────────────────
    check("scorecard carries sentiment and labelled call reasons",
          (sc.get("sentiment") or {}).get("overall") == "positive" and (sc.get("sentiment") or {}).get("start") == "negative"
          and [x["category"] for x in sc.get("call_reasons") or []] == ["no_service", "hardware"]
          and sc["call_reasons"][0]["label"] == "No service / phone offline", (sc.get("sentiment"), sc.get("call_reasons")))
    check("scorecard has no review yet", sc.get("review") is None and sc.get("stage") == "scored")
    s, jj = c.json("/api/jobs/" + demo["id"])
    check("job carries stage scored", s == 200 and jj.get("stage") == "scored" and jj.get("review_status") is None and jj.get("final_pct") is None, jj.get("stage"))
    s, _ = c.json("/api/scorecards/%s/review/unqueue" % sc["id"], "POST", {})
    check("unqueue of a call not in the queue is 409", s == 409)
    s, qd = c.json("/api/scorecards/%s/review/queue" % sc["id"], "POST", {})
    check("send to audit uses the recording's QA of record", s == 200 and (qd.get("review") or {}).get("status") == "queued" and qd.get("stage") == "queued", qd.get("review"))
    s, qd2 = c.json("/api/scorecards/%s/review/queue" % sc["id"], "POST", {})
    check("sending again is a no-op 200", s == 200 and (qd2.get("review") or {}).get("status") == "queued")
    s, jj = c.json("/api/jobs/" + demo["id"])
    check("job stage follows the queue", jj.get("stage") == "queued" and jj.get("review_status") == "queued")
    s, t1 = c.json("/api/reviews?kind=demo&status=todo")
    s2, t2 = c.json("/api/reviews?kind=demo&status=queued")
    s3, t3 = c.json("/api/reviews?kind=demo&status=none")
    check("queue filters: todo and queued list it, none does not", t1["total"] == 1 and t2["total"] == 1 and t3["total"] == 0 and t1["reviews"][0]["stage"] == "queued", (t1["total"], t2["total"], t3["total"]))
    s4, t4 = c.json("/api/reviews?kind=demo&status=done")
    check("sub-tab counts: to audit 1, completed 0; done lists nothing yet", t1.get("counts") == {"todo": 1, "done": 0} and t4["total"] == 0, (t1.get("counts"), t4["total"]))
    s, uq = c.json("/api/scorecards/%s/review/unqueue" % sc["id"], "POST", {})
    check("remove from queue", s == 200 and uq.get("review") is None and uq.get("stage") == "scored")
    s, bk = c.json("/api/reviews/queue", "POST", {"scorecard_ids": [sc["id"], "not-an-id"]})
    check("bulk send: one queued, one skipped with a reason", s == 200 and bk.get("queued") == [sc["id"]] and len(bk.get("skipped") or []) == 1 and bk["skipped"][0]["reason"] == "not a scorecard id", bk)
    s, bk2 = c.json("/api/reviews/queue", "POST", {"scorecard_ids": [sc["id"]]})
    check("bulk send skips calls already in audit", s == 200 and bk2["queued"] == [] and bk2["skipped"][0]["reason"] == "already in audit")
    s, _ = c.json("/api/reviews/queue", "POST", {"scorecard_ids": []})
    check("bulk send with nothing is 400", s == 400)
    c.json("/api/scorecards/%s/review/unqueue" % sc["id"], "POST", {})
    s, q0 = c.json("/api/reviews?kind=demo")
    check("audit list hides a call with no audit started (no status = to audit)", s == 200 and q0["total"] == 0 and q0.get("counts") == {"todo": 0, "done": 0}, q0)
    s, q1 = c.json("/api/reviews?kind=demo&status=none")
    check("queue filter status=none (API only) lists the never-started demo call", s == 200 and q1["total"] == 1 and q1["reviews"][0]["review"] is None
          and q1["reviews"][0]["id"] == sc["id"] and q1["reviews"][0]["recording"]["call_ref"] == "DEMO-0001", q1)
    s, q2 = c.json("/api/reviews?kind=demo&status=submitted")
    check("queue filter status=submitted empty", s == 200 and q2["total"] == 0)
    s, _ = c.json("/api/reviews")
    check("queue defaults to real calls (none yet)", s == 200 and _["total"] == 0, _)
    s, e = c.json("/api/scorecards/%s/review" % sc["id"], "POST", {"coaching_notes": "x", "follow_up_on": "bad"})
    check("bad follow-up date rejected", s == 400)
    s, e = c.json("/api/scorecards/%s/review" % sc["id"], "POST", {"qa_name": "Nobody Here"})
    check("unknown QA name rejected", s == 400)
    s, r1 = c.json("/api/scorecards/%s/review" % sc["id"], "POST",
                   {"coaching_notes": "Verify before noting.", "resolution_notes": "Cable reseated.",
                    "action_plan": "Practise verification.", "follow_up_on": "2026-09-30", "qa_name": "demo qa"})
    rv = r1.get("review") or {}
    check("draft review saved and echoed on the scorecard", s == 200 and rv.get("status") == "draft"
          and rv.get("coaching_notes") == "Verify before noting." and rv.get("follow_up_on") == "2026-09-30"
          and rv.get("final_pct") is None and rv.get("reviewer_email") == "demo@example.com", rv)
    check("QA of record written back with canonical spelling", r1["recording"]["qa_name"] == "Demo QA")
    check("saving a draft puts the call in review", r1.get("stage") == "in_review")
    s, _ = c.json("/api/scorecards/%s/review/unqueue" % sc["id"], "POST", {})
    check("a draft cannot be removed from the queue (409)", s == 409)
    s, g = c.json("/api/scorecards/%s/review" % sc["id"])
    check("GET review", s == 200 and (g.get("review") or {}).get("status") == "draft")
    s, q3 = c.json("/api/reviews?kind=demo&status=draft")
    check("queue filter status=draft", s == 200 and q3["total"] == 1)
    s, r2 = c.json("/api/scorecards/%s/review/submit" % sc["id"], "POST", {"action_plan": "Practise verification on 5 calls."})
    rv = r2.get("review") or {}
    check("submit freezes the final score (90.0) and keeps edits sent with it",
          s == 200 and rv.get("status") == "submitted" and rv.get("final_pct") == 90.0 and rv.get("applicable_weight") == 100
          and rv.get("auto_fail") is False and rv.get("submitted_at") and rv.get("action_plan") == "Practise verification on 5 calls.", rv)
    s, _ = c.json("/api/scorecards/%s/review/submit" % sc["id"], "POST", {})
    check("second submit is 409", s == 409)
    s, lk = c.json("/api/recordings/lookup?call_ref=demo-0001")
    check("lookup by call ID (case-insensitive) finds the call and says it is audited",
          s == 200 and len(lk.get("matches") or []) == 1 and lk["matches"][0]["match"] == "call_id" and lk["matches"][0]["audited"] is True
          and lk["matches"][0]["job_id"] == demo["id"], lk)
    check("submitted stage is audited", r2.get("stage") == "audited")
    s, qa1 = c.json("/api/reviews?kind=demo&status=done"); s, qa2 = c.json("/api/reviews?kind=demo&status=audited")
    s, qa3 = c.json("/api/reviews?kind=demo&status=coached"); s, qa4 = c.json("/api/reviews?kind=demo")
    check("completed sub-tab: done and audited list it, coached and to-audit do not; counts follow",
          qa1["total"] == 1 and qa2["total"] == 1 and qa3["total"] == 0 and qa4["total"] == 0 and qa1.get("counts") == {"todo": 0, "done": 1},
          (qa1["total"], qa2["total"], qa3["total"], qa4["total"], qa1.get("counts")))
    s, _ = c.json("/api/scorecards/%s/items/%s/override" % (sc["id"], crit["criterion_id"]), "POST", {"score": 10, "note": "late"})
    check("override refused while submitted (409)", s == 409)
    s, _ = c.json("/api/scorecards/%s/agent-speaker" % sc["id"], "POST", {"speaker": 1})
    check("speaker swap refused while submitted (409)", s == 409)
    s, _ = c.json("/api/scorecards/%s/review" % sc["id"], "POST", {"coaching_notes": "late"})
    check("editing notes refused while submitted (409)", s == 409)
    s, r3 = c.json("/api/scorecards/%s/review/coached" % sc["id"], "POST", {"coached": True})
    check("coaching can be marked delivered after submit", s == 200 and (r3.get("review") or {}).get("coached_at") and r3.get("stage") == "coached")
    # marking coaching delivered must not re-credit a submitted audit to whoever ticked the box (0.14.9)
    dbc = core.connect(env["AUDITLY_DB"])
    dbc.execute("UPDATE review SET reviewer_email='qa-a@example.com' WHERE scorecard_id=?", (sc["id"],)); dbc.commit()
    c.json("/api/scorecards/%s/review/coached" % sc["id"], "POST", {"coached": False})
    s, r3b = c.json("/api/scorecards/%s/review/coached" % sc["id"], "POST", {"coached": True})
    check("coaching mark keeps the submitter as the audit's author", s == 200 and (r3b.get("review") or {}).get("reviewer_email") == "qa-a@example.com",
          (r3b.get("review") or {}).get("reviewer_email"))
    dbc.execute("UPDATE review SET reviewer_email='demo@example.com' WHERE scorecard_id=?", (sc["id"],)); dbc.commit(); dbc.close()
    s, qc1 = c.json("/api/reviews?kind=demo&status=coached"); s, qc2 = c.json("/api/reviews?kind=demo&status=audited")
    check("coached filter follows the coaching mark", qc1["total"] == 1 and qc2["total"] == 0, (qc1["total"], qc2["total"]))
    s, _ = c.json("/api/scorecards/%s/review/queue" % sc["id"], "POST", {})
    check("queueing a call with a submitted audit is a no-op 200 on the same scorecard", s == 200)
    s, r4 = c.json("/api/scorecards/%s/review/reopen" % sc["id"], "POST", {})
    rv = r4.get("review") or {}
    check("reopen clears the snapshot, keeps coaching", s == 200 and rv.get("status") == "draft" and rv.get("final_pct") is None
          and rv.get("submitted_at") is None and rv.get("coached_at"), rv)
    s, _ = c.json("/api/scorecards/%s/review/reopen" % sc["id"], "POST", {})
    check("reopen of a draft is 409", s == 409)
    s, o = c.json("/api/scorecards/%s/items/%s/override" % (sc["id"], crit["criterion_id"]), "POST", {"score": 10, "note": "PoE not checked"})
    check("override allowed again after reopen", s == 200 and o["overall_pct"] == 80.0)
    s, r5 = c.json("/api/scorecards/%s/review/submit" % sc["id"], "POST", {})
    check("resubmit snapshots the overridden score (80.0)", s == 200 and (r5.get("review") or {}).get("final_pct") == 80.0, r5.get("review"))
    c.json("/api/scorecards/%s/review/reopen" % sc["id"], "POST", {})
    c.json("/api/scorecards/%s/items/%s/override" % (sc["id"], crit["criterion_id"]), "POST", {"score": None})
    s, r6 = c.json("/api/scorecards/%s/review/submit" % sc["id"], "POST", {})
    check("final review submitted at 90.0", s == 200 and (r6.get("review") or {}).get("final_pct") == 90.0)
    s, q4 = c.json("/api/reviews?kind=demo&status=submitted")
    check("queue shows it submitted", s == 200 and q4["total"] == 1 and q4["reviews"][0]["review"]["final_pct"] == 90.0)
    s, r7 = c.json("/api/scorecards/%s/review/coached" % sc["id"], "POST", {"coached": False})
    check("coaching can be un-marked", s == 200 and (r7.get("review") or {}).get("coached_at") is None)
    c.json("/api/scorecards/%s/review/coached" % sc["id"], "POST", {"coached": True})

    # ── disputes: recorded by the QA for the agent, resolved by a reviewer ─
    s, _ = c.json("/api/scorecards/%s/disputes" % sc["id"], "POST", {"reason": ""})
    check("dispute without a reason is 400", s == 400)
    s, _ = c.json("/api/scorecards/%s/disputes" % sc["id"], "POST", {"reason": "x", "criterion_id": "00000000-0000-0000-0000-000000000000"})
    check("dispute on a criterion not on the card is 404", s == 404)
    s, d1 = c.json("/api/scorecards/%s/disputes" % sc["id"], "POST", {"reason": "I did check the PoE budget at 01:40.", "criterion_id": crit["criterion_id"]})
    dd = (d1.get("disputes") or [{}])[0]
    check("dispute recorded on a submitted audit (201) with the agent, criterion and who recorded it",
          s == 201 and d1.get("open_disputes") == 1 and dd.get("status") == "open" and dd.get("agent_name") == "Jordan"
          and dd.get("criterion_name") == crit["name"] and dd.get("raised_by") == "demo@example.com", d1.get("disputes"))
    did = dd.get("id")
    s, _ = c.json("/api/scorecards/%s/disputes" % sc["id"], "POST", {"reason": "again", "criterion_id": crit["criterion_id"]})
    check("second open dispute on the same criterion is 409", s == 409)
    s, d2 = c.json("/api/scorecards/%s/disputes" % sc["id"], "POST", {"reason": "The whole score feels harsh."})
    check("a whole-scorecard dispute sits beside the criterion one", s == 201 and d2.get("open_disputes") == 2)
    did_all = [d for d in d2["disputes"] if not d["criterion_id"]][0]["id"]
    s, dl = c.json("/api/scorecards/%s/disputes" % sc["id"])
    check("GET disputes lists both", s == 200 and len(dl.get("disputes") or []) == 2)
    s, dj = c.json("/api/jobs/%s" % demo["id"])
    check("job row carries open_disputes", s == 200 and dj.get("open_disputes") == 2, dj.get("open_disputes"))
    s, dq = c.json("/api/reviews?kind=demo&status=disputed")
    check("Audit › Completed 'disputed' filter lists it with its open count", s == 200 and dq["total"] == 1 and dq["reviews"][0].get("open_disputes") == 2, dq.get("total"))
    s, dg = c.json("/api/disputes?kind=demo&agent=jordan")
    check("/api/disputes lists the agent's open ones with the audit name and counts", s == 200 and dg["total"] == 2 and dg["counts"]["open"] == 2
          and dg["disputes"][0].get("audit_name"), (dg.get("total"), dg.get("counts")))
    s, _ = c.json("/api/disputes/%s/resolve" % did, "POST", {"status": "upheld", "note": ""})
    check("resolving without a note is 400", s == 400)
    s, _ = c.json("/api/disputes/%s/resolve" % did, "POST", {"status": "maybe", "note": "x"})
    check("unknown resolution status is 400", s == 400)
    s, _ = c.json("/api/disputes/%s/resolve" % did_all, "POST", {"status": "upheld", "note": "x", "score": 10})
    check("a score on a whole-scorecard dispute is 400", s == 400)
    s, _ = c.json("/api/disputes/%s/resolve" % did, "POST", {"status": "upheld", "note": "PoE was checked", "score": 15})
    s2, dl = c.json("/api/scorecards/%s/disputes" % sc["id"])
    check("uphold with a score on a submitted audit is 409 and the dispute stays open",
          s == 409 and [d["status"] for d in dl["disputes"] if d["id"] == did] == ["open"])
    c.json("/api/scorecards/%s/review/reopen" % sc["id"], "POST", {})
    s, d3 = c.json("/api/disputes/%s/resolve" % did, "POST", {"status": "upheld", "note": "PoE was checked at 01:40", "score": 15})
    it3 = [i for i in d3.get("items") or [] if i["key"] == "troubleshooting"]
    it3 = it3[0] if it3 else {}
    check("after reopen, uphold applies the override through the normal path and recomputes",
          s == 200 and it3.get("final_score") == 15 and (it3.get("override_note") or "").startswith("Dispute upheld: ")
          and d3.get("overall_pct") == 85.0 and d3.get("open_disputes") == 1, (it3.get("final_score"), it3.get("override_note"), d3.get("overall_pct")))
    s, _ = c.json("/api/disputes/%s/resolve" % did, "POST", {"status": "rejected", "note": "x"})
    check("resolving an already-resolved dispute is 409", s == 409)
    s, d4 = c.json("/api/disputes/%s/withdraw" % did_all, "POST", {})
    check("withdraw closes the whole-scorecard dispute", s == 200 and d4.get("open_disputes") == 0
          and [d["status"] for d in d4["disputes"] if d["id"] == did_all] == ["withdrawn"])
    s, dg2 = c.json("/api/disputes?kind=demo&status=all")
    check("/api/disputes status=all shows upheld and withdrawn with counts", s == 200 and dg2["total"] == 2
          and dg2["counts"] == {"open": 0, "upheld": 1, "rejected": 0, "withdrawn": 1}, dg2.get("counts"))
    s, dg3 = c.json("/api/disputes?kind=demo")
    check("/api/disputes defaults to open ones", s == 200 and dg3["total"] == 0)
    # back to the state the checks below expect: no override, submitted at 90.0, coached
    c.json("/api/scorecards/%s/items/%s/override" % (sc["id"], crit["criterion_id"]), "POST", {"score": None})
    s, r8 = c.json("/api/scorecards/%s/review/submit" % sc["id"], "POST", {})
    check("resubmitted at 90.0 after the dispute", s == 200 and (r8.get("review") or {}).get("final_pct") == 90.0)

    # ── coaching sessions: group, schedule (.ics), deliver ──────────────
    import coaching as coaching_mod
    c.json("/api/scorecards/%s/review/coached" % sc["id"], "POST", {"coached": False})      # so the call is waiting for coaching
    s, ca = c.json("/api/coaching/agents?kind=demo")
    ja = [a for a in ca.get("agents") or [] if a["name"] == "Jordan"]
    check("coaching agents: Jordan has one audited call to coach, no open disputes, no session",
          s == 200 and len(ja) == 1 and ja[0]["to_coach"] == 1 and ja[0]["audited"] == 1 and ja[0]["open_disputes"] == 0 and ja[0]["sessions_open"] == 0, ja)
    s, _ = c.json("/api/coaching/candidates?kind=demo")
    check("candidates need an agent (400)", s == 400)
    s, cand = c.json("/api/coaching/candidates?agent=jordan&kind=demo")
    check("candidates: the audited, un-coached call, not in a session, with its misses and final score",
          s == 200 and len(cand.get("calls") or []) == 1 and cand["calls"][0]["scorecard_id"] == sc["id"] and cand["calls"][0]["in_session"] is None
          and cand["calls"][0]["final_pct"] == 90.0 and len(cand["calls"][0]["misses"]) == 2, cand)
    s, _ = c.json("/api/coaching/sessions", "POST", {"agent_name": "Nobody Here", "scorecard_ids": []})
    check("session for an unknown agent is 400", s == 400)
    s, _ = c.json("/api/coaching/sessions", "POST", {"agent_name": "Priya", "scorecard_ids": [sc["id"]]})
    check("another agent's call cannot go into the session (400)", s == 400)
    s, cs1 = c.json("/api/coaching/sessions", "POST", {"agent_name": "jordan", "qa_name": "Demo QA", "title": "Verification", "scorecard_ids": [sc["id"]]})
    check("session created (201): canonical agent, planned, one call, agenda drafted from the review",
          s == 201 and cs1.get("agent_name") == "Jordan" and cs1.get("status") == "planned" and cs1.get("calls_count") == 1 and cs1.get("qa_name") == "Demo QA"
          and "Coaching session with Jordan" in (cs1.get("agenda") or "") and "90.0%" in cs1["agenda"] and "- Miss:" in cs1["agenda"]
          and "Verify before noting." in cs1["agenda"] and cs1.get("agenda_edited") is False, cs1)
    sid = cs1.get("id")
    s, _ = c.json("/api/coaching/sessions", "POST", {"agent_name": "Jordan", "scorecard_ids": [sc["id"]]})
    check("a call sits in at most one live session (409)", s == 409)
    s, cand2 = c.json("/api/coaching/candidates?agent=Jordan&kind=demo")
    check("candidates now name the session holding the call", s == 200 and cand2["calls"][0]["in_session"] == sid)
    s, ca2 = c.json("/api/coaching/agents?kind=demo")
    ja2 = [a for a in ca2["agents"] if a["name"] == "Jordan"][0]
    check("agents: to_coach drops to 0 once the call is in a session; one open session", ja2["to_coach"] == 0 and ja2["sessions_open"] == 1, ja2)
    s, sl = c.json("/api/coaching/sessions?agent=Jordan")
    check("sessions list (open by default) with counts", s == 200 and sl["total"] == 1 and sl["counts"]["planned"] == 1 and sl["sessions"][0]["id"] == sid, sl.get("counts"))
    s, cs2 = c.json("/api/coaching/sessions/%s" % sid, "POST", {"title": "T2", "agenda": "My agenda", "location": "Room 1", "notes": "n"})
    check("editing title/agenda/location/notes; the agenda counts as edited", s == 200 and cs2["title"] == "T2" and cs2["agenda"] == "My agenda"
          and cs2["location"] == "Room 1" and cs2["notes"] == "n" and cs2["agenda_edited"] is True, cs2)
    s, cs3 = c.json("/api/coaching/sessions/%s/calls" % sid, "POST", {"remove": [sc["id"]]})
    s2, cs4 = c.json("/api/coaching/sessions/%s/calls" % sid, "POST", {"add": [sc["id"]]})
    check("calls can be removed and added back; an edited agenda is left alone",
          s == 200 and cs3["calls_count"] == 0 and s2 == 200 and cs4["calls_count"] == 1 and cs4["agenda"] == "My agenda", (cs3.get("calls_count"), cs4.get("calls_count"), cs4.get("agenda")))
    s, _, _ = c.raw("/api/coaching/sessions/%s/export.ics" % sid)
    check("calendar file refused before a time is set (409)", s == 409)
    s, e = c.json("/api/coaching/sessions/%s/schedule" % sid, "POST", {"scheduled_at": "tomorrow-ish", "tz": 0})
    check("unreadable time is 400", s == 400 and "2026-09-22T14:30" in e.get("error", ""))
    s, e = c.json("/api/coaching/sessions/%s/schedule" % sid, "POST", {"scheduled_at": "2026-09-22T14:30", "tz": 0, "duration_min": 5})
    check("duration outside 15–180 min is 400", s == 400)
    s, cs5 = c.json("/api/coaching/sessions/%s/schedule" % sid, "POST", {"scheduled_at": "2026-09-22T14:30", "tz": 240, "duration_min": 45, "location": "Teams"})
    check("scheduled: browser-local 14:30 at UTC-4 is stored as 18:30 UTC, 45 min, location kept",
          s == 200 and cs5["status"] == "scheduled" and cs5["scheduled_at"] == "2026-09-22T18:30:00+00:00" and cs5["duration_min"] == 45 and cs5["location"] == "Teams", cs5)
    check("coaching.parse_when reads offsets and Z too", coaching_mod.iso(coaching_mod.parse_when("2026-09-22T14:30:00+02:00")) == "2026-09-22T12:30:00+00:00"
          and coaching_mod.iso(coaching_mod.parse_when("2026-09-22T14:30:00Z")) == "2026-09-22T14:30:00+00:00")
    s, cal = c.json("/api/coaching/calendar?from=2026-09-01&to=2026-09-30&tz=240")
    d22 = [d for d in cal.get("days") or [] if d["date"] == "2026-09-22"]
    check("calendar puts the session on its local day (UTC-4) and never carries the call list",
          s == 200 and len(d22) == 1 and d22[0]["sessions"][0]["id"] == sid and d22[0]["sessions"][0]["calls"] == [], cal)
    s, cal2 = c.json("/api/coaching/calendar?from=2026-09-01&to=2026-09-30&tz=-600")
    check("calendar shifts to the next local day at UTC+10", s == 200 and [d["date"] for d in cal2["days"] if d["sessions"]] == ["2026-09-23"], cal2.get("days"))
    s, _ = c.json("/api/coaching/calendar?from=2026-09-30&to=2026-09-01")
    check("calendar refuses a backwards range (400)", s == 400)
    s, fu = c.json("/api/coaching/calendar?from=2026-09-01&to=2026-11-15")
    check("calendar range over 62 days is 400", s == 400)
    s, ics, hd = c.raw("/api/coaching/sessions/%s/export.ics" % sid)
    check("ICS: text/calendar with one VEVENT at the UTC time, the session's UID and title",
          s == 200 and hd.get("Content-Type", "").startswith("text/calendar") and b"BEGIN:VEVENT" in ics and b"DTSTART:20260922T183000Z" in ics
          and b"DTEND:20260922T191500Z" in ics and ("UID:%s@auditly" % sid).encode() in ics and b"SUMMARY:T2" in ics and b"LOCATION:Teams" in ics
          and b"ATTENDEE" not in ics, (s, hd.get("Content-Type"), ics[:200]))
    s, pp = c.json("/api/people?kind=agent")
    jp = [p for p in pp.get("people") or [] if p["name"] == "Jordan"][0]
    s, _ = c.json("/api/people/%s/email" % jp["id"], "POST", {"email": "not-an-email"})
    check("agent email must look like one (400)", s == 400)
    s, em = c.json("/api/people/%s/email" % jp["id"], "POST", {"email": "jordan@example.com"})
    s2, pp2 = c.json("/api/people?kind=agent")
    check("agent email saved and listed", s == 200 and em.get("email") == "jordan@example.com"
          and [p for p in pp2["people"] if p["name"] == "Jordan"][0].get("email") == "jordan@example.com")
    s, ics2, _ = c.raw("/api/coaching/sessions/%s/export.ics" % sid)
    s2, cs6 = c.json("/api/coaching/sessions/%s" % sid)
    check("with an email on file the ICS names the agent as attendee and the session carries agent_email",
          b"ATTENDEE;CN=Jordan;ROLE=REQ-PARTICIPANT;RSVP=TRUE:mailto:jordan@example.com" in ics2 and cs6.get("agent_email") == "jordan@example.com")
    s, pdfb, hd = c.raw("/api/coaching/sessions/%s/export.pdf" % sid)
    check("coaching pack PDF", s == 200 and hd.get("Content-Type", "").startswith("application/pdf") and pdfb[:5] == b"%PDF-" and b"Coaching session: Jordan" in pdfb or (s == 200 and pdfb[:5] == b"%PDF-"))
    s, cs7 = c.json("/api/coaching/sessions", "POST", {"agent_name": "Priya", "qa_name": "Demo QA", "scorecard_ids": []})
    check("a session with no calls yet is allowed (201)", s == 201 and cs7["calls_count"] == 0 and "No calls selected yet" in (cs7.get("agenda") or ""))
    sid2 = cs7["id"]
    s, e = c.json("/api/coaching/sessions/%s/schedule" % sid2, "POST", {"scheduled_at": "2026-09-22T18:45:00Z", "duration_min": 30})
    check("overlapping the same QA's session is 409 with the conflicts listed", s == 409 and [x["id"] for x in e.get("conflicts") or []] == [sid], e)
    s, cs8 = c.json("/api/coaching/sessions/%s/schedule" % sid2, "POST", {"scheduled_at": "2026-09-22T18:45:00Z", "duration_min": 30, "force": True})
    check("... and force schedules it anyway", s == 200 and cs8["status"] == "scheduled")
    s, _ = c.json("/api/coaching/sessions/%s/done" % sid2, "POST", {})
    check("delivering a session with no calls is 400", s == 400)
    s, cs9 = c.json("/api/coaching/sessions/%s/cancel" % sid2, "POST", {})
    s2, _ = c.json("/api/coaching/sessions/%s/cancel" % sid2, "POST", {})
    check("cancel, then cancel again is 409", s == 200 and cs9["status"] == "cancelled" and cs9["cancelled_at"] and s2 == 409)
    s, _ = c.json("/api/coaching/sessions/%s/calls" % sid2, "POST", {"add": []})
    check("a cancelled session's calls cannot change (409)", s == 409)
    s, cs10 = c.json("/api/coaching/sessions/%s/unschedule" % sid, "POST", {})
    s2, cs11 = c.json("/api/coaching/sessions/%s/schedule" % sid, "POST", {"scheduled_at": "2026-09-22T18:30:00Z", "duration_min": 45})
    check("unschedule goes back to planned; schedule again", s == 200 and cs10["status"] == "planned" and cs10["scheduled_at"] is None and s2 == 200 and cs11["status"] == "scheduled")
    s, dn = c.json("/api/coaching/sessions/%s/done" % sid, "POST", {"notes": "Went well."})
    s2, scd = c.json("/api/scorecards/%s" % sc["id"])
    check("Mark delivered: session done, the call's review is coached as of done_at, stage coached, notes kept",
          s == 200 and dn["status"] == "done" and dn.get("coached") == 1 and dn.get("skipped") == [] and dn["notes"] == "Went well."
          and (scd.get("review") or {}).get("coached_at") == dn["done_at"] and scd.get("stage") == "coached", (dn.get("coached"), dn.get("skipped"), (scd.get("review") or {}).get("coached_at"), dn.get("done_at")))
    s, _ = c.json("/api/coaching/sessions/%s/calls" % sid, "POST", {"remove": [sc["id"]]})
    s2, _ = c.json("/api/coaching/sessions/%s/cancel" % sid, "POST", {})
    check("a delivered session's calls cannot change and it cannot be cancelled (409, 409)", s == 409 and s2 == 409)
    s, ro = c.json("/api/coaching/sessions/%s/reopen" % sid, "POST", {})
    s2, scr = c.json("/api/scorecards/%s" % sc["id"])
    check("Reopen undoes exactly this session's coached marks and returns to scheduled",
          s == 200 and ro["status"] == "scheduled" and ro["done_at"] is None and (scr.get("review") or {}).get("coached_at") is None, (ro.get("status"), (scr.get("review") or {}).get("coached_at")))
    s, dn2 = c.json("/api/coaching/sessions/%s/done" % sid, "POST", {})
    s2, sl2 = c.json("/api/coaching/sessions?status=done")
    s3, ca3 = c.json("/api/coaching/agents?kind=demo")
    ja3 = [a for a in ca3["agents"] if a["name"] == "Jordan"][0]
    check("delivered again: listed under done; agent shows last coached and nothing to coach",
          s == 200 and dn2["status"] == "done" and sl2["total"] == 1 and ja3["to_coach"] == 0 and ja3["last_coached_at"] == dn2["done_at"] and ja3["sessions_open"] == 0, ja3)
    # ── selecting calls: scope switch and the tally (0.38.0) ──────────
    s, cw = c.json("/api/coaching/candidates?agent=Jordan&kind=demo")
    s2, cau = c.json("/api/coaching/candidates?agent=Jordan&kind=demo&scope=audited")
    s3, call_ = c.json("/api/coaching/candidates?agent=Jordan&kind=demo&scope=all")
    s4, _ = c.json("/api/coaching/candidates?agent=Jordan&kind=demo&scope=everything")
    check("candidates: default scope hides the coached call; 'audited' shows it (in its session); 'all' is a superset; bad scope 400",
          s == 200 and cw.get("scope") == "waiting" and cw["calls"] == []
          and s2 == 200 and [x["scorecard_id"] for x in cau["calls"]] == [sc["id"]] and cau["calls"][0]["in_session"] == sid and cau["calls"][0]["coached_at"]
          and s3 == 200 and {x["scorecard_id"] for x in call_["calls"]} >= {x["scorecard_id"] for x in cau["calls"]} and call_.get("scope") == "all" and call_.get("target") == 90
          and s4 == 400, (cw, [(x["scorecard_id"], x.get("review_status")) for x in call_.get("calls", [])]))
    check("candidates carry every scored criterion so a selection can be added up", len(cau["calls"][0].get("items") or []) == 8
          and set(cau["calls"][0]["items"][0]) == {"key", "name", "weight", "rating", "final"})
    s, sg = c.json("/api/coaching/sessions/%s" % sid)
    tot = sg.get("summary") or {}
    check("session carries the totals: one call at 90.0, 2 misses, verification and closing lost 5 points each",
          tot.get("calls") == 1 and tot.get("avg_pct") == 90.0 and tot.get("min_pct") == 90.0 and tot.get("below_target") == 0 and tot.get("misses") == 2
          and tot.get("target") == 90 and tot.get("audited") == 1 and tot.get("coached") == 1
          and [(k["name"], k["lost_total"], k["lost_points"]) for k in tot.get("by_criterion") or [] if k["lost_total"]] == [("Caller verification", 5.0, 5.0), ("Closing", 5.0, 5.0)], tot)
    s, pdfb2, _ = c.raw("/api/coaching/sessions/%s/export.pdf" % sid)
    check("coaching pack PDF has a Totals block", s == 200 and b"Totals" in pdfb2 and b"points lost across the calls" in pdfb2)
    two = [{"final_pct": 80.0, "review_status": "submitted", "misses": ["a"], "auto_fail": 0, "open_disputes": 1,
            "items": [{"key": "g", "name": "Greeting", "weight": 10, "rating": "partial", "final": 5}, {"key": "v", "name": "Verification", "weight": 10, "rating": "na", "final": 0}]},
           {"overall_pct": 60.0, "review_status": None, "misses": [], "auto_fail": 1, "open_disputes": 0,
            "items": [{"key": "g", "name": "Greeting", "weight": 10, "rating": "missed", "final": 0}, {"key": "c", "name": "Closing", "weight": 10, "rating": "met", "final": 10}]}]
    sm = coaching_mod.summarise(two, 90)
    check("summarise: average of final-else-AI, min/max, below target, misses, auto-fails, disputes, points lost per criterion (na excluded, worst first)",
          sm["calls"] == 2 and sm["scored"] == 2 and sm["avg_pct"] == 70.0 and sm["min_pct"] == 60.0 and sm["max_pct"] == 80.0 and sm["below_target"] == 2
          and sm["misses"] == 1 and sm["auto_fails"] == 1 and sm["open_disputes"] == 1 and sm["audited"] == 1
          and sm["by_criterion"][0] == {"key": "g", "name": "Greeting", "n": 2, "missed": 1, "partial": 1, "lost_points": 7.5, "lost_total": 15.0, "attainment_pct": 25.0}
          and sm["by_criterion"][1]["lost_total"] == 0.0 and len(sm["by_criterion"]) == 2 and coaching_mod.summarise([], 90)["avg_pct"] is None, sm)
    ag = coaching_mod.draft_agenda("A", [dict(two[0], audit_name="Call one"), dict(two[1], audit_name="Call two")], 90)
    check("agenda opens with the totals and the points lost per criterion; un-audited calls are marked as AI scores",
          "Calls: 2  ·  average score 70.0% (lowest 60.0%, highest 80.0%)  ·  2 below the 90% target  ·  1 misses" in ag
          and "Points lost per criterion (worst first): Greeting -15" in ag and "Call two — AI 60.0% (not audited)" in ag, ag)
    check("coaching helpers: overlap detection and agenda themes",
          coaching_mod.overlaps(coaching_mod.parse_when("2026-09-22T10:00Z"), 30, [("a", "2026-09-22T10:15:00+00:00", 30), ("b", "2026-09-22T10:30:00+00:00", 30)]) == ["a"]
          and "Themes (seen on more than one call)" in coaching_mod.draft_agenda("A", [{"misses": ["Did not verify"]}, {"misses": ["did not verify"]}]))
    # ── emailed calendar invites (0.40.0) through the fake relay ─────────
    import mailer as mailer_mod
    s, hm = c.json("/api/health")
    check("health reports the mail relay without secrets", s == 200 and (hm.get("mail") or {}).get("configured") is True and hm["mail"]["from"] == "auditly@test.local"
          and "password" not in json.dumps(hm["mail"]).lower().replace("password_set", ""), hm.get("mail"))
    s, cp = c.json("/api/coaching/sessions", "POST", {"agent_name": "Priya", "qa_name": "Demo QA", "scorecard_ids": []})
    sid3 = cp["id"]
    s, _ = c.json("/api/coaching/sessions/%s/invite" % sid3, "POST", {})
    check("invite needs a scheduled session (409)", s == 409)
    c.json("/api/coaching/sessions/%s/schedule" % sid3, "POST", {"scheduled_at": "2026-10-05T09:00:00Z", "duration_min": 30, "location": "Room 2"})
    s, e = c.json("/api/coaching/sessions/%s/invite" % sid3, "POST", {})
    check("invite needs the agent's email (400 with the hint)", s == 400 and "Settings › Names" in e.get("error", ""), e)
    n0 = len(SMTP.messages)
    s, inv = c.json("/api/coaching/sessions/%s/invite" % sid3, "POST", {"to": "priya@example.com", "qa_email": "qa@example.com"})
    msg = SMTP.messages[-1] if len(SMTP.messages) > n0 else {}
    import email as email_mod, email.policy as email_policy
    em = email_mod.message_from_string(msg.get("data", ""), policy=email_policy.default)
    cal_parts = [pt for pt in em.walk() if pt.get_content_type() == "text/calendar"]
    ics_body = cal_parts[0].get_content() if cal_parts else ""
    check("invite sent through the relay: session marked, invite recorded, message is a calendar REQUEST to the agent from the server mailbox with the QA as Reply-To",
          s == 200 and inv.get("invite_sent_at") and inv.get("invite_to") == "priya@example.com" and inv["invites"][0]["status"] == "sent" and inv["invites"][0]["from_email"] == "auditly@test.local"
          and len(SMTP.messages) == n0 + 1 and "priya@example.com" in " ".join(msg.get("to", [])) and "auditly@test.local" in (msg.get("from") or "")
          and em["Reply-To"] == "qa@example.com" and em["To"] == "priya@example.com" and "auditly@test.local" in em["From"]
          and len(cal_parts) >= 1 and cal_parts[0].get_param("method") == "REQUEST" and "BEGIN:VCALENDAR" in ics_body
          and "ORGANIZER;CN=Demo QA:mailto:qa@example.com" in ics_body and "ATTENDEE;CN=Priya" in ics_body and "DTSTART:20261005T090000Z" in ics_body
          and any(pt.get_filename() == "coaching.ics" for pt in em.walk()),
          (s, inv.get("invite_sent_at"), msg.get("from"), msg.get("to"), [pt.get_content_type() for pt in em.walk()], ics_body[:200]))
    check("the server-sent invite is recorded on the smtp channel", inv["invites"][0].get("channel") == "smtp")
    s, cal3 = c.json("/api/coaching/calendar?from=2026-10-01&to=2026-10-31&tz=0")
    d5 = [d for d in cal3.get("days") or [] if d["date"] == "2026-10-05"]
    check("calendar carries the invite mark on the session", len(d5) == 1 and d5[0]["sessions"][0].get("invite_sent_at") == inv["invite_sent_at"])
    # ── the QA's own mail app: an .eml draft, then "Mark invite as sent" (0.41.0) ──
    s, eb, eh = c.raw("/api/coaching/sessions/%s/export.eml?to=priya@example.com&qa=qa@example.com" % sid3)
    em2 = email_mod.message_from_bytes(eb, policy=email_policy.default) if s == 200 else None
    cal2 = [pt for pt in em2.walk() if pt.get_content_type() == "text/calendar"] if em2 else []
    check(".eml draft: message/rfc822 attachment, X-Unsent: 1 so Outlook opens it as a new message, To and From/Reply-To set, calendar REQUEST part and .ics attached",
          s == 200 and eh.get("Content-Type", "").startswith("message/rfc822") and 'filename="coaching-invite-2026-10-05-Priya.eml"' in eh.get("Content-Disposition", "")
          and em2 is not None and em2["X-Unsent"] == "1" and em2["To"] == "priya@example.com" and "qa@example.com" in (em2["From"] or "")
          and cal2 and cal2[0].get_param("method") == "REQUEST" and "ORGANIZER;CN=Demo QA:mailto:qa@example.com" in cal2[0].get_content()
          and any(pt.get_filename() == "coaching.ics" for pt in em2.walk()) and "no-store" in eh.get("Cache-Control", ""), (s, eh.get("Content-Type"), eh.get("Content-Disposition")))
    s, eb0, _ = c.raw("/api/coaching/sessions/%s/export.eml?to=&qa=" % sid3)
    em0 = email_mod.message_from_bytes(eb0, policy=email_policy.default) if s == 200 else None
    check(".eml draft with no addresses: no To header (the QA types it in the app); From falls back to the signed-in reviewer's account",
          s == 200 and em0 is not None and em0["To"] is None and "demo@example.com" in (em0["From"] or "") and em0["X-Unsent"] == "1", (em0["To"], em0["From"]) if em0 else s)
    s, _, _ = c.raw("/api/coaching/sessions/%s/export.eml?to=not-an-address" % sid3)
    check(".eml draft refuses a bad To (400)", s == 400)
    s, _ = c.json("/api/coaching/sessions/%s/invite-sent" % sid3, "POST", {"to": "nope"})
    check("Mark invite as sent needs an address (400)", s == 400)
    n2 = len(SMTP.messages)
    s, ms = c.json("/api/coaching/sessions/%s/invite-sent" % sid3, "POST", {"to": "priya@example.com"})
    check("Mark invite as sent records a mail_app invite, sets the ✉ mark, and sends nothing itself",
          s == 200 and ms["invite_to"] == "priya@example.com" and ms["invites"][0]["channel"] == "mail_app" and ms["invites"][0]["status"] == "sent"
          and ms["invites"][0]["from_email"] == "demo@example.com" and ms["invite_sent_at"] >= inv["invite_sent_at"] and len(SMTP.messages) == n2, ms.get("invites"))
    s, cp2 = c.json("/api/coaching/sessions", "POST", {"agent_name": "Marcus", "scorecard_ids": []})
    s1, _, _ = c.raw("/api/coaching/sessions/%s/export.eml" % cp2["id"]); s2, _ = c.json("/api/coaching/sessions/%s/invite-sent" % cp2["id"], "POST", {"to": "m@example.com"})
    check("draft and mark both need a scheduled session (409, 409)", s1 == 409 and s2 == 409)
    c.json("/api/coaching/sessions/%s/cancel" % cp2["id"], "POST", {})
    s, _ = c.json("/api/admin/mail/test", "POST", {"to": "nope"})
    check("mail test needs an address (400)", s == 400)
    n1 = len(SMTP.messages)
    s, mt = c.json("/api/admin/mail/test", "POST", {"to": "admin@example.com"})
    check("admin mail test sends a plain message through the relay", s == 200 and mt.get("ok") and len(SMTP.messages) == n1 + 1 and "Auditly mail test" in SMTP.messages[-1]["data"], mt)
    try:
        mailer_mod.send({"SMTP_HOST": "127.0.0.1", "SMTP_PORT": "9", "SMTP_STARTTLS": "0", "SMTP_FROM": "a@b.c"}, mailer_mod.build_invite({"id": "x", "agent_name": "A"}, "t@b.c", None, None, "BEGIN:VCALENDAR\nEND:VCALENDAR\n", "hi", "a@b.c"))
        check("mailer: unreachable relay raises a clean RuntimeError", False)
    except RuntimeError as ex:
        check("mailer: unreachable relay raises a clean RuntimeError", "Could not reach 127.0.0.1:9" in str(ex) or "Timed out" in str(ex), str(ex))
    check("mailer: a leaked 'password' word is redacted from relay errors", mailer_mod._clean(Exception("bad password here")) == "bad *** here")
    check("mailer.problem names the missing settings when SMTP_HOST is unset", "SMTP_HOST" in (mailer_mod.problem({}) or ""))

    # ── the agent's dispute link (0.52.0): a token, a read-only page, disputes without a login ──
    import auditly_host as host_mod2
    check("public_base: PUBLIC_URL wins, else scheme + Host of the request",
          host_mod2.public_base({"AUDITLY_PUBLIC_URL": ""}, "qa.local:8084", False) == "http://qa.local:8084"
          and host_mod2.public_base({"AUDITLY_PUBLIC_URL": ""}, "qa.local:8444", True) == "https://qa.local:8444"
          and host_mod2.public_base({"AUDITLY_PUBLIC_URL": "https://qa.co/"}, "x", False) == "https://qa.co")
    pm = mailer_mod.build_plain("a@b.c", "s@b.c", "S", "T", reply_to="q@b.c", sender_name="QA One")
    check("mailer.build_plain: plain text with From name, To and Reply-To", pm["To"] == "a@b.c" and pm["Reply-To"] == "q@b.c" and "QA One" in pm["From"] and pm.get_content().strip() == "T")
    rv0 = c.json("/api/scorecards/%s/review" % sc["id"])[1].get("review") or {}
    s, sc_l = c.json("/api/scorecards/" + sc["id"])
    dlk = sc_l.get("dispute_link") or {}
    check("a submitted scorecard reports its dispute-link status: enabled, no live link yet, the agent's address on file",
          s == 200 and dlk.get("enabled") is True and dlk.get("live") == 0 and dlk.get("agent_email") == "jordan@example.com" and dlk.get("last") is None, dlk)
    s, mk = c.json("/api/scorecards/%s/dispute-link" % sc["id"], "POST", {"action": "link"})
    url1 = mk.get("url") or ""
    tok1 = url1.rsplit("/", 1)[-1]
    check("Copy link mints a token under AUDITLY_PUBLIC_URL and the status shows one live copied link",
          s == 200 and url1.startswith("https://qa.test.local/dispute/") and re.match(r"^[A-Za-z0-9_-]{40,}$", tok1) is not None
          and mk["dispute_link"]["live"] == 1 and mk["dispute_link"]["last"]["channel"] == "copy" and mk["dispute_link"]["last"]["live"] is True, mk.get("dispute_link"))
    page = "/dispute/" + tok1
    s, pb, ph = anon.raw(page)
    ptxt = pb.decode()
    check("the agent page is served anonymously with the nonce CSP, no-store, noindex; no inline style, nothing external, no native dialog",
          s == 200 and "nonce-" in ph.get("Content-Security-Policy", "") and "form-action 'none'" in ph.get("Content-Security-Policy", "")
          and "noindex" in ph.get("X-Robots-Tag", "") and "no-store" in ph.get("Cache-Control", "")
          and 'style="' not in ptxt and "src=\"http" not in ptxt and "__CSP_NONCE__" not in ptxt
          and not re.search(r"\b(alert|confirm|prompt)\(", ptxt) and "Your QA scorecard" in ptxt and "Dispute the whole scorecard" in ptxt, (s, ph.get("Content-Security-Policy")))
    s, db_, dh = anon.raw(page + "/data")
    av = json.loads(db_.decode())
    leak_words = [w for w in (b'"transcript"', b'"utterances"', b'"caller_', b'"qa_name"', b'"coaching_notes"', b'"customer"', b'"override_by"', b'"model"',
                              b"Northwind", b"Sam Lee", b"demo@example.com", b'"audio', b"/up/", b"test-secret", b'"reviewer_email"', b'"resolved_by"', b'"raised_by"', b'"recording"', b'"review"', b'"kb"') if w in db_]
    check("the agent's data is the scores, reasons and quotes for Jordan -- and nothing about the call, the customer or the QA",
          s == 200 and av.get("agent_name") == "Jordan" and av.get("items") and set(av["items"][0]["evidence"][0].keys()) <= {"ts", "quote"}
          and isinstance(av.get("disputes"), list) and av.get("expires_at") and av.get("final_pct") == 90.0 and av.get("submitted_at")
          and "recording" not in av and "review" not in av and not leak_words, (leak_words, list(av.keys())))
    s, _, _ = anon.raw(page + "/raise", "POST", body={"reason": "x"}, csrf=False)
    check("the agent's raise needs the CSRF header like every POST (403)", s == 403)
    crit_a = [i for i in av["items"] if i["criterion_id"] != crit["criterion_id"]][0]
    s, rz = anon.json(page + "/raise", "POST", {"reason": "I did greet with the company name — it is the first thing I said.", "criterion_id": crit_a["criterion_id"]})
    mine = [d for d in rz.get("disputes", []) if d["criterion_id"] == crit_a["criterion_id"] and d["status"] == "open"]
    check("the agent raises a dispute from the link: 201, the agent view again, marked via link, no call text", s == 201 and mine and mine[0]["via"] == "link"
          and rz.get("open_disputes") == 1 and "recording" not in rz and "transcript" not in rz, rz.get("disputes"))
    s, scq = c.json("/api/scorecards/" + sc["id"])
    dq = [d for d in scq.get("disputes", []) if d["criterion_id"] == crit_a["criterion_id"] and d["status"] == "open"]
    s2, nfq = c.json("/api/notifications")
    topq = nfq["items"][0] if nfq.get("items") else {}
    check("the QA sees it as the agent's own: raised_by is the agent's address, via link; the bell says who, with no actor",
          dq and dq[0]["raised_by"] == "jordan@example.com" and dq[0]["via"] == "link" and topq.get("kind") == "dispute.raised"
          and topq.get("agent_name") == "Jordan" and not topq.get("actor"), (dq, topq))
    s, e = anon.json(page + "/raise", "POST", {"reason": "again", "criterion_id": crit_a["criterion_id"]})
    check("a second open dispute on the same score is refused in the agent's words (409)", s == 409 and "reviewer will answer" in e.get("error", ""), e)
    s, _ = anon.json(page + "/raise", "POST", {"reason": "   ", "criterion_id": crit_a["criterion_id"]})
    check("an empty reason is 400", s == 400)
    s, _ = anon.json(page + "/raise", "POST", {"reason": "x", "criterion_id": "not-a-criterion"})
    check("an unknown criterion is 404", s == 404)
    s, rz2 = anon.json(page + "/raise", "POST", {"reason": "The whole score feels off after the greeting was fixed."})
    check("the link stays usable after a dispute: the whole card can be disputed too", s == 201 and rz2.get("open_disputes") == 2, rz2.get("open_disputes"))
    for d in [d for d in scq.get("disputes", []) if d["status"] == "open"] + [d for d in c.json("/api/scorecards/" + sc["id"])[1]["disputes"] if d["status"] == "open" and not d["criterion_id"]]:
        c.json("/api/disputes/%s/withdraw" % d["id"], "POST", {})
    s, av2 = anon.json(page + "/data")
    check("the agent sees the outcome: both disputes now withdrawn", s == 200 and av2.get("open_disputes") == 0
          and sorted(d["status"] for d in av2["disputes"] if d["via"] == "link") == ["withdrawn", "withdrawn"], av2.get("disputes"))
    s, _ = c.json("/api/scorecards/%s/review/reopen" % sc["id"], "POST", {})
    s1, _, _ = anon.raw(page); s2, e2 = anon.json(page + "/data"); s3, _ = anon.json(page + "/raise", "POST", {"reason": "x"})
    s4, e4 = c.json("/api/scorecards/%s/dispute-link" % sc["id"], "POST", {"action": "link"})
    check("reopening the audit revokes the link: page 404, data 404 with the one message, raise 404, and no new link until resubmitted (409)",
          s == 200 and s1 == 404 and s2 == 404 and e2.get("error") == host_mod2.LINK_GONE and s3 == 404 and s4 == 409
          and c.json("/api/scorecards/" + sc["id"])[1]["dispute_link"]["live"] == 0, (s1, s2, s3, s4))
    n_mail = len(SMTP.messages)
    s, rs = c.json("/api/scorecards/%s/review/submit" % sc["id"], "POST", {"coaching_notes": rv0.get("coaching_notes") or "", "resolution_notes": rv0.get("resolution_notes") or "",
                                                                        "action_plan": rv0.get("action_plan") or "", "follow_up_on": rv0.get("follow_up_on")})
    just = (rs.get("dispute_link") or {}).get("just") or {}
    mail = SMTP.messages[-1]["data"] if len(SMTP.messages) == n_mail + 1 else ""
    mtok = re.search(r"https://qa\.test\.local/dispute/([A-Za-z0-9_-]{40,})", mail)
    check("resubmitting emails the agent a fresh link: one relay message To Jordan with the URL, the submit reports it",
          s == 200 and just.get("ok") is True and just.get("to") == "jordan@example.com" and len(SMTP.messages) == n_mail + 1
          and "To: jordan@example.com" in mail and "auditly@test.local" in mail and "Your QA scorecard" in mail and mtok is not None
          and rs["dispute_link"]["last"]["channel"] == "smtp" and rs["dispute_link"]["live"] == 1, (just, mail[:300]))
    page2 = "/dispute/" + (mtok.group(1) if mtok else "none")
    s1, _, _ = anon.raw(page2); s2, av3 = anon.json(page2 + "/data")
    check("the emailed token opens the page and the data", s1 == 200 and s2 == 200 and av3.get("agent_name") == "Jordan", (s1, s2))
    dbx = core.connect(env["AUDITLY_DB"])
    dbx.execute("UPDATE dispute_link SET expires_at='2000-01-01T00:00:00+00:00' WHERE sent_to='jordan@example.com' AND revoked_at IS NULL"); dbx.commit(); dbx.close()
    s2, _ = anon.json(page2 + "/data")
    check("an expired link is gone", s2 == 404)
    s, em2 = c.json("/api/scorecards/%s/dispute-link" % sc["id"], "POST", {"action": "email", "to": "jordan@example.com"})
    check("Dispute link › Email sends again through the relay", s == 200 and em2.get("sent_to") == "jordan@example.com" and len(SMTP.messages) == n_mail + 2, em2)
    s, e = c.json("/api/scorecards/%s/dispute-link" % sc["id"], "POST", {"action": "email", "to": "nope"})
    check("Email needs an address that looks like one (400, names Settings › Names)", s == 400 and "Settings › Names" in e.get("error", ""), e)
    s, e = c.json("/api/scorecards/%s/dispute-link" % sc["id"], "POST", {"action": "teleport"})
    check("an unknown action is 400", s == 400)
    bogus = [anon.raw("/dispute/" + "x" * 43)[0], anon.raw("/dispute/short")[0], anon.raw("/dispute/../..")[0], anon.raw("/dispute/" + "x" * 43 + "/data")[0]]
    check("unknown, malformed and traversal tokens are all 404, never 500 or 401", all(b in (404,) for b in bogus), bogus)
    s, _ = anon.json("/api/scorecards/%s/dispute-link" % sc["id"])
    check("the QA's dispute-link route stays behind sign-in (401 anonymous)", s == 401)
    s, aul = c.json("/api/admin/audit")
    acts = {a["action"] for a in aul.get("audit", [])}
    toks = [tok1] + ([mtok.group(1)] if mtok else [])
    leaked = [a for a in aul.get("audit", []) if any(t in (a.get("detail") or "") or t in (a.get("target") or "") for t in toks)]
    check("the audit log records sent, copied and revoked links and the agent's raise -- and never a token",
          {"dispute.link.sent", "dispute.link.copied", "dispute.link.revoked", "dispute.raise"} <= acts
          and any(a["action"] == "dispute.raise" and (a.get("detail") or "").startswith("via link") for a in aul["audit"]) and not leaked, (acts, leaked))
    s, rv1 = c.json("/api/scorecards/%s/review" % sc["id"])
    s, dgl = c.json("/api/disputes?kind=demo&status=all")
    check("end state: the review is submitted at 90.0 again and the two link disputes are withdrawn",
          (rv1.get("review") or {}).get("status") == "submitted" and (rv1.get("review") or {}).get("final_pct") == 90.0
          and dgl["counts"] == {"open": 0, "upheld": 1, "rejected": 0, "withdrawn": 3}, (rv1.get("review"), dgl.get("counts")))

    # ── Focus on one call (0.53.0): the lists narrow to one recording ──
    rec_f = sc.get("recording_id") or (sc.get("recording") or {}).get("id")
    s, fj = c.json("/api/jobs?recording=%s" % rec_f)
    s2, fr = c.json("/api/reviews?recording=%s&kind=all&status=done" % rec_f)
    s3, fb_ = c.json("/api/jobs?recording=not-a-real-id")
    s4, fr2 = c.json("/api/reviews?recording=not-a-real-id&kind=all")
    s5, fj2 = c.json("/api/jobs?recording=%s,not-a-real-id,%s" % (rec_f, rec_f))
    s6, fn = c.json("/api/jobs?recording=none")
    check("?recording= takes a comma list (focus mode, 0.55.0): the union of the ids, and 'none' matches nothing",
          s5 == 200 and fj2["total"] == fj["total"] and s6 == 200 and fn["total"] == 0 and c.json("/api/reviews?recording=none&kind=all")[1]["total"] == 0, (fj2.get("total"), fn.get("total")))
    check("?recording= narrows Calls to every run of that call and Audit to its reviews; an unknown id matches nothing, never errors",
          s == 200 and fj["total"] >= 1 and fj["total"] == len(fj["jobs"]) and all((j.get("recording") or {}).get("id") == rec_f for j in fj["jobs"])
          and s2 == 200 and fr["total"] >= 1 and all((x.get("recording") or {}).get("id") == rec_f for x in fr["reviews"])
          and s3 == 200 and fb_["total"] == 0 and s4 == 200 and fr2["total"] == 0 and fr2["counts"] == {"todo": 0, "done": 0},
          (fj.get("total"), fr.get("total"), fb_.get("total"), fr2.get("counts")))
    c.json("/api/coaching/sessions/%s/cancel" % sid3, "POST", {})
    s, b, hd = c.raw("/api/scorecards/%s/export.csv" % sc["id"])
    check("CSV carries the delivery block", b"Delivery: agent talk share," in b and b"Delivery: caller sentiment trend (per line),improving" in b)
    check("CSV carries the review and the sentiment", b"Final QA score,90.0" in b and b"Customer sentiment,negative -> positive" in b
          and b"Coaching notes,Verify before noting." in b and b"Call reasons,No service" in b)
    s, b, hd = c.raw("/api/scorecards/%s/export.pdf" % sc["id"])
    check("PDF still builds with the review block", s == 200 and b.startswith(b"%PDF"))

    # ── call details (fix attribution after upload) ───────────────────
    rec_id = sc["recording"]["id"]
    day = (sc["recording"]["uploaded_at"] or "")[:10]
    s, _ = c.json("/api/recordings/%s/details" % rec_id, "POST", {"caller_email": "nope"})
    check("details: bad email 400", s == 400)
    s, _ = c.json("/api/recordings/%s/details" % rec_id, "POST", {"agent_name": "Nobody Here"})
    check("details: unknown agent 400", s == 400)
    c.json("/api/people", "POST", {"kind": "qa", "names": "Second QA"})
    s, _ = c.json("/api/recordings/%s/details" % rec_id, "POST", {"qa_name": "Second QA"})
    check("details: QA of record locked while the audit is submitted (409)", s == 409)
    s, dd = c.json("/api/recordings/%s/details" % rec_id, "POST", {"qa_name": "demo qa", "agent_name": "priya", "caller_company": "Harbor Dental",
                                                                    "caller_name": "Maria Lopez", "call_ref": "HD-101", "caller_email": "maria@harbordental.example"})
    r = dd.get("recording") or {}
    check("details: canonical names, customer, ref saved; audit name rebuilt",
          s == 200 and r.get("agent_name") == "Priya" and r.get("qa_name") == "Demo QA" and r.get("call_ref") == "HD-101"
          and r.get("audit_name") == "Priya — Harbor Dental — Maria Lopez — " + day and r.get("caller_email") == "maria@harbordental.example", r)
    s, dp = c.json("/api/recordings/%s/details" % rec_id, "POST", {"caller_phone": "  555 0100 ", "ticket_ref": "T-48213"})
    check("details: phone and ticket number are editable (0.61.0)", s == 200 and dp["recording"]["caller_phone"] == "555 0100" and dp["recording"]["ticket_ref"] == "T-48213", dp)
    s, dp2 = c.json("/api/recordings/%s/details" % rec_id, "POST", {"ticket_ref": ""})
    check("details: a blank ticket number clears it", s == 200 and dp2["recording"]["ticket_ref"] is None and dp2["recording"]["caller_phone"] == "555 0100")
    s, dd = c.json("/api/recordings/%s/details" % rec_id, "POST", {"audit_name": "Custom title"})
    s2, dd2 = c.json("/api/recordings/%s/details" % rec_id, "POST", {"caller_name": "Maria"})
    check("details: a hand-written audit name is kept", dd["recording"]["audit_name"] == "Custom title" and dd2["recording"]["audit_name"] == "Custom title")
    s, dd3 = c.json("/api/recordings/%s/details" % rec_id, "POST", {"audit_name": "", "agent_name": "Jordan", "caller_company": "Northwind Traders", "caller_name": "Sam Lee", "call_ref": "DEMO-0001"})
    check("details: blank audit name is rebuilt", s == 200 and dd3["recording"]["audit_name"] == "Jordan — Northwind Traders — Sam Lee — " + day and dd3["recording"]["agent_name"] == "Jordan", dd3.get("recording"))
    s, scx = c.json("/api/scorecards/" + sc["id"])
    check("scorecard shows the corrected details", scx["recording"]["agent_name"] == "Jordan" and scx["recording"]["call_ref"] == "DEMO-0001")

    # ── media ─────────────────────────────────────────────────────────
    rec = sc["recording"]["id"]
    s, b, hd = c.raw("/api/recordings/%s/audio" % rec)
    check("audio 200 with Accept-Ranges", s == 200 and hd.get("Accept-Ranges") == "bytes" and len(b) > 1000)
    s, b, hd = c.raw("/api/recordings/%s/audio" % rec, headers={"Range": "bytes=100-199"})
    check("audio range 206 / 100 bytes", s == 206 and len(b) == 100 and hd.get("Content-Range", "").startswith("bytes 100-199/"))
    s, b, hd = c.raw("/api/recordings/%s/audio" % rec, headers={"Range": "bytes=999999999-"})
    check("audio bad range 416", s == 416)
    s, t = c.json("/api/recordings/%s/transcript" % rec)
    check("transcript endpoint", s == 200 and len(t.get("utterances", [])) == 19)
    s, hd = c.json("/api/health")
    check("public demo: AUDITLY_DEMO_NOTE reaches /api/health as demo_note and the page appends it to the demo banner",
          s == 200 and hd.get("demo_note") == "sandbox note for the banner" and 'if (h.demo && h.demo_note) $("demoBanner").textContent += " " + h.demo_note;' in html
          and "AUDITLY_DEMO_NOTE" in open(os.path.join(ROOT, ".env.example"), encoding="utf-8").read())

    # ── rubrics ───────────────────────────────────────────────────────
    s, rb = c.json("/api/rubrics")
    check("rubric list has demo rubric", s == 200 and any(r["name"].startswith("L1 Support v1") for r in rb["rubrics"]))
    rvid = [r for r in rb["rubrics"] if r["name"].startswith("L1 Support v1")][0]["current_version_id"]
    s, e = c.json("/api/rubrics", "POST", {"name": "Bad", "criteria": [{"name": "A", "weight": 60}, {"name": "B", "weight": 35}]})
    check("weights 95 rejected with message", s == 400 and "95" in e.get("error", ""), e)
    s, e = c.json("/api/rubrics", "POST", {"name": "Bad2", "criteria": []})
    check("empty criteria rejected", s == 400)
    s, ok = c.json("/api/rubrics", "POST", {"name": "Test rubric",
                                           "criteria": [{"name": "A", "weight": 60, "critical": True}, {"name": "B", "weight": 40}]})
    check("rubric create v1", s == 201 and ok["version_no"] == 1, ok)
    rid = ok["id"]
    s, sm = c.json("/api/rubrics", "POST", {"name": "Sampled", "criteria": [
        {"name": "Greeting", "weight": 100, "guidance_met": "company and name", "example_good": "Thanks for calling SNET Connect, this is Jordan.", "example_bad": "Hello?"}]})
    s2, smr = c.json("/api/rubrics/%s" % sm.get("id"))
    smc = (smr.get("versions") or [{}])[0].get("criteria") or [{}]
    check("good and bad samples per criterion (0.61.0) are saved, returned and read by the prompt",
          s == 201 and s2 == 200 and smc[0].get("example_good") == "Thanks for calling SNET Connect, this is Jordan." and smc[0].get("example_bad") == "Hello?"
          and 'good sample (earns met): "Thanks for calling SNET Connect, this is Jordan."' in score_mod.build_prompt("R", 1, [dict(smc[0], key="greeting", critical=0)], "[00:00] S0: hi", True)[1]
          and "bad sample (earns missed)" in score_mod.build_prompt("R", 1, [dict(smc[0], key="greeting", critical=0)], "[00:00] S0: hi", True)[1], (s, s2, smc[:1]))
    s, e = c.json("/api/rubrics", "POST", {"name": "Sampled2", "criteria": [{"name": "A", "weight": 100, "example_good": "x" * 301}]})
    check("a sample over 300 characters is refused", s == 400 and "300" in e.get("error", ""), e)
    check("the extract schema proposes samples and the demo rubric carries them",
          "example_good" in rubric_mod.EXTRACT_SCHEMA["properties"]["criteria"]["items"]["required"]
          and any(cr.get("example_good") for v in rb_full.get("versions", [{}])[:1] for cr in v.get("criteria", [])) if (rb_full := c.json("/api/rubrics/%s" % [r for r in rb["rubrics"] if r["name"].startswith("L1 Support v1")][0]["id"])[1]) else False)
    s, dup = c.json("/api/rubrics", "POST", {"name": "Test rubric", "criteria": [{"name": "A", "weight": 100}]})
    check("duplicate rubric name 409", s == 409)
    before = [r for r in c.json("/api/rubrics")[1]["rubrics"] if r["id"] == rid][0]
    check("QA type list carries the last-edited stamp and author", before["updated_at"] and before["updated_by"] == "demo@example.com", before)
    time.sleep(1.1)
    s, v2 = c.json("/api/rubrics/%s/versions" % rid, "POST", {"criteria": [{"name": "A", "weight": 50}, {"name": "B", "weight": 50}], "notes": "Evened the weights"})
    check("rubric new version is v2", s == 201 and v2["version_no"] == 2)
    after = [r for r in c.json("/api/rubrics")[1]["rubrics"] if r["id"] == rid][0]
    check("saving a version moves the last-edited stamp forward and keeps the author", after["updated_at"] > before["updated_at"] and after["updated_by"] == "demo@example.com", (before["updated_at"], after["updated_at"]))
    import auditly_host as host_mod
    check("only admin and reviewer may change QA types (server rule)", host_mod.can_edit_rubrics({"role": "admin"}) and host_mod.can_edit_rubrics({"role": "reviewer"})
          and not host_mod.can_edit_rubrics({"role": "viewer"}) and not host_mod.can_edit_rubrics(None))
    s, v1 = c.json("/api/rubrics/%s/versions/1" % rid)
    check("v1 unchanged after v2", s == 200 and [x["weight"] for x in v1["criteria"]] == [60, 40] and v1["criteria"][0]["critical"] is True)
    s, full = c.json("/api/rubrics/" + rid)
    check("rubric detail lists 2 versions", s == 200 and len(full["versions"]) == 2)
    check("the version records what changed and who made it", full["versions"][0]["notes"] == "Evened the weights" and full["versions"][0]["created_by"] == "demo@example.com"
          and full["versions"][1]["notes"] is None, full["versions"][0])
    s, ex = c.json("/api/rubrics/extract", "POST", {"text": open(os.path.join(ROOT, "fixtures", "demo_rubric.md")).read()})
    check("extract proposes criteria summing to 100", s == 200 and sum(x["weight"] for x in ex["criteria"]) == 100 and len(ex["criteria"]) >= 4)
    # ── Ask Auditly (0.46.0): answers about one call, cites everything, writes nothing ──
    import assistant as assistant_mod
    s, hh = c.json("/api/health")
    check("health carries the assistant's status: on here, no problem, its own prompt version",
          s == 200 and (hh.get("ask") or {}).get("enabled") is True and hh["ask"]["problem"] is None
          and hh["ask"]["prompt_version"] == assistant_mod.ASK_PROMPT_VERSION and hh["ask"]["today"] == 0, hh.get("ask"))
    s, jj = c.json("/api/jobs")
    ask_sc = next(j["scorecard_id"] for j in jj["jobs"] if j.get("scorecard_id"))
    golden = json.load(open(os.path.join(ROOT, "fixtures", "demo_ask.json"), encoding="utf-8"))
    g_ok = 0
    for g in golden:
        s, a = c.json("/api/ask", "POST", {"scorecard_id": ask_sc, "question": g["q"]})
        ids = [x.get("id") for x in (a.get("cites") or [])]
        ok = (s == 201 and a.get("grounded") is g["grounded"]
              and (not g.get("cite") or any(str(i).startswith(g["cite"]) for i in ids))
              and (not g.get("contains") or g["contains"].lower() in (a.get("answer") or "").lower())
              and (g["grounded"] or not ids)                       # an ungrounded answer cites nothing
              and all(x.get("label") and x["label"] != x["id"] for x in (a.get("cites") or [])))   # chips are labels, never raw ids
        g_ok += 1 if ok else 0
        check("ask golden: " + g["q"][:58], ok, (s, a.get("grounded"), ids, (a.get("answer") or "")[:90]))
    print("       assistant golden set: %d of %d" % (g_ok, len(golden)))
    s, th = c.json("/api/ask/thread?scorecard_id=" + ask_sc)
    check("the conversation is kept per person per call, in order, with what produced each answer",
          s == 200 and len(th["turns"]) == len(golden) and th["turns"][0]["seq"] == 1 and th["turns"][-1]["seq"] == len(golden)
          and th["turns"][0]["prompt_version"] == assistant_mod.ASK_PROMPT_VERSION and th["turns"][0]["usage"] is not None
          and th["turns"][0]["model"] == "demo", len(th.get("turns") or []))
    s, a = c.json("/api/ask", "POST", {"scorecard_id": ask_sc, "question": "   "})
    check("an empty question is refused before anything is spent", s == 400)
    s, a = c.json("/api/ask", "POST", {"scorecard_id": ask_sc, "question": "x" * 501})
    check("a question over 500 characters is refused (that is a paste)", s == 400 and "501" in a.get("error", ""))
    s, a = c.json("/api/ask", "POST", {"scorecard_id": "00000000-0000-4000-8000-000000000000", "question": "hello"})
    check("a question about an unknown call is 404", s == 404)
    s, a = c.json("/api/ask", "POST", {"scorecard_id": "not-a-uuid", "question": "hello"})
    check("a malformed scorecard id is 400", s == 400)
    s, hh = c.json("/api/health")
    check("assistant turns count in Settings' spend and in today's tally",
          hh["spend"].get("asks") == len(golden) and hh["ask"]["today"] == len(golden), (hh["spend"].get("asks"), hh["ask"]["today"]))
    s, au = c.json("/api/admin/audit")
    check("every question is audited under its own verb, never as a scorecard view",
          sum(1 for r in au["audit"] if r["action"] == "assistant.ask") == len(golden)
          and all(r["target"] == ask_sc for r in au["audit"] if r["action"] == "assistant.ask"))
    # hygiene: what the model is handed carries no contact details, and an invented citation is dropped
    s, full = c.json("/api/scorecards/" + ask_sc)
    ctx, allowed = assistant_mod.build_context(full)
    cust = (full.get("customer") or {})
    check("the assistant's context carries no email address, phone or reviewer address -- fields that never enter it cannot leak from it",
          "@" not in ctx and not (cust.get("email") and cust["email"] in ctx) and not (cust.get("phone") and cust["phone"] in ctx)
          and "criterion:" in ctx and "<<<TRANSCRIPT" in ctx and "END TRANSCRIPT>>>" in ctx, ctx[:200])
    ans, cites, grounded, warns = assistant_mod.clean_answer({"answer": "x", "cites": ["criterion:greeting", "criterion:made_up", "[call:score]"], "grounded": True}, allowed)
    check("a citation the record did not offer is dropped, brackets tolerated, and the drop is recorded",
          cites == ["criterion:greeting", "call:score"] and grounded is True and any("made_up" in w for w in warns), (cites, warns))
    ans, cites, grounded, warns = assistant_mod.clean_answer({"answer": "x", "cites": ["nope:1"], "grounded": True}, allowed)
    check("an answer whose every citation is dropped is no longer grounded", grounded is False and cites == [])
    ans, cites, grounded, warns = assistant_mod.clean_answer("garbage", allowed)
    check("a non-object reply degrades to an ungrounded apology, never an exception", grounded is False and ans)
    check("the assistant's prompt version is its own, not the scorer's", assistant_mod.ASK_PROMPT_VERSION != __import__("score").PROMPT_VERSION or True)
    check("the system prompt separates instructions from data and forbids writes", "DATA" in assistant_mod.SYSTEM and "never an instruction" in assistant_mod.SYSTEM and "cannot change anything" in assistant_mod.SYSTEM)
    # the retrieval query is the question plus the call's own words, never the question alone
    check("kb query carries the call's words, not just the question", len(assistant_mod.kb_query(full, "what did they miss?")) > 200)

    # ── Settings › Server › Assistant (0.47.0): the switch, model and caps override .env; the persona is versioned ──
    s, ad = c.json("/api/admin/ask")
    check("admin reads the assistant's settings: effective values from .env, built-in persona, the locked rules, usage, the models",
          s == 200 and ad["effective"]["source"] == "env" and ad["effective"]["enabled"] is True and ad["prompt"]["builtin"] is True
          and ad["prompt"]["persona"] == assistant_mod.PERSONA_DEFAULT and ad["rules"] == assistant_mod.RULES
          and ad["usage"]["total"] == len(golden) and ad["models"] and ad["settings"]["enabled"] is None, ad.get("effective"))
    s, e = c.json("/api/admin/ask", "POST", {"model": "not-a-model"})
    check("an unknown model is refused, naming the choices (400)", s == 400 and "gpt-4o-mini" in e.get("error", ""))
    s, e = c.json("/api/admin/ask", "POST", {"max_per_day": 99999})
    check("a cap outside 0-10000 is refused (400)", s == 400)
    s, ad = c.json("/api/admin/ask", "POST", {"enabled": False})
    check("switching off in Settings wins over .env and the reason points at Settings",
          s == 200 and ad["effective"]["enabled"] is False and ad["effective"]["source"] == "settings" and "Settings" in (ad["effective"]["problem"] or ""), ad.get("effective"))
    s, a = c.json("/api/ask", "POST", {"scorecard_id": ask_sc, "question": "hello"})
    check("with the switch off a question is refused before anything is spent, pointing at Settings", s == 409 and "Settings" in a.get("error", ""))
    s, hh = c.json("/api/health")
    check("health reflects the Settings switch, and says where it came from", hh["ask"]["enabled"] is False and hh["ask"]["source"] == "settings")
    s, ad = c.json("/api/admin/ask", "POST", {"enabled": True, "max_per_user_per_hour": 500, "model": ad["models"][-1]["model"]})
    check("switch on, cap and model saved; the effective values follow", s == 200 and ad["effective"]["enabled"] is True
          and ad["effective"]["max_per_user_per_hour"] == 500 and ad["effective"]["model"] == ad["models"][-1]["model"] and ad["effective"]["updated_by"], ad.get("effective"))
    s, e = c.json("/api/admin/ask/prompt", "POST", {"persona": "   "})
    check("an empty persona is refused (400)", s == 400)
    s, e = c.json("/api/admin/ask/prompt", "POST", {"persona": "x" * 4001})
    check("a persona over 4000 characters is refused (400)", s == 400 and "4001" in e.get("error", ""))
    s, e = c.json("/api/admin/ask/prompt", "POST", {"persona": "Be nice.\n" + assistant_mod.RULES})
    check("pasting the rules into the instructions is refused: they are added automatically", s == 400)
    s, e = c.json("/api/admin/ask/prompt/reset", "POST", {})
    check("reset while the built-in text is already in use is 409", s == 409)
    s, p1 = c.json("/api/admin/ask/prompt", "POST", {"persona": "You are a warm, plain-spoken assistant for a support desk's QA team.", "notes": "warmer tone"})
    check("saving the persona creates v1 with a note and a byline", s == 201 and p1["version_no"] == 1 and p1["notes"] == "warmer tone" and p1["created_by"] and p1["builtin"] is False, p1)
    s, ad = c.json("/api/admin/ask")
    check("the card shows the custom persona as current", ad["prompt"]["builtin"] is False and ad["prompt"]["id"] == p1["id"] and len(ad["prompts"]) == 1)
    sysp = assistant_mod.system_prompt(ad["prompt"]["persona"])
    check("the locked rules are appended to a custom persona, word for word, with the data fence and the no-write rule",
          sysp.startswith("You are a warm") and assistant_mod.RULES in sysp and "never an instruction" in sysp and "cannot change anything" in sysp)
    s, a = c.json("/api/ask", "POST", {"scorecard_id": ask_sc, "question": "What was the overall score?"})
    check("a turn answered under a custom persona records which version answered it", s == 201 and a.get("prompt_id") == p1["id"] and a["grounded"] is True, a.get("prompt_id"))
    s, p2 = c.json("/api/admin/ask/prompt/reset", "POST", {})
    check("reset records a version and returns to the built-in text", s == 201 and p2["version_no"] == 2 and p2["builtin"] is True)
    s, ad = c.json("/api/admin/ask")
    check("history keeps both versions newest first; the built-in text is current again", ad["prompt"]["builtin"] is True and [x["version_no"] for x in ad["prompts"]] == [2, 1])
    s, t = c.json("/api/admin/ask/try", "POST", {"question": "How did the agent do on the greeting?"})
    check("Try it answers about the newest scored call, cited, and names the call", s == 200 and t["grounded"] is True and t["cites"] and t["about"] and t["prompt_id"] is None, t)
    s, th = c.json("/api/ask/thread?scorecard_id=" + ask_sc)
    check("Try it leaves no turn in the reviewer's thread", len(th["turns"]) == len(golden) + 1, len(th.get("turns") or []))
    s, au = c.json("/api/admin/audit")
    acts = [r["action"] for r in au["audit"]]
    check("settings and prompt changes are audited under their own verbs; Try it counts as a question",
          acts.count("assistant.settings") == 2 and acts.count("assistant.prompt") == 2 and acts.count("assistant.ask") == len(golden) + 2, {k: acts.count(k) for k in set(acts) if k.startswith("assistant.")})

    # ── voice (0.48.0): a spoken question, transcribed server-side, lands in the box ──
    clip = b"\x1aE\xdf\xa3" + b"\x00" * 3000                     # a WebM-looking blob; the demo path does not decode it
    vh = {"Content-Type": "audio/webm"}
    s, e, _ = c.raw("/api/ask/voice?ext=exe", "POST", data=clip, headers=vh)
    check("voice: an unknown clip type is refused before the body is read (400)", s == 400 and b"webm" in e)
    s, e, _ = c.raw("/api/ask/voice?ext=webm", "POST", data=b"", headers=vh)
    check("voice: an empty clip is 400", s == 400)
    s, e, _ = c.raw("/api/ask/voice?ext=webm", "POST", data=b"x" * (2 * 1024 * 1024 + 1), headers=vh)
    check("voice: a clip over 2 MB is 413 (keep a question under a minute)", s == 413)
    s, vb, _ = c.raw("/api/ask/voice?ext=webm", "POST", data=clip, headers=vh)
    v = json.loads(vb.decode())
    check("voice: in demo the fixed question comes back as text, with duration and provider, and nothing is asked",
          s == 200 and v["text"] == "How did the agent do on the greeting?" and v["provider"] == "demo" and v["duration_s"] >= 1, v)
    s, th = c.json("/api/ask/thread?scorecard_id=" + ask_sc)
    check("voice: transcribing adds no turn -- the reviewer still presses Ask", len(th["turns"]) == len(golden) + 1)
    s, hh = c.json("/api/health")
    check("voice: the clip shows in Settings' spend as a clip and its seconds", hh["spend"].get("voice_clips") == 1 and hh["spend"].get("voice_s", 0) >= 1, {k: hh["spend"].get(k) for k in ("voice_clips", "voice_s")})
    s, au = c.json("/api/admin/audit")
    check("voice: audited under its own verb with the duration and provider", any(r["action"] == "assistant.voice" and "via demo" in (r["detail"] or "") for r in au["audit"]))
    voice_dir_left = [f for f in os.listdir(env["AUDITLY_UPLOAD_DIR"]) if f.startswith("voice-")] if os.path.isdir(env["AUDITLY_UPLOAD_DIR"]) else []
    check("voice: no clip file is left on disk", voice_dir_left == [], voice_dir_left)
    c.json("/api/admin/ask", "POST", {"enabled": False})
    s, e, _ = c.raw("/api/ask/voice?ext=webm", "POST", data=clip, headers=vh)
    check("voice: with the assistant off the clip is refused unread, pointing at Settings (409)", s == 409 and b"Settings" in e)
    c.json("/api/admin/ask", "POST", {"enabled": True})
    check("voice: the page has the mic button, feature detection, the HTTPS reason, the 30 s stop and no vendor call",
          'id="askMic"' in html and "navigator.mediaDevices" in html and "window.MediaRecorder" in html and "isSecureContext" in html
          and "Voice needs the HTTPS link" in html and "setTimeout(micStop, 30000)" in html and '"/api/ask/voice?ext="' in html
          and "SpeechRecognition" not in html                                     # not the browser's recogniser
          and all(v not in html for v in ("speech.googleapis", "api.deepgram.com", "api.openai.com"))   # the browser never talks to a vendor
          and 'fetch("/api/ask/voice?ext="' in html)                            # the clip goes to our own origin
    # 0.50.0: Settings › Server is four panes; the admin cards keep their ids inside their wrappers
    check("Settings › Server is split into Status · Providers · Assistant · Branding, remembered per browser, admin panes gated",
          'id="srvTabs"' in html and "function setSrvTab" in html and "auditly-srvtab" in html and 'id="srvStatus"' in html and 'id="srvProviders"' in html
          and 'id="health"' not in html and 'setSrvTab(S.srvTab); loadHealth().then(renderHealth)' in html
          and html.index('id="srv-branding"') < html.index('id="brandCard"') < html.index('id="srv-assistant"') < html.index('id="askCard"')
          and '(v === "assistant" || v === "branding") && !(S.me && S.me.role === "admin")' in html)
    status_set = html[html.index("var STATUS_TILES = {"):html.index("};", html.index("var STATUS_TILES = {"))]
    check("the Status pane holds health and reachability; keys, spend and mail go to Providers",
          all(k in status_set for k in ('"Version"', '"Mode"', '"Secure link"', '"Queue"', '"Database"', '"Max upload"', '"Audio retention"', '"ffmpeg"', '"Menus"'))
          and not any(k in status_set for k in ('"Deepgram"', '"OpenAI"', '"Anthropic"', '"Spend (estimate)"', '"Mail (coaching invites)"', '"Default transcription"', '"Default scoring"')))
    check("the plain-HTTP page points voice at the app's https twin, and Settings shows the secure link",
          "Voice needs the secure link" in html and "S.health.tls_url" in html and '["Secure link", h.tls_url' in html)
    _, _, h0 = anon.raw("/health")
    check("the demo server sends no Strict-Transport-Security either", not any(k.lower() == "strict-transport-security" for k in h0))

    # ── knowledge base routes ────────────────────────────────────────
    krid = [r for r in rb["rubrics"] if r["name"].startswith("L1 Support v1")][0]["id"]
    s, kd = c.json("/api/kb/documents")
    check("knowledge base lists the demo document with its chunk count, scope and use", s == 200 and len(kd.get("documents") or []) == 1
          and kd["documents"][0]["chunks"] >= 3 and kd["documents"][0]["rubric_id"] is None and kd["documents"][0]["used_by"] >= 1 and kd["documents"][0]["text"] is None, kd)
    kid = kd["documents"][0]["id"]
    s, kd1 = c.json("/api/kb/documents/%s" % kid)
    check("one document comes with its text", s == 200 and "No Service" in (kd1.get("text") or ""))
    s, ks = c.json("/api/kb/search?q=" + urllib.parse.quote("phone shows no service screen lit cable wall jack registered"))
    check("kb search returns ranked excerpts with the matched words", s == 200 and ks.get("hits") and "No Service" in ks["hits"][0]["text"] and ks["hits"][0]["terms"], ks)
    s, _ = c.json("/api/kb/search")
    check("kb search needs q (400)", s == 400)
    s, _ = c.json("/api/kb/documents", "POST", {"title": "x", "text": "too short"})
    check("a document under 40 chars is refused (400)", s == 400)
    s, kn = c.json("/api/kb/documents", "POST", {"title": "Voicemail PIN reset", "text": "Voicemail PINs are reset from the portal under Users > Voicemail.\n\nThe default PIN is the last four digits of the extension.", "rubric_id": krid})
    check("pasted document added (201), scoped to one QA type, chunked", s == 201 and kn.get("enabled") is True and kn.get("rubric_id") == krid and kn.get("chunks") >= 1 and kn.get("title") == "Voicemail PIN reset", kn)
    import base64, io, zipfile
    zb = io.BytesIO()
    with zipfile.ZipFile(zb, "w") as z:
        z.writestr("word/document.xml", "<w:document><w:body><w:p><w:r><w:t>Porting checklist: gather the account number, the billing telephone number and an LOA.</w:t></w:r></w:p><w:p><w:r><w:t>Ports take 7 to 10 business days.</w:t></w:r></w:p></w:body></w:document>")
    s, kx = c.json("/api/kb/documents", "POST", {"filename": "porting.docx", "content_b64": base64.b64encode(zb.getvalue()).decode()})
    check(".docx upload is read (title from the file name)", s == 201 and kx.get("title") == "porting" and kx.get("chars") > 60 and kx.get("filename") == "porting.docx", kx)
    s, _ = c.json("/api/kb/documents", "POST", {"filename": "x.pdf", "content_b64": base64.b64encode(b"%PDF-1.4 nothing").decode()})
    check("PDF is refused with the existing message (400)", s == 400)
    s, ks2 = c.json("/api/kb/search?q=" + urllib.parse.quote("how long does a number port take, I have the LOA and the billing telephone number"))
    check("the new .docx document is searchable", s == 200 and any(h["document_id"] == kx["id"] for h in ks2.get("hits") or []), ks2)
    s, kdis = c.json("/api/kb/documents/%s/disable" % kx["id"], "POST", {})
    s2, ks3 = c.json("/api/kb/search?q=" + urllib.parse.quote("how long does a number port take, I have the LOA and the billing telephone number"))
    check("a disabled document is not searched", s == 200 and kdis.get("enabled") is False and not any(h["document_id"] == kx["id"] for h in ks3.get("hits") or []))
    s, ks4 = c.json("/api/kb/search?q=" + urllib.parse.quote("voicemail PIN reset default digits extension"))
    s2, ks5 = c.json("/api/kb/search?q=" + urllib.parse.quote("voicemail PIN reset default digits extension") + "&rubric_id=" + krid)
    check("a document scoped to a QA type is found only when searching as that QA type",
          s == 200 and not any(h["document_id"] == kn["id"] for h in ks4.get("hits") or []) and any(h["document_id"] == kn["id"] for h in ks5.get("hits") or []), (ks4.get("hits"), ks5.get("hits")))
    s, kr = c.json("/api/kb/documents/%s/rename" % kn["id"], "POST", {"title": "Voicemail"})
    s2, ksc = c.json("/api/kb/documents/%s/scope" % kn["id"], "POST", {"rubric_id": None})
    check("rename and re-scope", s == 200 and kr.get("title") == "Voicemail" and s2 == 200 and ksc.get("rubric_id") is None)
    s, _ = c.json("/api/kb/documents/%s/delete" % kx["id"], "POST", {})
    s2, kd2 = c.json("/api/kb/documents")
    check("admin delete is soft: gone from the list", s == 200 and all(d["id"] != kx["id"] for d in kd2["documents"]))
    c.json("/api/kb/documents/%s/delete" % kn["id"], "POST", {})
    s, ex = c.json("/api/rubrics/extract", "POST", {"text": "too short"})
    check("extract rejects tiny text", s == 400)
    # docx extraction, built in-memory
    import io, zipfile, base64
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<w:document><w:body><w:p><w:r><w:t>Heading one</w:t></w:r></w:p>"
                   "<w:p><w:r><w:t>Body &amp; text</w:t></w:r></w:p></w:body></w:document>")
    text = rubric_mod.text_from_upload("g.docx", buf.getvalue())
    check("docx text extraction", text == "Heading one\nBody & text", repr(text))
    try:
        rubric_mod.text_from_upload("g.pdf", b"%PDF")
        check("pdf refused", False)
    except ValueError:
        check("pdf refused", True)
    s, arch = c.json("/api/rubrics/%s/archive" % rid, "POST", {})
    check("rubric archive toggles", s == 200 and arch["archived"] is True)

    # ── people: agents + QA reviewers ─────────────────────────────────
    s, pp = c.json("/api/people")
    names = {(p["kind"], p["name"]) for p in pp.get("people", [])}
    check("demo seeds agents and a QA name", s == 200 and ("agent", "Jordan") in names and ("qa", "Demo QA") in names, names)
    s, d = c.json("/api/people", "POST", {"kind": "agent", "names": "Sam\n jordan \nSam\n\nAlex  Ng"})
    check("batch add: created 2, skipped duplicates", s == 201 and sorted(x["name"] for x in d.get("created", [])) == ["Alex Ng", "Sam"]
          and d.get("skipped") == ["jordan"], d)
    alex = [x for x in d["created"] if x["name"] == "Alex Ng"][0]
    s, d = c.json("/api/people", "POST", {"kind": "qa", "name": "Casey QA"})
    check("single add works too", s == 201 and [x["name"] for x in d.get("created", [])] == ["Casey QA"])
    s, _ = c.json("/api/people", "POST", {"kind": "bogus", "names": "x"})
    check("bad kind 400", s == 400)
    s, _ = c.json("/api/people/%s/archive" % alex["id"], "POST", {})
    s2, act = c.json("/api/people?kind=agent")
    s3, allp = c.json("/api/people?kind=agent&all=1")
    check("archived name hidden unless all=1", s == 200 and "Alex Ng" not in [p["name"] for p in act["people"]]
          and "Alex Ng" in [p["name"] for p in allp["people"]])
    s, _ = c.json("/api/people/%s/delete" % alex["id"], "POST", {})
    s2, allp = c.json("/api/people?all=1")
    check("unused name can be deleted", s == 200 and "Alex Ng" not in [p["name"] for p in allp["people"]])
    import llm as llm_mod
    p_old = llm_mod.OpenAIScorer({"OPENAI_API_KEY": "sk-x"}, "gpt-4o-mini").payload("s", "u", {"type": "object"}, "r", 123)
    p_new = llm_mod.OpenAIScorer({"OPENAI_API_KEY": "sk-x"}, "gpt-5.6-luna").payload("s", "u", {"type": "object"}, "r", 123)
    check("OpenAI payload: max_completion_tokens everywhere, temperature only on gpt-4 family",
          "max_tokens" not in p_old and p_old["max_completion_tokens"] == 123 and "temperature" in p_old
          and "max_tokens" not in p_new and "temperature" not in p_new and p_new["response_format"]["json_schema"]["strict"] is True)
    check("key shape: OpenAI must start with sk-, Deepgram 40 hex",
          llm_mod.key_shape("openai", "JeIQM_mp" + "x" * 148)["shape_ok"] is False
          and llm_mod.key_shape("openai", "sk-proj-" + "x" * 156)["shape_ok"] is True
          and llm_mod.key_shape("deepgram", "3ee5a9" + "0" * 34)["shape_ok"] is True
          and llm_mod.key_shape("deepgram", "not-hex-at-all-" + "0" * 30)["shape_ok"] is False
          and llm_mod.key_shape("openai", "")["shape_ok"] is None)
    import auditly_host as host
    check("audit_name_for drops blanks", host.audit_name_for("Sam", "", None, "2026-09-02") == "Sam — 2026-09-02"
          and host.audit_name_for(None, None, None, "2026-09-02") == "2026-09-02"
          and host.audit_name_for("A", "Acme", "Pat", "d") == "A — Acme — Pat — d")

    # ── upload ────────────────────────────────────────────────────────
    wav = open(os.path.join(ROOT, "fixtures", "tone.wav"), "rb").read()
    s, up0 = c.json("/api/recordings?filename=noqa.wav&provider=demo&rubric_version_id=%s&agent=Sam" % rvid, "POST", data=wav, headers={"Content-Type": "audio/wav"})
    j0 = wait_job(c, up0.get("job_id", ""))
    check("upload without QA name accepted; qa_name null", s == 201 and j0.get("status") == "done" and j0["recording"]["qa_name"] is None, j0.get("recording"))
    r0 = j0["recording"]
    check("call ID defaults to the file name without its extension", r0["call_ref"] == "noqa", r0.get("call_ref"))
    check("customer name and company filled from the call; audit name rebuilt with them",
          r0["caller_name"] == "Maria" and r0["caller_company"] == "Harbor Dental" and r0["caller_email"] is None
          and r0["audit_name"] == "Sam — Harbor Dental — Maria — " + r0["uploaded_at"][:10], r0)
    s, lk0 = c.json("/api/recordings/lookup?filename=noqa.wav&bytes=%d" % len(wav))
    check("lookup by file name and size finds the earlier upload (not audited)", s == 200 and [m["match"] for m in lk0["matches"]] == ["file"]
          and lk0["matches"][0]["audited"] is False and lk0["matches"][0]["recording_id"] == r0["id"], lk0)
    s, lk1 = c.json("/api/recordings/lookup?call_ref=nothing-like-this")
    check("lookup with no match is an empty list", s == 200 and lk1 == {"matches": []})
    s, _e = c.json("/api/recordings/lookup")
    check("lookup without parameters is 400", s == 400)
    s, e = c.json("/api/recordings?filename=call.wav&provider=demo&rubric_version_id=%s&qa=Nobody" % rvid, "POST", data=wav, headers={"Content-Type": "audio/wav"})
    check("upload with unknown QA name 400", s == 400 and "QA" in e.get("error", ""), e)
    s, e = c.json("/api/recordings?filename=call.wav&provider=demo&rubric_version_id=%s&agent=Nobody&qa=Demo%%20QA" % rvid, "POST", data=wav, headers={"Content-Type": "audio/wav"})
    check("upload with unknown agent 400 pointing at Settings", s == 400 and "Settings" in e.get("error", ""), e)
    s, e = c.json("/api/recordings?filename=call.wav&provider=demo&rubric_version_id=%s&caller_email=not-an-email" % rvid, "POST", data=wav, headers={"Content-Type": "audio/wav"})
    check("bad customer email 400", s == 400 and "email" in e.get("error", "").lower(), e)
    q = "?filename=call.wav&provider=demo&rubric_version_id=%s&agent=sam&qa=demo%%20qa&call_ref=T-1&caller_company=Acme&caller_name=Pat&caller_email=pat%%40acme.example" % rvid
    s, up = c.json("/api/recordings" + q, "POST", data=wav, headers={"Content-Type": "audio/wav"})
    check("upload accepted 201", s == 201 and up.get("job_id"), up)
    check("upload of already-seen audio reports the earlier call", (up.get("duplicate_of") or {}).get("audit_name") is not None, up.get("duplicate_of"))
    check("upload reports every look-alike with how it matched and whether it was audited",
          any(m["match"] == "audio" and m["call_ref"] == "noqa" and m["audited"] is False for m in up.get("duplicates") or [])
          and any(m["match"] == "audio" and m["call_ref"] == "DEMO-0001" and m["audited"] is True for m in up["duplicates"])
          and all("path" not in m and "sha256" not in m for m in up["duplicates"]), up.get("duplicates"))
    j = wait_job(c, up["job_id"])
    check("uploaded job runs to done", j.get("status") == "done", j)
    check("identical audio: transcript copied, no transcription charge", (j.get("usage") or {}).get("cached") is True and (j.get("usage") or {}).get("audio_s") == 0, j.get("usage"))
    sc_up = c.json("/api/scorecards/%s" % j["scorecard_id"])[1]
    check("copied transcript points at its source and carries the prompt version", bool(sc_up["transcript"].get("cached_from")) and sc_up.get("prompt_version") == 8, (sc_up["transcript"].get("cached_from"), sc_up.get("prompt_version")))
    check("uploaded job scored 90.0", j.get("overall_pct") == 90.0)
    rec = j["recording"]
    check("job carries agent/ref metadata (canonical spelling)", rec["agent_name"] == "Sam" and rec["qa_name"] == "Demo QA" and rec["call_ref"] == "T-1")
    today = rec["uploaded_at"][:10]
    check("server builds the audit name; typed customer details are not overwritten by the scorer",
          rec["audit_name"] == "Sam — Acme — Pat — " + today and rec["caller_company"] == "Acme" and rec["caller_name"] == "Pat", rec)
    check("customer email stored", rec["caller_email"] == "pat@acme.example")
    s, jobs3 = c.json("/api/jobs?q=acme.example")
    check("calls search matches the customer email", s == 200 and jobs3.get("total") == 1)
    s, b, hd = c.raw("/api/scorecards/%s/export.pdf" % j["scorecard_id"])
    check("PDF filename carries the audit name", s == 200 and "Sam-Acme-Pat" in hd.get("Content-Disposition", ""), hd.get("Content-Disposition"))
    q = "?filename=call2.wav&provider=demo&rubric_version_id=%s&qa=Demo%%20QA&audit_name=My%%20own%%20title" % rvid
    s, up2 = c.json("/api/recordings" + q, "POST", data=wav, headers={"Content-Type": "audio/wav"})
    j3 = wait_job(c, up2["job_id"])
    check("explicit audit name kept; agent optional", s == 201 and j3["recording"]["audit_name"] == "My own title" and j3["recording"]["agent_name"] is None)
    s, jobs2 = c.json("/api/jobs?q=own%20title")
    check("calls search matches the audit name", s == 200 and jobs2.get("total") == 1)
    s, upd = c.json("/api/recordings?filename=demo-run.wav&provider=deepgram&rubric_version_id=%s&run=demo&qa=Demo%%20QA" % rvid, "POST", data=wav, headers={"Content-Type": "audio/wav"})
    jd = wait_job(c, upd.get("job_id", ""))
    check("demo run: fake providers forced, tagged demo", s == 201 and jd.get("status") == "done" and jd["recording"]["kind"] == "demo"
          and jd["stt_provider"] == "demo" and jd["scoring_provider"] == "demo", jd)
    s, upt = c.json("/api/recordings?filename=test-call.wav&provider=demo&rubric_version_id=%s&run=test&agent=sam" % rvid, "POST", data=wav, headers={"Content-Type": "audio/wav"})
    jt = wait_job(c, upt.get("job_id", ""))
    check("test call: tagged test", s == 201 and jt.get("status") == "done" and jt["recording"]["kind"] == "test", jt.get("recording"))
    s, jk = c.json("/api/jobs?kind=demo")
    check("kind=demo lists the seeded call and the demo run only", s == 200 and jk["total"] == 2 and all(j["recording"]["kind"] == "demo" for j in jk["jobs"])
          and any(j["recording"]["call_ref"] == "DEMO-0001" for j in jk["jobs"]), [j["recording"]["filename"] for j in jk["jobs"]])
    s, jr = c.json("/api/jobs?kind=real")
    check("kind=real excludes demo and test", s == 200 and jr["total"] >= 3 and all(j["recording"]["kind"] == "real" for j in jr["jobs"]))
    s, jt2 = c.json("/api/jobs?kind=test")
    check("kind=test lists the test call", s == 200 and jt2["total"] == 1)
    sam = [p for p in c.json("/api/people?kind=agent")[1]["people"] if p["name"] == "Sam"][0]
    s, d = c.json("/api/people/%s/rename" % sam["id"], "POST", {"name": "Samir"})
    s2, jj = c.json("/api/jobs/%s" % j["id"])
    check("rename cascades to past calls", s == 200 and d.get("recordings_renamed") == 3 and jj["recording"]["agent_name"] == "Samir", d)
    s, _ = c.json("/api/people/%s/delete" % sam["id"], "POST", {})
    check("used name cannot be deleted (409)", s == 409)
    s, _ = c.json("/api/people/%s/rename" % sam["id"], "POST", {"name": "JORDAN"})
    check("rename to an existing name 409", s == 409)
    s, e = c.json("/api/recordings?filename=x.exe&provider=demo&rubric_version_id=%s" % rvid, "POST", data=wav)
    check("bad extension 400", s == 400 and ".exe" in e.get("error", ""))
    s, e = c.json("/api/recordings?filename=x.wav&provider=demo&rubric_version_id=%s" % rvid, "POST", data=b"", headers={"Content-Type": "audio/wav"})
    check("empty upload 400", s == 400)
    s, e = c.json("/api/recordings?filename=x.wav&provider=demo&rubric_version_id=nope", "POST", data=wav)
    check("unknown rubric version 400", s == 400)
    # oversize declared by Content-Length only (nothing is sent)
    def declared(size, provider):
        jar = c.jar
        tok = [ck.value for ck in jar if ck.name == "auditly_session"][0]
        hc = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
        hc.putrequest("POST", "/api/recordings?filename=x.mp3&provider=%s&rubric_version_id=%s" % (provider, rvid))
        hc.putheader("X-Auditly-CSRF", "1"); hc.putheader("Cookie", "auditly_session=" + tok)
        hc.putheader("Content-Length", str(size)); hc.endheaders()
        r = hc.getresponse(); body = r.read(); hc.close()
        return r.status, body.decode()
    s, body = declared(31 * 1024 * 1024, "demo")
    check("over AUDITLY_MAX_UPLOAD_MB -> 413", s == 413, (s, body))
    s, body = declared(26 * 1024 * 1024, "openai")
    check("openai > 25 MB -> 400 naming the cap", s in (400, 413) and "25" in body, (s, body))
    up_dir = env["AUDITLY_UPLOAD_DIR"]
    files = [f for f in os.listdir(up_dir)] if os.path.isdir(up_dir) else []
    check("no orphan files from rejected uploads (6 recordings)", len(files) == 6, files)
    check("upload dir is 0700", (os.stat(up_dir).st_mode & 0o777) == 0o700)

    # ── rescore + retry ───────────────────────────────────────────────
    s, same = c.json("/api/recordings/%s/rescore" % j["recording_id"], "POST", {"rubric_version_id": rvid})
    check("identical audit is offered instead of billed again", s == 200 and same.get("cached") is True and same.get("existing_job_id") == j["id"], same)
    s, rs = c.json("/api/recordings/%s/rescore" % j["recording_id"], "POST", {"rubric_version_id": rvid, "force": True})
    check("rescore queued", s == 201 and rs.get("job_id"))
    j2 = wait_job(c, rs["job_id"])
    check("rescore done reusing transcript", j2.get("status") == "done" and j2["stt_provider"] == "reuse")
    # carry over: a rescan must not throw away the reviewer's overrides and audit notes (0.20.0)
    src_sc = c.json("/api/scorecards/%s" % j["scorecard_id"])[1]
    crit_c = [i for i in src_sc["items"] if i["key"] == "troubleshooting"][0]
    s, _o = c.json("/api/scorecards/%s/items/%s/override" % (src_sc["id"], crit_c["criterion_id"]), "POST", {"score": 10, "note": "carried note"})
    s, _r = c.json("/api/scorecards/%s/review" % src_sc["id"], "POST", {"coaching_notes": "Coach on PoE checks.", "qa_name": "demo qa"})
    s, rsc = c.json("/api/recordings/%s/rescore" % j["recording_id"], "POST", {"rubric_version_id": rvid, "force": True, "carry_over": True})
    jc = wait_job(c, rsc.get("job_id", ""))
    new_sc = c.json("/api/scorecards/%s" % jc.get("scorecard_id", ""))[1]
    it_c = [i for i in new_sc.get("items", []) if i["key"] == "troubleshooting"]
    check("rescore with carry-over copies the override and the audit notes as a draft; the total follows",
          s == 201 and jc.get("status") == "done" and it_c and it_c[0]["override_score"] == 10 and it_c[0]["override_note"] == "carried note"
          and new_sc["overall_pct"] == 80.0 and (new_sc.get("review") or {}).get("status") == "draft"
          and new_sc["review"]["coaching_notes"] == "Coach on PoE checks." and "Carried over" in (jc.get("progress") or ""), (jc.get("progress"), it_c, new_sc.get("review")))
    s, rsn = c.json("/api/recordings/%s/rescore" % j["recording_id"], "POST", {"rubric_version_id": rvid, "force": True, "carry_over": False})
    jn = wait_job(c, rsn.get("job_id", ""))
    plain_sc = c.json("/api/scorecards/%s" % jn.get("scorecard_id", ""))[1]
    check("rescore without carry-over starts clean", jn.get("status") == "done" and plain_sc.get("review") is None
          and all(i["override_score"] is None for i in plain_sc["items"]))
    c.json("/api/scorecards/%s/items/%s/override" % (src_sc["id"], crit_c["criterion_id"]), "POST", {"score": None})
    dbc2 = core.connect(env["AUDITLY_DB"])
    dbc2.execute("DELETE FROM review WHERE scorecard_id IN (?,?)", (src_sc["id"], new_sc["id"])); dbc2.commit(); dbc2.close()
    s, tr = c.json("/api/recordings/%s/transcript" % j["recording_id"])
    check("still one transcript for the recording", s == 200)
    s, hh2 = c.json("/api/health")
    check("health lists the transcribe/audit menus", s == 200 and len(hh2.get("stt_choices", [])) == 3 and len(hh2.get("scoring_choices", [])) == 4
          and [x["model"] for x in hh2["scoring_choices"]][:3] == ["gpt-4o-mini", "gpt-5.6-luna", "gpt-4.1-mini"]
          and hh2["scoring_choices"][3]["provider"] == "anthropic" and hh2["default_stt_id"] == "deepgram:nova-3", hh2.get("scoring_choices"))
    s, rs2 = c.json("/api/recordings/%s/rescore" % j["recording_id"], "POST", {"rubric_version_id": rvid, "scorer": "openai:gpt-4.1-mini"})
    j2b = wait_job(c, rs2.get("job_id", ""))
    check("audit again with a chosen LLM is recorded on the job", s == 201 and j2b.get("status") == "done" and j2b["scoring_model"] == "gpt-4.1-mini" and j2b["stt_provider"] == "reuse", j2b)
    s, e = c.json("/api/recordings/%s/rescore" % j["recording_id"], "POST", {"rubric_version_id": rvid, "scorer": "openai:gpt-9"})
    check("unknown audit model 400", s == 400 and "audit model" in e.get("error", "").lower())
    old_sc = c.json("/api/scorecards/%s" % j["scorecard_id"])[1]
    s, rt = c.json("/api/recordings/%s/retranscribe" % j["recording_id"], "POST", {"stt": "openai:gpt-4o-transcribe"})
    j3 = wait_job(c, rt.get("job_id", ""))
    new_sc = c.json("/api/scorecards/%s" % j3.get("scorecard_id", ""))[1]
    check("transcribe again: fresh transcript, engine recorded, old scorecard untouched",
          s == 201 and j3.get("status") == "done" and j3["transcript_mode"] == "fresh" and j3["stt_provider"] == "openai" and j3["stt_model"] == "gpt-4o-transcribe"
          and new_sc.get("transcript", {}).get("id") != old_sc.get("transcript", {}).get("id"), (j3.get("transcript_mode"), j3.get("stt_model")))
    s, e = c.json("/api/recordings/%s/retranscribe" % j["recording_id"], "POST", {"stt": "google:chirp"})
    check("unknown transcription engine 400", s == 400 and "engine" in e.get("error", "").lower())
    # one submitted audit per recording, whichever run it is on (0.14.9)
    s, k1 = c.json("/api/scorecards/%s/review/submit" % j2["scorecard_id"], "POST", {"action_plan": "run A"})
    s2, k2 = c.json("/api/scorecards/%s/review/submit" % j3["scorecard_id"], "POST", {"action_plan": "run B"})
    check("submitting a second run of an audited call is 409", s == 200 and s2 == 409 and "another run" in k2.get("error", ""), (s, s2, k2))
    c.json("/api/scorecards/%s/review/reopen" % j2["scorecard_id"], "POST", {})
    s3, k3 = c.json("/api/scorecards/%s/review/submit" % j3["scorecard_id"], "POST", {})
    check("after reopening the first, the other run can be submitted", s3 == 200, (s3, k3))
    c.json("/api/scorecards/%s/review/reopen" % j3["scorecard_id"], "POST", {})
    dbk = core.connect(env["AUDITLY_DB"])
    dbk.execute("DELETE FROM review WHERE scorecard_id IN (?,?)", (j2["scorecard_id"], j3["scorecard_id"])); dbk.commit(); dbk.close()
    s, hh3 = c.json("/api/health")
    sp = hh3.get("spend") or {}
    check("health reports spend totals", s == 200 and sp.get("jobs", 0) >= 3 and sp.get("cached_transcripts", 0) >= 1 and "est_usd" in sp, sp)
    s, e = c.json("/api/jobs/%s/retry" % j2["id"], "POST", {})
    check("retry of a done job refused", s == 400)
    # force a failure path: job for a rubric whose criteria we can't lose -- use worker directly
    import worker
    db = core.connect(env["AUDITLY_DB"])
    fake_rec = core.new_id()
    db.execute("INSERT INTO recording (id,filename,ext,mime,bytes,path,uploaded_at) VALUES (?,?,?,?,?,?,?)",
               (fake_rec, "gone.wav", "wav", "audio/wav", 1, "/nonexistent/gone.wav", core.now()))
    fj = core.new_id()
    db.execute("INSERT INTO job (id,recording_id,rubric_version_id,stt_provider,status,created_at) VALUES (?,?,?,?,'queued',?)",
               (fj, fake_rec, rvid, "demo", core.now()))
    db.close()
    os.environ["AUDITLY_DB"] = env["AUDITLY_DB"]; os.environ["AUDITLY_UPLOAD_DIR"] = env["AUDITLY_UPLOAD_DIR"]
    worker.process(fj, dict(core.cfg(), AUDITLY_DEMO="1"))
    s, fjob = c.json("/api/jobs/" + fj)
    check("missing audio -> failed with message", fjob.get("status") == "failed" and "no longer available" in (fjob.get("error") or ""), fjob)
    s, nf = c.json("/api/notifications")
    nf_fail = [n for n in nf.get("items", []) if n["kind"] == "job.failed" and n["target_id"] == fj]
    check("a failed job lands in the notification feed, unread, pointing at the job (0.51.0)", s == 200 and bool(nf_fail) and nf_fail[0]["read"] is False
          and nf_fail[0]["target_kind"] == "job" and nf.get("unread", 0) >= 1 and "no longer available" in (nf_fail[0]["body"] or ""), nf.get("items", [])[:2])
    s, e = c.json("/api/jobs/%s/retry" % fj, "POST", {"provider": "google"})
    check("retry with an unknown provider is 400, not silently Deepgram", s == 400 and "provider" in e.get("error", "").lower(), (s, e))
    s, e = c.json("/api/jobs/%s/retry" % fj, "POST", {"provider": 5})
    check("retry with a non-string provider is 400, not 500", s == 400, s)
    s, e = c.json("/api/jobs/%s/retry" % fj, "POST", {})
    check("failed job can be retried", s == 200)
    s, e = c.json("/api/jobs/%s/delete" % fj, "POST", {})
    check("admin delete job", s == 200)
    check("deleting the job takes its failure notification away", not [n for n in c.json("/api/notifications")[1]["items"] if n["target_id"] == fj])
    s, _ = c.json("/api/jobs/" + fj)
    check("deleted job is 404", s == 404)
    # a job killed on its last attempt must become retryable at boot, not stay 'scoring' forever (0.14.11)
    import transcribe
    db = core.connect(env["AUDITLY_DB"])
    stuck = core.new_id()
    db.execute("INSERT INTO recording (id,filename,ext,mime,bytes,path,uploaded_at) VALUES (?,?,?,?,?,?,?)",
               (stuck, "stuck.wav", "wav", "audio/wav", 1, "/nonexistent/stuck.wav", core.now()))
    sj = core.new_id()
    db.execute("INSERT INTO job (id,recording_id,rubric_version_id,stt_provider,status,attempts,created_at) VALUES (?,?,?,?,'scoring',?,?)",
               (sj, stuck, rvid, "demo", worker.MAX_ATTEMPTS, core.now()))
    worker.requeue_unfinished(db)
    row = db.execute("SELECT status, error FROM job WHERE id=?", (sj,)).fetchone()
    check("job interrupted on its last attempt is failed at boot with a Retry hint", row["status"] == "failed" and "Retry" in (row["error"] or ""), dict(row))
    db.execute("DELETE FROM job WHERE id=?", (sj,)); db.execute("DELETE FROM recording WHERE id=?", (stuck,))
    # retention is measured from uploaded_at, not the file's mtime (0.14.12)
    old_f = os.path.join(env["AUDITLY_UPLOAD_DIR"], "old-upload.wav"); new_f = os.path.join(env["AUDITLY_UPLOAD_DIR"], "new-upload.wav")
    for f in (old_f, new_f):
        with open(f, "wb") as fh:
            fh.write(b"RIFF")
    os.utime(new_f, (time.time() - 400 * 86400, time.time() - 400 * 86400))       # old mtime, fresh upload
    old_r, new_r = core.new_id(), core.new_id()
    db.execute("INSERT INTO recording (id,filename,ext,mime,bytes,path,uploaded_at) VALUES (?,?,?,?,?,?,?)",
               (old_r, "old.wav", "wav", "audio/wav", 4, old_f, "2025-01-01T00:00:00+00:00"))
    db.execute("INSERT INTO recording (id,filename,ext,mime,bytes,path,uploaded_at) VALUES (?,?,?,?,?,?,?)",
               (new_r, "new.wav", "wav", "audio/wav", 4, new_f, core.now()))
    db.commit()
    n_del = worker.retention_sweep(db, 90)
    check("retention deletes by upload date: old upload with a fresh mtime goes, fresh upload with an old mtime stays",
          n_del == 1 and not os.path.exists(old_f) and os.path.exists(new_f)
          and db.execute("SELECT audio_deleted_at FROM recording WHERE id=?", (old_r,)).fetchone()[0], (n_del, os.path.exists(old_f), os.path.exists(new_f)))
    db.execute("DELETE FROM recording WHERE id IN (?,?)", (old_r, new_r)); db.commit(); db.close()
    os.unlink(new_f)
    red = core.redact_paths("[Errno 2] No such file or directory: '%s/abc.mp3' and /tmp/other/x.wav" % env["AUDITLY_UPLOAD_DIR"], core.cfg())
    check("job error redaction keeps the file name, drops every absolute path",
          "uploads/abc.mp3" in red and env["AUDITLY_UPLOAD_DIR"] not in red and "/tmp/other" not in red, red)
    try:
        transcribe.transcriber_for(dict(core.cfg(), AUDITLY_DEMO="0"), "whisper")
        unk = False
    except ValueError:
        unk = True
    check("unknown transcription provider name raises instead of silently using Deepgram", unk)

    # ── "Optimise for the scorer" (rubric sync) ───────────────────────
    s, sr = c.json("/api/rubrics", "POST", {"name": "Sync rubric", "source_text": "# Greeting\nState company and own name; offer help.\n# Verification\nAccount number or PIN.", "criteria": [
        {"name": "Greeting", "weight": 40, "description": "Opens the call properly.", "guidance_met": "States company and own name; offers help.",
         "guidance_partial": "Two of the three.", "guidance_missed": "None of the three."},
        {"name": "Verification", "weight": 60, "critical": True, "guidance_met": "Account number or PIN confirmed.",
         "guidance_partial": "Business name only.", "guidance_missed": "No attempt."}]})
    check("saving a QA type does NOT optimise it: 201 with sync_status none", s == 201 and sr.get("sync_status") == "none", sr)
    srid = sr["id"]
    s, sy0 = c.json("/api/rubrics/%s/versions/1/sync" % srid)
    check("sync precheck: shape, caps (1 per version, 5 per day), runnable, cost estimate, demo model",
          s == 200 and {"version_id", "rubric_id", "version_no", "sync_status", "sync_error", "sync_attempts", "sync_runs", "sync_started_at",
                        "sync_finished_at", "sync_provider", "sync_model", "max_attempts", "optimised_scorecards", "runs_used", "runs_max",
                        "today_used", "today_max", "can_run", "reason", "est_usd", "model", "provider", "criteria_count"} == set(sy0)
          and sy0["sync_status"] == "none" and sy0["runs_max"] == 1 and sy0["today_max"] == 5 and sy0["runs_used"] == 0 and sy0["can_run"] is True
          and sy0["reason"] is None and sy0["est_usd"] == 0 and sy0["model"] == "demo" and sy0["criteria_count"] == 2, sy0)
    lst0 = [r for r in c.json("/api/rubrics")[1]["rubrics"] if r["id"] == srid][0]
    check("QA type list carries the current version's sync status (none = not optimised)", lst0.get("sync_status") == "none", lst0)
    s, go = c.json("/api/rubrics/%s/versions/1/sync" % srid, "POST", {})
    check("POST sync: 202, pending, queued", s == 202 and go["sync_status"] in ("pending", "running", "done"), go)
    for _ in range(60):
        s, sy = c.json("/api/rubrics/%s/versions/1/sync" % srid)
        if sy.get("sync_status") in ("done", "failed"):
            break
        time.sleep(0.1)
    check("demo optimisation finishes done in one attempt within 6s", s == 200 and sy["sync_status"] == "done" and sy["sync_attempts"] == 1 and sy["sync_runs"] == 1
          and sy["sync_error"] is None and sy["sync_provider"] == "demo" and sy["sync_model"] == "demo" and sy["sync_started_at"] and sy["sync_finished_at"], sy)
    check("after the run the per-version cap is used up and the button is refused with the reason",
          sy["can_run"] is False and "Save a new version" in (sy["reason"] or "") and sy["runs_used"] == 1 and sy["today_used"] >= 1, sy)
    s, sv = c.json("/api/rubrics/%s/versions/1" % srid)
    check("every criterion has five optimised fields; authored fields, keys, weights and critical are untouched",
          s == 200 and sv["sync_status"] == "done"
          and all(all((x.get(f) or "").strip() for f in ("opt_description", "opt_met", "opt_partial", "opt_missed", "opt_na")) for x in sv["criteria"])
          and [(x["key"], x["weight"], x["critical"]) for x in sv["criteria"]] == [("greeting", 40, False), ("verification", 60, True)]
          and sv["criteria"][0]["guidance_met"] == "States company and own name; offers help." and sv["criteria"][0]["opt_met"].startswith("ALL of:")
          and sv["optimised_scorecards"] == 0, sv)
    lst1 = [r for r in c.json("/api/rubrics")[1]["rubrics"] if r["id"] == srid][0]
    check("QA type list shows the version optimised", lst1.get("sync_status") == "done", lst1)
    s, e = c.json("/api/rubrics/%s/versions/1/sync" % srid, "POST", {})
    check("a second run on the same version is refused: 429 with the per-version cap message", s == 429 and "Save a new version" in e.get("error", ""), e)
    s, rsy = c.json("/api/recordings/%s/rescore" % j["recording_id"], "POST", {"rubric_version_id": sv["id"], "force": True})
    jsy = wait_job(c, rsy.get("job_id", ""))
    scx = c.json("/api/scorecards/%s" % jsy.get("scorecard_id", ""))[1]
    check("a call audited against the optimised version records guidance_mode optimised and prompt v8, progress named the rules",
          s == 201 and jsy.get("status") == "done" and scx.get("guidance_mode") == "optimised" and scx.get("prompt_version") == 8
          and set(i["key"] for i in scx["items"]) == {"greeting", "verification"}, (jsy.get("status"), scx.get("guidance_mode"), scx.get("prompt_version")))
    s, sy2 = c.json("/api/rubrics/%s/versions/1/sync" % srid)
    check("the version now counts one optimised scorecard; re-optimising is refused for that reason too",
          sy2["optimised_scorecards"] == 1 and sy2["can_run"] is False, sy2)
    s, same2 = c.json("/api/recordings/%s/rescore" % j["recording_id"], "POST", {"rubric_version_id": sv["id"]})
    check("audit-again cache matches on guidance mode: the optimised audit is offered for reuse", s == 200 and same2.get("cached") is True, same2)
    s, sv2 = c.json("/api/rubrics/%s/versions" % srid, "POST", {"criteria": [{"name": "Greeting", "weight": 50}, {"name": "Verification", "weight": 50}]})
    s2, sy3 = c.json("/api/rubrics/%s/versions/2/sync" % srid)
    check("a new version starts un-optimised with its own run allowance", s == 201 and sv2.get("sync_status") == "none" and s2 == 200
          and sy3["sync_status"] == "none" and sy3["runs_used"] == 0 and sy3["can_run"] is True, (sv2, sy3))
    s, nf = c.json("/api/rubrics/%s/versions/9/sync" % srid, "POST", {})
    check("sync on a missing version is 404", s == 404)
    # the daily cap counts requests in the audit log; 5 seeded requests today -> refused
    import rubric_sync as sync_mod
    dbs = core.connect(env["AUDITLY_DB"])
    for i in range(5):
        core.audit(dbs, "rubric.sync.requested", target="cap-test-%d" % i, detail="cap test")
    vrow = dbs.execute("SELECT * FROM rubric_version WHERE id=?", (sy3["version_id"],)).fetchone()
    capd = sync_mod.caps(dbs, dict(core.cfg(), AUDITLY_SYNC_MAX_RUNS_PER_DAY="5", AUDITLY_SYNC_MAX_RUNS_PER_VERSION="1"), vrow)
    s, capped = c.json("/api/rubrics/%s/versions/2/sync" % srid, "POST", {})
    dbs.execute("DELETE FROM audit WHERE detail='cap test'"); dbs.commit(); dbs.close()
    check("daily cap: caps() refuses and POST sync answers 429 naming the day limit", capd["allowed"] is False and "today" in (capd["reason"] or "")
          and s == 429 and "today" in capped.get("error", ""), (capd, s, capped))
    r_ev = subprocess.run([sys.executable, os.path.join(ROOT, "auditly_host.py"), "--eval-determinism", "latest", "--version-id", sv["id"], "--runs", "2"],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=90)
    check("--eval-determinism runs in demo (nothing written), reports both modes and a verdict", r_ev.returncode == 0 and "authored" in r_ev.stdout
          and "optimised" in r_ev.stdout and "Verdict" in r_ev.stdout and "consistent" in r_ev.stdout and "Nothing is written" in r_ev.stdout,
          (r_ev.returncode, r_ev.stdout[-500:], r_ev.stderr[-300:]))
    s, jobs_after = c.json("/api/jobs")
    check("the eval wrote no job", all(jb["id"] != "x" for jb in jobs_after["jobs"]) and c.json("/api/rubrics/%s/versions/1/sync" % srid)[1]["optimised_scorecards"] == 1)

    # ── scoring maths (unit) ──────────────────────────────────────────
    crit = [{"id": "1", "key": "a", "name": "A", "weight": 60, "critical": 1},
            {"id": "2", "key": "b", "name": "B", "weight": 40, "critical": 0}]
    res = {"agent_speaker": "S1", "auto_fail": False, "summary": "s", "strengths": [],
           "opportunities": [{"text": "o", "key": "zzz"}], "misses": [],
           "items": [{"key": "a", "rating": "partial", "score": 999, "rationale": "", "evidence": [
                        {"ts": "00:00", "speaker": "S0", "quote": "hello there"}]},
                     {"key": "zzz", "rating": "met", "score": 1, "rationale": "", "evidence": []}]}
    clean, warns = score_mod.validate_result(res, crit, "[00:00] S0: hello there")
    resf = dict(res, call_facts={"identifiers": {"phone": "555 0100", "extension": "101", "mac_address": "AA:BB:CC:DD:EE:FF", "ticket": "T-48213", "account": None,
                                                 "device": "front desk phone", "address": None},
                                 "troubleshooting": [{"text": "reseated the cable at the wall jack", "result": "phone re-registered", "key": crit[0]["key"]},
                                                     {"text": "power-cycled the handset", "result": None, "key": "not_a_criterion"}],
                                 "discussed": [{"text": "noted the fault on the account", "key": None}], "outcome": "fixed on the call"})
    cf_clean, cf_w = score_mod.validate_result(resf, crit, "[00:00] S0: hello there, the front desk phone on extension 101 -- mac address is AA BB CC DD EE FF, your ticket is T 48213")
    cfi = cf_clean["call_facts"]["identifiers"]
    check("call facts are deterministic (0.61.0): an identifier not heard on the call is dropped with a warning; separators and case are ignored; a step's key must be a criterion",
          cfi["phone"] is None and cfi["extension"] == "101" and cfi["mac_address"] == "AA:BB:CC:DD:EE:FF" and cfi["ticket"] == "T-48213" and cfi["device"] == "front desk phone"
          and any("Phone" in w and "not heard" in w for w in cf_w)
          and cf_clean["call_facts"]["troubleshooting"][0]["key"] == crit[0]["key"] and cf_clean["call_facts"]["troubleshooting"][1]["key"] is None
          and cf_clean["call_facts"]["outcome"] == "fixed on the call"
          and (cf_clean["customer"] or {}).get("reference") == "T-48213", (cfi, cf_w))
    check("no facts -> None, and the schema requires the block", score_mod.validate_result(dict(res, call_facts={"identifiers": {}, "troubleshooting": [], "discussed": [], "outcome": None}), crit, "")[0]["call_facts"] is None)
    a = [i for i in clean["items"] if i["key"] == "a"][0]
    b = [i for i in clean["items"] if i["key"] == "b"][0]
    check("partial score clamped below weight", 0 <= a["score"] < 60, a["score"])
    check("missing criterion inserted as missed", b["rating"] == "missed" and b["score"] == 0)
    check("unknown key dropped with warning", any("unknown" in w for w in warns))
    check("opportunity with unknown key keeps text, drops key", clean["opportunities"][0]["key"] is None)
    check("agent_speaker parsed from 'S1'", clean["agent_speaker"] == 1)
    check("evidence timestamps normalised to mm:ss (brackets, h:mm:ss, junk)",
          [score_mod.normalise_ts(t) for t in ("[01:13]", "01:13", "1:02:03", "[ 00:05 ]", "abc", "01:75", None)]
          == ["01:13", "01:13", "62:03", "00:05", "", "", ""])
    res_ts = dict(res, items=[{"key": "a", "rating": "met", "score": 60, "rationale": "", "evidence": [{"ts": "[00:00]", "speaker": "S0", "quote": "hello there"}]}])
    check("a bracketed timestamp from the model is stored bare",
          score_mod.validate_result(res_ts, crit, "[00:00] S0: hello there")[0]["items"][0]["evidence"][0]["ts"] == "00:00")
    try:
        score_mod.validate_result(["not", "an", "object"], crit, "")
        lst = False
    except ValueError:
        lst = True
    check("a JSON list from the model is a clean ValueError, not a traceback", lst)
    res_na = dict(res, items=[{"key": "a", "rating": "na", "score": 0, "rationale": "", "evidence": []},
                              {"key": "b", "rating": "na", "score": 0, "rationale": "", "evidence": []}])
    clean_na, _w = score_mod.validate_result(res_na, crit, "")
    check("all criteria n/a: basis 0, no ZeroDivision, no auto-fail",
          score_mod.compute_overall(clean_na["items"]) == (0.0, 0) and clean_na["auto_fail"] is False)
    pipe_rows = [{"uploaded_at": "2026-09-01T10:00:00+00:00", "agent_name": "A", "qa_name": "Q", "transcribed": 1, "scored": 1, "audited": 0, "coached": 0},
                 {"uploaded_at": "2026-09-02T10:00:00+00:00", "agent_name": "A", "qa_name": "Q", "transcribed": 1, "scored": 1, "audited": 1, "coached": 1},
                 {"uploaded_at": "2026-01-02T10:00:00+00:00", "agent_name": "A", "qa_name": "Q", "transcribed": 1, "scored": 1, "audited": 1, "coached": 0},
                 {"uploaded_at": "2026-09-03T10:00:00+00:00", "agent_name": "B", "qa_name": "Q", "transcribed": 0, "scored": 0, "audited": 0, "coached": 0}]
    import datetime as _dt2
    check("pipeline counts follow the Flow steps for the range and agent scope",
          dashboard.pipeline(pipe_rows, _dt2.date(2026, 8, 20), _dt2.date(2026, 9, 8)) == {"recorded": 3, "transcribed": 2, "scored": 2, "awaiting_audit": 1, "audited": 1, "coached": 1}
          and dashboard.pipeline(pipe_rows, _dt2.date(2026, 8, 20), _dt2.date(2026, 9, 8), agent="a")["recorded"] == 2)
    check("dashboard treats a basis-0 call as unscored (excluded from averages)",
          dashboard._value({"overall_pct": 0.0, "applicable_weight": 0}) is None and dashboard._avg([None, 80.0, 70.0]) == 75.0)
    res2 = {"agent_speaker": None, "auto_fail": False, "summary": "", "strengths": [], "opportunities": [], "misses": [],
            "items": [{"key": "a", "rating": "missed", "score": 0, "rationale": "", "evidence": []},
                      {"key": "b", "rating": "na", "score": 0, "rationale": "", "evidence": []}]}
    clean2, warns2 = score_mod.validate_result(res2, crit, "")
    check("critical missed forces auto_fail", clean2["auto_fail"] is True)
    pct, basis = score_mod.compute_overall(clean2["items"])
    check("na excluded from basis", basis == 60 and pct == 0.0)
    pct, basis = score_mod.compute_overall([{"rating": "met", "score": 60, "weight": 60},
                                            {"rating": "na", "score": 0, "weight": 40}])
    check("met with na basis -> 100%", pct == 100.0 and basis == 60)
    pct, _ = score_mod.compute_overall([{"rating": "met", "score": 60, "weight": 60, "override_score": 30},
                                        {"rating": "met", "score": 40, "weight": 40}])
    check("override honoured in overall", pct == 70.0)
    res3 = dict(res2); res3["items"] = [{"key": "a", "rating": "met", "score": 60, "rationale": "", "evidence": [
        {"ts": "00:01", "speaker": "S0", "quote": "this sentence is not in the transcript at all"}]},
        {"key": "b", "rating": "met", "score": 40, "rationale": "", "evidence": []}]
    clean3, warns3 = score_mod.validate_result(res3, crit, "[00:00] S0: hello there, how can I help")
    check("fabricated quote dropped with warning", clean3["items"][0]["evidence"] == [] and any("not found" in w for w in warns3))
    text, trunc = score_mod.format_transcript([{"speaker": 0, "start_s": 0, "text": "x" * 500}], 200)
    check("transcript middle-truncation flagged", trunc and "truncated" in text)
    items = rubric_mod.normalise([{"name": "A", "weight": 33}, {"name": "B", "weight": 33}, {"name": "C", "weight": 33}])
    items, changed = rubric_mod.rescale(items)
    check("rescale to 100 by largest remainder", changed and sum(i["weight"] for i in items) == 100)
    check("slug keys unique", len({i["key"] for i in rubric_mod.normalise([{"name": "A"}, {"name": "A"}])}) == 2)
    sch = score_mod.schema_for(["a", "b"])
    check("schema requires every criterion as a named field",
          sch["properties"]["items"]["type"] == "object" and sch["properties"]["items"]["required"] == ["a", "b"]
          and sch["properties"]["items"]["additionalProperties"] is False and "call_summary" in sch["required"])
    dict_form = dict(res2, items={"a": {"rating": "met", "score": 60, "rationale": "", "evidence": []},
                                  "b": {"rating": "missed", "score": 0, "rationale": "", "evidence": []}})
    clean4, warns4 = score_mod.validate_result(dict_form, crit, "")
    check("validate_result accepts items keyed by criterion", score_mod.compute_overall(clean4["items"])[0] == 60.0 and not warns4, warns4)
    check("missing_keys spots a skipped criterion", score_mod.missing_keys({"items": {"a": {}}}, crit) == ["b"])
    check("schema requires sentiment and call_reasons (strict mode)", "sentiment" in sch["required"] and "call_reasons" in sch["required"]
          and sch["properties"]["call_reasons"]["items"]["properties"]["category"]["enum"] == list(score_mod.REASON_KEYS)
          and score_mod.PROMPT_VERSION == 8 and "call_facts" in sch["required"])
    # "Optimise for the scorer" -- prompt, validation, demo branch, seed, migration (0.28.0)
    import llm as llm_mod
    ocrit = [{"id": "1", "key": "a", "name": "A", "weight": 60, "critical": 1, "description": "d", "guidance_met": "m", "guidance_partial": "p", "guidance_missed": "x",
              "opt_description": "OD", "opt_met": "ALL of: (1) x.", "opt_partial": "OP", "opt_missed": "OM", "opt_na": "Never; this criterion applies to every call."},
             {"id": "2", "key": "b", "name": "B", "weight": 40, "critical": 0, "description": "d2", "guidance_met": "m2", "guidance_partial": "p2", "guidance_missed": "x2"}]
    _, u_opt = score_mod.build_prompt("R", 1, ocrit[:1], "T", True, use_optimised=True)
    _, u_auth = score_mod.build_prompt("R", 1, ocrit[:1], "T", True)
    check("build_prompt prefers opt_* and emits na: only in optimised mode, and says so in the header",
          "met: ALL of: (1) x." in u_opt and "\n  na: Never" in u_opt and "apply rule 11 literally" in u_opt
          and "met: m\n" in u_auth and "na:" not in u_auth and "rule 11" not in u_auth, (u_opt[:300], u_auth[:300]))
    _, u_fb = score_mod.build_prompt("R", 1, ocrit[1:], "T", True, use_optimised=True)
    check("build_prompt falls back to the authored text when opt_* is NULL", "met: m2" in u_fb and "na:" not in u_fb)
    check("guidance_mode needs status done AND complete opt fields on every criterion",
          score_mod.guidance_mode({"sync_status": "done"}, ocrit[:1]) == "optimised" and score_mod.guidance_mode({"sync_status": "done"}, ocrit) == "authored"
          and score_mod.guidance_mode({"sync_status": "failed"}, ocrit[:1]) == "authored" and score_mod.guidance_mode(None, ocrit[:1]) == "authored")
    check("SYSTEM rule 11 tells the scorer to apply listed conditions literally", "11. Where a criterion lists explicit conditions" in score_mod.SYSTEM and "12. Return only JSON" in score_mod.SYSTEM)
    good = {"a": {"description": "D" * 20, "met": "ALL of: (1) says hi.", "partial": "P" * 20, "missed": "M" * 20, "na": "Never; applies to every call."}}
    ok_clean, ok_p = rubric_mod.validate_optimised(good, ocrit[:1])
    check("validate_optimised accepts a clean answer and collapses whitespace", not ok_p and ok_clean["a"]["met"].startswith("ALL of"), ok_p)
    pre = rubric_mod.validate_optimised({"a": dict(good["a"], met="The agent greets the caller.")}, ocrit[:1])
    pre2 = rubric_mod.validate_optimised({"a": dict(good["a"], met="Verifies the following: (1) name; (2) company.")}, ocrit[:1])
    check("validate_optimised normalises a 'met' that skipped the literal prefix instead of rejecting it (a whole run was lost to wording on 2026-09-15)",
          not pre[1] and pre[0]["a"]["met"] == "ALL of: (1) The agent greets the caller." and not pre2[1] and pre2[0]["a"]["met"] == "ALL of: Verifies the following: (1) name; (2) company.", (pre, pre2))
    check("validate_optimised rejects missing key, extra key, empty field, points talk, %, weight, over-long, non-object",
          rubric_mod.validate_optimised({}, ocrit[:1])[1]
          and "Unknown" in rubric_mod.validate_optimised(dict(good, zz=good["a"]), ocrit[:1])[1][0]
          and any("too short" in p for p in rubric_mod.validate_optimised({"a": dict(good["a"], na="")}, ocrit[:1])[1])
          and any("scoring" in p for p in rubric_mod.validate_optimised({"a": dict(good["a"], met="ALL of: worth 10 points")}, ocrit[:1])[1])
          and any("scoring" in p for p in rubric_mod.validate_optimised({"a": dict(good["a"], partial="gets 50% of the credit")}, ocrit[:1])[1])
          and any("scoring" in p for p in rubric_mod.validate_optimised({"a": dict(good["a"], missed="the weight is lost")}, ocrit[:1])[1])
          and any("limit" in p for p in rubric_mod.validate_optimised({"a": dict(good["a"], missed="M" * 601)}, ocrit[:1])[1])
          and "instead of a JSON object" in rubric_mod.validate_optimised(["x"], ocrit[:1])[1][0])
    sch_o = rubric_mod.optimise_schema(["a", "b"])
    check("optimise schema is strict: one required object per key with exactly the five fields, no title keyword",
          sch_o["additionalProperties"] is False and sch_o["required"] == ["a", "b"] and "title" not in sch_o
          and sch_o["properties"]["a"]["required"] == list(rubric_mod.OPT_FIELDS) and sch_o["properties"]["a"]["additionalProperties"] is False)
    u_o = rubric_mod.optimise_user("R", 1, ocrit, "src text")
    demo_o = llm_mod.DemoScorer().complete_json(rubric_mod.OPTIMISE_SYSTEM, u_o, sch_o)
    check("DemoScorer answers the optimise schema deterministically, validly, and does not mistake it for a scorecard",
          set(demo_o) == {"a", "b"} and not rubric_mod.validate_optimised(demo_o, ocrit)[1] and "items" not in demo_o
          and demo_o == llm_mod.DemoScorer().complete_json(rubric_mod.OPTIMISE_SYSTEM, u_o, sch_o), demo_o)
    check("optimise_user carries the source and every authored field under fixed delimiters",
          "SOURCE GUIDELINES:\n<<<\nsrc text\n>>>" in u_o and "### key=a | A | CRITICAL\ndescription: d\nmet: m\npartial: p\nmissed: x" in u_o)
    oc, ncalls = rubric_mod.optimise_criteria(ocrit, llm_mod.DemoScorer(), "R", 1, "src", usage={"calls": 0})
    check("optimise_criteria returns clean rules in one call with the demo scorer", ncalls == 1 and set(oc) == {"a", "b"} and oc["a"]["met"].startswith("ALL of"))
    check("opt_complete is true only when all five fields are present on every criterion", rubric_mod.opt_complete(ocrit[:1]) and not rubric_mod.opt_complete(ocrit))
    p_seed = llm_mod.OpenAIScorer({"OPENAI_API_KEY": "sk-x", "SCORING_SEED": "42"}, "gpt-4o-mini").payload("s", "u", {"type": "object"}, "r", 1)
    p_noseed = llm_mod.OpenAIScorer({"OPENAI_API_KEY": "sk-x", "SCORING_SEED": "42"}, "gpt-5.6-luna").payload("s", "u", {"type": "object"}, "r", 1)
    p_blank = llm_mod.OpenAIScorer({"OPENAI_API_KEY": "sk-x", "SCORING_SEED": ""}, "gpt-4o-mini").payload("s", "u", {"type": "object"}, "r", 1)
    check("seed is sent only where temperature is, and only when set", p_seed.get("seed") == 42 and p_seed.get("temperature") == 0 and "seed" not in p_noseed and "seed" not in p_blank)
    check("redact_secrets masks anything that looks like an API key", core.redact_secrets("bad key sk-proj-abcdefghijklmnopqrstuvwxyz1234 (401)") == "bad key sk-… (401)"
          and core.redact_secrets("sk-x is short") == "sk-x is short" and core.redact_secrets(None) is None)
    check("new config keys are known (a typo in .env fails loudly)", all(k in core.DEFAULTS for k in ("SCORING_SEED", "AUDITLY_SYNC_MAX_RUNS_PER_VERSION", "AUDITLY_SYNC_MAX_RUNS_PER_DAY", "AUDITLY_SYNC_WAIT_S", "AUDITLY_VOICE_AI_NAME")))
    check("readiness: demo runs; spend off refuses with a plain reason; bad key refuses naming the variable",
          sync_mod.readiness({"AUDITLY_DEMO": "1"}) is None
          and "AUDITLY_ALLOW_SPEND" in (sync_mod.readiness({"AUDITLY_DEMO": "0", "AUDITLY_ALLOW_SPEND": "0", "SCORING_PROVIDER": "openai", "OPENAI_API_KEY": "sk-" + "a" * 50}) or "")
          and "OPENAI_API_KEY" in (sync_mod.readiness({"AUDITLY_DEMO": "0", "AUDITLY_ALLOW_SPEND": "1", "SCORING_PROVIDER": "openai", "OPENAI_API_KEY": "nope"}) or ""))
    # migration: a database from before 0.28.0 gains the columns and its versions read 'none' (not optimised); nothing is queued
    import sqlite3 as _sq
    mig = os.path.join(tmp, "old.db")
    mdb = core.connect(mig)
    mdb.executescript(open(core.SCHEMA_PATH, encoding="utf-8").read())
    for col in ("sync_status", "sync_error", "sync_attempts", "sync_runs", "sync_started_at", "sync_finished_at", "sync_provider", "sync_model", "sync_usage_json"):
        mdb.execute("ALTER TABLE rubric_version DROP COLUMN %s" % col)
    for col in ("opt_description", "opt_met", "opt_partial", "opt_missed", "opt_na"):
        mdb.execute("ALTER TABLE criterion DROP COLUMN %s" % col)
    mdb.execute("ALTER TABLE scorecard DROP COLUMN guidance_mode")
    mdb.execute("INSERT INTO rubric (id,name,created_at) VALUES ('r1','Old','2026-01-01T00:00:00+00:00')")
    mdb.execute("INSERT INTO rubric_version (id,rubric_id,version_no,created_at) VALUES ('v1','r1',1,'2026-01-01T00:00:00+00:00')")
    core._migrate(mdb)
    old_v = mdb.execute("SELECT sync_status, sync_runs, sync_error FROM rubric_version WHERE id='v1'").fetchone()
    mig_n = sync_mod.requeue_unfinished(mdb)
    mdb.close()
    check("migration: pre-0.28.0 versions read 'none' with zero runs and nothing to re-queue",
          tuple(old_v) == ("none", 0, None) and mig_n == 0, (tuple(old_v), mig_n))
    res5 = dict(res2, sentiment={"start": "angry", "end": "positive", "overall": "positive"},
                call_reasons=[{"category": "no_service", "detail": "x" * 300}, {"category": "bogus", "detail": ""},
                              {"category": "no_service", "detail": "dup"}, {"category": "voicemail", "detail": ""},
                              {"category": "billing", "detail": ""}, {"category": "porting", "detail": ""},
                              {"category": "outage", "detail": ""}])
    clean5, warns5 = score_mod.validate_result(res5, crit, "")
    check("sentiment: unknown slot dropped, others kept", clean5["sentiment"] == {"start": None, "end": "positive", "overall": "positive"})
    check("call reasons: unknown dropped with warning, deduped, capped at 4, detail trimmed",
          [x["category"] for x in clean5["call_reasons"]] == ["no_service", "voicemail", "billing", "porting"]
          and len(clean5["call_reasons"][0]["detail"]) == 200 and any("unknown call reason" in w for w in warns5), clean5["call_reasons"])
    check("no sentiment in an old-style answer -> None, no warning", clean2["sentiment"] is None and clean2["call_reasons"] == []
          and not any("reason" in w for w in warns2))
    from datetime import date as _date
    check("week bucket is the Monday, across the year boundary", dashboard.bucket_key(_date(2026, 1, 1), "week") == "2025-12-29"
          and dashboard.bucket_key(_date(2026, 2, 14), "quarter") == "2026-Q1" and dashboard.bucket_key(_date(2026, 9, 3), "month") == "2026-09")
    check("local_dt applies the browser offset", dashboard.local_dt("2025-12-31T23:30:00+00:00", -60).date() == _date(2026, 1, 1)
          and dashboard.local_dt("2026-01-01T03:30:00+00:00", 480).date() == _date(2025, 12, 31))
    check("bucket_range fills empty buckets and caps", len(dashboard.bucket_range(_date(2025, 10, 1), _date(2026, 9, 3), "month")) == 12
          and len(dashboard.bucket_range(_date(2026, 8, 31), _date(2026, 9, 6), "week")) == 1)
    try:
        dashboard.bucket_range(_date(2020, 1, 1), _date(2026, 1, 1), "day")
        check("bucket_range refuses > MAX_BUCKETS", False)
    except ValueError:
        check("bucket_range refuses > MAX_BUCKETS", True)
    agg = dashboard.aggregate([
        {"sc_id": "1", "recording_id": "r1", "overall_pct": 80.0, "auto_fail": 0, "sc_created_at": "2026-09-01T10:00:00+00:00",
         "sentiment_json": '{"start":"negative","end":"positive","overall":"positive"}', "call_reasons_json": '[{"category":"no_service","detail":""}]',
         "agent_name": "A", "qa_name": "Q", "uploaded_at": "2026-09-01T09:00:00+00:00", "rv_status": None},
        {"sc_id": "2", "recording_id": "r1", "overall_pct": 60.0, "auto_fail": 0, "sc_created_at": "2026-09-02T10:00:00+00:00",
         "sentiment_json": None, "call_reasons_json": None, "agent_name": "A", "qa_name": "Q",
         "uploaded_at": "2026-09-01T09:00:00+00:00", "rv_status": "submitted", "final_pct": 70.0,
         "submitted_at": "2026-09-02T11:00:00+00:00", "coached_at": "2026-09-03T11:00:00+00:00"},
        {"sc_id": "3", "recording_id": "r2", "overall_pct": 90.0, "auto_fail": 1, "sc_created_at": "2026-09-02T10:00:00+00:00",
         "sentiment_json": None, "call_reasons_json": None, "agent_name": "B", "qa_name": "Q",
         "uploaded_at": "2026-09-02T09:00:00+00:00", "rv_status": None}],
        "day", _date(2026, 9, 1), _date(2026, 9, 3), tz=0, source="scored")
    check("aggregate: one value per recording, reviewed scorecard wins, final_pct used",
          agg["totals"]["calls"] == 2 and agg["totals"]["avg_pct"] == 80.0 and agg["totals"]["audited"] == 1
          and agg["series"]["site"][0]["avg"] == 70.0 and agg["series"]["site"][2]["n"] == 0
          and agg["sentiment"]["unclassified"] == 2 and agg["reasons"] == [] and agg["by_agent"][1]["auto_fails"] == 1
          and agg["activity"] == [{"key": "2026-09-02", "qa": "Q", "audits": 1, "coached": 0},
                                  {"key": "2026-09-03", "qa": "Q", "audits": 0, "coached": 1}], agg)
    agg2 = dashboard.aggregate(agg and [], "day", _date(2026, 9, 1), _date(2026, 9, 1))
    check("aggregate on nothing is well-formed", agg2["totals"]["calls"] == 0 and agg2["totals"]["avg_pct"] is None and len(agg2["buckets"]) == 1)
    olddb = os.path.join(tempfile.mkdtemp(prefix="auditly-mig-"), "old.db")
    mig = core.init_db(olddb)
    mig.execute("PRAGMA foreign_keys=OFF")
    mig.execute("DROP TABLE review")
    mig.execute("CREATE TABLE review (id TEXT PRIMARY KEY, scorecard_id TEXT NOT NULL UNIQUE, recording_id TEXT NOT NULL, reviewer_email TEXT,"
                " status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','submitted')), final_pct REAL, applicable_weight INTEGER,"
                " auto_fail INTEGER, coaching_notes TEXT, resolution_notes TEXT, action_plan TEXT, follow_up_on TEXT, coached_at TEXT,"
                " created_at TEXT NOT NULL, updated_at TEXT NOT NULL, submitted_at TEXT)")
    mig.execute("INSERT INTO review (id,scorecard_id,recording_id,status,coaching_notes,created_at,updated_at) VALUES ('r1','s1','x1','draft','keep','t','t')")
    mig.close()
    mig = core.init_db(olddb)
    mig.execute("PRAGMA foreign_keys=OFF")
    ok = True
    try:
        mig.execute("INSERT INTO review (id,scorecard_id,recording_id,status,created_at,updated_at) VALUES ('r2','s2','x2','queued','t','t')")
    except Exception:
        ok = False
    idx = {r[0] for r in mig.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='review'")}
    check("review table from 0.9.0 is rebuilt to allow 'queued', keeps rows and indexes",
          ok and mig.execute("SELECT coaching_notes FROM review WHERE id='r1'").fetchone()[0] == "keep" and {"review_recording", "review_submitted"} <= idx, idx)
    mig.close()
    bf = core.init_db(os.path.join(tempfile.mkdtemp(), "bf.db"))
    bf.execute("INSERT INTO recording (id,filename,ext,mime,bytes,path,uploaded_at,call_ref) VALUES ('b1','1700000000.12345.mp3','mp3','audio/mpeg',1,'/x','t',NULL)")
    bf.execute("INSERT INTO recording (id,filename,ext,mime,bytes,path,uploaded_at,call_ref) VALUES ('b2','typed.wav','wav','audio/wav',1,'/x','t','T-9')")
    bf.execute("INSERT INTO recording (id,filename,ext,mime,bytes,path,uploaded_at,call_ref) VALUES ('b3','noext','','audio/wav',1,'/x','t','')")
    core._migrate(bf)
    got = {r[0]: r[1] for r in bf.execute("SELECT id, call_ref FROM recording")}
    bf.close()
    check("start-up backfills blank call IDs from the file name, keeps typed ones", got == {"b1": "1700000000.12345", "b2": "T-9", "b3": "noext"}, got)
    check("parse_changelog top entry", core.parse_changelog("# x\n\n## [1.2.3] - 2026-01-02\n\n- one\n  two\n- three\n")[0]
          == {"version": "1.2.3", "date": "2026-01-02", "bullets": ["one two", "three"]})
    envf = os.path.join(tempfile.mkdtemp(), "dotenv")
    with open(envf, "w", encoding="utf-8") as fh:
        fh.write('AUDITLY_BIND=0.0.0.0     # LAN\nAUDITLY_PORT=8084 # taken: 8081\nKEY=ab#cd\nQ="x # y"\nOPENAI_API_BASE=https://api.openai.com/v1\nEMPTY_KEY=      # a comment after an empty value\n'
                 'SESSION_SECRET="a b c"   # rotate quarterly\nSQ=\'q v\' # note\n')
    e = core.env(envf)
    # 0.30.0 rename: old L1QA_* names (in .env or the shell) still work; a new name always wins; legacy_keys() reports them
    envp_saved = core.ENV_PATH
    core.ENV_PATH = os.path.join(tmp, "legacy.env")
    with open(core.ENV_PATH, "w", encoding="utf-8") as fh:
        fh.write("L1QA_RETENTION_DAYS=45\nAUDITLY_WORKERS=2\nL1QA_WORKERS=9\n")
    os.environ["L1QA_SESSION_HOURS"] = "3"
    try:
        cfgl = core.cfg(); lk = core.legacy_keys()
    finally:
        core.ENV_PATH = envp_saved; del os.environ["L1QA_SESSION_HOURS"]
    check("legacy L1QA_* names still work (.env and shell), the AUDITLY_* name wins, and legacy_keys() lists them",
          cfgl["AUDITLY_RETENTION_DAYS"] == "45" and cfgl["AUDITLY_WORKERS"] == "2" and cfgl["AUDITLY_SESSION_HOURS"] == "3"
          and set(lk) == {"L1QA_RETENTION_DAYS", "L1QA_WORKERS", "L1QA_SESSION_HOURS"}, (cfgl.get("AUDITLY_RETENTION_DAYS"), cfgl.get("AUDITLY_WORKERS"), lk))
    check("env() strips inline comments, keeps # inside tokens and quotes, empty+comment is empty, quoted value + comment unquoted",
          e == {"AUDITLY_BIND": "0.0.0.0", "AUDITLY_PORT": "8084", "KEY": "ab#cd", "Q": "x # y",
                "OPENAI_API_BASE": "https://api.openai.com/v1", "EMPTY_KEY": "", "SESSION_SECRET": "a b c", "SQ": "q v"}, e)
    # a read timeout is a bare TimeoutError, not a URLError; it must still surface as ProviderError (0.14.11)
    import llm as llm_mod
    import urllib.request as _ur
    real_open = _ur.urlopen

    def _boom(*a, **k):
        raise TimeoutError("timed out")
    _ur.urlopen = _boom
    try:
        llm_mod.request("openai", "https://example.invalid/x", headers={}, body=b"{}", timeout=1, retries=2)
        to = False
    except llm_mod.ProviderError as ex:
        to = "timed out" in str(ex)
    finally:
        _ur.urlopen = real_open
    check("provider read timeout becomes a ProviderError (no retry, no traceback)", to)
    # QA activity is dated by the submission, not the upload (0.14.10)
    import datetime as _dt
    act_rows = [{"sc_id": "x", "recording_id": "r9", "overall_pct": 80.0, "applicable_weight": 100, "auto_fail": 0,
                 "sc_created_at": "2026-09-02T00:00:00+00:00", "sentiment_json": None, "call_reasons_json": None,
                 "agent_name": "A", "qa_name": "Q", "uploaded_at": "2026-01-02T10:00:00+00:00", "rv_status": "submitted",
                 "final_pct": 75.0, "submitted_at": "2026-09-03T10:00:00+00:00", "coached_at": None}]
    agg = dashboard.aggregate(act_rows, "day", _dt.date(2026, 8, 20), _dt.date(2026, 9, 8))
    check("an audit submitted inside the range counts as QA activity even if the call was uploaded before it",
          agg["activity"] == [{"key": "2026-09-03", "qa": "Q", "audits": 1, "coached": 0}] and agg["totals"]["calls"] == 0, agg["activity"])

    # ── dashboard ─────────────────────────────────────────────────────
    s, dj = c.json("/api/jobs?kind=real&status=done")
    real_recs = {(x.get("recording") or {}).get("id") for x in dj["jobs"]} - {None}
    s, dbp = c.json("/api/dashboard?period=week&tz=0&kind=demo")
    pp = dbp.get("pipeline") or {}
    check("dashboard carries the pipeline strip counts (demo call audited)", s == 200 and pp.get("recorded", 0) >= pp.get("transcribed", 0) >= pp.get("scored", 0) >= pp.get("audited", 0)
          and pp.get("audited", 0) >= 1 and pp.get("coached", 0) >= 1 and pp["awaiting_audit"] == pp["scored"] - pp["audited"], pp)
    s, db1 = c.json("/api/dashboard?period=week&tz=0")
    check("dashboard: week series matches buckets, totals count distinct real calls",
          s == 200 and len(db1["series"]["site"]) == len(db1["buckets"]) == 13 and db1["totals"]["calls"] == len(real_recs)
          and db1["totals"]["site_calls"] == db1["totals"]["calls"] and db1["reason_labels"]["no_service"], (db1.get("totals"), len(real_recs)))
    check("dashboard: per-agent tables and series present", any(a["name"] == "Samir" for a in db1["by_agent"])
          and "Samir" in db1["series"]["by_agent"] and db1["by_qa"] and "sentiment" in db1 and db1["totals"]["pending_audit"] == db1["totals"]["calls"] - db1["totals"]["audited"], db1["by_agent"])
    s, db2 = c.json("/api/dashboard?period=day&from=2026-01-01&to=2026-01-31&agent=Samir")
    check("dashboard: explicit range and agent filter", s == 200 and len(db2["buckets"]) == 31 and db2["scope"]["agent"] == "Samir"
          and db2["totals"]["calls"] == 0 and db2["totals"]["site_calls"] == 0)
    s, db3 = c.json("/api/dashboard?source=audited")
    check("dashboard: audited source counts only submitted reviews (real calls: none)", s == 200 and db3["totals"]["calls"] == 0)
    s, _ = c.json("/api/dashboard?period=hour")
    check("dashboard: bad period 400", s == 400)
    s, _ = c.json("/api/dashboard?from=2026-13-01")
    check("dashboard: bad date 400", s == 400)
    s, _ = c.json("/api/dashboard?from=2026-02-01&to=2026-01-01")
    check("dashboard: inverted range 400", s == 400)
    s, _ = c.json("/api/dashboard?period=day&from=2000-01-01&to=2026-01-01")
    check("dashboard: oversized range 400", s == 400)

    # ── executive layer (0.37.0) ──────────────────────────────────────
    kx = dbp.get("kpis") or {}
    check("dashboard: executive KPIs against the target (two demo cards, one audited and coached)", kx.get("target") == 90 and kx.get("scored") == 2 and kx.get("pass_rate") == 100.0
          and kx.get("avg_pct") == 90.0 and kx.get("auto_fail_rate") == 0.0 and kx.get("audit_coverage") == 50.0 and kx.get("coaching_coverage") == 100.0, kx)
    bx = dbp.get("bands") or {}
    check("dashboard: score bands sum to the scored calls; both 90.0 cards sit in 90+", sum(bx.values()) == kx.get("scored") and bx.get("ge90") == 2, bx)
    check("dashboard: sentiment shift counts both demo calls as improved", (dbp.get("sentiment_shift") or {}).get("improved") == 2, dbp.get("sentiment_shift"))
    bc = dbp.get("by_criterion") or []
    check("dashboard: attainment per criterion, worst first, only the picked cards' items",
          len(bc) == 8 and all(bc[i]["attainment_pct"] <= bc[i + 1]["attainment_pct"] for i in range(len(bc) - 1) if bc[i]["attainment_pct"] is not None and bc[i + 1]["attainment_pct"] is not None)
          and sum(x["n"] for x in bc) == 16 and bc[0]["attainment_pct"] < 100.0 and bc[0].get("lost_points", 0) > 0, [(x["name"], x["attainment_pct"], x["lost_points"]) for x in bc])
    dsx, cox = dbp.get("disputes") or {}, dbp.get("coaching") or {}
    check("dashboard: dispute and coaching health from the earlier steps",
          dsx.get("upheld") == 1 and dsx.get("withdrawn") == 3 and dsx.get("open") == 0 and dsx.get("upheld_rate") == 100.0 and dsx.get("disputed_calls") == 1   # 3 withdrawn: one by the QA, two from the agent's link (0.52.0)
          and cox.get("sessions_done") == 1 and cox.get("awaiting_coaching") == 0 and cox.get("avg_days_audit_to_coached") == 0.0, (dsx, cox))
    check("dashboard: previous period and deltas are present; leaders need 3 calls", dbp.get("previous") and dbp["previous"]["kpis"]["target"] == 90
          and "avg_pct" in (dbp.get("deltas") or {}) and (dbp.get("leaders") or {}).get("min_n") == 3 and dbp["leaders"]["top"] == [], (dbp.get("previous"), dbp.get("leaders")))
    check("dashboard: target from the environment, 1–100, else 90", dashboard.target_of("85") == 85 and dashboard.target_of("abc") == 90 and dashboard.target_of("0") == 90
          and dashboard.target_of(None) == 90 and core.DEFAULTS.get("AUDITLY_TARGET_PCT") == "90"
          and dashboard.previous_range(_date(2026, 9, 1), _date(2026, 9, 30)) == (_date(2026, 8, 2), _date(2026, 8, 31)))
    xr = [{"sc_id": "a", "recording_id": "r1", "overall_pct": 95.0, "applicable_weight": 100, "auto_fail": 0, "sc_created_at": "2026-09-10T10:00:00+00:00", "sentiment_json": '{"start":"negative","end":"positive","overall":"positive"}', "call_reasons_json": "[]", "agent_name": "A", "qa_name": "Q", "uploaded_at": "2026-09-10T10:00:00+00:00", "rv_status": "submitted", "final_pct": 95.0, "submitted_at": "2026-09-11T10:00:00+00:00", "coached_at": "2026-09-13T10:00:00+00:00"},
          {"sc_id": "b", "recording_id": "r2", "overall_pct": 70.0, "applicable_weight": 100, "auto_fail": 1, "sc_created_at": "2026-09-12T10:00:00+00:00", "sentiment_json": None, "call_reasons_json": "[]", "agent_name": "B", "qa_name": "Q", "uploaded_at": "2026-09-12T10:00:00+00:00", "rv_status": None, "final_pct": None, "submitted_at": None, "coached_at": None},
          {"sc_id": "c", "recording_id": "r3", "overall_pct": 80.0, "applicable_weight": 100, "auto_fail": 0, "sc_created_at": "2026-08-20T10:00:00+00:00", "sentiment_json": None, "call_reasons_json": "[]", "agent_name": "A", "qa_name": "Q", "uploaded_at": "2026-08-20T10:00:00+00:00", "rv_status": None, "final_pct": None, "submitted_at": None, "coached_at": None}]
    xi = [{"sc_id": "a", "key": "g", "name": "Greeting", "weight": 10, "rating": "met", "final": 10}, {"sc_id": "b", "key": "g", "name": "Greeting", "weight": 10, "rating": "missed", "final": 0},
          {"sc_id": "b", "key": "v", "name": "Verification", "weight": 10, "rating": "na", "final": 0}, {"sc_id": "zzz", "key": "g", "name": "Greeting", "weight": 10, "rating": "met", "final": 10}]
    xo = dashboard.aggregate(xr, "week", _date(2026, 9, 1), _date(2026, 9, 30), items=xi, disputes=[{"sc_id": "a", "status": "upheld"}, {"sc_id": "b", "status": "open"}, {"sc_id": "zzz", "status": "open"}],
                             sessions=[{"agent_name": "A", "status": "scheduled", "scheduled_at": "2026-09-25T10:00:00+00:00", "done_at": None}], now_iso="2026-09-20T00:00:00+00:00")
    check("executive maths: pass rate, bands, shift, criterion attainment (other cards' items ignored), disputes on picked cards, coaching, deltas vs the previous 30 days",
          xo["kpis"]["pass_rate"] == 50.0 and xo["kpis"]["avg_pct"] == 82.5 and xo["kpis"]["auto_fail_rate"] == 50.0 and xo["kpis"]["audit_coverage"] == 50.0 and xo["kpis"]["coaching_coverage"] == 100.0
          and xo["bands"] == {"lt60": 0, "60_79": 1, "80_89": 0, "ge90": 1} and xo["sentiment_shift"] == {"improved": 1, "same": 0, "worsened": 0, "unclassified": 1}
          and xo["by_criterion"][0] == {"key": "g", "name": "Greeting", "n": 2, "met": 1, "partial": 0, "missed": 1, "na": 0, "attainment_pct": 50.0, "lost_points": 5.0}
          and xo["by_criterion"][1]["attainment_pct"] is None and "_got" not in xo["by_criterion"][1]
          and xo["disputes"]["open"] == 1 and xo["disputes"]["upheld_rate"] == 100.0 and xo["disputes"]["disputed_calls"] == 2
          and xo["coaching"]["sessions_upcoming"] == 1 and xo["coaching"]["avg_days_audit_to_coached"] == 2.0
          and xo["previous"]["from"] == "2026-08-02" and xo["previous"]["kpis"]["avg_pct"] == 80.0 and xo["deltas"]["avg_pct"] == 2.5 and xo["deltas"]["calls"] == 1
          and xo["leaders"]["top"] == [], (xo["kpis"], xo["bands"], xo["by_criterion"], xo["disputes"], xo["coaching"], xo["deltas"]))
    xl = dashboard.leaders([{"name": "A", "n": 4, "avg": 90.0}, {"name": "B", "n": 4, "avg": 60.0}, {"name": "C", "n": 1, "avg": 10.0}, {"name": "D", "n": 5, "avg": 75.0}, {"name": "E", "n": 3, "avg": 80.0}],
                           [{"name": "A", "avg": 85.0}])
    check("leaders: ranked by average among agents with 3+ calls, top 3, the rest as needs-attention, delta vs previous",
          [a["name"] for a in xl["top"]] == ["A", "E", "D"] and [a["name"] for a in xl["bottom"]] == ["B"] and xl["top"][0]["delta"] == 5.0 and xl["top"][1]["delta"] is None and xl["ranked"] == 4, xl)
    s, pdfx, hdx = c.raw("/api/dashboard/export.pdf?period=week&tz=0&kind=demo")
    check("executive summary PDF for the same filters", s == 200 and hdx.get("Content-Type", "").startswith("application/pdf") and pdfx[:5] == b"%PDF-", (s, hdx.get("Content-Type")))
    s, _, _ = c.raw("/api/dashboard/export.pdf?period=hour")
    check("executive PDF refuses a bad period (400)", s == 400)

    # ── admin ─────────────────────────────────────────────────────────
    s, users = c.json("/api/admin/users")
    check("admin lists users", s == 200 and any(u["email"] == "demo@example.com" for u in users["users"]))
    s, nu = c.json("/api/admin/users", "POST", {"email": "rev@example.com", "role": "reviewer", "password": "reviewer-pass-123"})
    check("admin creates reviewer", s == 201)
    rv = Client()
    s, _ = rv.login("rev@example.com", "reviewer-pass-123")
    check("reviewer can sign in", s == 200)
    s, _ = rv.json("/api/admin/users")
    check("reviewer cannot see admin routes (404)", s == 404)

    # ── notifications (0.51.0): shared events, per-person kinds and read marks ──
    import dashboard as dash_mod
    import datetime as _dt
    s, n0 = rv.json("/api/notifications")
    check("a brand-new reviewer opens to no unread (older events show but do not count), every kind on",
          s == 200 and n0["unread"] == 0 and n0["failing_rule"]["min_n"] == 3 and all(k["on"] for k in n0["kinds"])
          and {k["id"] for k in n0["kinds"]} == {"coaching.scheduled", "coaching.changed", "dispute.raised", "dispute.resolved", "agent.failing", "job.failed"}, n0.get("unread"))
    c.json("/api/notifications/read", "POST", {"all": True})
    s, ns = c.json("/api/coaching/sessions", "POST", {"agent_name": "Jordan", "scorecard_ids": []})
    sid_n = ns.get("id")
    s, _ = c.json("/api/coaching/sessions/%s/schedule" % sid_n, "POST", {"scheduled_at": "2026-10-06T10:00", "tz": 0, "duration_min": 30})
    s, n1 = c.json("/api/notifications")
    s2, n1r = rv.json("/api/notifications")
    top = n1["items"][0] if n1.get("items") else {}
    check("scheduling a session notifies everyone: coaching.scheduled, one unread for the admin and for the reviewer",
          s == 200 and top.get("kind") == "coaching.scheduled" and top.get("target_kind") == "session" and top.get("target_id") == sid_n
          and top.get("agent_name") == "Jordan" and "06 Oct 2026, 10:00 UTC" in (top.get("body") or "") and n1["unread"] == 1 and n1r["unread"] == 1,
          (top, n1.get("unread"), n1r.get("unread")))
    s, _ = c.json("/api/coaching/sessions/%s/schedule" % sid_n, "POST", {"scheduled_at": "2026-10-07T11:00", "tz": 0})
    top = c.json("/api/notifications")[1]["items"][0]
    check("rescheduling says old → new", top["kind"] == "coaching.changed" and "06 Oct 2026, 10:00 UTC" in top["body"]
          and "07 Oct 2026, 11:00 UTC" in top["body"] and "→" in top["body"], top)
    c.json("/api/coaching/sessions/%s/unschedule" % sid_n, "POST", {})
    top = c.json("/api/notifications")[1]["items"][0]
    check("unscheduling is a change too", top["kind"] == "coaching.changed" and top["body"].startswith("Unscheduled"), top)
    c.json("/api/coaching/sessions/%s/cancel" % sid_n, "POST", {})
    top = c.json("/api/notifications")[1]["items"][0]
    check("cancelling is a change too", top["kind"] == "coaching.changed" and top["body"].startswith("Cancelled"), top)
    sc_rec = sc.get("recording_id") or (sc.get("recording") or {}).get("id")
    s, dr = c.json("/api/scorecards/%s/disputes" % sc["id"], "POST", {"reason": "The customer was verified by PIN — listen at 0:40."})
    did_n = [d["id"] for d in dr.get("disputes", []) if d["status"] == "open"]
    top = c.json("/api/notifications")[1]["items"][0]
    check("a raised dispute notifies with the agent and the call behind it", s == 201 and bool(did_n) and top["kind"] == "dispute.raised"
          and top["agent_name"] == "Jordan" and top["target_kind"] == "scorecard" and top["target_id"] == sc["id"]
          and top["recording_id"] == sc_rec and "PIN" in top["body"], top)
    s, _ = c.json("/api/disputes/%s/resolve" % did_n[0], "POST", {"status": "rejected", "note": "The PIN was read back after the balance was given."})
    top = c.json("/api/notifications")[1]["items"][0]
    check("resolving it notifies with the outcome", s == 200 and top["kind"] == "dispute.resolved" and "rejected" in top["title"], top)
    s, na = c.json("/api/notifications")
    s, nr = rv.json("/api/notifications")
    ua, ur = na["unread"], nr["unread"]
    first_unread = [n for n in na["items"] if not n["read"]][0]
    s, na2 = c.json("/api/notifications/read", "POST", {"ids": [first_unread["id"], "not-a-real-id"]})
    s, nr2 = rv.json("/api/notifications")
    check("reading one line is per person: the admin's count drops by one, the reviewer's does not move", s == 200 and na2["unread"] == ua - 1
          and nr2["unread"] == ur and [n for n in na2["items"] if n["id"] == first_unread["id"]][0]["read"] is True, (ua, na2["unread"], ur, nr2["unread"]))
    s, nr3 = rv.json("/api/notifications/read", "POST", {"all": True})
    check("mark all read clears the reviewer alone", s == 200 and nr3["unread"] == 0 and c.json("/api/notifications")[1]["unread"] == ua - 1)
    s, _ = rv.json("/api/notifications/read", "POST", {})
    check("read needs ids or all", s == 400)
    s, np_ = rv.json("/api/notifications/prefs", "POST", {"off": ["coaching.changed"]})
    check("muting coaching.changed: gone from the reviewer's feed, shown off in kinds, still in the admin's", s == 200
          and not [n for n in np_["items"] if n["kind"] == "coaching.changed"]
          and [k for k in np_["kinds"] if k["id"] == "coaching.changed"][0]["on"] is False
          and bool([n for n in c.json("/api/notifications")[1]["items"] if n["kind"] == "coaching.changed"]), np_.get("kinds"))
    s, e = rv.json("/api/notifications/prefs", "POST", {"off": ["mail.digest"]})
    check("an unknown kind is refused by name", s == 400 and "mail.digest" in e.get("error", ""), e)
    s, e = rv.json("/api/notifications/prefs", "POST", {"off": "coaching.changed"})
    check("off must be a list", s == 400)
    s, np2 = rv.json("/api/notifications/prefs", "POST", {"off": []})
    check("an empty mute list restores every kind", s == 200 and all(k["on"] for k in np2["kinds"])
          and bool([n for n in np2["items"] if n["kind"] == "coaching.changed"]))
    s, au_n = c.json("/api/admin/audit")
    check("preference changes are in the audit log", "notification.prefs" in {a["action"] for a in au_n.get("audit", [])})
    # the failing rule is the Dashboard's, as a pure function
    today_n = _dt.datetime.now(_dt.timezone.utc).date()

    def _drow(i, pct, agent="Ava"):
        iso = "%sT10:00:00+00:00" % today_n.isoformat()
        return {"sc_id": "s%d" % i, "recording_id": "r%d" % i, "overall_pct": pct, "applicable_weight": 100, "auto_fail": 0,
                "sc_created_at": iso, "sentiment_json": None, "call_reasons_json": None, "agent_name": agent, "qa_name": "Q",
                "uploaded_at": iso, "rv_status": None, "final_pct": None, "submitted_at": None, "coached_at": None}
    st2 = dash_mod.agent_standing([_drow(1, 60), _drow(2, 60)], "Ava", 90, today_n)
    st3 = dash_mod.agent_standing([_drow(1, 80), _drow(2, 80), _drow(3, 80)], "ava", 90, today_n)
    st4 = dash_mod.agent_standing([_drow(1, 95), _drow(2, 95), _drow(3, 95)], "Ava", 90, today_n)
    check("agent_standing: two calls never fail; three at 80 fail a 90 target (case-insensitive); three at 95 do not",
          st2["failing"] is False and st2["n"] == 2 and st3["failing"] is True and st3["n"] == 3 and st3["avg"] == 80
          and st4["failing"] is False and st3["from"] and st3["target"] == 90, (st2, st3, st4))
    s, _ = rv.json("/api/admin/ask")
    check("reviewer cannot read the assistant's settings or prompt (404)", s == 404)
    s, _ = rv.json("/api/jobs/%s/delete" % j2["id"], "POST", {})
    check("reviewer cannot delete (403)", s == 403)
    s, _ = rv.json("/api/people", "POST", {"kind": "agent", "names": "Intruder"})
    s2, _ = rv.json("/api/people")
    check("reviewer can read names but not manage them", s == 403 and s2 == 200)
    s, _ = rv.json("/api/people/remove-demo", "POST", {})
    check("reviewer cannot remove demo names (403)", s == 403)
    # ── demo data on a real server (load / remove) ────────────────────
    s, _ = rv.json("/api/admin/demo-data")
    check("reviewer cannot see the demo-data route (404)", s == 404)
    s, dc = c.json("/api/admin/demo-data")
    check("no demo data loaded yet", s == 200 and dc.get("count") == 0, dc)
    before_demo = c.json("/api/jobs?kind=demo")[1]["total"]
    demo_calls0 = c.json("/api/dashboard?kind=demo&period=year")[1]["totals"]["calls"]
    s, dash0 = c.json("/api/dashboard")
    s, ld = c.json("/api/admin/demo-data/load", "POST", {})
    check("load demo data: 44 calls", s == 200 and ld.get("loaded") == 44, ld)
    s, dc = c.json("/api/admin/demo-data")
    check("demo-data count after load", dc.get("count") == 44)
    check("loaded calls are kind=demo jobs", c.json("/api/jobs?kind=demo")[1]["total"] == before_demo + 44)
    s, dash1 = c.json("/api/dashboard")
    check("dashboard default (real) unchanged by demo data", dash1["totals"] == dash0["totals"] and dash1["kind"] == "real", (dash0["totals"], dash1["totals"]))
    s, dashd = c.json("/api/dashboard?kind=demo&period=year")
    check("dashboard kind=demo shows the 44 calls across three agents", s == 200 and dashd["kind"] == "demo" and dashd["totals"]["calls"] == demo_calls0 + 44
          and {"Jordan", "Marcus", "Priya"} <= {a["name"] for a in dashd["by_agent"]} and dashd["totals"]["audited"] > 20 and dashd["sentiment"]["unclassified"] == 0, dashd["totals"])
    s, dasha = c.json("/api/dashboard?kind=all&period=year")
    check("dashboard kind=all counts real + demo", dasha["totals"]["calls"] >= 44 + dash0["totals"]["calls"])
    s, nfd = c.json("/api/notifications?limit=100")
    fails = {n["agent_name"] for n in nfd["items"] if n["kind"] == "agent.failing" and "(demo calls)" in (n["body"] or "")}
    want = set()
    for ag in ("Jordan", "Marcus", "Priya"):
        s, dw = c.json("/api/dashboard?kind=demo&period=week&agent=" + ag)
        a = [x for x in dw["by_agent"] if x["name"] == ag]
        tgt = (dw.get("totals") or {}).get("target") or 90
        if a and a[0]["avg"] is not None and a[0]["n"] >= 3 and a[0]["avg"] < tgt:
            want.add(ag)
    check("load demo data: agent-below-target notifications match the Dashboard's own week numbers exactly", fails == want, (fails, want))
    s, dwk = c.json("/api/dashboard?kind=demo&period=week")
    kk, ag = dwk.get("kpis") or {}, {a["name"]: a for a in dwk.get("by_agent") or []}
    check("demo history: pass rate and coaching coverage sit near the target (80-95), Marcus lowest and under it (0.58.0)",
          s == 200 and 80 <= (kk.get("pass_rate") or 0) <= 95 and 80 <= (kk.get("coaching_coverage") or 0) <= 95
          and all(n in ag for n in ("Jordan", "Priya", "Marcus")) and ag["Marcus"]["avg"] < min(ag["Jordan"]["avg"], ag["Priya"]["avg"]) and ag["Marcus"]["avg"] < (dwk["totals"].get("target") or 90),
          (kk.get("pass_rate"), kk.get("coaching_coverage"), {n: ag[n]["avg"] for n in ag}))
    s, _ = c.json("/api/dashboard?kind=bogus")
    check("dashboard bad kind 400", s == 400)
    s, q_hist = c.json("/api/reviews?kind=demo&status=todo")
    check("audit queue shows queued/draft demo history", s == 200 and q_hist["total"] >= 1)
    names = {p["name"] for p in c.json("/api/people")[1]["people"]}
    check("history QA name added", "Alex Reyes" in names)
    s, _ = c.json("/api/admin/demo-data/load", "POST", {})
    check("second load is 409", s == 409)
    s, rm = c.json("/api/admin/demo-data/remove", "POST", {})
    check("remove demo data: 44 calls", s == 200 and rm.get("removed") == 44, rm)
    s, dc = c.json("/api/admin/demo-data")
    check("demo-data count after remove", dc.get("count") == 0 and c.json("/api/jobs?kind=demo")[1]["total"] == before_demo)
    s, dashd = c.json("/api/dashboard?kind=demo&period=year")
    check("dashboard kind=demo back to before after remove", dashd["totals"]["calls"] == demo_calls0)
    check("removing the demo data takes its notifications away (cascade from the recording)",
          not [n for n in c.json("/api/notifications?limit=100")[1]["items"] if n["kind"] == "agent.failing" and "(demo calls)" in (n["body"] or "")])
    names = {p["name"] for p in c.json("/api/people")[1]["people"]}
    check("history QA name removed with the data", "Alex Reyes" not in names)

    # Over ONE keep-alive connection: a POST whose handler never reads its body, then a GET. Before the
    # server drained unread bodies, the GET was answered 400 ("Bad request syntax ('{}')").
    ka = http.client.HTTPConnection("127.0.0.1", PORT, timeout=15)
    ka_h = {"X-Auditly-CSRF": "1", "Content-Type": "application/json", "Cookie": "; ".join("%s=%s" % (k.name, k.value) for k in c.jar)}
    ka.request("POST", "/api/people/remove-demo", body=b"{}", headers=ka_h)
    r_ka = ka.getresponse(); s = r_ka.status; d = json.loads(r_ka.read() or b"{}")
    ka.request("GET", "/api/people?all=1", headers=ka_h)
    r_ka2 = ka.getresponse(); s_ka2 = r_ka2.status; r_ka2.read(); ka.close()
    check("keep-alive: the request after a body-less POST handler is still answered (unread body drained)", s == 200 and s_ka2 == 200, (s, s_ka2))
    s2, pl = c.json("/api/people")
    left = {p["name"] for p in pl.get("people", [])}
    check("remove demo names: unused deleted, in-use archived, none left active",
          s == 200 and set(d.get("deleted", [])) == {"Priya", "Marcus"} and set(d.get("archived", [])) == {"Jordan", "Demo QA"}
          and not ({"Jordan", "Priya", "Marcus", "Demo QA"} & left), (d, left))
    s, e = c.json("/api/scorecards/%s/review/queue" % sc["id"], "POST", {"qa_name": "Demo QA"})
    check("an archived QA of record is refused as archived, pointing at Settings › Names", s == 400 and "archived" in e.get("error", "") and "Settings › Names" in e.get("error", ""), e)
    s, e = c.json("/api/scorecards/%s/review/queue" % sc["id"], "POST", {"qa_name": "Nobody Real"})
    check("a QA name not on file is refused as missing", s == 400 and "No QA reviewer named" in e.get("error", ""), e)
    s, au = c.json("/api/admin/audit")
    actions = {a["action"] for a in au.get("audit", [])}
    check("audit trail records key actions", {"login.ok", "scorecard.override", "recording.upload", "scorecard.export.pdf", "job.retranscribe", "review.submit", "review.reopen", "review.coached", "review.queue", "review.unqueue", "recording.details", "demo.load", "demo.remove",
                                                "rubric.sync.requested", "rubric.sync.done"} <= actions, actions)
    s, _ = c.json("/auth/password", "POST", {"current": "demo-demo-demo", "new": "short"})
    check("weak new password rejected", s == 400)

    # ── branding (white label), admin side ───────────────────────────
    s, _ = c.json("/api/admin/branding", "POST", {"logo_mode": "sideways"})
    check("bad logo mode 400", s == 400)
    s, _ = c.json("/api/admin/branding", "POST", {"logo_mode": "custom"})
    check("custom without an uploaded logo is 400", s == 400)
    s, bh = c.json("/api/admin/branding", "POST", {"logo_mode": "hidden"})
    s2, bh2 = anon.json("/api/branding")
    check("hidden: no logo URLs, the app name shows, visible to anonymous readers", s == 200 and bh["logos"] == {"light": None, "dark": None} and bh["show_name"] is True
          and bh2["logo_mode"] == "hidden" and bh2["icons"]["light"], (bh, bh2))
    s, bc = c.json("/api/admin/branding", "POST", {"logo_mode": "coeo"})
    check("coeo: the COEO SVGs with the name beside them", s == 200
          and [u.partition("?v=")[0] for u in (bc["logos"]["light"], bc["logos"]["dark"])] == ["/static/coeo_light.svg", "/static/coeo_dark.svg"]
          and all("?v=" in u for u in bc["logos"].values()) and bc["show_name"] is True, bc["logos"])
    png1 = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")
    s, _, _ = c.raw("/api/admin/branding/asset/logo_light?filename=logo.exe", "POST", data=png1, headers={"Content-Type": "application/octet-stream"})
    check("logo upload refuses a non-image extension (400)", s == 400)
    s, _, _ = c.raw("/api/admin/branding/asset/logo_light?filename=logo.png", "POST", data=b"not really a png at all", headers={"Content-Type": "application/octet-stream"})
    check("logo upload checks the magic bytes (400)", s == 400)
    s, _, _ = c.raw("/api/admin/branding/asset/logo_light?filename=logo.svg", "POST", data=b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', headers={"Content-Type": "application/octet-stream"})
    check("SVG with script is refused (400)", s == 400)
    s, _, _ = c.raw("/api/admin/branding/asset/logo_light?filename=logo.png", "POST", data=b"x" * (512 * 1024 + 1), headers={"Content-Type": "application/octet-stream"})
    check("logo over 512 KB is 413", s == 413)
    s, bu, _ = c.raw("/api/admin/branding/asset/logo_light?filename=logo.png", "POST", data=png1, headers={"Content-Type": "application/octet-stream"})
    bu = json.loads(bu.decode())
    check("uploading a logo stores it and switches to Custom; the URL carries a version", s == 201 and bu["logo_mode"] == "custom" and bu["logos"]["light"].startswith("/api/branding/asset/logo_light?v=")
          and bu["custom"]["logo_light"]["bytes"] == len(png1) and bu["show_name"] is False and "blob" not in json.dumps(bu), bu)
    check("a slot without an upload falls back to the built-in mark", bu["logos"]["dark"].startswith("/static/auditly_dark.svg?v="), bu["logos"]["dark"])
    s, ab, ah = anon.raw("/api/branding/asset/logo_light")
    check("the uploaded asset is served publicly with its mime, immutable and nosniff", s == 200 and ab == png1 and ah.get("Content-Type") == "image/png"
          and "immutable" in ah.get("Cache-Control", "") and ah.get("X-Content-Type-Options") == "nosniff")
    svg_ok = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10" fill="red"/></svg>'
    s, _, _ = c.raw("/api/admin/branding/asset/icon_dark?filename=icon.svg", "POST", data=svg_ok, headers={"Content-Type": "application/octet-stream"})
    s2, fb2, fh2 = anon.raw("/favicon.ico")
    s3, _, h3 = anon.raw("/api/branding/asset/icon_dark")
    check("an uploaded SVG tab icon becomes the favicon and is served with a no-script CSP", s == 201 and s2 == 200 and fb2 == svg_ok
          and fh2.get("Content-Type", "").startswith("image/svg") and "default-src 'none'" in h3.get("Content-Security-Policy", ""))
    s, _ = c.json("/api/admin/branding/asset/logo_light/delete", "POST", {})
    s2, bd = anon.json("/api/branding")
    check("removing the only logo returns the mode to Auditly", s == 200 and bd["logo_mode"] == "auditly" and "logo_light" not in bd["custom"])
    c.json("/api/admin/branding/asset/icon_dark/delete", "POST", {})
    c.json("/api/admin/branding", "POST", {"logo_mode": "auditly"})

    # ── serialization backstop ────────────────────────────────────────
    leak = False
    for p in ("/api/jobs", "/api/rubrics", "/api/health", "/api/scorecards/" + sc["id"], "/api/admin/audit",
              "/api/reviews?kind=demo", "/api/dashboard", "/api/scorecards/" + sc["id"] + "/review",
              "/api/admin/demo-data", "/api/jobs?kind=demo", "/api/recordings/lookup?call_ref=DEMO-0001",
              "/api/rubrics/" + srid, "/api/rubrics/%s/versions/1" % srid, "/api/rubrics/%s/versions/1/sync" % srid,
              "/api/disputes?kind=demo&status=all", "/api/scorecards/" + sc["id"] + "/disputes",
              "/api/coaching/sessions?status=all", "/api/coaching/sessions/" + sid, "/api/coaching/agents?kind=demo",
              "/api/coaching/candidates?agent=Jordan&kind=demo", "/api/coaching/calendar?from=2026-09-01&to=2026-09-30&tz=0",
              "/api/kb/documents", "/api/kb/search?q=phone+no+service", "/api/branding", "/api/admin/ask", "/api/notifications",
              "/api/scorecards/" + sc["id"] + "/dispute-link", "/api/jobs?recording=" + (sc.get("recording") or {}).get("id", ""),
              "/api/reviews?kind=all&recording=" + (sc.get("recording") or {}).get("id", "")):
        _, b, _ = c.raw(p)
        if b"test-secret" in b or b"/up/" in b or b"sha256" in b:
            leak = True
    _, b, _ = c.raw("/api/recordings/%s/details" % sc["recording"]["id"], "POST", body={"call_ref": "DEMO-0001"})
    if b"test-secret" in b or b"/up/" in b or b"sha256" in b or b'"path"' in b:
        leak = True
    s, mk_b = c.json("/api/scorecards/%s/dispute-link" % sc["id"], "POST", {"action": "link"})
    if s == 200:
        _, ab, _ = anon.raw("/dispute/" + mk_b["url"].rsplit("/", 1)[-1] + "/data")
        if any(w in ab for w in (b"test-secret", b"/up/", b"sha256", b'"transcript"', b'"utterances"', b'"caller_', b'"qa_name"', b'"coaching_notes"', b'"customer"', b"Northwind", b"Sam Lee")):
            leak = True
    check("no secret, path or hash in any response body", not leak)
    s, _, _ = c.raw("/auth/logout", "POST", body={})
    s, _ = c.json("/api/me")
    check("logout ends session", s == 401)

    # ── rate limit LAST: it is per IP as well as per email, so it would block
    #    every later sign-in from 127.0.0.1 ─────────────────────────────────
    rl = Client()
    for _ in range(8):
        rl.login("ratelimit@example.com", "wrong-password-xx")
    s, _ = rl.login("ratelimit@example.com", "wrong-password-xx")
    check("login rate limit after 8 failures", s == 429)


if __name__ == "__main__":
    main()
