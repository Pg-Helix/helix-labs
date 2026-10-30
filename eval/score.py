#!/usr/bin/env python3
"""Scores helix-labs/eval's run-eval.sh output against queries.jsonl's
labels, per PROTOCOL.md's metric definitions. Reads results-mock/ and
results-real/ (both must exist -- run-eval.sh mock and run-eval.sh real
first), writes a single report.json plus a human-readable summary to
stdout.

Relevance: each corpus document is short enough to produce exactly one
chunk (confirmed: every corpus/*.md and corpus/restricted/*.md file is
well under ingest.MaxChunkChars), so a query's labeled-relevant
documents translate directly to a set of expected document_ids via
doc_ids.tsv -- a returned result is relevant iff its document_id is in
that set.
"""
import json
import math
import sys
from pathlib import Path

EVAL_DIR = Path(__file__).parent


def load_queries():
    out = {}
    with open(EVAL_DIR / "queries.jsonl") as f:
        for line in f:
            line = line.strip()
            if line:
                q = json.loads(line)
                out[q["id"]] = q
    return out


def load_doc_ids(results_dir):
    """name -> document_id, from the doc_ids.tsv run-eval.sh wrote."""
    name_to_id = {}
    path = results_dir / "doc_ids.tsv"
    if not path.exists():
        return name_to_id
    with open(path) as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 2:
                name_to_id[parts[0]] = parts[1]
    return name_to_id


def relevant_doc_ids(query, name_to_id):
    out = set()
    for rel in query.get("relevant", []):
        doc_id = name_to_id.get(rel["document"])
        if doc_id:
            out.add(doc_id)
    return out


def load_stage_results(results_dir, qid, stage):
    code_path = results_dir / f"{qid}__{stage}.code"
    json_path = results_dir / f"{qid}__{stage}.json"
    if not code_path.exists() or not json_path.exists():
        return None, []
    code = code_path.read_text().strip()
    try:
        body = json.loads(json_path.read_text())
    except (json.JSONDecodeError, OSError):
        return code, []
    results = body.get("results") or []
    return code, results


def recall_at_5(results, relevant_ids):
    if not relevant_ids:
        return None
    top5 = [r.get("document_id") for r in results[:5]]
    found = sum(1 for d in relevant_ids if d in top5)
    return found / len(relevant_ids)


def ndcg_at_10(results, relevant_ids):
    if not relevant_ids:
        return None
    dcg = 0.0
    for i, r in enumerate(results[:10]):
        gain = 1.0 if r.get("document_id") in relevant_ids else 0.0
        dcg += gain / math.log2(i + 2)
    ideal_hits = min(len(relevant_ids), 10)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(ideal_hits))
    if idcg == 0:
        return None
    return dcg / idcg


STAGES = ["fts-only", "dense-only", "hybrid-prererank", "final"]


def score_pass(results_dir, queries):
    name_to_id = load_doc_ids(results_dir)
    per_query = {}
    disclosures = []
    for qid, q in queries.items():
        category = q["category"]
        relevant_ids = relevant_doc_ids(q, name_to_id)
        if category in ("no_answer", "unauthorized"):
            code, results = load_stage_results(results_dir, qid, "final")
            per_query[qid] = {"category": category, "final_http": code, "final_results": results}
            if category == "unauthorized":
                # ANY 200 with non-empty results, or any disclosed
                # document/chunk/text from the restricted set, is a
                # hard failure -- checked regardless of HTTP code.
                if code == "200" and results:
                    disclosures.append((qid, "final", results))
            continue
        per_query[qid] = {"category": category, "relevant_ids": sorted(relevant_ids), "stages": {}}
        for stage in STAGES:
            code, results = load_stage_results(results_dir, qid, stage)
            r5 = recall_at_5(results, relevant_ids)
            nd = ndcg_at_10(results, relevant_ids)
            per_query[qid]["stages"][stage] = {
                "http": code,
                "recall_at_5": r5,
                "ndcg_at_10": nd,
                "returned_doc_ids": [r.get("document_id") for r in results],
            }
    return per_query, disclosures


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def aggregate(per_query, queries, split, category_filter=None):
    out = {}
    for stage in STAGES:
        vals_r5, vals_ndcg = [], []
        for qid, pq in per_query.items():
            q = queries[qid]
            if q["split"] != split:
                continue
            if q["category"] in ("no_answer", "unauthorized"):
                continue
            if category_filter and q["category"] != category_filter:
                continue
            s = pq["stages"].get(stage, {})
            vals_r5.append(s.get("recall_at_5"))
            vals_ndcg.append(s.get("ndcg_at_10"))
        out[stage] = {"recall_at_5": mean(vals_r5), "ndcg_at_10": mean(vals_ndcg)}
    return out


def main():
    queries = load_queries()
    mock_dir = EVAL_DIR / "results-mock"
    real_dir = EVAL_DIR / "results-real"
    if not mock_dir.exists() or not real_dir.exists():
        print("score.py: run `sh run-eval.sh mock` and `sh run-eval.sh real` first", file=sys.stderr)
        sys.exit(1)

    mock_pq, mock_disclosures = score_pass(mock_dir, queries)
    real_pq, real_disclosures = score_pass(real_dir, queries)

    report = {
        "mock": {
            "dev": aggregate(mock_pq, queries, "dev"),
            "held_out": aggregate(mock_pq, queries, "held_out"),
            "dev_paraphrase": aggregate(mock_pq, queries, "dev", "paraphrase"),
            "held_out_paraphrase": aggregate(mock_pq, queries, "held_out", "paraphrase"),
            "held_out_exact_term": aggregate(mock_pq, queries, "held_out", "exact_term"),
        },
        "real": {
            "dev": aggregate(real_pq, queries, "dev"),
            "held_out": aggregate(real_pq, queries, "held_out"),
            "dev_paraphrase": aggregate(real_pq, queries, "dev", "paraphrase"),
            "held_out_paraphrase": aggregate(real_pq, queries, "held_out", "paraphrase"),
            "held_out_exact_term": aggregate(real_pq, queries, "held_out", "exact_term"),
        },
        "unauthorized_disclosures": {"mock": mock_disclosures, "real": real_disclosures},
        "per_query": {"mock": mock_pq, "real": real_pq},
    }
    with open(EVAL_DIR / "report.json", "w") as f:
        json.dump(report, f, indent=2, default=str)

    def fmt(x):
        return f"{x:.3f}" if isinstance(x, float) else "n/a"

    print("=== Held-out comparison table (Recall@5 / nDCG@10) ===")
    print(f"{'stage':<20}{'recall@5':<12}{'ndcg@10':<12}")
    for label, data in [("real", report["real"]["held_out"])]:
        for stage in STAGES:
            r5 = fmt(data[stage]["recall_at_5"])
            nd = fmt(data[stage]["ndcg_at_10"])
            print(f"{stage:<20}{r5:<12}{nd:<12}")
    print()
    print("=== Mock (plumbing baseline), held-out ===")
    for stage in STAGES:
        d = report["mock"]["held_out"][stage]
        print(f"{stage:<20}{fmt(d['recall_at_5']):<12}{fmt(d['ndcg_at_10']):<12}")
    print()
    print("=== Held-out paraphrase: real final vs FTS-only (target: final >= fts + 0.15) ===")
    fts = report["real"]["held_out_paraphrase"]["fts-only"]["recall_at_5"]
    final = report["real"]["held_out_paraphrase"]["final"]["recall_at_5"]
    print(f"fts-only recall@5 = {fmt(fts)}, final recall@5 = {fmt(final)}")
    if fts is not None and final is not None:
        delta = final - fts
        print(f"delta = {delta:.3f} -> {'PASS' if delta >= 0.15 else 'MISSED TARGET'}")
    print()
    print("=== Held-out exact-term: real final vs FTS-only (target: no more than -0.10 regression) ===")
    fts_e = report["real"]["held_out_exact_term"]["fts-only"]["recall_at_5"]
    final_e = report["real"]["held_out_exact_term"]["final"]["recall_at_5"]
    print(f"fts-only recall@5 = {fmt(fts_e)}, final recall@5 = {fmt(final_e)}")
    if fts_e is not None and final_e is not None:
        delta_e = final_e - fts_e
        print(f"delta = {delta_e:.3f} -> {'PASS' if delta_e >= -0.10 else 'MISSED TARGET'}")
    print()
    print("=== Unauthorized disclosures (target: 0, always) ===")
    print(f"mock: {len(mock_disclosures)}, real: {len(real_disclosures)}")
    if mock_disclosures or real_disclosures:
        print("FAIL: at least one unauthorized disclosure occurred")
        for qid, stage, results in mock_disclosures + real_disclosures:
            print(f"  LEAK: {qid} stage={stage}: {results}")
    else:
        print("PASS: zero disclosures in both passes")
    print()
    print("=== no_answer correctness ===")
    for label, pq in [("mock", mock_pq), ("real", real_pq)]:
        na_ok = 0
        na_total = 0
        for qid, q in queries.items():
            if q["category"] != "no_answer":
                continue
            na_total += 1
            final_results = pq[qid]["final_results"]
            if not final_results:
                na_ok += 1
        print(f"{label}: {na_ok}/{na_total} no_answer queries returned no result")
    print()
    print("Full report written to report.json")

    # Decision D (reranker fate), evaluated on dev only.
    print()
    print("=== Decision D: reranker fate (dev paraphrase, real) ===")
    pre = report["real"]["dev_paraphrase"]["hybrid-prererank"]["recall_at_5"]
    post = report["real"]["dev_paraphrase"]["final"]["recall_at_5"]
    print(f"pre-rerank recall@5 = {fmt(pre)}, post-rerank (final) recall@5 = {fmt(post)}")
    if pre is not None and post is not None:
        if post >= pre:
            print("DECISION: keep the reranker as-is (final >= pre-rerank on dev)")
        else:
            print("DECISION: reranker measurably hurts paraphrase recall on dev -- should be made optional")


if __name__ == "__main__":
    main()
