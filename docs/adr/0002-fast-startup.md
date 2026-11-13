# 0002. Startup: stop/start, prebuilt installs, parallel steps, image tar cache

- **Status:** Accepted (2026-11-12)
- **Origin:** Owner set the goal ("the startup takes forever", meaning cluster bring-up); the
  mechanisms are implementation choices

## Context

Before: two clusters created one after the other, Substrate compiled from source with `ko` (~90 s of
builds, ~52 s waiting for Postgres, then golden snapshots), and `make down` deleted everything, so
every session paid the full cost (>10 min). On a slow link, image pulls dominated a fresh bring-up;
one stalled `calico/node` pull cost ~15 minutes on its own.

## Decision

1. **Stop, don't delete.** `make stop` stops the node container (~11 s); `make start` restarts it
   and every install step detects its work is done and skips (~2 min to all pods Ready). Gitea data,
   PRs and the agent's golden snapshot survive.
2. **No source builds.** `kagent install` brings Substrate as prebuilt charts and images.
3. **Parallel installs.** kagent and OpenShell install at the same time
   ([scripts/up.sh](../../scripts/up.sh)).
4. **Image tar cache** ([scripts/image-cache.sh](../../scripts/image-cache.sh)). After a bring-up,
   images in the node's containerd are exported to `.cache/image-cache/` (~22 images, 2.9 GB); a fresh
   cluster imports them from disk in ~30 s.
5. **Wait on real readiness.** `make start` waits for pods to settle and the kagent controller to be
   Ready, rather than trusting stale status (the controller races Postgres after a restart).
6. **Pinned tools in `.cache/bin`** (kind, kagent CLI) with checksum checks, leaving the user's global
   tools and kube context alone.

Measured: `make start` ~2 min, fresh `make up` with a warm cache ~6 min, cold ~10-40 min depending on
the network.

## Consequences

- Day-to-day cost is `make start`, not a rebuild.
- Digest-pinned images (kagent, Substrate, OpenShell) can't be exported from containerd here
  (missing content), so a fresh cluster still downloads those.
- The cache is 2.9 GB of local disk.

## Alternatives considered

- **Registry pull-through caches** (`registry:2` as a proxy per upstream). Tried first and removed:
  the proxy stalled on large layers, and Substrate's atelet pulls actor images itself, bypassing
  containerd mirrors.
- **Pre-baked node image with everything loaded.** Faster still, but has to be rebuilt on every
  version bump; not worth it for a POC.
