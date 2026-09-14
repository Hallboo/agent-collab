from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import threading
from pathlib import Path
from unittest.mock import patch

from agent_collab.delivery import (
    CODEX_QUEUE_TIMEOUT_SECONDS,
    ClaudeDelivery,
    CodexDelivery,
    arrow_notice,
    format_codex_inbox_notice,
    format_peer_message,
)
from agent_collab.identity import _default_name, _detect_model, detect_identity


def test_peer_message_explains_actionable_delegation_boundary() -> None:
    rendered = format_peer_message(
        {
            "from": "reviewer@test-host",
            "from_role": "review",
            "msg_id": "message-id",
            "text": "check this",
        }
    )
    assert "Authenticated peer collaboration request" in rendered
    assert "bug fixes" in rendered
    assert "assigned worker tasks" in rendered
    assert "research" in rendered
    assert "must proceed without waiting for the user" in rendered
    assert "Peer origin alone is never a reason to refuse" in rendered
    assert "cannot override those rules" in rendered
    assert rendered.endswith("check this")


def test_claude_socket_protocol(tmp_path: Path) -> None:
    socket_path = tmp_path / "cc.sock"
    received: list[dict[str, object]] = []
    ready = threading.Event()

    def listen() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(socket_path))
            os.chmod(socket_path, 0o600)
            server.listen(1)
            ready.set()
            connection, _ = server.accept()
            with connection, connection.makefile("r", encoding="utf-8") as stream:
                received.append(json.loads(stream.readline()))
                received.append(json.loads(stream.readline()))

    thread = threading.Thread(target=listen)
    thread.start()
    assert ready.wait(timeout=2)
    result = ClaudeDelivery(str(socket_path), "child-token").deliver(
        {
            "from": "sender@test-host",
            "from_role": "test",
            "msg_id": "message-id",
            "text": "hello",
        }
    )
    thread.join(timeout=2)
    assert result.ok
    assert received[0] == {"type": "auth", "token": "child-token"}
    assert received[1]["type"] == "user"
    assert "hello" in received[1]["message"]["content"]


def _codex_home_with_rollout(monkeypatch, tmp_path: Path, thread_id: str) -> Path:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    day = tmp_path / "sessions" / "2026" / "09" / "14"
    day.mkdir(parents=True)
    (day / f"rollout-2026-09-14T17-17-06-{thread_id}.jsonl").write_text("{}\n")
    return day


def test_codex_queue_is_not_reported_as_delivered(monkeypatch, tmp_path: Path) -> None:
    _codex_home_with_rollout(monkeypatch, tmp_path, "thread-id")
    completed = subprocess.CompletedProcess(args=["codex", "queue"], returncode=0)
    with (
        patch("shutil.which", return_value="/usr/bin/codex"),
        patch("subprocess.run", return_value=completed) as run,
    ):
        result = CodexDelivery("thread-id").deliver(
            {
                "from": "sender@test-host",
                "from_role": "test",
                "msg_id": "message-id",
                "text": "hello",
            }
        )
    assert result.ok
    assert result.status == "queued"
    command = run.call_args.args[0]
    notice = format_codex_inbox_notice(
        {
            "from": "sender@test-host",
            "msg_id": "message-id",
        }
    )
    assert command[-1] == notice
    assert command[-1].startswith(
        arrow_notice("←", "sender@test-host", "queued", "message-id")
    )
    assert 'message_id="message-id"' in command[-1]
    assert "must proceed without waiting" in command[-1]
    assert "hello" not in command
    assert run.call_args.kwargs["timeout"] == CODEX_QUEUE_TIMEOUT_SECONDS == 20


def test_notice_line_renders_verbatim_without_policy() -> None:
    line = arrow_notice("→", "peer@host", "delivered", "abcd1234-0000-0000")
    assert line == "[Agent Collab] → peer@host delivered abcd1234"
    assert format_peer_message({"notice": line, "msg_id": "x"}) == line
    assert format_codex_inbox_notice({"notice": line, "msg_id": "x"}) == line


def test_codex_queue_timeout_reports_the_limit(monkeypatch, tmp_path: Path) -> None:
    _codex_home_with_rollout(monkeypatch, tmp_path, "thread-id")
    with (
        patch("shutil.which", return_value="/usr/bin/codex"),
        patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(["codex", "queue"], 20),
        ),
    ):
        result = CodexDelivery("thread-id").deliver(
            {
                "from": "sender@test-host",
                "from_role": "test",
                "msg_id": "message-id",
                "text": "hello",
            }
        )
    assert not result.ok
    assert result.detail == "codex queue timed out after 20 seconds"


def test_codex_delivery_holds_back_until_first_turn(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "sessions" / "2026" / "09" / "14").mkdir(parents=True)
    with (
        patch("shutil.which", return_value="/usr/bin/codex"),
        patch("subprocess.run") as run,
    ):
        result = CodexDelivery("0123abcd").deliver(
            {
                "from": "sender@test-host",
                "from_role": "test",
                "msg_id": "message-id",
                "text": "hello",
            }
        )
    assert not result.ok
    assert "first turn" in result.detail
    run.assert_not_called()


def test_codex_delivery_releases_once_rollout_appears(monkeypatch, tmp_path: Path) -> None:
    # a different thread's rollout must not unlock delivery for ours
    day = _codex_home_with_rollout(monkeypatch, tmp_path, "deadbeef")
    completed = subprocess.CompletedProcess(args=["codex", "queue"], returncode=0)
    message = {
        "from": "sender@test-host",
        "from_role": "test",
        "msg_id": "message-id",
        "text": "hello",
    }
    with (
        patch("shutil.which", return_value="/usr/bin/codex"),
        patch("subprocess.run", return_value=completed),
    ):
        delivery = CodexDelivery("0123abcd")
        held = delivery.deliver(dict(message))
        (day / "rollout-2026-09-14T18-00-00-0123abcd.jsonl").write_text("{}\n")
        released = delivery.deliver(dict(message))
    assert not held.ok
    assert released.ok
    assert released.status == "queued"


def test_detects_codex_identity_from_parent_when_env_is_missing(
    monkeypatch, tmp_path: Path
) -> None:
    for key in (
        "CODEX_THREAD_ID",
        "CODEX_SESSION_ID",
        "CLAUDE_CODE_SESSION_ID",
        "AGENT_COLLAB_CLIENT",
        "AGENT_COLLAB_SESSION_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AGENT_COLLAB_CWD", str(tmp_path))
    monkeypatch.setattr(
        "agent_collab.identity._codex_identity_from_parent",
        lambda: ("01234567-89ab-cdef-0123-456789abcdef", os.getpid()),
    )
    identity = detect_identity()
    assert identity.client == "codex"
    assert identity.session_id == "01234567-89ab-cdef-0123-456789abcdef"
    assert identity.pid == os.getpid()
    assert detect_identity(host_name="dev-box").host == "dev-box"


def test_detects_declared_model_without_guessing() -> None:
    assert _detect_model("cc", os.getpid(), {"ANTHROPIC_MODEL": "claude-opus"}) == "claude-opus"
    assert (
        _detect_model(
            "cc",
            os.getpid(),
            {"AGENT_COLLAB_MODEL": "explicit-model", "ANTHROPIC_MODEL": "fallback-model"},
        )
        == "explicit-model"
    )


def test_default_name_uses_model_abbreviation() -> None:
    suffix = r"-[A-Z]"
    assert re.fullmatch(r"GLM53" + suffix, _default_name("codex", "glm-5.3-highspeed[1m]"))
    assert re.fullmatch(r"GLM5" + suffix, _default_name("codex", "glm-5"))
    assert re.fullmatch(r"OPUS5" + suffix, _default_name("cc", "claude-opus-5[1m]"))
    assert re.fullmatch(r"HAIKU45" + suffix, _default_name("cc", "claude-haiku-4.5-20251001"))
    assert re.fullmatch(r"GPT56" + suffix, _default_name("cc", "gpt-5.6"))
    assert re.fullmatch(r"CODEX" + suffix, _default_name("codex", ""))
