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
import re
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


# Bodies that look ready to a lax reader but are not an exact, strict-JSON
# "ready": a shell $(...) strips the decoded value's trailing newline, and
# Python's default json parser accepts NaN/Infinity and keeps the last of
# duplicate keys.
MALFORMED = {
    "newline": b'{"status": "ready\\n"}',
    "newlines": b'{"status": "ready\\n\\n"}',
    "nan": b'{"status": "ready", "progress": NaN}',
    "infinity": b'{"status": "ready", "progress": Infinity}',
    "neginfinity": b'{"status": "ready", "progress": -Infinity}',
    "dupkey": b'{"status": "processing", "status": "ready"}',
    "garbage": b"ready, but not JSON",
}


def mode_body(mode):
    """A mode token like trunc200, ok500, ok200pending, ok200nan:
    transfer (ok, or trunc = Content-Length 12 bytes longer than sent),
    HTTP status, and the body: a MALFORMED entry by name, else
    {"status": <suffix or "ready">}."""
    m = re.fullmatch(r"(ok|trunc)(\d{3})(\w*)", mode)
    body = MALFORMED.get(m[3]) or json.dumps({"status": m[3] or "ready"}).encode()
    return m[1] == "trunc", int(m[2]), body


class Handler(http.server.BaseHTTPRequestHandler):
    """/v1/trunc/<status>, /v1/ok/<status>: a decline body, truncated or not.
    /v1/collections/<modes>/documents/<key>  (GET, readiness) and
    /v1/probe/<modes>/<key>/collections      (GET, startup probe): the
    n-th request for that path answers with the n-th comma-separated mode
    (the last one repeats). A minimal Cortex for run-eval.sh's setup:
    GET /v1/collections -> 401 (no token), POST /v1/collections -> 201
    {"id": READY_SCRIPT}, POST .../documents -> 201. POST /v1/embeddings:
    deterministic 768-dimensional vectors (a stand-in Ollama)."""
    hits = {}
    ready_script = "ok200"

    def send(self, status, body, trunc=False):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body) + (12 if trunc else 0)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True

    def respond(self):
        req = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        parts = self.path.split("/")
        if parts[2] in ("trunc", "ok"):
            return self.send(int(parts[3]), DECLINE, parts[2] == "trunc")
        if parts[2] == "embeddings":
            text = json.loads(req)["input"][0].encode()
            vec = [b / 255 - 0.5 for b in hashlib.sha512(text).digest() * 12]
            return self.send(200, json.dumps({"data": [{"embedding": vec}]}).encode())
        if self.command == "GET" and (parts[2] == "probe" or "documents" in parts):
            modes = parts[3].split(",")
            n = Handler.hits[self.path] = Handler.hits.get(self.path, 0) + 1
            trunc, status, body = mode_body(modes[min(n, len(modes)) - 1])
            return self.send(status, body, trunc)
        if parts[2:] == ["collections"]:
            if self.command == "GET":
                return self.send(401, b'{"error":"unauthenticated"}')
            return self.send(201, json.dumps({"id": Handler.ready_script}).encode())
        if self.command == "POST" and parts[-1] == "documents":
            return self.send(201, json.dumps({"id": f"doc{len(Handler.hits)}-{threading.get_ident()}"}).encode())
        self.send(404, b"{}")

    do_GET = do_POST = do_DELETE = respond

    def log_message(self, *a):
        pass


def setUpModule():
    global SRV, PORT
    SRV = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=SRV.serve_forever, daemon=True).start()
    PORT = SRV.server_address[1]


def tearDownModule():
    SRV.shutdown()


class Layout(unittest.TestCase):
    """A disposable helix-labs + stub helix-foundry, a loopback server,
    and a stub docker that fails every call with exit 42."""

    def setUp(self):
        self.port = PORT
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

    def sh(self, script, *args, env=None):
        """Run a shell snippet as a script inside workflow-2/ (lib.sh
        derives its sibling paths from $0), with lib.sh sourced."""
        f = self.labs / "workflow-2" / "_t.sh"
        f.write_text('set -eu\n. "$(dirname "$0")/lib.sh"\n' + script)
        return subprocess.run(["sh", str(f), *args], capture_output=True, text=True, env=dict(self.env, **(env or {})))

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


class Readiness(Layout):
    """wf2_wait_document_status (Workflow 2, version-change, run-eval
    ingestion) and wf2_wait_api (startup, version-change cutover)."""

    def wait_doc(self, modes, key, deadline=2):
        r = self.sh(f'wf2_wait_document_status "{modes}" {key} token ready {deadline}')
        return r, Handler.hits.get(f"/v1/collections/{modes}/documents/{key}", 0)

    def test_persistent_failures_never_count_as_ready(self):
        for modes, last in (("trunc200", 'curl exit 18, http 200, document status "ready"'),  # whole ready body, short transfer
                            ("ok500", 'curl exit 0, http 500, document status "ready"'),      # ready-looking error response
                            ("ok200pending", 'curl exit 0, http 200, document status "pending"'),
                            ("ok200garbage", "curl exit 0, http 200, document status none (invalid JSON: Expecting value")):
            with self.subTest(modes):
                r, hits = self.wait_doc(modes, "persist")
                self.assertNotEqual(r.returncode, 0, r.stderr)
                self.assertIn(f"not ready within 2s (last poll: {last}", r.stderr)
                self.assertGreaterEqual(hits, 2)  # retried within the deadline, not one shot

    def test_malformed_ready_bodies_never_count_as_ready(self):
        for name, last in (("newline", '"ready\\n"'), ("newlines", '"ready\\n\\n"'),
                           ("nan", "none (invalid JSON: non-standard constant NaN)"),
                           ("infinity", "none (invalid JSON: non-standard constant Infinity)"),
                           ("neginfinity", "none (invalid JSON: non-standard constant -Infinity)"),
                           ("dupkey", "none (invalid JSON: duplicate key 'status')")):
            with self.subTest(name):
                r, hits = self.wait_doc(f"ok200{name}", "malformed")
                self.assertNotEqual(r.returncode, 0, r.stderr)
                self.assertIn(f"(last poll: curl exit 0, http 200, document status {last})", r.stderr)
                self.assertGreaterEqual(hits, 2)
                # ...and a valid response after it allows progress
                r, hits = self.wait_doc(f"ok200{name},ok200", "then-valid", deadline=10)
                self.assertEqual((r.returncode, hits), (0, 2), r.stderr)

    def test_a_failed_poll_is_retried_and_a_valid_one_accepted(self):
        r, hits = self.wait_doc("trunc200,ok500,ok200", "recover", deadline=10)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(hits, 3)
        r, hits = self.wait_doc("ok200", "control")
        self.assertEqual((r.returncode, hits), (0, 1), r.stderr)

    def test_startup_probe_requires_a_complete_401(self):
        for modes, ok, last in (("trunc401", False, "curl exit 18, http 401"), ("ok200", False, "curl exit 0, http 200"),
                                ("ok503,trunc401,ok401", True, None)):
            with self.subTest(modes):
                api = f"http://127.0.0.1:{self.port}/v1/probe/{modes}/k"
                r = self.sh("wf2_wait_api 5", env={"WF2_API_URL": api})
                self.assertEqual(r.returncode == 0, ok, r.stderr)
                if last:
                    self.assertIn(f"(last: {last})", r.stderr)
                else:
                    self.assertEqual(Handler.hits[f"/v1/probe/{modes}/k/collections"], 3)


FAKE_STACK = """#!/bin/sh
# docker for run-eval.sh's setup only: a healthy postgres, clean
# migrations, role checks, the mock profile's startup log lines.
case " $* " in
  *" ps postgres "*) echo healthy ;;
  *" ps -q "*) echo fake ;;
  *migrate.sh*) echo "already applied 0001" ;;
  *" logs "*) echo 'embedding profile: provider=mock answer_rule=none'
              echo 'worker loop started input_extractor=chunk-text document_prefix=""' ;;
  *" psql "*) echo 'helix_cortex_owner|5|0|f|f|f' ;;
esac
exit 0
"""


class RunnerReadiness(Layout):
    def setUp(self):
        super().setUp()
        (self.bin / "docker").write_text(FAKE_STACK)
        compose = self.root / "helix-foundry" / "compose"
        (compose / ".env").write_text(f"HELIX_CORTEX_API_PORT={self.port}\nHELIX_ADMIN_PASSWORD=unused\n")
        (compose / "auth-fixture.json").write_text(json.dumps({f"t-{p}": {"principal": p} for p in ("alice", "bob", "carol")}))
        (self.root / "helix-cortex").mkdir()
        (self.root / "helix-cortex" / "go.mod").write_text("module x\n")

    def run_eval(self, script):
        Handler.ready_script = script  # every document's readiness polls follow this script
        try:
            return subprocess.run(["sh", str(self.labs / "eval" / "run-eval.sh"), "mock"], capture_output=True, text=True,
                                  env=dict(self.env, EVAL_READY_TIMEOUT="2"))
        finally:
            Handler.ready_script = "ok200"

    def test_a_valid_response_after_a_malformed_one_lets_ingestion_proceed(self):
        before = dict(Handler.hits)
        r = self.run_eval("ok200newline,ok200")
        self.assertNotIn("not ready within", r.stderr)
        self.assertIn("eval(mock): running queries", r.stdout)  # past ingestion (the fake stack stops it later)
        out = self.labs / "eval" / "results-mock"
        self.assertEqual(len((out / "doc_ids.tsv").read_text().splitlines()), 17)
        polls = {k: v - before.get(k, 0) for k, v in Handler.hits.items() if "/ok200newline,ok200/documents/" in k}
        polls = {k: v for k, v in polls.items() if v}  # this run's documents only
        self.assertEqual((len(polls), set(polls.values())), (17, {2}))  # each: one malformed poll, then the valid one

    def test_persistent_readiness_failure_leaves_the_attempt_incomplete(self):
        passes = self.valid_passes()
        self.assertEqual(self.score(*(passes[p].dir for p in ("mock", "real", "real-norerank")))[0], 0)
        for script in ("trunc200", "ok500", "ok200newline", "ok200nan", "ok200infinity", "ok200dupkey"):
            with self.subTest(script):
                r = self.run_eval(script)
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn("wf2: provider ok: mock", r.stdout)  # setup passed; ingestion is what failed
                want = {"trunc200": "18, http 200", "ok500": "0, http 500"}.get(script, "0, http 200")
                self.assertIn(f"not ready within 2s (last poll: curl exit {want}, document status", r.stderr)
                meta = json.loads((passes["mock"].dir / "run.json").read_text())
                self.assertIs(meta["completed"], False)
                code, out = self.score(*(passes[p].dir for p in ("mock", "real", "real-norerank")))
                self.assertEqual(code, 2, out)
                self.assertIn("mock: run.json says the run did not complete", out)


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
        hard = self.tmp / "hard.json"
        os.link(self.ref, hard)  # a second name for the same file
        for out in (self.ref, link, hard, self.tmp / "sub" / ".." / "calibration.json"):
            r = self.run_cli(out)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("resolves to the committed reference", r.stderr)
            self.assertEqual(hashlib.sha256(self.ref.read_bytes()).hexdigest(), self.digest)
        self.assertFalse((self.tmp / "sub").exists())

    def test_a_separate_output_with_mismatched_model_metadata_exits_1(self):
        ref = json.loads(self.ref.read_text())
        ref["model"]["dims"] = 96
        self.ref.write_text(json.dumps(ref, indent=2) + "\n")
        digest = hashlib.sha256(self.ref.read_bytes()).hexdigest()
        weights = ref["model"]["weights_digest"]
        (self.tmp / "Dockerfile").write_text(f"ARG OLLAMA_IMAGE_DIGEST={ref['model']['ollama_image_digest']}\n"
                                             f"ARG MODEL_WEIGHTS_DIGEST={weights.replace('sha256:', 'sha256-')}\n")
        (self.tmp / "manifest.json").write_text(json.dumps({"layers": [{"mediaType": "application/vnd.ollama.image.model", "digest": weights}]}))
        out = self.tmp / "out" / "replay.json"
        r = subprocess.run([sys.executable, str(EVAL / "calibrate.py"), "--url", f"http://127.0.0.1:{PORT}",
                            "--manifest", str(self.tmp / "manifest.json"), "--dockerfile", str(self.tmp / "Dockerfile"),
                            "--record", str(self.ref), "--out", str(out)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("MISMATCH: model.dims 768 != recorded 96", r.stderr)
        self.assertEqual(json.loads(out.read_text())["model"]["dims"], 768)  # the replay went to its own file
        self.assertEqual(hashlib.sha256(self.ref.read_bytes()).hexdigest(), digest)

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
