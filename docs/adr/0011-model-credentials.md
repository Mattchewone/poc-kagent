# 0011. Gateway-injected credentials; API key for real use, subscription via a local proxy

- **Status:** Accepted (2026-11-12)
- **Origin:** Owner (API key for kagent; later "make it work so I can use the subscription… with the
  intention of going to API keys on a full deployment")

## Context

kagent's credential model: the harness gets `ANTHROPIC_API_KEY=kagent-credential-injected`, a
placeholder, and Substrate's egress gateway overwrites the `x-api-key` header on calls to
`api.anthropic.com` (or to `anthropic.baseUrl` if set) with the value from the `kagent-anthropic`
Secret. The agent never holds the real credential.

kagent 1.0.1 fixes that header per provider: Anthropic is always `x-api-key`. Console API keys
(`sk-ant-api…`) work that way. Claude subscription tokens from `claude setup-token` (`sk-ant-oat…`)
are only accepted as `Authorization: Bearer` with the `oauth-2025-04-20` beta flag, so sent as
`x-api-key` they fail with "Invalid API key". `ANTHROPIC_API_KEY` and `ANTHROPIC_BASE_URL` are
reserved in the Harness env, so they can't be overridden there.

## Decision

`ANTHROPIC_API_KEY` in `.env` may hold either kind; `make kagent-key`
([scripts/kagent-apply.sh](../../scripts/kagent-apply.sh)) picks the mode from the prefix
(override: `CLAUDE_AUTH=api|subscription`):

| Credential | Path | For |
|---|---|---|
| `sk-ant-api…` | gateway → `x-api-key` → api.anthropic.com. Key checked against `/v1/models` before it's stored; rejected keys never reach the cluster. ModelConfig has no `baseUrl`. | real deployments |
| `sk-ant-oat…` | ModelConfig `anthropic.baseUrl = http://host.docker.internal:8766`; gateway → `x-api-key` → [worker/claude_auth_proxy.py](../../worker/claude_auth_proxy.py) → `Authorization: Bearer` + beta flag → api.anthropic.com | local experiments |

The proxy stores nothing and logs no secrets. It re-encodes whatever the gateway injected, passes
API keys through untouched, and logs the auth mode per request (`.cache/claude-proxy.log`), which is
how "Invalid API key" was diagnosed.

## Consequences

- The agent never sees either credential in both modes; the deployment shape (API key, direct) is the
  default and needs no proxy.
- Moving to an API key is a new value plus `make kagent-key`; the `baseUrl` is removed.
- Subscription mode sends the token over plain HTTP from the cluster to the host. Fine on one
  machine, not on a shared network.
- Subscription use is subject to the plan's limits and Anthropic's terms; it's for local
  experimentation only. This was initially declined as too hacky and revisited at the owner's
  explicit request, scoped to local use.
- `up.sh` passes the key to `kagent install` only at install time; later changes need
  `make kagent-key` (this is what caused the first "Invalid API key": the cluster still held the
  install-time placeholder).

## Alternatives considered

- **Pass the token as `CLAUDE_CODE_OAUTH_TOKEN` in the Harness env**, with a wrapper that unsets the
  placeholder key. Works without a proxy, but the token sits in plain text in the Harness resource
  and the agent can read it.
- **Run Claude Code on the host with the owner's login.** Simplest, but loses kagent's UI, approval
  and sandboxing: a different system from the one being tested.
