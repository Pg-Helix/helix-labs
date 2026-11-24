#!/usr/bin/env python3
"""Answer-quality benchmark (ANSWER-QUALITY.md), offline.
Run: python3 -m unittest -v eval/test_answer_quality.py"""
import copy
import hashlib
import sys
import unittest
from pathlib import Path

EVAL = Path(__file__).resolve().parent
sys.path.insert(0, str(EVAL))
import answer_quality as aq  # noqa: E402


class FrozenFixture(unittest.TestCase):
    def setUp(self):
        self.fx = aq.load_fixture()

    def test_the_frozen_fixture_is_pinned_and_its_labels_resolve_in_the_corpus(self):
        self.assertEqual(aq.check_fixture(self.fx), [])
        pinned = [l.split()[0] for l in (EVAL / "HASHES.txt").read_text().splitlines() if l.endswith("  " + aq.FIXTURE)]
        self.assertEqual(pinned, [hashlib.sha256((EVAL / aq.FIXTURE).read_bytes()).hexdigest()])
        self.assertEqual(sum(len(q["facts"]) for q in self.fx["questions"]), 15)

    def test_the_check_rejects_bad_labels(self):
        for mutate, want in (
                (lambda f: f["questions"][0]["facts"][0].update(quote="taken in two blocks"), "occurs 0 times"),
                (lambda f: f["questions"][0].update(text="How many days of paid leave do employees accrue per year?"), "repeats existing question"),
                (lambda f: f["questions"][4]["facts"][0].update(document="parking-policy.md"), "occurs 0 times in parking-policy.md"),
                (lambda f: f["questions"].pop(), "kinds"),
                (lambda f: f["diagnostics"][0].update(text="changed"), "D1: diagnostic does not match")):
            fx = copy.deepcopy(self.fx)
            mutate(fx)
            errs = aq.check_fixture(fx)
            self.assertTrue(any(want in e for e in errs), errs)


if __name__ == "__main__":
    unittest.main()
