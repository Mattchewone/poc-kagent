# 0007. Verification: agent's tests and the original tests, both must pass

- **Status:** Accepted (2026-11-12)
- **Origin:** Implementation

## Context

The verifier ([0005](0005-agent-and-verifier-separation.md)) must accept good changes, including the
new tests that come with them, and reject changes that only pass because tests were weakened.
Running only the original tests misses the agent's new coverage; running only the agent's tests
trusts whatever it did to the existing ones.

## Decision

In the sandbox, the supervisor ([images/app/supervisor.mjs](../../images/app/supervisor.mjs)):

1. unpacks a `git archive` of `main` (from the worker, which has already checked that the diff
   applies with `git apply --check`);
2. applies the diff;
3. **run A:** `node --test` on the tree as the agent left it, so its new tests count;
4. **run B:** a copy of that tree with every base file under `tests/` restored to `main`'s version,
   and runs again, so existing tests can't have been weakened to pass;
5. flags `weakened`: any line removed or changed in a base test file;
6. passes only if A and B both pass; then starts the app as the preview.

Results (pass/fail counts per run, files touched, weakened list, output) go back to the agent as the
`submit_change` result and onto the change page.

## Consequences

- Deleting or loosening a test can't turn red into green: run B uses the originals.
- Adding tests is rewarded, not penalised: run A includes them.
- A legitimate change that *should* modify an existing test (renamed behaviour) passes run A but fails
  run B. That's deliberate for the demo; a real system would route it to the human with the `weakened`
  diff rather than hard-fail.
- Tests must be under `tests/` and runnable with `node --test`. The reporter is pinned to `spec`
  because Node 22's default (TAP in pipes) broke the counts.

## Alternatives considered

- **Original tests only.** Misses new coverage; a change with no tests would pass.
- **Mutation testing / coverage thresholds.** Stronger signal, much slower; out of scope for a POC.
