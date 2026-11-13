#!/usr/bin/env bash
# The delivery stack mock-poc ships to: three kind clusters plus a registry, all on the `kind` network.
#
#   kargo-hub       Argo CD (UI :31443), Kargo (UI :31444), Argo Rollouts (Kargo verification)
#   kargo-nonprod   Argo Rollouts; demo-todo dev, staging and PR previews
#   kargo-prod      Argo Rollouts; demo-todo prod
#   kargo-registry  TLS registry (self-signed), host localhost:5002, clusters kargo-registry:5000
#
# Idempotent and fast on re-run: stopped nodes are started, installed charts are skipped.
# Git is not here: Kargo and Argo CD read poc-gitea (scripts/gitea-up.sh, scripts/delivery-up.sh).
set -euo pipefail
cd "$(dirname "$0")/.."; ROOT=$PWD; source scripts/versions.env
mkdir -p .cache/logs
export PATH="$ROOT/.cache/bin:$PATH"
t0=$(date +%s); step() { printf '[%3ss] kargo: %s\n' $(( $(date +%s) - t0 )) "$*"; }

CLUSTERS=(hub nonprod prod)
WORKLOADS=(nonprod prod)
HUB=kind-kargo-hub
REGISTRY=kargo-registry
CERTS=$ROOT/.cache/kargo-registry-certs
hub() { kubectl --context "$HUB" "$@"; }
installed() { helm --kube-context "$1" -n "$2" status "$3" >/dev/null 2>&1; }

scripts/tools.sh >/dev/null

# ---- clusters ---------------------------------------------------------------------------
for c in "${CLUSTERS[@]}"; do
  node="kargo-$c-control-plane"
  state=$(docker inspect -f '{{.State.Status}}' "$node" 2>/dev/null | tr -d '[:space:]' || true)
  case "${state:-missing}" in
    running) ;;
    missing)
      step "creating kind cluster kargo-$c"
      prev_ctx=$(kubectl config current-context 2>/dev/null || true)
      kind create cluster --config "clusters/kargo-stack/$c.yaml" --image "$KARGO_NODE_IMAGE" --wait 0s
      # kind switches the current context; put the user's back (we always pass --context explicitly).
      [ -n "$prev_ctx" ] && kubectl config use-context "$prev_ctx" >/dev/null ;;
    *) step "starting stopped node $node"; docker start "$node" >/dev/null ;;
  esac
done

# ---- registry ---------------------------------------------------------------------------
if [ -z "$(docker ps -aq -f name="^${REGISTRY}$")" ]; then
  step "registry $REGISTRY (https://localhost:5002)"
  mkdir -p "$CERTS"
  [ -s "$CERTS/registry.crt" ] || openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=$REGISTRY" \
    -addext "subjectAltName=DNS:$REGISTRY,DNS:localhost,IP:127.0.0.1" \
    -keyout "$CERTS/registry.key" -out "$CERTS/registry.crt" 2>/dev/null
  docker run -d --restart=always --name "$REGISTRY" --network kind -p 127.0.0.1:5002:5000 \
    -v "$CERTS:/certs:ro" -v kargo-registry:/var/lib/registry \
    -e REGISTRY_HTTP_TLS_CERTIFICATE=/certs/registry.crt -e REGISTRY_HTTP_TLS_KEY=/certs/registry.key \
    registry:2 >/dev/null
else
  docker start "$REGISTRY" >/dev/null
fi
# containerd on every node pulls kargo-registry:5000 over TLS without verifying the self-signed cert.
for c in "${CLUSTERS[@]}"; do
  docker exec -i "kargo-$c-control-plane" sh -c "mkdir -p /etc/containerd/certs.d/$REGISTRY:5000 && cat > /etc/containerd/certs.d/$REGISTRY:5000/hosts.toml" <<EOF
server = "https://$REGISTRY:5000"
[host."https://$REGISTRY:5000"]
  capabilities = ["pull", "resolve"]
  skip_verify = true
EOF
done

for c in "${CLUSTERS[@]}"; do
  for _ in $(seq 1 60); do kubectl --context "kind-kargo-$c" get --raw /readyz >/dev/null 2>&1 && break; sleep 2; done
  kubectl --context "kind-kargo-$c" wait --for=condition=Ready nodes --all --timeout=300s >/dev/null
done
step "nodes ready"

# ---- charts (helm --repo: the user's helm repo list is left alone) ---------------------------
ARGO=https://argoproj.github.io/argo-helm
rollouts() { # context
  installed "$1" argo-rollouts argo-rollouts && return 0
  helm --kube-context "$1" upgrade --install argo-rollouts argo-rollouts --repo "$ARGO" \
    --version "$ROLLOUTS_CHART_VERSION" -n argo-rollouts --create-namespace --wait --timeout 10m
}
bcrypt() { # Argo CD and Kargo want $2a$ bcrypt; htpasswd ships with macOS, docker covers Linux.
  if command -v htpasswd >/dev/null; then htpasswd -nbBC 10 "" "$1"
  else docker run --rm httpd:2.4-alpine htpasswd -nbBC 10 "" "$1"; fi | tr -d ':\n' | sed 's/^\$2y/$2a/'
}
install_hub() {
  local hash; hash=$(bcrypt "$KARGO_ADMIN_PASSWORD")
  installed $HUB cert-manager cert-manager || helm --kube-context $HUB upgrade --install cert-manager cert-manager \
    --repo https://charts.jetstack.io --version "$CERT_MANAGER_VERSION" -n cert-manager --create-namespace \
    --set crds.enabled=true --wait --timeout 10m
  installed $HUB argocd argocd || helm --kube-context $HUB upgrade --install argocd argo-cd --repo "$ARGO" \
    --version "$ARGOCD_CHART_VERSION" -n argocd --create-namespace \
    --set "configs.secret.argocdServerAdminPassword=$hash" \
    --set dex.enabled=false --set notifications.enabled=false \
    --set server.service.type=NodePort --set server.service.nodePortHttps=31443 --wait --timeout 10m
  rollouts $HUB
  installed $HUB kargo kargo || helm --kube-context $HUB upgrade --install kargo oci://ghcr.io/akuity/kargo-charts/kargo \
    --version "$KARGO_CHART_VERSION" -n kargo --create-namespace \
    --set api.service.type=NodePort --set api.service.nodePort=31444 --set api.host=localhost:31444 \
    --set "api.adminAccount.passwordHash=$hash" \
    --set "api.adminAccount.tokenSigningKey=$(openssl rand -base64 29 | tr -d '=+/')" \
    --set controller.allowCredentialsOverHTTP=true --wait --timeout 10m
}

step "charts: Argo CD + Kargo + Rollouts on hub, Rollouts on workloads (parallel; logs in .cache/logs/)"
pids=()
install_hub > .cache/logs/kargo-hub.log 2>&1 & pids+=("$!:hub")
for c in "${WORKLOADS[@]}"; do rollouts "kind-kargo-$c" > ".cache/logs/kargo-$c.log" 2>&1 & pids+=("$!:$c"); done
rc=0
for p in "${pids[@]}"; do
  wait "${p%%:*}" || { echo "kargo-${p#*:} install failed:"; tail -30 ".cache/logs/kargo-${p#*:}.log"; rc=1; }
done
[ $rc = 0 ] || exit $rc

# ---- Argo CD: register the workload clusters (names match clusters/kargo/argocd-apps.yaml) -----
for c in "${WORKLOADS[@]}"; do
  kc=$(kind get kubeconfig --internal --name "kargo-$c")
  field() { echo "$kc" | awk -v k="$1" '$1==k":" {print $2; exit}'; }
  hub apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: cluster-kargo-$c
  namespace: argocd
  labels:
    argocd.argoproj.io/secret-type: cluster
type: Opaque
stringData:
  name: $c
  server: https://kargo-$c-control-plane:6443
  config: |
    {"tlsClientConfig":{"caData":"$(field certificate-authority-data)","certData":"$(field client-certificate-data)","keyData":"$(field client-key-data)"}}
EOF
done

hub -n kargo rollout status deploy --timeout=300s >/dev/null
hub -n argocd rollout status deploy --timeout=300s >/dev/null
step "up. Kargo https://localhost:31444  Argo CD https://localhost:31443  (admin / $KARGO_ADMIN_PASSWORD)"
