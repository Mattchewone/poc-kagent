---
name: poc-decisions
description: Guardrails from this repo's ADRs (Temporal owns the work item, agent/verifier split, gated tools, identity "agent on behalf of user", Kargo delivery, credential handling). Use before designing or changing anything in worker/, clusters/, scripts/, images/, demo-app/ or docs/adr/, and when asked how the flow should work.
---

# Working within mock-poc's decisions

The ADRs in `docs/adr/` are binding. Before a change, check it against the rules below.

- **Breaking a rule:** stop and tell the user which rule and ADR it touches. If they agree, write a
  new ADR that supersedes the old one (mark the old one `Superseded by NNNN`, link both ways, add a
  row to `docs/adr/README.md`) in the same change as the code.
- **Making a new decision:** add an ADR with the next number. Origin is `Owner` if the user chose,
  `Implementation` if you did.

## Flow

- **Temporal owns every work item end to end** ([0016](../../../docs/adr/0016-temporal-owns-the-work-item.md)).
  - Intake (chat, Gitea issue, later Jira) goes only through `start_work_item` in
    `worker/pipeline_server.py`.
  - The workflow decides when a PR opens, the preview deploys, the merge happens and the release
    builds. Agents never decide those.
- **dev-agent only codes, and only Temporal invokes it**, with one kagent session per work item.
  - It can't push. Its only way to ship is `submit_revision`.
- **delivery-agent is the chat front door.** It writes no code and has no forge credentials.
- **Consequential tools sit on a separate MCP server bound with `requireApproval`** (merge,
  promote). The Claude harness gates whole servers, not single tools. New dangerous tools go on
  `/mcp-gated`, never on `/mcp` or `/mcp-chat`.
- **Gates are enforced server-side** by workflow update validators: a PR only for a verified
  revision, no merge while feedback is pending, a promotion only to the next stage. Never rely on a
  prompt for a gate.

## Verification

- **The agent works in one Substrate actor for the whole work item**, suspended while waiting
  ([0004](../../../docs/adr/0004-claude-code-on-substrate.md), [0005](../../../docs/adr/0005-agent-and-verifier-separation.md)).
- **The PR gate trusts only the independent verifier**
  ([0006](../../../docs/adr/0006-openshell-verify-and-preview.md), [0007](../../../docs/adr/0007-verification-rule.md)):
  - a fresh OpenShell sandbox, with no network and no credentials;
  - the diff only, applied to a clean `main`;
  - the agent's tests and the restored original tests must both pass.
  - Never accept test results produced in the agent's sandbox as the gate.

## Delivery

- **Gitea (poc-gitea) is the source of truth** for the app and the gitops repo
  ([0010](../../../docs/adr/0010-gitea-local-pr.md)).
- **PR previews and releases go through Kargo / Argo CD**
  ([0017](../../../docs/adr/0017-kargo-delivery-and-pr-environments.md)):
  - dev is auto-promoted;
  - staging and prod need a promotion.
  - Don't add side channels that deploy outside GitOps.

## Identity ([0018](../../../docs/adr/0018-agent-identity-on-behalf-of-user.md), [identity.md](../../../docs/identity.md))

- **The IdP owns people.** Don't add user lists or user tables anywhere else.
- **Agents act under their own identity** (actor JWT to the pipeline, the `dev-agent` bot in Gitea).
  Never give an agent or a sandbox user, Gitea or Kargo credentials.
- **Record an actor, plus a principal when a human caused the action:** `dev-agent (on behalf of
  @user)`. That means commit trailers `On-behalf-of:` / `Work-Item:`, "Requested by" on PRs, and
  "on behalf of" on merges and comments.
- **Verify the principal once, at intake**, against the IdP's JWKS. kagent doesn't verify
  signatures. Store it as `requested_by` on the workflow. Every later step reads it from Temporal,
  never from agent output or free text.
- **Every handler that a human triggers carries `by`.** Before acting for a user, check that the
  user could do it themselves.
- An MCP server takes `actorJwt` **or** `tokenPassthrough`, not both. Agent-facing servers carry the
  agent identity; chat-facing servers carry the user's.
- **Status:** design only. Until phase 1 lands, writes use the `poc` admin. Don't spread that: new
  code should take the actor and principal as parameters.

## Credentials and environment

- **Never read or print `.env` or key values.** Scripts may `source` it.
- **The model key reaches the agent only via the egress gateway**
  ([0011](../../../docs/adr/0011-model-credentials.md)). Use API keys for real deployments; the
  subscription token via `worker/claude_auth_proxy.py` is for local use only.
- **Never change the user's global kube context or kind binary.** Use explicit
  `--context kind-poc` (and the kargo contexts) and `.cache/` kubeconfigs.
- **Control-plane processes run on the host via `make dev`**
  ([0013](../../../docs/adr/0013-host-side-control-plane.md)). Cluster workloads reach them at
  `host.docker.internal`.
- **Tests:** `make test` (or `pytest tests`). Never run from the repo root.
