#!/usr/bin/env python3
"""Answer-quality benchmark (ANSWER-QUALITY.md), offline.
Run: python3 -m unittest -v eval/test_answer_quality.py"""
import copy
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

EVAL = Path(__file__).resolve().parent
sys.path.insert(0, str(EVAL))
import answer_quality as aq  # noqa: E402
import test_score  # noqa: E402  (synthetic results directories)

FX = aq.load_fixture()
ITEMS = {q["id"]: q for q, _ in aq.items(FX)}
DECLINE = {"answer": "", "answer_method": "extractive", "citations": []}


class Bench(test_score.Pass):
    """A synthetic EVAL_SUITE=answer-quality results directory whose
    default answers are ideal: each answerable item cites exactly the
    documents of its required facts, each unsupported item declines."""

    def __init__(self, root):
        super().__init__(root, "real-norerank")
        self.dir = Path(root) / "results-answer-quality-real-norerank"
        self.responses = {f"{i}__answer": (200, self.answer(list(dict.fromkeys(f["document"] for f in q["facts"]))))
                          for i, q in ITEMS.items()}
        self.meta["suite"] = {"name": "answer-quality", "fixture": aq.FIXTURE, "fixture_commit": aq.FREEZE_COMMIT,
                              "run_commit": self.meta["revisions"]["helix-labs"]["commit"],
                              "fixture_sha256": hashlib.sha256((EVAL / aq.FIXTURE).read_bytes()).hexdigest()}

    def score(self, inputs=EVAL):
        self.write()
        return aq.score_run(self.dir, inputs)


class Provenance(unittest.TestCase):
    """The fixture's identity: bytes against HASHES.txt, the canonical
    freeze commit (a pin), and the run's own commit recorded separately."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_a_shallow_checkout_records_the_canonical_freeze_and_its_own_commit(self):
        clone = self.tmp / "shallow"
        subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{EVAL.parent}", str(clone)], check=True, capture_output=True)
        head = subprocess.run(["git", "-C", str(clone), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(subprocess.run(["git", "-C", str(clone), "rev-parse", "--is-shallow-repository"],
                                        capture_output=True, text=True).stdout.strip(), "true")
        derived = subprocess.run(["git", "-C", str(clone), "log", "-1", "--format=%H", "--", "eval/" + aq.FIXTURE],
                                 capture_output=True, text=True).stdout.strip()
        if head != aq.FREEZE_COMMIT:
            self.assertEqual(derived, head)  # the old derivation: whatever was cloned, not the freeze
        meta = aq.suite_meta(clone / "eval", repo=clone)
        self.assertEqual((meta["fixture_commit"], meta["run_commit"]), (aq.FREEZE_COMMIT, head))
        self.assertEqual(meta["fixture_sha256"], hashlib.sha256((clone / "eval" / aq.FIXTURE).read_bytes()).hexdigest())

    def test_a_plausible_but_incorrect_freeze_revision_is_invalid(self):
        for name, rev in (("ci-merge", "427ae05" + "0" * 33), ("run-head", "6cd0df698f117c38bd38aa79ec7e270a5ce27c62"),
                          ("zeros", "0" * 40), ("short", aq.FREEZE_COMMIT[:7])):
            with self.subTest(name):
                b = Bench(self.tmp / name)
                b.meta["suite"]["fixture_commit"] = rev
                r = b.score()
                self.assertEqual(r["exit_code"], 2)
                self.assertTrue(any("!= the canonical v1 freeze" in e for e in r["errors"]), r["errors"])

    def test_a_run_commit_that_disagrees_with_the_revisions_is_invalid(self):
        b = Bench(self.tmp / "run")
        b.meta["suite"]["run_commit"] = aq.FREEZE_COMMIT
        r = b.score()
        self.assertEqual(r["exit_code"], 2)
        self.assertTrue(any("run_commit" in e for e in r["errors"]), r["errors"])

    def test_a_mismatched_fixture_digest_is_invalid(self):
        inputs = self.tmp / "inputs"
        shutil.copytree(EVAL, inputs, ignore=shutil.ignore_patterns("results-*", "__pycache__", "report*.json"))
        h = (inputs / "HASHES.txt").read_text()
        pin = hashlib.sha256((EVAL / aq.FIXTURE).read_bytes()).hexdigest()
        (inputs / "HASHES.txt").write_text(h.replace(pin, "1" * 64))
        self.assertTrue(any("HASHES.txt pin" in e for e in aq.check_fixture(aq.load_fixture(inputs), inputs)))
        r = Bench(self.tmp / "digest").score(inputs)
        self.assertEqual(r["exit_code"], 2)
        self.assertTrue(any(f"{aq.FIXTURE} sha256 {pin} != HASHES.txt pin {'1' * 64}" in e for e in r["errors"]), r["errors"])
        # ...and changed fixture bytes under the original pin
        (inputs / "HASHES.txt").write_text(h)
        (inputs / aq.FIXTURE).write_bytes((EVAL / aq.FIXTURE).read_bytes() + b"\n")
        self.assertTrue(any("HASHES.txt pin" in e for e in aq.check_fixture(aq.load_fixture(inputs), inputs)))


class Scoring(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.b = Bench(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_ideal_answers_are_complete_and_unsupported_questions_declined(self):
        r = self.b.score()
        self.assertEqual(r["exit_code"], 0, r["errors"])
        h = r["headline"]
        self.assertEqual(h["answerable"], {"n": 8, "complete": 8, "partial": 0, "wrong": 0, "false_decline": 0})
        self.assertEqual(h["unsupported"], {"n": 4, "correct_decline": 4, "wrong": 0})
        self.assertEqual(h["facts"], {"required": 15, "credited": 15, "missing": 0, "missing_in_answers": 0, "missing_in_declines": 0})
        self.assertEqual((h["extracts"]["returned"], h["extracts"]["unrelated"]), (14, 0))
        self.assertEqual(set(r["diagnostics"]["rows"]), {"D1", "H3", "H7"})  # outside the headline
        self.assertEqual(h["questions"], 12)

    def test_a_cited_fact_cut_off_by_truncation_is_not_credited(self):
        b = self.b
        docs = ["code-of-conduct.md", "security-badge-policy.md", "it-equipment-policy.md"]
        full = b.answer(docs)
        cut = full["answer"][:full["answer"].index("reported to IT") + len("reported to")]  # the last extract, cut mid-quote
        b.responses["AQ-M1__answer"] = (200, dict(full, answer=cut))
        row = b.score()["questions"]["AQ-M1"]
        self.assertEqual(row["violations"], [])  # a valid truncated extractive answer
        self.assertEqual((row["outcome"], row["missing_facts"]), ("partial", ["lost-equipment"]))
        last = row["extracts"][-1]
        self.assertEqual((last["document"], last["truncated"], last["credits"], last["unrelated"]),
                         ("it-equipment-policy.md", True, [], True))  # its fact was cut, so it supports nothing

    def test_a_quote_paired_with_the_wrong_source_is_not_credited(self):
        b = self.b
        # (a) the extract is the right passage, but its citation names another document
        ans = b.answer(["wellness-benefit.md"])
        ans["citations"][0]["source_id"] = b.doc["parking-policy.md"]
        b.responses["AQ-S4__answer"] = (200, ans)
        # (b) both passages returned, citations listed in the other order than the text
        ans = b.answer(["travel-booking-policy.md", "expense-reimbursement.md"])
        ans["citations"].reverse()
        b.responses["AQ-M2__answer"] = (200, ans)
        r = b.score()
        s4, m2 = r["questions"]["AQ-S4"], r["questions"]["AQ-M2"]
        self.assertEqual((s4["outcome"], s4["credited_facts"], s4["extracts"][0]["valid_citation"]), ("wrong", {}, False))
        self.assertIn("citation 0 source_id does not match its passage's document", s4["violations"])
        self.assertEqual((m2["outcome"], m2["credited_facts"]), ("wrong", {}))
        self.assertIn("answer text is not the cited passages' own text", m2["violations"])
        self.assertEqual(r["headline"]["facts"]["missing"], 3)

    def test_a_partial_answer_with_an_unrelated_passage(self):
        b = self.b
        b.responses["AQ-M2__answer"] = (200, b.answer(["travel-booking-policy.md", "parking-policy.md"]))
        r = b.score()
        row = r["questions"]["AQ-M2"]
        self.assertEqual((row["outcome"], list(row["credited_facts"]), row["missing_facts"]), ("partial", ["business-class"], ["receipts"]))
        self.assertEqual([(e["document"], e["unrelated"]) for e in row["extracts"]],
                         [("travel-booking-policy.md", False), ("parking-policy.md", True)])
        h = r["headline"]
        self.assertEqual((h["facts"]["missing"], h["facts"]["missing_in_answers"]), (1, 1))
        self.assertEqual((h["extracts"]["unrelated"], h["extracts"]["unrelated_in_answerable"], h["extracts"]["returned"]), (1, 1, 14))
        self.assertEqual(h["answerable"]["partial"], 1)

    def test_declines_and_answered_unsupported_questions(self):
        b = self.b
        b.responses["AQ-M3__answer"] = (200, DECLINE)
        b.responses["AQ-U3__answer"] = (200, b.answer(["remote-work-policy.md"]))
        r = b.score()
        self.assertEqual(r["questions"]["AQ-M3"]["outcome"], "false_decline")
        self.assertEqual((r["questions"]["AQ-U3"]["outcome"], r["questions"]["AQ-U3"]["extracts"][0]["unrelated"]), ("wrong", True))
        h = r["headline"]
        self.assertEqual((h["facts"]["missing_in_declines"], h["extracts"]["unrelated_in_unsupported"]), (2, 1))
        self.assertEqual(h["unsupported"], {"n": 4, "correct_decline": 3, "wrong": 1})

    def test_invalid_evidence_exits_2(self):
        for name, mutate, want in (
                ("transport", lambda b: b.curl.update({"AQ-S1__answer": 18}), "transport failure: curl exit 18"),
                ("fixture", lambda b: b.meta["suite"].update(fixture_sha256="0" * 64), "is not answer-quality"),
                ("incomplete", lambda b: b.meta.update(completed=False), "did not complete"),
                ("plan", lambda b: b.responses.pop("H7__answer"), "unexecuted request"),
                ("profile", lambda b: b.meta["provider"].update(reranker="enabled"), "reranker"),
                ("labels", lambda b: b.text.update({"it-equipment-policy.md": "Laptops are replaced every three years."}),
                 "quote resolves to 0 passages of it-equipment-policy.md")):
            with self.subTest(name):
                b = Bench(self.tmp / name)
                mutate(b)
                r = b.score()
                self.assertEqual(r["exit_code"], 2)
                self.assertTrue(any(want in e for e in r["errors"]), r["errors"])
                self.assertNotIn("headline", r)

    def test_the_cli_writes_a_report_and_the_plan_matches_the_scorer(self):
        self.b.write()
        rep = self.tmp / "report.json"
        p = subprocess.run([sys.executable, str(EVAL / "answer_quality.py"), "score", str(self.b.dir), "--report", str(rep)],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("required facts: credited 15/15", p.stdout)
        self.assertEqual(json.loads(rep.read_text())["status"], "measured")
        plan = subprocess.run([sys.executable, str(EVAL / "answer_quality.py"), "plan", "P", "R", "E"], capture_output=True, text=True)
        names = [l.split("\t")[0] for l in plan.stdout.splitlines()]
        self.assertEqual(names, [f"{i}__answer" for i in ITEMS])
        self.assertEqual(len(names), 15)


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
