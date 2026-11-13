#!/usr/bin/env bash
# Render and apply dev-agent + delivery-agent (clusters/poc/kagent/) with the pushed harness image digest.
#   scripts/kagent-apply.sh          apply / update
#   scripts/kagent-apply.sh key      set the Anthropic credential from $ANTHROPIC_API_KEY (or .env)
#
# The credential can be either kind; the mode follows its prefix (override with CLAUDE_AUTH=api|subscription):
#   sk-ant-api...  API key. ModelConfig talks to api.anthropic.com directly (the production shape).
#   sk-ant-oat...  Subscription token from `claude setup-token` (local experiments only). ModelConfig
#                  gets anthropic.baseUrl = the host's claude auth proxy (worker/claude_auth_proxy.py,
#                  started by `make dev`), which re-sends the gateway-injected token as a Bearer header.
set -euo pipefail
cd "$(dirname "$0")/.."; source scripts/versions.env
[ -f .env ] && set -a && source .env && set +a
CTX=kind-$CLUSTER; K="kubectl --context $CTX -n kagent"
PROXY_PORT=${CLAUDE_PROXY_PORT:-8766}
PROXY_URL="http://host.docker.internal:$PROXY_PORT"

auth_mode() {
  if [ -n "${CLAUDE_AUTH:-}" ]; then echo "$CLAUDE_AUTH"; return; fi
  case "${ANTHROPIC_API_KEY:-}" in sk-ant-oat*) echo subscription ;; *) echo api ;; esac
}

# Point the ModelConfig at the proxy (subscription) or straight at Anthropic (api).
set_endpoint() {
  if [ "$(auth_mode)" = subscription ]; then
    $K patch modelconfig dev-agent-model --type merge -p "{\"spec\":{\"anthropic\":{\"baseUrl\":\"$PROXY_URL\"}}}" >/dev/null
    echo "auth: subscription token via $PROXY_URL (local only; switch to an sk-ant-api key for real deployments)"
    curl -sf -m 3 "localhost:$PROXY_PORT/healthz" >/dev/null \
      || echo "warning: claude auth proxy is not running on :$PROXY_PORT; start it with 'make dev'" >&2
  else
    $K patch modelconfig dev-agent-model --type merge -p '{"spec":{"anthropic":null}}' >/dev/null
    echo "auth: API key, direct to api.anthropic.com"
  fi
}

if [ "${1:-}" = key ]; then
  : "${ANTHROPIC_API_KEY:?set ANTHROPIC_API_KEY (environment or .env)}"
  case "$ANTHROPIC_API_KEY" in
    sk-ant-oat*) [ "$(auth_mode)" = subscription ] || { echo "sk-ant-oat token with CLAUDE_AUTH=api: Anthropic rejects it as x-api-key" >&2; exit 1; } ;;
    sk-ant-api*)
      code=$(curl -sS -m 20 -o /dev/null -w '%{http_code}' https://api.anthropic.com/v1/models \
        -H "x-api-key: $ANTHROPIC_API_KEY" -H 'anthropic-version: 2023-06-01' || echo 000)
      case "$code" in
        200) echo "key accepted by api.anthropic.com" ;;
        401|403) echo "api.anthropic.com rejected the key (HTTP $code); not updating the cluster" >&2; exit 1 ;;
        *) echo "warning: could not verify the key (HTTP $code); updating anyway" >&2 ;;
      esac ;;
    *) echo "warning: ANTHROPIC_API_KEY is neither an API key (sk-ant-api...) nor a subscription token (sk-ant-oat...)" >&2 ;;
  esac
  $K create secret generic kagent-anthropic --from-literal=ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
    --dry-run=client -o yaml | $K apply -f - >/dev/null
  echo "kagent-anthropic secret updated (Substrate's gateway injects it; agents never see it)"
  set_endpoint
  exit 0
fi

[ -f .cache/images.env ] || scripts/images.sh push
source .cache/images.env
export HARNESS_IMAGE GITEA_PORT GITEA_USER GITEA_REPO
export PIPELINE_PORT="${PIPELINE_PORT:-8765}" DEV_AGENT_MODEL="${DEV_AGENT_MODEL:-claude-sonnet-5-5}"
render() { python3 -c 'import os,re,sys; print(re.sub(r"\$\{([A-Z_]+)\}", lambda m: os.environ[m.group(1)], open(sys.argv[1]).read()), end="")' "$1"; }

for a in dev-agent delivery-agent; do
  render clusters/poc/kagent/$a-prompt.md > .cache/$a-prompt.md
  $K create configmap $a-prompt --from-file=prompt.md=.cache/$a-prompt.md --dry-run=client -o yaml | $K apply -f - >/dev/null
done
render clusters/poc/kagent/dev-agent.yaml | $K apply -f -
$K delete remotemcpserver pipeline-release --ignore-not-found   # replaced by delivery-release
set_endpoint
echo "agents applied: dev-agent (invoked by Temporal), delivery-agent (chat) (harness image $HARNESS_IMAGE, model $DEV_AGENT_MODEL)"
