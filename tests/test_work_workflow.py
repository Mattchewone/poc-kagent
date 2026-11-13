"""WorkItemWorkflow with stub activities (no kagent, Gitea, OpenShell, registry or Kargo).

The stub `run_agent` plays the dev agent: it calls the workflow's `submit` Update for each diff in
its script, exactly as the real agent does through the submit_revision MCP tool. A diff containing
'good' passes verification. Needs the `temporal` CLI on PATH.
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

from worker.work import (  # noqa: E402
    CLOSED, DONE, IN_REVIEW, NEEDS_INPUT, AppEnv, Revision, StartAppInput, VerifyInput, VerifyResult,
    AgentTurnInput, AgentTurnResult, CommentInput, Feedback,
    ImageInput, PrEnv, PrEnvInput, PrEvent, PrInfo, PublishInput, ReleaseInput, WorkItemWorkflow,
    WorkRequest, summarize,
)

TEMPORAL = shutil.which("temporal")
pytestmark = pytest.mark.skipif(TEMPORAL is None, reason="temporal CLI not installed")


class Stubs:
    def __init__(self, client, turns: list[list[str]]) -> None:
        self.client = client
        self.turns = list(turns)  # per agent turn: the diffs it submits
        self.prompts: list[str] = []
        self.published: list[PublishInput] = []
        self.images: list[ImageInput] = []
        self.deployed: list[PrEnvInput] = []
        self.torn_down: list[str] = []
        self.comments: list[str] = []
        self.merged: list[int] = []
        self.closed: list[int] = []
        self.releases: list[ReleaseInput] = []

    def activities(self):
        @activity.defn(name="start_app_env")
        async def start_app_env(inp: StartAppInput) -> AppEnv:
            return AppEnv(inp.runtime, f"p-{inp.n}", f"http://localhost:{20000 + inp.n}")

        @activity.defn(name="verify_change")
        async def verify_change(inp: VerifyInput) -> VerifyResult:
            ok = "good" in inp.diff
            return VerifyResult(passed=ok, base_sha="abc12345", output="" if ok else "1 failing",
                                agent_tests={"pass": 9, "fail": 0 if ok else 1, "ok": ok},
                                base_tests={"pass": 8, "fail": 0, "ok": True}, touched=["src/store.js"])

        @activity.defn(name="stop_app_env")
        async def stop_app_env(env: AppEnv) -> None:
            pass

        @activity.defn(name="run_agent")
        async def run_agent(inp: AgentTurnInput) -> AgentTurnResult:
            self.prompts.append(inp.prompt)
            diffs = self.turns.pop(0) if self.turns else []
            h = self.client.get_workflow_handle(inp.work_id)
            last = None
            for d in diffs:
                last = await h.execute_update(WorkItemWorkflow.submit, Revision(diff=d, summary=f"did {d}", rev_id=d))
            text = "Which colour do you want?" if not diffs else f"done: {last}"
            return AgentTurnResult("sess-1", "completed", text)

        @activity.defn(name="publish_pr")
        async def publish_pr(inp: PublishInput) -> PrInfo:
            self.published.append(inp)
            return PrInfo(7, "http://gitea/pulls/7", f"{len(self.published):040d}", f"agent/{inp.work_id}")

        @activity.defn(name="build_image")
        async def build_image(inp: ImageInput) -> str:
            self.images.append(inp)
            return f"kargo-registry:5000/{inp.repo}:{inp.tag}"

        @activity.defn(name="deploy_pr_env")
        async def deploy_pr_env(inp: PrEnvInput) -> PrEnv:
            self.deployed.append(inp)
            return PrEnv(f"demo-todo-pr-{inp.pr_number}", f"demo-todo-pr-{inp.pr_number}", "http://localhost:31107")

        @activity.defn(name="teardown_pr_env")
        async def teardown_pr_env(env: PrEnv) -> None:
            self.torn_down.append(env.app)

        @activity.defn(name="comment_pr")
        async def comment_pr(inp: CommentInput) -> None:
            self.comments.append(inp.body)

        @activity.defn(name="pr_state")
        async def pr_state(number: int) -> dict:
            return {"state": "open", "merged": False, "merge_sha": ""}

        @activity.defn(name="merge_pr")
        async def merge_pr(number: int) -> dict:
            self.merged.append(number)
            return {"merge_sha": "m" * 40}

        @activity.defn(name="close_pr")
        async def close_pr(number: int) -> None:
            self.closed.append(number)

        @activity.defn(name="release_image")
        async def release_image(inp: ReleaseInput) -> dict:
            self.releases.append(inp)
            return {"version": "0.2.0", "image": "kargo-registry:5000/demo-todo:0.2.0", "sha": inp.merge_sha}

        return [start_app_env, verify_change, stop_app_env, run_agent, publish_pr, build_image, deploy_pr_env,
                teardown_pr_env, comment_pr, pr_state, merge_pr, close_pr, release_image]


async def wait_for(h, pred, timeout: float = 20) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        st = await h.query(WorkItemWorkflow.status)
        if pred(st):
            return st
        assert asyncio.get_running_loop().time() < deadline, f"stuck in {st['phase']}: {summarize(st)}"
        await asyncio.sleep(0.1)


def run(turns, case):
    async def main():
        async with await WorkflowEnvironment.start_local(dev_server_existing_path=TEMPORAL) as env:
            stubs, tq, wid = Stubs(env.client, turns), f"tq-{uuid.uuid4().hex[:6]}", f"w-{uuid.uuid4().hex[:6]}"
            async with Worker(env.client, task_queue=tq, workflows=[WorkItemWorkflow], activities=stubs.activities()):
                req = WorkRequest(wid, "Fix counter", "left is wrong", kind="bug", runtime="local")
                h = await env.client.start_workflow(WorkItemWorkflow.run, req, id=wid, task_queue=tq)
                await case(h, stubs)
    asyncio.run(main())


def test_request_to_release():
    """fail -> fix -> PR + preview -> feedback -> revision updates the same PR -> merge -> release (then Kargo's job)"""
    async def case(h, s: Stubs):
        st = await wait_for(h, lambda st: st["phase"] == IN_REVIEW)
        assert [r["passed"] for r in st["revisions"]] == [False, True]
        assert st["preview"]["url"] == "http://localhost:31107" and st["pr"]["number"] == 7
        assert s.images[0].repo == "demo-todo-pr" and s.images[0].tag.startswith("pr7-")
        assert "Fix counter" in s.prompts[0] and "submit_revision" in s.prompts[0]

        await h.signal(WorkItemWorkflow.add_feedback, Feedback("make it plural-aware", "matt", "pr-comment"))
        with pytest.raises(WorkflowUpdateFailedError):  # can't merge with feedback pending / while coding
            await h.execute_update(WorkItemWorkflow.merge, "chat")
        st = await wait_for(h, lambda st: st["phase"] == IN_REVIEW and len(st["revisions"]) == 3)
        assert "plural-aware" in s.prompts[1] and "PR #7" in s.prompts[1]
        assert len(s.published) == 2 and s.published[1].diff == "good v2"
        assert st["preview"]["revision"] == 3 and s.torn_down == []  # same preview app, updated in place

        out = await h.execute_update(WorkItemWorkflow.merge, "chat")
        assert out["merge_sha"] == "m" * 40 and s.merged == [7]
        assert await h.result() == "0.2.0 released; Kargo rolls it out"
        st = await h.query(WorkItemWorkflow.status)
        assert st["phase"] == DONE and s.torn_down == ["demo-todo-pr-7"] and s.releases[0].merge_sha == "m" * 40
        assert summarize(st)["release"] == "0.2.0" and "Released as" in s.comments[-1]

    run([["bad", "good v1"], ["good v2"]], case)


def test_agent_question_then_merge_in_gitea():
    """No submission -> needs input; the answer starts a new turn; a merge in Gitea (webhook) releases it."""
    async def case(h, s: Stubs):
        st = await wait_for(h, lambda st: st["phase"] == NEEDS_INPUT)
        assert summarize(st)["agent_says"] == "Which colour do you want?"
        await h.signal(WorkItemWorkflow.add_feedback, Feedback("blue", "chat"))
        await wait_for(h, lambda st: st["phase"] == IN_REVIEW)
        assert "blue" in s.prompts[1]
        await h.signal(WorkItemWorkflow.pr_event, PrEvent("merged", "w" * 40, "matt"))
        assert await h.result() == "0.2.0 released; Kargo rolls it out"
        assert s.merged == [] and s.releases[0].merge_sha == "w" * 40 and s.torn_down == ["demo-todo-pr-7"]

    run([[], ["good"]], case)


def test_closed_pr_and_stuck_agent():
    """Three failing turns -> needs input; a PR closed in Gitea ends the work item and removes the preview."""
    async def case(h, s: Stubs):
        await wait_for(h, lambda st: st["phase"] == NEEDS_INPUT)
        assert len(s.prompts) == 3 and "did not pass verification" in s.prompts[1]
        await h.signal(WorkItemWorkflow.add_feedback, Feedback("try the store, not the UI", "chat"))
        await wait_for(h, lambda st: st["phase"] == IN_REVIEW)
        await h.signal(WorkItemWorkflow.pr_event, PrEvent("closed", "", "matt"))
        assert await h.result() == "pr closed without merging"
        st = await h.query(WorkItemWorkflow.status)
        assert st["phase"] == CLOSED and s.torn_down == ["demo-todo-pr-7"] and s.closed == []

    run([["bad 1"], ["bad 2"], ["bad 3"], ["good"]], case)


def test_abandon_closes_pr():
    async def case(h, s: Stubs):
        await wait_for(h, lambda st: st["phase"] == IN_REVIEW)
        await h.signal(WorkItemWorkflow.abandon, "not needed")
        assert await h.result() == "abandoned: not needed"
        assert s.closed == [7] and s.torn_down == ["demo-todo-pr-7"]

    run([["good"]], case)
