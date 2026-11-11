#!/usr/bin/env bash
# Background port-forwards (idempotent). Each runs in a restart loop, because
# `kubectl port-forward` exits for good on "lost connection to pod".
# Logs/pids in .cache/.
set -euo pipefail
cd "$(dirname "$0")/.."; source scripts/versions.env; mkdir -p .cache
pf() { # name ctx ns svc mapping
  local pidf=.cache/pf-$1.pid
  if [ -f "$pidf" ] && kill -0 "$(cat "$pidf")" 2>/dev/null; then echo "port-forward $1 already up"; return; fi
  nohup bash -c "while true; do kubectl --context '$2' -n '$3' port-forward 'svc/$4' '$5'; echo \"[\$(date +%T)] port-forward exited, restarting\"; sleep 1; done" \
    > ".cache/pf-$1.log" 2>&1 &
  echo $! > "$pidf"; sleep 2; echo "port-forward $1: $5 (loop pid $(cat "$pidf"))"
}
stop_pf() {
  local f=$1; [ -f "$f" ] || return 0
  local pid; pid=$(cat "$f")
  pkill -P "$pid" 2>/dev/null || true   # the kubectl child
  kill "$pid" 2>/dev/null || true       # the restart loop
  rm -f "$f"
}
case "${1:-all}" in
  openshell) pf openshell kind-poc-openshell openshell openshell "${OPENSHELL_GATEWAY_PORT}:8080" ;;
  substrate) pf substrate kind-poc-substrate ate-system atenet-router "${SUBSTRATE_ROUTER_PORT}:80" ;;
  all) "$0" openshell || true; "$0" substrate || true ;;
  restart) "$0" stop; "$0" all ;;
  stop) for f in .cache/pf-*.pid; do stop_pf "$f"; done
        pkill -f "port-forward svc/(openshell|atenet-router)" 2>/dev/null || true; echo "port-forwards stopped" ;;
esac
