#!/usr/bin/env python3
"""Call a pipeline MCP tool the way the agent would (for testing without kagent).

    .venv/bin/python scripts/mcp_call.py list
    .venv/bin/python scripts/mcp_call.py submit_change title="..." kind=bug description="..." summary="..." diff=@change.diff
    .venv/bin/python scripts/mcp_call.py get_change change_id=...
    .venv/bin/python scripts/mcp_call.py open_pull_request change_id=...      # goes to /mcp-gated

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
GATED = {"open_pull_request"}


async def main(argv: list[str]) -> None:
    tool = argv[0] if argv else "list"
    args = {}
    for a in argv[1:]:
        k, v = a.split("=", 1)
        args[k] = Path(v[1:]).read_text() if v.startswith("@") else v
    url = f"{BASE}/mcp-gated" if tool in GATED else f"{BASE}/mcp"
    async with streamablehttp_client(url, timeout=30, sse_read_timeout=900) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            if tool == "list":  # tools on /mcp (open_pull_request lives on /mcp-gated)
                tools = await s.list_tools()
                print(json.dumps([t.name for t in tools.tools]))
                return
            res = await s.call_tool(tool, args)
            for c in res.content:
                print(getattr(c, "text", c))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
