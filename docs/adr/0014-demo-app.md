# 0014. Zero-dependency Node todo app with a planted bug

- **Status:** Accepted (2026-11-12)
- **Origin:** Implementation (the owner asked for "a demo web-app")

## Context

The agent needs a real target: a web app with a UI to click in the preview, an API, and tests. It
must work in sandboxes with no network (no `npm install`), build fast, and have an obvious bug for a
bug-fix demo plus room for features.

## Decision

[demo-app/](../../demo-app/): a todo app on `node:http` with an in-memory store, a static front end,
and `node:test` tests. No npm dependencies (enforced by its `CLAUDE.md`, which the agent reads
first).

- **Planted bug:** `stats()` counts done todos as "left", so the footer says "1 item left" when two
  aren't done. The original 8 tests don't catch it, so a good fix has to add one.
- Relative URLs in the front end, so it works behind the supervisor's proxy at any port.
- Seeded into Gitea once ([0010](0010-gitea-local-pr.md)); from then on Gitea `main` is the truth,
  not `demo-app/`.

## Consequences

- Verification runs in seconds and needs nothing beyond Node in the image.
- Features that would normally pull a library (dates, validation) have to be hand-written, which is
  a useful constraint for the agent and occasionally a limitation for demos.
- In-memory store: every preview starts from the same seed data, so previews are comparable.

## Alternatives considered

- **Express + vitest (TypeScript).** The first sketch. Needs a dependency install in every sandbox,
  which the no-network verifier can't do without baking `node_modules` into the image.
- **Re-use the earlier POC's Python `calc` target.** No UI to preview.
