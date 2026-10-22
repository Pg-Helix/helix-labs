#!/bin/sh
# Workflow 1 end to end (RFC-0005 acceptance criteria): register a table,
# insert a row, let the real C1 worker claim/embed/publish it, verify the
# result, then clean up -- no demo data or containers left behind on
# success. Assumes this workspace's sibling-checkout layout (helix-labs
# next to helix-foundry/helix-kernel/helix-cortex, see
# helix-foundry/compose/docker-compose.yml).
#
# Separate credentials throughout, per RFC-0005: this script seeds/reads
# data as helix_app; it never uses helix_worker's or helix_owner's
# credential, and the worker container (brought up by Compose) never uses
# helix_app's. Runs psql inside the postgres container (via `docker
# compose exec`) rather than requiring a psql client on the host.
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
COMPOSE_DIR="$SCRIPT_DIR/../../helix-foundry/compose"
COMPOSE="docker compose -f $COMPOSE_DIR/docker-compose.yml"
APP_URL="postgres://helix_app:demo-app-only-not-for-prod@localhost/postgres?sslmode=disable"

app_psql() {
    $COMPOSE exec -T postgres psql "$APP_URL" -v ON_ERROR_STOP=1 "$@"
}

cleanup() {
    echo "workflow-1: cleaning up"
    $COMPOSE exec -T postgres psql "$APP_URL" -v ON_ERROR_STOP=0 -c "DROP TABLE IF EXISTS public.wf1_demo CASCADE;" >/dev/null 2>&1 || true
    $COMPOSE down -v >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "workflow-1: bringing up the local stack"
$COMPOSE up --build -d

echo "workflow-1: waiting for postgres"
i=0
until $COMPOSE ps postgres --format '{{.Health}}' 2>/dev/null | grep -q healthy; do
    i=$((i + 1))
    if [ "$i" -gt 60 ]; then
        echo "workflow-1: postgres never became healthy" >&2
        exit 1
    fi
    sleep 1
done

echo "workflow-1: registering a table as helix_app"
app_psql <<'EOF'
CREATE TABLE public.wf1_demo (id bigint PRIMARY KEY, body text);
SET helix.tenant = 'workflow1-demo';
SELECT helix_ai.register_source('public', 'wf1_demo', 'local-mock-embed', 'v1', 'pipeline-v1');
INSERT INTO public.wf1_demo VALUES (1, 'hello from workflow 1');
EOF

echo "workflow-1: waiting for the worker to publish a result"
i=0
while true; do
    status=$(app_psql -tAq -c "
        SET helix.tenant = 'workflow1-demo';
        SELECT ai_status FROM public.wf1_demo_ai WHERE id = 1;
    " | tail -1)
    if [ "$status" = "ready" ]; then
        break
    fi
    i=$((i + 1))
    if [ "$i" -gt 60 ]; then
        echo "workflow-1: worker never published a result (last status: $status)" >&2
        $COMPOSE logs worker || true
        exit 1
    fi
    sleep 1
done

value=$(app_psql -tAq -c "
    SET helix.tenant = 'workflow1-demo';
    SELECT ai_value FROM public.wf1_demo_ai WHERE id = 1;
")
echo "workflow-1: PASS -- ai_value = $value"
