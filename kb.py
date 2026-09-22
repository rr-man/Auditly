"""Knowledge base: the desk's reference documents (procedures, product facts), chunked and
searched with BM25 -- pure Python, no embeddings, no provider call, deterministic. At scoring
time the transcript is the query; the top chunks go into the scorer's prompt as reference
material it may use ONLY to judge whether what the agent said was accurate (score.SYSTEM rule 13).

Why lexical and not embeddings: nothing here may cost money by accident and the demo must work
without keys. A transcript and a procedure about the same fault share the same nouns (PoE, jack,
register, extension), which is what BM25 rewards; an unrelated call gets no excerpts at all
(min_terms), so the prompt only grows when there is something to say. retrieve() is the seam an
embedding-backed index would plug into later.

Pure functions except docs_for()/retrieve_for(), which read the kb_* tables."""

import hashlib
import math
import re

CHUNK_CHARS = 700          # target chunk size; paragraphs are merged up to this
MAX_DOC_CHARS = 400_000    # a document is capped at this on upload
K1, B = 1.5, 0.75

STOP = set("""a about above after again against all am an and any are as at be because been before being below
between both but by can could did do does doing down during each few for from further had has have having he her
here hers herself him himself his how i if in into is it its itself just let me more most my myself no nor not now
of off on once only or other our ours ourselves out over own same she should so some such than that the their theirs
them themselves then there these they this those through to too under until up very was we were what when where
which while who whom why will with would you your yours yourself yourselves ok okay yes yeah um uh hi hello thanks
thank please sorry right well got get go going one two three like know see say said sure just also still even
back call calling today morning help going want need let us think thing things really actually mean okay""".split())


def tokens(text):
    """Lower-case word tokens, stopwords dropped, light suffix stripping (plural / -ing / -ed)."""
    out = []
    for w in re.findall(r"[a-z0-9][a-z0-9'-]*", (text or "").lower()):
        w = w.strip("'-")
        if len(w) < 2 or w in STOP:
            continue
        if len(w) > 4 and w.endswith("ies"):
            w = w[:-3] + "y"
        elif len(w) > 5 and w.endswith("ing"):
            w = w[:-3]
        elif len(w) > 4 and w.endswith("ed"):
            w = w[:-2]
        elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return out


def chunk(text, size=CHUNK_CHARS):
    """Paragraph-merged chunks of about `size` chars, each starting with the previous chunk's last
    paragraph so a fact split across a boundary is still found. Headings stay with what follows."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", (text or "").replace("\r\n", "\n")) if p.strip()]
    # a very long paragraph is split on sentence ends
    flat = []
    for p in paras:
        if len(p) <= size * 1.5:
            flat.append(p)
            continue
        cur = ""
        for sent in re.split(r"(?<=[.!?])\s+", p):
            if cur and len(cur) + len(sent) + 1 > size:
                flat.append(cur)
                cur = sent
            else:
                cur = (cur + " " + sent).strip()
        if cur:
            flat.append(cur)
    out, cur, prev_tail = [], [], None
    for p in flat:
        if cur and sum(len(x) + 2 for x in cur) + len(p) > size:
            out.append("\n\n".join(cur))
            prev_tail = cur[-1]
            cur = [prev_tail] if len(prev_tail) < size // 2 else []
        cur.append(p)
    if cur and (not out or "\n\n".join(cur) != out[-1]):
        out.append("\n\n".join(cur))
    return out


class Index:
    """BM25 over chunks. chunks: [{document_id, title, seq, text}]."""

    def __init__(self, chunks):
        self.chunks = list(chunks or [])
        self.tf, self.df, self.lens = [], {}, []
        for ch in self.chunks:
            toks = tokens(ch.get("text"))
            tf = {}
            for t in toks:
                tf[t] = tf.get(t, 0) + 1
            self.tf.append(tf)
            self.lens.append(len(toks) or 1)
            for t in tf:
                self.df[t] = self.df.get(t, 0) + 1
        self.n = len(self.chunks)
        self.avg = (sum(self.lens) / self.n) if self.n else 1.0

    def idf(self, t):
        df = self.df.get(t, 0)
        return math.log((self.n - df + 0.5) / (df + 0.5) + 1.0)

    def search(self, query_text, k=6, max_chars=5000, min_terms=2):
        """Ranked hits [{document_id, title, seq, text, score, terms}] within k chunks and max_chars.
        A chunk needs at least `min_terms` distinct informative query terms, so an unrelated query
        returns nothing rather than the least-unrelated chunk."""
        if not self.n:
            return []
        q = set(tokens(query_text))
        if not q:
            return []
        tiny = self.n < 4                       # in a tiny corpus idf cannot separate common from rare
        scored = []
        for i, ch in enumerate(self.chunks):
            tf, ln = self.tf[i], self.lens[i]
            score, matched = 0.0, []
            for t in q:
                f = tf.get(t)
                if not f:
                    continue
                idf = self.idf(t)
                if idf >= 0.5 or tiny:
                    matched.append(t)
                score += idf * f * (K1 + 1) / (f + K1 * (1 - B + B * ln / self.avg))
            if len(matched) >= min_terms and score > 0:
                scored.append((score, i, sorted(matched)))
        scored.sort(key=lambda x: (-x[0], x[1]))
        # a long transcript matches every chunk on a few common nouns: keep only chunks that score
        # at least half as well as the best one, so the billing section stays out of a phone fault
        if scored:
            floor = scored[0][0] * 0.5
            scored = [x for x in scored if x[0] >= floor]
        out, used = [], 0
        for score, i, matched in scored:
            ch = self.chunks[i]
            if len(out) >= k or used + len(ch.get("text") or "") > max_chars:
                if out:
                    break
                # the single best chunk is always allowed, cut to the cap
            text = (ch.get("text") or "")[:max_chars]
            used += len(text)
            out.append({"document_id": ch.get("document_id"), "title": ch.get("title"), "seq": ch.get("seq"),
                        "text": text, "score": round(score, 3), "terms": matched[:12]})
        return out


def fingerprint(docs):
    """A short key for the set of enabled documents in scope (ids + updated_at); '' when none.
    Stored on the scorecard so 'the same audit' means the same reference material too."""
    rows = sorted("%s|%s" % (d["id"], d["updated_at"]) for d in docs)
    if not rows:
        return ""
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()[:32]


def docs_for(db, rubric_id):
    """Enabled, undeleted documents scoped to every QA type or to this one."""
    return [dict(r) for r in db.execute(
        "SELECT id, title, updated_at, rubric_id FROM kb_document WHERE enabled=1 AND deleted_at IS NULL"
        " AND (rubric_id IS NULL OR rubric_id=?) ORDER BY title COLLATE NOCASE", (rubric_id,))]


def index_for(db, docs):
    chunks = []
    for d in docs:
        for r in db.execute("SELECT seq, text FROM kb_chunk WHERE document_id=? ORDER BY seq", (d["id"],)):
            chunks.append({"document_id": d["id"], "title": d["title"], "seq": r["seq"], "text": r["text"]})
    return Index(chunks)


def retrieve_for(db, rubric_id, query_text, k=6, max_chars=5000):
    """(hits, key) for a scoring run: the excerpts to show the scorer and the fingerprint to store."""
    docs = docs_for(db, rubric_id)
    key = fingerprint(docs)
    if not docs:
        return [], key
    return index_for(db, docs).search(query_text, k=k, max_chars=max_chars), key


def excerpt_block(hits):
    """The prompt section; '' when there is nothing to add."""
    if not hits:
        return ""
    parts = ["[%s §%d]\n%s" % (h.get("title") or "document", int(h.get("seq") or 0) + 1, h.get("text") or "") for h in hits]
    return "\nKNOWLEDGE BASE EXCERPTS (reference material; see rule 13):\n<<<\n" + "\n\n".join(parts) + "\n>>>"


def public_hits(hits):
    """What is stored on the scorecard and shown in the UI: the excerpt, not the search internals."""
    return [{"document_id": h.get("document_id"), "title": h.get("title"), "seq": h.get("seq"),
             "chars": len(h.get("text") or ""), "text": h.get("text"), "score": h.get("score"), "terms": h.get("terms")} for h in hits]
