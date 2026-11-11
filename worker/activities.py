"""Activities. Each goes through the Runtime adapter; none knows which runtime it's on."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from temporalio import activity
from temporalio.exceptions import ApplicationError

from worker.runtimes import Handle, RuntimeError_, get_runtime

OUT_DIR = Path(__file__).resolve().parents[1] / "out"
HEARTBEAT_EVERY = 10  # seconds; heartbeat timeout is 30 s in the workflow

log = logging.getLogger("poc.activity")


# ---- payloads -------------------------------------------------------------

@dataclass
class StartEnvInput:
    runtime: str
    role: str
    run_id: str
    n: int | None = None


@dataclass
class ImplementInput:
    handle: Handle
    task: str
    feedback: str
    scenario: str
    sleep_seconds: float | None = None


@dataclass
class ImplementResult:
    diff: str
    notes: str
    attempt: int  # the agent's own counter, read from its workspace
    egress: dict | None = None


@dataclass
class RunTestsInput:
    runtime: str
    run_id: str
    n: int
    diff: str


@dataclass
class TestResult:
    passed: bool
    output: str
    tests_touched: bool
    env: str = ""


@dataclass
class SavePatchInput:
    run_id: str
    diff: str


# ---- logging --------------------------------------------------------------

def _event(**fields) -> None:
    info = activity.info() if activity.in_activity() else None
    if info:
        fields.setdefault("activity", info.activity_type)
        fields.setdefault("attempt", info.attempt)
    log.info(json.dumps(fields, default=str))


async def _timed_call(h: Handle, op: str, payload: dict) -> dict:
    t0 = time.monotonic()
    try:
        res = await get_runtime(h.runtime).call(h, op, payload)
        _event(run_id=h.run_id, runtime=h.runtime, role=h.role, env=h.name, op=op, ok=True,
               duration_s=round(time.monotonic() - t0, 2))
        return res
    except RuntimeError_ as e:
        _event(run_id=h.run_id, runtime=h.runtime, role=h.role, env=h.name, op=op, ok=False,
               duration_s=round(time.monotonic() - t0, 2), error=str(e)[:500])
        raise ApplicationError(str(e), type="RuntimeCallFailed") from e


# ---- activities -----------------------------------------------------------

@activity.defn
async def start_env(inp: StartEnvInput) -> Handle:
    t0 = time.monotonic()
    h = await get_runtime(inp.runtime).start(inp.role, inp.run_id, inp.n)  # type: ignore[arg-type]
    _event(run_id=inp.run_id, runtime=inp.runtime, role=inp.role, env=h.name, op="start",
           duration_s=round(time.monotonic() - t0, 2))
    return h


@activity.defn
async def implement(inp: ImplementInput) -> ImplementResult:
    payload = {"task": inp.task, "feedback": inp.feedback, "scenario": inp.scenario}
    if inp.sleep_seconds is not None:
        payload["sleep_seconds"] = inp.sleep_seconds
    call = asyncio.ensure_future(_timed_call(inp.handle, "implement", payload))
    try:
        while True:
            done, _ = await asyncio.wait({call}, timeout=HEARTBEAT_EVERY)
            if done:
                break
            activity.heartbeat({"env": inp.handle.name, "waiting_on": "implement"})
    except asyncio.CancelledError:
        call.cancel()
        raise
    out = call.result()
    return ImplementResult(diff=out["diff"], notes=out.get("notes", ""), attempt=int(out.get("attempt", 0)),
                           egress=out.get("egress"))


@activity.defn
async def run_tests(inp: RunTestsInput) -> TestResult:
    """Fresh test env per attempt. A failing test is a normal result, not an error."""
    rt = get_runtime(inp.runtime)
    h = await rt.start("test", inp.run_id, inp.n)
    _event(run_id=inp.run_id, runtime=inp.runtime, role="test", env=h.name, op="start")
    try:
        out = await _timed_call(h, "test", {"diff": inp.diff})
        return TestResult(passed=bool(out["passed"]), output=out.get("output", ""),
                          tests_touched=bool(out.get("tests_touched")), env=h.name)
    finally:
        await rt.stop(h)
        _event(run_id=inp.run_id, runtime=inp.runtime, role="test", env=h.name, op="stop")


@activity.defn
async def pause_env(h: Handle) -> None:
    t0 = time.monotonic()
    await get_runtime(h.runtime).pause(h)
    _event(run_id=h.run_id, runtime=h.runtime, role=h.role, env=h.name, op="pause",
           duration_s=round(time.monotonic() - t0, 2))


@activity.defn
async def save_patch(inp: SavePatchInput) -> str:
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / f"{inp.run_id}.patch"
    path.write_text(inp.diff)
    _event(run_id=inp.run_id, op="save_patch", path=str(path))
    return str(path)


@activity.defn
async def stop_env(h: Handle) -> None:
    await get_runtime(h.runtime).stop(h)  # adapters treat "already gone" as success
    _event(run_id=h.run_id, runtime=h.runtime, role=h.role, env=h.name, op="stop")


ALL = [start_env, implement, run_tests, pause_env, save_patch, stop_env]
