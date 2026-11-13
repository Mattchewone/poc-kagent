#!/usr/bin/env bash
# Host-side processes: Temporal dev server, worker, pipeline MCP server, Claude auth proxy. Pid files + logs in .cache/.
#   scripts/dev.sh start [openshell|local]   (verify/preview runtime; local needs no cluster)
#   scripts/dev.sh stop | stop-pipeline | status
set -euo pipefail
cd "$(dirname "$0")/.."; mkdir -p .cache
PY=.venv/bin/python

alive() { [ -f ".cache/$1.pid" ] && kill -0 "$(cat ".cache/$1.pid")" 2>/dev/null; }
stop_one() { if alive "$1"; then kill "$(cat ".cache/$1.pid")"; echo "stopped $1"; fi; rm -f ".cache/$1.pid"; }

case "${1:-start}" in
  start)
    rt=${2:-openshell}
    if ! temporal operator cluster health 2>/dev/null | grep -q SERVING; then
      nohup temporal server start-dev --db-filename .cache/temporal.db \
        --search-attribute Runtime=Keyword --search-attribute Scenario=Keyword >> .cache/temporal.log 2>&1 &
      echo $! > .cache/temporal.pid
      for _ in $(seq 1 30); do temporal operator cluster health 2>/dev/null | grep -q SERVING && break; sleep 1; done
      echo "temporal up (UI http://localhost:8233)"
    fi
    alive worker || { nohup $PY worker/main.py >> .cache/worker.log 2>&1 & echo $! > .cache/worker.pid; echo "worker up"; }
    stop_one pipeline >/dev/null
    APP_RUNTIME=$rt nohup $PY worker/pipeline_server.py >> .cache/pipeline.log 2>&1 & echo $! > .cache/pipeline.pid
    for _ in $(seq 1 20); do curl -sf localhost:8765/healthz >/dev/null && break; sleep 0.5; done
    echo "pipeline up on :8765 (verify/preview on $rt)   work items: http://localhost:8765"
    # Re-encodes a subscription token for kagent (worker/claude_auth_proxy.py); idle with an API key.
    alive claude-proxy || { nohup $PY worker/claude_auth_proxy.py >> .cache/claude-proxy.log 2>&1 & echo $! > .cache/claude-proxy.pid
      echo "claude auth proxy up on :${CLAUDE_PROXY_PORT:-8766}"; }
    scripts/app.sh start || echo "warning: app (main) not started" >&2
    ;;
  stop) scripts/app.sh stop; stop_one claude-proxy; stop_one pipeline; stop_one worker; stop_one temporal ;;
  stop-pipeline) stop_one pipeline ;;
  status) for p in temporal worker pipeline claude-proxy; do alive $p && echo "$p: running" || echo "$p: stopped"; done; scripts/app.sh status ;;
esac
