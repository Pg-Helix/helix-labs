# helix-labs

Reference applications, datasets, evaluations, FaultLab and benchmarks.

| | |
|---|---|
| Ownership group | Infrastructure and Reliability |
| Primary form | Go / SQL |
| Visibility | PUBLIC |
| Status | **Workflow 1 only.** `workflow-1/` has a short smoke demo and a five-scenario acceptance gate run against the real Foundry stack and the real cortex worker binary -- authorized registration, update-in-flight rejection, delete/reinsert/primary-key-change, crash/re-lease/duplicate-delivery/retry-exhaustion, and unauthorized-ops-rejected-with-positive-controls. Reference apps/datasets/evaluations/FaultLab/benchmarks beyond this are still design only. |

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

## Build and test

`sh workflow-1/acceptance.sh` is this repo's test today. Go-based
reference apps/fixtures ("go test ./... + fixtures") are not yet
implemented.

## License

Not yet decided. The license is an ADR in `helix-atlas`; until it is accepted, no license is granted and all rights are reserved.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).
