# mock-poc: Temporal + OpenShell + Substrate

One Temporal workflow (`FeatureWorkflow`) drives a scripted fake agent through write → test → fix,
on **OpenShell** sandboxes or **Substrate** actors, chosen per run. No model, no GitHub, no LiteLLM.

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

`make watch RUN=<id>` opens a tmux layout with a pane per layer: status query, worker log,
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
