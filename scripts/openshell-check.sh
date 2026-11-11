#!/usr/bin/env bash
# Milestone 2 exit: default-deny blocks pod-to-pod curl; `openshell sandbox create -- echo ok` prints ok.
set -uo pipefail
cd "$(dirname "$0")/.."; CTX=kind-poc-openshell
kubectl --context $CTX apply -f clusters/openshell/netpol-check.yaml >/dev/null
kubectl --context $CTX -n netpol-check wait --for=condition=Ready pod --all --timeout=120s >/dev/null
ip=$(kubectl --context $CTX -n netpol-check get pod server -o jsonpath='{.status.podIP}')
if kubectl --context $CTX -n netpol-check exec client -- curl -s --max-time 5 "http://$ip" >/dev/null 2>&1; then
  echo "FAIL  pod-to-pod curl succeeded under default-deny (NetworkPolicy not enforced)"; rc=1
else
  echo "ok    default-deny blocked pod-to-pod curl"; rc=0
fi
kubectl --context $CTX delete ns netpol-check --wait=false >/dev/null
out=$(openshell sandbox create --name m2-check --no-auto-providers --no-tty -- echo ok 2>&1); echo "$out" | tail -3
openshell sandbox delete m2-check >/dev/null 2>&1 || true
echo "$out" | grep -qx ok && echo "ok    sandbox printed ok" || { echo "FAIL  sandbox did not print ok"; rc=1; }
exit $rc
