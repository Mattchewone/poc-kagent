# 0013. Temporal, worker and pipeline run on the host, not in the cluster

- **Status:** Accepted (2026-11-12)
- **Origin:** Implementation

## Context

The orchestration pieces (Temporal dev server, the Python worker, the pipeline MCP server, and later
the auth proxy and main-branch app) change constantly during a POC. Running them in-cluster means an
image build and rollout per code change. The earlier POC already ran Temporal and the worker on the
host.

## Decision

`make dev` ([scripts/dev.sh](../../scripts/dev.sh)) runs them as background host processes with pid
files and logs in `.cache/`:

| Process | Port | Log |
|---|---|---|
| Temporal dev server (SQLite at `.cache/temporal.db`) | 7233, UI 8233 | `.cache/temporal.log` |
| worker (`worker/main.py`) | n/a | `.cache/worker.log` |
| pipeline server | 8765 | `.cache/pipeline.log` |
| claude auth proxy | 8766 | `.cache/claude-proxy.log` |
| app on main (`scripts/app.sh`) | 3000 | `.cache/app-main.log` |

`make dev APP_RUNTIME=local` runs the whole pipeline with docker previews and no cluster at all, for
working on the pipeline itself.

## Consequences

- Edit, restart one process, test: seconds, not an image build.
- The cluster depends on the host for MCP, Gitea and (in subscription mode) the model path
  ([0012](0012-cluster-networking.md)).
- Not the production shape. Moving the worker and pipeline into the cluster is mechanical (they are
  plain Python services with env-var config); Temporal would become a real deployment or Temporal
  Cloud.

## Alternatives considered

- **Everything in-cluster.** Closer to production, slower to iterate, and more bring-up time
  ([0002](0002-fast-startup.md)).
