"""Subprocess helper used by the activities (git, docker, kubectl, openshell, kagent)."""
from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger("poc.sh")


class RuntimeError_(Exception):
    """A command or remote call failed (non-zero exit, HTTP error)."""


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
