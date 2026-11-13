# 0004. Claude Code through kagent's Claude harness, on a Substrate actor

- **Status:** Accepted (2026-11-12)
- **Origin:** Owner ("a full harness and an actual LLM sub like claude-code"; "kagent does the coding too")

## Context

The earlier POC used a scripted fake agent. The brief asked for a real coding agent with a full
harness. kagent 1.0.1 ships a Claude harness: Claude Code 2.1.285 running headless, driven over A2A,
on a Substrate actor. Substrate runs each actor in gVisor, snapshots it to object storage (RustFS)
between turns, and restores it when the next message arrives.

The stock harness image is Alpine with Claude Code but no Node.js, and the demo app's tests need
`node`.

## Decision

- The dev agent uses a `Harness` with `claude: {}` and our image
  ([images/harness/Dockerfile](../../images/harness/Dockerfile)): the official harness image,
  pinned by sha256, plus `nodejs` and `npm`. `make images` pushes it to `localhost:5001` and
  `kagent-apply` renders the digest into the manifest.
- It runs on the `kagent-default` WorkerPool with snapshots in `s3://ate-snapshots/kagent/`.
- Each kagent chat session gets its own actor (`session-<id>`). The actor keeps its checkout and
  Claude session across turns, so "now make it plural-aware" continues where it left off.
- Model: `claude-sonnet-5-5` by default (`DEV_AGENT_MODEL=… make kagent-apply` to change).

## Consequences

- Real agent behaviour: it reads `CLAUDE.md`, writes tests, runs `npm test`, iterates on failures.
- Actors cost nothing while suspended; a wake takes ~1-4 s (`Restore`), suspend happens as soon as
  the turn ends (`Checkpoint`). Visible in `make watch` ([0015](0015-observability.md)).
- Changing the harness image or env rebuilds the golden snapshot (a few seconds; the Agent shows
  `ActorTemplatePending` meanwhile).
- Atelet pulls actor images itself, so it needs `--localhost-registry-replacement=kind-registry:5000`
  to find `localhost:5001` images; `up.sh` patches that in.
- Usage is billed per model call ([0011](0011-model-credentials.md)).

## Alternatives considered

- **Claude Code in an OpenShell sandbox driven by Temporal**, authenticated with a subscription
  token. Simpler auth, but no chat platform and no suspend between turns. Superseded by the owner's
  choice of kagent for coding.
- **kagent's own ADK agent** (non-Claude-Code). Not a "full harness" in the sense asked for.
