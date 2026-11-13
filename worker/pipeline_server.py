"""Pipeline server: how people and agents reach the WorkItemWorkflows in Temporal.

    python worker/pipeline_server.py            # :8765, reachable from kind as host.docker.internal:8765

MCP (streamable HTTP). Three endpoints, because kagent's Claude harness gates whole servers, not tools:
  /mcp         dev-agent      submit_revision, get_work                         (Temporal invokes it)
  /mcp-chat    delivery-agent start_work, get_work, wait_for_work, list_work,
                              request_changes, abandon_work                     (no approval)
  /mcp-gated   delivery-agent merge_pull_request, promote                       (requireApproval)

Webhooks:
  /hooks/gitea   pull_request closed/merged -> Signal pr_event
                 `/agent ...` comment on a PR -> Signal feedback
                 issue opened with `/agent` in it, or `/agent ...` on an issue -> start a work item

Pages:
  /              work items
  /work/<id>     phase, agent turns, revisions + test results, PR, preview, release, stages
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import html
import json
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
from temporalio.common import WorkflowIDReusePolicy  # noqa: E402
from temporalio.exceptions import WorkflowAlreadyStartedError  # noqa: E402
from temporalio.service import RPCError  # noqa: E402

from worker import delivery, gitea  # noqa: E402
from worker.change import Revision  # noqa: E402
from worker.work import (  # noqa: E402
    HUMAN_PHASES, STAGES, WORK_QUEUE, Feedback, PrEvent, WorkItemWorkflow, WorkRequest, summarize,
)

log = logging.getLogger("poc.pipeline")
ROOT = Path(__file__).resolve().parents[1]
PORT = int(os.environ.get("PIPELINE_PORT", "8765"))
RUNTIME = os.environ.get("APP_RUNTIME", "openshell")  # where verification runs
WAIT_MAX = float(os.environ.get("WAIT_MAX", "480"))  # stay under the RemoteMCPServer timeout
PUBLIC_URL = os.environ.get("PIPELINE_PUBLIC_URL", f"http://localhost:{PORT}")
SECRET_FILE = ROOT / ".cache/gitea-webhook-secret"
_client: Client | None = None


async def client() -> Client:
    global _client
    if _client is None:
        _client = await Client.connect(os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"))
    return _client


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:24].strip("-") or "work"


async def _status(work_id: str) -> dict:
    return await (await client()).get_workflow_handle(work_id).query(WorkItemWorkflow.status)


def _links(work_id: str, out: dict) -> dict:
    out["work_page"] = f"{PUBLIC_URL}/work/{work_id}"
    return out


def _err(e: Exception) -> str:
    return str(getattr(e, "cause", None) or e)


async def start_work_item(title: str, description: str, kind: str = "feature", source: str = "chat",
                          source_ref: str = "", requested_by: str = "", work_id: str | None = None) -> str:
    """The one intake path: chat, Gitea issues (and later Jira) all end up here."""
    work_id = work_id or f"{_slug(title)}-{uuid.uuid4().hex[:6]}"
    req = WorkRequest(work_id=work_id, title=title, description=description, kind=kind, source=source,
                      source_ref=source_ref, requested_by=requested_by, runtime=RUNTIME)
    await (await client()).start_workflow(WorkItemWorkflow.run, req, id=work_id, task_queue=WORK_QUEUE,
                                          id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)
    log.info("work item %s started from %s", work_id, source)
    return work_id


_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)  # called from kind via host.docker.internal

# ---- MCP: dev-agent ------------------------------------------------------------

agent_mcp = FastMCP("pipeline", stateless_http=True, json_response=True, streamable_http_path="/mcp",
                    transport_security=_security, instructions=(
                        "Ship code for a work item. submit_revision verifies your diff in a fresh, network-less "
                        "sandbox (your tests plus the original tests restored) and answers with the result. "
                        "When a revision passes, the pipeline opens the PR and deploys its preview itself."))


@agent_mcp.tool()
async def submit_revision(work_id: str, diff: str, summary: str) -> dict:
    """Submit the full unified diff against origin/main (`git add -A && git diff --cached origin/main`) for the
    work item you were given. Blocks until verification finishes and returns the test results."""
    if not diff.strip():
        return {"error": "diff is empty: run `git add -A && git diff --cached origin/main`"}
    rev = Revision(diff=diff, summary=summary, rev_id=uuid.uuid4().hex[:10])
    try:
        return await (await client()).get_workflow_handle(work_id).execute_update(WorkItemWorkflow.submit, rev)
    except (WorkflowUpdateFailedError, RPCError) as e:
        return {"error": f"could not submit to {work_id}: {_err(e)}"}


@agent_mcp.tool(name="get_work")
async def agent_get_work(work_id: str) -> dict:
    """Phase and latest verification result of a work item."""
    try:
        return summarize(await _status(work_id), verbose=True)
    except RPCError as e:
        return {"error": f"unknown work item {work_id}: {e}"}


# ---- MCP: delivery-agent (chat) --------------------------------------------------

chat_mcp = FastMCP("delivery", stateless_http=True, json_response=True, streamable_http_path="/mcp-chat",
                   transport_security=_security, instructions=(
                       "Request work on the demo-todo app and follow it to production. Temporal runs each work "
                       "item: the dev agent codes it, it is verified, a PR opens with a preview environment, and "
                       "after merge Kargo rolls the release out dev -> staging -> prod."))


@chat_mcp.tool()
async def start_work(title: str, description: str, kind: str = "feature") -> dict:
    """Start a work item. kind is "feature" or "bug". Returns at once with the work_id; use wait_for_work to follow it."""
    kind = kind.lower().strip()
    if kind not in ("feature", "bug"):
        return {"error": "kind must be 'feature' or 'bug'"}
    work_id = await start_work_item(title, description, kind, source="chat", requested_by="chat")
    return _links(work_id, {"work_id": work_id, "phase": "queued",
                            "next": "call wait_for_work to block until it needs a human (review, input, promotion)"})


@chat_mcp.tool()
async def get_work(work_id: str) -> dict:
    """Current phase, PR, preview URL, release version and stage status of a work item."""
    try:
        return _links(work_id, summarize(await _status(work_id)))
    except RPCError as e:
        return {"error": f"unknown work item {work_id}: {e}"}


@chat_mcp.tool()
async def wait_for_work(work_id: str, timeout_s: int = 420) -> dict:
    """Block until the work item needs a human (in review, needs input, awaiting promotion, done, closed)
    or its stage changes, at most timeout_s (<= 480) seconds. Call again if it returns still working."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + min(float(timeout_s), WAIT_MAX)
    h = (await client()).get_workflow_handle(work_id)
    try:
        first = await h.query(WorkItemWorkflow.status)
    except RPCError as e:
        return {"error": f"unknown work item {work_id}: {e}"}
    start_key = (first.get("phase"), first.get("stage"), len(first.get("revisions", [])))
    st = first
    while loop.time() < deadline:
        await asyncio.sleep(2)
        try:
            st = await h.query(WorkItemWorkflow.status)
        except RPCError:
            continue
        key = (st.get("phase"), st.get("stage"), len(st.get("revisions", [])))
        if st.get("phase") in HUMAN_PHASES and key != start_key:
            break
        if st.get("phase") in HUMAN_PHASES and first.get("phase") not in HUMAN_PHASES:
            break
    out = summarize(st)
    if st.get("phase") not in HUMAN_PHASES:
        out["note"] = "still working; call wait_for_work again"
    return _links(work_id, out)


@chat_mcp.tool()
async def list_work(limit: int = 10) -> dict:
    """Recent work items with their phase."""
    items = []
    async for wf in (await client()).list_workflows("WorkflowType='WorkItemWorkflow'", limit=limit):
        entry = {"work_id": wf.id, "workflow": wf.status.name.lower()}
        with contextlib.suppress(RPCError):
            st = await _status(wf.id)
            entry.update(title=st.get("title"), phase=st.get("phase"), stage=st.get("stage"), pr=st.get("pr", {}).get("url"))
        items.append({k: v for k, v in entry.items() if v})
    return {"work": items}


@chat_mcp.tool()
async def request_changes(work_id: str, feedback: str) -> dict:
    """Send review feedback (or an answer to the agent's question). The agent revises; the PR and its preview update."""
    try:
        await (await client()).get_workflow_handle(work_id).signal(WorkItemWorkflow.add_feedback, Feedback(feedback, "chat", "chat"))
    except RPCError as e:
        return {"error": _err(e)}
    return _links(work_id, {"work_id": work_id, "status": "feedback queued for the agent",
                            "next": "call wait_for_work to see the new revision"})


@chat_mcp.tool()
async def abandon_work(work_id: str, reason: str = "") -> dict:
    """Stop a work item: the PR is closed and its preview environment deleted."""
    await (await client()).get_workflow_handle(work_id).signal(WorkItemWorkflow.abandon, reason)
    return {"work_id": work_id, "status": "abandoning"}


# ---- MCP: gated (human approval in kagent) -------------------------------------

gated_mcp = FastMCP("delivery-release", stateless_http=True, json_response=True, streamable_http_path="/mcp-gated",
                    transport_security=_security,
                    instructions="Merge a reviewed PR, or promote a release to the next stage. Each call needs human approval.")


@gated_mcp.tool()
async def merge_pull_request(work_id: str) -> dict:
    """Merge the work item's PR into main. Merging builds a release and Kargo rolls it out to dev.
    Only after the human has said the preview looks good. A human must approve this call."""
    h = (await client()).get_workflow_handle(work_id)
    try:
        out = await h.execute_update(WorkItemWorkflow.merge, "chat")
    except (WorkflowUpdateFailedError, RPCError) as e:
        return {"error": _err(e)}
    return _links(work_id, {"work_id": work_id, **out, "next": "the release builds now; wait_for_work follows it to dev"})


@gated_mcp.tool()
async def promote(work_id: str, stage: str) -> dict:
    """Promote the work item's release to `stage` (staging or prod) through Kargo. A human must approve this call."""
    if stage not in STAGES:
        return {"error": f"stage must be one of {', '.join(STAGES)}"}
    h = (await client()).get_workflow_handle(work_id)
    try:
        out = await h.execute_update(WorkItemWorkflow.promote, stage)
    except (WorkflowUpdateFailedError, RPCError) as e:
        return {"error": _err(e)}
    return _links(work_id, {"work_id": work_id, "stage": stage, **out, "url": delivery.stage_url(stage)})


# ---- Gitea webhook ---------------------------------------------------------

def _work_id_from_branch(ref: str) -> str | None:
    return ref.split("/", 1)[1] if ref.startswith("agent/") else None


def _agent_text(body: str) -> str | None:
    m = re.match(r"\s*/agent\b[:\s]*(.*)", body or "", re.S)
    return m.group(1).strip() if m else None


async def gitea_hook(req: Request) -> JSONResponse:
    raw = await req.body()
    if SECRET_FILE.exists():
        want = hmac.new(SECRET_FILE.read_text().strip().encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(want, req.headers.get("x-gitea-signature", "")):
            return JSONResponse({"error": "bad signature"}, status_code=401)
    event = req.headers.get("x-gitea-event", "")
    p = json.loads(raw or b"{}")
    c = await client()
    try:
        if event == "pull_request" and p.get("action") == "closed":
            pr = p["pull_request"]
            wid = _work_id_from_branch(pr["head"]["ref"])
            if wid:
                ev = PrEvent("merged" if pr.get("merged") else "closed", pr.get("merge_commit_sha") or "",
                             (p.get("sender") or {}).get("login", ""))
                await c.get_workflow_handle(wid).signal(WorkItemWorkflow.pr_event, ev)
                return JSONResponse({"signalled": wid, "action": ev.action})
        elif event == "issue_comment" and p.get("action") == "created":
            text = _agent_text(p["comment"]["body"])
            issue, who = p["issue"], (p.get("sender") or {}).get("login", "")
            if text is None:
                return JSONResponse({"ignored": "not an /agent comment"})
            if issue.get("pull_request"):
                wid = _work_id_from_branch((await gitea.pr_state(issue["number"]))["head_ref"])
                if wid:
                    await c.get_workflow_handle(wid).signal(WorkItemWorkflow.add_feedback, Feedback(text, who, "pr-comment"))
                    return JSONResponse({"signalled": wid, "feedback": True})
            else:
                return JSONResponse(await _issue_intake(issue, who, extra=text))
        elif event == "issues" and p.get("action") == "opened":
            issue = p["issue"]
            if _agent_text(issue.get("body", "")) is not None or "/agent" in issue.get("title", ""):
                return JSONResponse(await _issue_intake(issue, (p.get("sender") or {}).get("login", "")))
    except RPCError as e:
        log.warning("webhook %s: %s", event, e)
        return JSONResponse({"error": str(e)}, status_code=200)  # don't make Gitea retry forever
    return JSONResponse({"ignored": event})


async def _issue_intake(issue: dict, who: str, extra: str = "") -> dict:
    """An issue becomes a work item (id issue-<n>, so a second /agent on it is feedback, not a duplicate)."""
    n = issue["number"]
    wid = f"issue-{n}-{_slug(issue['title'].replace('/agent', ''))}"
    labels = {lbl.get("name", "").lower() for lbl in issue.get("labels") or []}
    kind = "bug" if "bug" in labels or re.search(r"\b(bug|broken|fix)\b", issue["title"], re.I) else "feature"
    desc = (issue.get("body") or "").replace("/agent", "").strip() + (f"\n\n{extra}" if extra else "")
    async for wf in (await client()).list_workflows(f"WorkflowType='WorkItemWorkflow' AND WorkflowId STARTS_WITH 'issue-{n}-'"):
        if wf.status == WorkflowExecutionStatus.RUNNING:
            if extra:
                await (await client()).get_workflow_handle(wf.id).signal(WorkItemWorkflow.add_feedback, Feedback(extra, who, "issue-comment"))
            return {"existing": wf.id}
    try:
        await start_work_item(issue["title"].replace("/agent", "").strip(), desc or issue["title"], kind,
                              source="gitea-issue", source_ref=f"#{n}", requested_by=who, work_id=wid)
    except WorkflowAlreadyStartedError:
        return {"existing": wid}
    await gitea.comment(n, f"Started work item `{wid}`: {PUBLIC_URL}/work/{wid}")
    return {"started": wid}


# ---- pages -----------------------------------------------------------------

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--muted:#687087;--line:#e1e4eb;--ok:#1d8a4a;--bad:#c0392b;--accent:#3b5bdb}
@media (prefers-color-scheme:dark){:root{--bg:#11141a;--card:#1a1f28;--fg:#e5e8ef;--muted:#99a1b3;--line:#2b313d;--ok:#5bd08a;--bad:#ff7a6b;--accent:#8aa0ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{max-width:980px;margin:32px auto;padding:0 16px}a{color:var(--accent)}h1{font-size:22px;margin:0 0 4px}
.muted{color:var(--muted)}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:16px 0}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:8px;border-bottom:1px solid var(--line);vertical-align:top}
.ok{color:var(--ok)}.bad{color:var(--bad)}pre{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:12px;overflow:auto;font-size:12.5px;max-height:480px;white-space:pre-wrap}
.pill{display:inline-block;padding:2px 10px;border-radius:99px;border:1px solid var(--line);font-size:13px}
.steps{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}.steps span{padding:2px 10px;border-radius:99px;border:1px solid var(--line);font-size:13px;color:var(--muted)}
.steps span.on{border-color:var(--accent);color:var(--accent);font-weight:600}.steps span.did{color:var(--ok);border-color:var(--ok)}
details summary{cursor:pointer}
"""
STEPS = ["agent working", "opening pr", "deploying preview", "in review", "merging", "building release",
         "dev", "staging", "prod", "done"]


def _page(title: str, body: str, refresh: bool = False) -> HTMLResponse:
    meta = '<meta http-equiv="refresh" content="5">' if refresh else ""
    return HTMLResponse(f"<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                        f"{meta}<title>{html.escape(title)}</title><style>{CSS}</style></head><body><main>{body}</main></body></html>")


async def index(_: Request) -> HTMLResponse:
    rows = []
    async for wf in (await client()).list_workflows("WorkflowType='WorkItemWorkflow'", limit=50):
        phase = ""
        with contextlib.suppress(RPCError):
            st = await _status(wf.id)
            phase = st.get("phase", "") + (f": {st['stage']}" if st.get("stage") else "")
        rows.append(f"<tr><td><a href='/work/{wf.id}'>{html.escape(wf.id)}</a></td><td>{html.escape(phase)}</td>"
                    f"<td>{wf.status.name.lower()}</td><td class=muted>{wf.start_time:%Y-%m-%d %H:%M}</td></tr>")
    table = "<table><tr><th>Work item</th><th>Phase</th><th>Workflow</th><th>Started</th></tr>" + "".join(rows) + "</table>" if rows \
        else "<p class=muted>No work items yet. Ask delivery-agent in the kagent UI, or open a Gitea issue with <code>/agent</code> in it.</p>"
    return _page("Work items", "<h1>Work items</h1><p class=muted>Each one is a Temporal WorkItemWorkflow, from request to prod. "
                 "<a href='http://localhost:8233'>Temporal UI</a></p>" + f"<div class=card>{table}</div>")


def _tests(v: dict) -> str:
    def one(label: str, t: dict) -> str:
        if not t:
            return ""
        cls = "ok" if t.get("ok") else "bad"
        return f"<span class='pill {cls}'>{label}: {t.get('pass', 0)} passed, {t.get('fail', 0)} failed</span> "
    return one("agent's tests", v.get("agent_tests", {})) + one("base tests restored", v.get("base_tests", {}))


def _steps(st: dict) -> str:
    cur = st.get("stage") or st.get("phase", "")
    if st.get("phase") in ("needs input",):
        cur = "agent working"
    idx = STEPS.index(cur) if cur in STEPS else -1
    if st.get("phase") == "done":
        idx = len(STEPS) - 1
    out = []
    for i, s in enumerate(STEPS):
        cls = "on" if i == idx else ("did" if 0 <= i < idx else "")
        out.append(f"<span class='{cls}'>{html.escape(s)}</span>")
    return "<div class=steps>" + "".join(out) + "</div>"


async def work_page(req: Request) -> HTMLResponse:
    wid = req.path_params["wid"]
    h = (await client()).get_workflow_handle(wid)
    try:
        st = await h.query(WorkItemWorkflow.status)
    except RPCError:
        return _page("Not found", f"<h1>Unknown work item</h1><p>{html.escape(wid)}</p>")
    running = (await h.describe()).status == WorkflowExecutionStatus.RUNNING
    e = html.escape
    links = []
    pv = st.get("preview") or {}
    if pv.get("url") and not pv.get("torn_down"):
        links.append(f"<a href='{e(pv['url'])}' target=_blank>Preview (rev {pv.get('revision')}, kargo-nonprod) →</a>")
    if pv.get("error"):
        links.append(f"<span class=bad>preview failed: {e(pv['error'])}</span>")
    if st.get("pr"):
        links.append(f"<a href='{e(st['pr']['url'])}' target=_blank>Pull request #{st['pr']['number']} →</a>")
    links.append(f"<a href='http://localhost:8233/namespaces/default/workflows/{e(wid)}' target=_blank>Temporal →</a>")
    rel = st.get("release") or {}
    stages = ""
    if rel:
        cells = []
        for s in STAGES:
            info = st.get("stages", {}).get(s, {})
            state = "verified" if info.get("verified") else (info.get("message") or ("in progress" if st.get("stage") == s else "pending"))
            cells.append(f"<td><b>{s}</b><br><a href='{delivery.stage_url(s)}' target=_blank>{delivery.stage_url(s)}</a><br>"
                         f"<span class='{'ok' if info.get('verified') else 'muted'}'>{e(state)}</span></td>")
        stages = (f"<div class=card><h3 style='margin-top:0'>Release {e(rel.get('version', ''))}</h3>"
                  f"<p class=muted>{e(rel.get('image', ''))} · merge {e(st.get('merge_sha', '')[:8])} · "
                  f"<a href='https://localhost:31444/project/demo-todo' target=_blank>Kargo →</a></p><table><tr>{''.join(cells)}</tr></table></div>")
    revs = []
    for r in reversed(st.get("revisions", [])):
        v = r.get("verify", {})
        status = "verifying…" if "passed" not in r else ("<span class=ok>passed</span>" if r["passed"] else "<span class=bad>failed</span>")
        err = f"<p class=bad>{e(v['error'])}</p>" if v.get("error") else ""
        revs.append(f"<div class=card><h3 style='margin:0'>Revision {r['n']} — {status}</h3>"
                    f"<p>{e(r.get('summary') or '')}</p><p>{_tests(v)}</p>{err}"
                    f"<details><summary>Diff</summary><pre>{e(r.get('diff', ''))}</pre></details>"
                    f"<details><summary>Test output</summary><pre>{e(v.get('output', ''))}</pre></details></div>")
    turns = "".join(f"<details><summary>Turn {t['n']} · {e(t.get('state', 'running'))}</summary>"
                    f"<p class=muted>Prompt</p><pre>{e(t.get('prompt', ''))}</pre><p class=muted>Reply</p><pre>{e(t.get('reply', ''))}</pre></details>"
                    for t in reversed(st.get("turns", [])))
    log_rows = "".join(f"<tr><td class=muted>{e(x['at'][11:19])}</td><td>{e(x['msg'])}</td></tr>" for x in reversed(st.get("log", [])))
    body = (f"<p class=muted><a href='/'>← all work items</a></p><h1>{e(st.get('title', wid))}</h1>"
            f"<p class=muted>{e(st.get('kind', ''))} · from {e(st.get('source', ''))} {e(st.get('source_ref', ''))} · {e(wid)}"
            f"{' · kagent session ' + e(st['session_id']) if st.get('session_id') else ''}</p>"
            f"<div class=card>{_steps(st)}<p><b>Request:</b> {e(st.get('description', ''))}</p>"
            f"<p><b>Phase:</b> <span class=pill>{e(st.get('phase', ''))}{': ' + e(st['stage']) if st.get('stage') else ''}</span> "
            f"{e(st.get('result', '') or '')}</p><p>{' &nbsp; '.join(links)}</p></div>{stages}"
            + "".join(revs)
            + (f"<div class=card><h3 style='margin-top:0'>Agent turns (A2A)</h3>{turns}</div>" if turns else "")
            + f"<div class=card><h3 style='margin-top:0'>Timeline</h3><table>{log_rows}</table></div>")
    return _page(st.get("title", wid), body, refresh=running)


async def work_json(req: Request) -> JSONResponse:
    return JSONResponse(await _status(req.path_params["wid"]))


async def healthz(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


def app() -> Starlette:
    servers = (agent_mcp, chat_mcp, gated_mcp)
    apps = [s.streamable_http_app() for s in servers]

    @contextlib.asynccontextmanager
    async def lifespan(_):
        async with contextlib.AsyncExitStack() as stack:
            for s in servers:
                await stack.enter_async_context(s.session_manager.run())
            yield

    routes = [Route("/", index), Route("/healthz", healthz), Route("/work/{wid}", work_page),
              Route("/api/work/{wid}", work_json), Route("/hooks/gitea", gitea_hook, methods=["POST"]),
              *[r for a in apps for r in a.routes]]
    return Starlette(routes=routes, lifespan=lifespan)


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log.info("pipeline server on :%s (verify runtime: %s)", PORT, RUNTIME)
    uvicorn.run(app(), host="0.0.0.0", port=PORT, log_level="warning")
