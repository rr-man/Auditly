# Ask Auditly — design and recommendations

**Status:** design accepted 2026-09-21 · Phase 1 built in v0.46.0 · Settings card and editable prompt in v0.47.0 · voice input in v0.48.0 (server-side STT via the desk's provider; needs HTTPS) · the app serves its own HTTPS twin on 8444 in v0.49.0 (self-signed until IT issues a certificate)
**Audience:** whoever decides whether agents get logins, and whoever maintains this code next

0.43.0 put a launcher in the lower right of every screen and a panel that said, honestly, that the
assistant was not switched on. This document is the answer to *what should be behind it*, and the
reasoning behind the answer. It is deliberately longer than the code it describes: the hard parts here
are the access-control and evaluation decisions, not the plumbing.

## The questions, answered

1. **How it is added.** One module (`assistant.py`), one endpoint (`POST /api/ask`), and a composer in
   the panel that already exists.
2. **Its parameters.** Model, temperature, token ceiling, context budget, per-person rate limit, daily
   cap and a retention period — all `.env` keys, following the `SCORING_*` / `SMTP_*` pattern.
3. **Does it need its own prompt? Yes.** See *Why the prompt is separate*.
4. **Does it need its own RAG? Half.** See *Two retrievers, and why the corpus decides*.
5. **Scope.** One call at a time, QA reviewers, answers only. See *Phasing*.
6. **Both QA and agents?** The right destination, and much larger than the chatbot. See *Access*.
7. **Another OpenAI key?** Not required; a separate *project* key is recommended before agents get
   access. See *Keys and cost*.

## What the data actually looks like

Measured on the live instance, 21 September 2026:

| | |
|---|---|
| scorecards | 103 |
| recordings / transcripts | 73 / 76 |
| submitted reviews | 21 |
| people (agents + QA) | 14 |
| user accounts | 1 |
| knowledge-base documents | **0** |

Transcript length: median 3,668 characters (~917 tokens), 90th percentile 7,107, longest 9,882.

Two facts follow, and they decide the architecture.

**A whole call fits in the context window.** For questions about one call — which is all of Phase 1 —
there is no retrieval problem to solve. The scorecard and the full transcript go in as data. Chunking,
ranking and top-k tuning would be machinery in service of a problem this corpus does not have.

**Nobody has uploaded a reference document yet.** The document-retrieval half has nothing to retrieve on
day one. It is wired up because reusing `kb.py` is nearly free, but it is not what the feature is for.

## Two retrievers, and why the corpus decides

Most of what a QA asks about is **structured**: what was this criterion rated, what was the evidence,
what did the reviewer write, what does the QA type require. Structured facts are fetched with SQL and
handed over as data. Chunking a scorecard and ranking the chunks would lose the structure that makes
the answer checkable.

The second retriever is the existing BM25 over knowledge-base documents, reused unchanged. One caveat
found during the survey and worth recording: **a short question makes a bad BM25 query.** The stopword
list strips call-centre filler, and the index requires at least two informative terms before it returns
anything. The scoring path gets away with this because it queries with the whole transcript. "What did
they miss?" reduces to almost nothing. So the assistant queries with the question *plus* the call's
text, never the question alone.

No vector database, no embeddings. That is a considered choice, not an omission: the corpus is one call
plus a handful of procedures, BM25 is already here, it costs nothing, it adds no dependency, and the
house rule is stdlib-only. `kb.retrieve_for()` is the seam an embedding index would plug into later.

## Why the prompt is separate

`score.PROMPT_VERSION` participates in the identical-audit cache key, alongside the transcript, rubric
version, provider, model, guidance mode and knowledge-base fingerprint. If the assistant shared it,
every reword of the chatbot's instructions would invalidate every cached audit and re-bill the next
re-run of each one. So `assistant.ASK_PROMPT_VERSION` is its own integer, recorded on every answer, so
a bad answer can be traced to the prompt that produced it.

## Treating the transcript as hostile

The standard advice is to treat retrieved content as untrusted. Here it is sharper than usual: a
transcript is **a member of the public talking**. "Ignore your previous instructions and…" is a
sentence anyone can say into a support call, and it would land in the assistant's context as data. This
is indirect prompt injection requiring no attacker sophistication at all.

The defence is structural, not a phrase in the prompt:

- Instructions and data are separated, and the data is fenced and labelled as a recording of speech.
- The system prompt states that nothing inside the data block is an instruction.
- The assistant has no tools and no write access, so the worst case of a successful injection is a
  wrong answer on screen, not an action taken.
- An injection suite in the test file proves the behaviour rather than assuming it.

That last point matters most. A prompt instruction is not a control; a test is evidence.

## Access — the part that is bigger than the chatbot

The brief asked for QA reviewers *and* agents to use the assistant. That is the right destination. It is
also gated behind work this application has never done.

**There is no row-level authorisation anywhere in Auditly.** `current_user()` is checked once at the
top of the router; after that line every signed-in user can read every call, scorecard, review, coaching
note and dispute. Concretely: the Calls list has no agent predicate and its search covers caller names
and emails; any signed-in user can stream any customer's audio by id; the people endpoint returns the
whole staff roster with personal email addresses; the Dashboard's `?agent=` is a presentation filter
over a query that pulls every scorecard, and it still returns a ranked leaderboard of colleagues.

None of this has mattered, because every account belongs to a reviewer or an admin. It matters the
moment an agent can sign in — and an assistant is the worst place to discover it, being precisely a
machine for summarising what an agent must not read about colleagues.

Three further facts:

- **The LAN deployment has no sign-in at all.** `AUDITLY_OPEN_ACCESS=1` makes every visitor the same
  admin row, so there is no identity to scope against. Agent access and open access are mutually
  exclusive.
- **Nothing links a user to an agent.** `user` has no `person_id`; `recording.agent_name` is a bare
  string with no foreign key. And `user.role` carries a table-level `CHECK (role IN ('admin','reviewer'))`
  which SQLite cannot alter — admitting `'agent'` needs a table rebuild.
- **The PRD already says so.** Agent self-service login is listed out of scope for v1, and the roadmap
  defines the agent view precisely: their own trend and the site average, nothing else.

So agent access decomposes into four pieces, only the last of which is the chatbot: the role and the
link; a session-derived scope threaded through roughly fifteen endpoints including the audio stream; a
narrower field set for agents even on their own calls, decided in code rather than asked of the model;
and then the assistant, which by that point is easy.

**Decision: QA reviewers first. Agent access is its own project, with its own design and review.** Not
because agents should not have it, but because giving it to them safely means building an authorisation
layer that does not exist, and that deserves better than being carried in on the back of a chat panel.

## Keys and cost

**Functionally, no second key is needed** — `OPENAI_API_KEY` serves chat completions.

**Operationally, a separate OpenAI *project* key is recommended before agents get access**, for four
reasons: spend attribution (one key covers transcription and scoring today; adding chat makes the bill
one number with three causes); its own hard cap, so a runaway loop cannot starve the transcription
budget; its own rate limit, so questions cannot throttle scoring jobs; and revocation without
collateral damage. `ASK_API_KEY` falls back to `OPENAI_API_KEY` when unset, so deferring costs nothing.

Per question, on `gpt-4.1-mini` at $0.40/$1.60 per million tokens: roughly 3,000–3,700 input tokens
(system prompt, scorecard, transcript, recent turns) and ~300 out. About **$0.002 a question** — fifty a
day is around $3 a month, five hundred a day around $30. Cost is not the reason to be careful here.

## Management

- **Settings › Server › Assistant** (admin, shipped 0.47.0): on/off, model, daily cap, per-person rate limit,
  questions asked and what they cost, the editable instructions as immutable versions with the safety
  rules locked and always appended, and a Try it box through the real path. Settings override the `.env`
  keys; the key stays in `.env`. Off by default. (The golden set runs in the test suite rather than from
  the card, since the demo provider does not read the prompt and a real run would spend money.)
- **Its own audit verb.** `assistant.ask`, never `scorecard.view` — an assistant reading scorecards on
  someone's behalf would otherwise flood and destroy the meaning of the "who looked at this call" trail.
- **The daily cap is nearly free**, counting `audit` rows for the day, exactly as optimisation runs do.
- **The spend gate applies.** No key or no `AUDITLY_ALLOW_SPEND=1` and the panel says so plainly instead
  of failing when someone presses Send.
- **Demo mode answers with no provider**, so the whole feature demonstrates and tests without a key.
- **A retention asymmetry worth a decision.** The sweep deletes audio only; transcripts, scorecards and
  customer details are kept indefinitely. An assistant answering from a two-year-old transcript is
  answering from data whose recording was deleted long ago. That is pre-existing, not caused by this
  feature, but the feature makes it visible and someone should own it.

## Evaluation

- **A golden set** of fixed questions against the seeded demo call, each with the citation it must
  produce, run by the test suite against the demo provider — no key, no cost, runs on every change.
- **Every answer is checked four ways**: it cites something; the citation exists; a question whose
  answer is not in the record is refused rather than invented; and out-of-scope questions are declined.
- **An injection suite**, as above.
- **A visible baseline**, so a prompt change that trades accuracy for fluency shows up as a number
  going down rather than as a feeling.

## Phasing

| Phase | What | Blocked on |
|---|---|---|
| **1 — one call, QA, answers only** | The scorecard, criteria, evidence, review and transcript of the open call. Cites everything, writes nothing. | nothing — **built in 0.46.0** |
| 2 — documents | Knowledge-base excerpts once documents exist. | someone uploading one |
| 3 — agents | The authorisation layer, then the bot follows. | its own design review |
| 4 — across calls | Trends, answered from `dashboard.py` aggregates rather than retrieval. | Phase 1 in daily use |

## Sources

[Engineering the RAG Stack](https://arxiv.org/pdf/2601.05264) ·
[RAGOps](https://arxiv.org/pdf/2506.03401) ·
[Secure multitenant RAG (Microsoft)](https://learn.microsoft.com/en-us/azure/architecture/ai-ml/guide/secure-multitenant-rag) ·
[Permission-aware retrieval](https://tianpan.co/blog/2026/05/04/permission-aware-retrieval-enterprise-rag-access-control) ·
[OWASP prompt injection prevention](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html) ·
[Indirect prompt injection](https://www.lakera.ai/blog/indirect-prompt-injection) ·
[Golden dataset evaluation](https://langfuse.com/resources/engineering/golden-dataset-evaluation) ·
[Prompt versioning](https://www.braintrust.dev/articles/what-is-prompt-versioning) ·
[OpenAI production best practices](https://developers.openai.com/api/docs/guides/production-best-practices) ·
[Managing OpenAI projects](https://help.openai.com/en/articles/9186755-managing-projects-in-the-api-platform) ·
[OpenAI pricing](https://developers.openai.com/api/docs/pricing)
