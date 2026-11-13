# mock-poc: kagent + Substrate + OpenShell + Temporal + Kargo

Ask for a feature or a bug fix in chat (or in a Gitea issue). A Temporal workflow takes it from there
to a released image. A real coding agent (Claude Code) implements it with tests, every revision is verified in a
fresh sandbox, a PR opens with its own preview environment, and once you merge, Kargo rolls the
release out through dev → staging → prod.

```
 1 input   chat (delivery-agent) / Gitea issue with /agent ──▶ Temporal WorkItemWorkflow
 2 agent   Temporal ─A2A─▶ dev-agent: Claude Code on a Substrate actor, clones, codes, tests
             ◀─ submit_revision ─ verify in a fresh OpenShell sandbox (no network, base tests restored)
             passed ─▶ branch + PR on Gitea
 3 review  Temporal builds the PR image ─▶ Argo CD app ─▶ preview namespace on kargo-nonprod
             feedback (chat, /agent PR comment) ─▶ another agent turn ─▶ same PR, same preview, updated
 4 ship    merge (chat tool behind an Approve click, or Gitea) ─▶ Temporal builds demo-todo:<semver>
             (workflow done) ─▶ Kargo: dev (auto) ─▶ staging ─▶ prod (promotions), each smoke-tested
```

| Piece | Role | Where |
|---|---|---|
| **Temporal** | owns each work item from request to release: invokes the agent, verifies, opens the PR, deploys the preview, merges, builds the release | `worker/work.py` |
| **kagent** 1.0.1 | agent definitions (CRDs), the chat UI, A2A sessions, the Approve gate on merge/promote | `clusters/poc/kagent/` |
| **Substrate** v1.0.0 (kagent's fork) | runs both agents as gVisor actors; suspends them between turns; egress gateway injects the model key | `kagent-default` WorkerPool |
| **OpenShell** 0.1.8 | verify sandboxes: landlock, no providers, no network | `clusters/openshell/app-policy.yaml` |
| **Gitea** (poc-gitea) | app repo, gitops repo, PRs, webhooks to the pipeline | `scripts/gitea-up.sh` |
| **Kargo + Argo CD** (the kargo stack: kargo-hub, kargo-nonprod, kargo-prod) | Argo CD: PR previews on kargo-nonprod · Kargo: owns the release rollout dev → staging → prod | `scripts/kargo-up.sh`, `clusters/kargo-stack/`, `clusters/kargo/`, `deploy/gitops/` |
| **demo-todo** | the target app: zero-dependency Node todo app | `demo-app/` |

Why it's built this way: [docs/adr/](docs/adr/README.md), especially
[0016](docs/adr/0016-temporal-owns-the-work-item.md) (Temporal owns the work item) and
[0017](docs/adr/0017-kargo-delivery-and-pr-environments.md) (Kargo delivery and PR environments).

### Why one agent sandbox and a separate verify sandbox

The agent writes the fix *and* its tests and runs them as it goes. That loop is what makes a coding
agent good, so splitting "fix" and "tests" into two agents adds latency for nothing. Verification is
different: it must not trust the agent. Each submitted revision gets a fresh sandbox with no network
and no credentials, the diff is applied to a clean checkout of `main`, and two suites must pass:

1. the agent's tree as written, so the tests it added count;
2. a copy with every base file under `tests/` restored, so it cannot weaken existing tests
   (changed/removed lines in base test files are also flagged as `weakened`).

Verification only decides whether a revision may become a PR. What you review is the PR's own
environment on kargo-nonprod, built and deployed the way releases are.

## Quick start

Everything is in this repo; nothing else needs cloning.

```bash
make doctor                        # checks docker, kubectl, helm, uv, temporal, openshell, node
cp .env.example .env               # then set ANTHROPIC_API_KEY (gitignored)
make up                            # venv, poc cluster (kagent, Substrate, OpenShell), Gitea, both agents,
                                   #   kargo stack (Argo CD, Kargo), gitops repo, first release, stage URLs
make dev                           # temporal + worker + pipeline server on the host
make demo-ui                       # kagent, work items, Temporal, Gitea, Kargo, Argo CD, dev
```

Fresh machine: about 15 min on the first run, mostly image pulls. Give Docker 12+ GiB of memory
(four kind clusters). `make stop` / `make up` pause and resume everything. `make poc-up`, `make kargo`
and `make delivery` run one part at a time. If you already run the standalone kargo demo stack, its
`kargo-hub` / `kargo-nonprod` / `kargo-prod` clusters and `kargo-registry` are reused as they are.

For a guided walkthrough with what to watch at each step, see [docs/DEMO.md](docs/DEMO.md) and run
`make watch` alongside the chat. Diagrams are in [docs/architecture.md](docs/architecture.md). Who acts as whom: [docs/identity.md](docs/identity.md).

In the kagent UI pick **delivery-agent** and try:

- *"Add a 'Clear completed' button."*
- *"Let me filter the list by all / active / done."*

It starts a work item and follows it. When the PR and its preview are ready
(http://localhost:311NN), try the preview and ask for tweaks in chat or with a `/agent …` comment on the
PR. Say *"merge it"* and approve the tool call (or merge in Gitea, http://localhost:3300/poc/demo-todo,
login `poc` / `poc-password-local`). The release reaches dev (http://localhost:31081) on its own. Ask
it to promote to staging (:31082) and prod (:31083), or promote in the Kargo UI
(https://localhost:31444, password `admin`).

Set the key later without reinstalling: `make kagent-key`. Change the model (both agents): `DEV_AGENT_MODEL=… make kagent-apply`.

### API key or Claude subscription

`ANTHROPIC_API_KEY` in `.env` can hold either credential; `make kagent-key` picks the mode from the prefix:

| Credential | How it flows | Use for |
|---|---|---|
| `sk-ant-api…` (console.anthropic.com) | gateway injects `x-api-key` → api.anthropic.com | real deployments |
| `sk-ant-oat…` (`claude setup-token`) | gateway injects `x-api-key` → host proxy on :8766 re-sends it as `Authorization: Bearer` → api.anthropic.com | local experiments |

kagent 1.0.1 can only deliver the Anthropic credential as `x-api-key`, which subscription tokens are
rejected on. For a token, `make kagent-key` sets the ModelConfig's `anthropic.baseUrl` to
[worker/claude_auth_proxy.py](worker/claude_auth_proxy.py) (started by `make dev`). The proxy stores
nothing: it re-encodes the token the gateway injected, so the actor still never sees it. Switching to an API
key is just a new value plus `make kagent-key`: the baseUrl is removed and the proxy stops being used.
Its log (`.cache/claude-proxy.log`) shows the auth mode per request, which helps with "Invalid API key".
Subscription use is subject to your plan's limits and Anthropic's terms; keep it to local experiments.

### Driving it without kagent

Every MCP tool can be called directly, which is handy for testing the workflow:

```bash
.venv/bin/python scripts/mcp_call.py start_work title="Clear completed" kind=feature description="..."
.venv/bin/python scripts/mcp_call.py wait_for_work work_id=<id>
.venv/bin/python scripts/mcp_call.py request_changes work_id=<id> feedback="..."
.venv/bin/python scripts/mcp_call.py merge_pull_request work_id=<id>
.venv/bin/python scripts/mcp_call.py release_status version=0.2.0
.venv/bin/python scripts/mcp_call.py promote version=0.2.0 stage=staging
```

`make dev APP_RUNTIME=local` verifies in plain docker instead of OpenShell. The agent turns still
need kagent, and the previews and releases still need the kargo stack.

## Startup

Measured on this machine (Docker Desktop, arm64, slow-ish link):

| Path | Time | What happens |
|---|---|---|
| `make start` after `make stop` | **~2 min** to every pod Ready (cluster API in ~10 s) | `docker start` of the node; every install step sees its work done and skips; waits on real pod readiness (the kagent controller races Postgres for a few seconds after a restart) |
| `make up` on a fresh cluster, image cache warm | **~6 min** | kind create → load ~22 cached images from disk (30 s) → Calico → kagent + OpenShell in parallel → images, forwards, Gitea, dev agent |
| `make up`, nothing cached | ~10-40 min | dominated by image pulls; one stalled `calico/node` pull alone cost ~15 min on the first run |
| Before (two clusters) | >10 min every time | two kind clusters in series, Substrate compiled from source with `ko` (~90 s of builds, 52 s Postgres, golden snapshots), deleted by `make down` |

What changed:

- **One cluster** instead of two; kagent and OpenShell install **in parallel**.
- **No source builds**: `kagent install` brings Substrate v1.0.0 as prebuilt helm charts.
- **Stop, don't delete**: `make stop` / `make start` keep everything, including Gitea PRs and the dev agent's golden snapshot.
- **Image cache** (`scripts/image-cache.sh`): after each `make up`, images in the node are exported to `.cache/image-cache/`, and a fresh cluster loads them from disk. Digest-pinned images (kagent, Substrate, OpenShell) can't be exported from containerd here and still download on a fresh cluster. Registry pull-through caches were tried first and dropped: the `registry:2` proxy stalled on large layers.
- **Pinned tools** in `.cache/bin` (`kind` v0.33, `kagent` CLI). Your global `kind` and current kube context are left alone.

`make down` deletes the poc cluster but keeps `kind-registry`, the image cache, Gitea and the kargo stack. `make kargo-down` deletes the three kargo clusters.

### Gotchas found bringing it up

- **Pod CIDR vs Docker Desktop.** Calico's default pool `192.168.0.0/16` contains Docker Desktop's host address (`host.docker.internal` = `192.168.65.254`). Calico then treats traffic to the host as in-cluster, so pods time out reaching Gitea and the pipeline server even though the node can reach them. The cluster uses `10.244.0.0/16`.
- **Substrate pulls actor images itself.** The containerd mirror for `localhost:5001` doesn't help the atelet. `up.sh` sets `--localhost-registry-replacement=kind-registry:5000` on it, or the dev agent's golden build fails with "could not pull image".
- **The Claude harness accepts no Anthropic options** in ModelConfig except `baseUrl` (setting `maxTokens` makes the Agent `Compatible=False`).
- **The Claude harness gates whole MCP servers.** That's why `merge_pull_request` and `promote` are on their own server (`/mcp-gated`) with `requireApproval: true`.
- **OpenShell runs the sandbox command as its own user**, not the image's `USER`, so the supervisor works in `/tmp/work`.
- **`openshell forward start -d`** keeps the caller's stdout pipe open; the worker runs it with output to `/dev/null`.
- **kagent's A2A endpoint speaks A2A 1.0**: `SendMessage`, `ROLE_USER`, at `POST :8083/agents/kagent/dev-agent`.

## Layout

```
demo-app/                       target app (node:http, node:test, no deps) + CLAUDE.md for the agent
images/app/                     verify supervisor (POST /__poc/verify)
images/demo-todo/Dockerfile     release + PR preview image of the app (platform-owned)
images/harness/                 kagent's Claude harness image + node
clusters/poc/kind.yaml          single cluster, containerd mirrors
clusters/poc/kagent/            Harness, ModelConfig, EgressPolicy, 3× RemoteMCPServer, dev-agent + delivery-agent, prompts
clusters/openshell/app-policy.yaml   verify sandbox policy (no egress)
clusters/kargo/                 Kargo project demo-todo + Argo CD ApplicationSet (applied to kargo-hub)
deploy/gitops/                  pushed to poc/demo-todo-gitops: base, stages/{dev,staging,prod}, preview
worker/work.py                  WorkItemWorkflow (deterministic) + payloads
worker/work_activities.py       start/verify/stop env, run_agent (A2A), publish_pr, build_image, deploy_pr_env, release_image, …
worker/delivery.py              docker builds, registry, Argo CD preview apps, edges, Kargo, kagent CLI
worker/shell.py                 subprocess helper
worker/apps.py                  verify envs: local docker | OpenShell (+ forward)
worker/gitea.py                 base tree, apply check, branch + PR, merge, comments
worker/pipeline_server.py       MCP (/mcp, /mcp-chat, /mcp-gated), Gitea webhook, work pages
scripts/up.sh                   single-cluster bring-up; tools.sh, images.sh, kagent-apply.sh, dev.sh, gitea-up.sh
scripts/delivery-up.sh          wire into the kargo stack (make delivery)
scripts/mcp_call.py             call any pipeline tool by hand
```
