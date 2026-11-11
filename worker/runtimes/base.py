"""Runtime protocol: activities talk to this, never to a runtime directly."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Literal, Protocol

Role = Literal["agent", "test"]
RuntimeName = Literal["openshell", "substrate", "local"]

log = logging.getLogger("poc.runtime")


@dataclass
class Handle:
    runtime: str
    role: str
    run_id: str
    name: str
    meta: dict = field(default_factory=dict)


class RuntimeError_(Exception):
    """A runtime call failed (non-zero exit, HTTP error, agent error)."""


class Runtime(Protocol):
    async def start(self, role: Role, run_id: str, n: int | None = None) -> Handle: ...
    async def call(self, h: Handle, op: str, payload: dict) -> dict: ...
    async def pause(self, h: Handle) -> None: ...  # Substrate: suspend; OpenShell: no-op
    async def stop(self, h: Handle) -> None: ...


def env_name(role: str, run_id: str, n: int | None) -> str:
    """agent-<run id> once per run, test-<run id>-<n> per attempt. DNS-1123 safe."""
    base = f"{role}-{run_id}" if n is None else f"{role}-{run_id}-{n}"
    safe = "".join(c if c.isalnum() or c == "-" else "-" for c in base.lower()).strip("-")
    return safe[:63].rstrip("-")


async def sh(*args: str, stdin: str | None = None, timeout: float = 600, check: bool = True) -> tuple[int, str, str]:
    """Run a command; return (rc, stdout, stderr). Logged with duration."""
    t0 = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin.encode() if stdin is not None else None), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        proc.kill()
        await proc.wait()
        raise
    rc = proc.returncode or 0
    so, se = out.decode(errors="replace"), err.decode(errors="replace")
    log.debug("sh %s rc=%s %.2fs", " ".join(args[:4]), rc, time.monotonic() - t0)
    if check and rc != 0:
        raise RuntimeError_(f"{' '.join(args)} exited {rc}: {se.strip()[-2000:] or so.strip()[-2000:]}")
    return rc, so, se


def parse_agent_json(stdout: str) -> dict:
    """The agent prints one JSON object; CLIs may wrap it in banner lines, so take the last JSON line."""
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            data = json.loads(line)
            if "error" in data and len(data) == 1:
                raise RuntimeError_(f"agent error: {data['error']}")
            return data
    raise RuntimeError_(f"no JSON in agent output: {stdout[-1000:]!r}")
