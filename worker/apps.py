"""Verify + preview environments. One per revision; the env that verified a diff is the preview.

Both runtimes end up with the same thing: the supervisor (images/app/supervisor.mjs) reachable
at a localhost URL on the host. The worker POSTs /__poc/verify to it; reviewers open the same URL.

  local      docker run, --network none, port published on 127.0.0.1
  openshell  sandbox from the app image, no providers, app-policy.yaml (no egress),
             supervisor listens on a per-sandbox port P inside the sandbox and
             `openshell forward start -d P` exposes it on the host as localhost:P
"""
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

import httpx

from worker.change import AppEnv
from worker.runtimes.base import RuntimeError_, sh

ROOT = Path(__file__).resolve().parents[1]
LOCAL_IMAGE = os.environ.get("POC_APP_IMAGE", "mock-poc-app:dev")
OPENSHELL_IMAGE = os.environ.get("OPENSHELL_APP_IMAGE", "localhost:5001/mock-poc-app:dev")
OPENSHELL_POLICY = os.environ.get("OPENSHELL_APP_POLICY", str(ROOT / "clusters/openshell/app-policy.yaml"))
OPENSHELL = os.environ.get("OPENSHELL_BIN", "openshell")
SUPERVISOR_PORT = 8080


def env_name(change_id: str, n: int) -> str:
    """OpenShell caps names at 19 chars: p-<8 hex of change id>-<n>."""
    return f"p-{hashlib.sha1(change_id.encode()).hexdigest()[:8]}-{n}"


def stable_port(name: str) -> int:
    """Same port for the same sandbox, so a retried start finds its forward again."""
    return 20000 + int(hashlib.sha1(name.encode()).hexdigest(), 16) % 10000


async def wait_ready(url: str, timeout: float = 120) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    async with httpx.AsyncClient(timeout=5) as c:
        while True:
            try:
                if (await c.get(f"{url}/readyz")).status_code == 200:
                    return
            except httpx.TransportError:
                pass
            if asyncio.get_running_loop().time() > deadline:
                raise RuntimeError_(f"supervisor at {url} not ready after {timeout:.0f}s")
            await asyncio.sleep(1)


class LocalApps:
    async def start(self, change_id: str, n: int) -> AppEnv:
        name = "poc-" + env_name(change_id, n)
        rc, out, _ = await sh("docker", "port", name, str(SUPERVISOR_PORT), check=False)
        if rc != 0:
            await sh("docker", "rm", "-f", name, check=False)
            # No --network none: the port must be published. Egress is still blocked by the
            # supervisor having nothing to call; OpenShell is where egress denial is enforced.
            await sh("docker", "run", "-d", "--name", name, "--label", f"poc.change={change_id}",
                     "-p", f"127.0.0.1::{SUPERVISOR_PORT}", LOCAL_IMAGE)
            _, out, _ = await sh("docker", "port", name, str(SUPERVISOR_PORT))
        port = out.strip().splitlines()[0].rsplit(":", 1)[1]
        env = AppEnv("local", name, f"http://localhost:{port}")
        await wait_ready(env.url, 60)
        return env

    async def stop(self, env: AppEnv) -> None:
        await sh("docker", "rm", "-f", env.name, check=False)


class OpenShellApps:
    async def start(self, change_id: str, n: int) -> AppEnv:
        name = env_name(change_id, n)
        port = stable_port(name)
        rc, _, _ = await sh(OPENSHELL, "sandbox", "get", name, check=False)
        if rc != 0:
            await sh(OPENSHELL, "sandbox", "create", "--name", name, "--from", OPENSHELL_IMAGE,
                     "--no-auto-providers", "--policy", OPENSHELL_POLICY, "--detach", "--no-tty",
                     # OpenShell runs the command as its sandbox user, not the image's `node`
                     # user, so work under /tmp (read-write in app-policy.yaml).
                     "--", "env", "WORK_DIR=/tmp/work", "node", "/opt/poc/supervisor.mjs", str(port), timeout=300)
        await sh(OPENSHELL, "forward", "stop", str(port), name, check=False)
        await _detached(OPENSHELL, "forward", "start", "-d", str(port), name)
        env = AppEnv("openshell", name, f"http://localhost:{port}", {"port": port})
        await wait_ready(env.url, 120)
        return env

    async def stop(self, env: AppEnv) -> None:
        if env.meta.get("port"):
            await sh(OPENSHELL, "forward", "stop", str(env.meta["port"]), env.name, check=False)
        rc, _, err = await sh(OPENSHELL, "sandbox", "delete", env.name, check=False)
        if rc != 0 and "not found" not in err.lower():
            raise RuntimeError_(f"openshell sandbox delete {env.name}: {err.strip()}")


async def _detached(*args: str, timeout: float = 60) -> None:
    """Run a command that leaves a background process behind (`forward start -d`).

    Its output goes to /dev/null: with pipes, the background child keeps them open and we would
    wait for EOF forever. Success is judged by the readiness probe that follows.
    """
    proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.wait_for(proc.wait(), timeout)
    except asyncio.TimeoutError:
        pass  # still attached but forwarding; wait_ready decides


APPS = {"local": LocalApps(), "openshell": OpenShellApps()}


async def verify(env: AppEnv, base_tar_b64: str, diff: str) -> dict:
    async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10)) as c:
        r = await c.post(f"{env.url}/__poc/verify", json={"base_tar_b64": base_tar_b64, "diff": diff})
    if r.status_code != 200:
        raise RuntimeError_(f"verify on {env.name}: HTTP {r.status_code} {r.text[:1000]}")
    return r.json()
