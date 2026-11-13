#!/usr/bin/env bash
# One kind cluster with Calico, kagent (which installs its Substrate v1.0.0 fork) and OpenShell.
#
# Fast paths, in order of how much they save:
#   1. Cluster stopped with `make stop`  -> docker start the node, wait for Ready (~30-60 s).
#   2. Cluster running                    -> every step below checks state and skips.
#   3. Fresh                              -> prebuilt helm charts only (no source builds); images
#                                            from .cache/image-cache loaded from disk instead of
#                                            pulled; kagent and OpenShell installed in parallel.
set -euo pipefail
cd "$(dirname "$0")/.."; ROOT=$PWD; source scripts/versions.env
[ -f .env ] && set -a && source .env && set +a
mkdir -p .cache/logs
export PATH="$ROOT/.cache/bin:$PATH"
CTX=kind-$CLUSTER; K="kubectl --context $CTX"
t0=$(date +%s); step() { printf '[%3ss] %s\n' $(( $(date +%s) - t0 )) "$*"; }

scripts/tools.sh >/dev/null

# ---- local registry: where images.sh pushes our images (survives cluster deletes) ----------
step "registry"
if [ "$(docker inspect -f '{{.State.Running}}' kind-registry 2>/dev/null || true)" != true ]; then
  docker start kind-registry >/dev/null 2>&1 || docker run -d --restart=always --name kind-registry \
    -p 127.0.0.1:5001:5000 -v kind-registry:/var/lib/registry registry:2 >/dev/null
fi

# ---- cluster -------------------------------------------------------------------------
node="$CLUSTER-control-plane"
state=$(docker inspect -f '{{.State.Status}}' "$node" 2>/dev/null | tr -d '[:space:]' || true)
[ -n "$state" ] || state=missing
if [ "$state" = exited ]; then
  step "starting stopped cluster"
  docker start "$node" >/dev/null
elif [ "$state" = missing ]; then
  step "creating kind cluster $CLUSTER ($KIND_NODE_IMAGE)"
  prev_ctx=$(kubectl config current-context 2>/dev/null || true)
  kind create cluster --config clusters/poc/kind.yaml --image "$KIND_NODE_IMAGE" --wait 0s
  # kind switches the current context; put the user's back (we always pass --context explicitly).
  [ -n "$prev_ctx" ] && kubectl config use-context "$prev_ctx" >/dev/null
  # localhost:5001 inside the node means the kind-registry container.
  docker exec "$node" mkdir -p /etc/containerd/certs.d/localhost:5001
  printf '[host."http://kind-registry:5000"]\n  capabilities = ["pull", "resolve"]\n' \
    | docker exec -i "$node" tee /etc/containerd/certs.d/localhost:5001/hosts.toml >/dev/null
  step "loading cached images"
  scripts/image-cache.sh load
fi
docker network connect kind kind-registry 2>/dev/null || true
for _ in $(seq 1 60); do $K get --raw /readyz >/dev/null 2>&1 && break; sleep 2; done

# ---- Calico ---------------------------------------------------------------------------
if ! $K -n kube-system get ds calico-node >/dev/null 2>&1; then
  step "installing Calico ${CALICO_VERSION_POC}"
  f=.cache/calico-${CALICO_VERSION_POC}.yaml
  [ -s "$f" ] || curl -fsSL -o "$f" "https://raw.githubusercontent.com/projectcalico/calico/${CALICO_VERSION_POC}/manifests/calico.yaml"
  # Pod CIDR must match kind.yaml and stay clear of Docker Desktop's 192.168.65.0/24 (host.docker.internal),
  # or Calico treats traffic to the host as in-cluster and pods can't reach Gitea / the pipeline.
  sed -e 's|# - name: CALICO_IPV4POOL_CIDR|- name: CALICO_IPV4POOL_CIDR|' \
      -e 's|#   value: "192.168.0.0/16"|  value: "10.244.0.0/16"|' "$f" | $K apply -f - >/dev/null
fi
$K -n kube-system rollout status ds/calico-node --timeout=300s >/dev/null
$K wait --for=condition=Ready nodes --all --timeout=300s >/dev/null
step "node ready"

# ---- kagent (+ Substrate) and OpenShell, in parallel -----------------------------------
install_kagent() {
  if helm --kube-context $CTX -n kagent status kagent >/dev/null 2>&1; then echo "kagent already installed"; return; fi
  if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    echo "ANTHROPIC_API_KEY not set: installing with a placeholder; set it later with 'make kagent-key'"
    export ANTHROPIC_API_KEY=placeholder-set-with-make-kagent-key
  fi
  # Private kubeconfig: kagent installs into the current context, and the user's global one must not change.
  kind get kubeconfig --name "$CLUSTER" > .cache/kubeconfig-$CLUSTER
  KUBECONFIG=$ROOT/.cache/kubeconfig-$CLUSTER kagent install --namespace kagent --provider Anthropic --timeout 10m
}
install_openshell() {
  if ! $K get crd sandboxes.agents.x-k8s.io >/dev/null 2>&1; then
    f=.cache/agent-sandbox-${AGENT_SANDBOX_VERSION}.yaml
    [ -s "$f" ] || curl -fsSL -o "$f" "https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${AGENT_SANDBOX_VERSION}/sandbox.yaml"
    $K apply -f "$f"
  fi
  $K -n agent-sandbox-system rollout status deploy --timeout=300s
  if ! helm --kube-context $CTX -n openshell status openshell >/dev/null 2>&1; then
    helm --kube-context $CTX upgrade --install openshell oci://ghcr.io/nvidia/openshell/helm-chart \
      --version "${OPENSHELL_VERSION#v}" --namespace openshell --create-namespace -f clusters/openshell/values.yaml
  fi
  $K -n openshell rollout status statefulset/openshell --timeout=300s
}
step "kagent + Substrate and OpenShell (parallel; logs in .cache/logs/)"
install_kagent > .cache/logs/kagent-install.log 2>&1 & kp=$!
install_openshell > .cache/logs/openshell-install.log 2>&1 & op=$!
rc=0
wait $kp || { echo "kagent install failed:"; tail -30 .cache/logs/kagent-install.log; rc=1; }
wait $op || { echo "OpenShell install failed:"; tail -30 .cache/logs/openshell-install.log; rc=1; }
[ $rc = 0 ] || exit $rc
step "kagent and OpenShell installed"

# Substrate's atelet pulls actor images itself, not through containerd: point localhost:5001 at
# kind-registry so our sha256-pinned harness image resolves.
if ! $K -n ate-system get ds atelet -o jsonpath='{.spec.template.spec.containers[0].args}' | grep -q localhost-registry-replacement; then
  step "atelet: localhost registry -> kind-registry"
  helm --kube-context $CTX -n ate-system upgrade substrate-atelet oci://ghcr.io/kagent-dev/substrate/helm/substrate-atelet \
    --version 1.0.0 --reuse-values --set 'atelet.extraArgs={--localhost-registry-replacement=kind-registry:5000}' --wait --timeout 5m >/dev/null
fi

# Restarted clusters report stale "Running" pods for a few seconds and the kagent controller can
# race Postgres, so wait on actual pod readiness rather than rollout status.
pods_settled() {
  [ -z "$($K get pods -A --no-headers 2>/dev/null | awk '$4 != "Completed" && ($4 != "Running" || $3 ~ /^0\//)')" ]
}
for _ in $(seq 1 100); do pods_settled && break; sleep 3; done
$K -n kagent wait --for=condition=Ready pod -l app.kubernetes.io/component=controller --timeout=300s >/dev/null 2>&1 \
  || $K -n kagent rollout status deploy/kagent-controller --timeout=300s >/dev/null
step "all pods ready"

# ---- images, port-forwards, OpenShell gateway registration ----------------------------
scripts/images.sh push
scripts/port-forwards.sh all
openshell gateway add "http://127.0.0.1:${OPENSHELL_GATEWAY_PORT}" --local --name poc >/dev/null 2>&1 || true
openshell gateway select poc >/dev/null 2>&1 || true
scripts/gitea-up.sh
scripts/kagent-apply.sh
scripts/image-cache.sh save >/dev/null 2>&1 &   # so the next fresh cluster loads instead of pulling
step "up. kagent UI: http://localhost:${KAGENT_UI_PORT}   Gitea: http://localhost:${GITEA_PORT}/${GITEA_USER}/${GITEA_REPO}"
