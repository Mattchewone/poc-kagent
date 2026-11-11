"""Local docker runtime: not in the plan's two-runtime matrix, but lets the workflow
and every experiment run with no cluster at all. Command-shaped like OpenShell;
pause uses `docker pause` so it also mimics Substrate's suspend/resume.
"""
from __future__ import annotations

import json
import os

from .base import Handle, Role, env_name, parse_agent_json, sh

IMAGE = os.environ.get("POC_IMAGE", "mock-poc-agent:dev")
AGENT_CMD = ["python", "/opt/poc/agent/fake_agent.py"]


class LocalRuntime:
    name = "local"

    async def start(self, role: Role, run_id: str, n: int | None = None) -> Handle:
        name = "poc-" + env_name(role, run_id, n)
        rc, out, _ = await sh("docker", "inspect", "-f", "{{.State.Status}}", name, check=False)
        if rc == 0 and out.strip() in ("running", "paused"):
            # Activity retry after create already succeeded: reuse, don't duplicate.
            return Handle("local", role, run_id, name)
        if rc == 0:
            await sh("docker", "rm", "-f", name, check=False)
        await sh(
            "docker", "run", "-d", "--name", name,
            "--network", "none",  # egress denied, like the empty sandbox/egress policies
            "--label", f"poc.run={run_id}", "--label", f"poc.role={role}",
            IMAGE, "sleep", "infinity",
        )
        return Handle("local", role, run_id, name)

    async def call(self, h: Handle, op: str, payload: dict) -> dict:
        _, status, _ = await sh("docker", "inspect", "-f", "{{.State.Status}}", h.name)
        if status.strip() == "paused":
            await sh("docker", "unpause", h.name)  # "resumed by the next request"
        _, out, _ = await sh("docker", "exec", "-i", h.name, *AGENT_CMD, op, stdin=json.dumps(payload), check=False)
        return parse_agent_json(out)

    async def pause(self, h: Handle) -> None:
        _, status, _ = await sh("docker", "inspect", "-f", "{{.State.Status}}", h.name)
        if status.strip() == "running":
            await sh("docker", "pause", h.name)

    async def stop(self, h: Handle) -> None:
        await sh("docker", "rm", "-f", h.name, check=False)  # idempotent if already gone
