# 0010. Local Gitea is the source of truth and the PR target

- **Status:** Accepted (2026-11-12), amended by [0017](0017-kargo-delivery-and-pr-environments.md): poc-gitea also holds the gitops repo, sits on the kind network for Kargo/Argo CD, and sends webhooks to the pipeline
- **Origin:** Owner, choosing "Gitea container" over a bare repo with a PR page in the chat UI

## Context

Approval should create a pull request, locally. The options were a real git server with PRs, or a
bare repo plus a home-made PR page.

## Decision

Gitea 1.24.6 in a Docker container (`poc-gitea`, volume `poc-gitea`, port 3300), set up by
[scripts/gitea-up.sh](../../scripts/gitea-up.sh): admin `poc` / `poc-password-local` (local only), an
API token in `.cache/gitea-token`, and `poc/demo-todo` seeded once from `demo-app/`.

- The agent clones anonymously over HTTP (EgressPolicy `gitea`); it has no credentials to push.
- The worker ([worker/gitea.py](../../worker/gitea.py)) keeps a cached clone: it produces the base
  tarball for verification, checks diffs apply, and on approval creates `agent/<change id>` from the
  verified base, commits, force-pushes and opens a PR via the API (idempotent per branch). The PR
  body carries the summary and test results.
- Gitea `main` is what the agent works from and what `make app` serves on :3000.

## Consequences

- Real PR review, diffs and merges, with the same shape as GitHub/GitLab.
- Gitea runs outside the cluster, so it survives `make down` and cluster rebuilds.
- Re-seeding after `demo-app/` changes means recreating the volume; the seed runs only once.
- Swapping in GitHub for a real deployment touches `gitea.py` and the EgressPolicy, nothing else.

## Alternatives considered

- **Bare repo + PR page in the chat UI.** No extra service, but a fake PR experience and more UI to
  build.
- **Gitea in the cluster.** Would be deleted with the cluster and need ingress; no benefit here.
