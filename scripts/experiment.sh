#!/usr/bin/env bash
# Run one experiment row on one runtime and append the outcome to out/results.tsv.
#   scripts/experiment.sh <happy|egress|cheat|never|long-wait|reject|revise|crash|state|cancel|concurrency> <openshell|substrate|local>
set -uo pipefail
cd "$(dirname "$0")/.."
EXP=$1; RT=$2; PY=${PY:-.venv/bin/python}; START="$PY worker/start.py"
ID="${RT:0:2}-${EXP}-$(date +%H%M%S)"
LONG_WAIT=${LONG_WAIT:-600}
mkdir -p out

status() { $PY worker/start.py status "$1"; }
summary() { # id -> one line from the status query
  status "$1" | python3 -c '
import json,sys; d=json.load(sys.stdin)
h=[f"#{x["attempt"]}:ctr={x["agent_counter"]},pass={x["passed"]},touched={x["tests_touched"]}"
   + (f",egress_ok={x["egress"]["ok"]}" if x.get("egress") else "") for x in d.get("history",[])]
print(f"result={d.get("result")} phase={d.get("phase")} " + " ".join(h))'
}
result() { temporal workflow describe --workflow-id "$1" -o json 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin)["workflowExecutionInfo"]["status"].replace("WORKFLOW_EXECUTION_STATUS_",""))'; }
await_done() { temporal workflow result --workflow-id "$1" >/dev/null 2>&1 || true; }
leftovers() {
  case $RT in
    local) docker ps -aq --filter "label=poc.run=$1" | wc -l | tr -d ' ' ;;
    openshell) openshell sandbox list --names 2>/dev/null | grep -c -- "$1" ;;
    substrate) kubectl ate --context kind-poc-substrate get actors -a poc 2>/dev/null | grep -c -- "$1" ;;
  esac
}
actor_state() { kubectl ate --context kind-poc-substrate get actors -a poc 2>/dev/null | awk -v n="agent-$1" '$2==n{print $4}'; }

case $EXP in
  happy|egress|cheat|never)
    $START --runtime "$RT" --scenario "$EXP" --id "$ID" --approve --wait >/dev/null 2>&1 ;;
  reject)
    $START --runtime "$RT" --scenario happy --id "$ID" --reject --wait >/dev/null 2>&1 ;;
  revise)  # reviewer asks for a change, the agent revises, the second review approves
    $START --runtime "$RT" --scenario happy --id "$ID" >/dev/null
    $START reject "$ID" "please keep the original docstring" >/dev/null
    $START approve "$ID" >/dev/null; await_done "$ID" ;;
  long-wait)
    $START --runtime "$RT" --scenario happy --id "$ID" >/dev/null
    sleep 20; [ "$RT" = substrate ] && echo "agent actor state while awaiting review: $(actor_state "$ID")"
    echo "waiting ${LONG_WAIT}s before approving..."; sleep $((LONG_WAIT - 20))
    [ "$RT" = substrate ] && EXTRA="actor_state_before_approve=$(actor_state "$ID")"
    $START approve "$ID" >/dev/null; await_done "$ID" ;;
  crash)
    [ -f .cache/worker.pid ] || { echo "start the worker with 'make worker-bg' so it can be killed"; exit 2; }
    $START --runtime "$RT" --scenario slow --id "$ID" >/dev/null 2>&1
    $START approve "$ID" >/dev/null 2>&1 &   # waits through the crash for the review
    sleep 8; echo "killing worker $(cat .cache/worker.pid) mid-sleep"; kill -9 "$(cat .cache/worker.pid)"; sleep 3
    make -s worker-bg; await_done "$ID"; wait
    EXTRA="implement_attempts=$(temporal workflow show --workflow-id "$ID" -o json | python3 -c '
import json,sys
ev=json.load(sys.stdin)["events"]
print(max([int(e.get("activityTaskStartedEventAttributes",{}).get("attempt",0)) for e in ev] or [0]))')" ;;
  state)
    $START --runtime "$RT" --scenario happy --id "$ID" --approve --wait >/dev/null 2>&1 ;;
  cancel)
    $START --runtime "$RT" --scenario slow --id "$ID" >/dev/null 2>&1
    sleep 8; temporal workflow cancel --workflow-id "$ID" >/dev/null; sleep 15 ;;
  concurrency)
    for i in 1 2 3 4 5; do $START --runtime "$RT" --scenario happy --id "$ID-$i" --approve >/dev/null 2>&1 & done; wait
    for i in 1 2 3 4 5; do await_done "$ID-$i"; done
    [ "$RT" = openshell ] && EXTRA="pods=$(kubectl --context kind-poc-openshell get pods -A --no-headers | wc -l | tr -d ' ')"
    [ "$RT" = substrate ] && EXTRA="pool_pods=$(kubectl --context kind-poc-substrate get pods -n poc --no-headers | wc -l | tr -d ' ')"
    line="$(for i in 1 2 3 4 5; do printf '%s ' "$(result "$ID-$i")"; done)"
    printf '%s\t%s\t%s\t%s\t%s\n' "$(date -u +%FT%TZ)" "$EXP" "$RT" "$ID-*" "$line ${EXTRA:-} leftovers=$(leftovers "$ID")" | tee -a out/results.tsv
    exit 0 ;;
  *) echo "unknown experiment $EXP"; exit 2 ;;
esac
line="status=$(result "$ID") $(summary "$ID") ${EXTRA:-} leftovers=$(leftovers "$ID")"
printf '%s\t%s\t%s\t%s\t%s\n' "$(date -u +%FT%TZ)" "$EXP" "$RT" "$ID" "$line" | tee -a out/results.tsv
