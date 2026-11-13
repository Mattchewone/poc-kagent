"""Pipeline server: the coding agent's only way to ship code, plus pages humans read.

    python worker/pipeline_server.py            # :8765, reachable from kind as host.docker.internal:8765

MCP (streamable HTTP), two endpoints because kagent's Claude harness gates whole servers, not
individual tools:
  /mcp        submit_change, get_change, abandon_change          (no approval)
  /mcp-gated  open_pull_request                                   (Agent binds it with requireApproval)

Pages:
  /                    recent changes
  /changes/<id>        phase, test results, diff, preview + PR links
"""
from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import os
import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import HTMLResponse, JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowUpdateFailedError  # noqa: E402
from temporalio.service import RPCError  # noqa: E402

from worker.change import CHANGE_QUEUE, ChangeRequest, ChangeWorkflow, Revision, summarize  # noqa: E402

log = logging.getLogger("poc.pipeline")
PORT = int(os.environ.get("PIPELINE_PORT", "8765"))
RUNTIME = os.environ.get("APP_RUNTIME", "openshell")  # where verify + preview run
SUBMIT_WAIT = float(os.environ.get("SUBMIT_WAIT", "480"))  # stay under the RemoteMCPServer timeout
PUBLIC_URL = os.environ.get("PIPELINE_PUBLIC_URL", f"http://localhost:{PORT}")
_client: Client | None = None


async def client() -> Client:
    global _client
    if _client is None:
        _client = await Client.connect(os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"))
    return _client


def _new_id(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:24].strip("-") or "change"
    return f"{slug}-{uuid.uuid4().hex[:6]}"


async def _status(change_id: str) -> dict:
    return await (await client()).get_workflow_handle(change_id).query(ChangeWorkflow.status)


async def _wait_for_revision(change_id: str, rev_id: str) -> dict:
    """Block until this revision is verified (or superseded, or the change ends)."""
    h = (await client()).get_workflow_handle(change_id)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SUBMIT_WAIT
    while True:
        try:
            st = await h.query(ChangeWorkflow.status)
        except RPCError:
            st = {}
        if rev_id in st.get("superseded", []):
            return {**summarize(st), "note": "this revision was superseded by a newer submission"}
        mine = [r for r in st.get("revisions", []) if r.get("rev_id") == rev_id]
        if mine and "passed" in mine[-1] or st.get("phase") == "done":
            return summarize(st)
        if loop.time() > deadline:
            return {**summarize(st), "note": "still verifying; call get_change to check again"}
        await asyncio.sleep(1.5)


def _links(change_id: str, out: dict) -> dict:
    out["change_page"] = f"{PUBLIC_URL}/changes/{change_id}"
    return out


# ---- MCP: ungated ----------------------------------------------------------

_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)  # called from kind via host.docker.internal
pub = FastMCP("pipeline", stateless_http=True, json_response=True, streamable_http_path="/mcp",
              transport_security=_security, instructions=(
                  "Ship code changes to the demo-todo app. submit_change verifies your diff in a fresh, "
                  "network-less sandbox (your tests plus the original tests restored) and, if it passes, "
                  "gives you a preview URL for the human to review."))


@pub.tool()
async def submit_change(title: str, kind: str, description: str, diff: str, summary: str,
                        change_id: str | None = None) -> dict:
    """Submit a unified diff (output of `git diff` against origin/main, including new files) for verification.

    Omit change_id for a new change; pass the change_id you got back to submit a revision of the same change
    (always send the full diff against main, not an incremental one). kind is "feature" or "bug".
    Blocks until verification finishes. On success the result has preview_url for the human to review.
    """
    kind = kind.lower().strip()
    if kind not in ("feature", "bug"):
        return {"error": "kind must be 'feature' or 'bug'"}
    if not diff.strip():
        return {"error": "diff is empty: run `git diff origin/main` (and `git add -N` new files first)"}
    rev = Revision(diff=diff, summary=summary, rev_id=uuid.uuid4().hex[:10])
    c = await client()
    if change_id:
        try:
            await c.get_workflow_handle(change_id).execute_update(ChangeWorkflow.submit, rev)
        except (WorkflowUpdateFailedError, RPCError) as e:
            return {"error": f"could not add a revision to {change_id}: {getattr(e, 'cause', None) or e}"}
    else:
        change_id = _new_id(title)
        req = ChangeRequest(change_id=change_id, title=title, kind=kind, description=description,
                            first=rev, runtime=RUNTIME)
        await c.start_workflow(ChangeWorkflow.run, req, id=change_id, task_queue=CHANGE_QUEUE)
    log.info("submitted %s rev %s", change_id, rev.rev_id)
    return _links(change_id, await _wait_for_revision(change_id, rev.rev_id))


@pub.tool()
async def get_change(change_id: str, verbose: bool = False) -> dict:
    """Current phase, test results, preview URL and PR (if opened) for a change."""
    try:
        return _links(change_id, summarize(await _status(change_id), verbose))
    except RPCError as e:
        return {"error": f"unknown change {change_id}: {e}"}


@pub.tool()
async def abandon_change(change_id: str, reason: str = "") -> dict:
    """Stop a change: its preview is torn down and no PR is opened."""
    await (await client()).get_workflow_handle(change_id).signal(ChangeWorkflow.abandon, reason)
    return {"change_id": change_id, "status": "abandoning"}


# ---- MCP: gated ------------------------------------------------------------

gated = FastMCP("pipeline-release", stateless_http=True, json_response=True, streamable_http_path="/mcp-gated",
                transport_security=_security, instructions="Open a pull request for a verified change. Requires human approval.")


@gated.tool()
async def open_pull_request(change_id: str) -> dict:
    """Open a pull request on Gitea for the change's latest verified revision.

    Only call this after the human has looked at the preview and said they are happy with it.
    A human must approve this call in the kagent UI before it runs.
    """
    h = (await client()).get_workflow_handle(change_id)
    try:
        pr = await h.execute_update(ChangeWorkflow.approve)
    except (WorkflowUpdateFailedError, RPCError) as e:
        return {"error": str(getattr(e, "cause", None) or e)}
    return _links(change_id, {"change_id": change_id, "pr": pr})


# ---- pages -----------------------------------------------------------------

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--muted:#687087;--line:#e1e4eb;--ok:#1d8a4a;--bad:#c0392b;--accent:#3b5bdb}
@media (prefers-color-scheme:dark){:root{--bg:#11141a;--card:#1a1f28;--fg:#e5e8ef;--muted:#99a1b3;--line:#2b313d;--ok:#5bd08a;--bad:#ff7a6b;--accent:#8aa0ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{max-width:980px;margin:32px auto;padding:0 16px}a{color:var(--accent)}h1{font-size:22px;margin:0 0 4px}
.muted{color:var(--muted)}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:16px 0}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:8px;border-bottom:1px solid var(--line);vertical-align:top}
.ok{color:var(--ok)}.bad{color:var(--bad)}pre{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:12px;overflow:auto;font-size:12.5px;max-height:480px}
.pill{display:inline-block;padding:2px 10px;border-radius:99px;border:1px solid var(--line);font-size:13px}
details summary{cursor:pointer}
"""


def _page(title: str, body: str, refresh: bool = False) -> HTMLResponse:
    meta = '<meta http-equiv="refresh" content="5">' if refresh else ""
    return HTMLResponse(f"<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                        f"{meta}<title>{html.escape(title)}</title><style>{CSS}</style></head><body><main>{body}</main></body></html>")


async def index(_: Request) -> HTMLResponse:
    rows = []
    async for wf in (await client()).list_workflows("WorkflowType='ChangeWorkflow'", limit=50):
        rows.append(f"<tr><td><a href='/changes/{wf.id}'>{html.escape(wf.id)}</a></td><td>{wf.status.name.lower()}</td>"
                    f"<td class=muted>{wf.start_time:%Y-%m-%d %H:%M}</td></tr>")
    table = "<table><tr><th>Change</th><th>Workflow</th><th>Started</th></tr>" + "".join(rows) + "</table>" if rows \
        else "<p class=muted>No changes yet. Ask the dev agent for a feature or a bug fix in the kagent UI.</p>"
    return _page("Changes", f"<h1>Changes</h1><p class=muted>Submitted by the dev agent through the pipeline MCP server.</p><div class=card>{table}</div>")


def _tests(v: dict) -> str:
    def one(label: str, t: dict) -> str:
        if not t:
            return ""
        cls = "ok" if t.get("ok") else "bad"
        return f"<span class='pill {cls}'>{label}: {t.get('pass', 0)} passed, {t.get('fail', 0)} failed</span> "
    return one("agent's tests", v.get("agent_tests", {})) + one("base tests restored", v.get("base_tests", {}))


async def change_page(req: Request) -> HTMLResponse:
    cid = req.path_params["cid"]
    h = (await client()).get_workflow_handle(cid)
    try:
        st = await h.query(ChangeWorkflow.status)
    except RPCError:
        return _page("Not found", f"<h1>Unknown change</h1><p>{html.escape(cid)}</p>")
    desc = await h.describe()
    running = desc.status == WorkflowExecutionStatus.RUNNING
    links = []
    if st.get("preview_url"):
        links.append(f"<a href='{html.escape(st['preview_url'])}' target=_blank>Open preview →</a>")
    if st.get("pr"):
        links.append(f"<a href='{html.escape(st['pr']['url'])}' target=_blank>Pull request #{st['pr']['number']} →</a>")
    revs = []
    for r in reversed(st.get("revisions", [])):
        v = r.get("verify", {})
        status = "verifying…" if "passed" not in r else ("<span class=ok>passed</span>" if r["passed"] else "<span class=bad>failed</span>")
        warn = f"<p class=bad>Changed existing tests: {', '.join(map(html.escape, v['weakened']))}</p>" if v.get("weakened") else ""
        err = f"<p class=bad>{html.escape(v['error'])}</p>" if v.get("error") else ""
        revs.append(
            f"<div class=card><h3 style='margin:0'>Revision {r['n']} — {status}</h3>"
            f"<p>{html.escape(r.get('summary') or '')}</p><p>{_tests(v)}</p>{warn}{err}"
            f"<details><summary>Diff</summary><pre>{html.escape(r.get('diff', ''))}</pre></details>"
            f"<details><summary>Test output</summary><pre>{html.escape(v.get('output', ''))}</pre></details></div>")
    body = (f"<p class=muted><a href='/'>← all changes</a></p><h1>{html.escape(st.get('title', cid))}</h1>"
            f"<p class=muted>{html.escape(st.get('kind', ''))} · {html.escape(cid)} · verify/preview on {html.escape(st.get('runtime', ''))}</p>"
            f"<div class=card><p><b>Request:</b> {html.escape(st.get('description', ''))}</p>"
            f"<p><b>Phase:</b> <span class=pill>{html.escape(st.get('phase', ''))}</span> {html.escape(st.get('result', '') or '')}</p>"
            f"<p>{' &nbsp; '.join(links)}</p></div>" + "".join(revs))
    return _page(st.get("title", cid), body, refresh=running)


async def change_json(req: Request) -> JSONResponse:
    return JSONResponse(await _status(req.path_params["cid"]))


async def healthz(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


def app() -> Starlette:
    pub_app, gated_app = pub.streamable_http_app(), gated.streamable_http_app()

    @contextlib.asynccontextmanager
    async def lifespan(_):
        async with pub.session_manager.run(), gated.session_manager.run():
            yield

    routes = [Route("/", index), Route("/healthz", healthz), Route("/changes/{cid}", change_page),
              Route("/api/changes/{cid}", change_json), *pub_app.routes, *gated_app.routes]
    return Starlette(routes=routes, lifespan=lifespan)


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log.info("pipeline server on :%s (verify/preview runtime: %s)", PORT, RUNTIME)
    uvicorn.run(app(), host="0.0.0.0", port=PORT, log_level="warning")
