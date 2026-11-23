#!/usr/bin/env python3
"""Evidence-integrity regressions through the real entry points, offline.

Each test runs the actual script (run-eval.sh, workflow-2/lib.sh's request
helpers, calibrate.py, score.py) in a disposable sibling layout -- this
repo's eval/ and workflow-2/ next to a stub helix-foundry/compose -- with
a stub `docker` on PATH and a loopback HTTP server. The server can answer
with a complete JSON body while advertising 12 more bytes than it sends,
so real curl exits 18 after receiving the expected status line and body.
Run: python3 -m unittest -v eval/test_evidence.py
"""
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

EVAL = Path(__file__).resolve().parent
LABS = EVAL.parent
sys.path.insert(0, str(EVAL))
import test_score  # noqa: E402  (synthetic valid passes)
import calibrate  # noqa: E402

DECLINE = b'{"answer":"","answer_method":"extractive","citations":[]}'


class Handler(http.server.BaseHTTPRequestHandler):
    """/v1/trunc/<status>: full JSON body, Content-Length 12 bytes longer,
    then close (curl 18). /v1/ok/<status>: the same, well-formed."""

    def respond(self):
        _, _, mode, status = self.path.split("/")[:4]
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(DECLINE) + (12 if mode == "trunc" else 0)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(DECLINE)
        self.wfile.flush()
        self.close_connection = True

    do_GET = do_POST = do_DELETE = respond

    def log_message(self, *a):
        pass


class Layout(unittest.TestCase):
    """A disposable helix-labs + stub helix-foundry, a loopback server,
    and a stub docker that fails every call with exit 42."""

    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.port = cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.labs = self.root / "helix-labs"
        for d in ("eval", "workflow-2"):
            shutil.copytree(LABS / d, self.labs / d, ignore=shutil.ignore_patterns("results-*", "__pycache__", "report*.json"))
        compose = self.root / "helix-foundry" / "compose"
        compose.mkdir(parents=True)
        (compose / ".env").write_text(f"HELIX_CORTEX_API_PORT={self.port}\n")
        (compose / "auth-fixture.json").write_text("{}\n")
        (compose / "docker-compose.yml").write_text("services: {}\n")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "docker").write_text("#!/bin/sh\necho \"stub docker: $*\" >&2\nexit 42\n")
        (self.bin / "docker").chmod(0o755)
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", TMPDIR=str(self.root))

    def tearDown(self):
        shutil.rmtree(self.root)

    def sh(self, script, *args):
        """Run a shell snippet as a script inside workflow-2/ (lib.sh
        derives its sibling paths from $0), with lib.sh sourced."""
        f = self.labs / "workflow-2" / "_t.sh"
        f.write_text('set -eu\n. "$(dirname "$0")/lib.sh"\n' + script)
        return subprocess.run(["sh", str(f), *args], capture_output=True, text=True, env=self.env)

    def score(self, mock, real, norerank):
        p = subprocess.run([sys.executable, str(self.labs / "eval" / "score.py"), "--mock", str(mock), "--real", str(real),
                            "--real-norerank", str(norerank), "--report", str(self.root / "report.json")],
                           capture_output=True, text=True)
        return p.returncode, p.stdout + p.stderr

    def valid_passes(self):
        """Owned, completed, valid result dirs (synthetic) for all profiles."""
        out = {}
        for prof in ("mock", "real", "real-norerank"):
            P = test_score.Pass(self.labs / "eval", prof).write()
            (P.dir / ".helix-eval-results").write_text("")
            out[prof] = P
        return out


class StaleEvidence(Layout):
    def test_failed_startup_invalidates_the_previous_completed_run(self):
        passes = self.valid_passes()
        dirs = [passes[p].dir for p in ("mock", "real", "real-norerank")]
        self.assertEqual(self.score(*dirs)[0], 0)  # the earlier attempt was a valid pass
        r = subprocess.run(["sh", str(self.labs / "eval" / "run-eval.sh"), "mock"], capture_output=True, text=True, env=self.env)
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertIn("stub docker", r.stderr)  # it failed at stack startup
        meta = json.loads((passes["mock"].dir / "run.json").read_text())
        self.assertIs(meta["completed"], False)
        self.assertEqual(sorted(p.name for p in passes["mock"].dir.iterdir()), [".helix-eval-results", "run.json"])
        code, out = self.score(*dirs)
        self.assertEqual(code, 2, out)
        self.assertIn("mock: run.json says the run did not complete", out)

    def test_symlink_and_unowned_directories_are_refused_untouched(self):
        target = self.root / "user-data"
        target.mkdir()
        (target / "sentinel").write_text("keep")
        link = self.root / "link"
        link.symlink_to(target)
        for out_dir in (link, target):
            r = subprocess.run(["sh", str(self.labs / "eval" / "run-eval.sh"), "mock", str(out_dir)],
                               capture_output=True, text=True, env=self.env)
            self.assertEqual(r.returncode, 64, r.stderr)
        self.assertEqual(sorted(p.name for p in target.iterdir()), ["sentinel"])
        self.assertEqual((target / "sentinel").read_text(), "keep")


class Transport(Layout):
    def test_record_request_keeps_curl_18_and_the_scorer_rejects_it(self):
        out = self.root / "req"
        out.mkdir()
        r = self.sh('record_request "$1" E1__answer POST /trunc/200 token \'{"q":1}\' -; echo "rc=$?"', str(out))
        self.assertNotEqual(r.returncode, 0, "record_request must fail on a short body")
        self.assertEqual((out / "E1__answer.curl").read_text().strip(), "18")
        self.assertEqual((out / "E1__answer.code").read_text().strip(), "200")
        self.assertEqual(json.loads((out / "E1__answer.json").read_text())["citations"], [])  # the body parses
        ok = self.sh('record_request "$1" A1__final POST /ok/200 token \'{"q":1}\' -', str(out))
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual((out / "A1__final.curl").read_text().strip(), "0")
        # Carried into an otherwise valid real pass, the recorded
        # transport status alone makes the evidence invalid.
        passes = self.valid_passes()
        for ext in ("json", "code", "curl"):
            shutil.copy(out / f"E1__answer.{ext}", passes["real"].dir / f"E1__answer.{ext}")
        code, text = self.score(*(passes[p].dir for p in ("mock", "real", "real-norerank")))
        self.assertEqual(code, 2, text)
        self.assertIn("real/E1/answer: transport failure: curl exit 18 (HTTP status '200')", text)

    def test_background_requests_report_failed_transfers(self):
        r = self.sh('''
api POST /trunc/201 token '{"a":1}' "$WF2_TMP/a.json" > "$WF2_TMP/a.code" & PID_A=$!
api POST /ok/201 token '{"a":1}' "$WF2_TMP/b.json" > "$WF2_TMP/b.code" & PID_B=$!
( curl -s --max-time 5 -o "$WF2_TMP/c.json" -w '%{http_code}' -X POST "$API/trunc/403" > "$WF2_TMP/c.code" ) & PID_C=$!
bg_reap "$PID_A" "$WF2_TMP/a.code"; echo "A=$BG_STATUS"
bg_reap "$PID_B" "$WF2_TMP/b.code"; echo "B=$BG_STATUS"
bg_reap "$PID_C" "$WF2_TMP/c.code"; echo "C=$BG_STATUS"
jobs -p | wc -l | tr -d ' ' | sed 's/^/left=/'
''')
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout.splitlines()
        self.assertEqual(out[0], "A=curl-exit-18(http 201)")  # a 201 status line is not success
        self.assertEqual(out[1], "B=201")
        self.assertEqual(out[2], "C=curl-exit-18(http 403)")  # nor is the expected denial
        self.assertEqual(out[3], "left=0")  # every child reaped


class Calibration(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.ref = self.tmp / "calibration.json"
        shutil.copy(EVAL / "calibration.json", self.ref)
        self.digest = hashlib.sha256(self.ref.read_bytes()).hexdigest()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_cli(self, out):
        return subprocess.run([sys.executable, str(EVAL / "calibrate.py"), "--url", "http://127.0.0.1:9", "--manifest", "/nonexistent",
                               "--dockerfile", "/nonexistent", "--record", str(self.ref), "--out", str(out)],
                              capture_output=True, text=True)

    def test_output_resolving_to_the_reference_is_refused_before_writing(self):
        link = self.tmp / "replay.json"
        link.symlink_to(self.ref)
        for out in (self.ref, link, self.tmp / "sub" / ".." / "calibration.json"):
            r = self.run_cli(out)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("resolves to the committed reference", r.stderr)
            self.assertEqual(hashlib.sha256(self.ref.read_bytes()).hexdigest(), self.digest)
        self.assertFalse((self.tmp / "sub").exists())

    def test_every_model_field_is_compared(self):
        old = json.loads(self.ref.read_text())
        best = dict(old["result"])
        self.assertEqual(calibrate.compare_to_record(old, json.loads(self.ref.read_text()), best), [])
        for field, value in (("dims", 96), ("wire_model", "nomic-embed-text:latest"), ("metric", "dot"),
                             ("query_prefix", ""), ("document_prefix", "search_query: "),
                             ("ollama_image_digest", "sha256:" + "0" * 64), ("weights_digest", "sha256:" + "1" * 64)):
            new = json.loads(self.ref.read_text())
            new["model"][field] = value
            problems = calibrate.compare_to_record(old, new, best)
            self.assertEqual(len(problems), 1, problems)
            self.assertIn(f"model.{field}", problems[0])

    def test_cross_architecture_tolerance_and_exact_decisions_are_kept(self):
        old = json.loads(self.ref.read_text())
        best = dict(old["result"])
        new = json.loads(self.ref.read_text())
        for c in new["cases"]:
            if c["value"] is not None:
                c["value"] += 4.7e-4  # the measured arm64 -> x86_64 spread
        self.assertEqual(calibrate.compare_to_record(old, new, best), [])
        n1 = next(c for c in new["cases"] if c["id"] == "N1")
        n1["value"] = 0.7071  # recorded 0.7013: beyond the tolerance AND over the cutoff
        problems = calibrate.compare_to_record(old, new, best)
        self.assertTrue(any("N1 lands on the other side of 0.707" in p for p in problems), problems)


if __name__ == "__main__":
    unittest.main()
