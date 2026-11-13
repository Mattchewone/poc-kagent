# 0019. The kargo stack is built from this repo; `make up` brings up everything

- **Status:** Accepted (2026-11-13). Amends [0017](0017-kargo-delivery-and-pr-environments.md)
- **Origin:** Owner (a teammate clones the repo, runs `make up`, and gets the same flow: Gitea, Argo
  CD, Kargo). Which parts to bring in, and how, are implementation choices.

## Context

[0017](0017-kargo-delivery-and-pr-environments.md) put delivery on "the kargo stack", which was set
up outside this repo. `scripts/delivery-up.sh` refused to run until that stack was up, so anyone
else who cloned mock-poc had no way to get previews or releases.

mock-poc needs: `kargo-hub` (Argo CD, Kargo, Argo Rollouts for
AnalysisRuns), `kargo-nonprod`, `kargo-prod` and `kargo-registry`. Git is already in this repo
(poc-gitea, [0010](0010-gitea-local-pr.md)).

## Decision

- **[scripts/kargo-up.sh](../../scripts/kargo-up.sh)** (`make kargo`) builds exactly that. The
  kind configs are in [clusters/kargo-stack/](../../clusters/kargo-stack/), and the chart versions
  and node image are pinned in [scripts/versions.env](../../scripts/versions.env). It uses the pinned
  `.cache/bin/kind`, passes `helm --repo` so the user's helm repo list stays unchanged, and restores
  the kube context after `kind create`.
- **Same names as before**: `kargo-hub`, `kargo-nonprod`, `kargo-prod`, `kargo-registry`, and Argo CD
  cluster names `nonprod` / `prod`, so nothing else in the repo had to change. Re-runs are
  idempotent: existing nodes are started and installed charts are skipped.
- **No second Gitea, no extra clusters, no ingress controller.** demo-todo needs none of them.
- **`make up` = `doctor` → venv → poc cluster → kargo stack → delivery.**
  [scripts/doctor.sh](../../scripts/doctor.sh) checks the host tools and Docker memory first.
  `make stop` stops all four clusters. `make down` still deletes only the poc cluster, and
  `make kargo-down` deletes the three kargo clusters.
- The registry's self-signed cert is in `.cache/kargo-registry-certs/`, and its data is in the
  `kargo-registry` Docker volume.

## Consequences

- One clone, `cp .env.example .env`, `make up`. The first run takes about 15 min, mostly image pulls.
- Four kind clusters need about 12 GiB of Docker memory. `make doctor` warns when there is less.
- The kargo clusters run Kubernetes 1.33 and poc runs 1.37 (kagent needs it). They are pinned
  separately.

## Alternatives considered

- **Keep the stack outside the repo.** Every clone would need a separate setup step to get previews
  and releases.
- **Put Argo CD and Kargo on the poc cluster.** One cluster fewer, but previews and stages would
  share a node with kagent and the agents. It would also drop the hub → workload topology that 0017
  shows, and change every context in `worker/delivery.py`.
