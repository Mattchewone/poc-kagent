# Architecture

How a request becomes code running in prod. One Temporal workflow per request runs the whole thing:
intake, the coding agent's turns, verification, the PR and its preview environment, review, merge,
release build, and the Kargo rollout through dev → staging → prod. The reasoning behind each choice
is in [adr/](adr/README.md) (start with [0016](adr/0016-temporal-owns-the-work-item.md) and
[0017](adr/0017-kargo-delivery-and-pr-environments.md)). This page is the map.

## The flow in four steps

| # | Step | Who acts | What Temporal does |
|---|---|---|---|
| 1 | **Input**: chat with delivery-agent, or a Gitea issue with `/agent` (Jira later) | a person | starts a `WorkItemWorkflow` |
| 2 | **Agent works**: code + tests, verified in a fresh sandbox, PR published | dev-agent (Claude Code on Substrate) | invokes the agent over A2A, verifies each revision, retries and re-prompts, opens the PR |
| 3 | **Review the PR environment** on kargo-nonprod; feedback goes back to the agent | a person (Gitea or chat) | builds the PR image, deploys the preview via Argo CD, relays feedback, updates the preview |
| 4 | **Merge → build → GitOps rollout**: dev automatically, then staging and prod | a person merges and promotes; Kargo + Argo CD deploy | merges (or sees the webhook), builds the semver release, follows each Kargo stage until it's verified |

## Components and trust boundaries

```mermaid
flowchart LR
  user(["Developer<br/>(browser)"])

  subgraph host["Host machine (macOS, Docker Desktop)"]
    direction TB
    subgraph hostproc["Host processes · make dev"]
      pipeline["Pipeline server :8765<br/>MCP /mcp · /mcp-chat · /mcp-gated<br/>/hooks/gitea · work pages"]
      worker["Temporal worker<br/>WorkItemWorkflow + activities<br/>(docker build, kubectl, kagent CLI)"]
      temporal[("Temporal dev server<br/>:7233 · UI :8233")]
    end
    subgraph dockerc["Docker containers (kind network)"]
      gitea[("poc-gitea :3300<br/>poc/demo-todo (app)<br/>poc/demo-todo-gitops<br/>PRs · webhook")]
      kreg[("kargo-registry :5002<br/>demo-todo:semver<br/>demo-todo-pr:pr&lt;n&gt;-sha")]
      edge["edge (socat)<br/>:31081-31083, :311nn"]
    end

    subgraph poc["kind 'poc' · agents + verification"]
      direction TB
      subgraph kagentns["ns kagent"]
        ui["kagent UI :8082<br/>chat + Approve"]
        ctrl["kagent controller + API :8083<br/>A2A sessions"]
        chat["delivery-agent actor<br/>(chat front door)"]
        dev["dev-agent actor (gVisor)<br/>Claude Code + node<br/>one session per work item"]
      end
      sbx["OpenShell verify sandbox<br/>one per revision<br/>no network · no creds"]
    end

    subgraph kargo["kargo stack (../kargo)"]
      direction TB
      hub["kargo-hub<br/>Kargo project demo-todo<br/>Argo CD · Rollouts (analysis)"]
      nonprod["kargo-nonprod<br/>demo-todo-dev · demo-todo-staging<br/>demo-todo-pr-&lt;n&gt;"]
      prod["kargo-prod<br/>demo-todo-prod"]
    end
  end

  user -- "1 chat" --> ui --> ctrl --> chat
  user -. "1 issue /agent" .-> gitea
  chat -- "start_work · wait · feedback<br/>merge / promote (gated)" --> pipeline
  gitea -- "webhook: merged/closed,<br/>/agent comments, issues" --> pipeline
  pipeline -- "start · signal · update · query" --> temporal
  temporal <--> worker
  worker -- "2 A2A invoke (kagent CLI)" --> ctrl --> dev
  dev -- "git clone (read-only)" --> gitea
  dev -- "submit_revision" --> pipeline
  worker -- "verify each revision" --> sbx
  worker -- "2 branch + PR, comments, 4 merge" --> gitea
  worker -- "3 PR image · 4 release image" --> kreg
  worker -- "3 Argo CD app per PR · 4 Warehouse refresh, Promotions" --> hub
  hub -- "clone gitops, push stage/*" --> gitea
  hub -- sync --> nonprod
  hub -- sync --> prod
  kreg -. pull .-> nonprod
  kreg -. pull .-> prod
  user -- "3 review preview · 4 dev/staging/prod" --> edge
  edge --> nonprod
  edge --> prod
  user -- "review, merge" --> gitea

  classDef kagentc fill:#e8f0fe,stroke:#3b6fd4,color:#10224a
  classDef substratec fill:#e2f5f2,stroke:#0b8a7c,color:#073b35
  classDef openshellc fill:#fdf0e1,stroke:#b45f06,color:#4a2700
  classDef temporalc fill:#ebebfd,stroke:#4b4fd6,color:#1b1d5c
  classDef giteac fill:#e6f4ea,stroke:#1d8a4a,color:#0b3a1e
  classDef kargoc fill:#fbe9f3,stroke:#b03a7a,color:#4a0f2f
  classDef extc fill:#f4f4f5,stroke:#71717a,color:#18181b
  class ui,ctrl kagentc
  class chat,dev substratec
  class sbx openshellc
  class pipeline,worker,temporal temporalc
  class gitea giteac
  class hub,nonprod,prod,kreg kargoc
  class edge,user extc
```

Colours: blue kagent · teal Substrate actors · orange OpenShell · indigo orchestration (pipeline,
Temporal) · green Gitea · pink kargo stack · grey supporting pieces. Not shown: the model path (actors →
Substrate egress gateway, which injects the key → api.anthropic.com, see [0011](adr/0011-model-credentials.md)).
The rendered SVGs in [img/](img/) predate this flow ([0016](adr/0016-temporal-owns-the-work-item.md)).

What each boundary guarantees:

| Boundary | Holds | Cannot |
|---|---|---|
| **dev-agent actor** (Substrate, gVisor) | Claude Code, the repo checkout, the work item's session | see the model credential, push to Gitea, open/merge a PR, deploy anything, reach anything not in an EgressPolicy |
| **delivery-agent actor** | the chat | write code; merge or promote without a human Approve click |
| **OpenShell verify sandbox** | one revision: base snapshot + diff, test runs | make any outbound connection, read credentials, outlive its verification |
| **Temporal + worker** (host) | work item state, the only path to a PR, a merge, a release, a preview, a promotion | open a PR for an unverified revision, merge with feedback pending, promote out of order (validators) |
| **kagent approval** | the human click on `merge_pull_request` / `promote` | be skipped: those tools sit on a server bound with `requireApproval` |
| **Kargo** | which version is in which stage | promote to staging/prod Freight that wasn't verified upstream |

## A work item, end to end

```mermaid
sequenceDiagram
  autonumber
  actor Dev as Developer
  participant C as delivery-agent<br/>(kagent chat)
  participant P as Pipeline<br/>(MCP + webhook)
  participant T as Temporal<br/>WorkItemWorkflow
  participant A as dev-agent<br/>(A2A session)
  participant S as OpenShell<br/>verify sandbox
  participant G as Gitea
  participant K as Kargo + Argo CD<br/>(kargo clusters)

  Dev->>C: "Add a Clear completed button"
  C->>P: start_work
  P->>T: start workflow
  T->>A: run_agent: work item prompt (A2A invoke)
  A->>G: clone / fetch origin/main
  A->>A: code + tests, npm test
  A->>P: submit_revision(work_id, diff)
  P->>T: Update submit
  T->>S: fresh sandbox: apply diff, agent tests + restored base tests
  S-->>T: passed
  T-->>A: verification result (tool reply)
  A-->>T: turn ends with a summary
  T->>G: branch agent/ID + PR
  T->>K: build demo-todo-pr image, Argo CD app demo-todo-pr-N
  K-->>T: preview serves the new image
  T->>G: PR comment with the preview URL
  C-->>Dev: in review: preview + PR links (wait_for_work)
  Dev->>G: "/agent show the count on the button"
  G->>P: webhook (issue_comment)
  P->>T: Signal feedback
  T->>A: run_agent: feedback prompt (same session)
  Note over A,S: steps 6-11 again → revision 2
  T->>G: force-push branch (same PR)
  T->>K: new image, same Argo CD app → preview updated
  Dev->>C: "Looks good, merge it"
  C->>P: merge_pull_request (kagent asks for Approve)
  P->>T: Update merge
  T->>G: merge
  T->>K: delete preview app + namespace
  T->>K: build demo-todo:0.2.0, refresh Warehouse
  K->>K: Freight → dev auto-promoted → smoke test verified
  Dev->>C: "Promote to staging" (Approve)
  C->>P: promote(staging)
  P->>T: Update promote → Kargo Promotion
  K->>K: staging verified … prod the same way
  T-->>C: done: 0.2.0 live in prod
```

A merge in the Gitea UI instead of the chat reaches Temporal through the webhook (`pr_event`), with a
5-minute poll as the fallback. A promotion clicked in the Kargo UI is picked up by `wait_for_stage` just
the same.

## Work item lifecycle

```mermaid
stateDiagram-v2
  [*] --> agent_working: start (chat / issue)
  agent_working --> agent_working: revision failed, re-prompt (up to 3 turns)
  agent_working --> needs_input: agent asked a question / 3 failed turns
  needs_input --> agent_working: feedback
  agent_working --> opening_pr: latest revision passed
  opening_pr --> deploying_preview
  deploying_preview --> in_review: preview serves the PR image (or preview failed, noted)
  in_review --> agent_working: feedback (chat, /agent comment)
  in_review --> merging: merge (gated chat tool)
  merging --> building_release
  in_review --> building_release: merged in Gitea (webhook / poll)
  in_review --> closed: PR closed / abandon / 7 days idle
  needs_input --> closed: abandon / 7 days idle
  building_release --> rolling_out_dev
  rolling_out_dev --> awaiting_staging: verified in dev
  awaiting_staging --> awaiting_prod: promoted + verified
  awaiting_prod --> done: promoted + verified
  rolling_out_dev --> done: failed verification (recorded)
  done --> [*]
  closed --> [*]
```

Implemented in [worker/work.py](../worker/work.py). Activities are in
[worker/work_activities.py](../worker/work_activities.py) and [worker/delivery.py](../worker/delivery.py).
Verification reuses `start_app_env` / `verify_change` / `stop_app_env` from
[worker/change_activities.py](../worker/change_activities.py).

## Where things live

| Path | What |
|---|---|
| [worker/work.py](../worker/work.py) | `WorkItemWorkflow`: phases, handlers, validators |
| [worker/work_activities.py](../worker/work_activities.py) | activities: `run_agent`, `publish_pr`, `build_image`, `deploy_pr_env`, `release_image`, `wait_for_stage`, … |
| [worker/delivery.py](../worker/delivery.py) | image builds, registry, Argo CD preview apps, edges, Kargo status + promotions, kagent A2A |
| [worker/pipeline_server.py](../worker/pipeline_server.py) | MCP tools for both agents, Gitea webhook, intake, work pages |
| [worker/gitea.py](../worker/gitea.py) | branches, PRs, merges, comments on poc-gitea |
| [worker/apps.py](../worker/apps.py), [change_activities.py](../worker/change_activities.py) | OpenShell / docker verify sandboxes |
| [clusters/poc/kagent/](../clusters/poc/kagent/) | Harness, ModelConfig, EgressPolicy, RemoteMCPServers, dev-agent + delivery-agent and their prompts |
| [clusters/kargo/](../clusters/kargo/) | Kargo project demo-todo (Warehouse, stages, smoke test, promote task), Argo CD ApplicationSet |
| [deploy/gitops/](../deploy/gitops/) | the gitops repo pushed to `poc/demo-todo-gitops`: base, stages, preview overlay |
| [images/demo-todo/Dockerfile](../images/demo-todo/Dockerfile) | release + preview image of the app |
| [clusters/openshell/app-policy.yaml](../clusters/openshell/app-policy.yaml) | verify sandbox policy |
| [scripts/up.sh](../scripts/up.sh), [dev.sh](../scripts/dev.sh), [gitea-up.sh](../scripts/gitea-up.sh), [delivery-up.sh](../scripts/delivery-up.sh) | bring-up |
| [scripts/watch.py](../scripts/watch.py), [mcp_call.py](../scripts/mcp_call.py) | dashboard; call any MCP tool without kagent |
| [demo-app/](../demo-app/) | the target app (seeded into Gitea) |
