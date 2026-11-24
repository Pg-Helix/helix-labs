#!/usr/bin/env python3
"""Pre-registered experiment (EXPERIMENT-sentence-support.md): does
best-matching-sentence support recover more requested facts while
preserving correct declines? Development data only.

  python3 sentence_support.py plan PUBLIC_ID RESTRICTED_ID EMPTY_ID   (run-eval.sh EVAL_SUITE=dev-replay)
  python3 sentence_support.py suite-meta
  python3 sentence_support.py replay RESULTS_DIR --url URL --manifest FILE --dockerfile FILE
                                     [--cache FILE] [--report FILE]
  (sentence-support.sh runs replay against the pinned Ollama model.)

replay: validates the dev-replay evidence with score.py's checks; checks
the model weights against helix-foundry's pin; reproduces every live
/answers selection from the served order and recomputed passage cosines
(control rule, 0.707); selects the candidate cutoff from development data
with the accepted calibration method; replays the candidate; reports both
rules, the go/no-go criteria and the added embedding work.
Exit 0 = valid replay (whatever the decision), 2 = invalid evidence or the
control could not be reproduced.
"""
import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(EVAL_DIR))
import answer_quality as aq  # noqa: E402
import calibrate  # noqa: E402
import score as base  # noqa: E402

PROFILE = "real-norerank"
CONTROL_CUTOFF = 0.707          # helix-cortex embedconfig, the served rule
MAX_PASSAGES, MAX_BYTES, CANDIDATE_LIMIT = 3, 2000, 50   # helix-cortex retrieval/answer.go, store/retrieve.go
SEP = base.EXTRACTIVE_SEPARATOR
SEARCH_TOP_K = 50               # >= every served candidate (15 public passages)
CONTROLS = base.ANSWER_CONTROLS  # A1 A4 B1 B2
REFERENCE = {"snippets_returned": 16, "snippets": 23, "complete": 14, "answerable": 19,
             "declined": 7, "unsupported": 7, "off_label": 5}
EXCLUDED = ("C3",)              # historical snippet does not resolve: reported separately


def sentences(text):
    """The pre-registered deterministic split."""
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]


def select(order, support, cutoff):
    """Cortex Store.Answer: the first MAX_PASSAGES candidates, in served
    order and within the first CANDIDATE_LIMIT, whose support >= cutoff."""
    out = []
    for i, cid in enumerate(order):
        if len(out) >= MAX_PASSAGES or i >= CANDIDATE_LIMIT:
            break
        if support.get(cid) is not None and support[cid] >= cutoff:
            out.append(cid)
    return out


def assemble(passages):
    """retrieval.AssembleExtractiveAnswer, byte for byte: [(chunk_id, text)]
    -> (answer, [(chunk_id, returned extract)])."""
    b, used = b"", []
    for i, (cid, text) in enumerate(passages):
        if i >= MAX_PASSAGES:
            break
        t = text.strip().encode()
        if not t:
            continue
        sep = SEP.encode() if b else b""
        if len(b) + len(sep) + len(t) > MAX_BYTES:
            remaining = MAX_BYTES - len(b) - len(sep)
            if remaining <= 0:
                break
            t = t[:remaining]
        b += sep + t
        used.append((cid, t.decode("utf-8", "replace")))
        if len(b) >= MAX_BYTES:
            break
    return b.decode("utf-8", "replace"), used


# ---------------------------------------------------------------- plan

def dev_cases():
    """The 27 calibration development cases, chosen from labels and splits
    only (calibrate.select_cases), with their labeled snippets."""
    cases = calibrate.select_cases()
    rows = {r["id"]: r for name in ("queries.jsonl", "answerability.jsonl") for r in calibrate.rows(name)}
    for c in cases:
        c["labels"] = rows[c["id"]]["relevant"]
    return cases


def print_plan(public_id, restricted_id, empty_id):
    ids = {"public": public_id, "empty": empty_id}
    for c in dev_cases():
        q = {"tenant": "acme", "collection_id": ids[c["collection"]], "query": c["text"]}
        print("\t".join([f"{c['id']}__served", "/search", "alice", "-", json.dumps(dict(q, top_k=SEARCH_TOP_K))]))
        print("\t".join([f"{c['id']}__answer", "/answers", "alice", "-", json.dumps(q)]))


def suite_meta():
    return {"name": "dev-replay", "cases": [c["id"] for c in dev_cases()], "search_top_k": SEARCH_TOP_K}


class DevScorer(base.Scorer):
    def required_files(self):
        out = {}
        for c in dev_cases():
            out[f"{c['id']}__served"] = ("alice", "served")
            out[f"{c['id']}__answer"] = ("alice", "answer")
        return out


# ---------------------------------------------------------------- embeddings

class Embedder:
    """calibrate.embed with a cache keyed by sha256(weights digest, text)."""

    def __init__(self, url, weights, cache_path):
        self.url, self.weights, self.path = url, weights, Path(cache_path) if cache_path else None
        self.cache = json.loads(self.path.read_text()) if self.path and self.path.exists() else {}
        self.calls = {"passage": 0, "query": 0, "sentence": 0}
        self.hits = {"passage": 0, "query": 0, "sentence": 0}
        self.seconds = {"passage": 0.0, "query": 0.0, "sentence": 0.0}

    def __call__(self, kind, text):
        key = hashlib.sha256(f"{self.weights}\0{text}".encode()).hexdigest()
        if key in self.cache:
            self.hits[kind] += 1
        else:
            t = time.monotonic()
            self.cache[key] = calibrate.embed(self.url, text)
            self.seconds[kind] += time.monotonic() - t
            self.calls[kind] += 1
        return self.cache[key]

    def save(self):
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.cache))


# ---------------------------------------------------------------- measures

def grade(case, extracts, chunks):
    """extracts: [(chunk_id, returned text)]. Label returned iff its snippet
    is in the returned extract of a citation of its own document."""
    labels = []
    for lab in case["labels"]:
        hit = any(chunks[cid]["doc"] == lab["document"] and lab["snippet"] in text for cid, text in extracts)
        labels.append({"document": lab["document"], "snippet": lab["snippet"], "returned": hit})
    off = [cid for cid, text in extracts
           if not any(chunks[cid]["doc"] == lab["document"] and lab["snippet"] in text for lab in case["labels"])]
    if case["expect"] == "decline":
        outcome = "declined" if not extracts else "answered"
    elif not extracts:
        outcome = "declined"
    else:
        n = sum(l["returned"] for l in labels)
        outcome = "complete" if n == len(labels) else "partial" if n else "wrong"
    return {"outcome": outcome, "labels": labels, "cited": [chunks[c]["doc"] for c, _ in extracts],
            "off_label": [chunks[c]["doc"] for c in off], "extracts": len(extracts)}


def measures(rows):
    scorable = {k: r for k, r in rows.items() if k not in EXCLUDED}
    ans = [r for r in scorable.values() if r["expect"] == "answer"]
    dec = [r for r in scorable.values() if r["expect"] == "decline"]
    labs = [l for r in ans for l in r["labels"]]
    count = lambda rs, o: sum(r["outcome"] == o for r in rs)
    return {
        "snippets": {"returned": sum(l["returned"] for l in labs), "n": len(labs)},
        "answerable": {"n": len(ans), **{o: count(ans, o) for o in ("complete", "partial", "wrong", "declined")}},
        "unsupported": {"n": len(dec), "declined": count(dec, "declined"), "answered": count(dec, "answered")},
        "off_label_extracts": {"n": sum(len(r["off_label"]) for r in scorable.values()),
                               "of_returned": sum(r["extracts"] for r in scorable.values())},
        "positive_controls": {"retained": sum(rows[c]["outcome"] == "complete" for c in CONTROLS), "n": len(CONTROLS)},
    }


def criteria(m):
    ref = REFERENCE
    return {
        f"snippet coverage above {ref['snippets_returned']}/{ref['snippets']}": m["snippets"]["returned"] > ref["snippets_returned"],
        f"at least {ref['complete']} complete answers": m["answerable"]["complete"] >= ref["complete"],
        f"all {ref['unsupported']} unsupported cases declined": m["unsupported"]["declined"] == ref["unsupported"],
        f"all {len(CONTROLS)} positive controls retained": m["positive_controls"]["retained"] == len(CONTROLS),
        f"off-label extracts at most {ref['off_label']}": m["off_label_extracts"]["n"] <= ref["off_label"],
    }


# ---------------------------------------------------------------- replay

def replay(args):
    errs = []
    s = DevScorer(EVAL_DIR)
    s.load_inputs()
    P = s.load_pass(PROFILE, args.results_dir)
    suite = (P.get("meta") or {}).get("suite") or {}
    if suite.get("name") != "dev-replay" or suite.get("cases") != [c["id"] for c in dev_cases()]:
        s.err(PROFILE, f"run.json suite {suite!r} is not this dev-replay plan")
    cases = dev_cases()
    served, live = {}, {}
    for c in cases:
        got = s.search_results(P, f"{c['id']}__served", f"{PROFILE}/{c['id']}/served")
        if got is not None:
            served[c["id"]] = got[0]
        chk = s.check_answer(P, c["id"], c["collection"], f"{PROFILE}/{c['id']}/answer")
        if chk is not None:
            if chk["violations"]:
                s.err(f"{PROFILE}/{c['id']}/answer", f"contract violations: {chk['violations']}")
            live[c["id"]] = chk
    if s.errors:
        return {"status": "invalid_evidence", "exit_code": 2, "errors": s.errors[:base.MAX_LISTED]}

    # Pinned model (calibrate.py's check).
    dockerfile = Path(args.dockerfile).read_text()
    pinned = re.search(r"^ARG MODEL_WEIGHTS_DIGEST=(\S+)", dockerfile, re.M).group(1).replace("sha256-", "sha256:")
    layers = json.loads(Path(args.manifest).read_text()).get("layers", [])
    weights = next((l["digest"] for l in layers if l.get("mediaType", "").endswith(".model")), None)
    if weights != pinned:
        return {"status": "invalid_evidence", "exit_code": 2, "errors": [f"model weights {weights} != pinned {pinned}"]}
    emb = Embedder(args.url, weights, args.cache)
    public = {cid: c for cid, c in P["chunks"].items() if c["collection"] == "public"}
    pvec = {cid: emb("passage", calibrate.DOCUMENT_PREFIX + c["text"]) for cid, c in public.items()}
    split = {cid: sentences(c["text"]) for cid, c in public.items()}
    t0 = time.monotonic()
    svec = {cid: [emb("sentence", calibrate.DOCUMENT_PREFIX + x) for x in xs] for cid, xs in split.items()}
    sentence_seconds = time.monotonic() - t0

    control_support, cand_support = {}, {}
    for c in cases:
        if c["collection"] != "public":
            control_support[c["id"]] = cand_support[c["id"]] = {}
            continue
        q = emb("query", calibrate.QUERY_PREFIX + c["text"])
        control_support[c["id"]] = {cid: calibrate.cosine(q, v) for cid, v in pvec.items()}
        cand_support[c["id"]] = {cid: max(calibrate.cosine(q, v) for v in vs) for cid, vs in svec.items()}

    # 1. Reproduce every live selection with the control rule.
    rows = {"live": {}, "control": {}}
    for c in cases:
        order = served.get(c["id"], [])
        if any(cid not in public for cid in order):
            errs.append(f"{c['id']}: served order contains a non-public passage")
            continue
        sel = select(order, control_support[c["id"]], CONTROL_CUTOFF)
        text, used = assemble([(cid, public[cid]["text"]) for cid in sel])
        lv = live[c["id"]]
        if [cid for cid, _ in used] != lv["cited"] or text != lv["answer"]:
            errs.append(f"{c['id']}: control replay {[public[x]['doc'] for x, _ in used]} != live {[public[x]['doc'] for x in lv['cited']]}"
                        f"{'' if text == lv['answer'] else ' (answer text differs)'}")
        rows["control"][c["id"]] = dict(expect=c["expect"], **grade(c, used, public))
        live_parts = aq.split_extracts(lv["answer"], [public[x]["text"] for x in lv["cited"]]) or []
        rows["live"][c["id"]] = dict(expect=c["expect"], **grade(c, [(x, t) for x, (t, _) in zip(lv["cited"], live_parts)], public))
    if errs:
        return {"status": "control_not_reproduced", "exit_code": 2, "errors": errs}

    # The control's own cutoff, re-derived from these vectors (sanity).
    def decision_cases(support):
        out = []
        for c in cases:
            sup = support[c["id"]]
            if c["collection"] == "empty":
                v = None
            elif c["expect"] == "answer":
                v = max(v for cid, v in sup.items() if public[cid]["doc"] in c["documents"])
            else:
                v = max(sup.values())
            out.append({"id": c["id"], "expect": c["expect"], "value": v})
        return out
    control_dc, cand_dc = decision_cases(control_support), decision_cases(cand_support)
    control_sel = calibrate.select_cutoff(control_dc)
    if control_sel["rounded"] != CONTROL_CUTOFF:
        return {"status": "control_not_reproduced", "exit_code": 2,
                "errors": [f"re-derived control cutoff {control_sel['rounded']} != {CONTROL_CUTOFF}"]}

    # 2. Candidate cutoff: the accepted calibration method, development only.
    cand_sel = calibrate.select_cutoff(cand_dc)
    cand_without_c3 = calibrate.select_cutoff([x for x in cand_dc if x["id"] != "C3"])
    cutoff = cand_sel["rounded"]

    # 3. Candidate replay.
    rows["candidate"] = {}
    for c in cases:
        sel = select(served.get(c["id"], []), cand_support[c["id"]], cutoff)
        _, used = assemble([(cid, public[cid]["text"]) for cid in sel])
        rows["candidate"][c["id"]] = dict(expect=c["expect"], **grade(c, used, public))

    m = {k: measures(v) for k, v in rows.items()}
    crit = criteria(m["candidate"])
    reference_ok = (m["control"]["snippets"]["returned"], m["control"]["snippets"]["n"], m["control"]["answerable"]["complete"],
                    m["control"]["answerable"]["n"], m["control"]["unsupported"]["declined"], m["control"]["off_label_extracts"]["n"]) == \
        (REFERENCE["snippets_returned"], REFERENCE["snippets"], REFERENCE["complete"], REFERENCE["answerable"],
         REFERENCE["declined"], REFERENCE["off_label"])
    if not reference_ok or m["control"] != m["live"]:
        return {"status": "control_not_reproduced", "exit_code": 2,
                "errors": [f"control measures {m['control']} do not reproduce the reference {REFERENCE} / live {m['live']}"]}
    emb.save()
    n_sent = sum(len(x) for x in split.values())
    near = lambda dc, t: sorted(({"id": x["id"], "expect": x["expect"], "value": round(x["value"], 6)} for x in dc if x["value"] is not None),
                                key=lambda x: abs(x["value"] - t))[:6]
    return {
        "status": "measured", "exit_code": 0, "experiment": "EXPERIMENT-sentence-support.md",
        "scorer_revision": base.git_rev(), "results_dir": str(args.results_dir),
        "run": {k: (P["meta"] or {}).get(k) for k in ("revisions", "provider", "inputs", "started_at", "finished_at")},
        "model": {"weights_digest": weights, "query_prefix": calibrate.QUERY_PREFIX, "document_prefix": calibrate.DOCUMENT_PREFIX},
        "control": {"cutoff": CONTROL_CUTOFF, "rederived": {k: control_sel[k] for k in ("midpoint", "rounded", "correct", "n", "interval")},
                    "reproduced_live_selections": len(cases)},
        "candidate": {"cutoff": cutoff, "selection": {k: cand_sel[k] for k in ("midpoint", "rounded", "correct", "n", "interval", "gap")},
                      "without_c3": {k: cand_without_c3[k] for k in ("midpoint", "rounded", "correct", "n")},
                      "closest_decision_values": near(cand_dc, cutoff)},
        "measures": m, "criteria": crit, "decision": "go" if all(crit.values()) else "no-go",
        "rows": rows,
        "c3": {k: rows[k]["C3"] for k in ("control", "candidate")},
        "decision_values": {"control": control_dc, "candidate": cand_dc},
        "embedding_work": {
            "passages": len(public), "sentences": n_sent,
            "sentences_per_passage": sorted(len(x) for x in split.values()),
            "indexing_embeddings": {"control": len(public), "candidate_added": n_sent},
            "query_embeddings_per_question": {"control": 1, "candidate": 1},
            "cosines_per_question": {"control": len(public), "candidate": n_sent},
            "added_vector_storage_bytes_float32": n_sent * len(next(iter(pvec.values()))) * 4,
            "this_run": {"calls": emb.calls, "cache_hits": emb.hits, "seconds": {k: round(v, 2) for k, v in emb.seconds.items()},
                         "sentence_embedding_wall_seconds": round(sentence_seconds, 2)},
        },
    }


def print_report(r):
    if r["exit_code"]:
        print(f"{r['status'].upper()}: nothing below is a measured result")
        for e in r["errors"]:
            print(f"  - {e}")
        print(f"status: {r['status']} (exit {r['exit_code']})")
        return
    c, k = r["control"], r["candidate"]
    print(f"control: cutoff {c['cutoff']} (re-derived {c['rederived']['rounded']}, {c['rederived']['correct']}/{c['rederived']['n']}); "
          f"reproduced {c['reproduced_live_selections']}/27 live selections")
    print(f"candidate: cutoff {k['cutoff']} (midpoint {k['selection']['midpoint']:.7f} of {k['selection']['interval']}, "
          f"{k['selection']['correct']}/{k['selection']['n']}; without C3 {k['without_c3']['rounded']}, {k['without_c3']['correct']}/{k['without_c3']['n']})")
    print(f"{'case':<5} {'exp':<7} {'control':<9} {'candidate':<9} control cited -> candidate cited")
    for cid, row in r["rows"]["control"].items():
        cand = r["rows"]["candidate"][cid]
        print(f"{cid:<5} {row['expect']:<7} {row['outcome']:<9} {cand['outcome']:<9} {','.join(row['cited']) or '-'} -> {','.join(cand['cited']) or '-'}"
              f"{'  (C3: reported separately)' if cid in EXCLUDED else ''}")
    for name in ("control", "candidate"):
        m = r["measures"][name]
        print(f"{name}: snippets {m['snippets']['returned']}/{m['snippets']['n']}; answerable n={m['answerable']['n']} "
              f"complete={m['answerable']['complete']} partial={m['answerable']['partial']} wrong={m['answerable']['wrong']} "
              f"declined={m['answerable']['declined']}; unsupported declined {m['unsupported']['declined']}/{m['unsupported']['n']}; "
              f"off-label extracts {m['off_label_extracts']['n']}/{m['off_label_extracts']['of_returned']}; "
              f"controls {m['positive_controls']['retained']}/{m['positive_controls']['n']}")
    for name, ok in r["criteria"].items():
        print(f"{'PASS' if ok else 'FAIL'}: {name}")
    w = r["embedding_work"]
    print(f"embedding work: {w['passages']} passages -> {w['sentences']} sentences; +{w['indexing_embeddings']['candidate_added']} "
          f"indexing embeddings; query embeddings unchanged (1); cosines per question {w['cosines_per_question']['control']} -> "
          f"{w['cosines_per_question']['candidate']}; +{w['added_vector_storage_bytes_float32']} bytes of float32 vectors")
    print(f"decision: {r['decision'].upper()}")
    print(f"status: {r['status']} (exit {r['exit_code']})")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["plan"]:
        print_plan(*argv[1:4])
        return 0
    if argv[:1] == ["suite-meta"]:
        print(json.dumps(suite_meta()))
        return 0
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("replay")
    rp.add_argument("results_dir")
    rp.add_argument("--url", required=True)
    rp.add_argument("--manifest", required=True)
    rp.add_argument("--dockerfile", required=True)
    rp.add_argument("--cache", default=None)
    rp.add_argument("--report", default=str(EVAL_DIR / "report-sentence-support.json"))
    args = ap.parse_args(argv)
    r = replay(args)
    base.write_report(args.report, r)
    print_report(r)
    return r["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
