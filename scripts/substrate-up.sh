#!/usr/bin/env bash
# Milestone 3: Substrate's own kind scripts at a pinned tag, counter demo, kubectl-ate,
# then this POC's atespace, WorkerPool and templates.
set -euo pipefail
cd "$(dirname "$0")/.."; ROOT=$PWD; source scripts/versions.env
export KIND_CLUSTER_NAME=poc-substrate
# Substrate's script sets KO_DEFAULTPLATFORMS=linux/<arch>; ko refuses to combine that
# with an exported GOARCH/GOOS (e.g. GOARCH=arm64 from a shell profile). An exported
# CGO_ENABLED=1 makes ko cross-compile linux binaries with the macOS C toolchain, which
# fails in runtime/cgo; ko's default (cgo off) is what Substrate's .ko.yaml expects.
# Go's own defaults still give the right host arch for `go install ./cmd/kubectl-ate`.
unset GOARCH GOOS CGO_ENABLED CC CXX
CTX=kind-poc-substrate

if [ ! -d "$SUBSTRATE_DIR/.git" ]; then
  git clone --depth 1 --branch "$SUBSTRATE_VERSION" https://github.com/agent-substrate/substrate "$SUBSTRATE_DIR"
fi
( cd "$SUBSTRATE_DIR"
  git -c advice.detachedHead=false checkout -q "$SUBSTRATE_VERSION"
  kind get clusters | grep -qx poc-substrate || hack/create-kind-cluster.sh
  hack/install-ate-kind.sh --deploy-ate-system --credential-provider='{"name":"k8s.io"}'
  hack/install-ate-kind.sh --deploy-demo-counter
  go install ./cmd/kubectl-ate )
command -v kubectl-ate >/dev/null || { echo "kubectl-ate not on PATH; add \$(go env GOPATH)/bin"; exit 1; }
"$ROOT"/scripts/port-forwards.sh substrate
echo "substrate-up done. Run: make substrate-check, then make substrate-poc"
