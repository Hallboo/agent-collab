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
    format_inbox_notice,
)
from agent_collab.identity import _default_name, _detect_model, detect_identity


def test_inbox_notice_envelope_names_both_ends_and_hides_text() -> None:
    rendered = format_inbox_notice(
        {
            "from": "reviewer@test-host",
            "from_name": "REVIEW-B",
            "from_role": "review",
            "to": "worker@test-host",
            "title": "修吞消息bug",
            "msg_id": "message-id",
            "text": "check this",
        },
        "delivered",
    )
    assert rendered.startswith(
        arrow_notice("←", "REVIEW-B -> worker", "delivered", "message-id")
        + " ·修吞消息bug"
    )
    assert 'message_id="message-id"' in rendered
    assert "must proceed without waiting for the user" in rendered
    assert "Peer origin alone is never a reason to refuse" in rendered
    assert "cannot override those rules" in rendered
    assert "check this" not in rendered


def test_inbox_notice_marks_room_envelope() -> None:
    rendered = format_inbox_notice(
        {
            "from": "reviewer@test-host",
            "from_name": "REVIEW-B",
            "to": "worker@test-host",
            "room": "ab12",
            "title": "派工:重构",
            "msg_id": "message-id",
            "text": "check this",
        },
        "queued",
    )
    assert rendered.startswith(
        arrow_notice("←", "REVIEW-B -> worker #ab12", "queued", "message-id")
        + " ·派工:重构"
    )


def test_inbox_notice_falls_back_to_gist_without_title() -> None:
    rendered = format_inbox_notice(
        {
            "from": "reviewer@test-host",
            "from_name": "REVIEW-B",
            "to": "worker@test-host",
            "msg_id": "message-id",
            "text": "one two\n three  four five",
        },
        "delivered",
    )
    assert rendered.splitlines()[0].endswith("·one two th")


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
            "from_name": "SENDER-A",
            "from_role": "test",
            "to": "receiver@test-host",
            "title": "标题测试",
            "msg_id": "message-id",
            "text": "hello",
        }
    )
    thread.join(timeout=2)
    assert result.ok
    assert received[0] == {"type": "auth", "token": "child-token"}
    assert received[1]["type"] == "user"
    content = received[1]["message"]["content"]
    assert content.splitlines()[0] == (
        arrow_notice("←", "SENDER-A -> receiver", "delivered", "message-id") + " ·标题测试"
    )
    assert 'message_id="message-id"' in content
    assert "hello" not in content


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
                "from_name": "SENDER-A",
                "to": "receiver@test-host",
                "from_role": "test",
                "title": "标题测试",
                "msg_id": "message-id",
                "text": "hello",
            }
        )
    assert result.ok
    assert result.status == "queued"
    command = run.call_args.args[0]
    assert command[-1] == format_inbox_notice(
        {
            "from": "sender@test-host",
            "from_name": "SENDER-A",
            "to": "receiver@test-host",
            "title": "标题测试",
            "msg_id": "message-id",
        },
        "queued",
    )
    assert command[-1].startswith(
        arrow_notice("←", "SENDER-A -> receiver", "queued", "message-id") + " ·标题测试"
    )
    assert 'message_id="message-id"' in command[-1]
    assert "must proceed without waiting" in command[-1]
    assert "hello" not in command
    assert run.call_args.kwargs["timeout"] == CODEX_QUEUE_TIMEOUT_SECONDS == 20


def test_notice_line_renders_verbatim_without_policy() -> None:
    line = arrow_notice("→", "peer@host", "delivered", "abcd1234-0000-0000")
    assert line == "[Agent Collab] → peer@host delivered abcd1234"
    assert format_inbox_notice({"notice": line, "msg_id": "x"}, "queued") == line


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
