"""Activities for ChangeWorkflow (worker/change.py). Kept out of the workflow module because they
touch git, docker, OpenShell and the filesystem."""
from __future__ import annotations

from temporalio import activity

from worker import apps, gitea
from worker.change import AppEnv, OpenPrInput, StartAppInput, VerifyInput, VerifyResult

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


@activity.defn
async def open_pr(inp: OpenPrInput) -> dict:
    v = inp.verify
    body = "\n".join([
        f"**{inp.kind.title()} request:** {inp.description}",
        "",
        "### Agent summary",
        inp.summary or "_none given_",
        "",
        "### Verification (fresh sandbox, no network, base tests restored)",
        f"- agent's tests: {v.get('agent_tests', {}).get('pass', '?')} passed",
        f"- base tests: {v.get('base_tests', {}).get('pass', '?')} passed",
        f"- files: {', '.join(v.get('touched', []))}",
        "",
        f"Opened by the agent pipeline after human approval. Change `{inp.change_id}`, base `{inp.base_sha[:8]}`.",
    ])
    return await gitea.open_pull_request(f"agent/{inp.change_id}", inp.base_sha, inp.diff, inp.title, body)


ACTIVITIES = [start_app_env, verify_change, stop_app_env, open_pr]
