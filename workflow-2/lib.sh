#!/bin/sh
# Shared helpers for workflow-2/acceptance.sh, version-change.sh, run.sh
# and eval/run-eval.sh (ADR-0007, helix-atlas). Sourced, not executed.
# Mirrors workflow-1/lib.sh's patterns (unique Compose project per run,
# generated gitignored credentials, diagnostics preserved before
# cleanup) for the ingestion/retrieval stack.
#
# Environment knobs (all optional):
#   WF2_REAL_EMBEDDINGS=1     add Foundry's real-embeddings overlay
#                             (Ollama + nomic-embed-text) and REQUIRE the
#                             running services to report that provider.
#   CORTEX_DISABLE_RERANK=1   (real overlay only) serve RRF order without
#                             the lexical reranker; verified like above.
#   WF2_HTTP_TIMEOUT          per-request curl --max-time (default 60s).
#   WF2_ARTIFACT_DIR          on failure, service + migration logs are
#                             copied here (CI uploads it).
#   WF2_KEEP_TMP=1            keep this run's private temp dir.
set -eu

WF2_SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
WF2_LIB_DIR=$(CDPATH= cd -- "$WF2_SCRIPT_DIR/../workflow-2" && pwd)
COMPOSE_DIR="$WF2_LIB_DIR/../../helix-foundry/compose"
CORTEX_DIR="$WF2_LIB_DIR/../../helix-cortex"

[ -f "$COMPOSE_DIR/.env" ] || "$COMPOSE_DIR/generate-env.sh"
. "$COMPOSE_DIR/.env"
[ -f "$COMPOSE_DIR/auth-fixture.json" ] || "$COMPOSE_DIR/generate-auth-fixture.sh"

# Per-run private scratch space: every response body, status file and
# log of this run lives here, never at a fixed /tmp path another run
# (or a concurrent request in this run) could overwrite.
WF2_TMP=$(mktemp -d "${TMPDIR:-/tmp}/helix-wf2.XXXXXX")
WF2_BODY="$WF2_TMP/api_body.json"
WF2_HTTP_TIMEOUT=${WF2_HTTP_TIMEOUT:-60}

WF2_PROJECT="helix-wf2-$$"
COMPOSE_PROJECT_NAME="$WF2_PROJECT"
export COMPOSE_PROJECT_NAME
COMPOSE="docker compose -p $WF2_PROJECT -f $COMPOSE_DIR/docker-compose.yml"
if [ "${WF2_REAL_EMBEDDINGS:-0}" = "1" ]; then
    COMPOSE="$COMPOSE -f $COMPOSE_DIR/docker-compose.real-embeddings.yml"
    WF2_EXPECT_PROFILE=real
else
    WF2_EXPECT_PROFILE=mock
fi

# WF2_API_URL is test-only: it routes every request through something
# else (e.g. a fault-injecting loopback proxy) while Compose still
# publishes cortex-api on HELIX_CORTEX_API_PORT.
API=${WF2_API_URL:-"http://127.0.0.1:${HELIX_CORTEX_API_PORT:-55480}/v1"}

# token_for PRINCIPAL: reads the opaque token the local auth fixture
# mapped to PRINCIPAL -- this suite never invents its own tokens, it
# discovers the real ones the running service was actually configured
# with (ADR-0007 decision 1), same as reading HELIX_*_PASSWORD from .env.
token_for() {
    python3 - "$COMPOSE_DIR/auth-fixture.json" "$1" <<'EOF'
import json, sys
for token, info in json.load(open(sys.argv[1])).items():
    if info["principal"] == sys.argv[2]:
        print(token); sys.exit(0)
sys.exit("no token for principal " + sys.argv[2])
EOF
}

ALICE_TOKEN=""
BOB_TOKEN=""
CAROL_TOKEN=""

wf2_load_tokens() {
    ALICE_TOKEN=$(token_for alice)
    BOB_TOKEN=$(token_for bob)
    CAROL_TOKEN=$(token_for carol)
}

# api METHOD PATH TOKEN [JSON_BODY] [OUTFILE] [EXTRA_HEADER]
# Prints the HTTP status ("000" if no response arrived) and writes the
# body to OUTFILE (default $WF2_BODY). Returns curl's own exit status,
# so a transport failure or timeout is never mistaken for a response.
# Concurrent calls MUST each pass their own OUTFILE: a shared file
# would race, and neither request's status/body could be trusted.
# The body goes over stdin (-d @-), never argv: scenario 10's oversized
# document would exceed the OS argument-length limit.
api() {
    _method=$1 _path=$2 _token=$3 _body=${4:-} _out=${5:-$WF2_BODY} _extra=${6:-}
    set -- -sS -o "$_out" -w '%{http_code}' --max-time "$WF2_HTTP_TIMEOUT" \
        -X "$_method" "$API$_path" -H "Authorization: Bearer $_token"
    [ -n "$_extra" ] && set -- "$@" -H "$_extra"
    if [ -n "$_body" ]; then
        printf '%s' "$_body" | curl "$@" -H "Content-Type: application/json" -d @-
    else
        curl "$@"
    fi
}

# record_request DIR NAME METHOD PATH TOKEN BODY [EXTRA_HEADER]: one
# evaluated request. Writes DIR/NAME.json (body), DIR/NAME.code (HTTP
# status) and DIR/NAME.curl (curl's own exit status). Succeeds only if
# the transfer completed (curl 0) with an HTTP status: an expected status
# and a parseable body do not prove the response arrived whole -- e.g.
# curl 18 (connection closed before the advertised Content-Length) still
# reports the status line it received.
record_request() {
    _rc=0
    api "$3" "$4" "$5" "$6" "$1/$2.json" "${7:-}" > "$1/$2.code" || _rc=$?
    echo "$_rc" > "$1/$2.curl"
    [ "$_rc" -eq 0 ] && grep -Eq '^[1-5][0-9][0-9]$' "$1/$2.code"
}

# bg_reap PID CODEFILE: reaps one background request (a subshell whose
# exit status is curl's) and sets BG_STATUS to the HTTP status in
# CODEFILE if the transfer completed, otherwise to
# "curl-exit-N(http X)" -- which no expected status can equal, so a
# failed transfer fails its check even if its status line said 201/403.
bg_reap() {
    _rc=0
    wait "$1" || _rc=$?
    _http=$(cat "$2" 2>/dev/null || true)
    if [ "$_rc" -eq 0 ]; then
        BG_STATUS=$_http
    else
        BG_STATUS="curl-exit-$_rc(http ${_http:-none})"
    fi
}

# api_json METHOD PATH TOKEN [BODY] [EXTRA_HEADER]: api() with one extra
# raw header line (the eval stage / test-barrier headers).
api_json() {
    api "$1" "$2" "$3" "${4:-}" "$WF2_BODY" "${5:-}"
}

# json_field PATH [FILE]: dotted path into a JSON response (default the
# shared response file). Arguments go through argv, never into code.
json_field() {
    python3 - "$1" "${2:-$WF2_BODY}" <<'EOF'
import json, sys
v = json.load(open(sys.argv[2]))
for part in sys.argv[1].split("."):
    v = v[int(part)] if isinstance(v, list) else v[part]
print(v)
EOF
}

# pg_admin SQL...: a direct superuser connection, for setup/verification
# steps no HTTP endpoint exposes.
pg_admin() {
    $COMPOSE exec -T -e PGPASSWORD="$HELIX_ADMIN_PASSWORD" postgres psql -h 127.0.0.1 -U postgres -d postgres -v ON_ERROR_STOP=1 -tAq "$@"
}

# wf2_migrate LOGFILE: helix-cortex's own migration runner
# (migrations/migrate.sh), the supported path: a trusted setup
# connection that SET ROLEs into helix_cortex_owner for DDL (Foundry's
# initdb makes that role NOLOGIN and keeps helix_cortex_api out of it).
# Runs inside the postgres container so the host needs no psql.
wf2_migrate() {
    $COMPOSE exec -T -e PGHOST=127.0.0.1 -e PGUSER=postgres -e PGPASSWORD="$HELIX_ADMIN_PASSWORD" -e PGDATABASE=postgres \
        postgres sh /tmp/cortex-migrations/migrate.sh > "$1" 2>&1
}

# wf2_check_roles: the schema-owner / runtime-credential split still
# holds after migration (owner owns schema + every table, runtime role
# cannot become owner and cannot CREATE in the schema).
wf2_check_roles() {
    _roles=$(pg_admin -c "SELECT
        (SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'cortex'),
        (SELECT count(*) FROM pg_tables WHERE schemaname = 'cortex'),
        (SELECT count(*) FROM pg_tables WHERE schemaname = 'cortex' AND tableowner <> 'helix_cortex_owner'),
        pg_has_role('helix_cortex_api', 'helix_cortex_owner', 'MEMBER'),
        has_schema_privilege('helix_cortex_api', 'cortex', 'CREATE'),
        (SELECT rolcanlogin FROM pg_roles WHERE rolname = 'helix_cortex_owner')")
    IFS='|' read -r _owner _tables _foreign _member _create _login <<EOF
$_roles
EOF
    if [ "$_owner" != helix_cortex_owner ] || [ "$_tables" -lt 1 ] || [ "$_foreign" != 0 ] ||
       [ "$_member" != f ] || [ "$_create" != f ] || [ "$_login" != f ]; then
        echo "wf2: role separation FAILED: schema_owner=$_owner tables=$_tables not_owner_owned=$_foreign api_member_of_owner=$_member api_can_create=$_create owner_can_login=$_login" >&2
        return 1
    fi
    echo "wf2: role separation ok (schema + $_tables tables owned by helix_cortex_owner; helix_cortex_api not a member, no CREATE; owner NOLOGIN)"
}

# wf2_container_env SERVICE: the effective (running container) CORTEX_*
# provider settings only -- a fixed allow-list, so credentials such as
# DATABASE_URL or CORTEX_EMBED_API_KEY are never printed or recorded.
wf2_container_env() {
    docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$($COMPOSE ps -q "$1")" |
        grep -E '^CORTEX_(EMBED_PROVIDER|EMBED_WIRE_MODEL|EMBED_BASE_URL|CHUNK_MODEL_ID|CHUNK_MODEL_REVISION|CHUNK_MODEL_DIMS|MODEL_ID|MODEL_REVISION|MODEL_DIMS|INPUT_EXTRACTOR|DISABLE_RERANK)=' || true
}

# wf2_verify_provider: records what the running services were actually
# configured with to $WF2_TMP/provider.json and fails unless it is the
# profile this run asked for (WF2_REAL_EMBEDDINGS / CORTEX_DISABLE_RERANK).
# A results directory's name is not evidence of which model ran.
wf2_verify_provider() {
    _api_env=$(wf2_container_env cortex-api)
    _worker_env=$(wf2_container_env chunk-worker)
    _manifest="" _pinned=""
    # The profile each service RESOLVED at startup (helix-cortex
    # embedconfig.Resolve logs it), not just the env it was given.
    _api_profile=$($COMPOSE logs --no-log-prefix cortex-api 2>/dev/null | grep -o 'embedding profile: .*' | tail -1 || true)
    _worker_profile=$($COMPOSE logs --no-log-prefix chunk-worker 2>/dev/null | grep -o 'embedding profile: .*' | tail -1 || true)
    # ...and the input the chunk worker actually feeds its embedder.
    _worker_loop=$($COMPOSE logs --no-log-prefix chunk-worker 2>/dev/null | grep -o 'worker loop started .*' | tail -1 || true)
    if [ "$WF2_EXPECT_PROFILE" = "real" ]; then
        _manifest=$($COMPOSE exec -T ollama cat /root/.ollama/models/manifests/registry.ollama.ai/library/nomic-embed-text/v1.5 2>/dev/null || true)
        _pinned=$(sed -n 's/^ARG MODEL_WEIGHTS_DIGEST=//p' "$COMPOSE_DIR/../images/ollama/Dockerfile")
    fi
    API_ENV="$_api_env" WORKER_ENV="$_worker_env" OLLAMA_MANIFEST="$_manifest" PINNED_DIGEST="$_pinned" \
    EXPECT_PROFILE="$WF2_EXPECT_PROFILE" EXPECT_DISABLE_RERANK="${CORTEX_DISABLE_RERANK:-0}" \
    API_PROFILE="$_api_profile" WORKER_PROFILE="$_worker_profile" WORKER_LOOP="$_worker_loop" \
        python3 - "$WF2_TMP/provider.json" <<'EOF'
import json, os, sys
def env(name):
    return dict(l.split("=", 1) for l in os.environ[name].splitlines() if "=" in l)
api, worker = env("API_ENV"), env("WORKER_ENV")
profile = os.environ["EXPECT_PROFILE"]
want_off = os.environ["EXPECT_DISABLE_RERANK"] == "1"
side = lambda e, p: {
    "provider": e.get("CORTEX_EMBED_PROVIDER", "mock"),
    "wire_model": e.get("CORTEX_EMBED_WIRE_MODEL"),
    "model_id": e.get(p + "MODEL_ID"),
    "model_revision": e.get(p + "MODEL_REVISION"),
    "dims": int(e[p + "MODEL_DIMS"]) if e.get(p + "MODEL_DIMS") else None,
}
q, w = side(api, "CORTEX_CHUNK_"), side(worker, "CORTEX_")
# Mirrors helix-cortex internal/embedconfig QueryPrefix/DocumentPrefix
# (keyed by model_id); recorded so a report states the preprocessing.
prefix = {"nomic-embed-text": ("search_query: ", "search_document: ")}.get(q["model_id"], ("", ""))
digest = None
if os.environ["OLLAMA_MANIFEST"]:
    layers = json.loads(os.environ["OLLAMA_MANIFEST"]).get("layers", [])
    digest = next((l["digest"] for l in layers if l.get("mediaType", "").endswith(".model")), None)
pinned = os.environ["PINNED_DIGEST"].replace("sha256-", "sha256:") or None
rec = {
    "profile": profile,
    "query_embedding": q, "chunk_worker": w,
    "reranker": "disabled" if api.get("CORTEX_DISABLE_RERANK") == "1" else "enabled",
    "preprocessing": {"query_prefix": prefix[0], "document_prefix": prefix[1]},
    "model_artifact": {"weights_digest": digest, "pinned_digest": pinned},
    "resolved_profile": os.environ["API_PROFILE"].removeprefix("embedding profile: ") or None,
    "chunk_worker_input": None,
}
import re
m = re.search(r'input_extractor=(\S+) document_prefix=("(?:[^"\\]|\\.)*")', os.environ["WORKER_LOOP"])
if m:
    rec["chunk_worker_input"] = {"input_extractor": m.group(1), "document_prefix": json.loads(m.group(2))}
errs = []
# Workflow 2's chunk worker must embed "<document prefix><chunk text>",
# the preprocessing the profile (and, for real, the calibration) assumes.
wi = rec["chunk_worker_input"]
if not wi or wi["input_extractor"] != "chunk-text" or wi["document_prefix"] != prefix[1]:
    errs.append(f"chunk-worker effective input {wi!r} != chunk-text with document prefix {prefix[1]!r} ({os.environ['WORKER_LOOP']!r})")
if not os.environ["API_PROFILE"] or os.environ["API_PROFILE"] != os.environ["WORKER_PROFILE"]:
    errs.append(f"resolved profiles differ or are missing: cortex-api {os.environ['API_PROFILE']!r} / chunk-worker {os.environ['WORKER_PROFILE']!r}")
if q["model_id"] != w["model_id"] or q["dims"] != w["dims"] or q["provider"] != w["provider"]:
    errs.append("query embedder and chunk worker disagree")
if profile == "real":
    for name, s in (("cortex-api", q), ("chunk-worker", w)):
        exp = {"provider": "openai-compat", "wire_model": "nomic-embed-text:v1.5",
               "model_id": "nomic-embed-text", "model_revision": "v1.5", "dims": 768}
        for k, v in exp.items():
            if s[k] != v:
                errs.append(f"{name} {k}={s[k]!r}, want {v!r}")
    if "answer_rule=cosine>=0.707" not in (rec["resolved_profile"] or ""):
        errs.append(f"real profile without the calibrated answer rule: {rec['resolved_profile']!r}")
    if not digest or digest != pinned:
        errs.append(f"ollama weights digest {digest!r} != pinned {pinned!r}")
else:
    if q["provider"] != "mock" or w["provider"] != "mock":
        errs.append(f"mock profile requested but providers are {q['provider']}/{w['provider']}")
    if "answer_rule=none" not in (rec["resolved_profile"] or ""):
        errs.append(f"mock profile with an answer rule: {rec['resolved_profile']!r}")
if (rec["reranker"] == "disabled") != want_off:
    errs.append(f"reranker is {rec['reranker']} but CORTEX_DISABLE_RERANK={int(want_off)} was requested")
json.dump(rec, open(sys.argv[1], "w"), indent=2)
if errs:
    sys.exit("wf2: provider mismatch: " + "; ".join(errs))
print(f"wf2: provider ok: {profile} {q['provider']} {q['model_id']}:{q['model_revision']} dims={q['dims']} reranker={rec['reranker']} weights={digest or 'n/a'}")
EOF
}

wf2_up() {
    echo "wf2: bringing up project $WF2_PROJECT (profile=$WF2_EXPECT_PROFILE, tmp=$WF2_TMP)"
    $COMPOSE up --build -d
    i=0
    # Exact match: a substring test would also accept "unhealthy".
    until [ "$($COMPOSE ps postgres --format '{{.Health}}' 2>/dev/null || true)" = healthy ]; do
        i=$((i + 1))
        if [ "$i" -gt 60 ]; then
            echo "wf2: postgres never became healthy" >&2
            return 1
        fi
        sleep 1
    done
    $COMPOSE cp "$CORTEX_DIR/migrations" postgres:/tmp/cortex-migrations
    # Explicit status capture (not set -e inside a redirection): an
    # error must surface here, with the migration's own output.
    if ! wf2_migrate "$WF2_TMP/migration.log"; then
        echo "wf2: cortex migration FAILED -- migrate.sh output follows" >&2
        tail -50 "$WF2_TMP/migration.log" >&2
        return 3
    fi
    # Idempotent rerun: a second pass must apply nothing.
    if ! wf2_migrate "$WF2_TMP/migration-rerun.log" || grep -q 'applying' "$WF2_TMP/migration-rerun.log"; then
        echo "wf2: cortex migration rerun was not a clean no-op" >&2
        cat "$WF2_TMP/migration-rerun.log" >&2
        return 3
    fi
    echo "wf2: migrations applied as helix_cortex_owner; rerun was a no-op ($(grep -c 'already applied' "$WF2_TMP/migration-rerun.log") skipped)"
    wf2_check_roles
    wf2_wait_api 60
    wf2_verify_provider
    wf2_load_tokens
}

# wf2_wait_api SECONDS: cortex-api is up once its unauthenticated probe
# (GET /collections without a token) completes (curl 0) with its expected
# answer, HTTP 401. A refused connection, a failed transfer or any other
# status is retried until the deadline, then reported.
wf2_wait_api() {
    _deadline=$(( $(date +%s) + $1 ))
    while true; do
        _rc=0
        _http=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$API/collections") || _rc=$?
        [ "$_rc" -eq 0 ] && [ "$_http" = 401 ] && return 0
        if [ "$(date +%s)" -ge "$_deadline" ]; then
            echo "wf2: cortex-api never gave its unauthenticated 401 on $API/collections within $1s (last: curl exit $_rc, http ${_http:-none})" >&2
            return 1
        fi
        sleep 1
    done
}

# wf2_document_status FILE EXPECTED: succeeds only if FILE is strict JSON
# (UTF-8; no NaN/Infinity constants, which Python's default parser takes;
# no duplicate keys) holding an object whose "status" is exactly the
# string EXPECTED -- compared inside the parser, never after a shell
# command substitution, which strips trailing newlines ("ready\n" would
# become "ready"). Prints the decoded status JSON-encoded for diagnostics.
wf2_document_status() {
    python3 - "$1" "$2" <<'EOF'
import json, sys
def reject_constant(name):
    raise ValueError(f"non-standard constant {name}")
def reject_duplicates(pairs):
    keys = [k for k, _ in pairs]
    dup = sorted({k for k in keys if keys.count(k) > 1})
    if dup:
        raise ValueError(f"duplicate key {dup[0]!r}")
    return dict(pairs)
try:
    with open(sys.argv[1], "rb") as f:
        doc = json.loads(f.read().decode("utf-8"), parse_constant=reject_constant, object_pairs_hook=reject_duplicates)
except FileNotFoundError:
    print("none (no body)"); sys.exit(1)
except (UnicodeDecodeError, ValueError) as e:
    print(f"none (invalid JSON: {e})"); sys.exit(1)
if not isinstance(doc, dict) or "status" not in doc:
    print("none (no status field)"); sys.exit(1)
print(json.dumps(doc["status"]))
sys.exit(0 if isinstance(doc["status"], str) and doc["status"] == sys.argv[2] else 1)
EOF
}

# wf2_wait_document_status COLLECTION DOCUMENT TOKEN EXPECTED TIMEOUT_SECONDS
# Ready only on a response that is THIS poll's own and complete: curl
# exit 0, HTTP 200, and a fresh body that wf2_document_status accepts
# (strict JSON, "status" exactly EXPECTED). A failed transfer (e.g. curl
# 18 after a "ready" body), a non-200 with a ready-looking body, or any
# other body is retried until the deadline (plus at most one in-flight
# request, WF2_HTTP_TIMEOUT), then the last transport, HTTP and document
# status are reported.
wf2_wait_document_status() {
    _wcol=$1 _wdoc=$2 _wtok=$3 _wexp=$4 _wsec=${5:-30}
    _wout="$WF2_TMP/document_status.json"
    _wend=$(( $(date +%s) + _wsec ))
    while true; do
        rm -f "$_wout"
        _rc=0
        _http=$(api GET "/collections/$_wcol/documents/$_wdoc" "$_wtok" "" "$_wout") || _rc=$?
        _wok=0
        _wstat=$(wf2_document_status "$_wout" "$_wexp") && _wok=1
        [ "$_rc" -eq 0 ] && [ "$_http" = 200 ] && [ "$_wok" = 1 ] && return 0
        if [ "$(date +%s)" -ge "$_wend" ]; then
            printf 'wf2: document %s not %s within %ss (last poll: curl exit %s, http %s, document status %s)\n' \
                "$_wdoc" "$_wexp" "$_wsec" "$_rc" "${_http:-none}" "$_wstat" >&2
            return 1
        fi
        sleep 1
    done
}

wf2_cleanup() {
    status=$?
    trap - EXIT INT TERM
    if [ "$status" -ne 0 ]; then
        echo "wf2: FAILED (exit $status) -- diagnostics follow" >&2
        [ -f "$WF2_TMP/migration.log" ] && { echo "--- migration log" >&2; tail -50 "$WF2_TMP/migration.log" >&2; }
        $COMPOSE logs --no-color > "$WF2_TMP/compose.log" 2>&1 || true
        tail -150 "$WF2_TMP/compose.log" >&2 || true
        if [ -n "${WF2_ARTIFACT_DIR:-}" ]; then
            mkdir -p "$WF2_ARTIFACT_DIR"
            cp "$WF2_TMP"/*.log "$WF2_ARTIFACT_DIR"/ 2>/dev/null || true
        fi
    fi
    echo "wf2: cleaning up project $WF2_PROJECT"
    $COMPOSE down -v >/dev/null 2>&1 || true
    [ "${WF2_KEEP_TMP:-0}" = "1" ] || rm -rf "$WF2_TMP"
    exit "$status"
}
