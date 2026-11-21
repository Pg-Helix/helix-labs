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

Relevance is passage-level (PROTOCOL.md "What relevant means"): a
returned chunk is relevant iff it belongs to a labeled document AND its
full text (from the chunk inventory, not the 200-char search snippet)
contains that label's snippet. Repeated chunks in one result list count
once.

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
MAX_LISTED = 200
REAL = {"provider": "openai-compat", "wire_model": "nomic-embed-text:v1.5",
        "model_id": "nomic-embed-text", "model_revision": "v1.5", "dims": 768}
PROFILES = {
    "mock": {"embed": {"provider": "mock"}, "reranker": "enabled"},
    "real": {"embed": REAL, "reranker": "enabled"},
    "real-norerank": {"embed": REAL, "reranker": "disabled"},
}
NO_ANSWER_NOTE = (
    "PROTOCOL.md defines no_answer correctness as: the system's top result (if any) is NOT "
    "labeled relevant to any other query's answer key. Scored on POST /answers (the endpoint "
    "that asserts an answer): its first citation, resolved through the chunk inventory against "
    "every other query's passage labels; an answer with no citation abstained. Open ambiguity: "
    "the protocol says 'confidently', but the extractive /answers contract exposes no confidence "
    "or abstention signal, so every cited answer is treated as asserted; /search final top-1 is "
    "reported alongside for comparison but does not gate.")


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
        self.docs = {p.name: ("restricted" if p.parent.name == "restricted" else "public") for p in corpus}
        self.queries = {}
        for n, line in enumerate(qbytes.decode().splitlines(), 1):
            if line.strip():
                q = json.loads(line)
                if q["id"] in self.queries:
                    self.err("inputs", f"duplicate query id {q['id']}")
                self.queries[q["id"]] = q

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
                if q["category"] == "no_answer":
                    req[f"{qid}__answer"] = ("alice", "answer")
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
        P["doc_ids"] = self.load_doc_ids(label, d / "doc_ids.tsv")
        P["chunks"] = self.load_chunks(label, d / "chunks.jsonl", P["doc_ids"])
        required = self.required_files()
        present = {}
        for f in d.iterdir():
            if f.name in ("run.json", "doc_ids.tsv", "chunks.jsonl", ".helix-eval-results"):
                continue
            m = re.fullmatch(r"(.+)\.(json|code)", f.name)
            if not m or m.group(1) not in required:
                self.err(label, f"unknown input file {f.name}")
                continue
            present.setdefault(m.group(1), set()).add(m.group(2))
        P["responses"] = {}
        for name in required:
            if present.get(name) != {"json", "code"}:
                self.err(f"{label}/{name}", "unexecuted request (response .json/.code missing)")
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
            if label != "mock":
                art = prov.get("model_artifact") or {}
                if not art.get("weights_digest") or art.get("weights_digest") != art.get("pinned_digest"):
                    self.err(label, f"model artifact digest {art.get('weights_digest')!r} != pinned {art.get('pinned_digest')!r}")
        revs = meta.get("revisions")
        if not isinstance(revs, dict) or not all(isinstance(revs.get(r), dict) and revs[r].get("commit")
                                                 for r in ("helix-labs", "helix-foundry", "helix-kernel", "helix-cortex")):
            self.err(label, "run.json lacks git revisions for helix-labs/foundry/kernel/cortex")

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
        for qid, q in self.queries.items():
            s = set()
            for item in q["relevant"]:
                hits = {cid for cid, c in P["chunks"].items()
                        if c["doc"] == item["document"] and item["snippet"] in c["text"]}
                if not hits:
                    msg = f"label {item['document']}/{item['snippet']!r} matches no ingested chunk"
                    if required(q):
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

    def score_no_answer(self, P, rel):
        label = P["label"]
        out = {"queries": {}, "correct": 0, "n": 0}
        for qid, q in self.queries.items():
            if q["category"] != "no_answer":
                continue
            out["n"] += 1
            others = {}
            for oid, s in rel.items():
                if oid != qid and s:
                    for cid in s:
                        others.setdefault(cid, []).append(oid)
            for st in STAGES[:-1]:
                self.search_results(P, f"{qid}__{st}", f"{label}/{qid}/{st}")
            where = f"{label}/{qid}/answer"
            r = P["responses"].get(f"{qid}__answer")
            if r is None:
                continue
            if "invalid" in r:
                self.err(where, r["invalid"]); continue
            if r["code"] != "200":
                self.err(where, f"unexpected HTTP status {r['code']} (want 200): {error_code(r['body'])}"); continue
            b = r["body"]
            cits = b.get("citations") if isinstance(b, dict) else None
            if not (isinstance(b, dict) and isinstance(b.get("answer"), str) and b.get("answer_method") == "extractive"
                    and isinstance(cits, (list, type(None)))
                    and all(isinstance(c, dict) and isinstance(c.get("chunk_id"), str) for c in cits or [])):
                self.err(where, "schema: not an extractive AnswerResponse"); continue
            cits = cits or []
            if any(c["chunk_id"] not in P["chunks"] for c in cits):
                self.err(where, "citation references a chunk absent from the inventory"); continue
            top = cits[0]["chunk_id"] if cits else None
            search = self.search_results(P, f"{qid}__final", f"{label}/{qid}/final")
            top_search = search[0][0] if search and search[0] else None
            entry = {
                "answer_chars": len(b["answer"]), "citations": len(cits),
                "top_citation_doc": P["chunks"][top]["doc"] if top else None,
                "top_citation_labeled_for": others.get(top, []),
                "correct": not others.get(top),
                "search_top1_doc": P["chunks"][top_search]["doc"] if top_search else None,
                "search_top1_labeled_for": others.get(top_search, []),
            }
            out["queries"][qid] = entry
            out["correct"] += entry["correct"]
        return out

    # ---------- gates ----------
    def gate(self, name, passed, **detail):
        self.gates.append(dict(name=name, passed=bool(passed), **detail))


def required(q):
    """Queries feeding a required metric: the held-out targets and Decision D."""
    return (q["split"] == "held_out" and q["category"] in ("paraphrase", "exact_term")) or \
        (q["split"] == "dev" and q["category"] == "paraphrase")


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
                          "no_answer_min_correct": NO_ANSWER_MIN_CORRECT, "unauthorized_disclosures": 0}}
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
        s.gate("no_answer correctness >= 3 of 4 (POST /answers top citation)", na["correct"] >= NO_ANSWER_MIN_CORRECT,
               profile=served["label"], correct=na["correct"], n=na["n"], note=NO_ANSWER_NOTE)

    for d in sorted(s.label_defects):
        s.warnings.append(f"label defect (excluded, not a required metric; fixing it needs a visible PROTOCOL.md revision): {d}")
    report["passes"] = {l: {k: P.get(k) for k in ("dir", "ok", "meta", "metrics", "unauthorized", "no_answer", "per_query")}
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
    for g in r["gates"]:
        extra = {k: v for k, v in g.items() if k in ("n", "fts_recall_at_5", "served_final_recall_at_5", "delta", "correct", "requests", "denials_ok")}
        print(f"{'PASS' if g['passed'] else 'FAIL'}: {g['name']} {extra}")
    print(f"status: {r['status']} (exit {r['exit_code']})")


def print_plan(public_id, restricted_id, inputs=EVAL_DIR):
    """run-eval.sh's request list, from the same required_files() the
    scorer validates against: name, endpoint, principal, X-Eval-Stage
    header value ("-" = none: the served path), JSON body; tab-separated
    (no empty fields: IFS=<tab> read collapses them)."""
    s = Scorer(inputs)
    s.queries = {q["id"]: q for q in map(json.loads, filter(str.strip, (Path(inputs) / "queries.jsonl").read_text().splitlines()))}
    for name, (principal, stage) in s.required_files().items():
        q = s.queries[name.split("__")[0]]
        body = {"tenant": "acme", "collection_id": restricted_id if q["collection"] == "restricted" else public_id, "query": q["text"]}
        if stage == "answer":
            endpoint, header = "/answers", "-"
        else:
            endpoint, header = "/search", "-" if stage == "final" else stage
            body["top_k"] = 10
        print("\t".join([name, endpoint, principal, header, json.dumps(body)]))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["plan"]:
        print_plan(*argv[1:3])
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
