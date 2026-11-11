"""Start a run, or review its patch.

    python worker/start.py --runtime openshell --scenario happy [--approve] [--wait]
    python worker/start.py --runtime substrate --test-runtime openshell --scenario happy   # combined
    python worker/start.py approve <run id>
    python worker/start.py reject  <run id> ["what to change"]   # with a comment: agent revises
    python worker/start.py status  <run id>

approve/reject wait until the run's tests pass and it is awaiting review. If the run finishes
first (gave up, cancelled), they print that and exit.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from temporalio.client import Client, WorkflowExecutionStatus, WorkflowHandle  # noqa: E402
from temporalio.common import TypedSearchAttributes, SearchAttributePair  # noqa: E402

from worker.workflow import (  # noqa: E402
    AWAITING_REVIEW, SA_RUNTIME, SA_SCENARIO, TASK_QUEUE, FeatureWorkflow, Review, RunRequest,
)

SCENARIOS = ["happy", "never", "cheat", "egress", "slow"]


async def _client() -> Client:
    return await Client.connect(os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"))


async def start(a: argparse.Namespace) -> None:
    client = await _client()
    combined = a.test_runtime and a.test_runtime != a.runtime
    prefix = f"{a.runtime[0]}{a.test_runtime[0]}" if combined else a.runtime[:2]
    run_id = a.id or f"{prefix}-{a.scenario}-{uuid.uuid4().hex[:6]}"
    req = RunRequest(runtime=a.runtime, scenario=a.scenario, run_id=run_id, sleep_seconds=a.sleep_seconds,
                     test_runtime=a.test_runtime)
    sa_runtime = f"{a.runtime}+{a.test_runtime}" if combined else a.runtime
    kwargs = {}
    if not a.no_search_attributes:
        kwargs["search_attributes"] = TypedSearchAttributes([
            SearchAttributePair(SA_RUNTIME, sa_runtime), SearchAttributePair(SA_SCENARIO, a.scenario),
        ])
    h = await client.start_workflow(FeatureWorkflow.run, req, id=run_id, task_queue=TASK_QUEUE, **kwargs)
    print(run_id)
    if a.approve or a.reject:
        print(await _review_when_ready(h, Review(ok=not a.reject, comment=a.comment or "")), file=sys.stderr)
    if a.wait:
        print(await h.result(), file=sys.stderr)


async def _review_when_ready(h: WorkflowHandle, decision: Review, poll: float = 1.0) -> str:
    """Wait for the run to reach review, then send the decision. Tolerates a worker outage."""
    while True:
        try:
            if (await h.query(FeatureWorkflow.status)).get("phase") == AWAITING_REVIEW:
                return await h.execute_update(FeatureWorkflow.review_patch, decision)
        except Exception:  # worker down or busy; the describe below tells us if the run ended
            pass
        desc = await h.describe()
        if desc.status != WorkflowExecutionStatus.RUNNING:
            return f"run finished before review ({desc.status.name})"
        await asyncio.sleep(poll)


async def decide(run_id: str, decision: Review) -> None:
    h = (await _client()).get_workflow_handle(run_id)
    print(await _review_when_ready(h, decision))


async def status(run_id: str) -> None:
    h = (await _client()).get_workflow_handle(run_id)
    print(json.dumps(await h.query(FeatureWorkflow.status), indent=2))


def main() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] in ("approve", "reject", "status"):
        cmd, run_id = sys.argv[1], sys.argv[2]
        comment = " ".join(sys.argv[3:]).strip()
        asyncio.run(status(run_id) if cmd == "status" else decide(run_id, Review(cmd == "approve", comment)))
        return
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--runtime", required=True, choices=["openshell", "substrate", "local"])
    p.add_argument("--test-runtime", choices=["openshell", "substrate", "local"],
                   help="run tests on a different runtime than the agent (e.g. agent on substrate, tests on openshell)")
    p.add_argument("--scenario", required=True, choices=SCENARIOS)
    p.add_argument("--id", help="workflow id (= run id); generated if omitted")
    p.add_argument("--approve", action="store_true", help="approve the patch as soon as it is ready for review")
    p.add_argument("--reject", action="store_true", help="reject the patch as soon as it is ready for review")
    p.add_argument("--comment", help="with --reject: send this back to the agent instead of ending the run")
    p.add_argument("--wait", action="store_true", help="block until the workflow returns")
    p.add_argument("--sleep-seconds", type=float, default=None, help="override the slow scenario's 60 s")
    p.add_argument("--no-search-attributes", action="store_true",
                   help="skip Runtime/Scenario search attributes (if the server doesn't have them)")
    asyncio.run(start(p.parse_args()))


if __name__ == "__main__":
    main()
