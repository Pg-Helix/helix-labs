# Dependency manifest

The exact commits `workflow-1/acceptance.sh` and `workflow-2/acceptance.sh` are both
validated against. `prepare-checkouts.sh` reads this file to pin every sibling checkout
before a run, so either acceptance gate is reproducible from a clean set of clones, not
whatever happens to be checked out locally.

None of these branches are merged to `main` yet — this manifest pins feature branches,
not releases, and must be updated whenever one of them is rebased, amended, or merged.
It is the single discoverable source for "which commit was this actually tested against"
across every dependent PR; each PR's own description links back to this file rather than
repeating the SHA.

helix-cortex and helix-foundry now serve BOTH workflows from a single branch each (the
Workflow 2 branches are supersets of the Workflow 1 ones they're stacked on) — there is
only one row per repo below, and it is what both acceptance gates are tested against.

| repo | branch | commit | PR |
|---|---|---|---|
| helix-atlas | decisions/workflow2-ingestion-retrieval (stacked on k2-k3-security-corrections) | `bec6b1ef2d41f861a17293a69a33ee7b9d775f3b` | [#11](https://github.com/Pg-Helix/helix-atlas/pull/11) |
| helix-protocol | feat/workflow2-contract (stacked on k2-k3-contract-sync) | `9ada018776e9834388b9fd6ae19ff16b644efc5c` | [#7](https://github.com/Pg-Helix/helix-protocol/pull/7) |
| helix-kernel | feat/extension-skeleton (foundation, base of the two below) | `5de26ffac8bc7eb6b7d50cd2793a87c352fffc5c` | [#3](https://github.com/Pg-Helix/helix-kernel/pull/3) |
| helix-kernel | feat/k2-registration | `e2528cdf2b74edbe5be76cd5ad7ffb9a88a8ebf8` | [#6](https://github.com/Pg-Helix/helix-kernel/pull/6) |
| helix-kernel | feat/k3-refresh-publish (stacked on the two rows above) | `5f5895e5d3efd6e623d4366ab871dc7e9d251f91` | [#8](https://github.com/Pg-Helix/helix-kernel/pull/8) |
| helix-kernel | feat/embedding-binding-identity (stacked on feat/k3-refresh-publish) | `0f5bec3ee5f5f9506d0be87c1bec856836d10080` | [#9](https://github.com/Pg-Helix/helix-kernel/pull/9) |
| helix-infer | feat/i1-provider-interface | `444d5213ccb97dfb7dd438db689c93b646dd505c` | [#5](https://github.com/Pg-Helix/helix-infer/pull/5) |
| helix-infer | feat/real-embedding-provider (stacked on feat/i1-provider-interface) | `b577bff4e6a34fb41bcd48611688efcf25249b54` | [#6](https://github.com/Pg-Helix/helix-infer/pull/6) |
| helix-cortex | feat/workflow2-ingestion-retrieval (stacked on feat/c1-worker; serves both workflows) | `ec6df64af9ce41009e57e046d28903012b45f875` | [#5](https://github.com/Pg-Helix/helix-cortex/pull/5) |
| helix-cortex | feat/real-embedding-provider (stacked on feat/workflow2-ingestion-retrieval; serves all three workflows/milestones) | `4ab62cbdfbf8bac0bef6c275d5a08baa3d3a913b` | [#6](https://github.com/Pg-Helix/helix-cortex/pull/6) |
| helix-foundry | feat/workflow2-pgvector (stacked on feat/phase1-local-stack; serves both workflows) | `85637c8068427329ec32622b1954b852a48bbd72` | [#5](https://github.com/Pg-Helix/helix-foundry/pull/5) |
| helix-foundry | feat/real-embedding-provider (stacked on feat/workflow2-pgvector) | `18dffc19bb9cb79fbd83f9250e21682dd5936f9c` | [#6](https://github.com/Pg-Helix/helix-foundry/pull/6) |
| helix-labs | feat/workflow1-demo | this repo's own current `HEAD` -- not pinned here, since a commit adding/editing this file cannot name its own final SHA in advance | [#4](https://github.com/Pg-Helix/helix-labs/pull/4) |
| helix-labs | feat/workflow2-acceptance (stacked on feat/workflow1-demo) | this repo's own current `HEAD` -- not pinned here, for the same reason as above | [#5](https://github.com/Pg-Helix/helix-labs/pull/5) |
| helix-labs | feat/real-embedding-eval (stacked on feat/workflow2-acceptance) | this repo's own current `HEAD` -- not pinned here, for the same reason as above | [#6](https://github.com/Pg-Helix/helix-labs/pull/6) |

## Effective pins

The commits `prepare-checkouts.sh` and CI (`.github/workflows/ci.yml`, which runs
that same script) actually check out. Every stack-building job and a clean local
reproduction resolve exactly these three rows -- the table above is the PR stack's
history; this section is what is built and tested.

| repo | commit |
|---|---|
| helix-foundry | `18dffc19bb9cb79fbd83f9250e21682dd5936f9c` |
| helix-kernel | `0f5bec3ee5f5f9506d0be87c1bec856836d10080` |
| helix-cortex | `4ab62cbdfbf8bac0bef6c275d5a08baa3d3a913b` |

helix-protocol and helix-infer are not checked out separately: helix-cortex's
`go.mod`/`go.sum` pin them inside the cortex image build (`eval/run-eval.sh` records
those versions in each run's `run.json`). helix-atlas is documentation only.

## Reproducing

Use a disposable parent directory: the script detaches each sibling at its pin and
refuses to touch one with uncommitted changes.

```
mkdir helix-repro && cd helix-repro
git clone -b feat/real-embedding-eval https://github.com/Pg-Helix/helix-labs.git
sh helix-labs/prepare-checkouts.sh       # clones/pins helix-foundry, helix-kernel, helix-cortex
export COMPOSE_BAKE=false                # classic builder; see ci.yml
python3 -m unittest -v helix-labs/eval/test_score.py   # offline, seconds
sh helix-labs/eval/calibrate.sh        # replays the /answers cutoff (0.707) on the pinned model
cd helix-labs/workflow-1 && sh acceptance.sh
cd ../workflow-2 && sh acceptance.sh && sh version-change.sh
cd ../eval && sh run-eval.sh mock && sh run-eval.sh real && sh run-eval.sh real-norerank
python3 score.py                         # exit 0 pass / 1 target missed / 2 invalid evidence
```

Each script brings up and tears down its own Compose project on fixed loopback ports
(55480 by default): run them one at a time on a host.
