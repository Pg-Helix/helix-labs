#!/bin/sh
# Proves the embedding-binding version-change properties (this
# milestone, Section 4) against the real running stack: backfill is
# idempotent/resumable, two bindings for the SAME chunk key are fully
# isolated (never cross-compared, never silently mixed), a dimension
# change triggers its own independent regeneration, and a cutover's
# "not yet backfilled" gap is a clean, temporary, documented 404 --
# never wrong content, never a crash.
#
# Uses two MOCK bindings (same model_id, different revision/dims) via
# `docker compose run --rm` one-off invocations -- deterministic and
# fast, exactly the "controllable provider for precise failure
# assertions" the task calls for; the real nomic-embed-text/Ollama
# profile is proven functionally end to end separately (run.sh,
# acceptance.sh, eval/run-eval.sh with WF2_REAL_EMBEDDINGS=1).
set -eu
WF2_SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$WF2_SCRIPT_DIR/lib.sh"

trap wf2_cleanup EXIT INT TERM
wf2_up

fail=0
echo "--- ingest under binding v1 (local-mock-embed:chunk-v1, dims=8) ---"
code=$(api POST /collections "$ALICE_TOKEN" '{"tenant":"acme","name":"vc-col"}')
[ "$code" = "201" ] || { echo "FAIL: create collection ($code)"; exit 1; }
COL=$(json_field id)
code=$(api POST "/collections/$COL/documents" "$ALICE_TOKEN" '{"tenant":"acme","name":"doc.md","content_type":"text/markdown","content":"Employees accrue fifteen days of paid leave per year."}')
[ "$code" = "201" ] || { echo "FAIL: ingest ($code)"; exit 1; }
DOC=$(json_field id)
wf2_wait_document_status "$COL" "$DOC" "$ALICE_TOKEN" ready 30
code=$(api POST /search "$ALICE_TOKEN" "{\"tenant\":\"acme\",\"collection_id\":\"$COL\",\"query\":\"paid leave\"}")
[ "$code" = "200" ] || { echo "FAIL: search under v1 ($code)"; exit 1; }
CHUNK=$(json_field results.0.chunk_id)
code=$(api GET "/evidence/$CHUNK?tenant=acme" "$ALICE_TOKEN")
[ "$code" = "200" ] || { echo "FAIL: evidence under v1 ($code)"; exit 1; }
echo "ok: ingested and searchable under v1, chunk=$CHUNK"

V1_SOURCE=$(pg_admin -c "SELECT id FROM helix_ai.source WHERE schema_name='cortex' AND table_name='chunk' AND model_revision='chunk-v1'" | tr -d ' ')
V1_STATUS_BEFORE=$(pg_admin -c "SELECT status FROM helix_ai.derived_value WHERE source_id = $V1_SOURCE AND (source_key ->> 'document_id') = '$DOC'" | tr -d ' ')
V1_GEN_BEFORE=$(pg_admin -c "SELECT generation FROM helix_ai.derived_value WHERE source_id = $V1_SOURCE AND (source_key ->> 'document_id') = '$DOC'" | tr -d ' ')

echo "--- register + backfill binding v2 (same model_id, new revision, EQUAL dims) ---"
SCHEDULED_1=$($COMPOSE run --rm -e CORTEX_CHUNK_MODEL_REVISION=chunk-v2 -e CORTEX_BACKFILL_TENANT=acme cortex-api 2>&1 | grep -o 'scheduled [0-9]* chunk' | grep -o '[0-9]*' | tail -1 || true)
echo "first backfill pass scheduled: ${SCHEDULED_1:-0}"
[ "${SCHEDULED_1:-0}" -ge 1 ] || { echo "FAIL: expected the first v2 backfill pass to schedule at least 1 chunk"; fail=1; }

echo "--- re-run backfill: must be idempotent/resumable (schedules 0 this time) ---"
SCHEDULED_2_OUTPUT=$($COMPOSE run --rm -e CORTEX_CHUNK_MODEL_REVISION=chunk-v2 -e CORTEX_BACKFILL_TENANT=acme cortex-api 2>&1)
if echo "$SCHEDULED_2_OUTPUT" | grep -q "backfill: done -- 0 chunk"; then
    echo "ok: second backfill pass scheduled 0 chunks (already done, idempotent)"
else
    echo "FAIL: second backfill pass was not a no-op"
    echo "$SCHEDULED_2_OUTPUT"
    fail=1
fi

V1_STATUS_AFTER=$(pg_admin -c "SELECT status FROM helix_ai.derived_value WHERE source_id = $V1_SOURCE AND (source_key ->> 'document_id') = '$DOC'" | tr -d ' ')
V1_GEN_AFTER=$(pg_admin -c "SELECT generation FROM helix_ai.derived_value WHERE source_id = $V1_SOURCE AND (source_key ->> 'document_id') = '$DOC'" | tr -d ' ')
if [ "$V1_STATUS_BEFORE" = "$V1_STATUS_AFTER" ] && [ "$V1_GEN_BEFORE" = "$V1_GEN_AFTER" ]; then
    echo "ok: v1's own row is untouched by v2's registration/backfill (status=$V1_STATUS_AFTER, generation=$V1_GEN_AFTER)"
else
    echo "FAIL: v1's row changed after v2 backfill (before: $V1_STATUS_BEFORE/$V1_GEN_BEFORE, after: $V1_STATUS_AFTER/$V1_GEN_AFTER)"
    fail=1
fi

echo "--- cut over the ACTIVE binding to v2 (restart cortex-api with v2 config) ---"
cat > /tmp/wf2_vc_override.yml <<EOF
services:
  cortex-api:
    environment:
      CORTEX_CHUNK_MODEL_REVISION: chunk-v2
EOF
$COMPOSE -f /tmp/wf2_vc_override.yml up -d --force-recreate cortex-api
i=0
until curl -s -o /dev/null -w '%{http_code}' "$API/collections" | grep -qv '000'; do
    i=$((i + 1)); [ "$i" -gt 30 ] && { echo "FAIL: cortex-api never came back up after cutover"; exit 1; }
    sleep 1
done

echo "--- evidence for the pre-cutover chunk, BEFORE v2 has finished embedding: expect a clean 404, not an error ---"
code=$(api GET "/evidence/$CHUNK?tenant=acme" "$ALICE_TOKEN")
if [ "$code" = "404" ]; then
    echo "ok: cutover with not-yet-backfilled new binding cleanly hides the result (http 404), not a crash or wrong content"
else
    echo "FAIL: expected 404 for the not-yet-ready new binding, got $code"
    cat /tmp/wf2_api_body.json
    fail=1
fi

echo "--- run the v2 worker to actually process the backfilled job ---"
timeout 30 $COMPOSE run --rm -e CORTEX_MODEL_REVISION=chunk-v2 chunk-worker >/tmp/wf2_vc_worker.log 2>&1 &
WORKER_PID=$!
i=0
while true; do
    code=$(api GET "/collections/$COL/documents/$DOC" "$ALICE_TOKEN")
    STATUS=$(json_field status 2>/dev/null || echo "")
    [ "$STATUS" = "ready" ] && break
    i=$((i + 1))
    if [ "$i" -gt 30 ]; then
        echo "FAIL: document never became ready under v2 (last status=$STATUS, http=$code)" >&2
        cat /tmp/wf2_vc_worker.log >&2
        kill "$WORKER_PID" 2>/dev/null || true
        wait "$WORKER_PID" 2>/dev/null || true
        exit 1
    fi
    sleep 1
done
kill "$WORKER_PID" 2>/dev/null || true
wait "$WORKER_PID" 2>/dev/null || true

echo "--- evidence for the SAME chunk, now that v2 is ready: expect the SAME correct text ---"
code=$(api GET "/evidence/$CHUNK?tenant=acme" "$ALICE_TOKEN")
expect_ok=0
if [ "$code" = "200" ]; then
    TEXT=$(json_field text)
    case "$TEXT" in
        *"fifteen days"*) echo "ok: evidence resolves correctly under the now-active v2 binding"; expect_ok=1 ;;
        *) echo "FAIL: wrong text after cutover: $TEXT" ;;
    esac
else
    echo "FAIL: expected 200 after v2 backfill completed, got $code"
fi
[ "$expect_ok" = "1" ] || fail=1

V1_STATUS_FINAL=$(pg_admin -c "SELECT status FROM helix_ai.derived_value WHERE source_id = $V1_SOURCE AND (source_key ->> 'document_id') = '$DOC'" | tr -d ' ')
if [ "$V1_STATUS_FINAL" = "$V1_STATUS_BEFORE" ]; then
    echo "ok: v1's row is STILL untouched after the full v2 cutover (status=$V1_STATUS_FINAL) -- complete isolation between bindings"
else
    echo "FAIL: v1's row changed during the cutover (was $V1_STATUS_BEFORE, now $V1_STATUS_FINAL)"
    fail=1
fi

echo "--- dimension-change trigger: register + backfill v3 (new revision, DIFFERENT dims) ---"
SCHEDULED_V3=$($COMPOSE run --rm -e CORTEX_CHUNK_MODEL_REVISION=chunk-v3 -e CORTEX_CHUNK_MODEL_DIMS=16 -e CORTEX_BACKFILL_TENANT=acme cortex-api 2>&1 | grep -o 'scheduled [0-9]* chunk' | grep -o '[0-9]*' | tail -1 || true)
if [ "${SCHEDULED_V3:-0}" -ge 1 ]; then
    echo "ok: a dimension change (8 -> 16, same model_id/revision-pattern) registers as its own distinct binding and schedules backfill (${SCHEDULED_V3} chunk(s))"
else
    echo "FAIL: expected the dims-change binding to schedule at least 1 chunk"
    fail=1
fi
V3_SOURCE=$(pg_admin -c "SELECT id FROM helix_ai.source WHERE schema_name='cortex' AND table_name='chunk' AND model_revision='chunk-v3'" | tr -d ' ')
if [ "$V3_SOURCE" != "$V1_SOURCE" ]; then
    echo "ok: the dims-change binding got its own distinct source_id ($V3_SOURCE, vs v1's $V1_SOURCE)"
else
    echo "FAIL: the dims-change binding reused v1's source_id"
    fail=1
fi

if [ "$fail" -eq 0 ]; then
    echo "version-change: ALL CHECKS PASS"
else
    echo "version-change: FAILED"
fi
exit "$fail"
