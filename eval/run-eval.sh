#!/bin/sh
# Runs helix-labs/eval's retrieval comparison (PROTOCOL.md) against one
# provider profile, on its own fresh stack, and records everything
# score.py needs to validate before it computes anything.
#
# Usage: sh run-eval.sh mock|real|real-norerank [OUT_DIR]
#   mock           Foundry default (helix-infer Mock embeddings) -- plumbing baseline
#   real           + docker-compose.real-embeddings.yml (nomic-embed-text:v1.5 via Ollama)
#   real-norerank  real, with CORTEX_DISABLE_RERANK=1 (PROTOCOL.md Decision D)
# OUT_DIR defaults to eval/results-<profile>. An existing OUT_DIR is only
# replaced if it is empty or was written by this script (marker file).
#
# OUT_DIR contents:
#   run.json          profile, completed flag, git revisions + dirty state,
#                     input hashes, effective provider/model/reranker (read
#                     from the running containers), DB embedding binding
#   doc_ids.tsv       corpus file -> cortex document id -> collection
#   chunks.jsonl      every active chunk (id, document, FULL text) -- the
#                     inventory passage-level relevance is resolved against
#   <query>__<stage>.json/.code/.curl   one response body, HTTP status and
#                     curl exit status (0 = transfer completed) per request
#                     (score.py plan lists them: all four ranking stages for
#                     every query; /answers for every authorized v1 query
#                     and every answerability.jsonl row; unauthorized
#                     queries as bob at all four stages + /answers, plus an
#                     authorized alice control at all four stages)
#
# EVAL_SUITE=answer-quality runs the frozen answer-quality benchmark
# (ANSWER-QUALITY.md) instead, on the same fresh-stack setup: its plan is
# answer_quality.py plan (15 /answers requests), OUT_DIR defaults to
# eval/results-answer-quality-<profile>, run.json also records the suite
# (fixture file, sha256, last commit), and answer_quality.py score grades
# it. Without EVAL_SUITE the run (and its 201-request plan) is unchanged.
# The X-Eval-Stage header (dense-only, fts-only, hybrid-prererank) is
# test-only instrumentation, live only because this sets
# CORTEX_TEST_HOOKS=1; "final" is the ordinary served /search path.
set -eu
EVAL_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

PROFILE=${1:?usage: sh run-eval.sh mock|real|real-norerank [OUT_DIR]}
case "$PROFILE" in
    mock) WF2_REAL_EMBEDDINGS=0; CORTEX_DISABLE_RERANK=0 ;;
    real) WF2_REAL_EMBEDDINGS=1; CORTEX_DISABLE_RERANK=0 ;;
    real-norerank) WF2_REAL_EMBEDDINGS=1; CORTEX_DISABLE_RERANK=1 ;;
    *) echo "usage: sh run-eval.sh mock|real|real-norerank [OUT_DIR]" >&2; exit 64 ;;
esac
export WF2_REAL_EMBEDDINGS CORTEX_DISABLE_RERANK

case "${EVAL_SUITE:-}" in
    "") SUITE_PLAN="$EVAL_DIR/score.py"; OUT_DIR=${2:-$EVAL_DIR/results-$PROFILE} ;;
    answer-quality) SUITE_PLAN="$EVAL_DIR/answer_quality.py"; OUT_DIR=${2:-$EVAL_DIR/results-answer-quality-$PROFILE} ;;
    *) echo "run-eval: unknown EVAL_SUITE ${EVAL_SUITE}" >&2; exit 64 ;;
esac
case "$OUT_DIR" in /*) ;; *) OUT_DIR="$PWD/$OUT_DIR" ;; esac
MARKER=.helix-eval-results
# Validate before anything is deleted: never a symlink, never a non-
# directory, never a populated directory this script did not create.
if [ -L "$OUT_DIR" ] || { [ -e "$OUT_DIR" ] && [ ! -d "$OUT_DIR" ]; }; then
    echo "run-eval: refusing OUT_DIR $OUT_DIR (symlink or not a directory)" >&2; exit 64
fi
if [ -d "$OUT_DIR" ] && [ -n "$(ls -A "$OUT_DIR")" ] && [ ! -f "$OUT_DIR/$MARKER" ]; then
    echo "run-eval: refusing to replace $OUT_DIR: not empty and not written by run-eval.sh (move historical results elsewhere)" >&2; exit 64
fi

# Accepted: this attempt owns OUT_DIR from here on. Invalidate whatever an
# earlier attempt left BEFORE anything else can fail (helpers, Compose,
# migrations): a failed startup must never leave a previous completed run
# behind to be scored as this one.
STARTED=$(date -u +%Y-%m-%dT%H:%M:%SZ)
rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"
: > "$OUT_DIR/$MARKER"
printf '{"schema": 2, "profile": "%s", "completed": false, "started_at": "%s"}\n' "$PROFILE" "$STARTED" > "$OUT_DIR/run.json"

CORTEX_TEST_HOOKS=1
export CORTEX_TEST_HOOKS
. "$EVAL_DIR/../workflow-2/lib.sh"
trap wf2_cleanup EXIT INT TERM

wf2_up

# write_meta COMPLETED [REQUESTS]: run.json, rewritten at the end with
# completed=true -- a crashed run leaves completed=false behind. Written
# to a temp file and renamed, so a reader never sees a partial run.json.
write_meta() {
    BINDING=${BINDING:-[]} STARTED="$STARTED" PROFILE="$PROFILE" ROOT="$EVAL_DIR/../.." SUITE="${EVAL_SUITE:-}" \
        python3 - "$OUT_DIR/run.json.tmp" "$EVAL_DIR" "$WF2_TMP/provider.json" "$1" "${2:-0}" <<'EOF'
import datetime, hashlib, json, os, pathlib, re, subprocess, sys
out, eval_dir, provider, completed, requests = sys.argv[1:6]
root = pathlib.Path(os.environ["ROOT"]).resolve()
def git(repo, *a):
    return subprocess.run(["git", "-C", str(root / repo), *a], capture_output=True, text=True).stdout
revs = {}
for r in ("helix-labs", "helix-foundry", "helix-kernel", "helix-cortex"):
    dirty = git(r, "status", "--porcelain", "--untracked-files=no").strip() != ""
    revs[r] = {"commit": git(r, "rev-parse", "HEAD").strip() or None, "dirty": dirty,
               "diff_sha256": hashlib.sha256(git(r, "diff", "HEAD").encode()).hexdigest() if dirty else None}
gomod = (root / "helix-cortex" / "go.mod").read_text()
deps = dict(re.findall(r"github.com/Pg-Helix/(helix-infer|helix-protocol)\S*\s+(\S+)", gomod))
corpus = sorted(pathlib.Path(eval_dir, "corpus").rglob("*.md"))
meta = {
    "schema": 2, "profile": os.environ["PROFILE"], "completed": completed == "true",
    "started_at": os.environ["STARTED"],
    "finished_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if completed == "true" else None,
    "requests": int(requests),
    "inputs": {"queries_sha256": hashlib.sha256(pathlib.Path(eval_dir, "queries.jsonl").read_bytes()).hexdigest(),
               "corpus_sha256": hashlib.sha256(b"".join(p.read_bytes() for p in corpus)).hexdigest(),
               "answerability_sha256": hashlib.sha256(pathlib.Path(eval_dir, "answerability.jsonl").read_bytes()).hexdigest()},
    "revisions": revs, "cortex_go_mod": deps,
    "provider": json.load(open(provider)),
    "db_binding": json.loads(os.environ["BINDING"]),
}
if os.environ["SUITE"] == "answer-quality":
    fx = pathlib.Path(eval_dir, "answer-quality-v1.json")
    meta["suite"] = {"name": "answer-quality", "fixture": fx.name, "fixture_sha256": hashlib.sha256(fx.read_bytes()).hexdigest(),
                     "fixture_commit": git("helix-labs", "log", "-1", "--format=%H", "--", "eval/" + fx.name).strip() or None}
json.dump(meta, open(out, "w"), indent=2)
EOF
    mv "$OUT_DIR/run.json.tmp" "$OUT_DIR/run.json"
}
write_meta false

# ingest COLLECTION_ID LABEL FILES...
ingest() {
    _col=$1 _label=$2; shift 2
    for f in "$@"; do
        name=$(basename "$f")
        body=$(python3 -c "import json,sys; print(json.dumps({'tenant':'acme','name':sys.argv[1],'content_type':'text/markdown','content':open(sys.argv[2]).read()}))" "$name" "$f")
        code=$(api POST "/collections/$_col/documents" "$ALICE_TOKEN" "$body")
        [ "$code" = "201" ] || { echo "run-eval: ingest $name failed (http $code)" >&2; cat "$WF2_BODY" >&2; exit 1; }
        docid=$(json_field id)
        printf '%s\t%s\t%s\n' "$name" "$docid" "$_label" >> "$OUT_DIR/doc_ids.tsv"
        wf2_wait_document_status "$_col" "$docid" "$ALICE_TOKEN" ready "${EVAL_READY_TIMEOUT:-120}"
    done
}

echo "eval($PROFILE): creating collections"
code=$(api POST /collections "$ALICE_TOKEN" '{"tenant":"acme","name":"eval-public"}')
[ "$code" = "201" ] || { echo "run-eval: create eval-public failed (http $code)" >&2; exit 1; }
COL_PUBLIC=$(json_field id)
code=$(api POST /collections "$ALICE_TOKEN" '{"tenant":"acme","name":"eval-restricted"}')
[ "$code" = "201" ] || { echo "run-eval: create eval-restricted failed (http $code)" >&2; exit 1; }
COL_RESTRICTED=$(json_field id)
code=$(api POST /collections "$ALICE_TOKEN" '{"tenant":"acme","name":"eval-empty"}')
[ "$code" = "201" ] || { echo "run-eval: create eval-empty failed (http $code)" >&2; exit 1; }
COL_EMPTY=$(json_field id)
# Alice creates (owns) all three; bob holds NO grant on eval-restricted --
# he is the unauthorized principal for the F* queries. eval-empty stays
# empty: an authorized collection with no eligible passage (must decline).

echo "eval($PROFILE): ingesting corpus"
: > "$OUT_DIR/doc_ids.tsv"
ingest "$COL_PUBLIC" public "$EVAL_DIR"/corpus/*.md
ingest "$COL_RESTRICTED" restricted "$EVAL_DIR"/corpus/restricted/*.md

pg_admin -c "SELECT json_build_object(
        'chunk_id', c.document_id || '|' || c.content_digest || '|' || c.chunker_version || '|' || c.chunk_index,
        'document_id', c.document_id, 'text', c.text)
    FROM cortex.chunk c JOIN cortex.document_version v ON v.id = c.document_version_id
    WHERE v.superseded_at IS NULL AND c.collection_id IN ('$COL_PUBLIC', '$COL_RESTRICTED')
    ORDER BY 1::text" > "$OUT_DIR/chunks.jsonl"
BINDING=$(pg_admin -c "SELECT coalesce(json_agg(json_build_object('model_id', model_id, 'model_revision', model_revision,
        'dims', dims, 'metric', metric, 'pipeline_version', pipeline_version) ORDER BY id), '[]')
    FROM helix_ai.source WHERE schema_name = 'cortex' AND table_name = 'chunk'")
export BINDING

echo "eval($PROFILE): running queries"
python3 "$SUITE_PLAN" plan "$COL_PUBLIC" "$COL_RESTRICTED" "$COL_EMPTY" > "$WF2_TMP/plan.tsv"
TAB=$(printf '\t')
n=0
while IFS="$TAB" read -r name endpoint principal stage body; do
    case "$principal" in alice) token=$ALICE_TOKEN ;; bob) token=$BOB_TOKEN ;; esac
    [ "$stage" = "-" ] && hdr="" || hdr="X-Eval-Stage: $stage"
    if ! record_request "$OUT_DIR" "$name" POST "$endpoint" "$token" "$body" "$hdr"; then
        echo "run-eval: $name: transfer failed (curl exit $(cat "$OUT_DIR/$name.curl"), http $(cat "$OUT_DIR/$name.code")); aborting, run.json stays completed=false" >&2
        exit 1
    fi
    n=$((n + 1))
done < "$WF2_TMP/plan.tsv"

write_meta true "$n"
echo "eval($PROFILE): done -- $n requests, results in $OUT_DIR"
