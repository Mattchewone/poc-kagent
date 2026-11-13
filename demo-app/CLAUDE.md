# demo-todo

Small todo app used as the target for the agent pipeline.

- No npm dependencies. Do not add any: the sandboxes have no package registry access.
- Server: `src/server.js` (node:http), store: `src/store.js` (in-memory), UI: `public/` (vanilla JS, relative URLs only so it works behind a path prefix).
- Tests: `npm test` (node:test). Every change must come with tests that cover it.
- Do not weaken or delete existing tests; the verifier restores them from the base branch anyway.
- Keep changes small and focused on the request.
