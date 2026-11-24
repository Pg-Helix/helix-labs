# helix-labs

Reference applications, datasets, evaluations, FaultLab and benchmarks.

| | |
|---|---|
| Ownership group | Infrastructure and Reliability |
| Primary form | Go / SQL |
| Visibility | PUBLIC |
| Status | **Workflow 1 and 2 acceptance gates + a limited retrieval evaluation (unmerged feature branches).** `workflow-1/` and `workflow-2/` drive the real Foundry stack and the real cortex binaries; `eval/` runs PROTOCOL.md's fixed-target comparison of FTS, dense, hybrid and served ranking on a small synthetic corpus. FaultLab, benchmarks and reference apps are still design only. |

Design source of truth: the private `helix-atlas` repository (`architecture/repos/helix-labs.md`).
Organization master prompt: PgHelix Organization Master Prompt, 16 Oct 2026.

## Workflow 1 (`workflow-1/`)

```
cd workflow-1
./run.sh          # short smoke demo: register -> insert -> ready result -> clean up
./acceptance.sh   # the full five-scenario gate (see MANIFEST.md for the exact
                   # repository commits this is validated against)
```

Assumes this workspace's sibling-checkout layout (`helix-foundry`,
`helix-kernel`, `helix-cortex` next to this repo) -- see `MANIFEST.md` and
`prepare-checkouts.sh` for a reproducible clean-clone setup. Both scripts
use a uniquely-namespaced Compose project per run (never a fixed name)
and preserve worker logs before cleaning up on failure. Mock embeddings
throughout are deterministic functional fixtures (helix-infer's `Mock`
provider) -- no semantic-quality claim, no external provider or
credential.

## Workflow 2 (`workflow-2/`) and the evaluation (`eval/`)

```
cd workflow-2
./run.sh             # short demo: ingest -> search -> cited answer -> evidence -> revoke
./acceptance.sh      # ten-scenario gate (idempotency, races, freshness, ACL filtering, in-flight revocation, ...)
./version-change.sh  # embedding-binding backfill/cutover/isolation properties
cd ../eval
sh run-eval.sh mock && sh run-eval.sh real && sh run-eval.sh real-norerank
python3 score.py     # see eval/PROTOCOL.md
```

`workflow-2/lib.sh` is shared by all of these. Each run gets its own Compose
project and private temp directory. Before any scenario runs it:

- applies helix-cortex's migrations via `migrate.sh` as `helix_cortex_owner`;
- proves a rerun is a no-op and that `helix_cortex_api` cannot own or create
  schema objects;
- reads the provider, model, dims and reranker from the running containers
  and fails if they are not the requested profile.

`score.py` validates all evidence before computing anything: run metadata,
input hashes, provenance, the document and chunk inventory, every required
response, its status and its schema. It exits 0 when everything passes, 1 when
the evidence is valid but a target or security gate is missed, and 2 when the
evidence is invalid. A missing or empty results directory can never pass.

## Build and test

```
python3 -m unittest -v eval/test_score.py   # offline scorer regressions (seconds, no Docker)
sh workflow-1/acceptance.sh
sh workflow-2/acceptance.sh && sh workflow-2/version-change.sh
```

CI (`.github/workflows/ci.yml`) runs all of the above plus the three
evaluation passes and the scorer, against `MANIFEST.md`'s effective pins.

## License

Not yet decided. The license is an ADR in `helix-atlas`; until it is accepted, no license is granted and all rights are reserved.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).
