#!/usr/bin/env python3
"""Scores run-eval.sh output against queries.jsonl per PROTOCOL.md.

Every number this prints is derived from validated evidence, or the run
fails. Before any aggregation it checks, per pass (mock, real, and
real-norerank when present or required): run metadata (completed,
profile, provider/model, input hashes, revisions), the document mapping,
the full chunk inventory, every required query/stage response file,
its HTTP status and its response schema. A legitimately empty result
list is a measurement; a missing file, malformed JSON, an unexpected
status or an unexecuted request is not, and is reported by
pass/query/stage.

Answers (POST /answers, every authorized question) are checked against
the AnswerResponse contract (helix-atlas ADR-0008): a decline is exactly
HTTP 200, answer_method "extractive", answer "" and citations [];
missing/null citations violate the array contract. A non-empty answer
must be the cited passages' own text and every citation (not only the
first) must name an ingested passage of the asked collection with the
matching source_id and source_digest. Each answer is then classified as
correct cited answer / wrong answer / false decline / correct decline.

Relevance is passage-level (PROTOCOL.md "What relevant means"): a
returned chunk is relevant iff it belongs to a labeled document AND its
full text (from the chunk inventory, not the 200-char search snippet)
contains that label's snippet. Repeated chunks in one result list count
once.

Transport evidence (run.json schema 2): every request also records curl's
own exit status (<name>.curl). A missing, malformed or nonzero status is
invalid evidence for that profile/query/stage even when the HTTP status
and body look right -- e.g. curl 18, a body cut short of its advertised
length. Older artifacts (schema 1) predate this record; score them with
the scorer at the helix-labs commit their run.json records (PROTOCOL.md).

Exit status: 0 = valid evidence and every gate passed; 1 = valid
evidence but at least one acceptance target or security gate failed;
2 = evidence missing/invalid/inconsistent (nothing can be concluded).
A report is written in every case, including scorer crashes.
"""
import argparse
import hashlib
import json
import math
import re
import sys
import traceback
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
STAGES = ["fts-only", "dense-only", "hybrid-prererank", "final"]
RANKED = ["exact_term", "paraphrase", "distractor", "multi_passage"]
# PROTOCOL.md "Acceptance targets" -- fixed, never tuned.
PARAPHRASE_MIN_GAIN = 0.15
EXACT_TERM_MAX_REGRESSION = 0.10
NO_ANSWER_MIN_CORRECT = 3
# Authorized /answers positive controls: each produced its labeled
# passage with a citation to the expected source on the unmodified
# baseline (reviews/2026-11-22 handoff); an evidence rule must keep them.
ANSWER_CONTROLS = ("A1", "A4", "B1", "B2")
EXTRACTIVE_SEPARATOR = " ... "  # helix-cortex retrieval.AssembleExtractiveAnswer
MAX_LISTED = 200
REAL = {"provider": "openai-compat", "wire_model": "nomic-embed-text:v1.5",
        "model_id": "nomic-embed-text", "model_revision": "v1.5", "dims": 768}
PROFILES = {
    "mock": {"embed": {"provider": "mock"}, "reranker": "enabled"},
    "real": {"embed": REAL, "reranker": "enabled"},
    "real-norerank": {"embed": REAL, "reranker": "disabled"},
}
NO_ANSWER_NOTE = (
    "Gate: an explicit decline per helix-atlas ADR-0008 -- HTTP 200, answer_method 'extractive', "
    "answer '' and citations [] -- counts as not asserting an answer; any non-empty or "
    "contract-violating answer asserts one. The historical PROTOCOL.md reading (first citation "
    "not labeled relevant to another query; no citation = abstained) is reported alongside as "
    "historical_first_citation_correct and does not gate: it accepted unsupported prose whose "
    "citations were removed. /search final top-1 is also reported, not gated.")


class Scorer:
    def __init__(self, inputs_dir):
        self.inputs = Path(inputs_dir)
        self.errors, self.warnings, self.gates = [], [], []
        self.label_defects = set()

    def err(self, where, msg):
        self.errors.append(f"{where}: {msg}")

    # ---------- inputs ----------
    def load_inputs(self):
        hashes = {}
        section = None
        for line in (self.inputs / "HASHES.txt").read_text().splitlines():
            if line.endswith(":") and " " not in line:
                section = line[:-1]
                continue
            parts = line.split()
            if len(parts) == 2:
                hashes[section if section else parts[1]] = parts[0]
        corpus = sorted(p for p in (self.inputs / "corpus").rglob("*.md"))
        rel = {str(p.relative_to(self.inputs)): p for p in corpus}
        for name, path in rel.items():
            got = sha256(path.read_bytes())
            if hashes.get(name) != got:
                self.err("inputs", f"{name} sha256 {got} != HASHES.txt {hashes.get(name)}")
        for name in hashes:
            if name.startswith("corpus/") and name not in rel:
                self.err("inputs", f"{name} listed in HASHES.txt but missing")
        combined = sha256(b"".join(rel[k].read_bytes() for k in sorted(rel)))
        qbytes = (self.inputs / "queries.jsonl").read_bytes()
        self.input_hashes = {"queries_sha256": sha256(qbytes), "corpus_sha256": combined}
        if hashes.get("queries.jsonl") != self.input_hashes["queries_sha256"]:
            self.err("inputs", "queries.jsonl sha256 does not match HASHES.txt")
        if hashes.get("combined-corpus") != combined:
            self.err("inputs", "combined corpus sha256 does not match HASHES.txt")
        abytes = (self.inputs / "answerability.jsonl").read_bytes()
        self.input_hashes["answerability_sha256"] = sha256(abytes)
        if hashes.get("answerability.jsonl") != self.input_hashes["answerability_sha256"]:
            self.err("inputs", "answerability.jsonl sha256 does not match HASHES.txt")
        self.docs = {p.name: ("restricted" if p.parent.name == "restricted" else "public") for p in corpus}
        self.load_question_sets()

    def load_question_sets(self):
        """queries.jsonl (v1, ranked + no_answer + unauthorized) and
        answerability.jsonl (authorized /answers only: dev rows chose the
        evidence rule, challenge rows were frozen before it existed)."""
        self.queries, self.answerability = {}, {}
        for path, into in (("queries.jsonl", self.queries), ("answerability.jsonl", self.answerability)):
            for line in (self.inputs / path).read_text().splitlines():
                if line.strip():
                    q = json.loads(line)
                    if q["id"] in self.queries or q["id"] in self.answerability:
                        self.err("inputs", f"duplicate query id {q['id']}")
                    into[q["id"]] = q

    def required_files(self):
        req = {}
        for qid, q in self.queries.items():
            if q["category"] == "unauthorized":
                for st in STAGES + ["answer"]:
                    req[f"{qid}__{st}"] = ("bob", st)
                for st in STAGES:
                    req[f"{qid}__control-{st}"] = ("alice", st)
            else:
                for st in STAGES:
                    req[f"{qid}__{st}"] = ("alice", st)
                req[f"{qid}__answer"] = ("alice", "answer")
        for aid in self.answerability:
            req[f"{aid}__answer"] = ("alice", "answer")
        return req

    # ---------- one pass ----------
    def load_pass(self, label, d):
        d = Path(d)
        P = {"label": label, "dir": str(d), "ok": False}
        if not d.is_dir():
            self.err(label, f"results directory {d} does not exist")
            return P
        before = len(self.errors)
        meta = self.load_json(label, d / "run.json", "run metadata (run.json)")
        if isinstance(meta, dict):
            self.check_meta(label, meta)
            P["meta"] = meta
        elif (d / "run.json").exists() and not any(e.startswith(f"{label}: malformed run metadata") for e in self.errors):
            self.err(label, f"run.json is not a JSON object (got {type(meta).__name__})")
        P["doc_ids"] = self.load_doc_ids(label, d / "doc_ids.tsv")
        P["chunks"] = self.load_chunks(label, d / "chunks.jsonl", P["doc_ids"])
        required = self.required_files()
        present = {}
        for f in d.iterdir():
            if f.name in ("run.json", "doc_ids.tsv", "chunks.jsonl", ".helix-eval-results"):
                continue
            m = re.fullmatch(r"(.+)\.(json|code|curl)", f.name)
            if not m or m.group(1) not in required:
                self.err(label, f"unknown input file {f.name}")
                continue
            present.setdefault(m.group(1), set()).add(m.group(2))
        P["responses"] = {}
        for name in required:
            got = present.get(name, set())
            if got == {"json", "code"}:
                self.err(where(label, name), "missing transport status (.curl): cannot show the transfer completed")
                continue
            if got != {"json", "code", "curl"}:
                self.err(f"{label}/{name}", "unexecuted request (response .json/.code/.curl missing)")
                continue
            curl = (d / f"{name}.curl").read_text().strip()
            if not re.fullmatch(r"\d+", curl):
                self.err(where(label, name), f"malformed transport status {curl!r} in {name}.curl")
                continue
            if curl != "0":
                http = (d / f"{name}.code").read_text().strip()
                self.err(where(label, name), f"transport failure: curl exit {curl} (HTTP status {http!r}); the response is not evidence")
                continue
            P["responses"][name] = self.load_response(label, d, name)
        P["ok"] = len(self.errors) == before
        return P

    def load_json(self, where, path, what):
        if not path.exists():
            self.err(where, f"missing {what}")
            return None
        try:
            return json.loads(path.read_text())
        except (ValueError, UnicodeDecodeError) as e:
            self.err(where, f"malformed {what}: {e}")
            return None

    def check_meta(self, label, meta):
        if meta.get("schema") != 2:
            self.err(label, f"run.json schema {meta.get('schema')!r}: this scorer needs schema 2 (per-request transport status); "
                            "score older artifacts with the scorer at the helix-labs commit their run.json records (PROTOCOL.md)")
        if meta.get("completed") is not True:
            self.err(label, "run.json says the run did not complete")
        if meta.get("profile") != label:
            self.err(label, f"run.json profile {meta.get('profile')!r} != expected {label!r}")
        if meta.get("inputs") != self.input_hashes:
            self.err(label, f"run.json input hashes {meta.get('inputs')} != current inputs {self.input_hashes}")
        prov = meta.get("provider") or {}
        want = PROFILES.get(label)
        if want:
            for side in ("query_embedding", "chunk_worker"):
                got = prov.get(side) or {}
                for k, v in want["embed"].items():
                    if got.get(k) != v:
                        self.err(label, f"provider mismatch: {side}.{k}={got.get(k)!r}, want {v!r}")
            if prov.get("reranker") != want["reranker"]:
                self.err(label, f"provider mismatch: reranker={prov.get('reranker')!r}, want {want['reranker']!r}")
            # The served /answers rule as cortex resolved it at startup
            # (embedconfig.Resolve): calibrated only for the real profile.
            want_rule = "answer_rule=none" if label == "mock" else "answer_rule=cosine>=0.707"
            if want_rule not in (prov.get("resolved_profile") or ""):
                self.err(label, f"resolved embedding profile {prov.get('resolved_profile')!r} lacks {want_rule}")
            # The chunk worker's effective input: chunk text behind the
            # profile's document prefix (what the calibration embedded).
            want_input = {"input_extractor": "chunk-text", "document_prefix": "" if label == "mock" else "search_document: "}
            if prov.get("chunk_worker_input") != want_input:
                self.err(label, f"chunk-worker effective input {prov.get('chunk_worker_input')!r} != {want_input!r}")
            if label != "mock":
                art = prov.get("model_artifact") or {}
                if not art.get("weights_digest") or art.get("weights_digest") != art.get("pinned_digest"):
                    self.err(label, f"model artifact digest {art.get('weights_digest')!r} != pinned {art.get('pinned_digest')!r}")
        revs = meta.get("revisions")
        if not isinstance(revs, dict) or not all(
                isinstance(revs.get(r), dict) and isinstance(revs[r].get("commit"), str)
                and re.fullmatch(r"[0-9a-f]{40}", revs[r]["commit"]) and isinstance(revs[r].get("dirty"), bool)
                for r in ("helix-labs", "helix-foundry", "helix-kernel", "helix-cortex")):
            self.err(label, "run.json lacks valid git revisions (40-hex commit + dirty flag) for helix-labs/foundry/kernel/cortex")
        planned = len(self.required_files())
        if meta.get("requests") != planned:
            self.err(label, f"run.json records {meta.get('requests')!r} executed requests; the request plan has {planned}")

    def load_doc_ids(self, label, path):
        out, seen_ids = {}, set()
        if not path.exists():
            self.err(label, "missing document mapping (doc_ids.tsv)")
            return out
        for n, line in enumerate(path.read_text().splitlines(), 1):
            parts = line.split("\t")
            if len(parts) != 3:
                self.err(label, f"doc_ids.tsv line {n} malformed")
                continue
            name, did, coll = parts
            if name not in self.docs:
                self.err(label, f"doc_ids.tsv names unknown document {name}")
            elif name in out:
                self.err(label, f"doc_ids.tsv duplicate document {name}")
            elif did in seen_ids:
                self.err(label, f"doc_ids.tsv duplicate document id {did}")
            elif coll != self.docs[name]:
                self.err(label, f"doc_ids.tsv {name} in {coll}, expected {self.docs[name]}")
            else:
                out[name] = did
                seen_ids.add(did)
        for name in self.docs:
            if name not in out:
                self.err(label, f"doc_ids.tsv has no mapping for {name}")
        return out

    def load_chunks(self, label, path, doc_ids):
        out = {}
        if not path.exists():
            self.err(label, "missing chunk inventory (chunks.jsonl)")
            return out
        by_id = {v: k for k, v in doc_ids.items()}
        for n, line in enumerate(path.read_text().splitlines(), 1):
            try:
                c = json.loads(line)
                cid, did, text = c["chunk_id"], c["document_id"], c["text"]
            except (ValueError, KeyError, TypeError):
                self.err(label, f"chunks.jsonl line {n} malformed")
                continue
            if cid in out:
                self.err(label, f"chunks.jsonl duplicate chunk {cid}")
            elif did not in by_id:
                self.err(label, f"chunks.jsonl chunk {cid} belongs to unmapped document {did}")
            else:
                out[cid] = {"doc": by_id[did], "text": text, "collection": self.docs[by_id[did]]}
        for name in doc_ids:
            if not any(c["doc"] == name for c in out.values()):
                self.err(label, f"chunks.jsonl has no chunk for {name}")
        return out

    def load_response(self, label, d, name):
        where = f"{label}/{name}"
        code = (d / f"{name}.code").read_text().strip()
        raw = (d / f"{name}.json").read_bytes()
        r = {"code": code, "raw": raw.decode("utf-8", "replace"), "body": None}
        if not re.fullmatch(r"[1-5]\d\d", code):
            r["invalid"] = f"no HTTP response (status {code!r}: transport failure or timeout)"
            return r
        try:
            r["body"] = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as e:
            r["invalid"] = f"malformed JSON response: {e}"
        return r

    # ---------- validation helpers ----------
    def search_results(self, P, name, where):
        """Validated, de-duplicated chunk ids of a 200 search response, or None."""
        r = P["responses"].get(name)
        if r is None:
            return None
        if "invalid" in r:
            self.err(where, r["invalid"])
            return None
        if r["code"] != "200":
            self.err(where, f"unexpected HTTP status {r['code']} (want 200): {error_code(r['body'])}")
            return None
        b = r["body"]
        if not isinstance(b, dict) or "results" not in b or not isinstance(b["results"], (list, type(None))):
            self.err(where, "schema: search response lacks a results array")
            return None
        ids, dups = [], 0
        for i, item in enumerate(b["results"] or []):
            if not (isinstance(item, dict) and isinstance(item.get("chunk_id"), str)
                    and isinstance(item.get("document_id"), str) and isinstance(item.get("score"), (int, float))):
                self.err(where, f"schema: result {i} lacks chunk_id/document_id/score")
                return None
            c = P["chunks"].get(item["chunk_id"])
            if c is None:
                self.err(where, f"result {i} references chunk {item['chunk_id']} absent from the inventory")
                return None
            if P["doc_ids"].get(c["doc"]) != item["document_id"]:
                self.err(where, f"result {i} document_id does not match its chunk")
                return None
            if item["chunk_id"] in ids:
                dups += 1
            else:
                ids.append(item["chunk_id"])
        return ids, dups

    def relevant_sets(self, P, where):
        """qid -> set of chunk ids satisfying the passage-level rule."""
        out = {}
        for qid, q in list(self.queries.items()) + list(self.answerability.items()):
            s = set()
            for item in q["relevant"]:
                hits = {cid for cid, c in P["chunks"].items()
                        if c["doc"] == item["document"] and item["snippet"] in c["text"]}
                if not hits:
                    msg = f"label {item['document']}/{item['snippet']!r} matches no ingested chunk"
                    if qid in self.answerability or required(q):
                        self.err(f"{where}/{qid}", msg + " (query feeds a required metric)")
                    else:
                        self.label_defects.add(f"{qid}: {msg}")
                        s = None
                        break
                s |= hits
            out[qid] = s
        return out

    # ---------- scoring ----------
    def score_pass(self, P):
        label = P["label"]
        rel = self.relevant_sets(P, label)
        per_query = {}
        for qid, q in self.queries.items():
            if q["category"] not in RANKED:
                continue
            if not rel[qid]:  # unresolvable label: error (required) or reported defect
                per_query[qid] = {"split": q["split"], "category": q["category"], "undefined": "label unresolvable", "stages": {}}
                # Excluded from metrics, not from evidence validation:
                # its HTTP/JSON responses must still be well-formed.
                for st in STAGES:
                    self.search_results(P, f"{qid}__{st}", f"{label}/{qid}/{st}")
                continue
            per_query[qid] = {"split": q["split"], "category": q["category"], "relevant_total": len(rel[qid]), "stages": {}}
            for st in STAGES:
                got = self.search_results(P, f"{qid}__{st}", f"{label}/{qid}/{st}")
                if got is None:
                    per_query[qid]["stages"][st] = None
                    continue
                ids, dups = got
                r5, nd = recall_at_5(ids, rel[qid]), ndcg_at_10(ids, rel[qid])
                for v, n in ((r5, "recall@5"), (nd, "nDCG@10")):
                    if not 0.0 <= v <= 1.0:
                        raise AssertionError(f"{label}/{qid}/{st} {n}={v} outside [0,1]")
                per_query[qid]["stages"][st] = {
                    "http": 200, "returned": ids[:10], "duplicates": dups,
                    "relevant_in_top5": len(set(ids[:5]) & rel[qid]),
                    "recall_at_5": r5, "ndcg_at_10": nd}
        P["per_query"] = per_query
        P["metrics"] = self.aggregate(per_query)
        P["unauthorized"] = self.score_unauthorized(P)
        P["answers"] = self.score_answers(P, rel)
        P["no_answer"] = self.score_no_answer(P, rel)

    def aggregate(self, per_query):
        out = {}
        for split in ("dev", "held_out"):
            for cat in ["all_ranked"] + RANKED:
                group = [p for p in per_query.values() if p["split"] == split and (cat == "all_ranked" or p["category"] == cat)]
                qs = [p for p in group if "undefined" not in p]
                for st in STAGES:
                    vals = [p["stages"][st] for p in qs]
                    ok = [v for v in vals if v is not None]
                    out.setdefault(split, {}).setdefault(cat, {})[st] = {
                        "n": len(group), "n_scored": len(qs),
                        "recall_at_5": mean([v["recall_at_5"] for v in ok]) if qs and len(ok) == len(qs) else None,
                        "ndcg_at_10": mean([v["ndcg_at_10"] for v in ok]) if qs and len(ok) == len(qs) else None,
                    }
        return out

    def score_unauthorized(self, P):
        label = P["label"]
        restricted = {cid: c for cid, c in P["chunks"].items() if c["collection"] == "restricted"}
        needles = set(restricted)
        needles |= {P["doc_ids"][n] for n, coll in self.docs.items() if coll == "restricted" and n in P["doc_ids"]}
        needles |= {n for n, coll in self.docs.items() if coll == "restricted"}
        for c in restricted.values():
            needles |= {s.strip() for s in re.split(r"(?<=[.!?])\s+", c["text"]) if len(s.strip()) >= 20}
        for q in self.queries.values():
            if q["category"] == "unauthorized":
                needles |= {i["snippet"] for i in q["relevant"]}
        out = {"requests": 0, "denials_ok": 0, "disclosures": [], "bad_denials": [], "control": {}}
        for qid, q in self.queries.items():
            if q["category"] != "unauthorized":
                continue
            for st in STAGES + ["answer"]:
                r = P["responses"].get(f"{qid}__{st}")
                if r is None:
                    continue
                out["requests"] += 1
                if not re.fullmatch(r"[1-5]\d\d", r["code"]):
                    self.err(f"{label}/{qid}/{st}", r["invalid"])
                    continue
                leaked = sorted(n for n in needles if n in r["raw"])
                body = r["body"]
                if r["code"] == "200" and isinstance(body, dict) and (body.get("results") or body.get("citations") or body.get("answer")):
                    leaked.append("non-empty 200 response")
                if leaked:
                    out["disclosures"].append({"query": qid, "stage": st, "http": r["code"], "matched": leaked[:5]})
                if r["code"] == "403" and isinstance(body, dict) and body.get("code") == "forbidden" \
                        and isinstance(body.get("message"), str) and isinstance(body.get("trace_id"), str):
                    out["denials_ok"] += 1
                else:
                    out["bad_denials"].append({"query": qid, "stage": st, "http": r["code"], "code": error_code(body)})
            for st in STAGES:
                got = self.search_results(P, f"{qid}__control-{st}", f"{label}/{qid}/control-{st}")
                if got is not None:
                    ids = got[0]
                    if any(cid not in restricted for cid in ids):
                        self.err(f"{label}/{qid}/control-{st}", "authorized control returned a non-restricted chunk")
                    out["control"].setdefault(st, 0)
                    out["control"][st] += 1 if ids else 0
        if P["responses"] and self.errors_for(label) == 0:
            for st in STAGES:
                if not out["control"].get(st):
                    self.err(f"{label}/control-{st}", "authorized positive control returned no restricted result for any query: fixture/stage not proven")
        return out

    def errors_for(self, label):
        return sum(1 for e in self.errors if e.startswith((label + ":", label + "/")))

    def check_answer(self, P, qid, coll, where):
        """Contract check of one authorized /answers response. None means
        the evidence itself is unusable (error recorded, exit 2); otherwise
        a dict whose 'violations' are product failures."""
        r = P["responses"].get(f"{qid}__answer")
        if r is None:
            return None
        if "invalid" in r:
            self.err(where, r["invalid"]); return None
        if r["code"] != "200":
            self.err(where, f"unexpected HTTP status {r['code']} (want 200): {error_code(r['body'])}"); return None
        b, v = r["body"], []
        if not isinstance(b, dict):
            return {"declined": False, "violations": ["body is not a JSON object"], "cited": [], "answer": "", "first": None}
        ans, cits = b.get("answer"), b.get("citations")
        if b.get("answer_method") != "extractive":
            v.append(f"answer_method {b.get('answer_method')!r} is not 'extractive'")
        if not isinstance(ans, str):
            v.append("answer is not a string"); ans = ""
        if "citations" not in b:
            v.append("citations missing (contract: always an array)")
        elif cits is None:
            v.append("citations is null (contract: always an array)")
        elif not isinstance(cits, list):
            v.append("citations is not an array")
        cits = cits if isinstance(cits, list) else []
        first = cits[0].get("chunk_id") if cits and isinstance(cits[0], dict) else None
        cited = []
        for i, c in enumerate(cits):
            if not (isinstance(c, dict) and all(isinstance(c.get(k), str) and c.get(k) for k in ("chunk_id", "source_id", "source_digest"))):
                v.append(f"citation {i} lacks chunk_id/source_id/source_digest"); continue
            ch = P["chunks"].get(c["chunk_id"])
            if ch is None:
                v.append(f"citation {i} names {c['chunk_id']!r}, not an ingested passage"); continue
            if c["source_id"] != P["doc_ids"].get(ch["doc"]):
                v.append(f"citation {i} source_id does not match its passage's document")
            if c["source_digest"] != c["chunk_id"].split("|")[1]:
                v.append(f"citation {i} source_digest does not match its passage")
            if ch["collection"] != coll:
                v.append(f"citation {i} cites a {ch['collection']} passage for a question on the {coll} collection")
            cited.append(c["chunk_id"])
        if len(set(cited)) != len(cited):
            v.append("duplicate citations")
        if ans == "" and cits:
            v.append("empty answer with citations")
        if ans != "" and not cits:
            v.append("non-empty answer without citations")
        if ans and cited and len(cited) == len(cits) and not extractive_of(ans, [P["chunks"][c]["text"] for c in cited]):
            v.append("answer text is not the cited passages' own text")
        return {"declined": not v and ans == "" and cits == [], "violations": v, "cited": cited, "answer": ans, "first": first}

    def score_answers(self, P, rel):
        """Every authorized /answers response: contract check, then one of
        correct_answer / wrong_answer / false_decline / correct_decline
        against its labels (label-defect rows are validated, not counted)."""
        label, rows = P["label"], {}
        groups = {}
        items = [(qid, q, f"v1 {q['split']}", q["category"] != "no_answer") for qid, q in self.queries.items()
                 if q["category"] != "unauthorized"]
        items += [(aid, a, f"answerability {a['set']}", a["expect"] == "answer") for aid, a in self.answerability.items()]
        for qid, q, group, expect_answer in items:
            chk = self.check_answer(P, qid, q.get("collection", "public"), f"{label}/{qid}/answer")
            if chk is None:
                continue
            if expect_answer and not rel.get(qid):
                outcome = "undefined"  # label defect (C3): validated above, not counted
            elif chk["violations"]:
                outcome = "wrong_answer"
            elif chk["declined"]:
                outcome = "false_decline" if expect_answer else "correct_decline"
            elif not expect_answer:
                outcome = "wrong_answer"
            else:
                hit = set(chk["cited"]) & rel[qid]
                outcome = "correct_answer" if hit and any(i["snippet"] in chk["answer"] for i in q["relevant"]) else "wrong_answer"
            rows[qid] = {"group": group, "expect": "answer" if expect_answer else "decline", "outcome": outcome,
                         "violations": chk["violations"], "answer_chars": len(chk["answer"]),
                         "cited_docs": [P["chunks"][c]["doc"] for c in chk["cited"]], "first_citation": chk["first"]}
            for g in [group] + (["positive controls"] if qid in ANSWER_CONTROLS else []) + \
                     (["no_answer"] if q.get("category") == "no_answer" else []):
                c = groups.setdefault(g, {"n": 0, "correct_answer": 0, "wrong_answer": 0, "false_decline": 0,
                                          "correct_decline": 0, "undefined": 0, "contract_violations": 0})
                c["n"] += 1
                c[outcome] += 1
                c["contract_violations"] += bool(chk["violations"])
        return {"rows": rows, "groups": groups}

    def score_no_answer(self, P, rel):
        """The four no_answer queries: the gate (explicit decline) plus the
        historical first-citation reading and /search top-1, for comparison."""
        label = P["label"]
        out = {"queries": {}, "correct": 0, "historical_first_citation_correct": 0, "n": 0}
        for qid, q in self.queries.items():
            if q["category"] != "no_answer":
                continue
            out["n"] += 1
            others = {}
            for oid, s in rel.items():
                if oid != qid and s and oid in self.queries:
                    for cid in s:
                        others.setdefault(cid, []).append(oid)
            for st in STAGES[:-1]:
                self.search_results(P, f"{qid}__{st}", f"{label}/{qid}/{st}")
            row = P["answers"]["rows"].get(qid)
            if row is None:
                continue
            top = row["first_citation"] if row["first_citation"] in P["chunks"] else None
            search = self.search_results(P, f"{qid}__final", f"{label}/{qid}/final")
            top_search = search[0][0] if search and search[0] else None
            entry = {
                "outcome": row["outcome"], "violations": row["violations"], "answer_chars": row["answer_chars"],
                "citations": len(row["cited_docs"]), "correct": row["outcome"] == "correct_decline",
                "historical_first_citation_correct": not others.get(top),
                "top_citation_doc": P["chunks"][top]["doc"] if top else None,
                "top_citation_labeled_for": others.get(top, []),
                "search_top1_doc": P["chunks"][top_search]["doc"] if top_search else None,
                "search_top1_labeled_for": others.get(top_search, []),
            }
            out["queries"][qid] = entry
            out["correct"] += entry["correct"]
            out["historical_first_citation_correct"] += entry["historical_first_citation_correct"]
        return out

    # ---------- gates ----------
    def gate(self, name, passed, **detail):
        self.gates.append(dict(name=name, passed=bool(passed), **detail))


def required(q):
    """Queries feeding a required metric: the held-out targets and Decision D."""
    return (q["split"] == "held_out" and q["category"] in ("paraphrase", "exact_term")) or \
        (q["split"] == "dev" and q["category"] == "paraphrase")


def where(label, name):
    """profile/query/stage for one planned request file name."""
    qid, _, stage = name.partition("__")
    return f"{label}/{qid}/{stage}"


def extractive_of(answer, texts):
    """answer is exactly the passages' stripped texts joined by the
    extractive separator, the last one possibly cut at the length bound."""
    rest = answer
    for i, t in enumerate(texts):
        t = t.strip()
        if i:
            if not rest.startswith(EXTRACTIVE_SEPARATOR):
                return False
            rest = rest[len(EXTRACTIVE_SEPARATOR):]
        if rest.startswith(t):
            rest = rest[len(t):]
        elif i == len(texts) - 1 and rest and t.startswith(rest):
            rest = ""
        else:
            return False
    return rest == ""


def sha256(b):
    return hashlib.sha256(b).hexdigest()


def error_code(body):
    return body.get("code") if isinstance(body, dict) else None


def mean(xs):
    return sum(xs) / len(xs)


def recall_at_5(ids, relevant):
    return len(set(ids[:5]) & relevant) / len(relevant) if relevant else None


def ndcg_at_10(ids, relevant):
    if not relevant:
        return None
    dcg = sum(1 / math.log2(i + 2) for i, cid in enumerate(ids[:10]) if cid in relevant)
    idcg = sum(1 / math.log2(i + 2) for i in range(min(len(relevant), 10)))
    return dcg / idcg


def git_rev():
    import subprocess
    try:
        return subprocess.run(["git", "-C", str(EVAL_DIR), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip() or None
    except OSError:
        return None


def run(args):
    s = Scorer(args.inputs)
    report = {"status": "invalid_evidence", "scorer_revision": git_rev(),
              "targets": {"paraphrase_min_gain": PARAPHRASE_MIN_GAIN, "exact_term_max_regression": EXACT_TERM_MAX_REGRESSION,
                          "no_answer_min_correct": NO_ANSWER_MIN_CORRECT, "unauthorized_disclosures": 0,
                          "answer_positive_controls": list(ANSWER_CONTROLS)}}
    s.load_inputs()
    report["inputs"] = s.input_hashes
    counts = {}
    for q in s.queries.values():
        counts.setdefault(q["split"], {}).setdefault(q["category"], 0)
        counts[q["split"]][q["category"]] += 1
    report["query_counts"] = counts
    if counts.get("held_out", {}).get("paraphrase") != 5 or counts.get("held_out", {}).get("exact_term") != 3:
        s.warnings.append(
            f"PROTOCOL.md states 5 held-out paraphrase / 3 held-out exact_term queries and a 17/17 split; the hash-pinned "
            f"queries.jsonl has {counts.get('held_out', {}).get('paraphrase')} / {counts.get('held_out', {}).get('exact_term')} "
            f"and {sum(counts.get('dev', {}).values())} dev / {sum(counts.get('held_out', {}).values())} held_out. "
            f"Labels are not changed; every figure below states its actual n.")

    dirs = {"mock": args.mock, "real": args.real}
    if args.real_norerank and Path(args.real_norerank).exists():
        dirs["real-norerank"] = args.real_norerank
    passes = {label: s.load_pass(label, d) for label, d in dirs.items()}
    # Provenance: every pass must come from the same code + inputs.
    revs = {l: (P.get("meta") or {}).get("revisions") for l, P in passes.items()}
    if len({json.dumps(v, sort_keys=True) for v in revs.values()}) > 1:
        s.err("provenance", f"passes ran against different revisions/dirty state: {revs}")
    for P in passes.values():
        if P.get("doc_ids") and P.get("chunks"):
            s.score_pass(P)

    real = passes["real"].get("metrics")
    decision = {"split": "dev", "category": "paraphrase"}
    if real:
        dev = real["dev"]["paraphrase"]
        pre, post = dev["hybrid-prererank"]["recall_at_5"], dev["final"]["recall_at_5"]
        decision.update(n=dev["final"]["n"], hybrid_prererank_recall_at_5=pre, final_recall_at_5=post)
        if pre is None or post is None:
            s.err("decision-d", "undefined required metric: real dev paraphrase Recall@5")
        else:
            keep = post >= pre
            decision["decision"] = "keep reranker" if keep else "disable reranker (CORTEX_DISABLE_RERANK=1)"
            decision["served_profile"] = "real" if keep else "real-norerank"
            if not keep and "real-norerank" not in passes:
                s.err("decision-d", "Decision D selects real-norerank for held-out, but no real-norerank pass was supplied")
    report["decision_d"] = decision
    served = passes.get(decision.get("served_profile", ""), {})

    # Gates are evaluated only on fully valid evidence.
    if not s.errors:
        m = served["metrics"]["held_out"]
        for cat, name, ok_fn, thr in (
                ("paraphrase", "held-out paraphrase gain (served final - FTS) >= +0.15", lambda d: d >= PARAPHRASE_MIN_GAIN, PARAPHRASE_MIN_GAIN),
                ("exact_term", "held-out exact_term regression (served final - FTS) >= -0.10", lambda d: d >= -EXACT_TERM_MAX_REGRESSION, -EXACT_TERM_MAX_REGRESSION)):
            fts, fin = m[cat]["fts-only"]["recall_at_5"], m[cat]["final"]["recall_at_5"]
            delta = round(fin - fts, 9)
            s.gate(name, ok_fn(delta), profile=served["label"], split="held_out", category=cat,
                   n=m[cat]["final"]["n"], fts_recall_at_5=fts, served_final_recall_at_5=fin, delta=delta, threshold=thr)
        for l, P in passes.items():
            u = P["unauthorized"]
            s.gate(f"{l}: zero unauthorized disclosure at every stage", not u["disclosures"],
                   requests=u["requests"], disclosures=u["disclosures"])
            s.gate(f"{l}: documented denial (403 forbidden) at every stage", not u["bad_denials"],
                   requests=u["requests"], denials_ok=u["denials_ok"], bad=u["bad_denials"])
        na = served["no_answer"]
        s.gate("no_answer: explicit decline >= 3 of 4 (POST /answers, ADR-0008 decline body)", na["correct"] >= NO_ANSWER_MIN_CORRECT,
               profile=served["label"], correct=na["correct"], n=na["n"],
               historical_first_citation_correct=na["historical_first_citation_correct"], note=NO_ANSWER_NOTE)
        ctl = served["answers"]["groups"].get("positive controls", {})
        s.gate(f"answer positive controls {'/'.join(ANSWER_CONTROLS)}: labeled passage answered and cited (all {len(ANSWER_CONTROLS)})",
               ctl.get("correct_answer") == len(ANSWER_CONTROLS), profile=served["label"],
               correct=ctl.get("correct_answer"), n=len(ANSWER_CONTROLS),
               outcomes={q: served["answers"]["rows"][q]["outcome"] for q in ANSWER_CONTROLS if q in served["answers"]["rows"]})
        for l, P in passes.items():
            bad = {q: r["violations"] for q, r in P["answers"]["rows"].items() if r["violations"]}
            s.gate(f"{l}: every authorized /answers response satisfies the AnswerResponse contract", not bad,
                   requests=len(P["answers"]["rows"]), violations=bad)

    for d in sorted(s.label_defects):
        s.warnings.append(f"label defect (excluded, not a required metric; fixing it needs a visible PROTOCOL.md revision): {d}")
    report["passes"] = {l: {k: P.get(k) for k in ("dir", "ok", "meta", "metrics", "unauthorized", "answers", "no_answer", "per_query")}
                        for l, P in passes.items()}
    report["gates"] = s.gates
    report["warnings"] = s.warnings
    report["errors_total"] = len(s.errors)
    report["errors"] = s.errors[:MAX_LISTED]
    if s.errors:
        report["status"], code = "invalid_evidence", 2
    elif all(g["passed"] for g in s.gates):
        report["status"], code = "pass", 0
    else:
        report["status"], code = "quality_gate_failed", 1
    report["exit_code"] = code
    write_report(args.report, report)
    print_summary(report)
    return code


def write_report(path, report):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(report, indent=2, default=str))


def fmt(x):
    return "n/a" if x is None else f"{x:.3f}"


def print_summary(r):
    for w in r["warnings"]:
        print(f"WARNING: {w}")
    if r["errors"]:
        print(f"INVALID EVIDENCE: {r['errors_total']} problem(s); nothing below is a measured result")
        for e in r["errors"][:40]:
            print(f"  - {e}")
        if r["errors_total"] > 40:
            print(f"  ... {r['errors_total'] - 40} more in the report")
    d = r.get("decision_d", {})
    if "decision" in d:
        print(f"Decision D (real, dev paraphrase n={d['n']}): hybrid-prererank R@5={fmt(d['hybrid_prererank_recall_at_5'])} "
              f"final R@5={fmt(d['final_recall_at_5'])} -> {d['decision']}; held-out served profile: {d['served_profile']}")
    for l, P in r["passes"].items():
        m = P.get("metrics")
        if not m or r["errors"]:
            continue
        print(f"=== {l}: held-out Recall@5 / nDCG@10 (n per row) ===")
        for cat in ["all_ranked"] + RANKED:
            row = m["held_out"][cat]
            print(f"  {cat:<14} n={row['final']['n_scored']}/{row['final']['n']} " + "  ".join(
                f"{st}={fmt(row[st]['recall_at_5'])}/{fmt(row[st]['ndcg_at_10'])}" for st in STAGES))
        print(f"=== {l}: /answers outcomes (correct answer / wrong answer / false decline / correct decline, of n) ===")
        for g, c in sorted(P["answers"]["groups"].items()):
            print(f"  {g:<24} n={c['n']:<3} correct_answer={c['correct_answer']} wrong_answer={c['wrong_answer']} "
                  f"false_decline={c['false_decline']} correct_decline={c['correct_decline']}"
                  + (f" undefined={c['undefined']}" if c["undefined"] else "")
                  + (f" contract_violations={c['contract_violations']}" if c["contract_violations"] else ""))
    for g in r["gates"]:
        extra = {k: v for k, v in g.items() if k in ("n", "fts_recall_at_5", "served_final_recall_at_5", "delta", "correct", "requests",
                                                     "denials_ok", "historical_first_citation_correct", "outcomes")}
        print(f"{'PASS' if g['passed'] else 'FAIL'}: {g['name']} {extra}")
    print(f"status: {r['status']} (exit {r['exit_code']})")


def print_plan(public_id, restricted_id, empty_id, inputs=EVAL_DIR):
    """run-eval.sh's request list, from the same required_files() the
    scorer validates against: name, endpoint, principal, X-Eval-Stage
    header value ("-" = none: the served path), JSON body; tab-separated
    (no empty fields: IFS=<tab> read collapses them)."""
    s = Scorer(inputs)
    s.load_question_sets()
    if s.errors:
        raise SystemExit("; ".join(s.errors))
    collections = {"public": public_id, "restricted": restricted_id, "empty": empty_id}
    for name, (principal, stage) in s.required_files().items():
        qid = name.split("__")[0]
        q = s.queries.get(qid) or s.answerability[qid]
        body = {"tenant": "acme", "collection_id": collections[q["collection"]], "query": q["text"]}
        if stage == "answer":
            endpoint, header = "/answers", "-"
        else:
            endpoint, header = "/search", "-" if stage == "final" else stage
            body["top_k"] = 10
        print("\t".join([name, endpoint, principal, header, json.dumps(body)]))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["plan"]:
        print_plan(*argv[1:4])
        return 0
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mock", default=str(EVAL_DIR / "results-mock"))
    ap.add_argument("--real", default=str(EVAL_DIR / "results-real"))
    ap.add_argument("--real-norerank", default=str(EVAL_DIR / "results-real-norerank"),
                    help="validated when present; required when Decision D disables the reranker")
    ap.add_argument("--report", default=str(EVAL_DIR / "report.json"))
    ap.add_argument("--inputs", default=str(EVAL_DIR), help="directory holding queries.jsonl, corpus/, HASHES.txt")
    args = ap.parse_args(argv)
    try:
        return run(args)
    except Exception:
        tb = traceback.format_exc()
        write_report(args.report, {"status": "scorer_error", "exit_code": 2, "traceback": tb[-4000:]})
        print(tb, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
