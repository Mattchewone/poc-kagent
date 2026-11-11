"""FeatureWorkflow: one workflow, any runtime. Runtime choice is data, not code."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy, SearchAttributeKey

with workflow.unsafe.imports_passed_through():
    from worker.activities import (
        ImplementInput,
        ImplementResult,
        RunTestsInput,
        SavePatchInput,
        StartEnvInput,
        TestResult,
        implement,
        pause_env,
        run_tests,
        save_patch,
        start_env,
        stop_env,
    )
    from worker.runtimes import Handle

TASK_QUEUE = "mock-poc"
MAX_ATTEMPTS = 3
SA_RUNTIME = SearchAttributeKey.for_keyword("Runtime")
SA_SCENARIO = SearchAttributeKey.for_keyword("Scenario")


@dataclass
class RunRequest:
    runtime: str  # openshell | substrate | local — where the agent lives
    scenario: str  # happy | never | cheat | egress | slow
    run_id: str
    task: str = "Fix add() in calc so the tests pass."
    sleep_seconds: float | None = None  # override for the slow scenario
    test_runtime: str | None = None  # where tests run; defaults to `runtime` (set it to combine runtimes)


# Timeouts and retries, per the plan's activity table.
_OPTS = {
    "start_env": dict(start_to_close_timeout=timedelta(minutes=5),
                      retry_policy=RetryPolicy(initial_interval=timedelta(seconds=2), maximum_attempts=5)),
    "implement": dict(start_to_close_timeout=timedelta(minutes=10), heartbeat_timeout=timedelta(seconds=30),
                      retry_policy=RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=3)),
    "run_tests": dict(start_to_close_timeout=timedelta(minutes=10),
                      retry_policy=RetryPolicy(initial_interval=timedelta(seconds=2), maximum_attempts=3)),
    "pause_env": dict(start_to_close_timeout=timedelta(seconds=60), retry_policy=RetryPolicy(maximum_attempts=3)),
    "save_patch": dict(start_to_close_timeout=timedelta(seconds=30)),
    "stop_env": dict(start_to_close_timeout=timedelta(minutes=2),
                     retry_policy=RetryPolicy(initial_interval=timedelta(seconds=2), maximum_attempts=10)),
}


async def act(fn, arg, result_type=None):
    """Shorthand for workflow.execute_activity with the per-activity options above."""
    return await workflow.execute_activity(fn, arg, result_type=result_type, **_OPTS[fn.__name__])


@dataclass
class Review:
    """A human decision on a patch that passed the tests.

    ok=True approves it. ok=False with a comment requests changes: the comment goes back to the
    agent as feedback for another attempt. ok=False with no comment rejects the run outright.
    """
    ok: bool
    comment: str = ""


AWAITING_REVIEW = "awaiting review"


@workflow.defn
class FeatureWorkflow:
    def __init__(self) -> None:
        self.review: Review | None = None
        self.state: dict = {"phase": "starting", "attempt": 0, "history": []}

    @workflow.update
    async def review_patch(self, decision: Review) -> str:
        self.review = decision
        return "ok"

    @review_patch.validator
    def _validate_review(self, decision: Review) -> None:
        if self.state["phase"] != AWAITING_REVIEW or self.review is not None:
            raise ValueError(f"not awaiting review (phase={self.state['phase']})")

    @workflow.query
    def status(self) -> dict:
        return self.state

    @workflow.run
    async def run(self, req: RunRequest) -> str:
        test_runtime = req.test_runtime or req.runtime
        self.state.update(runtime=req.runtime, test_runtime=test_runtime, scenario=req.scenario)
        agent: Handle = await act(start_env, StartEnvInput(req.runtime, "agent", req.run_id), Handle)
        self.state["agent_env"] = agent.name
        try:
            feedback = ""
            for n in range(1, MAX_ATTEMPTS + 1):
                self.state.update(phase="implement", attempt=n)
                out: ImplementResult = await act(
                    implement,
                    ImplementInput(agent, req.task, feedback, req.scenario, req.sleep_seconds),
                    ImplementResult,
                )
                self.state["phase"] = "test"
                res: TestResult = await act(run_tests, RunTestsInput(test_runtime, req.run_id, n, out.diff), TestResult)
                entry = {
                    "attempt": n, "agent_counter": out.attempt, "notes": out.notes,
                    "passed": res.passed, "tests_touched": res.tests_touched,
                    "egress": out.egress, "test_env": res.env,
                }
                self.state["history"].append(entry)
                if not res.passed:
                    feedback = res.output
                    await act(pause_env, agent)
                    continue

                # Tests pass: a human reviews the patch while the agent sits paused.
                self.state["patch"] = await act(save_patch, SavePatchInput(req.run_id, out.diff), str)
                self.review = None
                self.state["phase"] = AWAITING_REVIEW
                await act(pause_env, agent)
                await workflow.wait_condition(lambda: self.review is not None)
                decision = self.review
                if decision.ok:
                    entry["review"] = "approved"
                    self.state["result"] = "approved"
                    return "approved"
                if not decision.comment:
                    entry["review"] = "rejected"
                    self.state["result"] = "rejected"
                    return "rejected"
                entry["review"] = f"changes requested: {decision.comment}"
                feedback = f"Reviewer requested changes:\n{decision.comment}"
            self.state["result"] = f"gave up after {MAX_ATTEMPTS} attempts"
            return self.state["result"]
        finally:
            # Also runs on cancel: the env never outlives the workflow.
            self.state["phase"] = "cleanup"
            await act(stop_env, agent)
            self.state["phase"] = "done"
