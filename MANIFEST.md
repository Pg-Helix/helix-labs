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
| helix-atlas | decisions/workflow2-ingestion-retrieval (stacked on k2-k3-security-corrections) | `3bd4760a5e1b3f3a2b577f6ea30d033c107a3dbd` | [#11](https://github.com/Pg-Helix/helix-atlas/pull/11) |
| helix-protocol | feat/workflow2-contract (stacked on k2-k3-contract-sync) | `e6e2a835bb2534c433e1339c8d2084bed528ca8c` | [#7](https://github.com/Pg-Helix/helix-protocol/pull/7) |
| helix-kernel | feat/extension-skeleton (foundation, base of the two below) | `5de26ffac8bc7eb6b7d50cd2793a87c352fffc5c` | [#3](https://github.com/Pg-Helix/helix-kernel/pull/3) |
| helix-kernel | feat/k2-registration | `e2528cdf2b74edbe5be76cd5ad7ffb9a88a8ebf8` | [#6](https://github.com/Pg-Helix/helix-kernel/pull/6) |
| helix-kernel | feat/k3-refresh-publish (stacked on the two rows above) | `5f5895e5d3efd6e623d4366ab871dc7e9d251f91` | [#8](https://github.com/Pg-Helix/helix-kernel/pull/8) |
| helix-kernel | feat/embedding-binding-identity (stacked on feat/k3-refresh-publish) | `0f5bec3ee5f5f9506d0be87c1bec856836d10080` | [#9](https://github.com/Pg-Helix/helix-kernel/pull/9) |
| helix-infer | feat/i1-provider-interface | `444d5213ccb97dfb7dd438db689c93b646dd505c` | [#5](https://github.com/Pg-Helix/helix-infer/pull/5) |
| helix-infer | feat/real-embedding-provider (stacked on feat/i1-provider-interface) | `b577bff4e6a34fb41bcd48611688efcf25249b54` | [#6](https://github.com/Pg-Helix/helix-infer/pull/6) |
| helix-cortex | feat/workflow2-ingestion-retrieval (stacked on feat/c1-worker; serves both workflows) | `ec6df64af9ce41009e57e046d28903012b45f875` | [#5](https://github.com/Pg-Helix/helix-cortex/pull/5) |
| helix-cortex | feat/real-embedding-provider (stacked on feat/workflow2-ingestion-retrieval; serves all three workflows/milestones) | `74213448fcd5d8521263098b6bd65db7885041ef` | [#6](https://github.com/Pg-Helix/helix-cortex/pull/6) |
| helix-foundry | feat/workflow2-pgvector (stacked on feat/phase1-local-stack; serves both workflows) | `85637c8068427329ec32622b1954b852a48bbd72` | [#5](https://github.com/Pg-Helix/helix-foundry/pull/5) |
| helix-foundry | feat/real-embedding-provider (stacked on feat/workflow2-pgvector) | `762407f8ac4b26c4c22fde4238cc1f55e62974ea` | [#6](https://github.com/Pg-Helix/helix-foundry/pull/6) |
| helix-labs | feat/workflow1-demo | this repo's own current `HEAD` -- not pinned here, since a commit adding/editing this file cannot name its own final SHA in advance | [#4](https://github.com/Pg-Helix/helix-labs/pull/4) |
| helix-labs | feat/workflow2-acceptance (stacked on feat/workflow1-demo) | this repo's own current `HEAD` -- not pinned here, for the same reason as above | [#5](https://github.com/Pg-Helix/helix-labs/pull/5) |
| helix-labs | feat/real-embedding-eval (stacked on feat/workflow2-acceptance) | this repo's own current `HEAD` -- not pinned here, for the same reason as above | [#6](https://github.com/Pg-Helix/helix-labs/pull/6) |

`prepare-checkouts.sh` only clones/pins `helix-foundry`, `helix-kernel` and `helix-cortex`
next to this repo -- the three repos either workflow's scripts actually check out or
build from directly (Foundry's Compose stack builds the kernel extension and runs the
cortex-api/worker images itself; helix-protocol and helix-infer are consumed as library
dependencies/contract docs by those builds -- `go.mod`/`go.sum` pin their exact commits
inside helix-cortex's own checkout -- not checked out separately by this manifest).

## Reproducing

```
sh prepare-checkouts.sh    # clones/pins helix-foundry, helix-kernel, helix-cortex
cd workflow-1 && ./acceptance.sh
cd ../workflow-2 && ./acceptance.sh
```
