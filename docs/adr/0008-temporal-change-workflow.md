# 0008. A Temporal workflow owns each change

- **Status:** Superseded by [0016](0016-temporal-owns-the-work-item.md) (2026-11-12). `ChangeWorkflow` stays registered so earlier executions can finish
- **Origin:** Implementation (Temporal was already the earlier POC's orchestrator)

## Context

A change lives much longer than one agent turn: several revisions, a preview that waits for a human
for minutes or days, an approval, a PR, cleanup. Along the way, sandboxes have to be created and
reliably destroyed even if a process restarts. The approval must only be possible for a revision that
actually passed, whatever the agent or the UI claims.

## Decision

One `ChangeWorkflow` per change ([worker/change.py](../../worker/change.py)), workflow id =
`change_id`:

- **`submit`** (update): queues a revision. Validator rejects it after the change is closed or after
  6 revisions. The newest revision wins; an older one still verifying is recorded as superseded.
- **`approve`** (update): validator requires phase `preview ready`, no pending revision and no
  earlier decision. Then `open_pr` and cleanup.
- **`abandon`** (signal), **`status`** (query). 24 h idle timeout abandons the change.
- Activities ([change_activities.py](../../worker/change_activities.py)): `start_app_env`,
  `verify_change`, `stop_app_env`, `open_pr`, called by name so the workflow module imports nothing
  that touches the filesystem (Temporal's sandbox rejects that).
- Infrastructure errors during start/verify mark **that revision** failed with the error; they
  don't fail the workflow, so the agent can resubmit.
- `finally` always stops the environment and waits for in-flight handlers.

## Consequences

- The PR gate is enforced server-side: even a buggy or malicious caller can't get `open_pr` to run
  for an unverified or superseded revision.
- Worker or pipeline restarts don't lose changes or leak sandboxes; Temporal resumes the workflow.
- The Temporal UI (:8233) shows every change's full history: a good demo surface.
- One more process (the dev server) to run ([0013](0013-host-side-control-plane.md)).

## Alternatives considered

- **State in the pipeline server's memory or a DB.** Simpler, but every crash path (leaked sandboxes,
  half-opened PRs) would need hand-written recovery.
- **kagent alone.** kagent tracks the conversation, not the change; it has no notion of "only
  approvable once verified".
