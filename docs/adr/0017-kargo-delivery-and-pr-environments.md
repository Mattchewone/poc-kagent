# 0017. Delivery through the kargo stack: PR environments on kargo-nonprod, Kargo rolls out releases

- **Status:** Accepted (2026-11-12). Amends [0010](0010-gitea-local-pr.md) (poc-gitea is now the one
  Gitea for the agent flow, including gitops) and [0015](0015-observability.md) (the main-branch app
  is no longer how changes reach users)
- **Origin:** Owner (tie into ../kargo as a new project and namespaces, PR previews on
  kargo-nonprod, the full dev → staging → prod flow, Temporal does the build, one Gitea). Mechanisms
  are implementation choices.

## Context

Before this, "deploy" meant [scripts/app.sh](../../scripts/app.sh) noticing a new commit on `main` and
restarting `node` on :3000. PR previews were the OpenShell verify sandboxes, deleted the moment the PR
opened, so nobody could review a PR against a running environment. The machine also runs a separate
Kargo demo: six kind clusters with Argo CD, Kargo and Argo Rollouts, and its own Gitea and registry.

## Decision

A new Kargo **project `demo-todo`** on the existing kargo clusters. Nothing in the `kargo-demo`
project changes. Everything is applied by [scripts/delivery-up.sh](../../scripts/delivery-up.sh)
(`make delivery`).

| Piece | Where | Defined in |
|---|---|---|
| Release images `demo-todo:<semver>` | `kargo-registry` (host `localhost:5002`) | built by `release_image` (Temporal) |
| Preview images `demo-todo-pr:pr<n>-<sha>` | same registry, separate repo, never Freight | built by `build_image` (Temporal) |
| Dockerfile | owned by the platform, not the app repo | [images/demo-todo/Dockerfile](../../images/demo-todo/Dockerfile) |
| Gitops repo `poc/demo-todo-gitops` | poc-gitea | [deploy/gitops/](../../deploy/gitops/) |
| Warehouse, stages, smoke test, promote task | kargo-hub, namespace `demo-todo` | [clusters/kargo/project.yaml](../../clusters/kargo/project.yaml) |
| Argo CD apps `demo-todo-{dev,staging,prod}` | kargo-hub → nonprod / prod | [clusters/kargo/argocd-apps.yaml](../../clusters/kargo/argocd-apps.yaml) |
| Preview apps `demo-todo-pr-<n>` | kargo-hub → kargo-nonprod, ns `demo-todo-pr-<n>` | created by `deploy_pr_env` ([worker/delivery.py](../../worker/delivery.py)) |

**Stages:**

| Stage | Cluster | Namespace | URL | Promotion |
|---|---|---|---|---|
| dev | kargo-nonprod | demo-todo-dev | http://localhost:31081 | automatic |
| staging | kargo-nonprod | demo-todo-staging | http://localhost:31082 | Kargo UI or chat `promote` |
| prod | kargo-prod | demo-todo-prod | http://localhost:31083 | Kargo UI or chat `promote` |
| PR *n* | kargo-nonprod | demo-todo-pr-*n* | http://localhost:311*nn* | each verified revision |

- **PR environments are GitOps too.** For each verified revision, Temporal builds the PR head into a
  preview image, then applies an Argo CD Application. The Application points at the gitops repo's
  `preview/` overlay and overrides the namespace, image tag and NodePort. A later revision updates the
  same Application, so the preview URL stays the same for the life of the PR. The activity only
  completes once `/version.txt` on the preview returns the new tag, so "preview ready" means the new
  code is actually being served. Merge, close and abandon all delete the Application (with the cascade
  finalizer), the namespace and the edge container.
- **Releases follow the existing Kargo pattern.** A semver tag in the registry becomes Freight. Each
  promotion renders `stages/<stage>` into the `stage/<stage>` branch and syncs the Argo CD app. The
  `smoke-test` AnalysisTemplate passes once the stage serves the promoted version (`/version.txt`)
  and `/api/stats` answers. staging only takes Freight verified in dev, and prod only Freight verified
  in staging.
- **Version bump:** a `feature` work item bumps the minor version and a `bug` bumps the patch.
  `delivery-up.sh` builds 0.1.0 from `main` when the registry has no release yet.
- **One Gitea: poc-gitea.** It holds the app repo, the gitops repo, PRs and webhooks. It joins the
  `kind` Docker network, so Kargo and Argo CD reach it as `http://poc-gitea:3000`. The poc cluster's
  agents and the host keep using `localhost:3300` / `host.docker.internal:3300`. The kargo hub's own
  Gitea stays as it is, serving only the `kargo-demo` project.
- **Host access through edge containers.** The kargo clusters' kind configs only map the `kargo-demo`
  ports, and adding mappings would mean recreating them. Instead, a small `alpine/socat` container per
  port (`demo-todo-edge-<port>`, on the kind network) forwards `127.0.0.1:<port>` to the node's
  NodePort. Kargo's verification doesn't use them: it calls `kargo-<cluster>-control-plane:<port>`
  directly, the same way `kargo-demo` does.

## Consequences

- A release reaches dev quickly. For the 0.1.0 bootstrap it took about 30 s from push to verified in
  dev (Warehouse refresh, promotion, 15-second canary, 10-check smoke test), plus the image build. Every step shows up in the Kargo UI, the Argo CD UI and the work item's
  Temporal history.
- The agent can't change how the app is built or deployed. The Dockerfile and manifests live in this
  repo and the gitops repo, neither of which the agent can reach.
- mock-poc now depends on the kargo stack for previews and delivery. Without it, `deploy_pr_env` fails
  and the work item records `preview failed` and stays reviewable through the PR. Verification and PRs
  still work. `make dev APP_RUNTIME=local` remains available for pipeline work without any cluster.
- poc-gitea was recreated once to allow webhooks to private addresses (`ALLOWED_HOST_LIST=*`). Its
  data volume carried over.
- The in-memory todo store means one replica per environment. The canary step is a short side-by-side
  rather than a weighted split.

## Alternatives considered

- **Move demo-todo into the kargo hub's Gitea.** That would mean one Gitea on the machine, but it runs
  on an `emptyDir`, so every PR and the app's history would vanish when the pod restarts.
- **Argo CD's pull-request ApplicationSet generator** for previews. It's declarative, but it polls, and
  it would race the image build: the PR head exists before its image does. Temporal creating the
  Application after the image is pushed avoids that, and lets it wait for the preview to be ready.
- **Keep the OpenShell sandbox as the PR preview.** Smallest change, but it isn't how the app runs in
  any real environment, and it can't outlive a worker restart cleanly. It is still the verifier.
- **Gitea Actions for the build.** More realistic CI, but the build would happen outside Temporal's
  view. The owner chose Temporal.
