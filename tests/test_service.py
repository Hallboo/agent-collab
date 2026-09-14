from __future__ import annotations

import os
import subprocess
import time
from dataclasses import replace

import pytest

from agent_collab.delivery import DeliveryResult
from agent_collab.identity import Identity, process_start
from agent_collab.service import CollaborationService
from agent_collab.store import StoreError, agent_id


class RecordingDelivery:
    def __init__(self) -> None:
        self.messages = []

    def deliver(self, message):
        self.messages.append(message)
        return DeliveryResult(True, "recorded")


class FlakyDelivery:
    def __init__(self) -> None:
        self.attempts = 0

    def deliver(self, message):
        self.attempts += 1
        if self.attempts == 1:
            return DeliveryResult(False, "temporary failure")
        return DeliveryResult(True, "recorded")


def identity(name: str, session: str, host: str = "test-host") -> Identity:
    return Identity(
        name=name,
        role="test",
        cwd="/tmp",
        repo="test",
        host=host,
        client="test",
        session_id=session,
        pid=os.getpid(),
        process_start=process_start(os.getpid()),
    )


def alive_identity(name: str, session: str, pid: int) -> Identity:
    return Identity(
        name=name,
        role="test",
        cwd="/tmp",
        repo="test",
        host="test-host",
        client="test",
        session_id=session,
        pid=pid,
        process_start=process_start(pid),
    )


def wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "condition not reached before timeout"


def test_two_services_deliver_once(settings) -> None:
    sender_delivery = RecordingDelivery()
    receiver_delivery = RecordingDelivery()
    sender = CollaborationService(settings, identity("sender", "session-a"), sender_delivery)
    receiver = CollaborationService(settings, identity("receiver", "session-b"), receiver_delivery)
    sender.start()
    receiver.start()
    try:
        result = sender.send("receiver", "hello")
        assert result["notice"] == (
            f"[Agent Collab] → receiver@test-host queued {result['msg_id'][:8]}"
        )
        deadline = time.monotonic() + 3
        while not receiver_delivery.messages and time.monotonic() < deadline:
            time.sleep(0.05)
        assert len(receiver_delivery.messages) == 1
        assert receiver_delivery.messages[0]["msg_id"] == result["msg_id"]
        time.sleep(0.4)
        assert len(receiver_delivery.messages) == 1
    finally:
        receiver.stop()
        sender.stop()


def test_transient_delivery_failure_is_retried(settings, monkeypatch) -> None:
    monkeypatch.setattr("agent_collab.service.DELIVERY_RETRY_SECONDS", 0.01)
    fast_settings = replace(settings, poll_seconds=0.01)
    delivery = FlakyDelivery()
    sender = CollaborationService(
        fast_settings,
        identity("sender", "session-a"),
        RecordingDelivery(),
    )
    receiver = CollaborationService(
        fast_settings,
        identity("receiver", "session-b"),
        delivery,
    )
    sender.start()
    receiver.start()
    try:
        result = sender.send("receiver", "hello")
        deadline = time.monotonic() + 3
        messages = []
        while time.monotonic() < deadline:
            messages = receiver.inbox(message_id=result["msg_id"])["messages"]
            if messages and messages[0]["status"] == "delivered":
                break
            time.sleep(0.01)
        assert delivery.attempts == 2
        assert [message["status"] for message in messages] == ["delivered"]
    finally:
        receiver.stop()
        sender.stop()


def test_sender_receives_final_status_notice(settings) -> None:
    fast_settings = replace(settings, poll_seconds=0.01, heartbeat_seconds=0.05)
    sender_delivery = RecordingDelivery()
    receiver_delivery = RecordingDelivery()
    sender = CollaborationService(
        fast_settings,
        identity("sender", "session-a"),
        sender_delivery,
    )
    receiver = CollaborationService(
        fast_settings,
        identity("receiver", "session-b"),
        receiver_delivery,
    )
    sender.start()
    receiver.start()
    try:
        result = sender.send("receiver", "hello")
        deadline = time.monotonic() + 3
        notices = []
        while time.monotonic() < deadline:
            notices = [m for m in sender_delivery.messages if m.get("notice")]
            if notices:
                break
            time.sleep(0.01)
        assert [m["notice"] for m in notices] == [
            f"[Agent Collab] → receiver@test-host delivered {result['msg_id'][:8]}"
        ]
        assert len(receiver_delivery.messages) == 1
    finally:
        receiver.stop()
        sender.stop()


def test_set_identity_keeps_registration(settings) -> None:
    service = CollaborationService(settings, identity("before", "session-a"), RecordingDelivery())
    service.start()
    try:
        updated = service.set_identity(role="infra", name="after")
        assert updated["name"] == "after"
        assert [agent["name"] for agent in service.store.list_agents()] == ["after"]
    finally:
        service.stop()


def test_two_hosts_share_directory_and_deliver(settings) -> None:
    fast_settings = replace(
        settings,
        poll_seconds=0.01,
        heartbeat_seconds=0.05,
        remote_stale_seconds=0.2,
    )
    sender_delivery = RecordingDelivery()
    receiver_delivery = RecordingDelivery()
    sender = CollaborationService(
        fast_settings,
        identity("sender", "session-a", host="host-a"),
        sender_delivery,
    )
    receiver = CollaborationService(
        fast_settings,
        identity("receiver", "session-b", host="host-b"),
        receiver_delivery,
    )
    sender.start()
    receiver.start()
    try:
        assert {agent["host"] for agent in sender.store.list_agents()} == {"host-a", "host-b"}
        assert {agent["host"] for agent in receiver.store.list_agents()} == {"host-a", "host-b"}
        result = sender.send("receiver@host-b", "hello")
        deadline = time.monotonic() + 3
        while not receiver_delivery.messages and time.monotonic() < deadline:
            time.sleep(0.01)
        assert [message["msg_id"] for message in receiver_delivery.messages] == [result["msg_id"]]
    finally:
        receiver.stop()
        sender.stop()


def test_watcher_refreshes_remote_heartbeat(settings) -> None:
    fast_settings = replace(settings, poll_seconds=0.01, heartbeat_seconds=0.03)
    service = CollaborationService(
        fast_settings,
        identity("receiver", "session-b", host="host-b"),
        RecordingDelivery(),
    )
    service.start()
    try:
        path = service.store._agent_path(service.identity)
        first_seen = service.store._read_payload(path)["seen"]
        deadline = time.monotonic() + 2
        current_seen = first_seen
        while current_seen == first_seen and time.monotonic() < deadline:
            time.sleep(0.02)
            current_seen = service.store._read_payload(path)["seen"]
        assert current_seen > first_seen
    finally:
        service.stop()


def test_find_coagents_returns_table_and_marks_local_host(settings) -> None:
    local_delivery = RecordingDelivery()
    remote_delivery = RecordingDelivery()
    service = CollaborationService(
        settings,
        replace(identity("local", "session-a", host="host-a"), model="gpt-local"),
        local_delivery,
    )
    remote = CollaborationService(
        settings,
        replace(identity("remote", "session-b", host="host-b"), model="gpt-remote"),
        remote_delivery,
    )
    service.start()
    remote.start()
    try:
        table = service.find_coagents()
        assert "| Agent ID | Repo | Host Name | Client | Model | PID |" in table
        assert (
            f"| test_test_host_a_{os.getpid()} | test | host-a（本机·自己） | test | "
            f"gpt-local | {os.getpid()} |"
        ) in table
        assert (
            f"| test_test_host_b_{os.getpid()} | test | host-b | test | "
            f"gpt-remote | {os.getpid()} |"
        ) in table
        assert "| local |" not in table
        assert "| remote |" not in table
        remote_table = remote.find_coagents()
        assert f"test_test_host_a_{os.getpid()}" in remote_table
        assert f"test_test_host_b_{os.getpid()}" in remote_table
        assert "host-b（本机·自己）" in remote_table
        assert "host-a（本机）" not in remote_table
        assert local_delivery.messages == []
        assert remote_delivery.messages == []
        assert list(service.store.spool_dir.rglob("*.json")) == []
    finally:
        remote.stop()
        service.stop()


def test_room_flow_create_join_send_leave(settings) -> None:
    fast_settings = replace(settings, poll_seconds=0.01, heartbeat_seconds=0.05)
    sleeper = subprocess.Popen(["sleep", "60"])
    a = CollaborationService(fast_settings, alive_identity("alpha", "session-a", os.getpid()), RecordingDelivery())
    b = CollaborationService(fast_settings, alive_identity("beta", "session-b", os.getppid()), RecordingDelivery())
    c = CollaborationService(fast_settings, alive_identity("gamma", "session-c", 1), RecordingDelivery())
    d = CollaborationService(fast_settings, alive_identity("delta", "session-d", sleeper.pid), RecordingDelivery())
    a.start()
    b.start()
    c.start()
    d.start()
    try:
        created = a.create_room(
            members=[
                agent_id("test", "test", "test-host", os.getppid()),
                agent_id("test", "test", "test-host", 1),
            ]
        )
        room_id = created["room_id"]
        assert f"#{room_id}" in created["charter"]
        assert created["log"].endswith(f"{created['long_id']}.jsonl")
        assert created["invited"] == ["beta@test-host", "gamma@test-host"]

        wait_for(lambda: len(svc_delivery_messages(b)) >= 1)
        invite_b = svc_delivery_messages(b)[0]
        assert room_id in str(invite_b.get("text", ""))
        wait_for(lambda: len(svc_delivery_messages(c)) >= 1)

        joined = b.join_room(room_id)
        assert joined["already"] is False
        assert f"#{room_id}" in joined["charter"]
        assert joined["log"].endswith(f"{joined['long_id']}.jsonl")
        # members = 加入后全量,自己靠 you 标记认出(条目名是入房时快照)
        you_rows = [member for member in joined["members"] if member["you"]]
        assert len(you_rows) == 1 and you_rows[0]["name"] == "beta"
        assert sum(1 for member in joined["members"] if not member["you"]) == 1
        c.join_room(room_id)

        join_notes = lambda svc: [
            m for m in svc_delivery_messages(svc) if m.get("room") == room_id and "加入" in str(m.get("text", ""))
        ]
        wait_for(lambda: len(join_notes(a)) == 2)
        table = a.find_coagents()
        assert f"| #{room_id} |" in table
        assert "alpha" in table and "beta" in table and "gamma" in table

        result = a.send(f"#{room_id}", "hello room")
        assert result["to"] == f"#{room_id}"
        assert result["notice"] == (
            f"[Agent Collab] → #{room_id} (2) queued {result['msg_id'][:8]}"
        )

        chat_to = lambda svc: [
            m for m in svc_delivery_messages(svc) if m.get("room") == room_id and m.get("text") == "hello room"
        ]
        wait_for(lambda: len(chat_to(b)) == 1)
        wait_for(lambda: len(chat_to(c)) == 1)

        final = lambda: [
            m["notice"]
            for m in svc_delivery_messages(a)
            if m.get("notice") and m["notice"].startswith(f"[Agent Collab] → #{room_id} delivered")
        ]
        wait_for(lambda: len(final()) == 1)
        assert final()[0] == f"[Agent Collab] → #{room_id} delivered 2 {result['msg_id'][:8]}"

        with pytest.raises(StoreError, match="not a member"):
            d.send(f"#{room_id}", "intruder")
        with pytest.raises(StoreError, match="room not found"):
            d.send("#zzzz", "hello")

        c.leave_room()
        leave_notes = lambda svc: [
            m for m in svc_delivery_messages(svc) if m.get("room") == room_id and "离开" in str(m.get("text", ""))
        ]
        wait_for(lambda: len(leave_notes(a)) == 1 and len(leave_notes(b)) == 1)

        second = c.create_room()
        assert second["room_id"] != room_id
        with pytest.raises(StoreError, match="one room per agent"):
            b.join_room(second["room_id"])
    finally:
        d.stop()
        c.stop()
        b.stop()
        a.stop()
        sleeper.terminate()
        sleeper.wait()


def svc_delivery_messages(service):
    return service.delivery.messages


def test_sidecar_unregisters_when_client_process_dies(settings) -> None:
    fast_settings = replace(settings, poll_seconds=0.01, heartbeat_seconds=0.05)
    dead = replace(identity("dead-client", "session-b"), pid=999_999, process_start="0")
    service = CollaborationService(fast_settings, dead, RecordingDelivery())
    service.start()
    try:
        path = service.store._agent_path(service.identity)
        assert path.exists()
        deadline = time.monotonic() + 3
        while path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not path.exists()
        assert not service._thread.is_alive()
    finally:
        service.stop()
