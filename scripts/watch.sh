#!/usr/bin/env bash
# One pane per layer. Usage: scripts/watch.sh <run id>   (RUN=... make watch)
# Open the Temporal UI yourself at http://localhost:8233.
set -euo pipefail
cd "$(dirname "$0")/.."
RUN=${1:-${RUN:-}}; [ -n "$RUN" ] || { echo "usage: $0 <run id>"; exit 2; }
S=poc-watch
OS=kind-poc-openshell; SUB=kind-poc-substrate
# OpenShell caps names at 19 chars; ask the adapter what it calls this run's agent sandbox.
AGENT_SB=$(.venv/bin/python -c "import sys; sys.path.insert(0, '.'); from worker.runtimes.openshell import sandbox_name; print(sandbox_name('agent', '$RUN'))")
tmux kill-session -t $S 2>/dev/null || true
tmux new-session -d -s $S -n run "watch -n1 temporal workflow query --workflow-id $RUN --name status"
tmux split-window -t $S:run -v "tail -n 50 -F .cache/worker.log"
tmux select-layout -t $S:run even-vertical
tmux new-window -t $S -n openshell "kubectl --context $OS get sandboxes.agents.x-k8s.io -A -w"
tmux split-window -t $S:openshell -v "bash -c 'until openshell sandbox get $AGENT_SB >/dev/null 2>&1; do sleep 2; done; openshell logs $AGENT_SB --tail --source sandbox'"   # supervisor log: NET:REFUSE lines on egress
tmux new-window -t $S -n substrate "watch -n1 kubectl ate --context $SUB get actors -a poc"
tmux split-window -t $S:substrate -v "kubectl --context $SUB get pods -n poc -w"
tmux select-window -t $S:run
exec tmux attach -t $S
