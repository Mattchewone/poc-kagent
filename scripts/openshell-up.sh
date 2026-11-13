#!/usr/bin/env bash
# Milestone 2: kind + Calico + Agent Sandbox + OpenShell chart; CLI registered to the gateway.
set -euo pipefail
cd "$(dirname "$0")/.."; source scripts/versions.env
CTX=kind-poc-openshell

if ! kind get clusters | grep -qx poc-openshell; then
  kind create cluster --config clusters/openshell/kind.yaml
fi
kubectl --context $CTX apply -f "https://raw.githubusercontent.com/projectcalico/calico/${CALICO_VERSION}/manifests/calico.yaml"
kubectl --context $CTX -n kube-system rollout status ds/calico-node --timeout=300s
kubectl --context $CTX wait --for=condition=Ready nodes --all --timeout=300s

kubectl --context $CTX apply -f "https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${AGENT_SANDBOX_VERSION}/sandbox.yaml"
kubectl --context $CTX -n agent-sandbox-system rollout status deploy --timeout=300s

kubectl --context $CTX create namespace openshell --dry-run=client -o yaml | kubectl --context $CTX apply -f -
helm --kube-context $CTX upgrade --install openshell oci://ghcr.io/nvidia/openshell/helm-chart \
  --version "${OPENSHELL_VERSION#v}" --namespace openshell -f clusters/openshell/values.yaml
kubectl --context $CTX -n openshell rollout status statefulset/openshell --timeout=300s

if ! command -v openshell >/dev/null; then
  echo "Installing openshell CLI ${OPENSHELL_VERSION} (macOS: via Homebrew; also starts a local gateway service you can stop with 'brew services stop openshell')"
  curl -LsSf https://raw.githubusercontent.com/NVIDIA/OpenShell/main/install.sh | OPENSHELL_VERSION=${OPENSHELL_VERSION} sh
fi

echo "Loading image into poc-openshell"
kind load docker-image mock-poc-agent:dev --name poc-openshell

scripts/port-forwards.sh legacy-openshell
openshell gateway add "http://127.0.0.1:${OPENSHELL_GATEWAY_PORT}" --local --name poc 2>/dev/null || true
openshell gateway select poc 2>/dev/null || true
openshell status
echo "openshell-up done. Run: make openshell-check"
