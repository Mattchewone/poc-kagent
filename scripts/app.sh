#!/usr/bin/env bash
# The demo app as it is on Gitea main, on http://localhost:3000 (APP_MAIN_PORT).
# Follows main: polls Gitea every few seconds and restarts the app when a PR is merged.
# Previews of unmerged changes are separate (OpenShell sandboxes, URLs from the agent).
#   scripts/app.sh start | stop | status
set -euo pipefail
cd "$(dirname "$0")/.."; source scripts/versions.env; mkdir -p .cache
PORT=${APP_MAIN_PORT:-3000}
DIR=.cache/app-main
REMOTE="http://localhost:$GITEA_PORT/$GITEA_USER/$GITEA_REPO.git"
PID=.cache/app-main.pid LOG=.cache/app-main.log

alive() { [ -f "$PID" ] && kill -0 "$(cat "$PID")" 2>/dev/null; }

follow() {  # runs in the background: keep the app on the latest main
  local server="" seen=""
  trap '[ -n "$server" ] && kill "$server" 2>/dev/null; exit 0' TERM INT
  while true; do
    if git -C "$DIR" fetch -q origin main 2>/dev/null; then
      head=$(git -C "$DIR" rev-parse origin/main)
      if [ "$head" != "$seen" ]; then
        git -C "$DIR" reset -q --hard origin/main
        [ -n "$server" ] && kill "$server" 2>/dev/null && wait "$server" 2>/dev/null || true
        (cd "$DIR" && PORT=$PORT exec node src/server.js) & server=$!
        echo "$(date +%T) main @ ${head:0:8}: $(git -C "$DIR" log -1 --format=%s)"
        seen=$head
      fi
    fi
    sleep 5 & wait $! || true   # interruptible, so stop takes effect at once
  done
}

case "${1:-start}" in
  start)
    alive && { echo "app (main) already on http://localhost:$PORT"; exit 0; }
    if lsof -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then echo "port $PORT is busy; set APP_MAIN_PORT" >&2; exit 1; fi
    [ -d "$DIR/.git" ] || git clone -q "$REMOTE" "$DIR" || { echo "cannot clone $REMOTE (is Gitea up? make gitea)" >&2; exit 1; }
    follow >> "$LOG" 2>&1 & echo $! > "$PID"
    for _ in $(seq 1 20); do curl -sf "localhost:$PORT/api/stats" >/dev/null && break; sleep 0.3; done
    echo "app (main) on http://localhost:$PORT, follows Gitea main"
    ;;
  stop) if alive; then kill "$(cat "$PID")"; echo "stopped app (main)"; fi; rm -f "$PID" ;;
  status) alive && echo "app-main: running (http://localhost:$PORT)" || echo "app-main: stopped" ;;
  *) echo "usage: $0 start|stop|status"; exit 2 ;;
esac
