"""Tone & delivery: what the timings of a call say about how it went, computed from the
utterances alone -- no audio decoding, no extra provider call.

Why this and not "tone of voice": neither transcription provider exposes acoustic tone.
Deepgram's sentiment is computed from the transcript text after speech-to-text (English only,
opt-in here with AUDITLY_STT_SENTIMENT=1), OpenAI's STT returns nothing tonal, and this host has
no ffmpeg, so pitch/volume analysis is out of reach. What the timestamps DO give, for free:
talk ratio, interruptions, silences, the agent's pace and how quickly they answer. Shown on the
scorecard for coaching; never scored.

Pure functions; the caller supplies utterances (dicts with start_s, end_s, speaker, text and,
when present, sentiment / sentiment_score) and who the agent and callers are."""

INTERRUPT_S = 0.3        # an utterance starting this long before the other party finished
LONG_SILENCE_S = 5.0     # a gap between turns longer than this is worth a look
MIN_UTTS = 4


def _words(text):
    return len((text or "").split())


def _median(xs):
    xs = sorted(xs)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def _mean(xs):
    return (sum(xs) / len(xs)) if xs else None


def _r(x, nd=2):
    return None if x is None else round(x, nd)


def roles_from(agent_speaker, speakers):
    """(agent labels, caller labels) as sets of ints. speakers = scorecard.speakers_json list;
    agent_speaker wins for the agent (the reviewer's swap only writes that column)."""
    agents, callers = set(), set()
    if agent_speaker is not None:
        try:
            agents.add(int(agent_speaker))
        except (TypeError, ValueError):
            pass
    for sp in speakers or []:
        if not isinstance(sp, dict):
            continue
        try:
            n = int(sp.get("speaker"))
        except (TypeError, ValueError):
            continue
        role = sp.get("role")
        if role == "caller" and n not in agents:
            callers.add(n)
        elif role == "agent" and not agents:
            agents.add(n)
    return agents, callers


def compute(utts, agent_speaker, speakers=None):
    """The delivery metrics for one call, or None when the transcript is too short or not
    diarized. Callers default to every non-agent speaker that is not labelled voice_ai/other."""
    utts = [u for u in (utts or []) if u.get("speaker") is not None]
    if len(utts) < MIN_UTTS:
        return None
    agents, callers = roles_from(agent_speaker, speakers)
    if not agents:
        return None
    excluded = {int(sp["speaker"]) for sp in (speakers or [])
                if isinstance(sp, dict) and sp.get("role") in ("voice_ai", "other")
                and str(sp.get("speaker", "")).lstrip("-").isdigit()}
    if not callers:
        callers = {u["speaker"] for u in utts} - agents - excluded
    if not callers:
        return None
    utts = sorted(utts, key=lambda u: (float(u.get("start_s") or 0), float(u.get("end_s") or 0)))

    def side(u):
        s = u["speaker"]
        return "agent" if s in agents else ("caller" if s in callers else None)

    talk = {"agent": 0.0, "caller": 0.0}
    words = {"agent": 0, "caller": 0}
    turns = {"agent": 0, "caller": 0}
    interruptions = {"agent": 0, "caller": 0}
    latencies, gaps = [], []
    prev = None                       # the previous utterance of the OTHER side (or any side, for gaps)
    last_end = None
    for u in utts:
        s = side(u)
        st, en = float(u.get("start_s") or 0), float(u.get("end_s") or 0)
        if s:
            talk[s] += max(0.0, en - st)
            words[s] += _words(u.get("text"))
            turns[s] += 1
        if last_end is not None:
            gap = st - last_end
            if gap > 0:
                gaps.append(gap)
        if prev is not None and s and side(prev) and side(prev) != s:
            gap = st - float(prev.get("end_s") or 0)
            if gap < -INTERRUPT_S:
                interruptions[s] += 1
            if s == "agent" and gap >= 0:
                latencies.append(gap)
        if s:
            prev = u
        last_end = max(last_end or 0.0, en)
    total = talk["agent"] + talk["caller"]
    out = {"agent_turns": turns["agent"], "caller_turns": turns["caller"],
           "talk_ratio_agent": _r(talk["agent"] / total, 3) if total > 0 else None,
           "agent_talk_s": _r(talk["agent"], 1), "caller_talk_s": _r(talk["caller"], 1),
           "interruptions_by_agent": interruptions["agent"], "interruptions_by_caller": interruptions["caller"],
           "longest_silence_s": _r(max(gaps), 1) if gaps else 0.0,
           "silences_over_5s": sum(1 for g in gaps if g > LONG_SILENCE_S),
           "agent_wpm": _r(words["agent"] * 60.0 / talk["agent"], 0) if talk["agent"] > 0 else None,
           "agent_response_latency_s": _r(_median(latencies), 2) if latencies else None}
    # per-line sentiment (Deepgram, opt-in): mean score per third of the call, per side
    scored = [u for u in utts if isinstance(u.get("sentiment_score"), (int, float)) and side(u)]
    if scored:
        t0, t1 = float(utts[0].get("start_s") or 0), float(utts[-1].get("end_s") or 0)
        span = max(1e-6, t1 - t0)
        for who in ("agent", "caller"):
            thirds = [[], [], []]
            for u in scored:
                if side(u) != who:
                    continue
                k = min(2, int(3 * (float(u.get("start_s") or 0) - t0) / span))
                thirds[k].append(float(u["sentiment_score"]))
            curve = [_r(_mean(t), 2) for t in thirds]
            out[who + "_sentiment_curve"] = curve
            if who == "caller":
                a, b = curve[0], curve[2]
                if a is None or b is None:
                    out["caller_trend"] = None
                else:
                    out["caller_trend"] = "improving" if b - a > 0.15 else ("worsening" if a - b > 0.15 else "flat")
        out["sentiment_lines"] = len(scored)
    else:
        out["sentiment_lines"] = 0
    return out


def trend_agrees(tone, sentiment):
    """Does the per-line sentiment trend agree with the scorer's start->end verdict? None when
    either side is missing."""
    if not tone or not tone.get("caller_trend") or not isinstance(sentiment, dict):
        return None
    order = {"negative": 0, "neutral": 1, "positive": 2}
    a, b = order.get(sentiment.get("start")), order.get(sentiment.get("end"))
    if a is None or b is None:
        return None
    llm = "improving" if b > a else ("worsening" if b < a else "flat")
    return llm == tone["caller_trend"]
