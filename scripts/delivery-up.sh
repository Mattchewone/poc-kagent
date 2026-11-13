#!/usr/bin/env bash
# Wire mock-poc into the kargo stack (scripts/kargo-up.sh): after a PR merges, Temporal builds a
# release and Kargo + Argo CD roll it out dev -> staging -> prod. PR previews also run there.
# Idempotent. Needs the kargo clusters up (scripts/kargo-up.sh) and .venv (make setup).
#
#   1. poc-gitea on the kind network + webhook          (scripts/gitea-up.sh)
#   2. deploy/gitops/ -> poc/demo-todo-gitops on poc-gitea
#   3. Kargo project demo-todo + git creds + Argo CD ApplicationSet on kargo-hub
#   4. first release (demo-todo:0.1.0 from Gitea main) if the registry has none
#   5. edge containers so localhost:31081/31082/31083 reach dev/staging/prod
set -euo pipefail
cd "$(dirname "$0")/.."; source scripts/versions.env; mkdir -p .cache
PY=.venv/bin/python
HUB="kubectl --context ${KARGO_HUB_CONTEXT}"
step() { printf '\033[1;34m==> %s\033[0m\n' "$*"; }

if ! $HUB get ns kargo >/dev/null 2>&1; then
  echo "kargo hub ($KARGO_HUB_CONTEXT) not reachable. Bring it up first: make kargo" >&2
  exit 1
fi

step "poc-gitea: kind network + webhook"
scripts/gitea-up.sh
for _ in $(seq 1 15); do
  docker exec kargo-hub-control-plane curl -sf -m 3 http://poc-gitea:3000/api/healthz >/dev/null && break; sleep 1
done
docker exec kargo-hub-control-plane curl -sf -m 3 http://poc-gitea:3000/api/healthz >/dev/null \
  || { echo "kargo-hub cannot reach http://poc-gitea:3000" >&2; exit 1; }

step "gitops repo $GITEA_USER/$GITOPS_REPO"
AUTH="$GITEA_USER:$GITEA_PASSWORD"
curl -sf -u "$AUTH" -H 'content-type: application/json' "http://localhost:$GITEA_PORT/api/v1/user/repos" \
  -d "{\"name\":\"$GITOPS_REPO\",\"private\":false,\"default_branch\":\"main\",\"description\":\"demo-todo deploy config (Kargo + Argo CD)\"}" >/dev/null 2>&1 || true
REMOTE="http://$AUTH@localhost:$GITEA_PORT/$GITEA_USER/$GITOPS_REPO.git"
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
git clone -q -c credential.helper= "$REMOTE" "$WORK" 2>/dev/null || git -C "$WORK" init -q -b main
git -C "$WORK" checkout -q -B main
find "$WORK" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
cp -R deploy/gitops/. "$WORK"
git -C "$WORK" add -A
if git -C "$WORK" diff --cached --quiet; then echo "already in sync"; else
  git -C "$WORK" -c user.name=poc -c user.email=poc@poc.local commit -qm "sync deploy/gitops from mock-poc"
  git -C "$WORK" -c credential.helper= push -q "$REMOTE" main
  echo "pushed $(git -C "$WORK" rev-parse --short HEAD)"
fi

step "Kargo project demo-todo + Argo CD apps on $KARGO_HUB_CONTEXT"
$HUB apply -f clusters/kargo/project.yaml >/dev/null 2>&1 || true   # Project first creates the namespace
for _ in $(seq 1 30); do $HUB get ns "$KARGO_PROJECT" >/dev/null 2>&1 && break; sleep 1; done
$HUB apply -f clusters/kargo/project.yaml
$HUB -n "$KARGO_PROJECT" create secret generic git-creds \
  --from-literal=repoURL="$GITEA_IN_KIND/$GITEA_USER/$GITOPS_REPO.git" \
  --from-literal=username="$GITEA_USER" --from-literal=password="$GITEA_PASSWORD" \
  --dry-run=client -o yaml | $HUB label --local -f - kargo.akuity.io/cred-type=git -o yaml | $HUB apply -f - >/dev/null
$HUB apply -f clusters/kargo/argocd-apps.yaml

step "first release"
$PY -m worker.delivery bootstrap

step "edges: localhost -> stage NodePorts"
$PY -m worker.delivery edge 31081 nonprod >/dev/null
$PY -m worker.delivery edge 31082 nonprod >/dev/null
$PY -m worker.delivery edge 31083 prod >/dev/null

cat <<MSG
delivery wired up.
  Kargo     https://localhost:31444/project/$KARGO_PROJECT   (admin password: admin)
  Argo CD   https://localhost:31443                          (admin / admin)
  dev       http://localhost:31081   (kargo-nonprod, auto-promoted)
  staging   http://localhost:31082   (kargo-nonprod)
  prod      http://localhost:31083   (kargo-prod)
  previews  http://localhost:$PR_PORT_BASE + PR number   (kargo-nonprod, one namespace per PR)
MSG
