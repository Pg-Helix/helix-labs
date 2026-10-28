#!/bin/sh
# Shared helpers for workflow-2/acceptance.sh (ADR-0007, helix-atlas).
# Sourced, not executed. Mirrors workflow-1/lib.sh's patterns (unique
# Compose project per run, generated gitignored credentials, diagnostics
# preserved before cleanup) for the ingestion/retrieval stack.
set -eu

WF2_SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
COMPOSE_DIR="$WF2_SCRIPT_DIR/../../helix-foundry/compose"
CORTEX_DIR="$WF2_SCRIPT_DIR/../../helix-cortex"

[ -f "$COMPOSE_DIR/.env" ] || "$COMPOSE_DIR/generate-env.sh"
. "$COMPOSE_DIR/.env"
[ -f "$COMPOSE_DIR/auth-fixture.json" ] || "$COMPOSE_DIR/generate-auth-fixture.sh"

WF2_PROJECT="helix-wf2-$$"
COMPOSE_PROJECT_NAME="$WF2_PROJECT"
export COMPOSE_PROJECT_NAME
COMPOSE="docker compose -p $WF2_PROJECT -f $COMPOSE_DIR/docker-compose.yml"

API="http://127.0.0.1:${HELIX_CORTEX_API_PORT:-55480}/v1"

# token_for PRINCIPAL: reads the opaque token the local auth fixture
# mapped to PRINCIPAL -- this suite never invents its own tokens, it
# discovers the real ones the running service was actually configured
# with (ADR-0007 decision 1), same as reading HELIX_*_PASSWORD from .env.
token_for() {
    principal=$1
    python3 -c "
import json, sys
d = json.load(open('$COMPOSE_DIR/auth-fixture.json'))
for token, info in d.items():
    if info['principal'] == '$principal':
        print(token); sys.exit(0)
sys.exit('no token for principal $principal')
"
}

ALICE_TOKEN=""
BOB_TOKEN=""
CAROL_TOKEN=""

wf2_load_tokens() {
    ALICE_TOKEN=$(token_for alice)
    BOB_TOKEN=$(token_for bob)
    CAROL_TOKEN=$(token_for carol)
}

# api METHOD PATH TOKEN [JSON_BODY]
api() {
    method=$1 path=$2 token=$3 body=${4:-}
    if [ -n "$body" ]; then
        curl -s -o /tmp/wf2_api_body.json -w '%{http_code}' -X "$method" "$API$path" \
            -H "Authorization: Bearer $token" -H "Content-Type: application/json" -d "$body"
    else
        curl -s -o /tmp/wf2_api_body.json -w '%{http_code}' -X "$method" "$API$path" \
            -H "Authorization: Bearer $token"
    fi
}

# api_json METHOD PATH TOKEN [BODY] [EXTRA_HEADER]: like api() but also
# supports one extra raw header line (used for the test-barrier header).
api_json() {
    method=$1 path=$2 token=$3 body=${4:-} extra=${5:-}
    if [ -n "$extra" ]; then
        curl -s -o /tmp/wf2_api_body.json -w '%{http_code}' -X "$method" "$API$path" \
            -H "Authorization: Bearer $token" -H "Content-Type: application/json" -H "$extra" ${body:+-d "$body"}
    else
        api "$method" "$path" "$token" "$body"
    fi
}

json_field() {
    python3 -c "
import json, sys
d = json.load(open('/tmp/wf2_api_body.json'))
v = d
for part in '$1'.split('.'):
    v = v[int(part)] if isinstance(v, list) else v[part]
print(v)
"
}

# pg_admin SQL...: a direct superuser connection, for setup/verification
# steps no HTTP endpoint exposes (pg_locks polling for the in-flight-
# revocation barrier's own sanity check, direct inspection of
# helix_ai.job for the retry-exhaustion scenario).
pg_admin() {
    $COMPOSE exec -T -e PGPASSWORD="$HELIX_ADMIN_PASSWORD" postgres psql -h 127.0.0.1 -U postgres -d postgres -v ON_ERROR_STOP=1 -tAq "$@"
}

pg_cortex_api() {
    $COMPOSE exec -T -e PGPASSWORD="$HELIX_CORTEX_API_PASSWORD" postgres psql -h 127.0.0.1 -U helix_cortex_api -d postgres -v ON_ERROR_STOP=1 "$@"
}

wf2_up() {
    echo "wf2: bringing up project $WF2_PROJECT"
    $COMPOSE up --build -d
    i=0
    until $COMPOSE ps postgres --format '{{.Health}}' 2>/dev/null | grep -q healthy; do
        i=$((i + 1))
        if [ "$i" -gt 60 ]; then
            echo "wf2: postgres never became healthy" >&2
            return 1
        fi
        sleep 1
    done
    # -f names a path INSIDE the container, which doesn't have this host
    # file -- pipe it over stdin instead (docker compose exec -T forwards
    # host stdin properly; psql reads commands from stdin by default).
    pg_cortex_api < "$CORTEX_DIR/migrations/001_cortex_schema.sql" > /tmp/wf2_migration.log 2>&1
    wf2_load_tokens
}

# wf2_wait_document_status COLLECTION DOCUMENT TOKEN EXPECTED TIMEOUT_SECONDS
wf2_wait_document_status() {
    col=$1 doc=$2 token=$3 expected=$4 timeout=${5:-30}
    i=0
    while true; do
        code=$(api GET "/collections/$col/documents/$doc" "$token")
        status=$(json_field status 2>/dev/null || echo "")
        if [ "$status" = "$expected" ]; then
            return 0
        fi
        i=$((i + 1))
        if [ "$i" -gt "$timeout" ]; then
            echo "wf2: timed out waiting for document $doc status=$expected (last saw: $status, http=$code)" >&2
            cat /tmp/wf2_api_body.json >&2
            return 1
        fi
        sleep 1
    done
}

wf2_cleanup() {
    status=$?
    if [ "$status" -ne 0 ]; then
        echo "wf2: FAILED (exit $status) -- service logs follow"
        $COMPOSE logs cortex-api chunk-worker 2>&1 | tail -150 || true
    fi
    echo "wf2: cleaning up project $WF2_PROJECT"
    $COMPOSE down -v >/dev/null 2>&1 || true
    exit "$status"
}
