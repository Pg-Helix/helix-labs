#!/bin/sh
# Shared helpers for workflow-1/run.sh (the short smoke demo) and
# workflow-1/acceptance.sh (the full gate). Sourced, not executed.
set -eu

WF1_SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
COMPOSE_DIR="$WF1_SCRIPT_DIR/../../helix-foundry/compose"

[ -f "$COMPOSE_DIR/.env" ] || "$COMPOSE_DIR/generate-env.sh"
. "$COMPOSE_DIR/.env"

# Each acceptance run gets its own Compose project name, so cleanup only
# ever touches containers/networks/volumes THIS run created -- never an
# unrelated developer stack that happens to be up under the default
# project name.
WF1_PROJECT="helix-wf1-$$"
# Exported (not just passed via -p below) so any script THIS script
# invokes as a subprocess -- scenario 5 shells out to Foundry's own
# test-auth-boundary.sh -- targets the same uniquely-namespaced project
# automatically; Compose falls back to this env var whenever a command
# doesn't pass -p itself.
COMPOSE_PROJECT_NAME="$WF1_PROJECT"
export COMPOSE_PROJECT_NAME
COMPOSE="docker compose -p $WF1_PROJECT -f $COMPOSE_DIR/docker-compose.yml"

APP_URL="postgres://helix_app:${HELIX_APP_PASSWORD}@localhost/postgres?sslmode=disable"
WORKER_URL="postgres://helix_worker:${HELIX_WORKER_PASSWORD}@localhost/postgres?sslmode=disable"

app_psql() {
    $COMPOSE exec -T postgres psql "$APP_URL" -v ON_ERROR_STOP=1 "$@"
}

# worker_psql: a manually-driven session authenticated as the REAL
# helix_worker role (not the always-running cortex binary), used to get
# deterministic control over claim/publish timing for the scenarios
# below -- a genuine authenticated connection over the real network
# path, not a superuser standing in for one.
worker_psql() {
    $COMPOSE exec -T postgres psql "$WORKER_URL" -v ON_ERROR_STOP=1 "$@"
}

wf1_up() {
    echo "wf1: bringing up project $WF1_PROJECT"
    $COMPOSE up --build -d
    i=0
    until $COMPOSE ps postgres --format '{{.Health}}' 2>/dev/null | grep -q healthy; do
        i=$((i + 1))
        if [ "$i" -gt 60 ]; then
            echo "wf1: postgres never became healthy" >&2
            return 1
        fi
        sleep 1
    done
}

wf1_stop_worker() {
    $COMPOSE stop worker >/dev/null
}

wf1_start_worker() {
    $COMPOSE start worker >/dev/null
}

# wf1_wait_status TENANT TABLE ID EXPECTED_STATUS TIMEOUT_SECONDS
wf1_wait_status() {
    tenant=$1 table=$2 id=$3 expected=$4 timeout=${5:-30}
    i=0
    while true; do
        status=$(app_psql -tAq -c "
            SET helix.tenant = '$tenant';
            SELECT ai_status FROM public.${table}_ai WHERE id = $id;
        " | tail -1)
        if [ "$status" = "$expected" ]; then
            return 0
        fi
        i=$((i + 1))
        if [ "$i" -gt "$timeout" ]; then
            echo "wf1: timed out waiting for $table id=$id status=$expected (last saw: $status)" >&2
            return 1
        fi
        sleep 1
    done
}

# wf1_cleanup: preserves diagnostics (worker logs) BEFORE tearing down,
# so a failure is debuggable, then removes only this run's own project
# (-v drops its volumes/networks too) -- never an unrelated stack.
wf1_cleanup() {
    status=$?
    if [ "$status" -ne 0 ]; then
        echo "wf1: FAILED (exit $status) -- worker logs follow"
        $COMPOSE logs worker 2>&1 | tail -100 || true
    fi
    echo "wf1: cleaning up project $WF1_PROJECT"
    $COMPOSE down -v >/dev/null 2>&1 || true
    exit "$status"
}
