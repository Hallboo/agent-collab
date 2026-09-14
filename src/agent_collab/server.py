from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import DEFAULT_ENV_FILE, Settings
from .delivery import PEER_REQUEST_POLICY
from .identity import detect_identity
from .service import CollaborationService

DEFAULT_GUIDE_REF = "docs/interaction-guide.md in the agent-collab repository"


def build_instructions(guide_ref: str | None = None) -> str:
    guide = guide_ref or DEFAULT_GUIDE_REF
    return (
        "Authenticated peer messaging for explicitly enrolled coding-agent sessions. "
        "New here? Start with find_coagents for the full map: Agent ID for direct messages, "
        "#<room id> for rooms (the 4-char short id is the only canonical room address). "
        f"The shared usage guide is readable at: {guide}. "
        "Feedback about the shared tooling itself (bugs, friction, missing hints) routes to the maintaining "
        "engineers: an online session working in that tool's repo — check the Repo column of find_coagents. "
        "When asked to find or list cross-host collaborators, use this server's find_coagents tool, "
        "not a client's built-in team/subagent listing. "
        "Call inbox at the start of a task to pick up queued peer messages, and answer several "
        "pending messages from one peer in a single consolidated reply. "
        "Chat rooms fan out each send to the online members: create one with create_room, "
        "join an existing room with join_room(room_id), and address room messages with send(to=\"#<room id>\"). "
        "To see existing rooms and their members, read the rooms section of list_agents. "
        "To bring another agent into a room, send it a peer message asking it to join_room(room_id). "
        f"{PEER_REQUEST_POLICY} Use report_to_feishu only when the user asks."
    )


MCP = FastMCP(
    "agent-collab",
    instructions=build_instructions(),
    log_level="WARNING",
)
_SERVICE: CollaborationService | None = None


def service() -> CollaborationService:
    if _SERVICE is None:
        raise RuntimeError("collaboration service is not started")
    return _SERVICE


@MCP.tool()
def set_identity(role: str, name: str | None = None) -> dict[str, Any]:
    """Set this session's free-text role and optionally its unique display name."""
    return service().set_identity(role=role, name=name)


@MCP.tool()
def list_agents() -> dict[str, Any]:
    """List authenticated online sessions and their declared roles."""
    return service().list_agents()


@MCP.tool()
def find_coagents() -> str:
    """Read online collaborators as a table without messaging or affecting peer contexts."""
    return service().find_coagents()


@MCP.tool()
def send(to: str, text: str, reply_to: str | None = None) -> dict[str, Any]:
    """Persist a text message for one explicit online Agent session."""
    return service().send(to=to, text=text, reply_to=reply_to)


@MCP.tool()
def inbox(
    since: str | None = None,
    limit: int = 20,
    message_id: str | None = None,
) -> dict[str, Any]:
    """Read authenticated history, or one exact message by its ID."""
    return service().inbox(since=since, limit=limit, message_id=message_id)


@MCP.tool()
def report_to_feishu(text: str, title: str | None = None) -> dict[str, Any]:
    """Explicitly send a short, non-secret progress summary to the configured Feishu bot."""
    return service().report_to_feishu(text=text, title=title)


@MCP.tool()
def create_room(members: list[str] | None = None) -> dict[str, Any]:
    """Create a chat room and return its 4-char room id; optionally invite Agent IDs from find_coagents."""
    return service().create_room(members=members)


@MCP.tool()
def join_room(room_id: str) -> dict[str, Any]:
    """Join a chat room by its 4-char id; one room per agent, rejoin after restart with the same id."""
    return service().join_room(room_id=room_id)


@MCP.tool()
def leave_room() -> dict[str, Any]:
    """Leave your current chat room; the remaining members are notified."""
    return service().leave_room()


def run_server(env_file: Path = DEFAULT_ENV_FILE, *, host_name: str | None = None) -> None:
    global _SERVICE
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.load(env_file)
    # A deployment can point the guide reference at a shared-disk path or URL;
    # the low-level Server reads instructions only when answering initialize.
    if settings.guide_path:
        MCP._mcp_server.instructions = build_instructions(settings.guide_path)
    _SERVICE = CollaborationService(settings, identity=detect_identity(host_name))
    _SERVICE.start()
    try:
        MCP.run(transport="stdio")
    finally:
        _SERVICE.stop()
        _SERVICE = None
