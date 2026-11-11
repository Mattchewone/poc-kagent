"""Substrate adapter (request-shaped): an actor is an HTTP server; the router resumes it on request.

Pinned to Substrate v0.5.0, where the router picks the actor from an
`ate-target-actor: <atespace>/<actor>` header (v0.1.0 used a Host header;
v1.0.0 replaces this with `kubectl ate proxy`). The worker reaches the router via
`kubectl port-forward -n ate-system svc/atenet-router 8000:80` (`make substrate-forward`).
"""
from __future__ import annotations

import asyncio
import os
import time

import httpx

from .base import Handle, Role, RuntimeError_, env_name, sh

CTX = os.environ.get("SUBSTRATE_CONTEXT", "kind-poc-substrate")
ATESPACE = os.environ.get("SUBSTRATE_ATESPACE", "poc")
ROUTER = os.environ.get("SUBSTRATE_ROUTER", "http://localhost:8000")
TEMPLATES = {"agent": os.environ.get("SUBSTRATE_AGENT_TEMPLATE", "agent"),
             "test": os.environ.get("SUBSTRATE_TEST_TEMPLATE", "test")}
READY_WAIT = float(os.environ.get("SUBSTRATE_READY_WAIT", "120"))


def _ate(*args: str) -> tuple[str, ...]:
    return ("kubectl", "ate", "--context", CTX, *args)


def _gone(err: str) -> bool:
    e = err.lower()
    return "not found" in e or "notfound" in e


class SubstrateRuntime:
    name = "substrate"

    def __init__(self) -> None:
        self._http = httpx.AsyncClient(base_url=ROUTER, timeout=httpx.Timeout(900, connect=10))

    async def start(self, role: Role, run_id: str, n: int | None = None) -> Handle:
        name = env_name(role, run_id, n)
        rc, _, _ = await sh(*_ate("get", "actor", name, "-a", ATESPACE, "-o", "json"), check=False)
        if rc != 0:
            await sh(*_ate("create", "actor", name, "-a", ATESPACE, "--template", TEMPLATES[role]), timeout=300)
        return Handle("substrate", role, run_id, name, {"atespace": ATESPACE})

    async def call(self, h: Handle, op: str, payload: dict) -> dict:
        headers = {"ate-target-actor": f"{h.meta.get('atespace', ATESPACE)}/{h.name}"}
        deadline = time.monotonic() + READY_WAIT
        while True:
            try:
                r = await self._http.post(f"/{op}", json=payload, headers=headers)
            except httpx.TransportError as e:
                if time.monotonic() > deadline:
                    raise RuntimeError_(f"router unreachable for {h.name}: {e!r}")
                await asyncio.sleep(2)
                continue
            # 502/503/504 while the actor is still being placed or resumed: retry until READY_WAIT.
            if r.status_code in (502, 503, 504) and time.monotonic() < deadline:
                await asyncio.sleep(2)
                continue
            if r.status_code != 200:
                raise RuntimeError_(f"{op} on {h.name}: HTTP {r.status_code} {r.text[:1000]}")
            return r.json()

    async def pause(self, h: Handle) -> None:
        rc, out, err = await sh(*_ate("suspend", "actor", h.name, "-a", ATESPACE), check=False)
        if rc != 0:
            _, state, _ = await sh(*_ate("get", "actor", h.name, "-a", ATESPACE, "-o", "json"), check=False)
            if "ACTOR_STATE_SUSPENDED" not in state:  # already suspended is fine
                raise RuntimeError_(f"suspend {h.name}: {err.strip() or out.strip()}")

    async def stop(self, h: Handle) -> None:
        # Substrate only deletes non-running actors ("not in a deletable state
        # (state: ACTOR_STATE_RUNNING)"), so suspend first, then retry briefly
        # while the suspend settles.
        err = ""
        for i in range(10):
            rc, _, err = await sh(*_ate("delete", "actor", h.name, "-a", ATESPACE), check=False)
            if rc == 0 or _gone(err):
                return
            if "deletable state" not in err:
                break
            if i == 0 or "RUNNING" in err:
                await sh(*_ate("suspend", "actor", h.name, "-a", ATESPACE), check=False)
            await asyncio.sleep(2)
        raise RuntimeError_(f"delete {h.name}: {err.strip()}")
