#!/usr/bin/env python3
"""Replays the /answers evidence-threshold calibration (helix-atlas ADR-0008).

Run through calibrate.sh, which starts helix-foundry's pinned Ollama image.
Steps, in this order:
  1. select the development cases from the hash-pinned inputs -- v1
     queries.jsonl split=dev on the public collection, plus
     answerability.jsonl set=dev -- before any embedding call. Held-out
     and challenge rows are never read into the selection.
  2. check the running model's weights digest against helix-foundry's pin;
  3. embed the 15 public corpus documents ("search_document: " + text) and
     each selected question ("search_query: " + text) with
     nomic-embed-text:v1.5 -- the prefixes helix-cortex embedconfig uses;
  4. per case, a document-level decision value: answerable -> highest
     cosine over its labeled documents; unanswerable -> highest cosine over
     all public documents; an empty collection has none (always declines);
  5. objective: maximize correct cases (answerable: value >= t;
     unanswerable: no value or value < t) over the midpoints between
     consecutive distinct decision values, ascending; ties go to the wider
     gap, then the lower midpoint. The served cutoff is the midpoint
     rounded to 3 decimals.
The replay must reproduce the committed record (calibration.json): same
case ids, every case on the same side of the served cutoff, same correct
count, same rounded cutoff, and every decision value within
VALUE_TOLERANCE. Exit 0 = reproduced, 1 = differs, 2 = could not run.
"""
import argparse
import hashlib
import json
import math
import os
import pathlib
import re
import sys
import tempfile
import urllib.request

EVAL = pathlib.Path(__file__).resolve().parent
QUERY_PREFIX, DOCUMENT_PREFIX = "search_query: ", "search_document: "
WIRE_MODEL = "nomic-embed-text:v1.5"
SERVED_CUTOFF = 0.707          # helix-cortex embedconfig calibrations
# Same weights, different CPU kernels: the arm64 record and an x86_64
# GitHub runner replay differed by up to 4.7e-4 per value (Labs CI run
# 45146471920). The closest case to the cutoff is 5.7e-3 away, so this
# cannot flip a decision; decisions themselves must match exactly.
VALUE_TOLERANCE = 1e-3


def sha256(b):
    return hashlib.sha256(b).hexdigest()


def rows(name):
    return [json.loads(l) for l in (EVAL / name).read_text().splitlines() if l.strip()]


def select_cases():
    """The development cases, chosen from labels and splits only."""
    cases = []
    for q in rows("queries.jsonl"):
        if q["split"] == "dev" and q["collection"] == "public":
            cases.append({"id": q["id"], "source": "queries.jsonl", "text": q["text"], "collection": "public",
                          "expect": "decline" if q["category"] == "no_answer" else "answer",
                          "documents": sorted({r["document"] for r in q["relevant"]})})
    for a in rows("answerability.jsonl"):
        if a["set"] == "dev":
            cases.append({"id": a["id"], "source": "answerability.jsonl", "text": a["text"], "collection": a["collection"],
                          "expect": a["expect"], "documents": sorted({r["document"] for r in a["relevant"]})})
    return cases


def embed(url, text):
    req = urllib.request.Request(url.rstrip("/") + "/v1/embeddings", json.dumps({"model": WIRE_MODEL, "input": [text]}).encode(),
                                 {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=120))["data"][0]["embedding"]


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b)) / math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))


def correct(case, t):
    v = case["value"]
    return (v is not None and v >= t) if case["expect"] == "answer" else (v is None or v < t)


def select_cutoff(cases):
    vals = sorted({c["value"] for c in cases if c["value"] is not None})
    best = None
    for lo, hi in zip(vals, vals[1:]):
        t = (lo + hi) / 2
        n = sum(correct(c, t) for c in cases)
        if best is None or (n, hi - lo) > (best["correct"], best["gap"]):
            best = {"correct": n, "midpoint": t, "interval": [lo, hi], "gap": hi - lo}
    best["rounded"] = round(best["midpoint"], 3)
    best["n"] = len(cases)
    return best


def compare_to_record(old, new, best):
    """Everything a replay must reproduce: inputs, the WHOLE model object
    (wire tag, dims, metric, prefixes, runtime image and weights digests),
    the case selection, each case's side of the served cutoff, the result,
    and every decision value within VALUE_TOLERANCE (cross-architecture)."""
    problems = []
    if old.get("inputs") != new["inputs"]:
        problems.append(f"input hashes differ from the committed record: {old.get('inputs')} vs {new['inputs']}")
    old_model = old.get("model") or {}
    for k in sorted(set(old_model) | set(new["model"])):
        if old_model.get(k) != new["model"].get(k):
            problems.append(f"model.{k} {new['model'].get(k)!r} != recorded {old_model.get(k)!r}")
    old_cases = old.get("cases") or []
    if [c.get("id") for c in old_cases] != [c["id"] for c in new["cases"]]:
        problems.append("selected cases differ from the committed record")
    for o, n in zip(old_cases, new["cases"]):
        if (o["value"] is None) != (n["value"] is None) or (o["value"] is not None and abs(o["value"] - n["value"]) > VALUE_TOLERANCE):
            problems.append(f"{n['id']} value {n['value']} != recorded {o['value']}")
        if correct(o, SERVED_CUTOFF) != correct(n, SERVED_CUTOFF):
            problems.append(f"{n['id']} lands on the other side of {SERVED_CUTOFF} than recorded")
    r = old.get("result") or {}
    if (r.get("correct"), r.get("rounded")) != (best["correct"], best["rounded"]):
        problems.append(f"result {best['correct']}/{best['rounded']} != recorded {r.get('correct')}/{r.get('rounded')}")
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="Ollama base URL serving the pinned model")
    ap.add_argument("--manifest", required=True, help="the running model's Ollama manifest (JSON file)")
    ap.add_argument("--dockerfile", required=True, help="helix-foundry/images/ollama/Dockerfile (the pins)")
    ap.add_argument("--out", required=True, help="where to write this replay's full record")
    ap.add_argument("--record", default=str(EVAL / "calibration.json"), help="committed record to reproduce")
    ap.add_argument("--arch", default=None, help="architecture the model ran on (recorded, not compared)")
    args = ap.parse_args()
    # The reference is never an output: refuse before anything is written.
    # Same resolved path (direct, relative, symlink) or, when both exist,
    # the same file (device + inode: a hard link is a second name for it).
    out, ref = pathlib.Path(args.out), pathlib.Path(args.record)
    if out.resolve() == ref.resolve() or (out.exists() and ref.exists() and out.samefile(ref)):
        print(f"--out {args.out} resolves to the committed reference {args.record} (same file); refusing to overwrite it", file=sys.stderr)
        return 2

    # 1. selection first -- nothing below can influence it.
    cases = select_cases()
    print(f"selected {len(cases)} development cases before embedding: {' '.join(c['id'] for c in cases)}")

    # 2. model artifact.
    dockerfile = pathlib.Path(args.dockerfile).read_text()
    pin = lambda k: re.search(rf"^ARG {k}=(\S+)", dockerfile, re.M).group(1)
    pinned_weights = pin("MODEL_WEIGHTS_DIGEST").replace("sha256-", "sha256:")
    layers = json.loads(pathlib.Path(args.manifest).read_text()).get("layers", [])
    served_weights = next((l["digest"] for l in layers if l.get("mediaType", "").endswith(".model")), None)
    if served_weights != pinned_weights:
        print(f"model weights {served_weights} != pinned {pinned_weights}; refusing to calibrate", file=sys.stderr)
        return 2

    # 3-4. embeddings and decision values.
    corpus = {p.name: p.read_text().strip() for p in sorted((EVAL / "corpus").glob("*.md"))}  # public only
    doc_vec = {n: embed(args.url, DOCUMENT_PREFIX + t) for n, t in corpus.items()}
    for c in cases:
        if c["collection"] == "empty":
            c["value"], c["value_from"] = None, "empty collection: no passage"
            continue
        qv = embed(args.url, QUERY_PREFIX + c["text"])
        sims = {n: cosine(qv, v) for n, v in doc_vec.items()}
        pool = c["documents"] if c["expect"] == "answer" else sorted(sims)
        c["value_from"] = "max over labeled documents" if c["expect"] == "answer" else "max over all public documents"
        c["value"] = max(sims[d] for d in pool)
        c["best_document"] = max(pool, key=lambda d: sims[d])

    # 5. selection, plus the C3 sensitivity check.
    best = select_cutoff(cases)
    without_c3 = select_cutoff([c for c in cases if c["id"] != "C3"])
    for c in cases:
        c["correct_at_cutoff"] = correct(c, best["midpoint"])
    answerable = [c["value"] for c in cases if c["expect"] == "answer"]
    record = {
        "inputs": {
            "queries_sha256": sha256((EVAL / "queries.jsonl").read_bytes()),
            "answerability_sha256": sha256((EVAL / "answerability.jsonl").read_bytes()),
            "public_corpus_sha256": sha256(b"".join((EVAL / "corpus" / n).read_bytes() for n in sorted(corpus))),
        },
        "model": {"wire_model": WIRE_MODEL, "dims": len(next(iter(doc_vec.values()))),
                  "ollama_image_digest": pin("OLLAMA_IMAGE_DIGEST"), "weights_digest": served_weights,
                  "metric": "cosine", "query_prefix": QUERY_PREFIX, "document_prefix": DOCUMENT_PREFIX},
        "runtime_arch": args.arch,
        "selection": "v1 queries.jsonl split=dev collection=public (18) + answerability.jsonl set=dev (9); held-out and challenge rows excluded",
        "objective": "maximize correct cases; answerable correct iff value >= t, unanswerable correct iff no value or value < t",
        "candidates": "midpoints between consecutive distinct decision values, ascending",
        "tie_break": "wider gap, then lower midpoint",
        "rounding": "midpoint rounded to 3 decimals",
        "result": best,
        "without_c3": without_c3,
        "answerable_value_range": [min(answerable), max(answerable)],
        "cases": [{k: c.get(k) for k in ("id", "source", "expect", "collection", "documents", "value", "value_from",
                                         "best_document", "correct_at_cutoff")} for c in cases],
    }
    # Written beside --out and renamed over it: replaces the directory entry,
    # never writes through it, so even a link created since the check above
    # cannot carry this replay into the reference.
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=out.parent, prefix=out.name + ".")  # a new file (O_EXCL)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(record, indent=2) + "\n")
    os.replace(tmp, out)
    print(f"cutoff: midpoint {best['midpoint']:.7f} of [{best['interval'][0]:.8f}, {best['interval'][1]:.8f}] "
          f"-> {best['rounded']}; {best['correct']}/{best['n']} correct (document-level)")
    print(f"without C3: {without_c3['correct']}/{without_c3['n']}, rounded cutoff {without_c3['rounded']}")
    print(f"answerable development values span {min(answerable):.3f}-{max(answerable):.3f}")

    # Reproduce the committed record.
    problems = []
    if best["rounded"] != SERVED_CUTOFF:
        problems.append(f"rounded cutoff {best['rounded']} != served {SERVED_CUTOFF}")
    rec_path = pathlib.Path(args.record)
    if rec_path.exists():
        old = json.loads(rec_path.read_text())
        problems += compare_to_record(old, record, best)
        if old.get("runtime_arch") != record["runtime_arch"]:
            print(f"note: recorded on {old.get('runtime_arch')}, replayed on {record['runtime_arch']}; values compared within {VALUE_TOLERANCE}")
    else:
        problems.append(f"no committed record at {rec_path} (this replay was written to {args.out})")
    for p in problems:
        print(f"MISMATCH: {p}", file=sys.stderr)
    print("replay reproduces the committed calibration" if not problems else "replay does NOT reproduce the committed calibration")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
