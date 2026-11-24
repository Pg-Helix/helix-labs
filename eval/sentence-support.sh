#!/bin/sh
# Pre-registered sentence-support replay (EXPERIMENT-sentence-support.md)
# against helix-foundry's pinned Ollama image, like calibrate.sh:
#
#   sh helix-labs/prepare-checkouts.sh
#   cd helix-labs/eval && EVAL_SUITE=dev-replay sh run-eval.sh real-norerank
#   sh sentence-support.sh [RESULTS_DIR]
#
# RESULTS_DIR defaults to eval/results-dev-replay-real-norerank. Writes
# $SENTENCE_SUPPORT_REPORT (default eval/report-sentence-support.json) and
# caches embeddings in $SENTENCE_SUPPORT_CACHE (default
# eval/results-embedding-cache/embeddings.json, gitignored; never inside
# the results dir, which holds only planned evidence).
# Exit 0 = valid replay (whatever the decision), 2 = invalid or the control
# could not be reproduced.
set -eu
EVAL_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
OLLAMA_DIR="$EVAL_DIR/../../helix-foundry/images/ollama"
PORT=${SENTENCE_SUPPORT_OLLAMA_PORT:-55493}
RESULTS=${1:-$EVAL_DIR/results-dev-replay-real-norerank}
[ -f "$OLLAMA_DIR/Dockerfile" ] || { echo "sentence-support: $OLLAMA_DIR/Dockerfile not found -- run prepare-checkouts.sh first" >&2; exit 2; }
T=$(mktemp -d)
docker build -q -t helix-calibration-ollama "$OLLAMA_DIR" >/dev/null
CID=$(docker run -d --rm -p "127.0.0.1:$PORT:11434" helix-calibration-ollama)
trap 'docker stop "$CID" >/dev/null 2>&1 || true; rm -rf "$T"' EXIT INT TERM
i=0
until curl -sf "http://127.0.0.1:$PORT/api/tags" >/dev/null; do
    i=$((i + 1)); [ "$i" -lt 60 ] || { echo "sentence-support: ollama did not become ready" >&2; exit 2; }
    sleep 1
done
docker exec "$CID" cat /root/.ollama/models/manifests/registry.ollama.ai/library/nomic-embed-text/v1.5 > "$T/manifest.json"
python3 "$EVAL_DIR/sentence_support.py" replay "$RESULTS" --url "http://127.0.0.1:$PORT" --manifest "$T/manifest.json" \
    --dockerfile "$OLLAMA_DIR/Dockerfile" --cache "${SENTENCE_SUPPORT_CACHE:-$EVAL_DIR/results-embedding-cache/embeddings.json}" \
    --report "${SENTENCE_SUPPORT_REPORT:-$EVAL_DIR/report-sentence-support.json}"
