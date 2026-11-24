# Experiment: best-matching-sentence support for `/answers` (pre-registered)

Recorded and committed before the candidate is scored on any data. Changing anything below
after the comparison has run requires a new, separately recorded experiment.

## Question

Does scoring the best-matching sentence within an authorized passage recover more requested
facts, while keeping the correct declines?

## Control

The current Cortex deployment (`2fcaa31`), unchanged: the served `real-norerank` profile.
- A passage supports an answer iff the query-to-passage cosine is at least **0.707**.
- Up to 3 supported passages, in served order, are joined by `" ... "` and cut at 2,000
  bytes.

## Data: development only

These are the 27 calibration development cases, chosen by `calibrate.py select_cases()` from
labels and splits only:
- the v1 dev split on the public collection (18);
- `answerability.jsonl` set=dev (9).

The 12 frozen benchmark questions (`answer-quality-v1.json`) and every held-out or challenge
row are excluded from this replay.

**Evidence.** One fresh `real-norerank` stack at the pinned revisions, run as
`EVAL_SUITE=dev-replay sh run-eval.sh real-norerank`. For each case it records:
- the ordinary served `/search` with `top_k` 50, which gives the full served candidate order
  (the public collection has 15 passages, under the 50-candidate limit);
- the live `/answers` response.

The run uses the existing transport, provenance and response checks (`score.py`).

**Model.** The pinned nomic-embed-text:v1.5. Its weights digest is checked against
helix-foundry's pin. Prefixes are `search_query: ` for queries and `search_document: ` for
documents. Passage vectors embed `search_document: ` plus the stored chunk text, as the chunk
worker does.

## Reproduce the control first

For every case, replaying the control rule over the served order and recomputed passage
cosines must reproduce the live `/answers` exactly: the same answer text and the same cited
chunk ids in the same order. Any mismatch stops the experiment (exit 2) before the candidate
is scored.

The control must also reproduce the independent development reference. Coverage counts
exclude C3, whose historical snippet does not resolve; it is reported separately.
- labeled snippets returned: **16/23**;
- answerable cases complete: **14/19**;
- unsupported cases declined: **7/7**;
- returned extracts with no matching development label: **5**.

## Candidate

- **Sentences.** Each authorized candidate passage is split deterministically. Strip it, then
  split at the whitespace that follows `.`, `!` or `?` (`re.split(r"(?<=[.!?])\s+", text)`),
  dropping empty pieces.
- **Support score.** Each sentence is embedded as `search_document: ` plus the sentence. The
  passage's support score is the highest query-to-sentence cosine.
- **Cache.** Sentence vectors are cached by SHA-256 of the model weights digest, the prefix and
  the sentence.
- **Unchanged from the control:**
  - the served order;
  - the collection scope, meaning only the served candidates;
  - at most 3 passages and 2,000 bytes;
  - the returned text is the original passage text with its original citation.
- Only the support test changes: `support >= candidate cutoff` replaces
  `passage cosine >= 0.707`.

## Candidate cutoff: selection method and tie-break

This is the accepted calibration method (`calibrate.py`, ADR-0008), applied to the new
support score. It uses development data only.
- **Decision value per case:**
  - answerable: the highest support score over its labeled documents' passages;
  - unanswerable: the highest over all public passages;
  - the empty collection: no value, so it always declines.
- **Objective:** maximize correct cases. An answerable case is correct iff its value is at
  least *t*. An unanswerable case is correct iff it has no value or its value is below *t*.
- **Candidates:** the midpoints between consecutive distinct values, ascending.
- **Tie-break:** the wider gap, then the lower midpoint.
- **Rounding:** the served candidate cutoff is the midpoint rounded to 3 decimals.
- **All 27 cases are used**, with C3 counted by document, exactly as the accepted
  calibration does. The result without C3 is reported as a sensitivity check and is not used
  for selection.

## Measures (both rules, on the same 27 cases)

- **Snippet coverage:** a labeled snippet counts as returned iff it appears in the returned
  extract of a citation of its own document. Denominator: 23, with C3 excluded.
- **Answerable cases (n=19):** complete (all labels returned), partial, wrong (answered with
  no label), or declined.
- **Unsupported cases (n=7):** declined or answered.
- **Off-label extracts:** returned extracts that contain no labeled snippet of their own
  question. The denominator is all returned extracts in the 26 scorable cases.
- **Positive controls** A1, A4, B1 and B2: each must be answered with its labeled snippet
  returned.
- **Embedding work:** sentences per passage, the added embedding calls (which are indexing
  cost; a query still embeds once), and query-time cosine comparisons.

## Go / no-go (all must hold)

1. Snippet coverage is above **16/23**.
2. At least **14** complete answers.
3. **All 7** unsupported cases are declined.
4. **All 4** positive controls are retained.
5. Off-label extracts are **5 or fewer**.

**On go:**
- freeze the candidate commit and settings (the cutoff and split rule);
- evaluate once on answer-quality v1, labeled as an offline replay. It uses the same
  served-order capture for the 12 frozen questions, with D1, H3 and H7 reported separately;
- then decide on implementation in Cortex.

**On no-go:** report the negative result and the criterion, or tradeoff, that ruled it out.
v1 is not touched.

## Result (2026-11-24): NO-GO (appended; nothing above was changed)

The experiment ran at pre-registration commit `0a0f26c` on a fresh `real-norerank` stack
(Foundry `ffc2102`, Kernel `0f5bec3`, Cortex `2fcaa31`). It was reproduced on CI amd64 in
[run 45367094247](https://github.com/Pg-Helix/helix-labs/actions/runs/45367094247).

**The control reproduced everything it had to:**
- all 27 live selections (answer text and citations);
- the 0.707 cutoff, re-derived from the same vectors at 24/27;
- the development reference: 16/23 snippets, 14/19 complete, 7/7 declines, 5 off-label
  extracts.

**Candidate cutoff** (pre-registered method): midpoint 0.6946594, rounded to **0.695**,
25/27 on arm64. On amd64 it is 0.6941823, rounded to 0.694, with identical selections. Without
C3 the result is also 0.695.

| Measure | Control 0.707 | Candidate 0.695 | Criterion |
|---|---|---|---|
| Labeled snippets returned | 16/23 | **18/23** | > 16 ✔ |
| Complete answers (n=19) | 14 | **15** | ≥ 14 ✔ |
| Unsupported declined (n=7) | 7 | **6** (N1 answered) | 7 ✘ |
| Positive controls A1/A4/B1/B2 | 4/4 | 4/4 | 4 ✔ |
| Off-label extracts | 5/21 | **7/25** | ≤ 5 ✘ |

**What changed under the candidate:**
- X3 is now complete.
- D3 is now partial.
- X2 adds an unrelated performance-review extract.
- N1 ("How many sick days do contractors get per year?") is answered with the sick-leave
  policy.

**The tradeoff that rules it out.** Sentence scoring lifts N1 to 0.7288, through the "ten sick
days … without a doctor's note" sentence, which lacks the decisive qualifier. That is above
positive control B1 (0.6962), and above B6 (0.6962), C1 (0.7006) and D1 (0.7059). Under this
signal, no cutoff can decline N1 without losing B1, so the gain in recovered facts cannot be
had while keeping every correct decline.

**Cost.** The 15 passages split into 43 sentences: 43 more indexing embeddings and about
132 KB of float32 vectors. Each question still needs one query embedding, and the cosine
comparisons per question rise from 15 to 43.

Because the result is no-go, v1 was not replayed and the candidate is not implemented in
Cortex.
