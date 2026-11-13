# 0006. Verify and preview in the same fresh OpenShell sandbox

- **Status:** Accepted (2026-11-12), open to revisit
- **Origin:** Implementation. Note the deviation below.

## Context

Each submitted revision needs two things:

- **verification** in an environment the agent can't influence ([0005](0005-agent-and-verifier-separation.md));
- **a preview** the developer can click: the app running with the change, staying up for as long as
  the review takes (minutes or days).

The pilot this POC grew from had already settled on a split: Substrate for the agent (it suspends
between turns at no cost) and a fresh OpenShell sandbox for each test run (locked down, disposable).
What it hadn't decided was where previews live.

What was available for the preview:

| Option | Inbound URL from the host | Isolation | Notes |
|---|---|---|---|
| **OpenShell sandbox** | yes: `openshell forward start <port> <sandbox>` | landlock filesystem policy, empty network policy, no credential providers | already used for verification |
| **kagent Sandbox** (SandboxTemplate) | no inbound port, ingress or URL exposure in 1.0.1 | Substrate actor | process/file API only, TTL ≤ 24 h |
| **Raw Substrate actor** (kagent's v1.0.0 fork) | no supported path in kagent 1.0.1; the earlier POC's router addressing was written for Substrate v0.5.0 | gVisor | would need its own routing to reach from a browser |
| **Local docker** | yes: `docker run -p` | container only | kept as the no-cluster path (`APP_RUNTIME=local`) |

## Decision

Each revision gets one fresh OpenShell sandbox ([worker/apps.py](../../worker/apps.py)), created from
`localhost:5001/mock-poc-app:dev` with:

- `--no-auto-providers`, so no credentials are injected;
- [clusters/openshell/app-policy.yaml](../../clusters/openshell/app-policy.yaml): read-write
  only `/tmp`, `/work`, `/dev/null`, and `network_policies: {}`, so no outbound connections at all.
  Landlock runs in `best_effort` mode, so filesystem confinement depends on the node kernel
  (Docker Desktop's VM supports it); the network block doesn't depend on landlock;
- a supervisor ([images/app/supervisor.mjs](../../images/app/supervisor.mjs)) on a port forwarded to
  the host.

The worker posts the base tarball and the diff to `/__poc/verify`. If the revision passes, **the same
sandbox** starts the app and proxies to it: that forwarded port is the `preview_url`. A new revision
gets a new sandbox and the old one is deleted; approval or abandonment deletes the last one.

### Why the same sandbox, and why OpenShell

1. **What you click is exactly what was verified.** No rebuild, no second deploy, no gap between
   the tested tree and the previewed one.
2. **It had a working way to expose a port.** OpenShell's `forward` gives a host URL today. Neither
   kagent Sandboxes nor kagent's Substrate offered a supported way to reach an actor's HTTP port from
   a browser.
3. **The verifier is in a different isolation system from the agent.** The agent runs on Substrate.
   The verifier and preview run in OpenShell, which the agent has no API, credentials or network path
   to. A compromise of the agent's runtime doesn't reach the verdict.
4. **No network is easy to state and to check.** An empty OpenShell network policy plus no providers
   was verified directly: outbound connections from the sandbox fail with `EACCES`.
5. **It continues the pilot's pattern** (OpenShell = the exam room), reusing its policy work
   (`/dev/null` needed for `git apply`; sandbox user can't write `/work`, so the supervisor uses
   `/tmp/work`).

### Deviation from the option chosen

When the owner picked "kagent does the coding too", that option read "OpenShell is only used for
verify", and the earlier plan had previews on Substrate actors so they could suspend while waiting for
review. During the build, previews were moved into the verify sandbox for the reasons above, mainly
(1) and (2). The change was reported in the build summary but not raised as its own question. This
record makes it explicit.

## Consequences

- Verify + preview ready in ~9-10 s per revision.
- **A preview holds a running pod for its whole review.** It does not suspend like an actor would.
  One pod per open change is cheap locally; at scale it's the main cost of this choice. Changes
  abandon themselves after 24 h idle.
- Preview URLs are `localhost` port-forwards (`20000 + hash`), reachable only from the machine running
  the worker. Sharing a preview with someone else needs real ingress.
- The preview has no network, so a change that calls an external API can't be previewed faithfully.
  The demo app has none.

## Revisit when

- kagent or Substrate gains a supported way to expose an actor's HTTP port. Then the preview could
  be an actor restored from a post-verify snapshot that **suspends between page views**, keeping
  property (1) and recovering the idle cost.
- Previews need to be shared beyond one machine (ingress, auth).
- The app needs outbound calls in preview (would need an allow-listed OpenShell network policy).

## Alternatives considered

- **Verify in OpenShell, preview on Substrate.** The original plan. Blocked on exposing a URL (above);
  also means building the app twice.
- **Preview in local docker.** Works (it's the `APP_RUNTIME=local` path), but weaker isolation than
  the sandbox that verified it.
- **Preview in the agent's own actor.** Rejected: the agent controls that environment, so the preview
  wouldn't be the verified artifact.
