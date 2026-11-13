# 0018. Agents act under their own identity, on behalf of a verified user

- **Status:** Proposed (2026-11-13). Design only; see the phases in [../identity.md](../identity.md#plan)
- **Origin:** Owner (requirement: log in once, keep the right identity through the flow, the agent has
  its own identity but is attributed to the user, "agent (on behalf of …)" on PRs and comments).
  The mechanisms are implementation choices.

## Context

There is currently no identity in the flow:

- kagent runs with `server.auth.mode: insecure`, so there is no login and every session belongs to
  the same anonymous user.
- `start_work` from chat records `requested_by="chat"`, and a merge from chat records `by="chat"`.
- Every Gitea write (push, PR, comment, merge) uses the `poc` admin account. Commits say
  `poc-agent <agent@poc.local>`.
- The dev-agent's `submit_revision` is unauthenticated. The workflow only checks that *an* agent turn
  is running.

kagent 1.0.1 already has the parts needed to fix this:

- an oauth2-proxy subchart for OIDC login;
- the `trusted-proxy` auth mode, which takes the principal from a configurable claim;
- two options on `RemoteMCPServer`, of which a server can set only one:
  - `tokenPassthrough` forwards the caller's bearer token;
  - `actorJwt` gives the server a Substrate-signed token for the calling actor, bound to its session.

Gitea 1.24 is an OIDC provider. Its discovery document lists `preferred_username`, `email` and
`groups`.

## Decision

- **The IdP owns people.** In the POC that is Gitea's OIDC provider, so a kagent user and a Gitea
  user are the same name. In production it is the corporate IdP, with the forge and Kargo federated
  to it.
- **Each agent has its own identity:**
  - for the pipeline: a Substrate actor JWT (`actorJwt` on the dev-agent's MCP server);
  - for the forge: a Gitea bot account (`dev-agent`) whose token only the pipeline holds.

  Agents never hold user credentials.
- **The user is passed through, then pinned.** The delivery-agent's MCP servers use
  `tokenPassthrough`. The pipeline verifies the token's signature against the IdP's JWKS, because
  kagent doesn't, and stores the principal as `requested_by` on the Temporal work item. Every later
  action reads the principal from the workflow. None takes it from an agent or from text.
- **Attribution is "actor, on behalf of principal":**
  - commits by `dev-agent`, with `On-behalf-of:` and `Work-Item:` trailers;
  - PRs opened by `dev-agent` with "Requested by @user";
  - chat merges done by `dev-agent` with "on behalf of @user" in the message;
  - promotions annotated with the requester.

  Human actions in the Gitea UI stay as the human.
- **Authorization is the intersection.** Before merging or promoting on a user's behalf, the
  pipeline checks that the user could do it themselves (their Gitea repo permission). The agent's
  role caps the rest.

## Consequences

- PRs, commits, comments, Temporal and kagent all answer "which agent did this, and for whom" the
  same way. A human can't impersonate an agent, and an agent can't act as a human.
- The pipeline becomes a security boundary. It holds the bot token and receives users' bearer
  tokens, which still carry their kagent authority, so it must verify, not trust. In production that
  means running it in-cluster.
- With login enabled, every MCP server that requires a caller token needs controller tool discovery
  disabled (`kagent.dev/discovery` label).
- Bring-up gains an OAuth2 app registration in Gitea and the oauth2-proxy config. That's a few
  seconds, not a new cluster component worth worrying about.
- Not built yet. Until phase 1 lands, everything is still the admin account.

## Alternatives considered

- **Agent acts as the user** (the user's OAuth token, or Gitea `Sudo`). Simpler attribution, but the
  agent's work becomes indistinguishable from the user's, and the pipeline holds a token that can do
  anything the user (or, with `Sudo`, anyone) can.
- **One shared bot, with the user only in the PR text.** That's today's shape with a nicer label:
  nothing is verified, and anyone can type any name.
- **A separate user store in the pipeline.** A second source of truth for people that drifts from
  the IdP.
