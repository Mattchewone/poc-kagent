# 0019. The kargo stack is built from this repo; `make up` brings up everything

- **Status:** Accepted (2026-11-13). Amends [0017](0017-kargo-delivery-and-pr-environments.md)
- **Origin:** Owner (a teammate clones the repo, runs `make up`, and gets the same flow: Gitea, Argo
  CD, Kargo). Which parts to bring in, and how, are implementation choices.

## Context

[0017](0017-kargo-delivery-and-pr-environments.md) put delivery on "the kargo stack": a separate
Kargo demo at `../kargo` that wasn't under version control. It ran six kind clusters,
its own Gitea, an A/B gate on Traefik and its own demo app. `scripts/delivery-up.sh` refused to run
until that stack was up. Anyone else who cloned mock-poc had no way to get previews or releases.

mock-poc uses only part of that stack: `kargo-hub` (Argo CD, Kargo, Argo Rollouts for
AnalysisRuns), `kargo-nonprod`, `kargo-prod` and `kargo-registry`. Git is already in this repo
(poc-gitea, [0010](0010-gitea-local-pr.md)).

## Decision

- **[scripts/kargo-up.sh](../../scripts/kargo-up.sh)** (`make kargo`) builds exactly that part. The
  kind configs are in [clusters/kargo-stack/](../../clusters/kargo-stack/), and the chart versions
  and node image are pinned in [scripts/versions.env](../../scripts/versions.env). It uses the pinned
  `.cache/bin/kind`, passes `helm --repo` so the user's helm repo list stays unchanged, and restores
  the kube context after `kind create`.
- **Same names as before**: `kargo-hub`, `kargo-nonprod`, `kargo-prod`, `kargo-registry`, and Argo CD
  cluster names `nonprod` / `prod`. A machine already running the standalone stack reuses it: existing
  nodes are started, installed charts are skipped, and nothing in the `kargo-demo` project is touched.
  Nothing else in the repo had to change.
- **Left out:** the hub's Gitea, the three regional clusters, Traefik / Gateway API and the
  `kargo-demo` project. demo-todo uses none of them.
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
- `make kargo-down` also deletes the clusters the standalone kargo demo uses, if both run on one
  machine.

## Alternatives considered

- **Git submodule of the kargo demo.** It isn't a repo, and it would bring the six-cluster setup,
  the second Gitea and the A/B gate with it.
- **Put Argo CD and Kargo on the poc cluster.** One cluster fewer, but previews and stages would
  share a node with kagent and the agents. It would also drop the hub → workload topology that 0017
  shows, and change every context in `worker/delivery.py`.
