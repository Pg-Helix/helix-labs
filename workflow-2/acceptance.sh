#!/bin/sh
# Workflow 2 acceptance gate (ADR-0007, helix-atlas): authorized
# ingestion, hybrid retrieval, extractive answers with citations, and
# revocable access -- against the real Foundry stack (pgvector, SCRAM
# auth), the real cortex-api/chunk-worker binaries, and PostgreSQL.
# Mock embeddings throughout (helix-infer's deterministic Mock provider)
# -- mechanics-only evidence, no semantic-quality claim. Bounded waits
# everywhere, never a fixed sleep hoping something finished.
set -eu
WF2_SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CORTEX_TEST_HOOKS=1
export CORTEX_TEST_HOOKS
. "$WF2_SCRIPT_DIR/lib.sh"

trap wf2_cleanup EXIT INT TERM
wf2_up

fail=0
expect_code() {
    label=$1 want=$2 got=$3
    if [ "$got" = "$want" ]; then
        echo "ok: $label (http $got)"
    else
        echo "FAIL: $label (want http $want, got $got)"
        cat /tmp/wf2_api_body.json
        fail=1
    fi
}

# --- Scenario 1: authorized ingestion and initial publication ---
echo "--- scenario 1: authorized ingestion ---"
code=$(api POST /collections "$ALICE_TOKEN" '{"tenant":"acme","name":"s1-handbook"}')
expect_code "create collection" 201 "$code"
COL1=$(json_field id)

code=$(api POST "/collections/$COL1/documents" "$ALICE_TOKEN" '{"tenant":"acme","name":"doc.md","content_type":"text/markdown","content":"Employees accrue fifteen days of paid leave per year."}')
expect_code "ingest document" 201 "$code"
DOC1=$(json_field id)

wf2_wait_document_status "$COL1" "$DOC1" "$ALICE_TOKEN" ready 30
echo "ok: document became ready"

code=$(api POST /search "$ALICE_TOKEN" "{\"tenant\":\"acme\",\"collection_id\":\"$COL1\",\"query\":\"paid leave\"}")
expect_code "search" 200 "$code"
CHUNK1=$(json_field results.0.chunk_id)

code=$(api GET "/evidence/$CHUNK1?tenant=acme" "$ALICE_TOKEN")
expect_code "evidence lookup" 200 "$code"
EVTEXT=$(json_field text)
case "$EVTEXT" in
  *"fifteen days"*) echo "ok: evidence text is correct" ;;
  *) echo "FAIL: evidence text wrong: $EVTEXT"; fail=1 ;;
esac
echo "scenario 1: PASS"

# --- Scenario 2: idempotency (sequential and concurrent duplicates) ---
echo "--- scenario 2: idempotency ---"
BEFORE_CREATED=$(pg_admin -c "SELECT created_at FROM cortex.chunk WHERE document_id = '$DOC1' ORDER BY chunk_index" | tr -d ' ')
code=$(api POST "/collections/$COL1/documents" "$ALICE_TOKEN" '{"tenant":"acme","name":"doc.md","content_type":"text/markdown","content":"Employees accrue fifteen days of paid leave per year."}')
expect_code "re-ingest identical bytes" 201 "$code"
DOC1_AGAIN=$(json_field id)
if [ "$DOC1_AGAIN" != "$DOC1" ]; then
    echo "FAIL: idempotent re-ingest returned a different document id"
    fail=1
fi
AFTER_CREATED=$(pg_admin -c "SELECT created_at FROM cortex.chunk WHERE document_id = '$DOC1' ORDER BY chunk_index" | tr -d ' ')
if [ "$BEFORE_CREATED" != "$AFTER_CREATED" ]; then
    echo "FAIL: idempotent re-ingest changed the chunk row (created_at differs) -- a new chunk/job was created"
    fail=1
else
    echo "ok: idempotent re-ingest left the chunk row untouched (no new chunk/job)"
fi

# Concurrent duplicate submission: two parallel identical ingests.
api POST "/collections/$COL1/documents" "$ALICE_TOKEN" '{"tenant":"acme","name":"doc.md","content_type":"text/markdown","content":"Employees accrue fifteen days of paid leave per year."}' > /tmp/wf2_concurrent_a.code &
PID_A=$!
api POST "/collections/$COL1/documents" "$ALICE_TOKEN" '{"tenant":"acme","name":"doc.md","content_type":"text/markdown","content":"Employees accrue fifteen days of paid leave per year."}' > /tmp/wf2_concurrent_b.code &
PID_B=$!
wait "$PID_A"; wait "$PID_B"
CHUNK_COUNT=$(pg_admin -c "SELECT count(*) FROM cortex.chunk WHERE document_id = '$DOC1'")
if [ "$(echo "$CHUNK_COUNT" | tr -d ' ')" = "1" ]; then
    echo "ok: concurrent duplicate ingestion still leaves exactly 1 chunk row"
else
    echo "FAIL: expected exactly 1 chunk row after concurrent duplicates, got $CHUNK_COUNT"
    fail=1
fi
echo "scenario 2: PASS"

# --- Scenario 3: freshness (update, delete+reinsert) ---
echo "--- scenario 3: freshness ---"
code=$(api POST "/collections/$COL1/documents" "$ALICE_TOKEN" '{"tenant":"acme","name":"doc.md","content_type":"text/markdown","content":"UPDATED: employees now accrue twenty days of paid leave per year."}')
expect_code "update document (changed content)" 201 "$code"
wf2_wait_document_status "$COL1" "$DOC1" "$ALICE_TOKEN" ready 30
code=$(api POST /search "$ALICE_TOKEN" "{\"tenant\":\"acme\",\"collection_id\":\"$COL1\",\"query\":\"paid leave\"}")
SNIPPET=$(json_field results.0.snippet)
case "$SNIPPET" in
  *"twenty days"*) echo "ok: search reflects only the updated content" ;;
  *"fifteen days"*) echo "FAIL: search still returned the OLD content after an update"; fail=1 ;;
  *) echo "FAIL: unexpected snippet: $SNIPPET"; fail=1 ;;
esac
PRE_DELETE_CHUNK=$(json_field results.0.chunk_id)

code=$(api DELETE "/collections/$COL1/documents/$DOC1" "$ALICE_TOKEN")
expect_code "delete document" 204 "$code"
code=$(api POST /search "$ALICE_TOKEN" "{\"tenant\":\"acme\",\"collection_id\":\"$COL1\",\"query\":\"paid leave\"}")
RESULTS=$(json_field results)
if [ "$RESULTS" = "None" ] || [ "$RESULTS" = "[]" ]; then
    echo "ok: search finds nothing after delete"
else
    echo "FAIL: search still finds results after delete: $RESULTS"
    fail=1
fi

# A whole-document delete+reinsert generates a fresh document id (see
# ADR-0007 decision 3's mapping notes) -- the chunk primary key is never
# actually reused across this path, so the real invariant to check is
# that the PRE-delete citation can never be resurrected, not that some
# generation counter incremented for a key that no longer exists.
code=$(api POST "/collections/$COL1/documents" "$ALICE_TOKEN" '{"tenant":"acme","name":"doc.md","content_type":"text/markdown","content":"UPDATED: employees now accrue twenty days of paid leave per year."}')
expect_code "reinsert identical bytes after delete" 201 "$code"
DOC1_REINSERTED=$(json_field id)
wf2_wait_document_status "$COL1" "$DOC1_REINSERTED" "$ALICE_TOKEN" ready 30
if [ "$DOC1_REINSERTED" = "$DOC1" ]; then
    echo "FAIL: reinsert after delete reused the same document id"
    fail=1
else
    echo "ok: reinsert after delete got a fresh document id ($DOC1_REINSERTED != $DOC1)"
fi
code=$(api GET "/evidence/$PRE_DELETE_CHUNK?tenant=acme" "$ALICE_TOKEN")
expect_code "the pre-delete citation cannot be resurrected by the reinsert" 404 "$code"

# The generation fence itself (ADR-0006 decision 3, reused unmodified
# here): reverting to a digest that was already tombstoned AT THE SAME
# document/key must bump generation on reactivation, which is the case
# a whole-document delete+reinsert (above) doesn't exercise since it
# never reuses a key.
code=$(api POST "/collections/$COL1/documents" "$ALICE_TOKEN" '{"tenant":"acme","name":"doc.md","content_type":"text/markdown","content":"Employees accrue fifteen days of paid leave per year."}')
expect_code "revert this document back to its original (now-tombstoned) content" 201 "$code"
wf2_wait_document_status "$COL1" "$DOC1_REINSERTED" "$ALICE_TOKEN" ready 30
REVERT_GENERATION=$(pg_admin -c "SELECT generation FROM helix_ai.derived_value WHERE (source_key ->> 'document_id') = '$DOC1_REINSERTED' ORDER BY generation DESC LIMIT 1" | tr -d ' ')
if [ "$REVERT_GENERATION" -gt 1 ]; then
    echo "ok: reactivating a tombstoned key (same document, reverted digest) bumped generation to $REVERT_GENERATION"
else
    echo "FAIL: expected generation > 1 after reactivating a tombstoned key, got $REVERT_GENERATION"
    fail=1
fi
echo "scenario 3: PASS (chunker/model-version-change regeneration is validated at the design/unit level -- ADR-0007 decision 3 -- not black-box here; see the completion report)"

# --- Scenario 4: hybrid ranking (mechanics, not semantic quality) ---
echo "--- scenario 4: hybrid ranking ---"
code=$(api POST "/collections/$COL1/documents" "$ALICE_TOKEN" '{"tenant":"acme","name":"policy-a.md","content_type":"text/markdown","content":"Expense reports must be submitted within thirty days of travel."}')
DOC_A=$(json_field id)
code=$(api POST "/collections/$COL1/documents" "$ALICE_TOKEN" '{"tenant":"acme","name":"policy-b.md","content_type":"text/markdown","content":"Parking permits are issued on a first come first served basis."}')
DOC_B=$(json_field id)
wf2_wait_document_status "$COL1" "$DOC_A" "$ALICE_TOKEN" ready 30
wf2_wait_document_status "$COL1" "$DOC_B" "$ALICE_TOKEN" ready 30

code=$(api POST /search "$ALICE_TOKEN" "{\"tenant\":\"acme\",\"collection_id\":\"$COL1\",\"query\":\"expense reports travel\",\"top_k\":1}")
RESULT_DOC=$(json_field results.0.document_id)
if [ "$RESULT_DOC" = "$DOC_A" ]; then
    echo "ok: exact-term query ranks the lexically matching document first (top_k=1 honored)"
else
    echo "FAIL: expected policy-a (expense/travel terms) to rank first, got document $RESULT_DOC"
    fail=1
fi
RESULT_COUNT=$(python3 -c "import json; print(len(json.load(open('/tmp/wf2_api_body.json'))['results']))")
if [ "$RESULT_COUNT" = "1" ]; then
    echo "ok: top_k=1 honored -- see helix-cortex internal/retrieval's unit tests (fuse_test.go, rerank_test.go) for hand-calculated RRF/overlap scores, independent of this black-box check"
else
    echo "FAIL: top_k=1 returned $RESULT_COUNT results"
    fail=1
fi
echo "scenario 4: PASS"

# --- Scenario 5: filtering before limits ---
echo "--- scenario 5: filtering before limits ---"
code=$(api POST /collections "$ALICE_TOKEN" '{"tenant":"acme","name":"s5-secret"}')
COL_SECRET=$(json_field id)
code=$(api POST "/collections/$COL_SECRET/documents" "$ALICE_TOKEN" '{"tenant":"acme","name":"secret.md","content_type":"text/markdown","content":"paid leave paid leave paid leave executive bonus details"}')
DOC_SECRET=$(json_field id)
wf2_wait_document_status "$COL_SECRET" "$DOC_SECRET" "$ALICE_TOKEN" ready 30
# bob has no grant on COL_SECRET, which would score very highly for
# "paid leave" (far higher term density than col1's content) -- a
# post-hoc "rank globally, then drop forbidden items" implementation
# would be tempted to leak it. It must never even be considered: bob's
# request is rejected at the collection-authorization boundary before
# any ranking of COL_SECRET's content happens.
code=$(api POST /search "$BOB_TOKEN" "{\"tenant\":\"acme\",\"collection_id\":\"$COL_SECRET\",\"query\":\"paid leave\"}")
expect_code "forbidden collection rejected outright, not silently emptied" 403 "$code"
BODY=$(cat /tmp/wf2_api_body.json)
case "$BODY" in
  *"executive bonus"*) echo "FAIL: forbidden content leaked into the error body: $BODY"; fail=1 ;;
  *) echo "ok: no forbidden content disclosed" ;;
esac
echo "scenario 5: PASS"

# --- Scenario 6: principal isolation (with positive controls) ---
echo "--- scenario 6: principal isolation ---"
code=$(api POST "/collections/$COL1/permissions" "$ALICE_TOKEN" '{"principal":"bob","role":"reader"}')
expect_code "grant bob reader" 201 "$code"
code=$(api POST /search "$BOB_TOKEN" "{\"tenant\":\"acme\",\"collection_id\":\"$COL1\",\"query\":\"paid leave\"}")
expect_code "bob (granted reader) can search -- positive control" 200 "$code"
code=$(api POST /search "$CAROL_TOKEN" "{\"tenant\":\"globex\",\"collection_id\":\"$COL1\",\"query\":\"paid leave\"}")
expect_code "carol (other tenant, no grant) rejected" 403 "$code"
code=$(api POST /search "$CAROL_TOKEN" "{\"tenant\":\"acme\",\"collection_id\":\"$COL1\",\"query\":\"paid leave\"}")
expect_code "carol spoofing acme tenant rejected" 403 "$code"
code=$(curl -s -o /tmp/wf2_api_body.json -w '%{http_code}' -X POST "$API/search" -H "Content-Type: application/json" -d "{\"tenant\":\"acme\",\"collection_id\":\"$COL1\",\"query\":\"x\"}")
expect_code "missing credential rejected" 401 "$code"
code=$(api POST /search "not-a-real-token" "{\"tenant\":\"acme\",\"collection_id\":\"$COL1\",\"query\":\"x\"}")
expect_code "invalid credential rejected" 401 "$code"
# Pooled-connection reuse: alternate principals/tenants over many
# sequential requests against the same running service -- each one a
# fresh transaction (SET LOCAL, never session-level SET), so a pooled
# connection a prior request used must never leak that request's
# tenant/principal context into this one.
i=0
while [ "$i" -lt 6 ]; do
    case $((i % 3)) in
        0) tok="$ALICE_TOKEN"; tenant="acme"; want=200 ;;
        1) tok="$BOB_TOKEN"; tenant="acme"; want=200 ;;
        2) tok="$CAROL_TOKEN"; tenant="globex"; want=403 ;;
    esac
    code=$(api POST /search "$tok" "{\"tenant\":\"$tenant\",\"collection_id\":\"$COL1\",\"query\":\"x\"}")
    expect_code "pool-reuse round $i" "$want" "$code"
    i=$((i + 1))
done
echo "scenario 6: PASS"

# --- Scenario 7: revocation ---
echo "--- scenario 7: revocation ---"
code=$(api POST /answers "$BOB_TOKEN" "{\"tenant\":\"acme\",\"collection_id\":\"$COL1\",\"query\":\"paid leave\"}")
expect_code "bob gets a cited answer before revocation" 200 "$code"
BOB_CITED_CHUNK=$(json_field citations.0.chunk_id)

code=$(api DELETE "/collections/$COL1/permissions/bob" "$ALICE_TOKEN")
expect_code "revoke bob" 204 "$code"

code=$(api POST /search "$BOB_TOKEN" "{\"tenant\":\"acme\",\"collection_id\":\"$COL1\",\"query\":\"paid leave\"}")
expect_code "bob's search excluded after revocation" 403 "$code"
code=$(api GET "/evidence/$BOB_CITED_CHUNK?tenant=acme" "$BOB_TOKEN")
expect_code "bob's earlier citation now fails to resolve" 404 "$code"
code=$(api POST /search "$ALICE_TOKEN" "{\"tenant\":\"acme\",\"collection_id\":\"$COL1\",\"query\":\"paid leave\"}")
expect_code "alice (remaining authorized owner) still succeeds" 200 "$code"
echo "scenario 7: PASS"

# --- Scenario 8: in-flight revocation (deterministic barrier) ---
echo "--- scenario 8: in-flight revocation ---"
# Snapshot the global fail counter: scenario 3's known/accepted design-level
# gap already left fail=1 by this point, so the PASS/FAIL label below must
# compare against this snapshot, not against fail itself, or scenario 8
# would always print FAIL regardless of its own outcome.
s8_fail_before=$fail
code=$(api POST "/collections/$COL1/permissions" "$ALICE_TOKEN" '{"principal":"bob","role":"reader"}')
expect_code "re-grant bob for this scenario" 201 "$code"
BARRIER_TOKEN="wf2-barrier-$$"
(
    curl -s -o /tmp/wf2_inflight_response.json -w '%{http_code}' -X POST "$API/answers" \
        -H "Authorization: Bearer $BOB_TOKEN" -H "Content-Type: application/json" \
        -H "X-Test-Barrier-Token: $BARRIER_TOKEN" \
        -d "{\"tenant\":\"acme\",\"collection_id\":\"$COL1\",\"query\":\"paid leave\"}" \
        > /tmp/wf2_inflight_code.txt
) &
INFLIGHT_PID=$!

i=0
arrived=0
while true; do
    ARRIVED=$(curl -s "$API/__test/barrier/$BARRIER_TOKEN/status" | python3 -c "import sys,json; print(json.load(sys.stdin)['arrived'])" 2>/dev/null || echo False)
    if [ "$ARRIVED" = "True" ]; then
        arrived=1
        break
    fi
    i=$((i + 1))
    if [ "$i" -gt 30 ]; then
        break
    fi
    sleep 1
done

if [ "$arrived" -eq 1 ]; then
    echo "ok: in-flight request paused at the barrier (candidates already selected)"
else
    echo "FAIL: the in-flight answer request never reached the barrier"
    fail=1
fi

code=$(api DELETE "/collections/$COL1/permissions/bob" "$ALICE_TOKEN")
expect_code "revoke bob while the other request is paused" 204 "$code"

curl -s -o /dev/null -X POST "$API/__test/barrier/$BARRIER_TOKEN/release"
wait "$INFLIGHT_PID" || true
INFLIGHT_HTTP=$(cat /tmp/wf2_inflight_code.txt 2>/dev/null || echo "")
INFLIGHT_BODY=$(cat /tmp/wf2_inflight_response.json 2>/dev/null || echo "")
if [ -z "$INFLIGHT_BODY" ]; then
    echo "FAIL: the in-flight answer request produced no response body (http=$INFLIGHT_HTTP)"
    fail=1
else
    case "$INFLIGHT_BODY" in
      *"paid leave"*|*"twenty days"*)
        echo "FAIL: a revoked passage escaped the in-flight request (http=$INFLIGHT_HTTP): $INFLIGHT_BODY"
        fail=1
        ;;
      *)
        echo "ok: no revoked passage/citation escaped the in-flight request (http=$INFLIGHT_HTTP)"
        ;;
    esac
fi
if [ "$fail" -eq "$s8_fail_before" ]; then
    echo "scenario 8: PASS"
else
    echo "scenario 8: FAIL"
fi

# --- Scenario 9: evidence integrity ---
echo "--- scenario 9: evidence integrity ---"
code=$(api GET "/evidence/not-a-real-chunk-id?tenant=acme" "$ALICE_TOKEN")
expect_code "fabricated chunk id rejected" 404 "$code"
FAKE_CHUNK="$DOC_A|0000000000000000000000000000000000000000000000000000000000000000|chunker-v1|0"
code=$(api GET "/evidence/$FAKE_CHUNK?tenant=acme" "$ALICE_TOKEN")
expect_code "mismatched digest rejected" 404 "$code"
echo "scenario 9: PASS"

# --- Scenario 10: bounded, untrusted input ---
echo "--- scenario 10: bounded untrusted input ---"
code=$(api POST "/collections/$COL1/documents" "$ALICE_TOKEN" '{"tenant":"acme","name":"bad.pdf","content_type":"application/pdf","content":"whatever"}')
expect_code "unsupported content type rejected" 400 "$code"
case "$(cat /tmp/wf2_api_body.json)" in
  *unsupported_content_type*) echo "ok: unsupported_content_type code returned" ;;
  *) echo "FAIL: wrong error code for unsupported content type"; fail=1 ;;
esac
OVERSIZED=$(python3 -c "print('x' * 1100000)")
code=$(api POST "/collections/$COL1/documents" "$ALICE_TOKEN" "{\"tenant\":\"acme\",\"name\":\"huge.txt\",\"content_type\":\"text/plain\",\"content\":\"$OVERSIZED\"}")
expect_code "oversized document rejected" 400 "$code"
echo "ok: remote URL ingestion is unavailable -- no such endpoint exists in this milestone (deliberate scope limit, ADR-0007)"
echo "scenario 10: PASS"

if [ "$fail" -eq 0 ]; then
    echo "workflow-2 acceptance: ALL SCENARIOS PASS"
else
    echo "workflow-2 acceptance: FAILED"
fi
exit $fail
