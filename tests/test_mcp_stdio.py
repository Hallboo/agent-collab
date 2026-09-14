from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@pytest.mark.anyio
async def test_stdio_server_lists_tools(settings) -> None:
    env = dict(os.environ)
    for key in (
        "CODEX_THREAD_ID",
        "CODEX_SESSION_ID",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_PID",
        "CLAUDE_CODE_MESSAGING_SOCKET",
        "CLAUDE_CODE_MESSAGING_TOKEN",
    ):
        env.pop(key, None)
    env.update(
        {
            "AGENT_COLLAB_CLIENT": "test",
            "AGENT_COLLAB_SESSION_ID": "mcp-test-session",
            "AGENT_COLLAB_CLIENT_PID": str(os.getpid()),
            "AGENT_COLLAB_CWD": os.getcwd(),
        }
    )
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "agent_collab.cli",
            "serve",
            "--env-file",
            str(settings.env_file),
        ],
        env=env,
    )
    async with (
        stdio_client(parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        assert {tool.name for tool in tools.tools} == {
            "set_identity",
            "list_agents",
            "find_coagents",
            "send",
            "inbox",
            "report_to_feishu",
            "create_room",
            "join_room",
            "leave_room",
        }
        inbox_tool = next(tool for tool in tools.tools if tool.name == "inbox")
        assert "message_id" in inbox_tool.inputSchema["properties"]
        result = await session.call_tool("list_agents")
        payload = json.loads(result.content[0].text)
        assert payload["self"]["client"] == "test"
