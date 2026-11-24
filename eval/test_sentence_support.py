#!/usr/bin/env python3
"""Sentence-support experiment harness (EXPERIMENT-sentence-support.md), offline.
Run: python3 -m unittest -v eval/test_sentence_support.py"""
import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

EVAL = Path(__file__).resolve().parent
sys.path.insert(0, str(EVAL))
import answer_quality as aq  # noqa: E402
import calibrate  # noqa: E402
import score as base  # noqa: E402
import sentence_support as ss  # noqa: E402
import test_score  # noqa: E402

WEIGHTS = "sha256:" + "a" * 64


class Units(unittest.TestCase):
    def test_the_split_is_deterministic_and_keeps_every_character_but_separators(self):
        text = " Lost badges must be reported. A replacement costs $25!  Badges are worn? Economy; business. "
        self.assertEqual(ss.sentences(text), ["Lost badges must be reported.", "A replacement costs $25!",
                                              "Badges are worn?", "Economy; business."])
        for p in sorted((EVAL / "corpus").glob("*.md")):
            t = p.read_text()
            self.assertEqual(" ".join(ss.sentences(t)), " ".join(t.split()))

    def test_selection_follows_served_order_cutoff_and_limits(self):
        order = [f"c{i}" for i in range(60)]
        sup = {c: 0.8 for c in order}
        sup["c0"] = 0.1
        self.assertEqual(ss.select(order, sup, 0.707), ["c1", "c2", "c3"])
        late = {c: 0.1 for c in order}
        late["c55"] = 0.9  # beyond the 50-candidate limit
        self.assertEqual(ss.select(order, late, 0.707), [])
        self.assertEqual(ss.select(order, {"c5": 0.707}, 0.707), ["c5"])  # >= cutoff

    def test_assembly_is_byte_exact_and_matches_the_scorers_contract(self):
        texts = [("a", " " + "x" * 1500 + " "), ("b", "y" * 400), ("c", "z" * 300)]
        answer, used = ss.assemble(texts)
        self.assertEqual(len(answer.encode()), 2000)
        self.assertEqual([c for c, _ in used], ["a", "b", "c"])  # c fits only cut: 2000 - 1500 - 400 - 2 separators
        self.assertEqual(len(used[2][1]), 2000 - 1500 - 400 - 2 * len(ss.SEP))
        self.assertTrue(base.extractive_of(answer, [t for _, t in texts]))
        self.assertEqual([t for t, _ in aq.split_extracts(answer, [t for _, t in texts])], [t for _, t in used])
        _, used = ss.assemble([("a", "x" * 1996), ("b", "y")])  # no room after the separator: b is dropped, not cited
        self.assertEqual([c for c, _ in used], ["a"])
        self.assertEqual(ss.assemble([]), ("", []))

    def test_grading_and_measures(self):
        chunks = {"p": {"doc": "paid-leave-policy.md"}, "r": {"doc": "remote-work-policy.md"}}
        case = {"expect": "answer", "labels": [{"document": "paid-leave-policy.md", "snippet": "fifteen days"}]}
        g = ss.grade(case, [("r", "remote"), ("p", "accrue fifteen days")], chunks)
        self.assertEqual((g["outcome"], g["off_label"]), ("complete", ["remote-work-policy.md"]))
        g = ss.grade(case, [("p", "accrue fif")], chunks)  # snippet cut off: not returned
        self.assertEqual(g["outcome"], "wrong")
        self.assertEqual(ss.grade(dict(case, expect="decline", labels=[]), [], chunks)["outcome"], "declined")


class Replay(unittest.TestCase):
    """The reproduce-first guards on a synthetic dev-replay results dir:
    nothing about the candidate is reported unless the control reproduces
    every live selection and the development reference."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        P = test_score.Pass(self.tmp, "real-norerank")
        public = [n for n in P.doc if P.coll[n] == "public"]
        P.responses = {}
        for c in ss.dev_cases():
            P.responses[f"{c['id']}__served"] = (200, P.results(public if c["collection"] == "public" else []))
            P.responses[f"{c['id']}__answer"] = (200, dict(test_score.DECLINE, citations=[]))
        P.meta["suite"] = ss.suite_meta()
        self.P = P
        (self.tmp / "Dockerfile").write_text(f"ARG MODEL_WEIGHTS_DIGEST={WEIGHTS.replace('sha256:', 'sha256-')}\n")
        (self.tmp / "manifest.json").write_text(json.dumps({"layers": [{"mediaType": "application/vnd.ollama.image.model", "digest": WEIGHTS}]}))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def replay(self):
        self.P.write()
        args = argparse.Namespace(results_dir=self.P.dir, url="http://unused", manifest=self.tmp / "manifest.json",
                                  dockerfile=self.tmp / "Dockerfile", cache=None)
        fake = lambda url, text: [b / 255 - 0.5 for b in hashlib.sha512(text.encode()).digest() * 12]  # ~orthogonal vectors
        with mock.patch.object(calibrate, "embed", fake):
            return ss.replay(args)

    def test_a_live_selection_the_control_cannot_reproduce_stops_the_replay(self):
        self.P.responses["A1__answer"] = (200, self.P.answer(["paid-leave-policy.md"]))
        r = self.replay()
        self.assertEqual((r["status"], r["exit_code"]), ("control_not_reproduced", 2))
        self.assertTrue(any(e.startswith("A1: control replay [] != live ['paid-leave-policy.md']") for e in r["errors"]), r["errors"])
        self.assertNotIn("candidate", r)

    def test_a_control_that_misses_the_development_reference_stops_the_replay(self):
        r = self.replay()  # every live answer declines, and so does the control: not the accepted cutoff, not 16/23
        self.assertEqual((r["status"], r["exit_code"]), ("control_not_reproduced", 2))
        self.assertTrue(any("re-derived control cutoff" in e or "do not reproduce the reference" in e for e in r["errors"]), r["errors"])
        self.assertNotIn("candidate", r)

    def test_invalid_evidence_and_unpinned_weights_exit_2(self):
        self.P.curl["B1__served"] = 18
        r = self.replay()
        self.assertEqual(r["exit_code"], 2)
        self.assertTrue(any("transport failure: curl exit 18" in e for e in r["errors"]), r["errors"])
        self.P.curl.clear()
        (self.tmp / "manifest.json").write_text(json.dumps({"layers": [{"mediaType": "application/vnd.ollama.image.model", "digest": "sha256:" + "b" * 64}]}))
        r = self.replay()
        self.assertEqual(r["exit_code"], 2)
        self.assertIn("!= pinned", r["errors"][0])

    def test_the_plan_is_the_27_development_cases_only(self):
        cases = [c["id"] for c in ss.dev_cases()]
        self.assertEqual(cases, [c["id"] for c in calibrate.select_cases()])
        self.assertEqual(len(cases), 27)
        frozen = {q["id"] for q in aq.load_fixture()["questions"]}  # the 12 (D1 is a v1 diagnostic AND a dev case)
        self.assertFalse(frozen & set(cases))
        texts = {q["text"] for q in aq.load_fixture()["questions"]}
        self.assertFalse(texts & {c["text"] for c in ss.dev_cases()})


if __name__ == "__main__":
    unittest.main()
