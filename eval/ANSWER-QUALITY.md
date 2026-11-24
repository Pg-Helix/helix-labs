# Answer-quality benchmark v1: scoring rubric

This file is frozen together with `answer-quality-v1.json` (the fixture), its SHA-256 in
`HASHES.txt` and the corpus hash it records. The fixture and this rubric were committed
before any of its new questions was sent to `POST /v1/answers`.

## Scope

- **What is measured:** the extractive answers of the currently served profile,
  `real-norerank`. That is nomic-embed-text:v1.5, the reranker disabled, and the calibrated
  0.707 cutoff (helix-atlas ADR-0008).
- **What is not changed:** the existing retrieval evaluation (201 requests per profile, 13
  gates), the model, the reranker setting, the cutoff and product behavior.
- **No pass/fail target.** This is a baseline. A low score is a valid result. The benchmark
  exits 2 only when the evidence is invalid.

## Fixture (`answer-quality-v1.json`)

- **Twelve new questions** on the public collection, asked as `alice`:
  - four `single_fact` paraphrases;
  - four `multi_passage` questions that need facts from two or three documents;
  - four `unsupported` questions. Each one is near a policy that exists, listed under
    `neighbors`, but nothing in the corpus answers it.
- **New means new:** no id and no normalized text is shared with `queries.jsonl` or
  `answerability.jsonl`.
- **Required facts:** every answerable question lists them. Each fact records:
  - its source `document`;
  - a supporting `quote`, verbatim, occurring exactly once in that document;
  - `alternatives`: other verbatim `{document, quote}` pairs that state the same fact.
    In v1 each fact is stated in only one public document, so none are recorded.
- **Corpus pin:** the combined corpus hash and each referenced document's hash are pinned.
- **Reserved for evaluation:** never used for calibration, cutoff selection, tuning or
  development. Changing a question, fact or quote requires a new version file, never an
  edit of v1.
- **Diagnostics, outside the headline:** D1, H3 and H7 are the known examples from the
  existing sets. They are labeled the same way and reported separately.

## Label validity (`answer_quality.py check`; repeated at score time)

- Every quote must resolve to exactly one passage of its document in the run's own chunk
  inventory.
- Each answerable question's facts must fit the API's answer bound (helix-cortex
  `AssembleExtractiveAnswer`): at most 3 passages whose stripped texts, joined by `" ... "`,
  fit in 2,000 bytes.
- A label that does not resolve is invalid evidence (exit 2), never a quality result.

## Evidence validity (exit 2 on any failure)

The benchmark reuses the existing scorer's checks (`score.py`):
- **Run metadata:** `run.json` schema 2, `completed: true`, profile `real-norerank`,
  provider, model, digest, reranker, answer rule, chunk-worker input, input hashes and
  40-hex revisions.
- **Mapping and inventory:** the document mapping and the full chunk inventory.
- **Responses:** one `.json`, `.code` and `.curl` per planned request, with curl exit 0,
  HTTP 200 and JSON.

In addition, `run.json` must record `suite.name = answer-quality`, the fixture's file name
and its SHA-256, which must equal the current fixture and `HASHES.txt`. It also records the
commit that last changed the fixture. The plan is exactly 15 `/answers` requests: the 12
questions plus the 3 diagnostics.

## What is credited

**Contract and citations (reused).** A decline is HTTP 200 with
`{"answer": "", "answer_method": "extractive", "citations": []}`. A non-empty answer must be
the cited passages' own stripped texts, joined by `" ... "` in citation order, with only the
last one possibly cut at the 2,000-byte bound. A citation is valid when:
- `chunk_id` names an ingested passage of the public collection;
- its `source_id` is that passage's document;
- its `source_digest` matches.

**Extract per citation.** The answer is split, in citation order, into the returned extract
for each citation: the part of the returned text that came from that passage, after any
truncation. Scoring uses this returned text, never the inventory's full passage. If the
answer is not the cited passages' own text, no extract can be associated with a citation and
no fact is credited.

**Fact credit.** A required fact is credited only when its quote, or an alternative, appears
verbatim (byte-exact substring) in a returned extract that meets both conditions:
- its citation is valid;
- its citation's document is the document the quote belongs to.

So:
- a fact cut off by truncation is not credited;
- a quote attributed to a different document's citation is not credited.

## Outcomes per question

| Expect | Response | Outcome |
|---|---|---|
| answer | every required fact credited | `complete` |
| answer | some, not all, required facts credited | `partial` |
| answer | non-empty answer with no fact credited, or any contract violation | `wrong` |
| answer | decline | `false_decline` |
| decline | decline | `correct_decline` |
| decline | any non-empty answer, or any contract violation | `wrong` |

**Missing facts.** For an answerable question, a missing fact is a required fact that was not
credited. This includes every fact of a falsely declined question.

**Unrelated extracts.** A returned extract that credits no required fact of its question is
unrelated. Every extract returned for an unsupported question is unrelated.

## Report

The headline covers the 12 questions only:
- outcome counts out of their denominators: answerable n=8, unsupported n=4, total n=12;
- credited and missing required facts out of all 15 required facts. Missing facts are also
  split into those in answered responses and those in declined ones;
- unrelated extracts out of all returned extracts;
- per question: the outcome, credited and missing fact ids, each extract's document, length
  and whether it was truncated, and any contract violation.

The diagnostics are scored by the same rules and listed separately. The report also records
the fixture SHA-256 and commit, the run's revisions and the scorer revision, so an
independent rescore needs only the results directory and the helix-labs checkout.
