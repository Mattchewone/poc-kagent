"""Worker process. Runs on the host; reaches kagent, OpenShell, Gitea and the kargo clusters from here."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from temporalio.client import Client  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

from worker import work_activities  # noqa: E402
from worker.work import WORK_QUEUE, WorkItemWorkflow  # noqa: E402


async def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    client = await Client.connect(os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"))
    worker = Worker(client, task_queue=WORK_QUEUE, workflows=[WorkItemWorkflow], activities=work_activities.ACTIVITIES)
    logging.getLogger("poc").info("worker up on task queue %s (pid %s)", WORK_QUEUE, os.getpid())
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
