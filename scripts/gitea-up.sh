#!/usr/bin/env bash
# Local Gitea: where approved changes land as real pull requests.
# Idempotent: container, admin user, API token and the demo-todo repo are created once
# and kept in the poc-gitea volume, so `make stop` / `make start` keep every PR.
set -euo pipefail
cd "$(dirname "$0")/.."; source scripts/versions.env; mkdir -p .cache
NAME=poc-gitea
URL="http://localhost:${GITEA_PORT}"
TOKEN_FILE=.cache/gitea-token

if [ "$(docker inspect -f '{{.State.Running}}' $NAME 2>/dev/null || true)" != "true" ]; then
  docker start $NAME >/dev/null 2>&1 || docker run -d --name $NAME --restart unless-stopped \
    -p "${GITEA_PORT}:3000" -v poc-gitea:/data \
    -e GITEA__security__INSTALL_LOCK=true \
    -e GITEA__database__DB_TYPE=sqlite3 \
    -e GITEA__server__ROOT_URL="${URL}/" \
    -e GITEA__service__DISABLE_REGISTRATION=true \
    -e GITEA__repository__DEFAULT_BRANCH=main \
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
echo "gitea: $URL/$GITEA_USER/$GITEA_REPO  (login $GITEA_USER / $GITEA_PASSWORD)"
