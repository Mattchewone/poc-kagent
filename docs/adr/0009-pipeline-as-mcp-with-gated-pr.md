# 0009. The pipeline is an MCP server; the PR tool is on its own gated server

- **Status:** Superseded by [0016](0016-temporal-owns-the-work-item.md) (2026-11-12). The idea stands (MCP for tools, a separate server for gated tools); the endpoints are now `/mcp` (dev-agent), `/mcp-chat` and `/mcp-gated` (delivery-agent: merge, promote)
- **Origin:** Implementation

## Context

The agent needs a way to hand over a change and get a verdict, and a way to ask for a PR. kagent
agents consume tools over MCP (`RemoteMCPServer`). kagent's `requireApproval` applies to a whole MCP
server binding in the Claude harness, not to individual tools on it. We want submitting and checking
status to be free, and opening a PR to need a human click.

## Decision

[worker/pipeline_server.py](../../worker/pipeline_server.py) (FastMCP, stateless HTTP, port 8765)
exposes two MCP endpoints:

| Endpoint | RemoteMCPServer | Tools | Approval |
|---|---|---|---|
| `/mcp` | `pipeline` (timeout 10 min) | `submit_change`, `get_change`, `abandon_change` | none |
| `/mcp-gated` | `pipeline-release` | `open_pull_request` | `requireApproval: true` |

- `submit_change` starts or updates the ChangeWorkflow and **blocks until that revision is verified**
  (up to 480 s), so the agent gets the verdict in the same tool call and can react immediately.
- `open_pull_request` sends the `approve` update; its validator is the second gate
  ([0008](0008-temporal-change-workflow.md)).
- The same server renders change pages (`/`, `/changes/<id>`) with revisions, test output and links.

**Two gates for a PR:** the human's Approve click in kagent, and Temporal's check that the change is
`preview ready`. Neither alone is enough: the click doesn't know what was verified, and the validator
doesn't know a human looked.

## Consequences

- The agent never gets Gitea write access; the MCP tools are its only route to a PR.
- Blocking `submit_change` keeps the agent's loop simple, at the cost of a long-lived tool call
  (needs the 10 min MCP timeout).
- If kagent adds per-tool approval, the two servers can merge.
- `scripts/mcp_call.py` calls the same tools from a terminal, which is how the pipeline was tested
  without an LLM.

## Alternatives considered

- **One server, approval on everything.** Every status check would need a click.
- **Approval implemented in the pipeline** (e.g. a link the human opens). Works, but duplicates what
  kagent's UI already does, inside the chat.
