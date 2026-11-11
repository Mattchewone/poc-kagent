#!/usr/bin/env bash
# Milestone 3 exit: the README's counter increments through the router, and survives suspend + resume.
set -uo pipefail
cd "$(dirname "$0")/.."; source scripts/versions.env
command -v kubectl-ate >/dev/null || { echo "kubectl-ate not installed yet: finish 'make substrate-up' first (it runs 'go install ./cmd/kubectl-ate' last)"; exit 1; }
curl -s -o /dev/null --max-time 3 "http://localhost:${SUBSTRATE_ROUTER_PORT}/" || { echo "router not reachable on :${SUBSTRATE_ROUTER_PORT}; run 'make forwards'"; exit 1; }
A="kubectl ate --context kind-poc-substrate"
R="http://localhost:${SUBSTRATE_ROUTER_PORT}/"
H="ate-target-actor: ate-demo-counter/m3-counter"
$A create actor m3-counter -a ate-demo-counter --template counter 2>/dev/null || true
c1=$(curl -s -X POST -H "$H" "$R"); c2=$(curl -s -X POST -H "$H" "$R"); echo "before suspend: $c1 -> $c2"
$A suspend actor m3-counter -a ate-demo-counter
$A get actors -a ate-demo-counter
c3=$(curl -s -X POST -H "$H" "$R"); echo "after resume-on-request: $c3"
$A get actors -a ate-demo-counter
# Only non-running actors can be deleted: suspend, then delete.
$A suspend actor m3-counter -a ate-demo-counter >/dev/null
for _ in 1 2 3 4 5; do $A delete actor m3-counter -a ate-demo-counter >/dev/null 2>&1 && break; sleep 2; done
echo "Expect three increasing counts; the third must continue from the second."
