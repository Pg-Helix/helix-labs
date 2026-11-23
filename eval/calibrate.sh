#!/bin/sh
# One command to replay the /answers threshold calibration (helix-atlas
# ADR-0008) from a clean checkout:
#
#   sh helix-labs/prepare-checkouts.sh     # pins helix-foundry next to this repo
#   sh helix-labs/eval/calibrate.sh
#
# Builds helix-foundry's pinned Ollama image (images/ollama: pinned
# runtime digest, pinned nomic-embed-text:v1.5 weights, build fails on a
# digest mismatch), runs it on loopback only, and runs calibrate.py
# against it. Writes the replay record to $CALIBRATION_OUT (default:
# eval/calibration-replay.json, gitignored) and exits 0 only if it
# reproduces the reference ($CALIBRATION_REFERENCE, default the committed
# eval/calibration.json); 1 if it differs, 2 if it could not run or the
# output path resolves to the reference.
set -eu
EVAL_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
OLLAMA_DIR="$EVAL_DIR/../../helix-foundry/images/ollama"
PORT=${CALIBRATION_OLLAMA_PORT:-55491}
[ -f "$OLLAMA_DIR/Dockerfile" ] || { echo "calibrate: $OLLAMA_DIR/Dockerfile not found -- run prepare-checkouts.sh first" >&2; exit 2; }
T=$(mktemp -d)
OUT=${CALIBRATION_OUT:-$EVAL_DIR/calibration-replay.json}

docker build -q -t helix-calibration-ollama "$OLLAMA_DIR" >/dev/null
CID=$(docker run -d --rm -p "127.0.0.1:$PORT:11434" helix-calibration-ollama)
trap 'docker stop "$CID" >/dev/null 2>&1 || true; rm -rf "$T"' EXIT INT TERM
i=0
until curl -sf "http://127.0.0.1:$PORT/api/tags" >/dev/null; do
    i=$((i + 1)); [ "$i" -lt 60 ] || { echo "calibrate: ollama did not become ready" >&2; exit 2; }
    sleep 1
done
docker exec "$CID" cat /root/.ollama/models/manifests/registry.ollama.ai/library/nomic-embed-text/v1.5 > "$T/manifest.json"
python3 "$EVAL_DIR/calibrate.py" --url "http://127.0.0.1:$PORT" --manifest "$T/manifest.json" \
    --dockerfile "$OLLAMA_DIR/Dockerfile" --out "$OUT" --record "${CALIBRATION_REFERENCE:-$EVAL_DIR/calibration.json}" \
    --arch "$(docker version -f '{{.Server.Arch}}')"
