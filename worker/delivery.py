"""Delivery side of a work item: images, PR environments, Kargo stages, and the dev agent over A2A.

Everything here talks to the kargo stack (../kargo, six kind clusters) and to the
kagent API, always through explicit kube contexts. The user's current context is never changed.

  images        docker build of a Gitea commit with images/demo-todo/Dockerfile (owned by the
                platform, not the app repo), pushed to kargo-registry (localhost:5002 on the host)
  PR env        Argo CD Application on kargo-hub -> namespace demo-todo-pr-<n> on kargo-nonprod,
                from the gitops repo's preview/ overlay with the PR image; reachable on the host
                through a socat "edge" container on the kind network
  Kargo         Warehouse picks up semver tags; dev auto-promotes; staging/prod via Promotion
  dev agent     `kagent agent session create/invoke` (A2A); one kagent session per work item

    python -m worker.delivery bootstrap     first release of Gitea main (scripts/delivery-up.sh)
    python -m worker.delivery edge PORT CLUSTER
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tarfile
import io
import tempfile
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from worker import gitea  # noqa: E402
from worker.runtimes.base import RuntimeError_, sh  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
_V = gitea._V


def cfg(key: str, default: str) -> str:
    return os.environ.get(key, _V.get(key, default))


HUB = cfg("KARGO_HUB_CONTEXT", "kind-kargo-hub")
NONPROD = cfg("KARGO_NONPROD_CONTEXT", "kind-kargo-nonprod")
PROJECT = cfg("KARGO_PROJECT", "demo-todo")
REGISTRY_PUSH = cfg("KARGO_REGISTRY_PUSH", "localhost:5002")  # host side
REGISTRY_PULL = cfg("KARGO_REGISTRY_PULL", "kargo-registry:5000")  # what clusters pull from
GITOPS_REPO = cfg("GITOPS_REPO", "demo-todo-gitops")
GITEA_IN_KIND = cfg("GITEA_IN_KIND", "http://poc-gitea:3000")  # poc-gitea on the kind network
PR_PORT_BASE = int(cfg("PR_PORT_BASE", "31100"))
STAGE_PORTS = {"dev": 31081, "staging": 31082, "prod": 31083}
STAGE_CLUSTERS = {"dev": "nonprod", "staging": "nonprod", "prod": "prod"}
KAGENT = cfg("KAGENT_BIN", str(ROOT / ".cache/bin/kagent"))
KAGENT_API = cfg("KAGENT_API_URL", f"http://localhost:{cfg('KAGENT_API_PORT', '8083')}")
DEV_AGENT = cfg("DEV_AGENT_NAME", "dev-agent")
DOCKERFILE = ROOT / "images/demo-todo/Dockerfile"
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


async def kubectl(ctx: str, *args: str, stdin: str | None = None, check: bool = True) -> str:
    rc, out, err = await sh("kubectl", "--context", ctx, *args, stdin=stdin, check=False, timeout=120)
    if rc and check:
        raise RuntimeError_(f"kubectl {' '.join(args[:3])}: {err.strip() or out.strip()}")
    return out


# ---- images ---------------------------------------------------------------

async def build_image(sha: str, repo: str, tag: str) -> str:
    """Build commit `sha` of the app repo into REGISTRY/repo:tag and push it. Returns the pull ref."""
    tar = await gitea.archive(sha)
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(fileobj=io.BytesIO(tar)) as tf:
            tf.extractall(tmp, filter="data")
        ref = f"{REGISTRY_PUSH}/{repo}:{tag}"
        await sh("docker", "build", "-q", "-f", str(DOCKERFILE), "--build-arg", f"VERSION={tag}",
                 "--build-arg", f"GIT_SHA={sha}", "-t", ref, tmp, timeout=600)
    await sh("docker", "push", "-q", ref, timeout=300)
    return f"{REGISTRY_PULL}/{repo}:{tag}"


async def registry_tags(repo: str) -> list[str]:
    async with httpx.AsyncClient(timeout=15, verify=False) as c:  # self-signed local registry
        r = await c.get(f"https://{REGISTRY_PUSH}/v2/{repo}/tags/list")
        if r.status_code == 404:
            return []
        r.raise_for_status()
        return r.json().get("tags") or []


def next_version(tags: list[str], kind: str) -> str:
    vers = sorted(tuple(map(int, m.groups())) for t in tags if (m := SEMVER.match(t)))
    if not vers:
        return "0.1.0"
    major, minor, patch = vers[-1]
    return f"{major}.{minor + 1}.0" if kind == "feature" else f"{major}.{minor}.{patch + 1}"


async def release(sha: str, kind: str) -> dict:
    """Semver image of a merge commit; then nudge the Kargo Warehouse so Freight appears now."""
    version = next_version(await registry_tags("demo-todo"), kind)
    image = await build_image(sha, "demo-todo", version)
    await kubectl(HUB, "-n", PROJECT, "annotate", "warehouse", "demo-todo",
                  f"kargo.akuity.io/refresh={int(asyncio.get_running_loop().time() * 1000)}", "--overwrite", check=False)
    return {"version": version, "image": image, "sha": sha}


# ---- edge: host access to NodePorts inside the kargo clusters ----------------

def edge_name(port: int) -> str:
    return f"demo-todo-edge-{port}"


async def edge_up(port: int, cluster: str) -> str:
    """localhost:PORT -> kargo-<cluster>-control-plane:PORT, via a socat container on the kind network."""
    name = edge_name(port)
    rc, out, _ = await sh("docker", "inspect", "-f", "{{.State.Running}}", name, check=False)
    if rc != 0 or out.strip() != "true":
        await sh("docker", "rm", "-f", name, check=False)
        await sh("docker", "run", "-d", "--name", name, "--restart", "unless-stopped", "--network", "kind",
                 "--label", "poc.edge=demo-todo", "-p", f"127.0.0.1:{port}:{port}", "alpine/socat:1.8.0.3",
                 f"tcp-listen:{port},fork,reuseaddr", f"tcp-connect:kargo-{cluster}-control-plane:{port}")
    return f"http://localhost:{port}"


async def edge_down(port: int) -> None:
    await sh("docker", "rm", "-f", edge_name(port), check=False)


# ---- PR environments -----------------------------------------------------

def pr_port(n: int) -> int:
    return PR_PORT_BASE + n % 100


def pr_app_manifest(work_id: str, n: int, tag: str) -> str:
    ns, port = f"demo-todo-pr-{n}", pr_port(n)
    app = {
        "apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
        "metadata": {"name": ns, "namespace": "argocd",
                     "labels": {"poc.local/work-id": work_id[:63], "poc.local/kind": "pr-env"},
                     "finalizers": ["resources-finalizer.argocd.argoproj.io"]},
        "spec": {
            "project": "default",
            "source": {
                "repoURL": f"{GITEA_IN_KIND}/{gitea.USER}/{GITOPS_REPO}.git", "targetRevision": "main",
                "path": "preview",
                "kustomize": {
                    "namespace": ns,
                    "images": [f"{REGISTRY_PULL}/demo-todo={REGISTRY_PULL}/demo-todo-pr:{tag}"],
                    "patches": [{"target": {"kind": "Service", "name": "demo-todo"},
                                 "patch": json.dumps([{"op": "add", "path": "/spec/ports/0/nodePort", "value": port}])},
                                {"target": {"kind": "Rollout", "name": "demo-todo"},
                                 "patch": json.dumps([{"op": "add", "path": "/spec/template/spec/containers/0/env/-",
                                                       "value": {"name": "STAGE", "value": f"pr-{n}"}}])}],
                },
            },
            "destination": {"name": "nonprod", "namespace": ns},
            "syncPolicy": {"automated": {"prune": True, "selfHeal": True}, "syncOptions": ["CreateNamespace=true"]},
        },
    }
    return json.dumps(app)


async def deploy_pr_env(work_id: str, n: int, tag: str, heartbeat=lambda *_: None) -> dict:
    """Create/update the PR's Argo CD Application and wait until the preview serves this image."""
    app = f"demo-todo-pr-{n}"
    await kubectl(HUB, "apply", "-f", "-", stdin=pr_app_manifest(work_id, n, tag))
    await kubectl(HUB, "-n", "argocd", "annotate", "application", app, "argocd.argoproj.io/refresh=hard",
                  "--overwrite", check=False)
    url = await edge_up(pr_port(n), "nonprod")
    deadline = asyncio.get_running_loop().time() + 420
    last = ""
    async with httpx.AsyncClient(timeout=3) as c:
        while True:
            try:
                r = await c.get(f"{url}/version.txt")
                last = r.text.strip() if r.status_code == 200 else f"HTTP {r.status_code}"
                if last == tag:
                    return {"app": app, "namespace": app, "url": url}
            except httpx.TransportError as e:
                last = type(e).__name__
            heartbeat(f"waiting for {app}: {last}")
            if asyncio.get_running_loop().time() > deadline:
                health = await kubectl(HUB, "-n", "argocd", "get", "application", app, "-o",
                                       "jsonpath={.status.sync.status}/{.status.health.status}", check=False)
                raise RuntimeError_(f"preview {app} not serving {tag} after 7m (argo {health}, last answer: {last})")
            await asyncio.sleep(3)


async def teardown_pr_env(app: str, namespace: str, n: int | None = None) -> None:
    await kubectl(HUB, "-n", "argocd", "delete", "application", app, "--ignore-not-found", "--wait=true",
                  "--timeout=90s", check=False)
    await kubectl(NONPROD, "delete", "namespace", namespace, "--ignore-not-found", "--wait=false", check=False)
    if n is None:
        m = re.search(r"(\d+)$", namespace)
        n = int(m.group(1)) if m else None
    if n is not None:
        await edge_down(pr_port(n))


# ---- Kargo ---------------------------------------------------------------

async def _freight_for(version: str) -> dict | None:
    out = await kubectl(HUB, "-n", PROJECT, "get", "freight", "-o", "json")
    for f in json.loads(out).get("items", []):
        if any(i.get("tag") == version for i in f.get("images", [])):
            return f
    return None


async def stage_status(stage: str, version: str) -> dict:
    """verified | failed | promoting | waiting, for this version's Freight in this stage."""
    f = await _freight_for(version)
    if not f:
        return {"state": "no-freight", "message": f"Warehouse has not discovered {version} yet"}
    name, alias = f["metadata"]["name"], f.get("alias", "")
    base = {"freight": name, "alias": alias}
    if stage in (f.get("status", {}).get("verifiedIn") or {}):
        return {**base, "state": "verified"}
    st = json.loads(await kubectl(HUB, "-n", PROJECT, "get", "stage", stage, "-o", "json")).get("status", {})
    hist = st.get("freightHistory") or []
    current = hist[0] if hist else {}
    mine = any(it.get("name") == name for it in (current.get("items") or {}).values())
    lp = st.get("lastPromotion") or {}
    if lp.get("freight", {}).get("name") == name:
        phase = (lp.get("status") or {}).get("phase", "")
        if phase in ("Failed", "Errored", "Aborted"):
            return {**base, "state": "failed", "message": f"promotion {phase.lower()}: "
                    + (lp.get("status") or {}).get("message", "")[:300]}
        if phase and phase != "Succeeded":
            return {**base, "state": "promoting", "message": f"promotion {phase.lower()}"}
    if mine:
        vh = (current.get("verificationHistory") or [{}])[0]
        if vh.get("phase") in ("Failed", "Error", "Aborted"):
            return {**base, "state": "failed", "message": f"verification {vh['phase'].lower()}: {vh.get('message', '')[:300]}"}
        return {**base, "state": "verifying", "message": f"verification {vh.get('phase', 'pending').lower()}"}
    return {**base, "state": "waiting", "message": f"waiting for a promotion to {stage}"}


async def promote(stage: str, version: str) -> dict:
    f = await _freight_for(version)
    if not f:
        raise RuntimeError_(f"no Freight for {version} yet")
    manifest = json.dumps({"apiVersion": "kargo.akuity.io/v1alpha1", "kind": "Promotion",
                           "metadata": {"generateName": f"{stage}-", "namespace": PROJECT},
                           "spec": {"stage": stage, "freight": f["metadata"]["name"]}})
    out = await kubectl(HUB, "create", "-f", "-", "-o", "name", stdin=manifest)
    return {"promotion": out.strip().split("/")[-1], "freight": f["metadata"]["name"], "alias": f.get("alias", "")}


def stage_url(stage: str) -> str:
    return f"http://localhost:{STAGE_PORTS[stage]}"


# ---- the dev agent over A2A (kagent) -------------------------------------------

async def _kagent(*args: str, timeout: float = 120) -> dict:
    rc, out, err = await sh(KAGENT, "--api-url", KAGENT_API, "--gateway-url", KAGENT_API, "-o", "json",
                            *args, check=False, timeout=timeout)
    if rc:
        raise RuntimeError_(f"kagent {' '.join(args[:3])}: {(err or out).strip()[-800:]}")
    return json.loads(out[out.index("{"):]) if "{" in out else {}


async def agent_session(work_id: str) -> str:
    """One kagent session per work item; the work id is the idempotency key, so retries reuse it."""
    out = await _kagent("agent", "session", "create", "--agent", DEV_AGENT, "--request-id", f"work-{work_id}")
    return out["session"]["id"]


async def agent_invoke(session_id: str, prompt: str, timeout_s: float) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(prompt)
        path = f.name
    try:
        out = await _kagent("--timeout", f"{int(timeout_s)}s", "agent", "invoke", "--session", session_id,
                            "--file", path, timeout=timeout_s + 30)
    finally:
        os.unlink(path)
    task = out.get("task", out)
    state = (task.get("status") or {}).get("state", "").replace("TASK_STATE_", "").lower()
    # Parts are streamed chunks of one message: concatenate them; separate whole artifacts.
    texts = ["".join(p.get("text", "") for p in a.get("parts") or []) for a in task.get("artifacts") or []]
    msg = (task.get("status") or {}).get("message") or {}
    texts.append("".join(p.get("text", "") for p in msg.get("parts") or []))
    return {"state": state or "unknown", "text": "\n\n".join(t.strip() for t in texts if t.strip())}


# ---- CLI (bootstrap from scripts/delivery-up.sh) --------------------------------

async def _bootstrap() -> None:
    tags = await registry_tags("demo-todo")
    if any(SEMVER.match(t) for t in tags):
        print(f"demo-todo already released: {sorted(t for t in tags if SEMVER.match(t))[-3:]}")
        return
    sha = (await gitea.base("main")).sha
    print(f"building demo-todo 0.1.0 from main@{sha[:8]}")
    print(await release(sha, "feature"))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "bootstrap":
        asyncio.run(_bootstrap())
    elif cmd == "edge":
        print(asyncio.run(edge_up(int(sys.argv[2]), sys.argv[3])))
    else:
        sys.exit(__doc__)
