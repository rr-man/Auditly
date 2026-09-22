#!/usr/bin/env python3
"""The ONE place this project makes an outbound HTTP call.

Deepgram, OpenAI and Anthropic clients all go through request() below. API keys
travel in headers only (never in a URL -- URLs land in logs), are never logged,
and never appear in an exception message. Stdlib urllib; no SDKs.

Also home to the Scorer classes (chat-completion-shaped JSON calls) and the
Demo fakes that let the whole pipeline run with no keys and no network.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid

import core

USER_AGENT = "auditly"
RETRY_STATUSES = {429, 500, 502, 503, 504}


class ProviderError(Exception):
    def __init__(self, provider, status, body=""):
        self.provider, self.status = provider, status
        self.body = (body or "")[:600]
        super().__init__("%s returned %s: %s" % (provider, status, self.body[:200]))


class SpendBlocked(ProviderError):
    """Raised when AUDITLY_ALLOW_SPEND is not 1. Nothing is sent."""

    def __init__(self, provider):
        super().__init__(provider, 0, "AUDITLY_ALLOW_SPEND is not 1; refusing to call "
                                      "a paid provider. Set it in .env or use --demo.")


def _allowed(c):
    return (c.get("AUDITLY_ALLOW_SPEND") or "").strip() == "1"


def request(provider, url, method="POST", headers=None, body=None,
            timeout=120, retries=2):
    """Return the decoded JSON body. Retries 429/5xx with backoff."""
    attempt = 0
    while True:
        r = urllib.request.Request(url, data=body, method=method)
        for k, v in (headers or {}).items():
            r.add_header(k, v)
        r.add_header("User-Agent", USER_AGENT)
        r.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return json.loads(raw or "{}")
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            if e.code in RETRY_STATUSES and attempt < retries:
                attempt += 1
                time.sleep(1.5 * attempt)
                continue
            raise ProviderError(provider, e.code, text)
        except urllib.error.URLError as e:
            if attempt < retries:
                attempt += 1
                time.sleep(1.5 * attempt)
                continue
            raise ProviderError(provider, 0, "unreachable: %s" % e.reason)
        except (TimeoutError, OSError) as e:
            # a timeout while waiting for or reading the response is a bare TimeoutError,
            # not a URLError. Not retried: the request body may already have been accepted
            # (and billed), so repeating it could pay twice.
            raise ProviderError(provider, 0, "timed out or lost the connection while waiting for the"
                                             " response (%s)" % (e.__class__.__name__))
        except ValueError:
            raise ProviderError(provider, 0, "response was not JSON")


def post_json(provider, url, headers, payload, timeout=180):
    h = dict(headers or {})
    h["Content-Type"] = "application/json"
    return request(provider, url, "POST", h, json.dumps(payload).encode("utf-8"), timeout)


def post_bytes(provider, url, headers, data, content_type, timeout=600):
    h = dict(headers or {})
    h["Content-Type"] = content_type
    return request(provider, url, "POST", h, data, timeout)


def encode_multipart(fields, file_field, filename, file_bytes, content_type):
    """Hand-rolled multipart/form-data: Python 3.13 removed cgi, and the
    encoding side was never in the stdlib anyway."""
    boundary = "----auditly" + uuid.uuid4().hex
    out = bytearray()
    for k, v in (fields or {}).items():
        out += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                % (boundary, k, v)).encode("utf-8")
    out += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
            "Content-Type: %s\r\n\r\n"
            % (boundary, file_field, filename.replace('"', ""), content_type)).encode("utf-8")
    out += file_bytes
    out += ("\r\n--%s--\r\n" % boundary).encode("utf-8")
    return bytes(out), "multipart/form-data; boundary=" + boundary


def post_multipart(provider, url, headers, fields, file_field, filename,
                   file_bytes, content_type, timeout=600):
    body, ctype = encode_multipart(fields, file_field, filename, file_bytes, content_type)
    h = dict(headers or {})
    h["Content-Type"] = ctype
    return request(provider, url, "POST", h, body, timeout)


# ── scorers: "give me JSON matching this schema" ──────────────────────────
class Scorer:
    name = "base"
    model = ""
    last_usage = None            # {prompt_tokens, completion_tokens, cached_tokens} of the last call

    def complete_json(self, system, user, schema, schema_name="result", max_tokens=4000):
        raise NotImplementedError


def _usage(prompt, completion, cached):
    def n(v):
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0
    return {"prompt_tokens": n(prompt), "completion_tokens": n(completion), "cached_tokens": n(cached)}


class OpenAIScorer(Scorer):
    name = "openai"

    def __init__(self, c, model=None):
        self.c = c
        self.key = (c.get("OPENAI_API_KEY") or "").strip()
        self.base = (c.get("OPENAI_API_BASE") or "https://api.openai.com/v1").rstrip("/")
        self.model = (model or c.get("OPENAI_SCORING_MODEL") or "gpt-4o-mini").strip()

    def payload(self, system, user, schema, schema_name="result", max_tokens=4000):
        """Chat Completions body. Newer models (gpt-5.x) reject max_tokens and a
        non-default temperature; max_completion_tokens works on every current model."""
        p = {"model": self.model,
             "max_completion_tokens": int(max_tokens),
             "messages": [{"role": "system", "content": system},
                          {"role": "user", "content": user}],
             "response_format": {"type": "json_schema",
                                 "json_schema": {"name": schema_name, "strict": True,
                                                 "schema": schema}}}
        if self.model.startswith(("gpt-4o", "gpt-4.1", "gpt-4-", "gpt-3.5")):
            p["temperature"] = float(self.c.get("SCORING_TEMPERATURE") or 0)
            seed = (self.c.get("SCORING_SEED") or "").strip()
            if seed.lstrip("-").isdigit():
                # Best effort: the same seed on the same system_fingerprint gives (mostly)
                # the same answer. Not accepted by the newer models, hence inside this branch.
                p["seed"] = int(seed)
        return p

    def complete_json(self, system, user, schema, schema_name="result", max_tokens=4000):
        if not _allowed(self.c):
            raise SpendBlocked(self.name)
        if not self.key:
            raise ProviderError(self.name, 0, "OPENAI_API_KEY is not set")
        payload = self.payload(system, user, schema, schema_name, max_tokens)
        d = post_json(self.name, self.base + "/chat/completions",
                      {"Authorization": "Bearer " + self.key}, payload)
        u = d.get("usage") or {}
        self.last_usage = _usage(u.get("prompt_tokens"), u.get("completion_tokens"),
                                 ((u.get("prompt_tokens_details") or {}).get("cached_tokens")))
        self.last_usage["fingerprint"] = d.get("system_fingerprint")
        try:
            content = d["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError(self.name, 0, "no choices in response")
        return _parse_json(self.name, content)


class AnthropicScorer(Scorer):
    """Forces a single tool whose input_schema is our schema; the tool call's
    input is the structured answer."""
    name = "anthropic"

    def __init__(self, c, model=None):
        self.c = c
        self.key = (c.get("ANTHROPIC_API_KEY") or "").strip()
        self.model = (model or c.get("ANTHROPIC_MODEL") or "claude-sonnet-5").strip()

    def complete_json(self, system, user, schema, schema_name="result", max_tokens=4000):
        if not _allowed(self.c):
            raise SpendBlocked(self.name)
        if not self.key:
            raise ProviderError(self.name, 0, "ANTHROPIC_API_KEY is not set")
        payload = {
            "model": self.model,
            "max_tokens": int(max_tokens),
            "temperature": float(self.c.get("SCORING_TEMPERATURE") or 0),
            # The system prompt is identical across calls: mark it cacheable so repeated
            # audits of one rubric pay the discounted cached-input rate.
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}],
            "tools": [{"name": schema_name, "description": "Return the structured result.",
                       "input_schema": schema}],
            "tool_choice": {"type": "tool", "name": schema_name},
        }
        d = post_json(self.name, "https://api.anthropic.com/v1/messages",
                      {"x-api-key": self.key, "anthropic-version": "2023-06-01"}, payload)
        u = d.get("usage") or {}
        self.last_usage = _usage(u.get("input_tokens"), u.get("output_tokens"), u.get("cache_read_input_tokens"))
        self.last_usage["fingerprint"] = None
        for block in d.get("content") or []:
            if block.get("type") == "tool_use":
                return block.get("input") or {}
        raise ProviderError(self.name, 0, "no tool_use block in response")


OPT_FIELD_SET = {"description", "met", "partial", "missed", "na"}   # mirrors rubric.OPT_FIELDS


class DemoScorer(Scorer):
    """Deterministic, offline. Answers a criteria-extraction request from the
    headings of the text it is given, an "Optimise for the scorer" request by
    rewriting the authored blocks it is sent, and a scoring request from
    fixtures/demo_scorecard.json mapped onto whatever criterion keys the schema
    asks for. Used by --demo and by the tests."""
    name = "demo"
    model = "demo"

    def complete_json(self, system, user, schema, schema_name="result", max_tokens=4000):
        time.sleep(0.05)
        self.last_usage = _usage(0, 0, 0)
        self.last_usage["fingerprint"] = "demo"
        props = (schema or {}).get("properties") or {}
        # The optimise schema is recognised by shape: every top-level property is an
        # object with exactly the five guidance fields (strict schemas allow no marker).
        if props and all(set(((v or {}).get("properties") or {}).keys()) == OPT_FIELD_SET for v in props.values()):
            return self._optimise(user, list(props))
        if "criteria" in props:
            return self._extract(user)
        if "grounded" in props and "cites" in props:      # assistant.ASK_SCHEMA
            return self._ask(user)
        return self._score(schema)

    @staticmethod
    def _ask(user):
        """Offline answer for --demo and the golden set. It reads the CALL RECORD it was handed and
        answers from it with a real citation id, so the tests exercise the actual context builder and
        the actual citation discipline in assistant.clean_answer(), not a stub. Deterministic."""
        q = user.rsplit("THE REVIEWER NOW ASKS:", 1)[-1].strip()
        ql = q.lower()
        record = user.split("CONVERSATION SO FAR", 1)[0]
        lines = {}
        for line in record.splitlines():
            m = re.match(r"\s*\[([a-z_]+:[^\]]+)\]\s*(.*)", line)
            if m and m.group(1) not in lines:
                lines[m.group(1)] = m.group(2).strip()
        refuse = {"answer": "That is not in this call's record.", "cites": [], "grounded": False}
        # a request about the instructions, or to change how it works: declined, never obeyed
        if re.search(r"(your|the) (instructions|rules|prompt)|system prompt|ignore (all|your|the)|admin mode|reveal", ql):
            return {"answer": "I can only explain this call's record. I do not share my instructions or change how I work.",
                    "cites": [], "grounded": False}
        # a person named mid-question who is not in the record is not this call (the first word is
        # skipped: "Please", "What", "Tell" are sentence starts, not names)
        for name in re.findall(r"\b([A-Z][a-z]{2,})\b", q)[1 if q[:1].isupper() else 0:]:
            if name.lower() not in record.lower():
                return refuse
        words = set(re.findall(r"[a-z]{3,}", ql))
        best, best_n = None, 0
        for cid, text in lines.items():
            if not cid.startswith("criterion:"):
                continue
            name = text.split(" -- ", 1)[0].lower() + " " + cid.split(":", 1)[1].replace("_", " ")
            n = len(words & set(re.findall(r"[a-z]{3,}", name)))
            if n > best_n:
                best, best_n = cid, n
        if best:
            return {"answer": lines[best] + ".", "cites": [best], "grounded": True}
        if re.search(r"coach|action plan|resolution|reviewer (wrote|said)", ql):
            ks = [k for k in lines if k.startswith("review:")]
            if ks:
                return {"answer": lines[ks[0]], "cites": [ks[0]], "grounded": True}
            return {"answer": "No QA review has been written for this call yet.", "cites": [], "grounded": False}
        if re.search(r"miss|lose|lost|points|wrong|weak", ql) and "call:misses" in lines:
            return {"answer": lines["call:misses"], "cites": ["call:misses"], "grounded": True}
        if re.search(r"score|overall|final|percent|pass|fail|result", ql) and "call:score" in lines:
            cites = ["call:score"] + (["call:final"] if "call:final" in lines else [])
            return {"answer": " ".join(lines[c] for c in cites) + ".", "cites": cites, "grounded": True}
        if re.search(r"extension|mac|ticket|account number|address|device|serial|what number", ql) and "call:facts" in lines:
            return {"answer": lines["call:facts"] + ".", "cites": ["call:facts"], "grounded": True}
        if re.search(r"steps?|troubleshoot|what did (the agent|they) (do|try)", ql) and "call:steps" in lines:
            return {"answer": lines["call:steps"] + ".", "cites": ["call:steps"], "grounded": True}
        if re.search(r"about|summary|happen|issue|problem|why did .* call", ql) and "call:summary" in lines:
            return {"answer": lines["call:summary"], "cites": ["call:summary"], "grounded": True}
        return refuse

    @staticmethod
    def _optimise(user, keys):
        """Deterministic rewrite of the '### key=' blocks that rubric.optimise_user() emits."""
        blocks, cur = {}, None
        for line in user.splitlines():
            if line.startswith("### key="):
                cur = line[8:].split(" |", 1)[0].strip()
                blocks[cur] = {}
            elif cur and ":" in line:
                f, _, v = line.partition(":")
                blocks[cur][f.strip()] = v.strip()
        out = {}
        for k in keys:
            b = blocks.get(k, {})
            conds = [s.strip(" .") for s in re.split(r";|,\s|\band\b", b.get("met") or "the behaviour is shown")
                     if s.strip(" .")]
            met = "ALL of: " + "; ".join("(%d) %s" % (i + 1, s) for i, s in enumerate(conds[:6])) + "."
            desc = b.get("description") or ""
            out[k] = {"description": ("What is assessed: %s" % desc if desc else "Assessed from the live agent's turns on the call.")[:600],
                      "met": met[:600],
                      "partial": ("At least one met-condition is present but not all. Also partial when: %s"
                                  % (b.get("partial") or "the attempt is incomplete"))[:600],
                      "missed": ("None of the met-conditions is present in the agent's turns. %s"
                                 % (b.get("missed") or ""))[:600].strip(),
                      "na": "Never; this criterion applies to every call."}
        return out

    @staticmethod
    def _extract(user):
        # Lines that look like headings or numbered items become criteria.
        names = []
        for line in user.splitlines():
            s = line.strip().lstrip("#-*0123456789. )").strip()
            if 3 < len(s) < 70 and (line.lstrip().startswith(("#", "-", "*"))
                                    or line.strip()[:1].isdigit()):
                if s not in names:
                    names.append(s)
        names = names[:8] or ["Greeting", "Verification", "Troubleshooting", "Closing"]
        n = len(names)
        base, extra = divmod(100, n)
        out = []
        for i, nm in enumerate(names):
            out.append({"name": nm, "description": "Assessed from the guideline section '%s'." % nm,
                        "weight": base + (1 if i < extra else 0),
                        "guidance_met": "Fully demonstrated.",
                        "guidance_partial": "Attempted but incomplete.",
                        "guidance_missed": "Not done.", "critical": False})
        return {"criteria": out}

    @staticmethod
    def _score(schema):
        fx = _fixture("demo_scorecard.json")
        keys = []
        try:
            keys = list(schema["properties"]["items"]["properties"].keys())
        except (KeyError, TypeError, AttributeError):
            pass
        by_key = {i["key"]: i for i in fx.get("items", [])}
        items = {}
        for k in keys:
            src = by_key.get(k) or {"rating": "met", "score": 100,
                                    "rationale": "Demo scorer: no fixture for this criterion; treated as met.",
                                    "evidence": []}
            items[k] = {x: src[x] for x in ("rating", "score", "rationale", "evidence") if x in src}
        out = dict(fx)
        out["items"] = items
        return out


def _parse_json(provider, content):
    if isinstance(content, (dict, list)):
        return content
    s = (content or "").strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
    try:
        return json.loads(s)
    except ValueError:
        raise ProviderError(provider, 0, "model reply was not JSON: " + s[:300])


def _fixture(name):
    p = os.path.join(core.HERE, "fixtures", name)
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def scorer_for(c, name=None, model=None):
    """A model override (from the job) wins over .env."""
    name = (name or c.get("SCORING_PROVIDER") or "openai").strip().lower()
    if (c.get("AUDITLY_DEMO") or "").strip() == "1" or name == "demo":
        return DemoScorer()
    if name == "anthropic":
        return AnthropicScorer(c, model)
    return OpenAIScorer(c, model)


# ── the menus: what a reviewer may pick per call ──────────────────────────
# (provider, model, label, note). Prices from OpenAI's pricing page, 2026-09.
STT_CHOICES = [
    ("deepgram", "nova-3", "Deepgram nova-3", "speaker labels · timestamps · up to 200 MB"),
    ("openai", "whisper-1", "OpenAI whisper-1", "timestamps · no speaker labels · 25 MB cap"),
    ("openai", "gpt-4o-transcribe", "OpenAI gpt-4o-transcribe", "newer · no speaker labels · no timestamps · 25 MB cap"),
]


def scoring_choices(c):
    out = [("openai", "gpt-4o-mini", "OpenAI gpt-4o-mini", "default · under 1¢ per call"),
           ("openai", "gpt-5.6-luna", "OpenAI gpt-5.6-luna", "newer generation · under 1¢ per call"),
           ("openai", "gpt-4.1-mini", "OpenAI gpt-4.1-mini", "about 1¢ per call")]
    if (c.get("ANTHROPIC_API_KEY") or "").strip():
        m = (c.get("ANTHROPIC_MODEL") or "claude-sonnet-5").strip()
        out.append(("anthropic", m, "Anthropic " + m, "second vendor"))
    return out


# List prices used only for the Settings spend ESTIMATE (USD): per audio minute, per
# million input / output tokens. 2026-09 list prices; update when they change.
PRICES = {
    "stt": {"deepgram:nova-3": 0.0077, "openai:whisper-1": 0.006, "openai:gpt-4o-transcribe": 0.006, "demo:demo": 0.0},
    "llm": {"gpt-4o-mini": (0.15, 0.60), "gpt-5.6-luna": (0.20, 1.20), "gpt-4.1-mini": (0.40, 1.60), "demo": (0.0, 0.0)},
}


def choice_id(provider, model):
    return "%s:%s" % (provider, model)


def parse_choice(c, kind, cid):
    """'openai:gpt-4.1-mini' -> (provider, model) if it is on the menu, else None."""
    table = STT_CHOICES if kind == "stt" else scoring_choices(c)
    for p, m, _l, _n in table:
        if choice_id(p, m) == (cid or ""):
            return p, m
    return None


def default_scorer(c):
    p = (c.get("SCORING_PROVIDER") or "openai").strip().lower()
    m = (c.get("ANTHROPIC_MODEL") if p == "anthropic" else c.get("OPENAI_SCORING_MODEL")) or "gpt-4o-mini"
    return p if p in ("openai", "anthropic") else "openai", m.strip()


def default_stt(c):
    p = (c.get("TRANSCRIBE_PROVIDER") or "deepgram").strip().lower()
    m = (c.get("OPENAI_TRANSCRIBE_MODEL") or "whisper-1") if p == "openai" else (c.get("DEEPGRAM_MODEL") or "nova-3")
    return ("openai" if p == "openai" else "deepgram"), m.strip()


def choices_public(c):
    demo = (c.get("AUDITLY_DEMO") or "").strip() == "1"
    def pub(table):
        return [{"id": choice_id(p, m), "provider": p, "model": m, "label": l, "note": n,
                 "available": demo or key_problem(c, p) is None} for p, m, l, n in table]
    return {"stt_choices": pub(STT_CHOICES), "scoring_choices": pub(scoring_choices(c)),
            "default_stt_id": choice_id(*default_stt(c)), "default_scorer_id": choice_id(*default_scorer(c))}


KEY_SHAPES = {
    # provider: (test, what a right one looks like). Presence is not enough: a key
    # pasted without its prefix (seen 2026-09-02: an OpenAI key missing "sk-proj-")
    # only fails after the transcription has already been paid for.
    "openai": (lambda k: k.startswith("sk-") and len(k) >= 40 and " " not in k,
               "OpenAI keys start with sk- (usually sk-proj-) and are about 160 characters"),
    "deepgram": (lambda k: len(k) >= 32 and " " not in k and all(ch in "0123456789abcdefABCDEF" for ch in k),
                 "Deepgram keys are 40 hexadecimal characters"),
    "anthropic": (lambda k: k.startswith("sk-ant-") and " " not in k,
                  "Anthropic keys start with sk-ant-"),
}


def key_shape(provider, key):
    """{'shape_ok': bool|None, 'shape_hint': str}. shape_ok is None when no key is set."""
    k = (key or "").strip()
    test, hint = KEY_SHAPES[provider]
    return {"shape_ok": bool(test(k)) if k else None, "shape_hint": hint}


def key_problem(c, provider):
    """None, or a one-line reason this provider cannot be called right now."""
    var = {"openai": "OPENAI_API_KEY", "deepgram": "DEEPGRAM_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}[provider]
    k = (c.get(var) or "").strip()
    if not k:
        return "%s is not set on the server." % var
    if not key_shape(provider, k)["shape_ok"]:
        return "%s does not look like a valid key (%s). Check the paste in .env." % (var, KEY_SHAPES[provider][1])
    return None


def provider_status(c):
    """For /api/health and the Settings tab. Presence, length and shape only -- never the key."""
    demo = (c.get("AUDITLY_DEMO") or "").strip() == "1"
    return {
        "demo": demo,
        "allow_spend": _allowed(c),
        "default_stt": (c.get("TRANSCRIBE_PROVIDER") or "deepgram").strip().lower(),
        "default_scoring": (c.get("SCORING_PROVIDER") or "openai").strip().lower(),
        "deepgram": dict(core.key_status(c.get("DEEPGRAM_API_KEY")), **key_shape("deepgram", c.get("DEEPGRAM_API_KEY")),
                         model=c.get("DEEPGRAM_MODEL") or "nova-3"),
        "openai": dict(core.key_status(c.get("OPENAI_API_KEY")), **key_shape("openai", c.get("OPENAI_API_KEY")),
                       transcribe_model=c.get("OPENAI_TRANSCRIBE_MODEL") or "whisper-1",
                       scoring_model=c.get("OPENAI_SCORING_MODEL") or "gpt-4o-mini",
                       stt_cap_mb=core.OPENAI_STT_CAP // (1024 * 1024)),
        "anthropic": dict(core.key_status(c.get("ANTHROPIC_API_KEY")), **key_shape("anthropic", c.get("ANTHROPIC_API_KEY")),
                          model=c.get("ANTHROPIC_MODEL") or ""),
        **choices_public(c),
    }
