#!/usr/bin/env python3
"""Live dashboard of a change moving through the stack (make watch). Read-only; Ctrl-C quits.

 ┌ header ───────────────────────────────────────────────────────────┐
 │ Substrate actors              │ Temporal WorkItemWorkflows        │
 │ OpenShell sandboxes           │ Gitea (PRs, branches, main)       │
 │ Timeline: everything above, as events, newest at the bottom      │
 └──────────────────────────────────────────────────────────────────┘

Sources:
  actors     atelet's log (Run/Restore/Checkpoint/Terminate RPCs). Actors are gVisor sandboxes
             inside the kagent-default pod, so they never appear as pods; the log is the only view.
             Names map to chat sessions: session-<kagent session id>.
  sessions   `kagent agent session list`
  sandboxes  pods in the openshell namespace (default--p-<sha8 of change id>-<revision>)
  temporal   `temporal workflow list` + the pipeline server's /api/work/<id> for the phase
  gitea      REST API with .cache/gitea-token

WATCH_INTERVAL (seconds, default 2) sets how often sources are polled.
"""
from __future__ import annotations

import collections
import hashlib
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from rich import box
from rich.console import Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

ROOT = Path(__file__).resolve().parents[1]
ENV = dict(l.split("=", 1) for l in (ROOT / "scripts/versions.env").read_text().split()
           if "=" in l and not l.startswith("#"))
CTX = f"kind-{ENV.get('CLUSTER', 'poc')}"
GITEA = f"http://localhost:{ENV.get('GITEA_PORT', '3300')}"
REPO = f"{ENV.get('GITEA_USER', 'poc')}/{ENV.get('GITEA_REPO', 'demo-todo')}"
PIPELINE = f"http://localhost:{ENV.get('PIPELINE_PORT', '8765')}"
INTERVAL = float(os.environ.get("WATCH_INTERVAL", "2"))
TOKEN_FILE = ROOT / ".cache/gitea-token"
KAGENT = str(ROOT / ".cache/bin/kagent")

lock = threading.Lock()
actors: dict[str, dict] = {}                       # actor name -> state, since, last
timeline: collections.deque = collections.deque(maxlen=200)
snap: dict = {"sessions": {}, "sandboxes": [], "changes": [], "gitea": None, "errors": {}, "at": None}

ACTOR_VERBS = {"Run": ("created", "green"), "Restore": ("woke", "green"),
               "Checkpoint": ("suspended", "yellow"), "Terminate": ("terminated", "red")}
SOURCE_STYLE = {"actor": "magenta", "temporal": "cyan", "openshell": "blue", "gitea": "green"}


# ---------------------------------------------------------------- helpers
def now() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(s: str) -> datetime:
    """RFC 3339 with any fraction length (atelet logs nanoseconds; fromisoformat takes 6 digits)."""
    if not s:
        return now()
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)?$", s.strip())
    if not m:
        return now()
    frac = (m.group(2) or "0")[:6].ljust(6, "0")
    tz = m.group(3) or "Z"
    return datetime.fromisoformat(f"{m.group(1)}.{frac}{'+00:00' if tz == 'Z' else tz}")


def ago(ts: datetime | None) -> str:
    if not ts:
        return "-"
    s = max(0, int((now() - ts).total_seconds()))
    return f"{s}s" if s < 120 else f"{s // 60}m" if s < 7200 else f"{s // 3600}h"


def sh(*cmd: str, timeout: float = 8) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def get_json(url: str, token: bool = False):
    req = urllib.request.Request(url)
    if token and TOKEN_FILE.exists():
        req.add_header("Authorization", f"token {TOKEN_FILE.read_text().strip()}")
    with urllib.request.urlopen(req, timeout=4) as r:
        return json.load(r)


def event(source: str, text: str, style: str = "", ts: datetime | None = None) -> None:
    with lock:
        timeline.append((ts or now(), source, text, style))


def sandbox_name(change_id: str, n: int) -> str:      # mirrors worker/apps.py env_name
    return f"p-{hashlib.sha1(change_id.encode()).hexdigest()[:8]}-{n}"


def preview_port(name: str) -> int:                    # mirrors worker/apps.py stable_port
    return 20000 + int(hashlib.sha1(name.encode()).hexdigest(), 16) % 10000


# ---------------------------------------------------------------- collectors
def follow_atelet() -> None:
    """Stream atelet's log; reconnect if it drops. Backfills 6h so history shows at startup."""
    while True:
        p = subprocess.Popen(["kubectl", "--context", CTX, "-n", "ate-system", "logs", "-f", "--since=6h",
                              "-c", "atelet", "ds/atelet"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        for line in p.stdout:  # type: ignore[union-attr]
            if '"Handle RPC"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            verb = d.get("method", "").rsplit("/", 1)[-1]
            if verb not in ACTOR_VERBS:
                continue
            name = d.get("req", {}).get("actor_name", "?")
            ts = parse_ts(d.get("time", ""))
            with lock:
                a = actors.setdefault(name, {"since": ts})
                a.update(state=verb, last=ts)
                if verb in ("Run", "Restore"):
                    a["since"] = ts
            word, style = ACTOR_VERBS[verb]
            event("actor", f"{word:<10} {name}", style, ts)
        p.wait()
        with lock:
            snap["errors"]["actors"] = "atelet log stream dropped; reconnecting"
        time.sleep(3)


def collect_sessions() -> dict:
    out = {}
    for line in sh(KAGENT, "agent", "session", "list").splitlines():
        cols = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cols) == 4 and cols[0] != "ID" and cols[0].count("-") == 4:
            out[f"session-{cols[0]}"] = {"agent": cols[1], "created": parse_ts(cols[3])}
    return out


def pod_status(p: dict) -> str:
    st = p.get("status", {})
    for c in st.get("conditions", []):
        if c.get("type") == "PodScheduled" and c.get("reason") == "SchedulingGated":
            return "gated"
    inits = st.get("initContainerStatuses", [])
    done = sum(1 for c in inits if c.get("state", {}).get("terminated", {}).get("exitCode") == 0)
    if inits and done < len(inits):
        return f"init {done}/{len(inits)}"
    if p["metadata"].get("deletionTimestamp"):
        return "terminating"
    ready = all(c.get("ready") for c in st.get("containerStatuses", [])) and st.get("containerStatuses")
    return "ready" if ready else st.get("phase", "?").lower()


def collect_sandboxes() -> list[dict]:
    raw = sh("kubectl", "--context", CTX, "-n", "openshell", "get", "pods", "-o", "json")
    pods = json.loads(raw)["items"] if raw else []
    out = []
    for p in pods:
        name = p["metadata"]["name"]
        if not name.startswith("default--"):
            continue
        short = name.removeprefix("default--")
        out.append({"name": short, "status": pod_status(p),
                    "created": parse_ts(p["metadata"]["creationTimestamp"]), "port": preview_port(short)})
    return sorted(out, key=lambda s: s["created"])


def collect_changes() -> list[dict]:
    raw = sh("temporal", "workflow", "list", "--query", "WorkflowType='WorkItemWorkflow'", "--limit", "8", "-o", "json")
    if not raw.strip():  # CLI timed out or Temporal is down: keep the last good view (poll_forever)
        raise RuntimeError("temporal CLI returned nothing (is `make dev` running?)")
    out = []
    for w in json.loads(raw or "[]"):
        wid = w["execution"]["workflowId"]
        try:
            d = get_json(f"{PIPELINE}/api/work/{wid}")
        except Exception:
            d = {}
        revs = d.get("revisions", [])
        out.append({"id": wid, "status": w["status"].replace("WORKFLOW_EXECUTION_STATUS_", "").lower(),
                    "phase": d.get("phase", "") + (f": {d['stage']}" if d.get("stage") else ""),
                    "revs": len(revs), "kind": d.get("kind", ""),
                    "preview": (d.get("preview") or {}).get("url", "") if not (d.get("preview") or {}).get("torn_down") else "",
                    "pr": (d.get("pr") or {}).get("url", ""), "release": (d.get("release") or {}).get("version", ""),
                    "result": d.get("result", ""),
                    "passed": [r.get("passed") for r in revs if "passed" in r],
                    "started": parse_ts(w.get("startTime", ""))})
    return out


def collect_gitea() -> dict:
    branches = get_json(f"{GITEA}/api/v1/repos/{REPO}/branches", token=True) or []
    pulls = get_json(f"{GITEA}/api/v1/repos/{REPO}/pulls?state=all&limit=6", token=True) or []
    commits = get_json(f"{GITEA}/api/v1/repos/{REPO}/commits?sha=main&limit=3&stat=false", token=True) or []
    return {"branches": branches, "pulls": pulls, "commits": commits}


def diff_events(old: dict, new: dict) -> None:
    """Turn snapshot changes into timeline events (skipped on the first poll)."""
    oc = {c["id"]: c for c in old["changes"]}
    for c in new["changes"]:
        prev = oc.get(c["id"])
        if not prev:
            event("temporal", f"work item started  {c['id']}", "cyan")
        else:
            if c["revs"] > prev["revs"]:
                event("temporal", f"revision {c['revs']} submitted  {c['id']}", "cyan")
            if c["phase"] != prev["phase"] and c["phase"]:
                style = {"in review": "bold green", "needs input": "yellow", "done": "dim", "closed": "dim"}.get(c["phase"], "cyan")
                event("temporal", f"phase → {c['phase']}  {c['id']}", style)
    osb = {s["name"]: s for s in old["sandboxes"]}
    nsb = {s["name"]: s for s in new["sandboxes"]}
    for name, s in nsb.items():
        if name not in osb:
            event("openshell", f"sandbox created  {name}", "blue")
        elif s["status"] != osb[name]["status"]:
            event("openshell", f"sandbox {s['status']}  {name}", "green" if s["status"] == "ready" else "blue")
    for name in osb.keys() - nsb.keys():
        event("openshell", f"sandbox removed  {name}", "dim")
    og, ng = old["gitea"], new["gitea"]
    if og and ng:
        opr = {p["number"]: p for p in og["pulls"]}
        for p in ng["pulls"]:
            prev = opr.get(p["number"])
            state = "merged" if p.get("merged") else p["state"]
            pstate = ("merged" if prev.get("merged") else prev["state"]) if prev else None
            if state != pstate:
                event("gitea", f"PR #{p['number']} {'opened' if not prev else state}  {p['title']}", "bold green")
        ob = {b["name"] for b in og["branches"]}
        for b in ng["branches"]:
            if b["name"] not in ob:
                event("gitea", f"branch pushed  {b['name']}", "green")
        if og["commits"] and ng["commits"] and og["commits"][0]["sha"] != ng["commits"][0]["sha"]:
            event("gitea", f"main updated  {ng['commits'][0]['commit']['message'].splitlines()[0]}", "bold green")


def poll_forever() -> None:
    first = True
    while True:
        new = {"errors": {}}
        for key, fn in (("sessions", collect_sessions), ("sandboxes", collect_sandboxes),
                        ("changes", collect_changes), ("gitea", collect_gitea)):
            try:
                new[key] = fn()
            except Exception as e:  # keep the dashboard alive when one source is down
                new[key] = snap.get(key) if key != "gitea" else None
                new["errors"][key] = f"{type(e).__name__}: {e}"[:80]
        new["at"] = now()
        if not first:
            diff_events(snap, new)
        with lock:
            snap.update(new)
        first = False
        time.sleep(INTERVAL)


# ---------------------------------------------------------------- rendering
def table(*cols: tuple[str, dict]) -> Table:
    t = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False, show_edge=False, header_style="bold dim")
    for name, kw in cols:
        t.add_column(name, **kw)
    return t


def empty(msg: str) -> Text:
    return Text(msg, style="dim italic")


def error_line(key: str) -> Text | None:
    err = snap["errors"].get(key)
    return Text(f"⚠ {err}", style="red") if err else None


def actors_panel() -> Panel:
    with lock:
        rows = sorted(actors.items(), key=lambda kv: kv[1]["last"], reverse=True)
        sessions = snap["sessions"]
    awake = sum(1 for _, a in rows if a["state"] in ("Run", "Restore"))
    suspended = sum(1 for _, a in rows if a["state"] == "Checkpoint")
    t = table(("state", {"width": 12, "no_wrap": True}), ("for", {"width": 5, "justify": "right"}),
              ("actor", {"ratio": 1, "no_wrap": True, "overflow": "ellipsis"}), ("what", {"width": 16, "no_wrap": True}))
    for name, a in rows[:10]:
        if a["state"] in ("Run", "Restore"):
            state, since = Text("● awake", "bold green"), ago(a["since"])
        elif a["state"] == "Checkpoint":
            state, since = Text("◌ suspended", "yellow"), ago(a["last"])
        else:
            state, since = Text("✕ gone", "red dim"), ago(a["last"])
        what = sessions.get(name, {}).get("agent", "") or ("golden build" if not name.startswith("session-") else "chat (deleted?)")
        t.add_row(state, since, name.removeprefix("session-"), what)
    body = [t] if rows else [empty("waiting for atelet events…")]
    body += [x for x in [error_line("actors")] if x]
    sub = f"[green]{awake} awake[/] · [yellow]{suspended} suspended[/] · {len(sessions)} chat sessions"
    return Panel(Group(*body), title="[bold magenta]Substrate actors[/] [dim]one per chat · gVisor in kagent-default[/]",
                 subtitle=sub, border_style="magenta", title_align="left", subtitle_align="right")


def temporal_panel() -> Panel:
    changes = snap["changes"] or []
    t = table(("status", {"width": 9, "no_wrap": True}), ("phase", {"width": 20, "no_wrap": True, "overflow": "ellipsis"}),
              ("change", {"ratio": 3, "no_wrap": True, "overflow": "ellipsis"}), ("rev", {"width": 3, "justify": "right"}),
              ("tests", {"width": 6, "no_wrap": True}), ("detail", {"ratio": 2, "min_width": 22, "no_wrap": True, "overflow": "ellipsis"}))
    for c in changes:
        status_style = {"running": "bold cyan", "completed": "dim", "failed": "red"}.get(c["status"], "")
        phase_style = {"in review": "bold green", "needs input": "yellow", "agent working": "yellow",
                       "done": "dim", "closed": "dim"}.get(c["phase"].split(":")[0], "cyan")
        tests = Text("".join("✓" if p else "✗" for p in c["passed"][-5:]))
        tests.stylize("green")
        for i, p in enumerate(c["passed"][-5:]):
            if not p:
                tests.stylize("red", i, i + 1)
        detail = c["result"] or (f"release {c['release']}" if c["release"] else "") or c["preview"] or c["pr"]
        t.add_row(Text(c["status"], status_style), Text(c["phase"], phase_style), c["id"], str(c["revs"]), tests, detail)
    body = [t] if changes else [empty("no work items yet — ask delivery-agent for something")]
    body += [x for x in [error_line("changes")] if x]
    running = sum(1 for c in changes if c["status"] == "running")
    return Panel(Group(*body), title="[bold cyan]Temporal[/] [dim]WorkItemWorkflow · http://localhost:8233[/]",
                 subtitle=f"{running} running", border_style="cyan", title_align="left", subtitle_align="right")


def openshell_panel() -> Panel:
    sandboxes = snap["sandboxes"] or []
    by_name = {}
    for c in snap["changes"] or []:
        for n in range(1, c["revs"] + 2):
            by_name[sandbox_name(c["id"], n)] = (c["id"], n)
    t = table(("sandbox", {"width": 13, "no_wrap": True}), ("pod", {"width": 11, "no_wrap": True}),
              ("age", {"width": 5, "justify": "right"}), ("change · rev", {"ratio": 2, "no_wrap": True, "overflow": "ellipsis"}),
              ("preview", {"min_width": 22, "no_wrap": True}))
    for s in sandboxes:
        style = {"ready": "bold green", "gated": "yellow", "terminating": "dim"}.get(s["status"], "yellow")
        cid, n = by_name.get(s["name"], ("?", "?"))
        preview = f"http://localhost:{s['port']}" if s["status"] == "ready" else ""
        t.add_row(s["name"], Text(s["status"], style), ago(s["created"]), f"{cid} · {n}", preview)
    body = [t] if sandboxes else [empty("none — one appears per submitted revision (verify, then preview)")]
    body += [x for x in [error_line("sandboxes")] if x]
    return Panel(Group(*body), title="[bold blue]OpenShell sandboxes[/] [dim]verify + preview · no network[/]",
                 subtitle="pods in ns openshell (make k9s)", border_style="blue", title_align="left", subtitle_align="right")


def gitea_panel() -> Panel:
    g = snap["gitea"]
    if not g:
        return Panel(error_line("gitea") or empty("unreachable (is poc-gitea running?)"), title="[bold green]Gitea[/]",
                     border_style="green", title_align="left")
    prs = table(("PR", {"width": 4}), ("state", {"width": 7}), ("title", {"ratio": 2, "no_wrap": True, "overflow": "ellipsis"}),
                ("branch", {"ratio": 2, "no_wrap": True, "overflow": "ellipsis"}))
    for p in g["pulls"][:4]:
        state = "merged" if p.get("merged") else p["state"]
        prs.add_row(f"#{p['number']}", Text(state, {"open": "bold green", "merged": "magenta"}.get(state, "dim")),
                    p["title"], p["head"]["ref"])
    commits = table(("main", {"width": 8, "style": "yellow"}), ("commit", {"ratio": 3, "no_wrap": True, "overflow": "ellipsis"}),
                    ("when", {"width": 5, "justify": "right"}))
    for c in g["commits"][:3]:
        commits.add_row(c["sha"][:8], c["commit"]["message"].splitlines()[0], ago(parse_ts(c["created"])))
    agent_branches = [b["name"] for b in g["branches"] if b["name"].startswith("agent/")]
    body = [prs if g["pulls"] else empty("no PRs yet"), commits]
    sub = f"{len(agent_branches)} agent/* branches · login poc / poc-password-local"
    return Panel(Group(*body), title=f"[bold green]Gitea[/] [dim]{GITEA}/{REPO}[/]", subtitle=sub,
                 border_style="green", title_align="left", subtitle_align="right")


def timeline_panel(height: int) -> Panel:
    with lock:
        evs = sorted(timeline, key=lambda e: e[0])[-max(1, height - 2):]
    t = Table.grid(padding=(0, 1), expand=True)
    t.add_column(width=8, style="dim")
    t.add_column(width=9)
    t.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    for ts, source, text, style in evs:
        t.add_row(ts.astimezone().strftime("%H:%M:%S"), Text(source, SOURCE_STYLE.get(source, "")), Text(text, style))
    return Panel(t if evs else empty("events from every panel land here"), title="[bold]Timeline[/] [dim]newest last[/]",
                 border_style="white", title_align="left")


def header() -> Text:
    t = Text()
    t.append(" mock-poc watch ", "bold reverse")
    t.append(f"  {datetime.now():%H:%M:%S}", "bold")
    at = snap.get("at")
    t.append(f"  polled {ago(at)} ago" if at else "  first poll…", "dim")
    t.append("   kagent :8082 · changes :8765 · Temporal :8233 · Gitea :3300", "dim")
    t.append("   Ctrl-C quits", "dim")
    return t


def build(console_height: int) -> Layout:
    root = Layout()
    root.split_column(Layout(name="header", size=1), Layout(name="top", ratio=3),
                      Layout(name="mid", ratio=3), Layout(name="timeline", ratio=2))
    root["header"].update(header())
    root["top"].split_row(Layout(actors_panel(), name="actors"), Layout(temporal_panel(), name="temporal"))
    root["mid"].split_row(Layout(openshell_panel(), name="openshell"), Layout(gitea_panel(), name="gitea"))
    root["timeline"].update(timeline_panel(max(4, (console_height - 1) * 2 // 8)))
    return root


def main() -> None:
    os.chdir(ROOT)
    threading.Thread(target=follow_atelet, daemon=True).start()
    threading.Thread(target=poll_forever, daemon=True).start()
    with Live(screen=True, refresh_per_second=2, transient=True) as live:
        try:
            while True:
                live.update(build(live.console.size.height))
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
