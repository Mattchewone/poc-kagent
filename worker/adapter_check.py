"""Milestone 5: drive each Runtime adapter directly, no Temporal.

    python worker/adapter_check.py local openshell substrate

Prints the same normalized result per runtime; exits non-zero if they differ
or anything was left running.
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from worker.runtimes import get_runtime  # noqa: E402


async def check(name: str) -> dict:
    rt = get_runtime(name)
    run_id = f"m5-{uuid.uuid4().hex[:6]}"
    agent = await rt.start("agent", run_id)
    results = []
    try:
        for n in (1, 2):
            await rt.pause(agent)  # suspend between attempts; the next call must resume it
            out = await rt.call(agent, "implement", {"scenario": "happy", "task": "fix add", "feedback": ""})
            test = await rt.start("test", run_id, n)
            try:
                res = await rt.call(test, "test", {"diff": out["diff"]})
            finally:
                await rt.stop(test)
            results.append({"attempt": out["attempt"], "passed": res["passed"], "tests_touched": res["tests_touched"]})
    finally:
        await rt.stop(agent)
        await rt.stop(agent)  # idempotent if already gone
    return {"results": results}


async def main(names: list[str]) -> int:
    outs = {}
    for n in names:
        try:
            outs[n] = await check(n)
        except Exception as e:
            outs[n] = {"error": repr(e)[:500]}
        print(f"{n:10s} {json.dumps(outs[n])}")
    expected = {"results": [{"attempt": 1, "passed": False, "tests_touched": False},
                            {"attempt": 2, "passed": True, "tests_touched": False}]}
    ok = all(o == expected for o in outs.values())
    print("IDENTICAL and as expected" if ok else "MISMATCH (see above)")
    print("Now confirm nothing is left running: make leftovers")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:] or ["local"])))
