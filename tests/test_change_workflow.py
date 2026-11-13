"""ChangeWorkflow with stub activities (no Gitea, no sandboxes, no cluster).

Covers: failed revision -> fixed revision -> preview -> approval opens a PR and tears the preview
down; approve is refused while nothing is previewable; infrastructure errors fail a revision, not
the change; abandon cleans up. Needs the `temporal` CLI on PATH.
"""
import asyncio
import shutil
import sys
import uuid
from pathlib import Path

import pytest
from temporalio import activity
from temporalio.client import WorkflowUpdateFailedError
from temporalio.service import RPCError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from worker.change import (  # noqa: E402
    PREVIEW, AppEnv, ChangeRequest, ChangeWorkflow, OpenPrInput, Revision, StartAppInput, VerifyInput,
    VerifyResult, summarize,
)

TEMPORAL = shutil.which("temporal")
pytestmark = pytest.mark.skipif(TEMPORAL is None, reason="temporal CLI not installed")


class Stubs:
    """A diff containing 'good' passes; 'boom' makes the env fail to start; anything else fails tests."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.prs: list[OpenPrInput] = []

    def activities(self):
        @activity.defn(name="start_app_env")
        async def start_app_env(inp: StartAppInput) -> AppEnv:
            name = f"p-{inp.change_id}-{inp.n}"
            self.started.append(name)
            return AppEnv(inp.runtime, name, f"http://localhost:{20000 + inp.n}")

        @activity.defn(name="verify_change")
        async def verify_change(inp: VerifyInput) -> VerifyResult:
            if "boom" in inp.diff:
                raise RuntimeError("sandbox unreachable")
            ok = "good" in inp.diff
            return VerifyResult(passed=ok, base_sha="abc12345", output="" if ok else "1 failing",
                                agent_tests={"pass": 9, "fail": 0 if ok else 1, "ok": ok},
                                base_tests={"pass": 8, "fail": 0, "ok": True}, touched=["src/store.js"])

        @activity.defn(name="stop_app_env")
        async def stop_app_env(env: AppEnv) -> None:
            self.stopped.append(env.name)

        @activity.defn(name="open_pr")
        async def open_pr(inp: OpenPrInput) -> dict:
            self.prs.append(inp)
            return {"number": len(self.prs), "url": f"http://gitea/pulls/{len(self.prs)}"}

        return [start_app_env, verify_change, stop_app_env, open_pr]


async def wait_phase(h, phase: str, revisions: int, timeout: float = 15) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        st = await h.query(ChangeWorkflow.status)
        if st["phase"] == phase and len(st["revisions"]) >= revisions and "passed" in st["revisions"][-1]:
            return st
        assert asyncio.get_running_loop().time() < deadline, f"stuck in {st['phase']}: {st}"
        await asyncio.sleep(0.1)


def request(cid: str, diff: str) -> ChangeRequest:
    return ChangeRequest(change_id=cid, title="Fix counter", kind="bug", description="left is wrong",
                         first=Revision(diff=diff, summary="try 1", rev_id="r1"), runtime="local")


def run(case):
    """Each test gets its own throwaway dev server (no pytest-asyncio in this repo)."""
    async def main():
        async with await WorkflowEnvironment.start_local(dev_server_existing_path=TEMPORAL) as env:
            await case(env)
    asyncio.run(main())


def test_fail_revise_preview_approve():
    run(_fail_revise_preview_approve)


async def _fail_revise_preview_approve(env):
    stubs, tq, cid = Stubs(), f"tq-{uuid.uuid4().hex[:6]}", f"c-{uuid.uuid4().hex[:6]}"
    async with Worker(env.client, task_queue=tq, workflows=[ChangeWorkflow], activities=stubs.activities()):
        h = await env.client.start_workflow(ChangeWorkflow.run, request(cid, "bad diff"), id=cid, task_queue=tq)
        st = await wait_phase(h, "verify failed", 1)
        assert summarize(st)["verify"]["output_tail"] == "1 failing"
        assert stubs.stopped == [f"p-{cid}-1"]  # failed env torn down straight away

        with pytest.raises(WorkflowUpdateFailedError):  # nothing to approve yet
            await h.execute_update(ChangeWorkflow.approve)

        await h.execute_update(ChangeWorkflow.submit, Revision(diff="good diff", summary="fixed", rev_id="r2"))
        st = await wait_phase(h, PREVIEW, 2)
        assert st["preview_url"] == "http://localhost:20002"
        assert st["revisions"][-1]["rev_id"] == "r2"

        pr = await h.execute_update(ChangeWorkflow.approve)
        assert pr["number"] == 1
        assert await h.result() == "pr opened: http://gitea/pulls/1"
        assert stubs.prs[0].diff == "good diff" and stubs.prs[0].base_sha == "abc12345"
        assert stubs.stopped == [f"p-{cid}-1", f"p-{cid}-2"]  # preview torn down after the PR

        with pytest.raises((WorkflowUpdateFailedError, RPCError)):  # change is finished
            await h.execute_update(ChangeWorkflow.submit, Revision(diff="good again", rev_id="r3"))


def test_new_revision_replaces_preview():
    run(_new_revision_replaces_preview)


async def _new_revision_replaces_preview(env):
    stubs, tq, cid = Stubs(), f"tq-{uuid.uuid4().hex[:6]}", f"c-{uuid.uuid4().hex[:6]}"
    async with Worker(env.client, task_queue=tq, workflows=[ChangeWorkflow], activities=stubs.activities()):
        h = await env.client.start_workflow(ChangeWorkflow.run, request(cid, "good v1"), id=cid, task_queue=tq)
        await wait_phase(h, PREVIEW, 1)
        await h.execute_update(ChangeWorkflow.submit, Revision(diff="good v2", summary="reviewer asked", rev_id="r2"))
        st = await wait_phase(h, PREVIEW, 2)
        assert stubs.stopped == [f"p-{cid}-1"] and st["preview_url"].endswith("20002")
        await h.signal(ChangeWorkflow.abandon, "changed my mind")
        assert await h.result() == "abandoned: changed my mind"
        assert stubs.stopped == [f"p-{cid}-1", f"p-{cid}-2"] and not stubs.prs


def test_infra_error_fails_revision_not_change():
    run(_infra_error_fails_revision_not_change)


async def _infra_error_fails_revision_not_change(env):
    stubs, tq, cid = Stubs(), f"tq-{uuid.uuid4().hex[:6]}", f"c-{uuid.uuid4().hex[:6]}"
    async with Worker(env.client, task_queue=tq, workflows=[ChangeWorkflow], activities=stubs.activities()):
        h = await env.client.start_workflow(ChangeWorkflow.run, request(cid, "boom"), id=cid, task_queue=tq)
        st = await wait_phase(h, "verify failed", 1, timeout=60)  # verify retries 3x first
        assert "verification could not run" in st["revisions"][0]["verify"]["error"]
        await h.execute_update(ChangeWorkflow.submit, Revision(diff="good", rev_id="r2"))
        await wait_phase(h, PREVIEW, 2)
        await h.signal(ChangeWorkflow.abandon)
        await h.result()
