#!/usr/bin/env python3
"""Call a pipeline MCP tool the way the agents would (for testing without kagent).

    .venv/bin/python scripts/mcp_call.py list [chat|gated|agent]
    .venv/bin/python scripts/mcp_call.py start_work title="..." kind=bug description="..."   # /mcp-chat
    .venv/bin/python scripts/mcp_call.py wait_for_work work_id=...
    .venv/bin/python scripts/mcp_call.py request_changes work_id=... feedback="..."
    .venv/bin/python scripts/mcp_call.py merge_pull_request work_id=...                     # /mcp-gated
    .venv/bin/python scripts/mcp_call.py promote work_id=... stage=staging
    .venv/bin/python scripts/mcp_call.py submit_revision work_id=... summary=... diff=@x.diff  # /mcp

`key=@file` reads the value from a file.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

BASE = os.environ.get("PIPELINE_URL", "http://localhost:8765")
ENDPOINTS = {"chat": "/mcp-chat", "gated": "/mcp-gated", "agent": "/mcp"}
TOOL_ENDPOINT = {"merge_pull_request": "gated", "promote": "gated", "submit_revision": "agent"}  # rest: chat


async def main(argv: list[str]) -> None:
    tool = argv[0] if argv else "list"
    args = {}
    if tool == "list":
        url = BASE + ENDPOINTS[argv[1] if len(argv) > 1 else "chat"]
    else:
        for a in argv[1:]:
            k, v = a.split("=", 1)
            args[k] = Path(v[1:]).read_text() if v.startswith("@") else v
        url = BASE + ENDPOINTS[TOOL_ENDPOINT.get(tool, "chat")]
    async with streamablehttp_client(url, timeout=30, sse_read_timeout=900) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            if tool == "list":
                tools = await s.list_tools()
                print(json.dumps([t.name for t in tools.tools]))
                return
            res = await s.call_tool(tool, args)
            for c in res.content:
                print(getattr(c, "text", c))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
