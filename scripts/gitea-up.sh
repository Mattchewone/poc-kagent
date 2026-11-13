#!/usr/bin/env bash
# Local Gitea (poc-gitea): the one Gitea for the agent flow. App repo (poc/demo-todo), the gitops repo
# (poc/demo-todo-gitops, pushed by scripts/delivery-up.sh), PRs, and the webhook to the pipeline server.
# Idempotent: container, admin user, API token, repo and webhook are created once and kept in the
# poc-gitea volume, so `make stop` / `make start` keep every PR.
#
# Also joins the `kind` docker network, so the kargo clusters (Kargo, Argo CD) reach it as
# http://poc-gitea:3000 and the poc cluster's agents keep using host.docker.internal:${GITEA_PORT}.
set -euo pipefail
cd "$(dirname "$0")/.."; source scripts/versions.env; mkdir -p .cache
NAME=poc-gitea
URL="http://localhost:${GITEA_PORT}"
TOKEN_FILE=.cache/gitea-token

# Webhooks go to the pipeline server on the host (host.docker.internal = a private address), which
# Gitea blocks by default. Containers from before this setting are recreated; the volume keeps the data.
if docker inspect $NAME >/dev/null 2>&1 && ! docker inspect -f '{{.Config.Env}}' $NAME | grep -q ALLOWED_HOST_LIST; then
  echo "recreating $NAME to allow webhooks to the host (data volume kept)"
  docker rm -f $NAME >/dev/null
fi
if [ "$(docker inspect -f '{{.State.Running}}' $NAME 2>/dev/null || true)" != "true" ]; then
  docker start $NAME >/dev/null 2>&1 || docker run -d --name $NAME --restart unless-stopped \
    -p "${GITEA_PORT}:3000" -v poc-gitea:/data \
    -e GITEA__security__INSTALL_LOCK=true \
    -e GITEA__database__DB_TYPE=sqlite3 \
    -e GITEA__server__ROOT_URL="${URL}/" \
    -e GITEA__service__DISABLE_REGISTRATION=true \
    -e GITEA__repository__DEFAULT_BRANCH=main \
    -e GITEA__webhook__ALLOWED_HOST_LIST='*' \
    -e GITEA__webhook__DELIVER_TIMEOUT=15 \
    "gitea/gitea:${GITEA_VERSION}" >/dev/null
fi
for _ in $(seq 1 60); do curl -sf "$URL/api/healthz" >/dev/null && break; sleep 1; done
curl -sf "$URL/api/healthz" >/dev/null || { echo "gitea did not become healthy"; docker logs --tail 30 $NAME; exit 1; }

# Admin user (no-op if it exists).
docker exec -u git $NAME gitea admin user create --admin --username "$GITEA_USER" --password "$GITEA_PASSWORD" \
  --email "$GITEA_USER@poc.local" --must-change-password=false >/dev/null 2>&1 || true
AUTH=(-u "$GITEA_USER:$GITEA_PASSWORD")

if [ ! -s "$TOKEN_FILE" ] || ! curl -sf -H "Authorization: token $(cat $TOKEN_FILE)" "$URL/api/v1/user" >/dev/null; then
  curl -sf "${AUTH[@]}" -X DELETE "$URL/api/v1/users/$GITEA_USER/tokens/poc" >/dev/null 2>&1 || true
  curl -sf "${AUTH[@]}" -H 'content-type: application/json' "$URL/api/v1/users/$GITEA_USER/tokens" \
    -d '{"name":"poc","scopes":["write:repository","write:user","write:issue"]}' \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha1"])' > "$TOKEN_FILE"
fi

# demo-todo repo, seeded from demo-app/ on first run only (later runs keep merged PRs).
if ! curl -sf "${AUTH[@]}" "$URL/api/v1/repos/$GITEA_USER/$GITEA_REPO" >/dev/null; then
  curl -sf "${AUTH[@]}" -H 'content-type: application/json' "$URL/api/v1/user/repos" \
    -d "{\"name\":\"$GITEA_REPO\",\"default_branch\":\"main\",\"description\":\"Target app for the agent POC\"}" >/dev/null
  tmp=$(mktemp -d); cp -R demo-app/. "$tmp"/
  ( cd "$tmp" && git init -q -b main && git add -A \
    && git -c user.name=poc -c user.email=poc@poc.local commit -qm "demo-todo: initial import" \
    && git -c credential.helper= push -q "http://$GITEA_USER:$GITEA_PASSWORD@localhost:${GITEA_PORT}/$GITEA_USER/$GITEA_REPO.git" main )
  rm -rf "$tmp"
  echo "seeded $GITEA_USER/$GITEA_REPO from demo-app/"
fi
# On the kind network as poc-gitea (Kargo, Argo CD and the kargo clusters clone from there).
if docker network inspect kind >/dev/null 2>&1 \
   && ! docker inspect -f '{{json .NetworkSettings.Networks}}' $NAME | grep -q '"kind"'; then
  docker network connect kind $NAME
fi

# Webhook -> pipeline server (worker/pipeline_server.py /hooks/gitea), HMAC-signed with a local secret.
SECRET_FILE=.cache/gitea-webhook-secret
[ -s "$SECRET_FILE" ] || openssl rand -hex 20 > "$SECRET_FILE"
HOOK_URL="http://host.docker.internal:${PIPELINE_PORT:-8765}/hooks/gitea"
TOK=(-H "Authorization: token $(cat $TOKEN_FILE)")
if ! curl -sf "${TOK[@]}" "$URL/api/v1/repos/$GITEA_USER/$GITEA_REPO/hooks" | grep -q "$HOOK_URL"; then
  curl -sf "${TOK[@]}" -H 'content-type: application/json' "$URL/api/v1/repos/$GITEA_USER/$GITEA_REPO/hooks" -d "{
    \"type\":\"gitea\",\"active\":true,\"events\":[\"pull_request\",\"issues\",\"issue_comment\"],
    \"config\":{\"url\":\"$HOOK_URL\",\"content_type\":\"json\",\"secret\":\"$(cat $SECRET_FILE)\"}}" >/dev/null
  echo "webhook -> $HOOK_URL"
fi
echo "gitea: $URL/$GITEA_USER/$GITEA_REPO  (login $GITEA_USER / $GITEA_PASSWORD)"
