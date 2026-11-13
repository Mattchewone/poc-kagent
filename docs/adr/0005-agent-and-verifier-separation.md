# 0005. One agent sandbox for code + tests; a separate verifier

- **Status:** Accepted (2026-11-12)
- **Origin:** Implementation, answering the owner's question "should we do fix and tests or feature
  and tests in the same agent sandbox, is there a need for two"

## Context

Two different things were being asked of "the sandbox":

1. somewhere the agent works: edit code, write tests, run them, iterate;
2. somewhere that decides whether the change is acceptable.

Splitting (1) into a "fix" agent and a "tests" agent sounds rigorous, but a coding agent is good
precisely because it runs its own tests in a tight loop. Splitting adds a hand-off per iteration and
loses context. (2), though, must not be something the agent controls: an agent that can edit tests
can make any change "pass" (the earlier POC's `cheat` scenario demonstrated this).

## Decision

- **One agent sandbox** (the Substrate actor, [0004](0004-claude-code-on-substrate.md)) does the
  feature or fix *and* its tests, and runs them.
- **A separate verifier** runs every submitted revision in a fresh environment the agent has no
  access to, with no network and no credentials, applying only the diff to a clean checkout of `main`
  ([0006](0006-openshell-verify-and-preview.md), [0007](0007-verification-rule.md)).
- The agent's only way to ship is `submit_revision`; it cannot push to Gitea.
- The agent's sandbox lives for the whole work item: one kagent session, the same checkout, suspended
  while it waits for review or input. The verifier is not a second workspace. It is the check that
  gates the PR, so that gate never relies on test results the agent produced itself.
- `submit_revision` blocks and returns the verdict inside the agent's turn, so the agent can fix and
  resubmit while it still has the context. The alternative is ending the turn, suspending, and being
  woken with a new prompt.

## Consequences

- The agent's loop stays fast; verification adds ~10 s per submission.
- The verifier's verdict doesn't depend on anything in the agent's environment: not installed
  packages, not network, not modified test files.
- Two places run the tests, so a flaky test can pass in one and fail in the other. The verifier's
  result wins and the agent sees its output.

## Alternatives considered

- **Fix agent + test agent.** Rejected for the latency and context loss above.
- **Trust the agent's own test run.** Rejected: no defence against weakened tests or environment
  drift.
- **Verify in the agent's sandbox, then rely on CI on the PR** (the common software-factory shape).
  It's viable and saves a sandbox per revision, but PRs can then open for unverified code. Our
  verifier is effectively CI that runs before the PR exists.
