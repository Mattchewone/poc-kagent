"""Review gate on FeatureWorkflow, with stub activities (no runtimes, no cluster).

Needs the `temporal` CLI on PATH; the test starts its own throwaway dev server.
"""
import asyncio
import shutil
import sys
import uuid
from pathlib import Path

import pytest
from temporalio import activity
from temporalio.client import WorkflowUpdateFailedError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from worker.activities import (  # noqa: E402
    ImplementInput, ImplementResult, RunTestsInput, SavePatchInput, StartEnvInput, TestResult,
)
from worker.runtimes import Handle  # noqa: E402
from worker.start import _review_when_ready  # noqa: E402
from worker.workflow import AWAITING_REVIEW, FeatureWorkflow, Review, RunRequest  # noqa: E402

TEMPORAL = shutil.which("temporal")
pytestmark = pytest.mark.skipif(TEMPORAL is None, reason="temporal CLI not installed")


class Stubs:
    """Happy scenario: attempt 1 fails, every later attempt passes. Records what the agent saw."""

    def __init__(self) -> None:
        self.feedback: list[str] = []
        self.stopped = False

    def activities(self):
        @activity.defn(name="start_env")
        async def start_env(inp: StartEnvInput) -> Handle:
            return Handle(inp.runtime, inp.role, inp.run_id, f"a-{inp.run_id}")

        @activity.defn(name="implement")
        async def implement(inp: ImplementInput) -> ImplementResult:
            self.feedback.append(inp.feedback)
            return ImplementResult(diff=f"diff {len(self.feedback)}", notes="", attempt=len(self.feedback))

        @activity.defn(name="run_tests")
        async def run_tests(inp: RunTestsInput) -> TestResult:
            return TestResult(passed=inp.n > 1, output="1 failed", tests_touched=False, env=f"t-{inp.n}")

        @activity.defn(name="pause_env")
        async def pause_env(h: Handle) -> None:
            return None

        @activity.defn(name="save_patch")
        async def save_patch(inp: SavePatchInput) -> str:
            return f"out/{inp.run_id}.patch"

        @activity.defn(name="stop_env")
        async def stop_env(h: Handle) -> None:
            self.stopped = True

        return [start_env, implement, run_tests, pause_env, save_patch, stop_env]


async def _run(decisions: list[Review]) -> tuple[str, dict, Stubs]:
    stubs = Stubs()
    async with await WorkflowEnvironment.start_local(dev_server_existing_path=TEMPORAL) as env:
        tq = f"tq-{uuid.uuid4().hex[:6]}"
        async with Worker(env.client, task_queue=tq, workflows=[FeatureWorkflow], activities=stubs.activities()):
            run_id = f"t-{uuid.uuid4().hex[:6]}"
            h = await env.client.start_workflow(
                FeatureWorkflow.run, RunRequest(runtime="local", scenario="happy", run_id=run_id),
                id=run_id, task_queue=tq,
            )
            for d in decisions:
                assert await _review_when_ready(h, d, poll=0.1) == "ok"
            result = await h.result()
            return result, await h.query(FeatureWorkflow.status), stubs


def run(decisions):
    return asyncio.run(_run(decisions))


def test_agent_starts_without_approval_and_approve_ends_run():
    result, state, stubs = run([Review(True)])
    assert result == "approved"
    assert [h["passed"] for h in state["history"]] == [False, True]
    assert state["history"][-1]["review"] == "approved"
    assert state["patch"].endswith(".patch") and stubs.stopped


def test_reject_without_comment_ends_run():
    result, state, stubs = run([Review(False)])
    assert result == "rejected" and len(state["history"]) == 2 and stubs.stopped


def test_reject_with_comment_goes_back_to_agent():
    result, state, stubs = run([Review(False, "keep the docstring"), Review(True)])
    assert result == "approved"
    assert len(state["history"]) == 3
    assert "keep the docstring" in stubs.feedback[2]
    assert state["history"][1]["review"] == "changes requested: keep the docstring"


def test_decision_refused_before_review():
    async def go():
        stubs = Stubs()
        gate = asyncio.Event()

        @activity.defn(name="implement")
        async def slow_implement(inp: ImplementInput) -> ImplementResult:
            await gate.wait()
            return ImplementResult(diff="d", notes="", attempt=1)

        acts = [a for a in stubs.activities() if a.__temporal_activity_definition.name != "implement"]
        async with await WorkflowEnvironment.start_local(dev_server_existing_path=TEMPORAL) as env:
            async with Worker(env.client, task_queue="tq", workflows=[FeatureWorkflow], activities=acts + [slow_implement]):
                h = await env.client.start_workflow(
                    FeatureWorkflow.run, RunRequest(runtime="local", scenario="happy", run_id="early"),
                    id="early", task_queue="tq",
                )
                while (await h.query(FeatureWorkflow.status))["phase"] != "implement":
                    await asyncio.sleep(0.1)
                with pytest.raises(WorkflowUpdateFailedError):
                    await h.execute_update(FeatureWorkflow.review_patch, Review(True))
                gate.set()
                while (await h.query(FeatureWorkflow.status))["phase"] != AWAITING_REVIEW:
                    await asyncio.sleep(0.1)
                await h.execute_update(FeatureWorkflow.review_patch, Review(True))
                assert await h.result() == "approved"

    asyncio.run(go())
