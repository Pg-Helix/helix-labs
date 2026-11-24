#!/usr/bin/env python3
"""Answer-quality benchmark (ANSWER-QUALITY.md): label checks, plan, scorer.

  python3 answer_quality.py check [--chunks RESULTS_DIR]
  python3 answer_quality.py plan PUBLIC_ID RESTRICTED_ID EMPTY_ID   (run-eval.sh)
  python3 answer_quality.py score RESULTS_DIR [--report FILE]

score grades a run-eval.sh results directory made with
EVAL_SUITE=answer-quality on the served profile (real-norerank), after
the existing score.py evidence checks (run metadata, provider/model/
reranker/answer rule, revisions, document mapping, chunk inventory,
per-request transport status, HTTP 200, JSON, AnswerResponse contract
and citations). It credits a required fact only when its quote appears
in the returned extract of a valid citation of the quote's own document.
Exit 0 = valid evidence (a baseline: no quality target), 2 = invalid.

check validates the frozen fixture (answer-quality-v1.json) against the
hash-pinned public corpus: 4/4/4 new questions (ids and texts unused by
queries.jsonl / answerability.jsonl), every required fact's quote
verbatim and exactly once in its source document, diagnostics matching
their source rows. With --chunks (a run-eval.sh results directory: its
doc_ids.tsv + chunks.jsonl) it also resolves every quote to exactly one
passage of the pinned chunker and checks that each answerable
question's facts fit the API's answer bound: at most 3 passages whose
stripped texts, joined by " ... ", fit in 2,000 bytes.
check exits 0 = labels valid, 2 = invalid.
"""
import argparse
import hashlib
import itertools
import json
import re
import sys
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(EVAL_DIR))
import score as base  # noqa: E402  (the existing evidence checks)

FIXTURE = "answer-quality-v1.json"
PROFILE = "real-norerank"  # the served profile (PROTOCOL.md Decision D)
# The canonical v1 freeze: the commit that froze the fixture, its rubric
# and its HASHES.txt digest, pushed (with CI) before any of its questions
# reached /answers. A pin, never derived from local history: in a shallow
# checkout `git log -- <fixture>` names whatever commit was cloned.
FREEZE_COMMIT = "de2bdc7ab21d2d536c736f5a7402092cb801b594"
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


def read_hashes(inputs):
    hashes, section = {}, None
    for line in (Path(inputs) / "HASHES.txt").read_text().splitlines():
        if line.endswith(":") and " " not in line:
            section = line[:-1]
            continue
        p = line.split()
        if len(p) == 2:
            hashes[section or p[1]] = p[0]
    return hashes


def suite_meta(inputs=EVAL_DIR, repo=None):
    """run.json's suite record (run-eval.sh EVAL_SUITE=answer-quality):
    the fixture's sha256, the canonical freeze commit (the pin, in full and
    shallow checkouts alike) and, separately, the commit this run ran at."""
    import subprocess
    repo = Path(repo) if repo else Path(inputs).parent
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    return {"name": "answer-quality", "fixture": FIXTURE, "fixture_sha256": sha256((Path(inputs) / FIXTURE).read_bytes()),
            "fixture_commit": FREEZE_COMMIT, "run_commit": head if re.fullmatch(r"[0-9a-f]{40}", head) else None}


def check_fixture(fx, inputs=EVAL_DIR):
    """Document-level label checks. Returns a list of problems."""
    inputs = Path(inputs)
    errs = []
    hashes = read_hashes(inputs)
    got = sha256((inputs / FIXTURE).read_bytes())
    if hashes.get(FIXTURE) != got:
        errs.append(f"fixture: {FIXTURE} sha256 {got} != HASHES.txt pin {hashes.get(FIXTURE)}")
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


# ---------------------------------------------------------------- plan

def print_plan(public_id, restricted_id, empty_id, inputs=EVAL_DIR):
    """run-eval.sh's request list for EVAL_SUITE=answer-quality: one
    /answers request as alice on the public collection per question and
    diagnostic (same tab-separated format as score.py plan)."""
    for q, _ in items(load_fixture(inputs)):
        body = {"tenant": "acme", "collection_id": public_id, "query": q["text"]}
        print("\t".join([f"{q['id']}__answer", "/answers", "alice", "-", json.dumps(body)]))


# ---------------------------------------------------------------- scoring

class BenchmarkScorer(base.Scorer):
    """score.py's Scorer, with this suite's request plan."""

    def __init__(self, inputs, fx):
        super().__init__(inputs)
        self.fx = fx

    def required_files(self):
        return {f"{q['id']}__answer": ("alice", "answer") for q, _ in items(self.fx)}


def split_extracts(answer, texts):
    """The returned extract of each cited passage: answer must be the
    passages' stripped texts joined by the separator in citation order,
    the last possibly cut at the byte bound. Returns [(extract,
    truncated)] or None when the answer is not the cited passages' text."""
    sep = base.EXTRACTIVE_SEPARATOR
    rest, out = answer, []
    for i, t in enumerate(texts):
        t = t.strip()
        if i:
            if not rest.startswith(sep):
                return None
            rest = rest[len(sep):]
        if rest.startswith(t):
            out.append((t, False))
            rest = rest[len(t):]
        elif i == len(texts) - 1 and rest and t.startswith(rest):
            out.append((rest, True))
            rest = ""
        else:
            return None
    return out if rest == "" else None


def citation_ok(c, P):
    """A citation of an ingested public passage whose source_id and
    source_digest match it."""
    if not (isinstance(c, dict) and all(isinstance(c.get(k), str) and c.get(k) for k in ("chunk_id", "source_id", "source_digest"))):
        return False
    ch = P["chunks"].get(c["chunk_id"])
    return bool(ch) and ch["collection"] == "public" and c["source_id"] == P["doc_ids"].get(ch["doc"]) \
        and c["source_digest"] == c["chunk_id"].split("|")[1]


def grade(q, chk, body, P):
    """One question's outcome under the rubric. chk is score.py's
    check_answer result (contract violations, decline)."""
    cits = body.get("citations") if isinstance(body, dict) and isinstance(body.get("citations"), list) else []
    known = [isinstance(c, dict) and c.get("chunk_id") in P["chunks"] for c in cits]
    parts = split_extracts(chk["answer"], [P["chunks"][c["chunk_id"]]["text"] for c in cits]) if cits and all(known) else None
    extracts = []
    for i, c in enumerate(cits):
        doc = P["chunks"][c["chunk_id"]]["doc"] if known[i] else None
        text, truncated = parts[i] if parts else (None, False)
        extracts.append({"citation": i, "document": doc, "valid_citation": citation_ok(c, P),
                         "bytes": len(text.encode()) if text is not None else None, "truncated": truncated,
                         "text": text, "credits": []})
    credited = {}
    for fact in q["facts"]:
        for doc, quote in evidence(fact):
            for e in extracts:
                if e["valid_citation"] and e["document"] == doc and e["text"] is not None and quote in e["text"]:
                    credited.setdefault(fact["id"], e["citation"])
                    if fact["id"] not in e["credits"]:
                        e["credits"].append(fact["id"])
    for e in extracts:
        e["unrelated"] = not e["credits"]
    missing = [f["id"] for f in q["facts"] if f["id"] not in credited]
    if q["expect"] == "decline":
        outcome = "correct_decline" if chk["declined"] else "wrong"
    elif chk["declined"]:
        outcome = "false_decline"
    elif chk["violations"] or not credited:
        outcome = "wrong"
    else:
        outcome = "complete" if not missing else "partial"
    if parts is None and cits:
        chk["violations"] = chk["violations"] + ["extracts cannot be associated with citations: no fact credited"]
    return {"expect": q["expect"], "kind": q.get("kind", "diagnostic"), "outcome": outcome,
            "required_facts": [f["id"] for f in q["facts"]], "credited_facts": credited, "missing_facts": missing,
            "extracts": extracts, "answer_bytes": len(chk["answer"].encode()), "violations": chk["violations"]}


def summarize(rows):
    """Counts with denominators over a set of graded rows."""
    ans = [r for r in rows.values() if r["expect"] == "answer"]
    dec = [r for r in rows.values() if r["expect"] == "decline"]
    count = lambda rs, o: sum(r["outcome"] == o for r in rs)
    req = sum(len(r["required_facts"]) for r in ans)
    missing_declined = sum(len(r["missing_facts"]) for r in ans if r["outcome"] == "false_decline")
    missing = sum(len(r["missing_facts"]) for r in ans)
    ex = [e for r in rows.values() for e in r["extracts"]]
    return {
        "questions": len(rows),
        "answerable": {"n": len(ans), **{o: count(ans, o) for o in ("complete", "partial", "wrong", "false_decline")}},
        "unsupported": {"n": len(dec), **{o: count(dec, o) for o in ("correct_decline", "wrong")}},
        "facts": {"required": req, "credited": req - missing, "missing": missing,
                  "missing_in_answers": missing - missing_declined, "missing_in_declines": missing_declined},
        "extracts": {"returned": len(ex), "unrelated": sum(e["unrelated"] for e in ex),
                     "unrelated_in_answerable": sum(e["unrelated"] for r in ans for e in r["extracts"]),
                     "unrelated_in_unsupported": sum(e["unrelated"] for r in dec for e in r["extracts"])},
        "contract_violations": sum(bool(r["violations"]) for r in rows.values()),
    }


def score_run(results_dir, inputs=EVAL_DIR):
    inputs = Path(inputs)
    fx = load_fixture(inputs)
    s = BenchmarkScorer(inputs, fx)
    s.load_inputs()
    for e in check_fixture(fx, inputs):
        s.err("fixture", e)
    fixture_sha = sha256((inputs / FIXTURE).read_bytes())
    P = s.load_pass(PROFILE, results_dir)
    meta = P.get("meta") or {}
    suite = meta.get("suite") if isinstance(meta.get("suite"), dict) else {}
    if suite.get("name") != "answer-quality" or suite.get("fixture") != FIXTURE or suite.get("fixture_sha256") != fixture_sha:
        s.err(PROFILE, f"run.json suite {suite!r} is not answer-quality with {FIXTURE} sha256 {fixture_sha}")
    if suite.get("fixture_commit") != FREEZE_COMMIT:
        s.err(PROFILE, f"run.json suite fixture_commit {suite.get('fixture_commit')!r} != the canonical v1 freeze {FREEZE_COMMIT}")
    labs = ((meta.get("revisions") or {}).get("helix-labs") or {}).get("commit")
    if "run_commit" in suite and suite["run_commit"] != labs:
        s.err(PROFILE, f"run.json suite run_commit {suite['run_commit']!r} != revisions.helix-labs.commit {labs!r}")
    fit = {}
    if P.get("chunks"):
        e, _, fit = resolve(fx, {cid: (c["doc"], c["text"]) for cid, c in P["chunks"].items()})
        for x in e:
            s.err(f"{PROFILE}/labels", x)
    rows, diag = {}, {}
    for q, is_diag in items(fx):
        chk = s.check_answer(P, q["id"], "public", f"{PROFILE}/{q['id']}/answer")
        if chk is None:
            continue
        (diag if is_diag else rows)[q["id"]] = grade(q, chk, P["responses"][f"{q['id']}__answer"]["body"], P)
    report = {
        "benchmark": {"name": fx["name"], "version": fx["version"], "fixture": FIXTURE, "fixture_sha256": fixture_sha,
                      "fixture_commit": suite.get("fixture_commit"), "run_commit": suite.get("run_commit"), "rubric": fx["rubric"]},
        "profile": PROFILE, "results_dir": str(results_dir), "scorer_revision": base.git_rev(),
        "run": {k: meta.get(k) for k in ("started_at", "finished_at", "requests", "inputs", "revisions", "provider", "db_binding")},
        "label_fit": fit,
        "errors_total": len(s.errors), "errors": s.errors[:base.MAX_LISTED],
    }
    if not s.errors:
        report["headline"] = summarize(rows)
        report["questions"] = rows
        report["diagnostics"] = {"summary": summarize(diag), "rows": diag,
                                 "known_issues": {d["id"]: d["known_issue"] for d in fx["diagnostics"]}}
    report["status"], report["exit_code"] = ("invalid_evidence", 2) if s.errors else ("measured", 0)
    return report


def print_report(r, fx):
    if r["errors"]:
        print(f"INVALID EVIDENCE: {r['errors_total']} problem(s); nothing below is a measured result")
        for e in r["errors"][:40]:
            print(f"  - {e}")
    else:
        text = {q["id"]: q for q, _ in items(fx)}
        for title, rows, summ in (("headline: 12 frozen questions", r["questions"], r["headline"]),
                                  ("diagnostics (outside the headline)", r["diagnostics"]["rows"], r["diagnostics"]["summary"])):
            print(f"=== {title} ({PROFILE}) ===")
            for qid, row in rows.items():
                docs = ", ".join(f"{e['document']}{' (truncated)' if e['truncated'] else ''}{'' if e['credits'] else ' [unrelated]'}"
                                 for e in row["extracts"]) or "-"
                print(f"  {qid:<6} {row['outcome']:<16} credited {len(row['credited_facts'])}/{len(row['required_facts'])}"
                      f"{' missing ' + ','.join(row['missing_facts']) if row['missing_facts'] else ''}  extracts: {docs}"
                      f"{'  VIOLATIONS: ' + '; '.join(row['violations']) if row['violations'] else ''}")
            a, u, f, x = summ["answerable"], summ["unsupported"], summ["facts"], summ["extracts"]
            print(f"  answerable n={a['n']}: complete={a['complete']} partial={a['partial']} wrong={a['wrong']} false_decline={a['false_decline']}")
            print(f"  unsupported n={u['n']}: correct_decline={u['correct_decline']} wrong={u['wrong']}")
            print(f"  required facts: credited {f['credited']}/{f['required']}, missing {f['missing']}/{f['required']} "
                  f"({f['missing_in_answers']} in answers, {f['missing_in_declines']} in false declines)")
            print(f"  extracts: unrelated {x['unrelated']}/{x['returned']} "
                  f"({x['unrelated_in_answerable']} in answerable, {x['unrelated_in_unsupported']} in unsupported)")
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
    c = sub.add_parser("check", help="validate the frozen fixture's labels")
    c.add_argument("--inputs", default=str(EVAL_DIR))
    c.add_argument("--chunks", help="a run-eval.sh results directory (doc_ids.tsv + chunks.jsonl) of the pinned stack")
    sc = sub.add_parser("score", help="grade an EVAL_SUITE=answer-quality results directory")
    sc.add_argument("results_dir")
    sc.add_argument("--inputs", default=str(EVAL_DIR))
    sc.add_argument("--report", default=str(EVAL_DIR / "report-answer-quality.json"))
    args = ap.parse_args(argv)
    if args.cmd == "score":
        try:
            r = score_run(args.results_dir, args.inputs)
        except Exception:
            import traceback
            r = {"status": "scorer_error", "exit_code": 2, "errors": [], "errors_total": 0, "traceback": traceback.format_exc()[-4000:]}
            print(r["traceback"], file=sys.stderr)
        base.write_report(args.report, r)
        if "traceback" not in r:
            print_report(r, load_fixture(args.inputs))
        return r["exit_code"]
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
