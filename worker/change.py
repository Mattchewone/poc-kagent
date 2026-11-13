"""ChangeWorkflow: what happens to code the agent submits, from verify to pull request.

The coding agent (kagent's Claude harness on Substrate) works on its own and calls MCP tools on
the pipeline server; each tool maps onto this workflow:

  submit_change      start the workflow, or Update `submit` with a new revision
  get_change         Query `status`
  open_pull_request  Update `approve`  (the MCP binding has requireApproval: a human clicks Approve
                                        in the kagent UI before this call is even made)
  abandon_change     Signal `abandon`

Per revision: resolve main on Gitea -> fresh verify env (OpenShell sandbox or local docker) ->
apply diff, run the agent's tests and the restored base tests -> on pass, that env stays up as the
preview. A new revision replaces the preview; approval opens a PR on Gitea and tears it down.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

CHANGE_QUEUE = "mock-poc"
MAX_REVISIONS = 6
IDLE_TIMEOUT = timedelta(hours=24)  # no new revision or decision for this long -> abandon, clean up


# ---- payloads (pure dataclasses: this module must stay importable in the workflow sandbox) ----

@dataclass
class AppEnv:
    """A verify/preview env: the supervisor, reachable from the host at `url`."""
    runtime: str
    name: str
    url: str
    meta: dict = field(default_factory=dict)


@dataclass
class Revision:
    diff: str
    summary: str = ""
    rev_id: str = ""  # set by the caller so it can wait for this revision's result


@dataclass
class ChangeRequest:
    change_id: str
    title: str
    kind: str  # feature | bug
    description: str
    first: Revision
    runtime: str = "openshell"  # where verify + preview run: openshell | local


@dataclass
class StartAppInput:
    runtime: str
    change_id: str
    n: int


@dataclass
class VerifyInput:
    env: AppEnv
    diff: str


@dataclass
class VerifyResult:
    passed: bool
    base_sha: str
    output: str = ""
    agent_tests: dict = field(default_factory=dict)
    base_tests: dict = field(default_factory=dict)
    tests_touched: bool = False
    weakened: list = field(default_factory=list)
    touched: list = field(default_factory=list)
    error: str = ""


@dataclass
class OpenPrInput:
    change_id: str
    title: str
    kind: str
    description: str
    base_sha: str
    diff: str
    summary: str
    verify: dict



_OPTS = {
    "start_app_env": dict(start_to_close_timeout=timedelta(minutes=5),
                          retry_policy=RetryPolicy(initial_interval=timedelta(seconds=2), maximum_attempts=4)),
    "verify_change": dict(start_to_close_timeout=timedelta(minutes=6),
                          retry_policy=RetryPolicy(initial_interval=timedelta(seconds=2), maximum_attempts=3)),
    "stop_app_env": dict(start_to_close_timeout=timedelta(minutes=2),
                         retry_policy=RetryPolicy(initial_interval=timedelta(seconds=2), maximum_attempts=10)),
    "open_pr": dict(start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=RetryPolicy(initial_interval=timedelta(seconds=2), maximum_attempts=5)),
}


async def act(name: str, arg, result_type=None):
    """Activities live in worker/change_activities.py and are called by name."""
    return await workflow.execute_activity(name, arg, result_type=result_type, **_OPTS[name])


# ---- workflow -------------------------------------------------------------

VERIFYING = "verifying"
FAILED = "verify failed"  # waiting for a new revision
PREVIEW = "preview ready"  # waiting for a new revision or approval
OPENING_PR = "opening pr"
DONE = "done"


@workflow.defn
class ChangeWorkflow:
    def __init__(self) -> None:
        self.pending: list[Revision] = []
        self.decision: str | None = None  # approve | abandon
        self.abandon_reason = ""
        self.pr_outcome: dict | None = None  # set by the main loop for the approve handler
        self.state: dict = {"phase": "starting", "revisions": []}

    # -- handlers --
    @workflow.update
    async def submit(self, rev: Revision) -> str:
        self.pending.append(rev)
        return rev.rev_id

    @submit.validator
    def _v_submit(self, rev: Revision) -> None:
        if self.state["phase"] in (OPENING_PR, DONE) or self.decision:
            raise ValueError(f"change is {self.state['phase']}; start a new change instead")
        if len(self.state["revisions"]) + len(self.pending) >= MAX_REVISIONS:
            raise ValueError(f"revision limit ({MAX_REVISIONS}) reached")

    @workflow.update
    async def approve(self) -> dict:
        self.pr_outcome = None
        self.decision = "approve"
        await workflow.wait_condition(lambda: self.pr_outcome is not None)
        if "error" in self.pr_outcome:
            raise ApplicationError(self.pr_outcome["error"], non_retryable=True)
        return self.pr_outcome["pr"]

    @approve.validator
    def _v_approve(self) -> None:
        if self.state["phase"] != PREVIEW or self.pending or self.decision:
            raise ValueError(f"nothing to approve: change is {self.state['phase']}"
                             + (" with a newer revision queued" if self.pending else ""))

    @workflow.signal
    def abandon(self, reason: str = "") -> None:
        self.decision = self.decision or "abandon"
        self.abandon_reason = reason

    @workflow.query
    def status(self) -> dict:
        return self.state

    # -- main loop --
    @workflow.run
    async def run(self, req: ChangeRequest) -> str:
        self.state.update(change_id=req.change_id, title=req.title, kind=req.kind,
                          description=req.description, runtime=req.runtime)
        self.pending.append(req.first)
        env: AppEnv | None = None
        try:
            while True:
                try:
                    await workflow.wait_condition(lambda: bool(self.pending) or self.decision is not None,
                                                  timeout=IDLE_TIMEOUT)
                except asyncio.TimeoutError:
                    self.decision, self.abandon_reason = "abandon", f"idle for {IDLE_TIMEOUT}"

                if self.decision == "abandon":
                    self.state["result"] = f"abandoned{': ' + self.abandon_reason if self.abandon_reason else ''}"
                    return self.state["result"]

                if self.decision == "approve":
                    self.state["phase"] = OPENING_PR
                    last = self.state["revisions"][-1]
                    try:
                        pr = await act("open_pr", OpenPrInput(
                            req.change_id, req.title, req.kind, req.description, last["base_sha"],
                            last["diff"], last["summary"], last["verify"]), dict)
                    except Exception as e:  # noqa: BLE001 - surfaced to the approver, who can approve again
                        self.state["phase"] = PREVIEW
                        self.decision = None
                        self.pr_outcome = {"error": f"could not open PR: {e}"}
                        continue
                    self.state["pr"] = pr
                    self.pr_outcome = {"pr": pr}
                    self.state["result"] = f"pr opened: {pr['url']}"
                    return self.state["result"]

                # Newest revision wins; anything submitted in between is superseded.
                rev, superseded = self.pending[-1], [r.rev_id for r in self.pending[:-1]]
                self.pending.clear()
                self.state.setdefault("superseded", []).extend(superseded)
                n = len(self.state["revisions"]) + 1
                entry = {"n": n, "rev_id": rev.rev_id, "summary": rev.summary, "diff": rev.diff, "submitted_at": workflow.now().isoformat()}
                self.state["revisions"].append(entry)
                self.state.pop("preview_url", None)
                self.state["phase"] = VERIFYING
                if env:
                    await act("stop_app_env", env)
                    env = None
                try:
                    env = await act("start_app_env", StartAppInput(req.runtime, req.change_id, n), AppEnv)
                    entry["env"] = env.name
                    res: VerifyResult = await act("verify_change", VerifyInput(env, rev.diff), VerifyResult)
                except ActivityError as e:
                    # Infrastructure trouble fails this revision, not the change: the agent can resubmit.
                    cause = e.cause.message if e.cause else str(e)
                    res = VerifyResult(passed=False, base_sha="", error=f"verification could not run: {cause}")
                entry.update(base_sha=res.base_sha, passed=res.passed, verify={
                    "agent_tests": res.agent_tests, "base_tests": res.base_tests, "tests_touched": res.tests_touched,
                    "weakened": res.weakened, "touched": res.touched, "error": res.error, "output": res.output,
                })
                if res.passed:
                    self.state["phase"] = PREVIEW
                    self.state["preview_url"] = env.url
                else:
                    self.state["phase"] = FAILED
                    if env:
                        await act("stop_app_env", env)
                    env = None
        finally:
            self.state["phase"] = "cleanup"
            if env:
                await act("stop_app_env", env)
            await workflow.wait_condition(workflow.all_handlers_finished)
            self.state["phase"] = DONE


def summarize(state: dict, verbose: bool = False) -> dict:
    """What the agent sees from a tool call: no diffs, trimmed test output."""
    revs = state.get("revisions", [])
    last = revs[-1] if revs else {}
    v = last.get("verify", {})
    out = {
        "change_id": state.get("change_id"),
        "phase": state.get("phase"),
        "revision": last.get("n"),
        "passed": last.get("passed"),
        "preview_url": state.get("preview_url"),
        "pr": state.get("pr"),
        "result": state.get("result"),
    }
    if v:
        out["verify"] = {k: v.get(k) for k in ("agent_tests", "base_tests", "tests_touched", "weakened", "touched", "error")}
        if last.get("passed") is False or verbose:
            out["verify"]["output_tail"] = (v.get("output") or "")[-2500:]
    return {k: val for k, val in out.items() if val not in (None, "", [], {})}

