# 0016. Temporal owns each work item end to end, and drives the agent

- **Status:** Accepted (2026-11-12). Supersedes [0008](0008-temporal-change-workflow.md); replaces the
  tool split in [0009](0009-pipeline-as-mcp-with-gated-pr.md)
- **Origin:** Owner (Temporal must be critical, not a test harness; the flow is intake → agent →
  PR environment → merge → GitOps rollout; the chat may merge). Mechanisms are implementation choices.

## Context

Under 0008, kagent was in charge. The agent ran in a chat session and called `submit_change` whenever
it decided to, and only that call started a `ChangeWorkflow`. Temporal only saw the middle of the flow:
verify → preview → approve → PR. It never saw the request, it didn't know when the agent stalled, the
workflow ended once the PR opened, and merge and deploy happened outside it (a `git fetch` poller in
`scripts/app.sh`). If the agent crashed, a review stalled, or a deploy failed, no durable record
showed it.

## Decision

One **`WorkItemWorkflow`** per request ([worker/work.py](../../worker/work.py)), workflow id =
work id. It runs from intake until the release is verified in prod, or the item is closed.

```
intake ─▶ run_agent ⇄ submit (verify) ─▶ publish_pr ─▶ build_image + deploy_pr_env ─▶ in review
              ▲                                                                      │
              └────────────── feedback (chat or a `/agent` PR comment) ◀────────────┤
                                                                                     ▼ merge
          wait_for_stage(prod) ◀─ wait_for_stage(staging) ◀─ wait_for_stage(dev) ◀─ release_image
```

- **Intake is a plain workflow start.** Chat (`start_work`) and Gitea issues (webhook, `/agent` in an
  issue) both call `start_work_item` in [worker/pipeline_server.py](../../worker/pipeline_server.py).
  Jira would be one more caller of the same function. Issue-sourced ids are `issue-<n>-…`, so a
  repeated webhook delivery can't start a duplicate.
- **Temporal drives the agent.** The `run_agent` activity invokes dev-agent over A2A with the kagent
  CLI (`kagent agent session create --request-id work-<id>` then `agent invoke`). Each work item gets
  one kagent session, so the Substrate actor keeps its checkout between turns. The activity heartbeats
  and is retried. A retry tells the agent it was interrupted, so it doesn't redo finished work.
- **The agent still submits through MCP.** `submit_revision(work_id, diff)` is the workflow's
  `submit` Update. The handler verifies right away (fresh OpenShell sandbox, [0006](0006-openshell-verify-and-preview.md),
  [0007](0007-verification-rule.md)) and returns the result, so the agent can fix and resubmit within
  the same turn. The validator rejects submissions unless an agent turn is running.
- **The workflow decides the rest.** After a turn, it checks whether the latest revision from that
  turn passed:
  - If it passed, the workflow opens or updates the PR and deploys its preview ([0017](0017-kargo-delivery-and-pr-environments.md)).
  - If it failed, the workflow re-prompts the agent with the failure output. After 3 failed turns it
    moves to `needs input`.
  - If nothing was submitted, the agent asked a question, and the workflow moves to `needs input`.
- **Review is a wait with three exits.** `feedback` (Signal) starts another agent turn, and the PR
  and preview update in place. `merge` (Update, from the chat's gated tool) merges via the Gitea API.
  `pr_event` (Signal, from the Gitea webhook) covers merges and closes done in the Gitea UI. A 5-minute
  `pr_state` poll is the fallback for a missed webhook.
- **Delivery is part of the same workflow.** On merge, the workflow tears down the preview, then
  `release_image` builds the merge commit as the next semver tag, pushes it, and refreshes the Kargo
  Warehouse. `wait_for_stage` then waits for each of dev, staging and prod. It is one long activity per
  stage that polls Kargo and heartbeats, which keeps workflow history small. dev is auto-promoted.
  staging and prod wait for a promotion, made through the Kargo UI or the gated `promote` Update.
- **Two agents.** `dev-agent` only codes, and only Temporal invokes it. `delivery-agent` is the chat
  front door: start, follow, give feedback, abandon. Its `merge_pull_request` and `promote` tools sit
  on a separate MCP server bound with `requireApproval`, so each one needs a human click in kagent.

| Handler | Kind | Caller |
|---|---|---|
| `submit` | Update (validator: agent turn running) | dev-agent via `submit_revision` |
| `feedback` | Signal | chat `request_changes`, `/agent` PR comment, `/agent` issue comment |
| `merge` | Update (validator: in review, no pending feedback) | chat `merge_pull_request` (gated) |
| `pr_event` | Signal | Gitea webhook: PR merged / closed |
| `promote` | Update (validator: that stage is next) | chat `promote` (gated) |
| `abandon` | Signal | chat `abandon_work` |
| `status` | Query | pages, chat tools, `make watch` |

## Consequences

- One Temporal history per request covers everything: the prompt, each agent turn and reply, each
  revision with its tests, the PR, every preview deploy, the merge, the release, and each stage
  verification. The Temporal UI and `/work/<id>` show all of it.
- An agent crash, a worker restart or a slow rollout is a retry or a wait. Nothing is lost.
- The gates are enforced by the server. A PR only opens for a verified revision. A merge is refused
  while feedback is pending. A promotion is refused unless that stage is next.
- Merging no longer waits for an approval click on PR creation: PRs open automatically once a revision
  is verified, because review now happens on the PR and its preview. The human gates are review and
  merge (in Gitea or a gated chat tool), plus the staging and prod promotions.
- A work item can live for days (review, then manual promotions). Polls are kept infrequent (5-minute
  PR fallback, stage waits as single heartbeating activities), so history stays in the low hundreds
  of events. A very long-lived item would want `continue_as_new`, which isn't implemented.
- `ChangeWorkflow` is still registered so earlier executions can finish, but nothing starts new ones.

## Alternatives considered

- **Keep the agent in charge and let Temporal pick up from the PR.** Smaller change, but it leaves the
  most failure-prone part (the agent loop) out of the durable record. The owner rejected it.
- **The agent returns the diff in its final A2A message.** Then the agent couldn't see verification
  results within its turn, and parsing diffs out of chat text is brittle. Keeping the submit tool as an
  Update gives it both.
- **Push callbacks instead of a blocking invoke** (`kagent agent invoke --push-url`). This would avoid
  holding an activity open for a whole turn, but needs a callback route and correlation. Worth doing
  if turns get long; heartbeats are enough for now.
