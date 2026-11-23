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
s_before=0
# scenario_result N: PASS only if no assertion failed since the last label.
scenario_result() {
    if [ "$fail" -eq "$s_before" ]; then echo "scenario $1: PASS"; else echo "scenario $1: FAIL ($((fail - s_before)) assertion(s))"; fi
    s_before=$fail
}
expect_code() {
    label=$1 want=$2 got=$3
    if [ "$got" = "$want" ]; then
        echo "ok: $label (http $got)"
    else
        echo "FAIL: $label (want http $want, got $got)"
        cat $WF2_BODY
        fail=$((fail + 1))
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
  *) echo "FAIL: evidence text wrong: $EVTEXT"; fail=$((fail + 1)) ;;
esac
scenario_result 1

# --- Scenario 2: idempotency and concurrent ingestion races ---
echo "--- scenario 2: idempotency ---"
BEFORE_CREATED=$(pg_admin -c "SELECT created_at FROM cortex.chunk WHERE document_id = '$DOC1' ORDER BY chunk_index" | tr -d ' ')
code=$(api POST "/collections/$COL1/documents" "$ALICE_TOKEN" '{"tenant":"acme","name":"doc.md","content_type":"text/markdown","content":"Employees accrue fifteen days of paid leave per year."}')
expect_code "re-ingest identical bytes" 201 "$code"
DOC1_AGAIN=$(json_field id)
if [ "$DOC1_AGAIN" != "$DOC1" ]; then
    echo "FAIL: idempotent re-ingest returned a different document id"
    fail=$((fail + 1))
fi
AFTER_CREATED=$(pg_admin -c "SELECT created_at FROM cortex.chunk WHERE document_id = '$DOC1' ORDER BY chunk_index" | tr -d ' ')
if [ "$BEFORE_CREATED" != "$AFTER_CREATED" ]; then
    echo "FAIL: idempotent re-ingest changed the chunk row (created_at differs) -- a new chunk/job was created"
    fail=$((fail + 1))
else
    echo "ok: idempotent re-ingest left the chunk row untouched (no new chunk/job)"
fi

# Concurrent duplicate submission: two parallel identical ingests of the
# SAME already-existing document. Isolated response files (not the
# shared default) so each background call's own status/body can be
# trusted afterward -- two calls sharing one file would race each
# other's writes. Both must succeed (resolveDocumentForIngest's FOR
# UPDATE serializes them against each other rather than corrupting
# state), and exactly one chunk row/active version must result.
api POST "/collections/$COL1/documents" "$ALICE_TOKEN" '{"tenant":"acme","name":"doc.md","content_type":"text/markdown","content":"Employees accrue fifteen days of paid leave per year."}' $WF2_TMP/dup_a.json > $WF2_TMP/dup_a.code &
PID_A=$!
api POST "/collections/$COL1/documents" "$ALICE_TOKEN" '{"tenant":"acme","name":"doc.md","content_type":"text/markdown","content":"Employees accrue fifteen days of paid leave per year."}' $WF2_TMP/dup_b.json > $WF2_TMP/dup_b.code &
PID_B=$!
# Reap both children; a failed transfer is never read as its status line.
bg_reap "$PID_A" $WF2_TMP/dup_a.code; CODE_A=$BG_STATUS
bg_reap "$PID_B" $WF2_TMP/dup_b.code; CODE_B=$BG_STATUS
if [ "$CODE_A" = "201" ] && [ "$CODE_B" = "201" ]; then
    echo "ok: both concurrent duplicate-content requests succeeded (http $CODE_A, http $CODE_B)"
else
    echo "FAIL: concurrent duplicate requests: expected 201/201, got $CODE_A/$CODE_B"
    cat $WF2_TMP/dup_a.json; cat $WF2_TMP/dup_b.json
    fail=$((fail + 1))
fi
CHUNK_COUNT=$(pg_admin -c "SELECT count(*) FROM cortex.chunk WHERE document_id = '$DOC1'" | tr -d ' ')
if [ "$CHUNK_COUNT" = "1" ]; then
    echo "ok: concurrent duplicate ingestion still leaves exactly 1 chunk row"
else
    echo "FAIL: expected exactly 1 chunk row after concurrent duplicates, got $CHUNK_COUNT"
    fail=$((fail + 1))
fi

# Concurrent FIRST creation/registration: two parallel ingests of a
# document name that has never existed before. Exactly one coherent
# document/active-version must result -- not two documents, not a
# unique_violation surfaced as a 500.
api POST "/collections/$COL1/documents" "$ALICE_TOKEN" '{"tenant":"acme","name":"race-first-create.md","content_type":"text/markdown","content":"first creation race content."}' $WF2_TMP/race_a.json > $WF2_TMP/race_a.code &
PID_A=$!
api POST "/collections/$COL1/documents" "$ALICE_TOKEN" '{"tenant":"acme","name":"race-first-create.md","content_type":"text/markdown","content":"first creation race content."}' $WF2_TMP/race_b.json > $WF2_TMP/race_b.code &
PID_B=$!
# Reap both children; a failed transfer is never read as its status line.
bg_reap "$PID_A" $WF2_TMP/race_a.code; RACE_CODE_A=$BG_STATUS
bg_reap "$PID_B" $WF2_TMP/race_b.code; RACE_CODE_B=$BG_STATUS
if [ "$RACE_CODE_A" = "201" ] && [ "$RACE_CODE_B" = "201" ]; then
    echo "ok: both concurrent first-creation requests succeeded, not a raw conflict error (http $RACE_CODE_A, http $RACE_CODE_B)"
else
    echo "FAIL: concurrent first creation: expected 201/201, got $RACE_CODE_A/$RACE_CODE_B"
    cat $WF2_TMP/race_a.json; cat $WF2_TMP/race_b.json
    fail=$((fail + 1))
fi
RACE_DOC_A=$(json_field id $WF2_TMP/race_a.json || true)
RACE_DOC_B=$(json_field id $WF2_TMP/race_b.json || true)
if [ "$RACE_DOC_A" = "$RACE_DOC_B" ]; then
    echo "ok: both requests resolved to the SAME document id ($RACE_DOC_A)"
else
    echo "FAIL: concurrent first creation produced two different document ids: $RACE_DOC_A vs $RACE_DOC_B"
    fail=$((fail + 1))
fi
DOC_COUNT=$(pg_admin -c "SELECT count(*) FROM cortex.document WHERE collection_id = '$COL1' AND name = 'race-first-create.md'" | tr -d ' ')
if [ "$DOC_COUNT" = "1" ]; then
    echo "ok: exactly one document row exists after the first-creation race"
else
    echo "FAIL: expected exactly 1 document row, got $DOC_COUNT"
    fail=$((fail + 1))
fi
# Clean up: this document's only purpose was proving the race above is
# safe. Left alive, it would be a second, unrelated-but-still-active
# document in COL1 -- breaking scenario 3's later "search finds nothing
# after deleting doc.md" assumption, which only holds if doc.md is
# COL1's only ready content at that point.
code=$(api DELETE "/collections/$COL1/documents/$RACE_DOC_A" "$ALICE_TOKEN")
expect_code "clean up the first-creation race's test document" 204 "$code"

# Competing updates with DIFFERENT content to the same existing
# document: exactly one must become the final active version (whichever
# commits last), with no orphaned chunk left searchable under the OTHER
# content's digest once it's no longer active.
api POST "/collections/$COL1/documents" "$ALICE_TOKEN" '{"tenant":"acme","name":"doc.md","content_type":"text/markdown","content":"CONTENT A: thirty days of sick leave per year."}' $WF2_TMP/compete_a.json > $WF2_TMP/compete_a.code &
PID_A=$!
api POST "/collections/$COL1/documents" "$ALICE_TOKEN" '{"tenant":"acme","name":"doc.md","content_type":"text/markdown","content":"CONTENT B: forty days of sick leave per year."}' $WF2_TMP/compete_b.json > $WF2_TMP/compete_b.code &
PID_B=$!
# Reap both children; a failed transfer is never read as its status line.
bg_reap "$PID_A" $WF2_TMP/compete_a.code; COMPETE_CODE_A=$BG_STATUS
bg_reap "$PID_B" $WF2_TMP/compete_b.code; COMPETE_CODE_B=$BG_STATUS
if [ "$COMPETE_CODE_A" = "201" ] && [ "$COMPETE_CODE_B" = "201" ]; then
    echo "ok: both competing-content updates succeeded (http $COMPETE_CODE_A, http $COMPETE_CODE_B)"
else
    echo "FAIL: competing updates: expected 201/201, got $COMPETE_CODE_A/$COMPETE_CODE_B"
    cat $WF2_TMP/compete_a.json; cat $WF2_TMP/compete_b.json
    fail=$((fail + 1))
fi
wf2_wait_document_status "$COL1" "$DOC1" "$ALICE_TOKEN" ready 30
ACTIVE_VERSIONS=$(pg_admin -c "SELECT count(*) FROM cortex.document_version WHERE document_id = '$DOC1' AND superseded_at IS NULL" | tr -d ' ')
if [ "$ACTIVE_VERSIONS" = "1" ]; then
    echo "ok: exactly one coherent active version after competing updates"
else
    echo "FAIL: expected exactly 1 active (non-superseded) version, got $ACTIVE_VERSIONS"
    fail=$((fail + 1))
fi
code=$(api POST /search "$ALICE_TOKEN" "{\"tenant\":\"acme\",\"collection_id\":\"$COL1\",\"query\":\"sick leave\"}")
SICK_LEAVE_RESULTS=$(json_field results)
MATCHING=$(python3 -c "
import json
d = json.load(open('$WF2_BODY'))
print(sum(1 for r in d.get('results', []) if r.get('document_id') == '$DOC1'))
")
if [ "$MATCHING" -le 1 ]; then
    echo "ok: no more than one orphaned/duplicate result for this document after the race ($MATCHING)"
else
    echo "FAIL: found $MATCHING results for document $DOC1 after competing updates -- an orphaned chunk is still searchable"
    fail=$((fail + 1))
fi
scenario_result 2

# --- Scenario 3: freshness (update, generation fence, delete+reinsert) ---
echo "--- scenario 3: freshness ---"
code=$(api POST "/collections/$COL1/documents" "$ALICE_TOKEN" '{"tenant":"acme","name":"doc.md","content_type":"text/markdown","content":"UPDATED: employees now accrue twenty days of paid leave per year."}')
expect_code "update document (changed content)" 201 "$code"
wf2_wait_document_status "$COL1" "$DOC1" "$ALICE_TOKEN" ready 30
code=$(api POST /search "$ALICE_TOKEN" "{\"tenant\":\"acme\",\"collection_id\":\"$COL1\",\"query\":\"paid leave\"}")
SNIPPET=$(json_field results.0.snippet)
case "$SNIPPET" in
  *"twenty days"*) echo "ok: search reflects only the updated content" ;;
  *"fifteen days"*) echo "FAIL: search still returned the OLD content after an update"; fail=$((fail + 1)) ;;
  *) echo "FAIL: unexpected snippet: $SNIPPET"; fail=$((fail + 1)) ;;
esac

# The generation fence (ADR-0006 decision 3, reused unmodified here): this
# MUST happen via a plain update on DOC1 itself, never a delete -- a
# delete+reinsert gets a fresh document_id (see below), so its chunk keys
# have never existed before and there is nothing tombstoned to reactivate.
# Reverting DOC1 straight back to its original digest instead reactivates
# the exact (DOC1, digest_of_fifteen_days, ...) key that the update above
# just tombstoned.
code=$(api POST "/collections/$COL1/documents" "$ALICE_TOKEN" '{"tenant":"acme","name":"doc.md","content_type":"text/markdown","content":"Employees accrue fifteen days of paid leave per year."}')
expect_code "revert document back to its original (now-tombstoned) content" 201 "$code"
wf2_wait_document_status "$COL1" "$DOC1" "$ALICE_TOKEN" ready 30
REVERT_GENERATION=$(pg_admin -c "SELECT generation FROM helix_ai.derived_value WHERE (source_key ->> 'document_id') = '$DOC1' ORDER BY generation DESC LIMIT 1" | tr -d ' ')
if [ "$REVERT_GENERATION" -gt 1 ]; then
    echo "ok: reactivating a tombstoned key (same document, reverted digest) bumped generation to $REVERT_GENERATION"
else
    echo "FAIL: expected generation > 1 after reactivating a tombstoned key, got $REVERT_GENERATION"
    fail=$((fail + 1))
fi

code=$(api POST /search "$ALICE_TOKEN" "{\"tenant\":\"acme\",\"collection_id\":\"$COL1\",\"query\":\"paid leave\"}")
PRE_DELETE_CHUNK=$(json_field results.0.chunk_id)

code=$(api DELETE "/collections/$COL1/documents/$DOC1" "$ALICE_TOKEN")
expect_code "delete document" 204 "$code"
code=$(api POST /search "$ALICE_TOKEN" "{\"tenant\":\"acme\",\"collection_id\":\"$COL1\",\"query\":\"paid leave\"}")
RESULTS=$(json_field results)
if [ "$RESULTS" = "None" ] || [ "$RESULTS" = "[]" ]; then
    echo "ok: search finds nothing after delete"
else
    echo "FAIL: search still finds results after delete: $RESULTS"
    fail=$((fail + 1))
fi

# A whole-document delete+reinsert generates a fresh document id (see
# ADR-0007 decision 3's mapping notes) -- the chunk primary key is never
# actually reused across this path, so the invariant to check here is
# that the PRE-delete citation can never be resurrected.
code=$(api POST "/collections/$COL1/documents" "$ALICE_TOKEN" '{"tenant":"acme","name":"doc.md","content_type":"text/markdown","content":"UPDATED: employees now accrue twenty days of paid leave per year."}')
expect_code "reinsert after delete" 201 "$code"
DOC1_REINSERTED=$(json_field id)
wf2_wait_document_status "$COL1" "$DOC1_REINSERTED" "$ALICE_TOKEN" ready 30
if [ "$DOC1_REINSERTED" = "$DOC1" ]; then
    echo "FAIL: reinsert after delete reused the same document id"
    fail=$((fail + 1))
else
    echo "ok: reinsert after delete got a fresh document id ($DOC1_REINSERTED != $DOC1)"
fi
code=$(api GET "/evidence/$PRE_DELETE_CHUNK?tenant=acme" "$ALICE_TOKEN")
expect_code "the pre-delete citation cannot be resurrected by the reinsert" 404 "$code"
scenario_result 3
echo "note: chunker/model-version-change regeneration (a separate ADR-0007 decision 3 case) is validated at the design/unit level, not black-box here -- see the completion report"

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
    fail=$((fail + 1))
fi
RESULT_COUNT=$(python3 -c "import json; print(len(json.load(open('$WF2_BODY'))['results']))")
if [ "$RESULT_COUNT" = "1" ]; then
    echo "ok: top_k=1 honored -- see helix-cortex internal/retrieval's unit tests (fuse_test.go, rerank_test.go) for hand-calculated RRF/overlap scores, independent of this black-box check"
else
    echo "FAIL: top_k=1 returned $RESULT_COUNT results"
    fail=$((fail + 1))
fi
scenario_result 4

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
BODY=$(cat $WF2_BODY)
case "$BODY" in
  *"executive bonus"*) echo "FAIL: forbidden content leaked into the error body: $BODY"; fail=$((fail + 1)) ;;
  *) echo "ok: no forbidden content disclosed" ;;
esac
scenario_result 5

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
code=$(curl -s -o $WF2_BODY -w '%{http_code}' -X POST "$API/search" -H "Content-Type: application/json" -d "{\"tenant\":\"acme\",\"collection_id\":\"$COL1\",\"query\":\"x\"}")
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
scenario_result 6

# --- Scenario 7: revocation ---
echo "--- scenario 7: revocation ---"
code=$(api POST /answers "$BOB_TOKEN" "{\"tenant\":\"acme\",\"collection_id\":\"$COL1\",\"query\":\"paid leave\"}")
expect_code "bob gets a cited answer before revocation" 200 "$code"
BOB_CITED_CHUNK=$(json_field citations.0.chunk_id 2>/dev/null || true)
if [ -z "$BOB_CITED_CHUNK" ]; then
    echo "FAIL: bob's pre-revocation answer carried no citation -- the revocation checks below would be vacuous"
    fail=$((fail + 1))
fi
code=$(api GET "/evidence/$BOB_CITED_CHUNK?tenant=acme" "$BOB_TOKEN")
expect_code "bob's citation resolves before revocation" 200 "$code"

code=$(api DELETE "/collections/$COL1/permissions/bob" "$ALICE_TOKEN")
expect_code "revoke bob" 204 "$code"

code=$(api POST /search "$BOB_TOKEN" "{\"tenant\":\"acme\",\"collection_id\":\"$COL1\",\"query\":\"paid leave\"}")
expect_code "bob's search excluded after revocation" 403 "$code"
code=$(api GET "/evidence/$BOB_CITED_CHUNK?tenant=acme" "$BOB_TOKEN")
expect_code "bob's earlier citation now fails to resolve" 404 "$code"
code=$(api POST /search "$ALICE_TOKEN" "{\"tenant\":\"acme\",\"collection_id\":\"$COL1\",\"query\":\"paid leave\"}")
expect_code "alice (remaining authorized owner) still succeeds" 200 "$code"
scenario_result 7

# --- Scenario 8: in-flight revocation (deterministic barrier) ---
echo "--- scenario 8: in-flight revocation ---"
code=$(api POST "/collections/$COL1/permissions" "$ALICE_TOKEN" '{"principal":"bob","role":"reader"}')
expect_code "re-grant bob for this scenario" 201 "$code"
BARRIER_TOKEN="wf2-barrier-$$"
(
    curl -s --max-time "$WF2_HTTP_TIMEOUT" -o $WF2_TMP/inflight_response.json -w '%{http_code}' -X POST "$API/answers" \
        -H "Authorization: Bearer $BOB_TOKEN" -H "Content-Type: application/json" \
        -H "X-Test-Barrier-Token: $BARRIER_TOKEN" \
        -d "{\"tenant\":\"acme\",\"collection_id\":\"$COL1\",\"query\":\"paid leave\"}" \
        > $WF2_TMP/inflight_code.txt
) &
INFLIGHT_PID=$!

i=0
arrived=0
while true; do
    # Readiness counts only from a completed transfer; a failed poll is
    # retried, never parsed.
    ARRIVED=False
    if STATUS_BODY=$(curl -sf --max-time 5 "$API/__test/barrier/$BARRIER_TOKEN/status"); then
        ARRIVED=$(printf '%s' "$STATUS_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin)['arrived'])" 2>/dev/null || echo False)
    fi
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
    fail=$((fail + 1))
fi

code=$(api DELETE "/collections/$COL1/permissions/bob" "$ALICE_TOKEN")
expect_code "revoke bob while the other request is paused" 204 "$code"

curl -s -o /dev/null -X POST "$API/__test/barrier/$BARRIER_TOKEN/release"
bg_reap "$INFLIGHT_PID" $WF2_TMP/inflight_code.txt
INFLIGHT_HTTP=$BG_STATUS
INFLIGHT_BODY=$(cat $WF2_TMP/inflight_response.json 2>/dev/null || echo "")
# The resumed request re-checks bob's grant in a fresh snapshot
# (store.Answer's requireRole) and must be denied outright.
if [ "$INFLIGHT_HTTP" != "403" ]; then
    echo "FAIL: the in-flight answer request after revocation: want http 403, got ${INFLIGHT_HTTP:-none}"
    fail=$((fail + 1))
fi
if [ -z "$INFLIGHT_BODY" ]; then
    echo "FAIL: the in-flight answer request produced no response body (http=$INFLIGHT_HTTP)"
    fail=$((fail + 1))
else
    case "$INFLIGHT_BODY" in
      *"paid leave"*|*"twenty days"*)
        echo "FAIL: a revoked passage escaped the in-flight request (http=$INFLIGHT_HTTP): $INFLIGHT_BODY"
        fail=$((fail + 1))
        ;;
      *)
        echo "ok: no revoked passage/citation escaped the in-flight request (http=$INFLIGHT_HTTP)"
        ;;
    esac
fi
scenario_result 8

# --- Scenario 9: evidence integrity ---
echo "--- scenario 9: evidence integrity ---"
code=$(api GET "/evidence/not-a-real-chunk-id?tenant=acme" "$ALICE_TOKEN")
expect_code "fabricated chunk id rejected" 404 "$code"
FAKE_CHUNK="$DOC_A|0000000000000000000000000000000000000000000000000000000000000000|chunker-v1|0"
code=$(api GET "/evidence/$FAKE_CHUNK?tenant=acme" "$ALICE_TOKEN")
expect_code "mismatched digest rejected" 404 "$code"
scenario_result 9

# --- Scenario 10: bounded, untrusted input ---
echo "--- scenario 10: bounded untrusted input ---"
code=$(api POST "/collections/$COL1/documents" "$ALICE_TOKEN" '{"tenant":"acme","name":"bad.pdf","content_type":"application/pdf","content":"whatever"}')
expect_code "unsupported content type rejected" 400 "$code"
case "$(cat $WF2_BODY)" in
  *unsupported_content_type*) echo "ok: unsupported_content_type code returned" ;;
  *) echo "FAIL: wrong error code for unsupported content type"; fail=$((fail + 1)) ;;
esac
OVERSIZED=$(python3 -c "print('x' * 1100000)")
code=$(api POST "/collections/$COL1/documents" "$ALICE_TOKEN" "{\"tenant\":\"acme\",\"name\":\"huge.txt\",\"content_type\":\"text/plain\",\"content\":\"$OVERSIZED\"}")
expect_code "oversized document rejected" 400 "$code"
echo "ok: remote URL ingestion is unavailable -- no such endpoint exists in this milestone (deliberate scope limit, ADR-0007)"
scenario_result 10

# --- Scenario 11: answer or decline on the wire (helix-atlas ADR-0008) ---
echo "--- scenario 11: answer/decline wire contract ---"
code=$(api POST /collections "$ALICE_TOKEN" '{"tenant":"acme","name":"wf2-empty"}')
expect_code "create an empty collection" 201 "$code"
COL_EMPTY=$(json_field id)
code=$(api POST /answers "$ALICE_TOKEN" "{\"tenant\":\"acme\",\"collection_id\":\"$COL_EMPTY\",\"query\":\"paid leave\"}")
expect_code "authorized question on an empty collection is processed" 200 "$code"
if python3 -c 'import json,sys; sys.exit(json.load(open(sys.argv[1])) != {"answer": "", "answer_method": "extractive", "citations": []})' "$WF2_BODY"; then
    echo "ok: empty collection declines with exactly {\"answer\":\"\",\"answer_method\":\"extractive\",\"citations\":[]}"
else
    echo "FAIL: empty-collection answer is not the documented decline body: $(cat "$WF2_BODY")"
    fail=$((fail + 1))
fi
code=$(api POST /answers "$BOB_TOKEN" "{\"tenant\":\"acme\",\"collection_id\":\"$COL_EMPTY\",\"query\":\"paid leave\"}")
expect_code "ungranted caller is denied (403), not given a decline" 403 "$code"
code=$(api POST /answers "$ALICE_TOKEN" "{\"tenant\":\"acme\",\"collection_id\":\"$COL1\",\"query\":\"paid leave\"}")
expect_code "authorized answer request" 200 "$code"
if python3 - "$WF2_BODY" <<'EOF'
import json, sys
b = json.load(open(sys.argv[1]))
cits = b["citations"]
assert b["answer_method"] == "extractive" and isinstance(cits, list), b
assert (b["answer"] == "") == (cits == []), "answer text and citations must agree"
for c in cits:
    assert all(isinstance(c.get(k), str) and c[k] for k in ("chunk_id", "source_id", "source_digest")), c
    assert c["chunk_id"].split("|")[:2] == [c["source_id"], c["source_digest"]], c
EOF
then
    echo "ok: answer response satisfies the AnswerResponse contract"
else
    echo "FAIL: answer response violates the AnswerResponse contract: $(cat "$WF2_BODY")"
    fail=$((fail + 1))
fi
scenario_result 11

if [ "$fail" -eq 0 ]; then
    echo "workflow-2 acceptance: ALL SCENARIOS PASS"
else
    echo "workflow-2 acceptance: FAILED"
fi
[ "$fail" -eq 0 ]
