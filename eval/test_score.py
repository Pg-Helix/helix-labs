#!/usr/bin/env python3
"""Offline regressions for score.py, exercised through its real CLI.

Fixtures are synthesized from the hash-pinned corpus/queries (no Docker,
no network): a valid fixture is an ideal-ranking run whose served final
ranking beats FTS on paraphrase, plus targeted mutations of it.
Run: python3 -m unittest -v eval/test_score.py
"""
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

EVAL = Path(__file__).resolve().parent
SCORE = EVAL / "score.py"
STAGES = ["fts-only", "dense-only", "hybrid-prererank", "final"]
QUERIES = [json.loads(l) for l in (EVAL / "queries.jsonl").read_text().splitlines() if l.strip()]
Q = {q["id"]: q for q in QUERIES}
CORPUS = sorted((EVAL / "corpus").rglob("*.md"))
REVS = {r: {"commit": "0" * 40, "dirty": False} for r in ("helix-labs", "helix-foundry", "helix-kernel", "helix-cortex")}
REAL = {"provider": "openai-compat", "wire_model": "nomic-embed-text:v1.5", "model_id": "nomic-embed-text",
        "model_revision": "v1.5", "dims": 768}
DIGEST = "sha256:970aa74c0a90ef7482477cf803618e776e173c007bf957f635f1015bfcfef0e6"
FORBIDDEN = {"code": "forbidden", "message": "the authenticated principal does not hold the required grant", "trace_id": "t"}


def sha(b):
    return hashlib.sha256(b).hexdigest()


class Pass:
    """One synthetic results directory; mutate .responses then .write()."""

    def __init__(self, root, profile):
        self.dir = Path(root) / f"results-{profile}"
        self.profile = profile
        self.doc = {p.name: str(uuid.uuid5(uuid.NAMESPACE_URL, profile + p.name)) for p in CORPUS}
        self.coll = {p.name: "restricted" if p.parent.name == "restricted" else "public" for p in CORPUS}
        self.chunk = {p.name: f"{self.doc[p.name]}|{sha(p.read_bytes())}|chunker-v1|0" for p in CORPUS}
        self.text = {p.name: p.read_text() for p in CORPUS}
        public = [n for n in self.doc if self.coll[n] == "public"]
        files = {}
        for q in QUERIES:
            rel = [i["document"] for i in q["relevant"]]
            ideal = list(dict.fromkeys(rel)) + [n for n in public if n not in rel]
            if q["category"] == "unauthorized":
                for st in STAGES + ["answer"]:
                    files[f"{q['id']}__{st}"] = (403, FORBIDDEN)
                for st in STAGES:
                    files[f"{q['id']}__control-{st}"] = (200, self.results(rel))
                continue
            for st in STAGES:
                docs = ideal[:10]
                if st == "fts-only" and q["category"] == "paraphrase":
                    docs = []  # a legitimate empty FTS result
                files[f"{q['id']}__{st}"] = (200, self.results(docs))
            if q["category"] == "no_answer":
                files[f"{q['id']}__answer"] = (200, {"answer": "", "answer_method": "extractive", "citations": None})
        self.responses = files
        self.meta = {
            "schema": 1, "profile": profile, "completed": True,
            "inputs": {"queries_sha256": sha((EVAL / "queries.jsonl").read_bytes()),
                       "corpus_sha256": sha(b"".join(p.read_bytes() for p in CORPUS))},
            "revisions": json.loads(json.dumps(REVS)),
            "provider": self.provider(),
        }

    def provider(self):
        embed = {"provider": "mock", "wire_model": None, "model_id": "local-mock-embed", "model_revision": "chunk-v1", "dims": 8}
        if self.profile != "mock":
            embed = dict(REAL)
        return {"profile": self.profile.split("-")[0], "query_embedding": embed, "chunk_worker": dict(embed),
                "reranker": "disabled" if self.profile == "real-norerank" else "enabled",
                "model_artifact": {"weights_digest": DIGEST if self.profile != "mock" else None,
                                   "pinned_digest": DIGEST if self.profile != "mock" else None}}

    def results(self, docs):
        return {"results": [{"chunk_id": self.chunk[n], "document_id": self.doc[n], "score": 1.0 / (i + 1),
                             "snippet": self.text[n][:200]} for i, n in enumerate(docs)] or None}

    def write(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "run.json").write_text(json.dumps(self.meta))
        (self.dir / "doc_ids.tsv").write_text("".join(f"{n}\t{self.doc[n]}\t{self.coll[n]}\n" for n in self.doc))
        (self.dir / "chunks.jsonl").write_text("".join(
            json.dumps({"chunk_id": self.chunk[n], "document_id": self.doc[n], "text": self.text[n]}) + "\n" for n in self.doc))
        for name, (code, body) in self.responses.items():
            (self.dir / f"{name}.code").write_text(f"{code}\n")
            (self.dir / f"{name}.json").write_text(body if isinstance(body, str) else json.dumps(body))
        return self


class ScoreCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mock, self.real = Pass(self.tmp, "mock"), Pass(self.tmp, "real")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_cli(self, *extra, write=True):
        if write:
            self.mock.write(); self.real.write()
        report = self.tmp / "out" / "report.json"
        p = subprocess.run([sys.executable, str(SCORE), "--mock", str(self.mock.dir), "--real", str(self.real.dir),
                            "--real-norerank", str(self.tmp / "results-real-norerank"), "--report", str(report), *extra],
                           capture_output=True, text=True)
        self.out = p.stdout + p.stderr
        self.report = json.loads(report.read_text())
        return p.returncode

    def assertFails(self, code, *needles):
        self.assertEqual(self.run_cli() if code is None else code, code if code is not None else 2, self.out)
        for n in needles:
            self.assertIn(n, self.out)

    def test_valid_fixture_passes_with_hand_calculated_metrics(self):
        # A1 (1 relevant passage) at rank 2: Recall@5 = 1, nDCG@10 = (1/log2 3)/1 = 0.630930
        a1 = self.real.responses["A1__final"][1]["results"]
        a1[0], a1[1] = a1[1], a1[0]
        # D1 (3 relevant passages) at ranks 1, 6, 10: Recall@5 = 1/3,
        # nDCG@10 = (1 + 1/log2 7 + 1/log2 11) / (1 + 1/log2 3 + 1/log2 4) = 1.645272 / 2.130930 = 0.772091
        d1 = [r["document_id"] for r in self.real.responses["D1__final"][1]["results"]]
        names = {v: k for k, v in self.real.doc.items()}
        order = [names[d] for d in d1]
        rel, rest = order[:3], order[3:]
        self.real.responses["D1__final"] = (200, self.real.results([rel[0]] + rest[:4] + [rel[1]] + rest[4:7] + [rel[2]]))
        self.assertEqual(self.run_cli(), 0, self.out)
        pq = self.report["passes"]["real"]["per_query"]
        self.assertEqual(pq["A1"]["stages"]["final"]["recall_at_5"], 1.0)
        self.assertAlmostEqual(pq["A1"]["stages"]["final"]["ndcg_at_10"], 0.630930, places=6)
        self.assertAlmostEqual(pq["D1"]["stages"]["final"]["recall_at_5"], 1 / 3, places=9)
        self.assertAlmostEqual(pq["D1"]["stages"]["final"]["ndcg_at_10"], 0.772091, places=6)
        self.assertEqual(self.report["status"], "pass")
        g = {x["name"]: x for x in self.report["gates"]}
        para = next(v for k, v in g.items() if "paraphrase" in k)
        self.assertEqual((para["n"], para["fts_recall_at_5"], para["served_final_recall_at_5"]), (4, 0.0, 1.0))
        self.assertEqual(self.report["decision_d"]["served_profile"], "real")
        self.assertIn("PROTOCOL.md states 5 held-out paraphrase", self.out)
        # C3's committed label does not occur in its document: reported, excluded, not required.
        self.assertIn("label defect", self.out)
        self.assertEqual(pq["C3"]["undefined"], "label unresolvable")
        self.assertEqual(self.report["passes"]["real"]["metrics"]["dev"]["distractor"]["final"]["n_scored"], 2)

    def test_empty_directories_fail(self):
        self.mock.dir.mkdir(); self.real.dir.mkdir()
        self.assertEqual(self.run_cli(write=False), 2, self.out)
        self.assertIn("missing run metadata", self.out)
        self.assertIn("missing document mapping", self.out)
        self.assertIn("unexecuted request", self.out)
        self.assertNotIn("PASS", self.out)
        self.assertEqual(self.report["status"], "invalid_evidence")

    def test_missing_results_directory_fails(self):
        self.mock.write()
        self.assertEqual(self.run_cli(write=False), 2, self.out)
        self.assertIn("real: results directory", self.out)

    def test_missing_document_mapping_fails(self):
        self.real.write(); (self.real.dir / "doc_ids.tsv").unlink(); self.mock.write()
        self.assertEqual(self.run_cli(write=False), 2, self.out)
        self.assertIn("real: missing document mapping", self.out)

    def test_missing_stage_fails_naming_provider_query_stage(self):
        del self.real.responses["B2__dense-only"]
        self.assertFails(None, "real/B2__dense-only: unexecuted request")

    def test_malformed_response_fails(self):
        self.real.responses["A3__final"] = (200, "{not json")
        self.assertFails(None, "real/A3/final: malformed JSON")

    def test_unexpected_status_fails(self):
        self.mock.responses["A1__final"] = (500, {"code": "internal", "message": "x", "trace_id": "t"})
        self.assertFails(None, "mock/A1/final: unexpected HTTP status 500")

    def test_transport_failure_fails(self):
        self.real.responses["E2__answer"] = (0, "")
        self.assertFails(None, "real/E2/answer: no HTTP response")

    def test_schema_violation_fails(self):
        self.real.responses["C1__final"] = (200, {"hits": []})
        self.assertFails(None, "real/C1/final: schema")

    def test_label_unresolvable_in_required_query_fails(self):
        q = next(q for q in QUERIES if q["split"] == "held_out" and q["category"] == "paraphrase")
        doc = q["relevant"][0]["document"]
        self.real.text[doc] = "rewritten without the labeled snippet"
        self.assertFails(None, f"real/{q['id']}: label {doc}/", "feeds a required metric")

    def test_decision_d_requires_norerank_pass(self):
        # Dev paraphrase final worse than pre-rerank -> protocol selects
        # the no-rerank configuration; without that pass nothing is served.
        for q in QUERIES:
            if q["split"] == "dev" and q["category"] == "paraphrase":
                self.real.responses[f"{q['id']}__final"] = (200, {"results": None})
        self.assertFails(None, "no real-norerank pass was supplied")
        Pass(self.tmp, "real-norerank").write()
        self.assertEqual(self.run_cli(), 0, self.out)
        self.assertEqual(self.report["decision_d"]["served_profile"], "real-norerank")

    def test_duplicate_and_unknown_inputs_fail(self):
        self.real.write()
        with open(self.real.dir / "doc_ids.tsv", "a") as f:
            f.write(f"paid-leave-policy.md\t{self.real.doc['paid-leave-policy.md']}\tpublic\n")
        (self.real.dir / "Z9__final.json").write_text("{}")
        self.mock.write()
        self.assertEqual(self.run_cli(write=False), 2, self.out)
        self.assertIn("duplicate document paid-leave-policy.md", self.out)
        self.assertIn("unknown input file Z9__final.json", self.out)

    def test_mismatched_provenance_fails(self):
        self.real.meta["revisions"]["helix-cortex"]["commit"] = "1" * 40
        self.assertFails(None, "provenance: passes ran against different revisions")

    def test_provider_mismatch_fails(self):
        self.real.meta["provider"]["chunk_worker"]["model_id"] = "local-mock-embed"
        self.assertFails(None, "real: provider mismatch: chunk_worker.model_id")

    def test_incomplete_run_fails(self):
        self.mock.meta["completed"] = False
        self.assertFails(None, "mock: run.json says the run did not complete")

    def test_altered_hashes_fail(self):
        self.real.meta["inputs"]["queries_sha256"] = "0" * 64
        self.assertFails(None, "real: run.json input hashes")
        inputs = self.tmp / "inputs"
        shutil.copytree(EVAL / "corpus", inputs / "corpus")
        shutil.copy(EVAL / "queries.jsonl", inputs)
        shutil.copy(EVAL / "HASHES.txt", inputs)
        with open(inputs / "corpus" / "parking-policy.md", "a") as f:
            f.write("tampered\n")
        self.assertEqual(self.run_cli("--inputs", str(inputs)), 2, self.out)
        self.assertIn("corpus/parking-policy.md sha256", self.out)

    def test_duplicate_results_cannot_inflate(self):
        a1 = self.real.responses["A1__final"][1]["results"]
        self.real.responses["A1__final"] = (200, {"results": [a1[0]] * 4 + a1[1:]})
        d1 = self.real.responses["D1__final"][1]["results"]
        self.real.responses["D1__final"] = (200, {"results": [d1[0]] * 6 + d1[3:]})
        self.run_cli()
        pq = self.report["passes"]["real"]["per_query"]
        self.assertEqual(pq["A1"]["stages"]["final"]["ndcg_at_10"], 1.0)
        self.assertEqual(pq["A1"]["stages"]["final"]["duplicates"], 3)
        self.assertAlmostEqual(pq["D1"]["stages"]["final"]["recall_at_5"], 1 / 3, places=9)
        self.assertLess(pq["D1"]["stages"]["final"]["ndcg_at_10"], 1.0)

    def test_missed_threshold_fails_and_names_it(self):
        for q in QUERIES:
            if q["split"] == "held_out" and q["category"] == "paraphrase":
                self.real.responses[f"{q['id']}__final"] = (200, {"results": None})
        self.assertEqual(self.run_cli(), 1, self.out)
        self.assertIn("FAIL: held-out paraphrase gain", self.out)
        self.assertEqual(self.report["status"], "quality_gate_failed")

    def test_exact_term_regression_fails(self):
        for q in QUERIES:
            if q["split"] == "held_out" and q["category"] == "exact_term":
                self.real.responses[f"{q['id']}__final"] = (200, {"results": None})
        self.assertEqual(self.run_cli(), 1, self.out)
        self.assertIn("FAIL: held-out exact_term regression", self.out)

    def test_denial_recognized_and_leak_fails_regardless_of_status(self):
        self.assertEqual(self.run_cli(), 0, self.out)
        u = self.report["passes"]["real"]["unauthorized"]
        self.assertEqual((u["requests"], u["denials_ok"], u["disclosures"]), (15, 15, []))
        leak = dict(FORBIDDEN, message="denied: platform and infrastructure teams")
        self.real.responses["F2__dense-only"] = (403, leak)
        self.assertEqual(self.run_cli(), 1, self.out)
        self.assertIn("FAIL: real: zero unauthorized disclosure", self.out)
        d = self.report["passes"]["real"]["unauthorized"]["disclosures"]
        self.assertEqual([(x["query"], x["stage"], x["http"]) for x in d], [("F2", "dense-only", "403")])

    def test_wrong_denial_status_fails(self):
        self.mock.responses["F1__hybrid-prererank"] = (200, {"results": None})
        self.assertEqual(self.run_cli(), 1, self.out)
        self.assertIn("FAIL: mock: documented denial", self.out)

    def test_no_answer_asserting_labeled_content_fails(self):
        for qid in ("E1", "E2"):
            self.real.responses[f"{qid}__answer"] = (200, {
                "answer": "x", "answer_method": "extractive",
                "citations": [{"chunk_id": self.real.chunk["paid-leave-policy.md"], "source_id": "s", "source_digest": "d"}]})
        self.assertEqual(self.run_cli(), 1, self.out)
        self.assertIn("FAIL: no_answer correctness", self.out)
        na = self.report["passes"]["real"]["no_answer"]
        self.assertEqual((na["correct"], na["n"]), (2, 4))
        self.assertIn("A1", na["queries"]["E1"]["top_citation_labeled_for"])


class Plan(unittest.TestCase):
    def test_plan_matches_required_files_without_empty_fields(self):
        out = subprocess.run([sys.executable, str(SCORE), "plan", "PUB", "RES"], capture_output=True, text=True, check=True).stdout
        rows = [l.split("\t") for l in out.splitlines()]
        self.assertEqual(len(rows), len(Pass(tempfile.gettempdir(), "mock").responses))
        for r in rows:
            self.assertEqual(len(r), 5, r)
            self.assertTrue(all(r), r)  # IFS=<tab> read collapses empty fields
            body = json.loads(r[4])
            self.assertEqual(body["collection_id"], "RES" if Q[r[0].split("__")[0]]["collection"] == "restricted" else "PUB")


if __name__ == "__main__":
    unittest.main()
