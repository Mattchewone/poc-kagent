#!/usr/bin/env bash
# Build the POC images; with `push`, push them to the local registry and record digests.
#   mock-poc-app      verify + preview supervisor (OpenShell sandboxes, local docker)
#   mock-poc-harness  kagent Claude harness + node (the dev agent's Substrate actor)
#   mock-poc-agent    the original fake agent (milestones 1-8)
# Digests land in .cache/images.env; kagent requires the harness image pinned by sha256.
set -euo pipefail
cd "$(dirname "$0")/.."; mkdir -p .cache
REG=localhost:5001
docker build -q -f images/app/Dockerfile -t mock-poc-app:dev . >/dev/null
docker build -q -f images/harness/Dockerfile -t mock-poc-harness:dev . >/dev/null
docker build -q -f images/Dockerfile -t mock-poc-agent:dev . >/dev/null
echo "built mock-poc-app, mock-poc-harness, mock-poc-agent"
[ "${1:-}" = push ] || exit 0

: > .cache/images.env
for img in mock-poc-app mock-poc-harness; do
  docker tag "$img:dev" "$REG/$img:dev"
  docker push -q "$REG/$img:dev" >/dev/null
  digest=$(docker inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$REG/$img:dev" | grep "^$REG/$img@" | head -1)
  [ -n "$digest" ] || { echo "no digest for $REG/$img:dev"; exit 1; }
  var=$(echo "${img#mock-poc-}" | tr '[:lower:]' '[:upper:]')_IMAGE
  echo "$var=$digest" >> .cache/images.env
  echo "pushed $digest"
done
