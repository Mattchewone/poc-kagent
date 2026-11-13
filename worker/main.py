"""Worker process. Runs on the host; reaches the runtimes via port-forwards and kube contexts."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from temporalio.client import Client  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

from worker import activities, change, change_activities, work, work_activities  # noqa: E402
from worker.workflow import TASK_QUEUE, FeatureWorkflow  # noqa: E402


async def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    client = await Client.connect(os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"))
    worker = Worker(client, task_queue=TASK_QUEUE, workflows=[FeatureWorkflow, change.ChangeWorkflow, work.WorkItemWorkflow],
                    activities=activities.ALL + change_activities.ACTIVITIES + work_activities.ACTIVITIES)
    logging.getLogger("poc").info("worker up on task queue %s (pid %s)", TASK_QUEUE, os.getpid())
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
