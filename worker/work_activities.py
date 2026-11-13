"""Activities for WorkItemWorkflow (worker/work.py). verify uses the ChangeWorkflow activities
(start_app_env / verify_change / stop_app_env in change_activities.py) unchanged."""
from __future__ import annotations

import asyncio

from temporalio import activity

from worker import delivery, gitea
from worker.work import (
    AgentTurnInput, AgentTurnResult, CommentInput, ImageInput, PrEnv, PrEnvInput, PrInfo, PublishInput,
    ReleaseInput, StageInput,
)


async def _heartbeating(coro, every: float = 20, detail: str = ""):
    """Await `coro` while heartbeating, so Temporal notices a dead worker within heartbeat_timeout."""
    task = asyncio.ensure_future(coro)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=every)
            if done:
                return task.result()
            activity.heartbeat(detail)
    finally:
        if not task.done():
            task.cancel()


@activity.defn
async def run_agent(inp: AgentTurnInput) -> AgentTurnResult:
    """One dev-agent turn in the work item's kagent session (A2A). Blocks until the turn ends."""
    session = await delivery.agent_session(inp.work_id)
    prompt = inp.prompt
    if activity.info().attempt > 1:
        prompt = (f"(The pipeline lost track of your previous turn on work item `{inp.work_id}`, attempt "
                  f"{activity.info().attempt}. If you already submitted a passing revision, just summarise it; "
                  "otherwise carry on.)\n\n" + prompt)
    timeout = max(60.0, (activity.info().start_to_close_timeout.total_seconds() if activity.info().start_to_close_timeout else 2700) - 60)
    res = await _heartbeating(delivery.agent_invoke(session, prompt, timeout), detail=f"session {session}")
    if res["state"] in ("failed", "rejected", "canceled"):
        raise RuntimeError(f"agent turn {res['state']}: {res['text'][-500:]}")
    return AgentTurnResult(session, res["state"], res["text"])


def _pr_body(inp: PublishInput) -> str:
    v = inp.verify
    lines = [
        f"**{inp.kind.title()} request** (from {inp.source}{' ' + inp.source_ref if inp.source_ref else ''}):",
        "", inp.description, "",
        "### Agent summary", inp.summary or "_none given_", "",
        f"### Verification of revision {inp.revision} (fresh OpenShell sandbox, no network, base tests restored)",
        f"- agent's tests: {v.get('agent_tests', {}).get('pass', '?')} passed",
        f"- base tests: {v.get('base_tests', {}).get('pass', '?')} passed",
        f"- files: {', '.join(v.get('touched', []))}", "",
        f"Work item `{inp.work_id}`, base `{inp.base_sha[:8]}`. Managed by Temporal: a preview environment is "
        "deployed to kargo-nonprod for every revision. Comment `/agent <feedback>` to request changes.",
    ]
    if inp.source == "gitea-issue" and inp.source_ref:
        lines += ["", f"Closes {inp.source_ref}"]
    return "\n".join(lines)


@activity.defn
async def publish_pr(inp: PublishInput) -> PrInfo:
    branch = f"agent/{inp.work_id}"
    pr = await gitea.open_pull_request(branch, inp.base_sha, inp.diff, inp.title, _pr_body(inp))
    if inp.source == "gitea-issue" and inp.source_ref.startswith("#") and not pr.get("existing"):
        await gitea.comment(int(inp.source_ref[1:]), f"The agent opened {pr['url']} for this issue.")
    return PrInfo(pr["number"], pr["url"], pr["head_sha"], branch)


@activity.defn
async def build_image(inp: ImageInput) -> str:
    return await _heartbeating(delivery.build_image(inp.sha, inp.repo, inp.tag), detail=f"building {inp.repo}:{inp.tag}")


@activity.defn
async def deploy_pr_env(inp: PrEnvInput) -> PrEnv:
    out = await delivery.deploy_pr_env(inp.work_id, inp.pr_number, inp.image_tag, heartbeat=activity.heartbeat)
    return PrEnv(out["app"], out["namespace"], out["url"])


@activity.defn
async def teardown_pr_env(env: PrEnv) -> None:
    await delivery.teardown_pr_env(env.app, env.namespace)


@activity.defn
async def comment_pr(inp: CommentInput) -> None:
    await gitea.comment(inp.pr_number, inp.body)


@activity.defn
async def pr_state(number: int) -> dict:
    return await gitea.pr_state(number)


@activity.defn
async def merge_pr(number: int) -> dict:
    return await gitea.merge_pr(number)


@activity.defn
async def close_pr(number: int) -> None:
    await gitea.close_pr(number)


@activity.defn
async def release_image(inp: ReleaseInput) -> dict:
    return await _heartbeating(delivery.release(inp.merge_sha, inp.kind), detail=f"release of {inp.merge_sha[:8]}")


@activity.defn
async def wait_for_stage(inp: StageInput) -> dict:
    """Poll Kargo until this version is verified in the stage (or fails there). Heartbeats every poll."""
    while True:
        st = await delivery.stage_status(inp.stage, inp.version)
        activity.heartbeat(st)
        if st["state"] == "verified":
            return {**st, "verified": True, "url": delivery.stage_url(inp.stage)}
        if st["state"] == "failed":
            return {**st, "verified": False}
        await asyncio.sleep(10)


@activity.defn
async def promote_stage(inp: StageInput) -> dict:
    return await delivery.promote(inp.stage, inp.version)


ACTIVITIES = [run_agent, publish_pr, build_image, deploy_pr_env, teardown_pr_env, comment_pr, pr_state,
              merge_pr, close_pr, release_image, wait_for_stage, promote_stage]
