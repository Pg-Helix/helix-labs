#!/bin/sh
# Workflow-1 acceptance gate: the five scenarios RFC-0005/ADR-0006 commit
# to, run against the real Foundry stack and the real cortex worker
# binary (not pg_regress's superuser simulation). Uses its own uniquely
# namespaced Compose project (lib.sh) so cleanup never touches an
# unrelated developer stack, and preserves worker logs before tearing
# down on failure.
#
# Scenarios 2-4 stop the real worker to get deterministic control, then
# drive claim/get_job_input/publish_result manually over a connection
# genuinely authenticated as helix_worker (not a superuser standing in
# for one) -- the same credential the real worker service uses, just
# driven by hand for this one window. Scenario 4's lease expiry is a
# real bounded wait (sleep past a short lease), not a cheated deadline
# UPDATE -- this script has no admin credential to cheat with anyway.
set -eu
. "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/lib.sh"
trap wf1_cleanup EXIT

claim_field() {
    # $1 = claim tuple string (psql -F'|' output), $2 = 1-based field index
    echo "$1" | cut -d'|' -f"$2"
}

claim_one() {
    # Claims one job for local-mock-embed:v1 as the real helix_worker
    # role and prints job_id|lease_epoch|source_digest|target_model|pipeline_version.
    model="$1" lease_seconds="${2:-30}" max_attempts="${3:-5}"
    worker_psql -tAq -F'|' -c "
        SELECT (j).job_id, (j).lease_epoch, (j).source_digest, (j).target_model, (j).pipeline_version
        FROM (SELECT helix_ai.claim_job('$model', $lease_seconds, $max_attempts) AS j) s
    "
}

scenario_1() {
    echo "--- scenario 1: authorized registration and initial publication ---"
    app_psql <<'EOF'
CREATE TABLE public.wf1_s1 (id bigint PRIMARY KEY, body text);
SET helix.tenant = 'wf1-s1-tenant';
SELECT helix_ai.register_source('public', 'wf1_s1', 'local-mock-embed', 'v1', 'pipeline-v1');
INSERT INTO public.wf1_s1 VALUES (1, 'alpha');
EOF
    wf1_wait_status wf1-s1-tenant wf1_s1 1 ready 30
    echo "scenario 1: PASS"
}

scenario_2() {
    echo "--- scenario 2: update in flight -- old publish rejected, current result ready ---"
    wf1_stop_worker
    app_psql <<'EOF'
CREATE TABLE public.wf1_s2 (id bigint PRIMARY KEY, body text);
SET helix.tenant = 'wf1-s2-tenant';
SELECT helix_ai.register_source('public', 'wf1_s2', 'local-mock-embed', 'v1', 'pipeline-v1');
INSERT INTO public.wf1_s2 VALUES (1, 'original');
EOF
    claim=$(claim_one local-mock-embed:v1)
    job_id=$(claim_field "$claim" 1); epoch=$(claim_field "$claim" 2)
    digest=$(claim_field "$claim" 3); model=$(claim_field "$claim" 4); pipeline=$(claim_field "$claim" 5)

    app_psql -c "SET helix.tenant = 'wf1-s2-tenant'; UPDATE public.wf1_s2 SET body = 'updated' WHERE id = 1;"

    outcome=$(worker_psql -tAq -c "SELECT helix_ai.publish_result('$job_id', $epoch, '$digest', '$model', '$pipeline', '\"stale embedding\"');")
    [ "$outcome" = "stale_source" ] || { echo "scenario 2: FAIL -- expected stale_source, got $outcome" >&2; return 1; }

    wf1_start_worker
    wf1_wait_status wf1-s2-tenant wf1_s2 1 ready 30
    echo "scenario 2: PASS"
}

scenario_3() {
    echo "--- scenario 3: delete+identical-reinsert and primary-key change ---"
    wf1_stop_worker
    app_psql <<'EOF'
CREATE TABLE public.wf1_s3 (id bigint PRIMARY KEY, body text);
SET helix.tenant = 'wf1-s3-tenant';
SELECT helix_ai.register_source('public', 'wf1_s3', 'local-mock-embed', 'v1', 'pipeline-v1');
INSERT INTO public.wf1_s3 VALUES (1, 'gamma');
EOF
    claim=$(claim_one local-mock-embed:v1)
    job_id=$(claim_field "$claim" 1); epoch=$(claim_field "$claim" 2)

    app_psql -c "SET helix.tenant = 'wf1-s3-tenant'; DELETE FROM public.wf1_s3 WHERE id = 1; INSERT INTO public.wf1_s3 VALUES (1, 'gamma');"

    if worker_psql -c "SELECT helix_ai.get_job_input('$job_id', $epoch);" >/tmp/wf1_s3.log 2>&1; then
        echo "scenario 3: FAIL -- obsolete lease's get_job_input should have been rejected" >&2
        cat /tmp/wf1_s3.log
        return 1
    fi
    echo "scenario 3: ok -- obsolete lease rejected after delete+reinsert"

    wf1_start_worker
    wf1_wait_status wf1-s3-tenant wf1_s3 1 ready 30
    echo "scenario 3: PASS (delete+reinsert)"

    wf1_stop_worker
    app_psql -c "SET helix.tenant = 'wf1-s3-tenant'; UPDATE public.wf1_s3 SET id = 2 WHERE id = 1;"
    wf1_start_worker
    wf1_wait_status wf1-s3-tenant wf1_s3 2 ready 30

    old_status=$(app_psql -tAq -c "SET helix.tenant = 'wf1-s3-tenant'; SELECT status FROM helix_ai.status WHERE source_key = jsonb_build_object('id','1');" | tail -1)
    [ "$old_status" = "deleted" ] || { echo "scenario 3: FAIL -- old key should be tombstoned, got status=$old_status" >&2; return 1; }
    echo "scenario 3: PASS (primary-key change)"
}

scenario_4() {
    echo "--- scenario 4: crash/re-lease, duplicate delivery, retry exhaustion ---"
    wf1_stop_worker
    app_psql <<'EOF'
CREATE TABLE public.wf1_s4 (id bigint PRIMARY KEY, body text);
SET helix.tenant = 'wf1-s4-tenant';
SELECT helix_ai.register_source('public', 'wf1_s4', 'local-mock-embed', 'v1', 'pipeline-v1');
INSERT INTO public.wf1_s4 VALUES (1, 'delta');
EOF
    # Duplicate delivery: a second job for the same row before either is claimed.
    app_psql -c "
        SET helix.tenant = 'wf1-s4-tenant';
        SELECT helix_ai.request_refresh(
            (SELECT source_id FROM helix_ai.status WHERE source_key = jsonb_build_object('id','1') LIMIT 1),
            jsonb_build_object('id','1')
        );
    " >/dev/null

    claim_a=$(claim_one local-mock-embed:v1 2 1)
    job_a_id=$(claim_field "$claim_a" 1); job_a_epoch=$(claim_field "$claim_a" 2)
    job_a_digest=$(claim_field "$claim_a" 3); job_a_model=$(claim_field "$claim_a" 4); job_a_pipeline=$(claim_field "$claim_a" 5)

    claim_b=$(claim_one local-mock-embed:v1 2 1)
    job_b_id=$(claim_field "$claim_b" 1); job_b_epoch=$(claim_field "$claim_b" 2)
    job_b_digest=$(claim_field "$claim_b" 3); job_b_model=$(claim_field "$claim_b" 4); job_b_pipeline=$(claim_field "$claim_b" 5)

    outcome_b=$(worker_psql -tAq -c "SELECT helix_ai.publish_result('$job_b_id', $job_b_epoch, '$job_b_digest', '$job_b_model', '$job_b_pipeline', '\"delta embedding\"');")
    [ "$outcome_b" = "ok" ] || { echo "scenario 4: FAIL -- job B expected ok, got $outcome_b" >&2; return 1; }

    # Job A "crashed": never published. Real elapsed wait past its 2s
    # lease (not a cheated deadline UPDATE), then a genuine re-claim
    # attempt pushes attempts past max_attempts=1, dead-lettering it.
    sleep 3
    exhausted=$(worker_psql -tAq -c "SELECT (helix_ai.claim_job('local-mock-embed:v1', 2, 1)) IS NULL;")
    [ "$exhausted" = "t" ] || { echo "scenario 4: FAIL -- expected job A's re-claim to exhaust/dead-letter" >&2; return 1; }

    status_after=$(app_psql -tAq -c "SET helix.tenant='wf1-s4-tenant'; SELECT ai_status FROM public.wf1_s4_ai WHERE id=1;" | tail -1)
    [ "$status_after" = "ready" ] || { echo "scenario 4: FAIL -- dead-lettering obsolete job A corrupted job B's ready result (status=$status_after)" >&2; return 1; }

    outcome_a=$(worker_psql -tAq -c "SELECT helix_ai.publish_result('$job_a_id', $job_a_epoch, '$job_a_digest', '$job_a_model', '$job_a_pipeline', '\"forged\"');")
    [ "$outcome_a" = "dead_lettered" ] || { echo "scenario 4: FAIL -- expected job A's late publish dead_lettered, got $outcome_a" >&2; return 1; }

    wf1_start_worker
    echo "scenario 4: PASS"
}

scenario_5() {
    echo "--- scenario 5: unauthorized ops rejected, permitted ops as positive controls ---"
    sh "$COMPOSE_DIR/test-auth-boundary.sh"

    app_psql <<'EOF'
CREATE TABLE public.wf1_s5 (id bigint PRIMARY KEY, body text);
SET helix.tenant = 'wf1-s5-tenant-a';
SELECT helix_ai.register_source('public', 'wf1_s5', 'local-mock-embed', 'v1', 'pipeline-v1');
EOF
    cross=$(app_psql -tAq -c "SET helix.tenant = 'wf1-s5-tenant-b'; SELECT count(*) FROM helix_ai.source WHERE table_name = 'wf1_s5';" | tail -1)
    [ "$cross" = "0" ] || { echo "scenario 5: FAIL -- tenant B saw tenant A's registration ($cross rows)" >&2; return 1; }

    own=$(app_psql -tAq -c "SET helix.tenant = 'wf1-s5-tenant-a'; SELECT count(*) FROM helix_ai.source WHERE table_name = 'wf1_s5';" | tail -1)
    [ "$own" = "1" ] || { echo "scenario 5: FAIL -- tenant A should see its own registration, saw $own" >&2; return 1; }
    echo "scenario 5: PASS"
}

wf1_up
scenario_1
scenario_2
scenario_3
scenario_4
scenario_5
echo "workflow-1 acceptance: ALL SCENARIOS PASS"
