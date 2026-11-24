#!/usr/bin/env python3
"""Answer-quality benchmark (ANSWER-QUALITY.md): frozen fixture checks.

  python3 answer_quality.py check [--chunks RESULTS_DIR]

check validates the frozen fixture (answer-quality-v1.json) against the
hash-pinned public corpus: 4/4/4 new questions (ids and texts unused by
queries.jsonl / answerability.jsonl), every required fact's quote
verbatim and exactly once in its source document, diagnostics matching
their source rows. With --chunks (a run-eval.sh results directory: its
doc_ids.tsv + chunks.jsonl) it also resolves every quote to exactly one
passage of the pinned chunker and checks that each answerable
question's facts fit the API's answer bound: at most 3 passages whose
stripped texts, joined by " ... ", fit in 2,000 bytes.
Exit 0 = labels valid, 2 = invalid.
"""
import argparse
import hashlib
import itertools
import json
import re
import sys
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
FIXTURE = "answer-quality-v1.json"
KINDS = {"single_fact": 4, "multi_passage": 4, "unsupported": 4}


def sha256(b):
    return hashlib.sha256(b).hexdigest()


def norm(text):
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def rows(path):
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def load_fixture(inputs=EVAL_DIR):
    return json.loads((Path(inputs) / FIXTURE).read_text())


def items(fx):
    """(item, is_diagnostic) for the 12 questions, then the diagnostics."""
    return [(q, False) for q in fx["questions"]] + [(d, True) for d in fx["diagnostics"]]


def evidence(fact):
    """Every (document, quote) that credits this fact: the supporting
    quote, then any acceptable alternatives."""
    return [(fact["document"], fact["quote"])] + [(a["document"], a["quote"]) for a in fact.get("alternatives", [])]


def check_fixture(fx, inputs=EVAL_DIR):
    """Document-level label checks. Returns a list of problems."""
    inputs = Path(inputs)
    errs = []
    hashes = {}
    section = None
    for line in (inputs / "HASHES.txt").read_text().splitlines():
        if line.endswith(":") and " " not in line:
            section = line[:-1]
            continue
        p = line.split()
        if len(p) == 2:
            hashes[section or p[1]] = p[0]
    corpus = sorted((inputs / "corpus").rglob("*.md"))
    combined = sha256(b"".join(p.read_bytes() for p in sorted(corpus, key=lambda p: str(p.relative_to(inputs)))))
    if fx["corpus"]["combined_sha256"] != combined or hashes.get("combined-corpus") != combined:
        errs.append(f"corpus: combined sha256 {combined} != fixture {fx['corpus']['combined_sha256']} / HASHES.txt {hashes.get('combined-corpus')}")
    public = {p.name: p.read_text() for p in corpus if p.parent.name != "restricted"}
    for name, digest in fx["corpus"]["documents"].items():
        path = inputs / "corpus" / name
        if not path.exists() or sha256(path.read_bytes()) != digest:
            errs.append(f"corpus: {name} missing or its sha256 != fixture")
    kinds = {}
    for q in fx["questions"]:
        kinds[q["kind"]] = kinds.get(q["kind"], 0) + 1
    if kinds != KINDS:
        errs.append(f"questions: kinds {kinds} != {KINDS}")
    existing = {}
    for path, key in (("queries.jsonl", "queries.jsonl"), ("answerability.jsonl", "answerability.jsonl")):
        for r in rows(inputs / path):
            existing[r["id"]] = (key, r)
    texts = {norm(r["text"]): rid for rid, (_, r) in existing.items()}
    seen = set()
    for q, diag in items(fx):
        where = f"{q['id']}"
        if q["id"] in seen:
            errs.append(f"{where}: duplicate id")
        seen.add(q["id"])
        if diag:
            src = existing.get(q["id"])
            if not src or src[0] != q["source"] or src[1]["text"] != q["text"]:
                errs.append(f"{where}: diagnostic does not match its {q['source']} row")
        else:
            if q["id"] in existing:
                errs.append(f"{where}: id already used by {existing[q['id']][0]}")
            if norm(q["text"]) in texts:
                errs.append(f"{where}: text repeats existing question {texts[norm(q['text'])]}")
            if q["expect"] != ("decline" if q["kind"] == "unsupported" else "answer"):
                errs.append(f"{where}: expect {q['expect']!r} does not fit kind {q['kind']!r}")
        if q["expect"] == "answer":
            if not q["facts"]:
                errs.append(f"{where}: answerable question without required facts")
            if q.get("kind") == "multi_passage" and len({f["document"] for f in q["facts"]}) not in (2, 3):
                errs.append(f"{where}: multi_passage needs facts from two or three documents")
        else:
            if q["facts"]:
                errs.append(f"{where}: unsupported question with facts")
            for n in q.get("neighbors", []):
                if n not in public:
                    errs.append(f"{where}: neighbor {n} is not a public document")
        for fact in q["facts"]:
            for doc, quote in evidence(fact):
                text = public.get(doc)
                if text is None:
                    errs.append(f"{where}/{fact['id']}: {doc} is not a public corpus document")
                elif text.count(quote) != 1:
                    errs.append(f"{where}/{fact['id']}: quote occurs {text.count(quote)} times in {doc} (need exactly 1): {quote!r}")
    return errs


def load_inventory(results_dir):
    """{chunk_id: (document name, text)} from a run-eval.sh results dir."""
    d = Path(results_dir)
    names = {}
    for line in (d / "doc_ids.tsv").read_text().splitlines():
        name, did, _ = line.split("\t")
        names[did] = name
    out = {}
    for line in (d / "chunks.jsonl").read_text().splitlines():
        c = json.loads(line)
        out[c["chunk_id"]] = (names[c["document_id"]], c["text"])
    return out


def resolve(fx, chunks):
    """Resolve every fact against a chunk inventory. Returns (problems,
    {item id: {fact id: [chunk ids that credit it]}}, {item id: fit})."""
    errs, where_found, fit = [], {}, {}
    lim = fx["api_limits"]
    for q, _ in items(fx):
        per = where_found.setdefault(q["id"], {})
        for fact in q["facts"]:
            hits = []
            for doc, quote in evidence(fact):
                found = [cid for cid, (name, text) in chunks.items() if name == doc and quote in text]
                if len(found) != 1:
                    errs.append(f"{q['id']}/{fact['id']}: quote resolves to {len(found)} passages of {doc} (need exactly 1)")
                hits += found
            per[fact["id"]] = sorted(set(hits))
        if not q["facts"] or any(not h for h in per.values()):
            continue
        best = None
        for combo in itertools.product(*per.values()):
            used = sorted(set(combo))
            size = sum(len(chunks[c][1].strip().encode()) for c in used) + len(lim["separator"].encode()) * (len(used) - 1)
            if len(used) <= lim["max_passages"] and size <= lim["max_answer_bytes"] and (best is None or size < best[1]):
                best = (used, size)
        if best is None:
            errs.append(f"{q['id']}: no set of <= {lim['max_passages']} passages within {lim['max_answer_bytes']} bytes holds every required fact")
        else:
            fit[q["id"]] = {"passages": len(best[0]), "bytes": best[1]}
    return errs, where_found, fit


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="validate the frozen fixture's labels")
    c.add_argument("--inputs", default=str(EVAL_DIR))
    c.add_argument("--chunks", help="a run-eval.sh results directory (doc_ids.tsv + chunks.jsonl) of the pinned stack")
    args = ap.parse_args(argv)
    fx = load_fixture(args.inputs)
    errs = check_fixture(fx, args.inputs)
    if args.chunks:
        e, _, fit = resolve(fx, load_inventory(args.chunks))
        errs += e
        for qid, f in fit.items():
            print(f"{qid}: required facts fit in {f['passages']} passage(s), {f['bytes']} bytes")
    for e in errs:
        print(f"INVALID LABEL: {e}", file=sys.stderr)
    print("labels valid" if not errs else f"{len(errs)} label problem(s)")
    return 2 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
