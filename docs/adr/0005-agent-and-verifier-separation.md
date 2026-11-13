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
- The agent's only way to ship is `submit_change`; it cannot push to Gitea.

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
