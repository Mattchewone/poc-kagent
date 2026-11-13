# 0001. One kind cluster for kagent, Substrate and OpenShell

- **Status:** Accepted (2026-11-12)
- **Origin:** Owner, choosing "one cluster, fall back to two"

## Context

The earlier POC ran two kind clusters, one for OpenShell and one for Substrate, created one after the
other. Bring-up took over 10 minutes every time and two Kubernetes control planes cost memory on a
laptop. kagent 1.0.1 installs its own Substrate (v1.0.0 fork) and needs Kubernetes 1.37 (it uses
PodCertificateRequest and ClusterTrustBundle), so the clusters had to be rebuilt anyway.

The open question was whether OpenShell (landlock sandboxes, needs NetworkPolicy) and Substrate's
gVisor workers would conflict on one node.

## Decision

One kind cluster, `poc` ([clusters/poc/kind.yaml](../../clusters/poc/kind.yaml)): kind v0.33.0,
`kindest/node:v1.37.0`, no default CNI, Calico v3.33.0. kagent (with Substrate) and OpenShell
(agent-sandbox controller + helm chart) install side by side in their own namespaces. If they
conflicted, the fallback was to split back into two clusters.

They did not conflict.

## Consequences

- One control plane to start, stop and reason about; one kube context (`kind-poc`).
- Faster bring-up, and kagent and OpenShell can install in parallel ([0002](0002-fast-startup.md)).
- A single node means everything shares the Docker Desktop VM's CPU and memory. Fine for a demo; not
  a statement about production topology.
- The old two-cluster stack (fake agent, milestones 1-8) has since been removed; it is in git history before this cleanup.

## Alternatives considered

- **Keep two clusters, bring them up in parallel.** Halves wall time but keeps double the memory and
  the cross-cluster plumbing.
- **Separate clusters per concern in production.** Still plausible later; nothing here depends on
  co-location except the bring-up script.
