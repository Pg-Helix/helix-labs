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

## Revision 2026-11-21: measurement notes (definitions and targets unchanged)

This section records how `run-eval.sh`/`score.py` implement the rules
above, and three discrepancies found while doing so. No metric
definition, threshold, label, split or corpus file changed;
`HASHES.txt` still verifies.

- **Counts.** The hash-pinned `queries.jsonl` has 4 held-out
  `paraphrase` and 2 held-out `exact_term` queries (not 5 and 3), and a
  20 dev / 14 held_out split (not 17/17). Category totals match. Every
  reported figure states its actual `n`. The targets apply to those
  actual sets.
- **Label defect.** C3's snippet `does not change paid leave accrual`
  does not occur in `remote-work-policy.md` (`do not change ...`). Under
  the passage rule, C3 has no relevant chunk. It is a dev `distractor`
  and feeds no target or Decision D, so it is reported and excluded
  (`n_scored`). An unresolvable label on a held-out `paraphrase`/
  `exact_term` query or a dev `paraphrase` query fails the run instead.
  Fixing C3 requires its own visible revision here plus a `HASHES.txt`
  update.
- **Relevance** is resolved against each run's full chunk inventory
  (`chunks.jsonl`, the cortex chunk rows). Search snippets are not used
  because they are truncated to 200 characters. A chunk repeated within
  one result list counts once.
- **Unauthorized disclosure** is measured at all four ranking stages
  (FTS-only, dense-only, hybrid pre-rerank via the test-only
  `X-Eval-Stage` header, and the served `/search`), plus `/answers`. Bob
  holds no grant, so every request must be the documented `403
  forbidden`. The whole raw body, whatever its status, is scanned for
  restricted document/chunk ids, file names, sentences and labeled
  snippets. Alice runs the same queries at all four stages as a positive
  control: the fixture exists, and each stage path returns restricted
  results to an authorized caller.
- **no_answer** is scored on `POST /answers`, the endpoint that asserts
  an answer. "Top result" means its first citation. The answer is
  incorrect if that chunk is labeled relevant to any other query, and
  an answer with no citation counts as abstaining. The `/search` final
  top-1 is reported alongside but does not gate. Open ambiguity: the
  word "confidently" has no counterpart in the extractive contract,
  which exposes no confidence or abstention signal, so every cited
  answer counts as asserted. No threshold was invented. Resolving this
  needs a product decision (helix-cortex + helix-atlas) on whether and
  how `/answers` abstains.
- **Decision D** is recomputed from the real (reranker-on) pass's dev
  `paraphrase` queries in every scoring run. The held-out targets are
  evaluated on the configuration it selects: `real`, or `real-norerank`
  (`CORTEX_DISABLE_RERANK=1`). The report records that served profile,
  verified from the running container, not from a directory name.
- **Historical numbers.** Results produced before this revision carry no
  run metadata, so their provider, revision and completion cannot be
  verified, and they are not evidence for any gate. The figures
  0.091/0.803/0.924 quoted in helix-labs PR #6 are held-out Recall@5
  over all 11 ranked queries (FTS / final with reranker / hybrid), not
  the paraphrase subset.

## Revision 2026-11-22: answer behavior

The v1 corpus, queries, labels, splits and every threshold above are unchanged.

- **no_answer gate.** The open "confidently" ambiguity noted above is resolved by a
  product decision, helix-atlas ADR-0008. `/answers` now has an explicit decline: HTTP
  200, `answer_method` "extractive", `answer` "" and `citations` []. The gate (≥3 of
  4) counts only that exact decline. Any non-empty answer, and any violation of the
  array contract (missing or null `citations`), counts as asserting an answer. The
  historical reading (first citation not labeled for another query; no citation means
  abstained) is still reported as `historical_first_citation_correct`, but it no longer
  gates. Deleting the citations from unsupported prose used to pass it.
- **Answer contract, every authorized `/answers` response.** A non-empty answer must be
  exactly its cited passages' text (Cortex's extractive join). Every citation, not only
  the first, must name an ingested passage of the requested collection with the
  matching `source_id` and `source_digest`. A violation fails the pass's contract gate
  (exit 1). A transport failure, non-200 status or malformed JSON is invalid evidence
  (exit 2).
- **Positive controls.** A1, A4, B1 and B2 must each answer with their labeled passage
  and cite it. All four did on the baseline before the evidence rule existed, so a rule
  that declines too much fails here.
- **Outcome report.** Every authorized v1 query and every `answerability.jsonl` row is
  asked on `/answers`. Each result is classified as a correct cited answer, wrong answer,
  false decline or correct decline, with denominators per group: v1 dev, v1 held-out,
  answerability dev, answerability challenge, positive controls and no_answer.
  `answerability.jsonl` is new and hash-pinned. Its dev rows (with the v1 dev split)
  were used to choose the evidence rule. Its challenge rows had expected outcomes and
  passage references fixed (2026-11-22T15:09Z) before the rule existed, and they were
  never used to choose it. Only the controls and no_answer gate; the rest is reported.
  Row N5 asks an authorized collection with no passages.
- **Evidence checks added.** `run.json` must be a JSON object with 40-hex revisions and
  a boolean dirty flag, and its executed-request count must equal the request plan. C3's
  responses are still validated although its defective label keeps it out of the
  metrics.

## Revision 2026-11-23: calibration record

The v1 corpus, queries, labels, splits, thresholds and gates are unchanged, and so is the
served cutoff.

- **Replay.** `sh eval/calibrate.sh` (after `prepare-checkouts.sh`) replays the `/answers`
  threshold selection from the repository against helix-foundry's pinned Ollama image. The
  script refuses to run if the served weights digest differs from the pin. It selects the 27
  development cases before any embedding call (v1 dev split on the public collection plus
  `answerability.jsonl` dev). It must reproduce the committed `eval/calibration.json`, which
  holds the input hashes, model and runtime pins, prefixes, every per-case value, the
  objective, the tie-break and the rounding. The result is midpoint 0.7070457, rounded to
  0.707, with 24/27 correct on the document-level measure (23/26 without C3; C3 counts by
  document name). CI's real-model-eval job runs the replay first.
- **Across CPU architectures.** The record was made on arm64. The same pinned weights on
  GitHub's x86_64 runner give values up to 4.7e-4 different: the midpoint is 0.7072474 instead
  of 0.7070457, with the same 24/27, the same rounded 0.707 and every case on the same side of
  it. The case closest to the cutoff (N1) is 5.7e-3 away. A replay must therefore match the
  record exactly in case selection, each case's side of 0.707, the correct count and the
  rounded cutoff, and match its decision values within 1e-3.
- **What the measure covers.** The calibration measure is document-level: does a labeled
  document clear the cutoff, or does every document stay below it. It does not score whether
  the assembled answer is complete or free of unrelated extracts. Neither does the served
  "correct answer" class: it requires one cited labeled passage and an expected snippet. The
  positive controls and the no_answer gate are valid under those stated criteria.
- **Resolved profile.** Each pass's `run.json` now records the embedding profile Cortex
  resolved at startup. cortex-api and chunk-worker must log the same one. Real passes must show
  the calibrated rule (`answer_rule=cosine>=0.707`) and mock passes `answer_rule=none`. The chunk worker's logged effective input must be `chunk-text` behind the profile's document prefix (`search_document: ` for real, empty for mock), which is the preprocessing the calibration measured.

## Revision 2026-11-23 (2): evidence integrity

The fixtures, thresholds, gates, cutoff and product behavior are unchanged.

- **Attempts.** Once `run-eval.sh` has accepted an output directory (not a symlink;
  either empty or carrying its marker), it removes the previous contents and writes
  `run.json` with `completed: false` before it sources helpers or starts Compose. A
  failed startup therefore leaves nothing scoreable, and `score.py` exits 2. Completion
  is written only after every planned request has finished, via an atomic rename.
- **Transport status (`run.json` schema 2).** Every request records curl's own exit
  status as `<name>.curl`, next to `.code` and `.json`. The runner aborts the attempt,
  leaving it incomplete, on any nonzero status, even when the HTTP status and body
  look right. For example, curl 18 means the connection closed before the advertised
  length. A missing, malformed or nonzero `.curl` is invalid evidence (exit 2), reported
  as profile/query/stage. Exit 1 still means valid evidence with a missed target.
- **Historical artifacts.** Schema 1 artifacts (everything before this revision,
  including CI artifacts 14701162129, 14760491989, 14805346665 and 14808741266) carry
  no transport status. The current scorer rejects them by schema. To reproduce their
  results, use the scorer from the helix-labs commit their `run.json` records under
  `revisions.helix-labs.commit`:
  `git -C helix-labs worktree add /tmp/scorer-<commit> <commit>`, then
  `python3 /tmp/scorer-<commit>/eval/score.py --inputs /tmp/scorer-<commit>/eval --mock … --real … --real-norerank …`.
- **Workflow 2 background requests** are reaped individually. A transfer that failed is
  reported as `curl-exit-N(http X)` and fails its scenario, even if its status line was
  the expected 201 or 403. The in-flight barrier poll counts readiness only from
  completed transfers.
- **Calibration replay** also compares the whole recorded `model` object: wire tag,
  dimensions, metric, prefixes, runtime image digest and weights digest. Any difference
  returns 1. The 1e-3 cross-architecture value tolerance and the exact per-case cutoff
  decisions are unchanged. An output path that resolves to the reference is refused
  (exit 2) before anything is written. `CALIBRATION_REFERENCE` points the replay at a
  disposable reference.
- `eval/test_evidence.py` runs these checks offline in CI, through the real scripts.
