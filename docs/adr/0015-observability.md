# 0015. `make watch` dashboard and an always-on main-branch app

- **Status:** Accepted (2026-11-12), amended by [0017](0017-kargo-delivery-and-pr-environments.md): releases reach users through Kargo stages (:31081-31083); the main-branch app on :3000 is only a quick look at `main`
- **Origin:** Owner asked to see actors "working up and down" alongside Temporal and Gitea, in
  sections, and to see the app before asking for a change; the mechanisms are implementation choices

## Context

- Substrate actors are gVisor sandboxes inside the `kagent-default` worker pod. They never appear as
  pods, so k9s and `kubectl` can't show them. kagent 1.0.1 has no actor listing; `kagent agent
  session list` shows sessions but not whether their actor is running.
- atelet logs a `Handle RPC` line per lifecycle call, naming the actor (`session-<id>`): `Run`
  (create), `Restore` (wake), `Checkpoint` (suspend), `Terminate`.
- OpenShell sandboxes *are* pods (`openshell` namespace, `default--p-<hash>-<rev>`).
- Previews show unmerged changes; there was no view of the app as it is on `main`.

## Decision

- **`make watch`** ([scripts/watch.py](../../scripts/watch.py), `rich`): full-screen, read-only
  dashboard with panels for Substrate actors (from a followed atelet log), Temporal ChangeWorkflows
  (CLI + pipeline API for phase), OpenShell sandbox pods (state, change, preview URL) and Gitea (PRs,
  commits on `main`), plus a timeline built from atelet events and diffs between polls. Each source is
  polled in a background thread; a failing source shows ⚠ and keeps its last data.
- **`make k9s`** opens on the `openshell` namespace, where the sandboxes are.
- **`make app`** ([scripts/app.sh](../../scripts/app.sh)): Gitea `main` on :3000, polled every 5 s
  and restarted when `main` moves, so a merge shows up without a manual step. Started by `make dev`.
- Walkthrough in [../DEMO.md](../DEMO.md).

## Consequences

- One terminal shows every hop: actor wakes, sandbox created, preview ready, PR opened, main updated.
- Actor state depends on atelet's log format, an internal detail that may change between Substrate
  versions. If kagent adds an actor API, switch to it.
- The dashboard backfills 6 h of atelet history at start, so old sessions appear as suspended.

## Alternatives considered

- **tmux with one pane per tool** (the earlier POC's `make watch`, now `make legacy-watch`). Raw logs,
  no correlation between layers.
- **Grafana / OTel.** Substrate and kagent emit OTLP, but no collector is deployed (atelet logs
  export errors). Worth doing for a real deployment; heavy for a laptop demo.
