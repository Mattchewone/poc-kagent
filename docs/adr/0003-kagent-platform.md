# 0003. kagent for chat, agent definition and the approval gate

- **Status:** Accepted (2026-11-12)
- **Origin:** Owner ("work kagent in where it seems logical", then "kagent does the coding too")

## Context

The brief asked for a simple chat interface, a real coding agent, and a gated human approval before a
PR. Building a chat UI and an approval flow by hand was an option, but kagent 1.0.1 provides:

- a chat UI and an A2A 1.0 API for agents;
- agents, harnesses, model configs, egress policies and MCP servers as Kubernetes CRDs
  (`api.kagent.dev/v1alpha3`);
- `requireApproval` on an MCP tool binding: the run pauses and the UI shows Approve / Reject;
- its own Substrate install, which replaced our source build ([0002](0002-fast-startup.md)).

## Decision

kagent is the front door and the agent platform. Everything about the agent is declared in
[clusters/poc/kagent/dev-agent.yaml](../../clusters/poc/kagent/dev-agent.yaml) and applied by
`make kagent-apply`; the system prompt lives in
[dev-agent-prompt.md](../../clusters/poc/kagent/dev-agent-prompt.md) as a ConfigMap. The chat is the
kagent UI on :8082.

## Consequences

- No custom chat UI or approval UI to build or maintain.
- The agent is declarative and versioned with the repo.
- We inherit kagent 1.0.1's limits, each worked around and recorded elsewhere:
  - approval gates whole MCP servers, not single tools ([0009](0009-pipeline-as-mcp-with-gated-pr.md));
  - the Claude harness only sends credentials as `x-api-key` ([0011](0011-model-credentials.md));
  - the Claude harness rejects Anthropic options other than `baseUrl` (setting `maxTokens` makes the
    Agent `Compatible=False`);
  - kagent sandboxes can't expose a URL, so previews live elsewhere ([0006](0006-openshell-verify-and-preview.md)).
- kagent 1.0.1 was one day old when adopted; expect API churn.

## Alternatives considered

- **kagent as chat + approval only**, with a small intake agent (Haiku) driving a Claude Code agent
  in OpenShell on the owner's subscription. This was the first recommendation; the owner chose to have
  kagent do the coding as well ([0004](0004-claude-code-on-substrate.md)).
- **Custom chat page + approval UI.** More control, more code, and no agent platform underneath.
