from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from agent_collab.auth import open_sealed
from agent_collab.identity import Identity, process_start
from agent_collab.store import FileStore, StoreError, agent_id, agent_key, short_host


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


def test_registration_send_and_archive(settings) -> None:
    store = FileStore(settings, local_host="test-host")
    store.prepare()
    sender = identity("sender", "session-a")
    receiver = identity("receiver", "session-b")
    store.register(sender)
    store.register(receiver)

    message = store.send(sender, "receiver", "hello", None)
    pending = store.pending(receiver)
    assert [item[1]["msg_id"] for item in pending] == [message["msg_id"]]
    store.archive(pending[0][0], receiver, "delivered")
    assert store.pending(receiver) == []
    assert store.inbox(receiver, since=None, limit=10)[0]["status"] == "delivered"


def test_pending_skips_deferred_message_without_reading_it(settings) -> None:
    store = FileStore(settings, local_host="test-host")
    store.prepare()
    sender = identity("sender", "session-a")
    receiver = identity("receiver", "session-b")
    store.register(sender)
    store.register(receiver)
    message = store.send(sender, "receiver", "hello", None)

    with patch.object(store, "_read_payload", side_effect=AssertionError("unexpected read")):
        assert store.pending(receiver, skip_ids={message["msg_id"]}) == []


def test_online_name_collision_is_rejected(settings) -> None:
    store = FileStore(settings, local_host="test-host")
    store.prepare()
    first = identity("same", "session-a")
    second = identity("same", "session-b")
    store.register(first)
    with pytest.raises(StoreError, match="already exists"):
        store.register(second)


def test_tampered_message_is_rejected(settings) -> None:
    store = FileStore(settings, local_host="test-host")
    store.prepare()
    sender = identity("sender", "session-a")
    receiver = identity("receiver", "session-b")
    store.register(sender)
    store.register(receiver)
    message = store.send(sender, "receiver", "hello", None)
    path = store.spool_dir / agent_key(receiver.host, receiver.session_id) / f"{message['msg_id']}.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["payload"]["text"] = "forged"
    path.write_text(json.dumps(document), encoding="utf-8")
    os.chmod(path, 0o600)
    assert store.pending(receiver) == []
    rejected = store.archive_dir / agent_key(receiver.host, receiver.session_id) / "rejected" / path.name
    assert rejected.exists()


def test_wrong_key_cannot_list_agents(settings) -> None:
    store = FileStore(settings, local_host="test-host")
    store.prepare()
    store.register(identity("sender", "session-a"))
    other = FileStore(replace(settings, auth_key=b"z" * 48), local_host="test-host")
    other.prepare()
    assert other.list_agents() == []


def test_wrong_key_registration_is_ignored(settings) -> None:
    trusted = FileStore(settings, local_host="test-host")
    trusted.prepare()
    untrusted = FileStore(replace(settings, auth_key=b"z" * 48), local_host="test-host")
    untrusted.prepare()
    untrusted.register(identity("intruder", "session-x"))
    assert trusted.list_agents() == []


def test_pid_reuse_record_is_not_online(settings) -> None:
    store = FileStore(settings, local_host="test-host")
    store.prepare()
    stale = replace(identity("stale", "session-b"), process_start="0")
    store.register(stale)
    assert store.list_agents() == []
    with pytest.raises(StoreError, match="not online"):
        store.send(identity("sender", "session-a"), "stale", "hello", None)


def test_local_agent_with_stale_sidecar_heartbeat_is_not_online(settings, monkeypatch) -> None:
    stale_time = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    monkeypatch.setattr("agent_collab.store.utc_now", lambda: stale_time)
    store = FileStore(settings, local_host="test-host")
    store.prepare()
    store.register(identity("stale", "session-b"))

    assert store.list_agents() == []


def test_remote_agent_uses_signed_heartbeat(settings) -> None:
    remote = FileStore(settings, local_host="host-b")
    observer = FileStore(settings, local_host="host-a")
    remote.prepare()
    remote.register(identity("receiver", "session-b", host="host-b"))

    assert [agent["name"] for agent in observer.list_agents()] == ["receiver"]
    target = agent_id("test", "test", "host-b", os.getpid())
    assert target.isascii() and target.isidentifier()
    assert agent_id("/workspace/my-repo", "cc", "dev-box", 4242) == (
        "my_repo_claude_dev_box_4242"
    )
    assert (
        agent_id("/workspace/my-repo", "codex", "build-cluster-worker-node7", 4242)
        == "my_repo_codex_node7_4242"
    )
    assert short_host("build-cluster-worker-node7") == "node7"
    assert short_host("dev-box") == "dev-box"
    message = observer.send(identity("sender", "session-a", host="host-a"), target, "hello", None)
    assert message["to"] == "receiver@host-b"


def test_stale_remote_heartbeat_is_offline(settings, monkeypatch) -> None:
    stale_time = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    monkeypatch.setattr("agent_collab.store.utc_now", lambda: stale_time)
    remote = FileStore(settings, local_host="host-b")
    observer = FileStore(settings, local_host="host-a")
    remote.prepare()
    remote.register(identity("receiver", "session-b", host="host-b"))

    assert observer.list_agents() == []
    with pytest.raises(StoreError, match="not online"):
        observer.send(
            identity("sender", "session-a", host="host-a"),
            "receiver@host-b",
            "hello",
            None,
        )


def test_inbox_can_read_one_exact_message(settings) -> None:
    store = FileStore(settings, local_host="test-host")
    store.prepare()
    sender = identity("sender", "session-a")
    receiver = identity("receiver", "session-b")
    store.register(sender)
    store.register(receiver)
    first = store.send(sender, "receiver", "first", None)
    store.send(sender, "receiver", "second", None)

    messages = store.inbox(
        receiver,
        since="9999-12-31T23:59:59+00:00",
        limit=1,
        message_id=first["msg_id"],
    )

    assert [message["text"] for message in messages] == ["first"]

    with pytest.raises(StoreError, match="canonical UUID"):
        store.inbox(receiver, since=None, limit=1, message_id="../message")


def test_requeues_failed_message_for_same_online_instance(settings) -> None:
    store = FileStore(settings, local_host="test-host")
    store.prepare()
    sender = identity("sender", "session-a")
    receiver = identity("receiver", "session-b")
    store.register(sender)
    store.register(receiver)
    message = store.send(sender, "receiver", "hello", None)
    path, _ = store.pending(receiver)[0]
    store.archive(path, receiver, "failed")

    assert store.requeue_failed(receiver) == 1
    assert [item[1]["msg_id"] for item in store.pending(receiver)] == [message["msg_id"]]


def test_requeues_failed_message_for_same_session_new_instance(settings) -> None:
    store = FileStore(settings, local_host="test-host")
    store.prepare()
    sender = identity("sender", "session-a")
    receiver = identity("receiver", "session-b")
    store.register(sender)
    store.register(receiver)
    message = store.send(sender, "receiver", "hello", None)
    path, _ = store.pending(receiver)[0]
    store.archive(path, receiver, "failed")

    resumed = replace(receiver, process_start="different-process")
    assert store.requeue_failed(resumed) == 1
    assert [item[1]["msg_id"] for item in store.pending(resumed)] == [message["msg_id"]]


def test_does_not_requeue_failed_message_for_other_session(settings) -> None:
    store = FileStore(settings, local_host="test-host")
    store.prepare()
    sender = identity("sender", "session-a")
    receiver = identity("receiver", "session-b")
    store.register(sender)
    store.register(receiver)
    store.send(sender, "receiver", "hello", None)
    path, _ = store.pending(receiver)[0]
    store.archive(path, receiver, "failed")

    other = replace(receiver, session_id="session-c", process_start="different-process")
    assert store.requeue_failed(other) == 0
    assert store.pending(other) == []


def test_sent_status_reports_pending_archived_and_absent(settings) -> None:
    store = FileStore(settings, local_host="test-host")
    store.prepare()
    sender = identity("sender", "session-a")
    receiver = identity("receiver", "session-b")
    store.register(sender)
    store.register(receiver)
    message = store.send(sender, "receiver", "hello", None)
    key = agent_key(receiver.host, receiver.session_id)

    assert store.sent_status(key, message["msg_id"]) == "pending"

    path, _ = store.pending(receiver)[0]
    store.archive(path, receiver, "delivered")
    assert store.sent_status(key, message["msg_id"]) == "delivered"
    assert store.sent_status(key, "00000000-0000-0000-0000-000000000000") is None


def test_room_lifecycle_and_membership(settings) -> None:
    store = FileStore(settings, local_host="test-host")
    store.prepare()
    creator = alive_identity("creator", "session-a", os.getpid())
    joiner = alive_identity("joiner", "session-b", os.getppid())
    store.register(creator)
    store.register(joiner)

    created = store.room_create(creator)
    room_id = created["room_id"]
    assert len(room_id) == 4
    assert len(created["long_id"]) == 16 and created["long_id"].startswith(room_id)

    long_id, payload = store.room_resolve(f"#{room_id}")
    assert long_id == created["long_id"]
    assert [member["agent_id"] for member in payload["members"]] == [
        agent_id(creator.repo, creator.client, creator.host, creator.pid)
    ]
    assert store.rooms_of(creator) == [room_id]
    assert store.rooms_of(joiner) == []

    joined = store.room_join(room_id, joiner)
    assert joined["already"] is False
    assert len(joined["members"]) == 2
    assert store.rooms_of(joiner) == [room_id]

    left = store.room_leave(room_id, joiner)
    assert len(left["members"]) == 1
    assert store.rooms_of(joiner) == []
    with pytest.raises(StoreError, match="not a member"):
        store.room_leave(room_id, joiner)

    with pytest.raises(StoreError, match="room not found"):
        store.room_resolve("zzzz")


def test_room_ids_stay_unique(settings) -> None:
    store = FileStore(settings, local_host="test-host")
    store.prepare()
    creator = alive_identity("creator", "session-a", os.getpid())
    store.register(creator)
    room_ids = {store.room_create(creator)["room_id"] for _ in range(3)}
    assert len(room_ids) == 3


def test_room_prunes_offline_members(settings) -> None:
    store = FileStore(settings, local_host="test-host")
    store.prepare()
    creator = alive_identity("creator", "session-a", os.getpid())
    gone = replace(alive_identity("gone", "session-b", os.getpid()), pid=999_999)
    store.register(creator)
    created = store.room_create(creator)
    store.room_join(created["room_id"], gone)

    long_id, payload = store.room_resolve(created["room_id"])
    assert len(payload["members"]) == 2

    payload = store.room_prune(long_id, payload)
    assert [member["name"] for member in payload["members"]] == ["creator"]


def test_room_chat_persists_to_jsonl_and_fans_out(settings) -> None:
    store = FileStore(settings, local_host="test-host")
    store.prepare()
    sender = alive_identity("alpha", "session-a", os.getpid())
    member = alive_identity("beta", "session-b", os.getppid())
    store.register(sender)
    store.register(member)
    created = store.room_create(sender)
    store.room_join(created["room_id"], member)

    sent = store.send_room(sender, f"#{created['room_id']}", "hello room", None)

    copies = store.pending(member)
    assert [copy[1]["msg_id"] for copy in copies] == [sent["msg_id"]]
    assert copies[0][1]["room"] == created["room_id"]
    assert copies[0][1]["from_name"] == "alpha"
    assert store.pending(sender) == []

    log_path = store.rooms_dir / f"{created['long_id']}.jsonl"
    lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    events = [open_sealed(line, settings.auth_key) for line in lines]
    assert [event["kind"] for event in events] == ["created", "join", "chat"]
    chat = events[-1]
    assert chat["msg_id"] == sent["msg_id"]
    assert chat["text"] == "hello room"
    assert chat["room"] == created["room_id"]

    listing = store.list_rooms()
    assert [room["room_id"] for room in listing] == [created["room_id"]]
    assert {member_entry["name"] for member_entry in listing[0]["members"]} == {"alpha", "beta"}


def test_room_resolve_accepts_long_id_as_alias(settings) -> None:
    store = FileStore(settings, local_host="test-host")
    store.prepare()
    creator = alive_identity("creator", "session-a", os.getpid())
    store.register(creator)
    created = store.room_create(creator)

    long_id, payload = store.room_resolve(created["long_id"])
    assert long_id == created["long_id"]
    assert payload["room_id"] == created["room_id"]

    # 报错要指路而不只是判错:带 find_coagents 提示,教人当场自愈
    with pytest.raises(StoreError, match="room not found") as exc_info:
        store.room_resolve("x" * 16)
    assert "find_coagents" in str(exc_info.value)
    assert "long id" in str(exc_info.value)

    with pytest.raises(StoreError, match="room not found") as exc_info:
        store.room_resolve("zzzz")
    assert "find_coagents" in str(exc_info.value)

    with pytest.raises(StoreError, match="room id must be") as exc_info:
        store.room_resolve("abcd!")  # 非法字符
    assert "find_coagents" in str(exc_info.value)
