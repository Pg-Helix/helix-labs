#!/bin/sh
# Runs helix-labs/eval's retrieval comparison (PROTOCOL.md) against one
# provider profile. Reuses workflow-2/lib.sh's proven compose/api/
# json_field helpers rather than a parallel copy of that plumbing.
#
# Usage: sh run-eval.sh mock|real
#
# Each invocation brings up its OWN stack (mock profile, or real via
# WF2_REAL_EMBEDDINGS=1), ingests eval/corpus fresh, waits for every
# document to become ready, then for each labeled query in
# queries.jsonl calls /search once per applicable ranking stage
# (fts-only is computed in both passes as a consistency check; dense-
# only/hybrid-prererank/final reflect whichever provider this pass is
# running) via the test-only X-Eval-Stage header (CORTEX_TEST_HOOKS=1).
# Raw results go to results-<label>/ as one JSON file per query+stage;
# score.py (run separately, after both passes) computes the actual
# metrics and the comparison table.
set -eu
EVAL_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
WF2_DIR="$EVAL_DIR/../workflow-2"

PROVIDER_LABEL=${1:?usage: sh run-eval.sh mock|real}
case "$PROVIDER_LABEL" in
    mock) ;;
    real) WF2_REAL_EMBEDDINGS=1; export WF2_REAL_EMBEDDINGS ;;
    *) echo "usage: sh run-eval.sh mock|real" >&2; exit 1 ;;
esac

CORTEX_TEST_HOOKS=1
export CORTEX_TEST_HOOKS
. "$WF2_DIR/lib.sh"

trap wf2_cleanup EXIT INT TERM
wf2_up

OUT_DIR="$EVAL_DIR/results-$PROVIDER_LABEL"
rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"

echo "eval($PROVIDER_LABEL): creating collections"
code=$(api POST /collections "$ALICE_TOKEN" '{"tenant":"acme","name":"eval-public"}')
[ "$code" = "201" ] || { echo "FAIL: create eval-public ($code)"; cat /tmp/wf2_api_body.json; exit 1; }
COL_PUBLIC=$(json_field id)
code=$(api POST /collections "$ALICE_TOKEN" '{"tenant":"acme","name":"eval-restricted"}')
[ "$code" = "201" ] || { echo "FAIL: create eval-restricted ($code)"; cat /tmp/wf2_api_body.json; exit 1; }
COL_RESTRICTED=$(json_field id)
# Bob gets NO grant on eval-restricted -- he is this eval's "unauthorized"
# test principal for the F* queries. Alice (the creator/owner) is the
# only authorized principal for it.

echo "eval($PROVIDER_LABEL): ingesting public corpus"
: > "$OUT_DIR/doc_ids.tsv"
for f in "$EVAL_DIR"/corpus/*.md; do
    name=$(basename "$f")
    content=$(cat "$f")
    body=$(python3 -c "import json,sys; print(json.dumps({'tenant':'acme','name':sys.argv[1],'content_type':'text/markdown','content':sys.argv[2]}))" "$name" "$content")
    code=$(api POST "/collections/$COL_PUBLIC/documents" "$ALICE_TOKEN" "$body")
    [ "$code" = "201" ] || { echo "FAIL: ingest $name ($code)"; cat /tmp/wf2_api_body.json; exit 1; }
    docid=$(json_field id)
    printf '%s\t%s\t%s\n' "$name" "$docid" "public" >> "$OUT_DIR/doc_ids.tsv"
    wf2_wait_document_status "$COL_PUBLIC" "$docid" "$ALICE_TOKEN" ready 60
done

echo "eval($PROVIDER_LABEL): ingesting restricted corpus"
for f in "$EVAL_DIR"/corpus/restricted/*.md; do
    name=$(basename "$f")
    content=$(cat "$f")
    body=$(python3 -c "import json,sys; print(json.dumps({'tenant':'acme','name':sys.argv[1],'content_type':'text/markdown','content':sys.argv[2]}))" "$name" "$content")
    code=$(api POST "/collections/$COL_RESTRICTED/documents" "$ALICE_TOKEN" "$body")
    [ "$code" = "201" ] || { echo "FAIL: ingest restricted $name ($code)"; cat /tmp/wf2_api_body.json; exit 1; }
    docid=$(json_field id)
    printf '%s\t%s\t%s\n' "$name" "$docid" "restricted" >> "$OUT_DIR/doc_ids.tsv"
    wf2_wait_document_status "$COL_RESTRICTED" "$docid" "$ALICE_TOKEN" ready 60
done

run_query() {
    qid=$1 collection=$2 text=$3 stage=$4 token=$5
    outfile="$OUT_DIR/${qid}__${stage}.json"
    body=$(python3 -c "import json,sys; print(json.dumps({'tenant':'acme','collection_id':sys.argv[1],'query':sys.argv[2],'top_k':10}))" "$collection" "$text")
    if [ "$stage" = "final" ]; then
        extra=""
    else
        extra="X-Eval-Stage: $stage"
    fi
    code=$(api_json POST /search "$token" "$body" "$extra")
    echo "$code" > "$OUT_DIR/${qid}__${stage}.code"
    cp /tmp/wf2_api_body.json "$outfile"
}

echo "eval($PROVIDER_LABEL): running queries"
python3 -c "
import json
with open('$EVAL_DIR/queries.jsonl') as f:
    for line in f:
        line = line.strip()
        if line:
            print(json.loads(line)['id'])
" | while read -r qid; do
    QUERY_JSON=$(python3 -c "
import json
with open('$EVAL_DIR/queries.jsonl') as f:
    for line in f:
        line = line.strip()
        if not line: continue
        q = json.loads(line)
        if q['id'] == '$qid':
            print(json.dumps(q))
            break
")
    text=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['text'])" "$QUERY_JSON")
    category=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['category'])" "$QUERY_JSON")
    coll_label=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['collection'])" "$QUERY_JSON")

    if [ "$coll_label" = "public" ]; then
        collection="$COL_PUBLIC"
        token="$ALICE_TOKEN"
    else
        collection="$COL_RESTRICTED"
        # Unauthorized queries run as Bob, who holds no grant here --
        # the correct, required outcome is a 403 / zero disclosure for
        # every stage, checked by score.py.
        token="$BOB_TOKEN"
    fi

    if [ "$category" = "no_answer" ] || [ "$category" = "unauthorized" ]; then
        run_query "$qid" "$collection" "$text" "final" "$token"
    else
        run_query "$qid" "$collection" "$text" "fts-only" "$token"
        run_query "$qid" "$collection" "$text" "dense-only" "$token"
        run_query "$qid" "$collection" "$text" "hybrid-prererank" "$token"
        run_query "$qid" "$collection" "$text" "final" "$token"
    fi
    echo "eval($PROVIDER_LABEL): $qid done"
done

echo "eval($PROVIDER_LABEL): done -- results in $OUT_DIR"
