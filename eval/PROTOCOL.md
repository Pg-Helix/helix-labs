# Retrieval evaluation protocol

Checked in **before** any ranking configuration is tuned or any held-out
number is looked at, per this milestone's own requirement: metric
definitions, split, baseline configuration and numerical acceptance
targets are fixed here first. If a later report changes any of these
without a visible, explained revision to this file, treat the later
number as untrustworthy.

This establishes a **limited local retrieval result** on one small,
synthetic corpus — not a general claim about model quality, and not an
answer-quality benchmark (answers stay deterministic extracts; see
`helix-cortex/internal/retrieval`).

## Corpus and queries

- `corpus/*.md` (15 documents) + `corpus/restricted/*.md` (2 documents,
  a separate collection no eval principal but the designated "owner" is
  granted). All synthetic, invented for this evaluation — no real
  company, no secrets, no personal data.
- `queries.jsonl` (34 labeled queries), one JSON object per line:
  `id`, `text`, `category` (`exact_term` | `paraphrase` | `distractor` |
  `multi_passage` | `no_answer` | `unauthorized`), `split` (`dev` |
  `held_out`), `collection` (`public` | `restricted`), `relevant` (a
  list of `{document, snippet}` — the snippet is a substring expected
  inside the correct chunk's text, resolved against whatever chunk
  boundaries the real chunker actually produces at eval time, never a
  hand-picked chunk index).
- Category counts: 6 exact_term, 10 paraphrase, 6 distractor, 5
  multi_passage, 4 no_answer, 3 unauthorized = 34 total. 17 dev / 17
  held_out (both query sets are stratified across every category).
- `HASHES.txt`: sha256 of every corpus file, of `queries.jsonl`, and of
  the whole corpus concatenated in sorted path order. A later run that
  doesn't reproduce these hashes is not evaluating this corpus.

## What "relevant" means

A chunk counts as relevant to a query iff it comes from one of that
query's labeled `relevant` documents AND its text contains that
document's labeled snippet. `no_answer` queries have an empty
`relevant` list by definition — a system that returns zero confident
results, or results whose snippets don't overlap with the query's
intent, is correct, not the thing a recall score should punish.
`unauthorized` queries are run under a principal who holds NO grant on
the `restricted` collection; the ONLY correct outcome is zero results
and zero disclosed text — this is checked as a hard pass/fail gate, not
averaged into the ranking metrics below.

## Metrics

- **Recall@5**: for a query with a non-empty relevant set, the fraction
  of its labeled-relevant chunks that appear in the top 5 results.
  Undefined (excluded from the average) for `no_answer` queries.
- **nDCG@10**: standard normalized discounted cumulative gain over the
  top 10 results, with a binary relevance gain (1 if the chunk is
  labeled relevant, 0 otherwise) and log2 position discount. Undefined
  for `no_answer` queries for the same reason.
- **no_answer correctness**: for the 4 `no_answer` queries, whether the
  system's top result (if any) is NOT labeled relevant to any OTHER
  query's answer key by coincidence — i.e., it didn't confidently
  assert an answer that happens to be some unrelated document's content.
  Reported as a pass/fail count, not folded into Recall/nDCG.
- **Unauthorized-disclosure count**: for the 3 `unauthorized` queries,
  the count of responses (across every ranking stage measured, not just
  the final one) that returned ANY text or chunk/document identifier
  belonging to the restricted collection. The target is exactly 0,
  always, for every stage — this is the one metric in this protocol
  with no tolerance.

## Ranking stages compared

All five read the SAME ingested corpus and the SAME candidate limits;
only the ranking signal differs:

1. **FTS-only** — `to_tsvector`/`ts_rank` order, no vector component at
   all. Provider-independent (text-only), computed once.
2. **Mock** — the full hybrid-plus-rerank pipeline, backed by
   helix-infer's deterministic Mock provider. Explicitly labeled a
   **plumbing baseline**: Mock's vectors carry no real semantic
   content, so this measures "does the pipeline run correctly," not
   "is dense retrieval any good."
3. **Dense-only (real)** — pgvector cosine order alone, backed by the
   real nomic-embed-text/Ollama profile, no FTS and no rerank.
4. **Hybrid RRF (real, pre-rerank)** — `retrieval.RRF` fusing FTS +
   real dense candidates, k=60, BEFORE `retrieval.Rerank` is applied.
5. **Final served ranking (real)** — the actual `/search` response:
   hybrid RRF then the lexical-overlap reranker, exactly what a caller
   receives today.

## Baseline configuration (fixed, not tuned per query)

- `candidateLimit` = 50 (helix-cortex's existing constant, unchanged).
- `top_k` = 10 for every measurement (covers both Recall@5 and nDCG@10
  within one retrieved list).
- RRF constant k = 60 (helix-cortex's existing `DefaultRRFConstant`,
  unchanged).
- Reranker: helix-cortex's existing deterministic lexical-overlap
  reranker, unchanged, exactly as shipped.
- Embedding binding for the "real" stages: nomic-embed-text:v1.5, 768
  dims, cosine metric, `search_query: `/`search_document: ` prefixes
  (helix-cortex's `embedconfig` package) — see helix-foundry's
  real-embeddings profile for the exact pinned image/model digests.
- No query-specific tuning anywhere in this list. The ONE question this
  milestone's development split is allowed to answer before looking at
  held-out numbers is Decision D below (reranker fate) — nothing else
  changes between dev and held-out evaluation.

## Acceptance targets (set now, evaluated once on held-out)

- **Paraphrase benefit**: mean Recall@5 over the 5 held-out `paraphrase`
  queries for stage 5 (final served ranking, real) must exceed stage 1
  (FTS-only) by at least +0.15 (absolute). This is the one number this
  milestone exists to produce: proof that real embeddings measurably
  help exactly the case lexical search is weak at.
- **No unacceptable exact-term regression**: mean Recall@5 over the 3
  held-out `exact_term` queries for stage 5 must not fall more than
  0.10 below stage 1. Real embeddings must not make the easy case worse
  by a meaningful margin.
- **Unauthorized disclosure**: 0, every stage, no exceptions (see
  above) — a single disclosure fails this milestone's security
  requirement regardless of every other number.
- **no_answer correctness**: at least 3 of 4 held-out-plus-dev
  `no_answer` queries (pooled, since there are only 4 total) must not
  assert a confidently-wrong answer.

These thresholds are fixed before the held-out set is ever evaluated.
If held-out results don't meet them, that is reported as a missed
target, not a reason to adjust the corpus, relabel a query, or lower
the bar after the fact.

## Decision D: should the reranker apply to the real binding?

helix-cortex's reranker sorts lexical-overlap first and uses RRF only
to break ties (`internal/retrieval/rerank.go`) — a real embedding
alone may therefore contribute nothing to paraphrase ranking if the
reranker's lexical-overlap term dominates it. This is evaluated ONCE on
the **dev** split, comparing stage 4 (hybrid, pre-rerank) against stage
5 (final, post-rerank) specifically on dev's paraphrase queries:

- If stage 5's dev-paraphrase Recall@5 is >= stage 4's, the reranker is
  kept as-is for the held-out run and the final report.
- If stage 5 is measurably worse than stage 4 on dev's paraphrase
  queries, the reranker is made optional (an explicit flag, default
  unchanged) for the held-out run and final report, and that decision
  -- with the dev numbers that drove it -- is recorded in the results
  report, never silently.

This is the only tuning decision this protocol allows between dev and
held-out; it is made from dev data only and verified (not re-decided)
on held-out.
