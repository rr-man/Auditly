#!/usr/bin/env python3
"""Transcription clients. Every provider returns the same shape:

    {"provider", "model", "language", "duration_s", "full_text",
     "utterances": [{"start_s", "end_s", "speaker" (int|None), "text", "confidence"}]}

Deepgram diarizes (speaker ints). OpenAI whisper-1 does not (speaker None);
the scorer is told so. Files over OpenAI's 25 MB cap are refused at upload
time in auditly_host.py, so this module never has to split audio.
"""
import json
import os
import time
import urllib.parse

import core
import llm


class Transcriber:
    name = "base"
    model = ""

    def transcribe(self, path, mime):
        raise NotImplementedError


class DeepgramTranscriber(Transcriber):
    name = "deepgram"

    def __init__(self, c, model=None):
        self.c = c
        self.key = (c.get("DEEPGRAM_API_KEY") or "").strip()
        self.model = (model or c.get("DEEPGRAM_MODEL") or "nova-3").strip()

    def transcribe(self, path, mime):
        if not llm._allowed(self.c):
            raise llm.SpendBlocked(self.name)
        if not self.key:
            raise llm.ProviderError(self.name, 0, "DEEPGRAM_API_KEY is not set")
        # clip=True (a spoken question for Ask Auditly, one speaker, a few seconds): no diarization and a
        # short timeout. The default is a full call: 900 s is right for a 200 MB recording.
        clip = bool(getattr(self, "clip", False))
        params = {"model": self.model, "diarize": "false" if clip else "true", "utterances": "true", "smart_format": "true",
                  "punctuate": "true"}
        # Opt-in per-line sentiment. Deepgram derives it from the transcript TEXT after recognition
        # (English only, billed per token on top of the audio minutes); it is not tone of voice.
        if (self.c.get("AUDITLY_STT_SENTIMENT") or "0").strip() == "1":
            params["sentiment"] = "true"
        q = urllib.parse.urlencode(params)
        with open(path, "rb") as f:
            data = f.read()
        d = llm.post_bytes(self.name, "https://api.deepgram.com/v1/listen?" + q,
                           {"Authorization": "Token " + self.key}, data, mime, timeout=60 if clip else 900)
        return self._normalise(d)

    def _normalise(self, d):
        res = d.get("results") or {}
        utts = []
        for i, u in enumerate(res.get("utterances") or []):
            utts.append({"start_s": float(u.get("start") or 0),
                         "end_s": float(u.get("end") or 0),
                         "speaker": _int_or_none(u.get("speaker")),
                         "text": (u.get("transcript") or "").strip(),
                         "confidence": u.get("confidence")})
        full = ""
        try:
            alt = res["channels"][0]["alternatives"][0]
            full = alt.get("transcript") or ""
            if not utts:            # utterances off or empty: fall back to one block
                utts = [{"start_s": 0.0, "end_s": float((d.get("metadata") or {}).get("duration") or 0),
                         "speaker": None, "text": full, "confidence": alt.get("confidence")}]
        except (KeyError, IndexError, TypeError):
            pass
        lang = None
        try:
            lang = res["channels"][0].get("detected_language")
        except (KeyError, IndexError, TypeError, AttributeError):
            pass
        try:
            words = res["channels"][0]["alternatives"][0].get("words") or []
        except (KeyError, IndexError, TypeError, AttributeError):
            words = []
        map_sentiments(words, (res.get("sentiments") or {}).get("segments"), utts)
        return {"provider": self.name, "model": self.model, "language": lang,
                "duration_s": _float_or_none((d.get("metadata") or {}).get("duration")),
                "full_text": full or " ".join(u["text"] for u in utts), "utterances": utts}


def map_sentiments(words, segments, utts):
    """Deepgram's sentiment segments (start_word/end_word indexes into the word list, each with a
    label and a -1..1 score) onto utterances by time overlap: every utterance gets the
    overlap-weighted mean score and the label of the segment that covers most of it. In place;
    a no-op when either list is empty or the indexes do not resolve."""
    if not words or not segments or not utts:
        return utts
    spans = []
    for sg in segments:
        try:
            a, b = int(sg.get("start_word")), int(sg.get("end_word"))
            st, en = float(words[a]["start"]), float(words[b]["end"])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if en <= st:
            continue
        spans.append((st, en, sg.get("sentiment"), _float_or_none(sg.get("sentiment_score"))))
    if not spans:
        return utts
    for u in utts:
        us, ue = float(u.get("start_s") or 0), float(u.get("end_s") or 0)
        best, best_ov, wsum, ssum = None, 0.0, 0.0, 0.0
        for st, en, label, score in spans:
            ov = min(ue, en) - max(us, st)
            if ov <= 0:
                continue
            if score is not None:
                wsum += ov
                ssum += ov * score
            if ov > best_ov:
                best, best_ov = label, ov
        if best_ov > 0:
            u["sentiment"] = best if best in ("negative", "neutral", "positive") else None
            u["sentiment_score"] = round(ssum / wsum, 3) if wsum > 0 else None
    return utts


class OpenAITranscriber(Transcriber):
    name = "openai"

    def __init__(self, c, model=None):
        self.c = c
        self.key = (c.get("OPENAI_API_KEY") or "").strip()
        self.base = (c.get("OPENAI_API_BASE") or "https://api.openai.com/v1").rstrip("/")
        self.model = (model or c.get("OPENAI_TRANSCRIBE_MODEL") or "whisper-1").strip()

    def transcribe(self, path, mime):
        if not llm._allowed(self.c):
            raise llm.SpendBlocked(self.name)
        if not self.key:
            raise llm.ProviderError(self.name, 0, "OPENAI_API_KEY is not set")
        size = os.path.getsize(path)
        if size > core.OPENAI_STT_CAP:
            raise llm.ProviderError(self.name, 0, "file is %.1f MB; OpenAI accepts at most 25 MB. "
                                    "Use Deepgram for this recording." % (size / 1048576.0))
        with open(path, "rb") as f:
            data = f.read()
        if self.model.startswith("whisper"):
            fields = {"model": self.model, "response_format": "verbose_json",
                      "timestamp_granularities[]": "segment"}
        else:                                   # gpt-4o-transcribe family: json/text only, no segments
            fields = {"model": self.model, "response_format": "json"}
        d = llm.post_multipart(self.name, self.base + "/audio/transcriptions",
                               {"Authorization": "Bearer " + self.key}, fields,
                               "file", os.path.basename(path), data, mime, timeout=60 if getattr(self, "clip", False) else 900)
        return self._normalise(d)

    def _normalise(self, d):
        utts = []
        for s in d.get("segments") or []:
            utts.append({"start_s": float(s.get("start") or 0), "end_s": float(s.get("end") or 0),
                         "speaker": None, "text": (s.get("text") or "").strip(),
                         "confidence": None})
        full = d.get("text") or " ".join(u["text"] for u in utts)
        if not utts and full:
            utts = [{"start_s": 0.0, "end_s": _float_or_none(d.get("duration")) or 0.0,
                     "speaker": None, "text": full, "confidence": None}]
        return {"provider": self.name, "model": self.model, "language": d.get("language"),
                "duration_s": _float_or_none(d.get("duration")), "full_text": full,
                "utterances": utts}


class DemoTranscriber(Transcriber):
    """Offline: returns fixtures/demo_call.json after a short pause so the UI's
    'transcribing' state is visible."""
    name = "demo"
    model = "demo"

    def transcribe(self, path, mime):
        time.sleep(0.3)
        with open(os.path.join(core.HERE, "fixtures", "demo_call.json"), "r",
                  encoding="utf-8") as f:
            d = json.load(f)
        d.setdefault("provider", "demo")
        d.setdefault("model", "demo")
        return d


def transcriber_for(c, name=None, model=None):
    """A model override (from the job) wins over .env."""
    name = (name or c.get("TRANSCRIBE_PROVIDER") or "deepgram").strip().lower()
    if (c.get("AUDITLY_DEMO") or "").strip() == "1" or name == "demo":
        return DemoTranscriber()
    if name == "openai":
        return OpenAITranscriber(c, model)
    if name == "deepgram":
        return DeepgramTranscriber(c, model)
    # never fall back to a paid provider the caller did not name ("reuse", a typo, ...)
    raise ValueError("Unknown transcription provider '%s'." % name)


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float_or_none(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
