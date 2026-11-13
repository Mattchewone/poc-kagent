# 0012. Calico on 10.244.0.0/16; host services via host.docker.internal

- **Status:** Accepted (2026-11-12)
- **Origin:** Implementation (forced by failures found during bring-up)

## Context

- OpenShell's sandbox isolation relies on NetworkPolicy, which kind's default CNI doesn't enforce, so
  the cluster runs Calico.
- Several things the cluster must reach run on the host or in sibling containers: Gitea (:3300), the
  pipeline server (:8765), the auth proxy (:8766), and the local registry.
- Calico's default IP pool is `192.168.0.0/16`. Docker Desktop's host address
  (`host.docker.internal`) is `192.168.65.254`, inside that range. Calico then treated host traffic
  as in-cluster: the node could reach Gitea and the pipeline, pods timed out.
- Substrate's atelet pulls actor images itself and ignored containerd's `localhost:5001` mirror.

## Decision

- kind with `disableDefaultCNI` and `podSubnet: 10.244.0.0/16`; `up.sh` rewrites Calico's manifest
  CIDR to match.
- Cluster workloads reach host services at `host.docker.internal:<port>`: Gitea via an EgressPolicy,
  the pipeline via RemoteMCPServer URLs, the proxy via ModelConfig `baseUrl`.
- `localhost:5001` → `kind-registry:5000` for containerd (`hosts.toml`) and for atelet
  (`--localhost-registry-replacement`, patched into its helm release).

## Consequences

- Pods and actors reach the host reliably.
- These addresses are Docker Desktop specific. On Linux Docker or a real cluster, `host.docker.internal`
  and the registry mapping change; the manifests template the ports but not the hostname.

## Alternatives considered

- **Keep 192.168/16, exclude the host range from Calico.** More fragile than moving the pool.
- **Run Gitea and the pipeline in-cluster** to avoid host networking. See
  [0013](0013-host-side-control-plane.md) for why they stay on the host.
