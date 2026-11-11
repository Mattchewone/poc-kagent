"""OpenShell adapter (command-shaped): create a sandbox, exec in it, delete it.

Shells out to the `openshell` CLI (pinned v0.1.8), against whichever gateway is
active (`make openshell-up` registers the port-forwarded one as `poc`).
Verified from the v0.1.8 CLI source: `sandbox create --from --no-auto-providers
--policy`, `sandbox exec -n NAME -- cmd` with piped stdin (4 MiB cap),
`sandbox get`, `sandbox delete`. Not yet verified against a live gateway:
whether `create --detach -- sleep infinity` is the right way to keep it idle.
Override the create flags with OPENSHELL_CREATE_EXTRA if needed.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
from pathlib import Path

from .base import Handle, Role, RuntimeError_, env_name, parse_agent_json, sh

ROOT = Path(__file__).resolve().parents[2]
CLI = os.environ.get("OPENSHELL_BIN", "openshell")
IMAGE = os.environ.get("OPENSHELL_IMAGE", os.environ.get("POC_IMAGE", "mock-poc-agent:dev"))
POLICY = os.environ.get("OPENSHELL_POLICY", str(ROOT / "clusters/openshell/sandbox-policy.yaml"))
EXTRA = shlex.split(os.environ.get("OPENSHELL_CREATE_EXTRA", "--detach --no-tty"))
AGENT_CMD = ["python", "/opt/poc/agent/fake_agent.py"]


MAX_NAME = 19  # OpenShell v0.1.8: "name exceeds maximum length (21 > 19)"


def sandbox_name(role: str, run_id: str, n: int | None = None) -> str:
    """a-<run id> / t-<run id>-<n>; falls back to a hash of the run id when that's > 19 chars."""
    suffix = "" if n is None else f"-{n}"
    name = env_name(f"{role[0]}", run_id, n)
    if len(name) > MAX_NAME:
        name = f"{role[0]}-{hashlib.sha1(run_id.encode()).hexdigest()[:8]}{suffix}"
    return name


def _gone(err: str) -> bool:
    e = err.lower()
    return "not found" in e or "notfound" in e or "does not exist" in e


class OpenShellRuntime:
    name = "openshell"

    async def start(self, role: Role, run_id: str, n: int | None = None) -> Handle:
        name = sandbox_name(role, run_id, n)
        rc, _, _ = await sh(CLI, "sandbox", "get", name, check=False)
        if rc == 0:  # activity retry after a create that already landed
            return Handle("openshell", role, run_id, name)
        await sh(
            CLI, "sandbox", "create", "--name", name, "--from", IMAGE,
            "--no-auto-providers",  # no providers: no credentials, no provider egress
            "--policy", POLICY, *EXTRA, "--", "sleep", "infinity",
            timeout=300,
        )
        return Handle("openshell", role, run_id, name)

    async def call(self, h: Handle, op: str, payload: dict) -> dict:
        rc, out, err = await sh(
            CLI, "sandbox", "exec", "-n", h.name, "--no-tty", "--timeout", "0", "--", *AGENT_CMD, op,
            stdin=json.dumps(payload), check=False,
        )
        try:
            return parse_agent_json(out)
        except RuntimeError_:
            raise RuntimeError_(f"openshell exec {op} rc={rc}: {err.strip()[-1500:]} {out.strip()[-500:]}")

    async def pause(self, h: Handle) -> None:  # noqa: D102
        return None  # OpenShell sandboxes stay up; nothing to suspend

    async def stop(self, h: Handle) -> None:
        rc, _, err = await sh(CLI, "sandbox", "delete", h.name, check=False)
        if rc != 0 and not _gone(err):
            raise RuntimeError_(f"openshell sandbox delete {h.name}: {err.strip()}")
