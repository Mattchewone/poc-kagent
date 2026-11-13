"""Local-only shim so kagent's Claude harness can run on a Claude subscription token.

kagent (1.0.1) always delivers the Anthropic credential as `x-api-key`: the harness gets a
placeholder and Substrate's egress gateway overwrites that header with the kagent-anthropic
Secret. Subscription OAuth tokens (sk-ant-oat..., from `claude setup-token`) are only accepted
as `Authorization: Bearer` with the oauth beta flag, so this proxy sits at the ModelConfig's
`anthropic.baseUrl` and re-encodes the header:

    Claude Code (actor) -> gateway (injects x-api-key) -> this proxy -> api.anthropic.com

It holds no credential itself and never logs one. API keys (sk-ant-api...) pass through
untouched, so the same path works when you switch to a key; for a real deployment drop the
baseUrl and this proxy entirely (scripts/kagent-apply.sh does that for API keys).

    .venv/bin/python worker/claude_auth_proxy.py      # :8766 (CLAUDE_PROXY_PORT)
"""
from __future__ import annotations

import logging
import os

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

UPSTREAM = os.environ.get("CLAUDE_PROXY_UPSTREAM", "https://api.anthropic.com")
PORT = int(os.environ.get("CLAUDE_PROXY_PORT", "8766"))
OAUTH_BETA = "oauth-2025-04-20"
HOP = {"host", "content-length", "connection", "keep-alive", "transfer-encoding", "te",
       "upgrade", "proxy-authorization", "proxy-connection", "accept-encoding"}

log = logging.getLogger("claude-auth-proxy")
client = httpx.AsyncClient(base_url=UPSTREAM, timeout=httpx.Timeout(600, connect=15))


def translate(headers: dict[str, str]) -> tuple[dict[str, str], str]:
    """Return upstream headers and the auth mode, without exposing the credential."""
    out = {k: v for k, v in headers.items() if k not in HOP}
    key = out.get("x-api-key", "")
    if key.startswith("sk-ant-oat"):
        del out["x-api-key"]
        out["authorization"] = f"Bearer {key}"
        betas = [b.strip() for b in out.get("anthropic-beta", "").split(",") if b.strip()]
        if OAUTH_BETA not in betas:
            betas.append(OAUTH_BETA)
        out["anthropic-beta"] = ",".join(betas)
        return out, "oauth"
    if key == "kagent-credential-injected":
        return out, "placeholder (gateway did not inject the Secret)"
    return out, "api-key" if key else "none"


async def proxy(request: Request):
    headers, mode = translate({k.lower(): v for k, v in request.headers.items()})
    upstream = client.build_request(request.method, request.url.path, params=request.query_params,
                                    headers=headers, content=await request.body())
    resp = await client.send(upstream, stream=True)
    log.info("%s %s -> %s (auth: %s)", request.method, request.url.path, resp.status_code, mode)
    out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in HOP | {"content-encoding"}}

    async def body():
        try:
            async for chunk in resp.aiter_bytes():  # decoded, so content-encoding is dropped above
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(body(), status_code=resp.status_code, headers=out_headers)


async def healthz(_: Request):
    return JSONResponse({"ok": True, "upstream": UPSTREAM})


app = Starlette(routes=[Route("/healthz", healthz),
                        Route("/{path:path}", proxy, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])])

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log.info("claude auth proxy on :%s -> %s", PORT, UPSTREAM)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
