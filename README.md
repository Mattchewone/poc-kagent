# mock-poc: kagent + Substrate + OpenShell + Temporal + Kargo

Ask for a feature or a bug fix in chat (or in a Gitea issue). A Temporal workflow takes it from there
to prod. A real coding agent (Claude Code) implements it with tests, every revision is verified in a
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
             ─▶ Kargo: dev (auto) ─▶ staging ─▶ prod (promotions), each smoke-tested
```

| Piece | Role | Where |
|---|---|---|
| **Temporal** | owns each work item end to end: invokes the agent, verifies, opens the PR, deploys the preview, merges, builds, follows the rollout | `worker/work.py` |
| **kagent** 1.0.1 | agent definitions (CRDs), the chat UI, A2A sessions, the Approve gate on merge/promote | `clusters/poc/kagent/` |
| **Substrate** v1.0.0 (kagent's fork) | runs both agents as gVisor actors; suspends them between turns; egress gateway injects the model key | `kagent-default` WorkerPool |
| **OpenShell** 0.1.8 | verify sandboxes: landlock, no providers, no network | `clusters/openshell/app-policy.yaml` |
| **Gitea** (poc-gitea) | app repo, gitops repo, PRs, webhooks to the pipeline | `scripts/gitea-up.sh` |
| **Kargo + Argo CD** (the kargo stack, `../kargo`) | project `demo-todo`: PR previews on kargo-nonprod, releases dev → staging → prod | `clusters/kargo/`, `deploy/gitops/` |
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

```bash
make setup                         # python venv
echo 'ANTHROPIC_API_KEY=sk-ant-…' > .env    # the agents' model key (gitignored)
make up                            # poc cluster, kagent + Substrate, OpenShell, Gitea, both agents
make dev                           # temporal + worker + pipeline server on the host
../kargo/scripts/up.sh       # the kargo stack, if it isn't up
make delivery                      # Kargo project demo-todo, gitops repo, first release, stage URLs
make demo-ui                       # kagent, work items, Temporal, Gitea, Kargo, Argo CD, dev
```

For a guided walkthrough with what to watch at each step, see [docs/DEMO.md](docs/DEMO.md) and run
`make watch` alongside the chat. Diagrams are in [docs/architecture.md](docs/architecture.md).

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
.venv/bin/python scripts/mcp_call.py promote work_id=<id> stage=staging
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

`make down` deletes the cluster but keeps `kind-registry`, the image cache and Gitea.

### Gotchas found bringing it up

- **Pod CIDR vs Docker Desktop.** Calico's default pool `192.168.0.0/16` contains Docker Desktop's host address (`host.docker.internal` = `192.168.65.254`). Calico then treats traffic to the host as in-cluster, so pods time out reaching Gitea and the pipeline server even though the node can reach them. The cluster uses `10.244.0.0/16`.
- **Substrate pulls actor images itself.** The containerd mirror for `localhost:5001` doesn't help the atelet. `up.sh` sets `--localhost-registry-replacement=kind-registry:5000` on it, or the dev agent's golden build fails with "could not pull image".
- **The Claude harness accepts no Anthropic options** in ModelConfig except `baseUrl` (setting `maxTokens` makes the Agent `Compatible=False`).
- **The Claude harness gates whole MCP servers.** That's why `merge_pull_request` and `promote` are on their own server (`/mcp-gated`) with `requireApproval: true`.
- **OpenShell runs the sandbox command as its own user**, not the image's `USER`, so the supervisor works in `/tmp/work`.
- **`openshell forward start -d`** keeps the caller's stdout pipe open; the worker runs it with output to `/dev/null`.
- **kagent's A2A endpoint speaks A2A 1.0**: `SendMessage`, `ROLE_USER`, at `POST :8083/agents/kagent/dev-agent`.

## Layout (new pieces)

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
worker/work_activities.py       run_agent (A2A), publish_pr, build_image, deploy_pr_env, release_image, wait_for_stage, …
worker/delivery.py              docker builds, registry, Argo CD preview apps, edges, Kargo, kagent CLI
worker/change.py, change_activities.py   verify payloads + start/verify/stop env (ChangeWorkflow: superseded)
worker/apps.py                  verify envs: local docker | OpenShell (+ forward)
worker/gitea.py                 base tree, apply check, branch + PR, merge, comments
worker/pipeline_server.py       MCP (/mcp, /mcp-chat, /mcp-gated), Gitea webhook, work pages
scripts/up.sh                   single-cluster bring-up; tools.sh, images.sh, kagent-apply.sh, dev.sh, gitea-up.sh
scripts/delivery-up.sh          wire into the kargo stack (make delivery)
scripts/mcp_call.py             call any pipeline tool by hand
```

---

# Legacy: fake agent across two clusters (milestones 1-8)

Everything below predates the kagent stack and still works with `make legacy-up`.

One Temporal workflow (`FeatureWorkflow`) drives a scripted fake agent through write → test → fix,
on **OpenShell** sandboxes or **Substrate** v0.5.0 actors, chosen per run.

A third runtime, `local` (plain docker, `--network none`, `docker pause` as "suspend"), is added
so the workflow and every experiment can run with no cluster. It is not part of the comparison.

## Review gate

The agent starts work as soon as the run starts. When an attempt passes the tests, the workflow
saves the patch to `out/<id>.patch`, pauses the agent (Substrate suspends it; local runs
`docker pause`; OpenShell leaves the sandbox up) and waits for a reviewer:

- `make approve RUN=<id>`: result `approved`.
- `make reject RUN=<id> MSG="…"`: the comment goes back to the agent as feedback and it tries again.
  This uses one of the 3 attempts.
- `make reject RUN=<id>` with no `MSG`: result `rejected`. The run ends.

`approve` and `reject` wait until the run reaches review, so you can type them early. If the run
gives up or is cancelled first, they say so and exit.

## Quick start (no clusters)

```bash
make setup images test          # venv, image, milestone-4 unit tests
make temporal                   # terminal 1 — UI at http://localhost:8233
make worker-bg                  # worker in background, logs in .cache/worker.log
make run RUNTIME=local SCENARIO=happy     # prints a run id
make status RUN=<id>                      # "awaiting review" once tests pass; patch in out/<id>.patch
make approve RUN=<id>                     # or: make reject RUN=<id> [MSG="what to change"]
RUNTIMES=local make experiments           # all ten rows -> out/results.tsv
```

## Full POC, milestone by milestone

| # | Command | Exit check |
|---|---------|-----------|
| 1 | `make host-check` | tools present; node kernel ≥ 6.2 with landlock (on macOS this checks Docker Desktop's VM) |
| 2 | `make openshell-up && make openshell-check` | default-deny blocks pod-to-pod curl; `sandbox create -- echo ok` prints ok |
| 3 | `make substrate-up && make substrate-check` | counter increments through the router and survives suspend/resume |
| 4 | `make images test` | happy: buggy then fixed; cheat reports `tests_touched` |
| 5 | `make substrate-poc && RUNTIMES="openshell substrate" make adapters` | identical output on both; `make leftovers` empty |
| 6 | `make run RUNTIME=openshell SCENARIO=happy` + `make approve RUN=…` | `out/<id>.patch`; one failed + one passing attempt, then approved |
| 7 | same with `RUNTIME=substrate` | agent actor `ACTOR_STATE_SUSPENDED` while awaiting review |
| 8 | `make experiments` | every row below filled in |

**Combined runs.** `make combined` puts the agent on Substrate, where it suspends at no cost while its patch awaits review, and runs each test in a fresh OpenShell sandbox. The general form is `make run RUNTIME=<agent runtime> TEST_RUNTIME=<test runtime>`, and run ids for mixed pairs start with both runtimes' initials (e.g. `so-…`).

`make legacy-watch RUN=<id>` opens a tmux layout with a pane per layer: status query, worker log,
OpenShell sandboxes plus supervisor `NET:` lines, and Substrate actors plus worker pods.
`make down` deletes both clusters.

## Layout

```
agent/fake_agent.py      CLI mode (stdin/stdout JSON) and HTTP mode (:8080, or a port given as an argument)
agent/scenarios.py       happy | never | cheat | egress | slow
target/                  calc.add() with a planted bug + 3 tests
images/Dockerfile        python, pytest, git, curl, target/, agent/
clusters/openshell/      kind.yaml (no default CNI), values.yaml, sandbox-policy.yaml, netpol-check.yaml
clusters/substrate/      workerpool.yaml, agent-template.yaml, test-template.yaml, egress-policy.yaml
worker/runtimes/         base.py (Runtime protocol, Handle), openshell.py, substrate.py, local.py
worker/activities.py     start_env, implement (heartbeats), run_tests, pause_env, save_patch, stop_env
worker/workflow.py       FeatureWorkflow: 3-attempt loop, review_patch Update once tests pass, status Query, finally → stop_env
worker/start.py          start / approve / reject / status
worker/adapter_check.py  milestone 5
scripts/                 cluster bring-up, checks, port-forwards, watch.sh, experiment.sh, versions.env
```

## Pinned versions (`scripts/versions.env`)

| Component | Version | Notes |
|---|---|---|
| OpenShell CLI + chart | v0.1.8 (chart `0.1.8`) | chart `oci://ghcr.io/nvidia/openshell/helm-chart` |
| Agent Sandbox | v1.0.10 | `sandbox.yaml` release asset |
| Calico | v3.28.2 | |
| Substrate | **v0.5.0** | last tag with `hack/install-ate-kind.sh`; v1.0.0 replaced it with `ate-setup` and router headers with `kubectl ate proxy` |
| temporalio (Python) | 1.36.0 | |

## Where the build departs from the plan

- **Substrate router addressing.** v0.5.0 routes on `ate-target-actor: <atespace>/<actor>`, not a Host header. The Host format existed only in v0.1.0.
- **Substrate actors listen on :80.** The counter template's probe and the router's default port are both 80. The templates start the agent with `serve 80`.
- **ActorTemplate has no `readyz` or `ports` field in v0.5.0.** It uses `wakeupProbe.httpGet`, which is a one-shot gate on each resume.
- **Egress policy is per actor.** Substrate already denies all egress when an actor has no policy, so `egress-policy.yaml` (`rules: []`) is documentation plus an optional explicit check.
- **OpenShell exec.** v0.1.8 has `openshell sandbox exec -n NAME -- cmd` with piped stdin (4 MiB cap), so the adapter shells out to the CLI rather than using the Python SDK.
- **Scenario travels in the `implement` payload.** The attempt counter still lives only in the workspace.
- **`run_tests` creates and stops its own test env**, named `test-<run id>-<n>`.

## Verified on real clusters (milestones 2, 3, 5)

- OpenShell chart 0.1.8 with no TLS and no auth works on kind (`openshell status` connects).
- `sandbox create --detach --no-tty -- sleep infinity` keeps an idle sandbox, and `sandbox exec` with piped stdin works.
- The sandbox uses the image loaded with `kind load`.
- Landlock enforcement needed `/dev/null` added as read-write in the sandbox policy, because `git apply` opens it.
- Substrate's gVisor actors pull `localhost:5001/…@sha256` and serve on :80.
- The `durableDir` workspace keeps the attempt counter across suspend and resume.
- `kubectl ate get actors` column 4 is STATE.

## Gotchas found during bring-up

- **Exported `GOARCH`, `GOOS`, `CGO_ENABLED`, `CC` or `CXX` break Substrate's `ko` builds.** `substrate-up.sh` unsets them for its own run.
- **`WorkerPool.spec.workerImage` must be a resolved image, not `ko://`.** `substrate-poc.sh` copies the one the counter demo's pool uses.
- **Substrate won't delete a `RUNNING` actor.** The adapter suspends it first, then deletes.
- **ActorTemplate delete is asynchronous, and actors need the template's golden snapshot.** `substrate-poc.sh` waits for both.

## Experiments

Run with `make experiment EXP=<row> RUNTIME=<rt>`. Each run appends a line to `out/results.tsv`.

| Experiment | `EXP=` | Expected | local (docker) | OpenShell | Substrate |
|---|---|---|---|---|---|
| Happy path | `happy` | patch saved after attempt 2; approved | ✅ passed, ctr 1→2 | | |
| Egress denial | `egress` | curl fails in env; run passes | ✅ `curl: (6) Could not resolve host`; passed | | |
| Grader tamper | `cheat` | every attempt fails; `tests_touched` true | ✅ 3× fail, touched=True | | |
| Loop bound | `never` | "gave up after 3 attempts" | ✅ | | |
| Long review wait | `long-wait` (`LONG_WAIT=600`) | resumes cleanly; actor suspended while awaiting review | ✅ (40 s wait; container paused) | | |
| Rejection | `reject` | "rejected" at review; env deleted | ✅ leftovers=0 | | |
| Changes requested | `revise` | reviewer comment reaches the agent as feedback; attempt 3 reviewed and approved | | | |
| Worker crash | `crash` (needs `make worker-bg`) | heartbeat timeout, retry, passes | ✅ retry started 31 s after kill, same agent env, passed | | |
| State across attempts | `state` | attempt 2 sees attempt 1's counter, incl. after suspend | ✅ ctr 1→2 across `docker pause` | | |
| Cancel | `cancel` | `finally` runs; no envs left | ✅ CANCELED, leftovers=0 | | |
| Concurrency | `concurrency` | all pass; note pod counts | ✅ 5/5 | | |

The crash row also shows the agent's own counter at 2 on the retried attempt. The killed attempt
had already incremented it, which is exactly the "lost vs kept state" signal the counter is for.
