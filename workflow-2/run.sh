#!/bin/sh
# Workflow 2 end to end, the short version (ADR-0007, helix-atlas): one
# authorized user ingests a document, searches it, gets a cited
# extractive answer, resolves the citation, then has access revoked and
# can no longer do any of that. For the full fail-closed regression
# gate (idempotency, concurrent duplicates, in-flight revocation via a
# deterministic barrier, evidence-integrity edge cases, etc.) see
# acceptance.sh -- this script is the readable demo, not the test suite.
set -eu
. "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/lib.sh"
trap wf2_cleanup EXIT INT TERM

wf2_up

echo "workflow-2: alice creates a collection and ingests a document"
code=$(api POST /collections "$ALICE_TOKEN" '{"tenant":"acme","name":"handbook"}')
[ "$code" = "201" ] || { echo "workflow-2: create collection failed (http $code)"; cat /tmp/wf2_api_body.json; exit 1; }
COL=$(json_field id)

code=$(api POST "/collections/$COL/documents" "$ALICE_TOKEN" '{"tenant":"acme","name":"policy.md","content_type":"text/markdown","content":"Employees accrue fifteen days of paid leave per year."}')
[ "$code" = "201" ] || { echo "workflow-2: ingest failed (http $code)"; cat /tmp/wf2_api_body.json; exit 1; }
DOC=$(json_field id)

echo "workflow-2: waiting for the chunk-worker to embed it"
wf2_wait_document_status "$COL" "$DOC" "$ALICE_TOKEN" ready 30

echo "workflow-2: searching"
code=$(api POST /search "$ALICE_TOKEN" "{\"tenant\":\"acme\",\"collection_id\":\"$COL\",\"query\":\"paid leave\"}")
[ "$code" = "200" ] || { echo "workflow-2: search failed (http $code)"; cat /tmp/wf2_api_body.json; exit 1; }
echo "workflow-2: top result snippet: $(json_field results.0.snippet)"

echo "workflow-2: asking for a cited answer"
code=$(api POST /answers "$ALICE_TOKEN" "{\"tenant\":\"acme\",\"collection_id\":\"$COL\",\"query\":\"paid leave\"}")
[ "$code" = "200" ] || { echo "workflow-2: answer failed (http $code)"; cat /tmp/wf2_api_body.json; exit 1; }
echo "workflow-2: answer: $(json_field answer)"
CHUNK=$(json_field citations.0.chunk_id)

echo "workflow-2: resolving the citation via the evidence endpoint"
code=$(api GET "/evidence/$CHUNK?tenant=acme" "$ALICE_TOKEN")
[ "$code" = "200" ] || { echo "workflow-2: evidence lookup failed (http $code)"; cat /tmp/wf2_api_body.json; exit 1; }
echo "workflow-2: cited text: $(json_field text)"

echo "workflow-2: granting bob read access, confirming he can search, then revoking it"
code=$(api POST "/collections/$COL/permissions" "$ALICE_TOKEN" '{"principal":"bob","role":"reader"}')
[ "$code" = "201" ] || { echo "workflow-2: grant failed (http $code)"; exit 1; }
code=$(api POST /search "$BOB_TOKEN" "{\"tenant\":\"acme\",\"collection_id\":\"$COL\",\"query\":\"paid leave\"}")
[ "$code" = "200" ] || { echo "workflow-2: bob's search before revocation should have succeeded (http $code)"; exit 1; }
echo "workflow-2: bob can search while granted (http $code)"

code=$(api DELETE "/collections/$COL/permissions/bob" "$ALICE_TOKEN")
[ "$code" = "204" ] || { echo "workflow-2: revoke failed (http $code)"; exit 1; }

code=$(api POST /search "$BOB_TOKEN" "{\"tenant\":\"acme\",\"collection_id\":\"$COL\",\"query\":\"paid leave\"}")
if [ "$code" = "403" ]; then
    echo "workflow-2: bob's search is rejected after revocation (http 403)"
else
    echo "workflow-2: FAIL -- bob could still search after revocation (http $code)" >&2
    exit 1
fi

code=$(api GET "/evidence/$CHUNK?tenant=acme" "$BOB_TOKEN")
if [ "$code" = "403" ] || [ "$code" = "404" ]; then
    echo "workflow-2: bob's evidence lookup is rejected after revocation (http $code)"
else
    echo "workflow-2: FAIL -- bob could still resolve the citation after revocation (http $code)" >&2
    exit 1
fi

echo "workflow-2: PASS -- alice (still authorized) retains access throughout"
code=$(api POST /search "$ALICE_TOKEN" "{\"tenant\":\"acme\",\"collection_id\":\"$COL\",\"query\":\"paid leave\"}")
[ "$code" = "200" ] || { echo "workflow-2: FAIL -- alice lost access she should still have (http $code)"; exit 1; }
