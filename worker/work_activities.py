"""Activities for WorkItemWorkflow (worker/work.py). Kept out of the workflow module because they
touch git, docker, kubectl, OpenShell, kagent and the filesystem."""
from __future__ import annotations

import asyncio

from temporalio import activity

from worker import apps, delivery, gitea
from worker.work import (
    AppEnv, StartAppInput, VerifyInput, VerifyResult, AgentTurnInput, AgentTurnResult, CommentInput, ImageInput, PrEnv, PrEnvInput, PrInfo, PublishInput,
    ReleaseInput,
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


# ---- verification (fresh OpenShell sandbox per revision) ------------------------

@activity.defn
async def start_app_env(inp: StartAppInput) -> AppEnv:
    return await apps.APPS[inp.runtime].start(inp.change_id, inp.n)


@activity.defn
async def verify_change(inp: VerifyInput) -> VerifyResult:
    base = await gitea.base("main")
    problem = await gitea.check_applies(base.sha, inp.diff) if inp.diff.strip() else "empty diff"
    if problem:
        return VerifyResult(passed=False, base_sha=base.sha, error=f"diff does not apply to main@{base.sha[:8]}: {problem}")
    res = await apps.verify(inp.env, base.tar_b64, inp.diff)
    return VerifyResult(
        passed=bool(res.get("passed")), base_sha=base.sha, output=res.get("output", "")[-6000:],
        agent_tests=res.get("agent_tests", {}), base_tests=res.get("base_tests", {}),
        tests_touched=bool(res.get("tests_touched")), weakened=res.get("weakened", []),
        touched=res.get("touched", []),
    )


@activity.defn
async def stop_app_env(env: AppEnv) -> None:
    await apps.APPS[env.runtime].stop(env)


# ---- the agent, PRs, previews, releases ---------------------------------------

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



ACTIVITIES = [start_app_env, verify_change, stop_app_env, run_agent, publish_pr, build_image, deploy_pr_env, teardown_pr_env, comment_pr, pr_state,
              merge_pr, close_pr, release_image]
