# Architecture Decision Records

One file per decision: the context, what was decided, what it costs, and what else was considered.
The system they add up to is drawn in [../architecture.md](../architecture.md).

**Origin** says who made the call. *Owner* means the project owner chose between presented options.
*Implementation* means it was decided while building, within the owner's choices. Those are the ones
most worth challenging.

| # | Decision | Origin | Status |
|---|---|---|---|
| [0001](0001-single-kind-cluster.md) | One kind cluster for kagent, Substrate and OpenShell | Owner | Accepted |
| [0002](0002-fast-startup.md) | Startup: stop/start, prebuilt installs, parallel steps, image tar cache | Owner (goal) / Implementation | Accepted |
| [0003](0003-kagent-platform.md) | kagent for chat, agent definition and the approval gate | Owner | Accepted |
| [0004](0004-claude-code-on-substrate.md) | Claude Code through kagent's Claude harness, on a Substrate actor | Owner | Accepted |
| [0005](0005-agent-and-verifier-separation.md) | One agent sandbox for code + tests; a separate verifier | Implementation (owner's question) | Accepted |
| [0006](0006-openshell-verify-and-preview.md) | Verify and preview in the same fresh OpenShell sandbox | Implementation | Accepted, open to revisit |
| [0007](0007-verification-rule.md) | Verification: agent's tests and the original tests, both must pass | Implementation | Accepted |
| [0008](0008-temporal-change-workflow.md) | A Temporal workflow owns each change | Implementation | Superseded by 0016 |
| [0009](0009-pipeline-as-mcp-with-gated-pr.md) | The pipeline is an MCP server; the PR tool is on its own gated server | Implementation | Superseded by 0016 |
| [0010](0010-gitea-local-pr.md) | Local Gitea is the source of truth and the PR target | Owner | Accepted, amended by 0017 |
| [0011](0011-model-credentials.md) | Gateway-injected credentials; API key for real use, subscription via a local proxy | Owner | Accepted |
| [0012](0012-cluster-networking.md) | Calico on 10.244.0.0/16; host services via host.docker.internal | Implementation | Accepted |
| [0013](0013-host-side-control-plane.md) | Temporal, worker and pipeline run on the host, not in the cluster | Implementation | Accepted |
| [0014](0014-demo-app.md) | Zero-dependency Node todo app with a planted bug | Implementation | Accepted |
| [0015](0015-observability.md) | `make watch` dashboard and an always-on main-branch app | Owner (goal) / Implementation | Accepted, amended by 0017 |
| [0016](0016-temporal-owns-the-work-item.md) | Temporal owns each work item end to end, and drives the agent over A2A | Owner / Implementation | Accepted |
| [0017](0017-kargo-delivery-and-pr-environments.md) | Delivery through the kargo stack: PR environments on kargo-nonprod, Kargo rolls out releases | Owner / Implementation | Accepted |

To add one: copy the shape of any record, take the next number, and add a row here. Supersede rather
than rewrite: mark the old record `Superseded by NNNN` and link both ways.
