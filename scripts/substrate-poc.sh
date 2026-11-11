#!/usr/bin/env bash
# Push the image to Substrate's local registry, pin by digest, create atespace, pool and templates.
set -euo pipefail
cd "$(dirname "$0")/.."; CTX=kind-poc-substrate; A="kubectl ate --context $CTX"
REG=localhost:5001
docker tag mock-poc-agent:dev $REG/mock-poc-agent:dev
docker push -q $REG/mock-poc-agent:dev
IMAGE=$(docker inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' $REG/mock-poc-agent:dev | grep "^$REG/" | head -1)
[ -n "$IMAGE" ] || { echo "no digest for $REG/mock-poc-agent:dev"; exit 1; }
echo "image: $IMAGE"
$A create atespace poc 2>/dev/null || echo "atespace poc exists"
mkdir -p .cache
# workerImage must be a real image, not ko://. Reuse the one Substrate's installer built
# for the counter demo's pool (same ateom-gvisor, already in the kind registry).
WORKER_IMAGE=$(kubectl --context $CTX -n ate-demo-counter get workerpool counter -o jsonpath='{.spec.workerImage}')
[[ "$WORKER_IMAGE" == *@sha256:* ]] || { echo "could not read resolved workerImage from ate-demo-counter/counter"; exit 1; }
echo "worker image: $WORKER_IMAGE"
sed "s#\${WORKER_IMAGE}#$WORKER_IMAGE#" clusters/substrate/workerpool.yaml > .cache/workerpool.yaml
kubectl --context $CTX apply -f .cache/workerpool.yaml
kubectl --context $CTX -n poc rollout status deploy/poc --timeout=300s 2>/dev/null \
  || kubectl --context $CTX -n poc get pods
for t in agent test; do
  sed "s#\${IMAGE}#$IMAGE#" clusters/substrate/$t-template.yaml > .cache/$t-template.yaml
  # Templates are immutable in practice; recreate so a new digest takes effect.
  # Delete is asynchronous: wait until the name is free before re-creating.
  $A delete actor-template $t -a poc >/dev/null 2>&1 || true
  for _ in $(seq 1 30); do $A get actor-templates -a poc 2>/dev/null | awk 'NR>1{print $2}' | grep -qx $t || break; sleep 2; done
  $A create actor-template -f .cache/$t-template.yaml
done
# Actors can only start once each template has its golden snapshot (GOLDEN TAG set).
echo "waiting for golden snapshots..."
for _ in $(seq 1 60); do
  ready=$($A get actor-templates -a poc -o json 2>/dev/null | python3 -c '
import json,sys
ts=json.load(sys.stdin).get("actorTemplates",[])
print(sum(1 for t in ts if ((t.get("status") or {}).get("goldenSnapshotStatus") or {}).get("goldenTag")))' 2>/dev/null || echo 0)
  [ "${ready:-0}" -ge 2 ] && break; sleep 5
done
$A get actor-templates -a poc
