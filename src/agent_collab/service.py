from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any

from .config import Settings
from .delivery import (
    PEER_REQUEST_POLICY,
    Delivery,
    DeliveryResult,
    TITLE_MAX_CHARS,
    arrow_notice,
    delivery_from_environment,
    room_charter,
)
from .feishu import post_text
from .identity import Identity, _default_name, detect_identity, process_is_alive
from .store import FileStore, StoreError, agent_id, agent_key, short_host

LOG = logging.getLogger(__name__)
DELIVERY_RETRY_SECONDS = 5.0
SENT_STATUS_MAX_ATTEMPTS = 5
NAME_COLLISION_RETRIES = 12


def _send_notice(label: str, status_word: str, msg_id: str, title: str) -> str:
    return f"{arrow_notice('→', label, status_word, msg_id)} ·{title}"


class RateLimitError(RuntimeError):
    pass


class MinuteRateLimiter:
    def __init__(self, limit: int):
        self.limit = limit
        self.events: deque[float] = deque()
        self.lock = threading.Lock()

    def acquire(self) -> None:
        now = time.monotonic()
        with self.lock:
            while self.events and now - self.events[0] >= 60:
                self.events.popleft()
            if len(self.events) >= self.limit:
                raise RateLimitError("message rate limit exceeded")
            self.events.append(now)


class CollaborationService:
    def __init__(
        self,
        settings: Settings,
        identity: Identity | None = None,
        delivery: Delivery | None = None,
    ):
        self.settings = settings
        self.identity = identity or detect_identity()
        self.store = FileStore(settings, local_host=self.identity.host)
        if self.identity.name_generated:
            # detect_identity cannot see the registry; assign the unique numeric
            # suffix now that the store is available (smallest free index).
            self.identity = self.identity.with_generated_name(self._generated_name())
        self.delivery = delivery or delivery_from_environment(self.identity)
        self.rate_limiter = MinuteRateLimiter(settings.send_limit_per_minute)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._delivery_retry_at: dict[str, float] = {}
        self._sent: dict[str, dict[str, object]] = {}

    def start(self) -> None:
        self.store.prepare()
        self._register_with_name_retry()
        recovered = self.store.requeue_failed(self.identity)
        if recovered:
            LOG.info("requeued %d failed messages for delivery", recovered)
        self._thread = threading.Thread(target=self._watch, name="agent-collab-inbox", daemon=True)
        self._thread.start()

    def _generated_name(self) -> str:
        base = _default_name(
            self.identity.client,
            self.identity.model,
            self.identity.repo,
            self.settings.repo_short_names,
        )
        used = self.store.used_name_suffixes(base, self.identity.host)
        index = 1
        while index in used:
            index += 1
        return f"{base}-{index}"

    def _register_with_name_retry(self) -> None:
        for attempt in range(NAME_COLLISION_RETRIES):
            try:
                self.store.register(self.identity)
                return
            except StoreError:
                if attempt == NAME_COLLISION_RETRIES - 1 or not self.identity.name_generated:
                    raise
                # re-scan the registry: the colliding registration is visible now
                self.identity = self.identity.with_generated_name(self._generated_name())

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self.store.unregister(self.identity)

    def _watch(self) -> None:
        next_heartbeat = time.monotonic() + self.settings.heartbeat_seconds
        while not self._stop.wait(self.settings.poll_seconds):
            try:
                if not process_is_alive(self.identity.pid, self.identity.process_start):
                    LOG.info("client process %s is gone; unregistering and stopping sidecar", self.identity.pid)
                    self.store.unregister(self.identity)
                    self._stop.set()
                    break
                now = time.monotonic()
                if now >= next_heartbeat:
                    next_heartbeat = now + self.settings.heartbeat_seconds
                    self.store.register(self.identity)
                deferred = {
                    msg_id
                    for msg_id, retry_at in self._delivery_retry_at.items()
                    if retry_at > now
                }
                for path, message in self.store.pending(self.identity, skip_ids=deferred):
                    if self._stop.is_set():
                        break
                    msg_id = str(message.get("msg_id", ""))
                    now = time.monotonic()
                    if self._delivery_retry_at.get(msg_id, 0) > now:
                        continue
                    try:
                        result = self.delivery.deliver(message)
                    except Exception as exc:  # noqa: BLE001 - retry unexpected adapter failures
                        self._delivery_retry_at[msg_id] = time.monotonic() + DELIVERY_RETRY_SECONDS
                        LOG.error(
                            "message %s delivery error=%s; retrying",
                            msg_id,
                            type(exc).__name__,
                        )
                        continue
                    status = result.status if result.ok else "failed"
                    if result.ok:
                        self.store.archive(path, self.identity, status)
                        self._delivery_retry_at.pop(msg_id, None)
                        LOG.info(
                            "message %s delivery=%s detail=%s",
                            msg_id,
                            status,
                            result.detail,
                        )
                    else:
                        self._delivery_retry_at[msg_id] = time.monotonic() + DELIVERY_RETRY_SECONDS
                        LOG.warning(
                            "message %s delivery=%s detail=%s; retrying",
                            msg_id,
                            status,
                            result.detail,
                        )
                    now = time.monotonic()
                    if not self._stop.is_set() and now >= next_heartbeat:
                        next_heartbeat = now + self.settings.heartbeat_seconds
                        self.store.register(self.identity)
                self._check_sent_status()
            except Exception as exc:  # noqa: BLE001 - watcher must stay alive after one bad file
                LOG.error("inbox watcher error: %s", type(exc).__name__)

    def _status_counts(self, msg_id: str, entry: dict[str, Any]) -> tuple[dict[str, int], bool]:
        counts: dict[str, int] = {}
        settled = True
        for key, _addr, member_agent_id in entry["members"]:
            status = self.store.sent_status(key, msg_id)
            if status in ("delivered", "queued", "duplicate"):
                word = "delivered" if status == "duplicate" else status
                counts[word] = counts.get(word, 0) + 1
            elif status == "rejected":
                counts["rejected"] = counts.get("rejected", 0) + 1
            elif entry["room"] and member_agent_id and self.store.online_member(member_agent_id) is None:
                counts["offline"] = counts.get("offline", 0) + 1
            else:
                settled = False
        return counts, settled

    @staticmethod
    def _status_word(counts: dict[str, int], total: int) -> str:
        if total == 1 and len(counts) == 1:
            return next(iter(counts))
        return (
            " ".join(
                f"{word} {counts[word]}"
                for word in ("delivered", "queued", "rejected", "offline")
                if counts.get(word)
            )
            or "unreached"
        )

    def _wait_terminal_status(self, msg_id: str, entry: dict[str, Any]) -> str | None:
        """Block briefly until every copy reaches a transport terminal state.

        The send tool result is the moment a human treats the message as sent, so it
        should report the real state instead of an unconditional "queued": wait up to
        send_wait_seconds for the receiver sidecar to confirm; None still means
        pending, and the async final-status notice keeps covering that case.
        """
        deadline = time.monotonic() + self.settings.send_wait_seconds
        while True:
            counts, settled = self._status_counts(msg_id, entry)
            if settled:
                return self._status_word(counts, len(entry["members"]))
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.05)

    def _check_sent_status(self) -> None:
        for msg_id, entry in list(self._sent.items()):
            members = entry["members"]
            if not members:
                self._sent.pop(msg_id, None)
                continue
            counts, settled = self._status_counts(msg_id, entry)
            if not settled:
                continue
            status_word = self._status_word(counts, len(members))
            notice = _send_notice(str(entry["label"]), status_word, msg_id, str(entry.get("title", "")))
            try:
                result = self.delivery.deliver({"notice": notice, "msg_id": msg_id})
            except Exception as exc:  # noqa: BLE001 - status notices must not kill the watcher
                result = DeliveryResult(False, type(exc).__name__)
            if result.ok:
                self._sent.pop(msg_id, None)
                LOG.info("sent message %s status=%s notified", msg_id, status_word)
                continue
            entry["attempts"] = int(entry["attempts"]) + 1
            if int(entry["attempts"]) >= SENT_STATUS_MAX_ATTEMPTS:
                self._sent.pop(msg_id, None)
                LOG.info("sent message %s status notice dropped after retries", msg_id)

    def set_identity(self, role: str, name: str | None = None) -> dict[str, Any]:
        updated = self.identity.with_identity(role=role, name=name)
        self.store.register(updated)
        self.identity = updated
        return self.public_identity()

    def public_identity(self) -> dict[str, Any]:
        return {
            "name": self.identity.name,
            "role": self.identity.role,
            "repo": self.identity.repo,
            "host": self.identity.host,
            "client": self.identity.client,
            "model": self.identity.model,
            "session_id": self.identity.session_id,
            "pid": self.identity.pid,
            "agent_id": agent_id(
                self.identity.repo,
                self.identity.client,
                self.identity.host,
                self.identity.pid,
            ),
        }

    def list_agents(self) -> dict[str, Any]:
        return {
            "self": self.public_identity(),
            "agents": self.store.list_agents(),
            "rooms": self.store.list_rooms(),
        }

    def find_coagents(self) -> str:
        agents = self.store.list_agents()
        agents.sort(
            key=lambda item: (
                item["host"] != self.identity.host,
                str(item["agent_id"]),
            )
        )
        lines = [
            "| Agent ID | Name | Repo | Host Name | Client | Model | PID |",
            "| --- | --- | --- | --- | --- | --- | ---: |",
        ]
        for agent in agents:
            full_host = str(agent["host"])
            host = short_host(full_host)
            if full_host == self.identity.host:
                if str(agent["session_id"]) == self.identity.session_id:
                    host += "（本机·自己）"
                else:
                    host += "（本机）"
            client = {"cc": "Claude", "codex": "Codex"}.get(
                str(agent["client"]), str(agent["client"])
            )
            cells = (
                agent["agent_id"],
                agent["name"],
                agent["repo"],
                host,
                client,
                agent["model"] or "-",
                agent["pid"],
            )
            escaped = [str(cell).replace("|", "\\|") for cell in cells]
            lines.append("| " + " | ".join(escaped) + " |")
        rooms = self.store.list_rooms()
        if rooms:
            lines.append("")
            lines.append("| Room ID | Members | Created |")
            lines.append("| --- | --- | --- |")
            for room in rooms:
                members = ", ".join(
                    str(member["name"]) for member in room["members"]
                ) or "（空房,可凭 ID 重进）"
                escaped_members = members.replace("|", "\\|")
                lines.append(
                    f'| #{room["room_id"]} | {escaped_members} | {str(room["created"])[:16]} |'
                )
        return "\n".join(lines)

    def send(self, to: str, title: str, text: str, reply_to: str | None = None) -> dict[str, Any]:
        self._validate_title(title)
        self._validate_text(text)
        self.rate_limiter.acquire()
        if to.lstrip().startswith("#"):
            return self._send_room(to, title, text, reply_to)
        message = self.store.send(self.identity, to, title, text, reply_to)
        msg_id = str(message["msg_id"])
        peer = str(message["to"])
        host = peer.split("@", 1)[1]
        entry = {
            "label": peer,
            "room": False,
            "members": [(agent_key(host, str(message["to_session"])), peer, None)],
            "attempts": 0,
            "title": title,
        }
        status_word = self._settle_or_keep_pending(msg_id, entry)
        return {
            "msg_id": msg_id,
            "to": peer,
            "status": status_word,
            "title": title,
            "notice": _send_notice(peer, status_word, msg_id, title),
        }

    def _send_room(self, to: str, title: str, text: str, reply_to: str | None) -> dict[str, Any]:
        result = self.store.send_room(self.identity, to, title, text, reply_to)
        msg_id = str(result["msg_id"])
        label = f"#{result['room_id']}"
        entry = {
            "label": label,
            "room": True,
            "members": [
                (str(recipient["key"]), str(recipient["address"]), str(recipient["agent_id"]))
                for recipient in result["recipients"]
            ],
            "attempts": 0,
            "title": title,
        }
        status_word = self._settle_or_keep_pending(msg_id, entry)
        return {
            "msg_id": msg_id,
            "to": label,
            "status": status_word,
            "recipients": [str(recipient["address"]) for recipient in result["recipients"]],
            "title": title,
            "notice": _send_notice(f"{label} ({len(entry['members'])})", status_word, msg_id, title),
        }

    def _settle_or_keep_pending(self, msg_id: str, entry: dict[str, Any]) -> str:
        """Wait for a terminal transport state; register for the async notice only if still pending."""
        settled = self._wait_terminal_status(msg_id, entry)
        if settled is not None:
            return settled
        self._sent[msg_id] = entry
        return "pending"

    def _agent_id(self) -> str:
        return agent_id(self.identity.repo, self.identity.client, self.identity.host, self.identity.pid)

    def create_room(self, members: list[str] | None = None) -> dict[str, Any]:
        self.rate_limiter.acquire()
        current = self.store.rooms_of(self.identity)
        if current:
            raise StoreError(f"already in room #{current[0]}; call leave_room() first (one room per agent)")
        created = self.store.room_create(self.identity)
        room_id = str(created["room_id"])
        invited: list[str] = []
        skipped: list[str] = []
        for target in members or []:
            try:
                recipient = self.store.resolve_target(target)
            except StoreError:
                skipped.append(target)
                continue
            invite = (
                f"[room {room_id}] {self.identity.name} invites you to chat room #{room_id}. "
                f'Join with join_room(room_id="{room_id}"), then send with send(to="#{room_id}", ...).'
            )
            self.store.send(self.identity, target, "房间邀请", invite, None, room=room_id)
            invited.append(f"{recipient['name']}@{recipient['host']}")
        return {
            "room_id": room_id,
            "long_id": str(created["long_id"]),
            "log": str(self.store.rooms_dir / f"{created['long_id']}.jsonl"),
            "invited": invited,
            "skipped_offline": skipped,
            "charter": room_charter(room_id),
        }

    def join_room(self, room_id: str) -> dict[str, Any]:
        self.rate_limiter.acquire()
        current = self.store.rooms_of(self.identity)
        if current:
            raise StoreError(f"already in room #{current[0]}; call leave_room() first (one room per agent)")
        joined = self.store.room_join(room_id, self.identity)
        if not joined["already"]:
            self._broadcast_room_event(str(joined["room_id"]), joined["members"], "成员加入", f"{self.identity.name} 加入了聊天室")
        me = self._agent_id()
        return {
            "room_id": joined["room_id"],
            "long_id": joined["long_id"],
            "log": str(self.store.rooms_dir / f"{joined['long_id']}.jsonl"),
            "members": [
                {
                    "agent_id": str(member["agent_id"]),
                    "name": str(member["name"]),
                    "address": str(member["address"]),
                    # 成员条目存的是入房时名字(改名滞后),靠 you 标记认出自己
                    "you": str(member["agent_id"]) == me,
                }
                for member in joined["members"]
            ],
            "already": joined["already"],
            "charter": room_charter(str(joined["room_id"])),
        }

    def leave_room(self) -> dict[str, Any]:
        self.rate_limiter.acquire()
        current = self.store.rooms_of(self.identity)
        if not current:
            raise StoreError("not in any room")
        left = self.store.room_leave(current[0], self.identity)
        self._broadcast_room_event(str(left["room_id"]), left["members"], "成员离开", f"{self.identity.name} 离开了聊天室")
        return {"left": left["room_id"]}

    def _broadcast_room_event(
        self, room_id: str, members: list[dict[str, Any]], title: str, text: str
    ) -> None:
        my_agent_id = self._agent_id()
        for member in members:
            if str(member["agent_id"]) == my_agent_id:
                continue
            online = self.store.online_member(str(member["agent_id"]))
            if online is None:
                continue
            self.store.send(
                self.identity,
                f"{online['name']}@{online['host']}",
                title,
                f"[room {room_id}] {text}",
                None,
                room=room_id,
            )

    def inbox(
        self,
        since: str | None = None,
        limit: int = 20,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        if limit < 1 or limit > 100:
            raise StoreError("limit must be between 1 and 100")
        return {
            "handling": PEER_REQUEST_POLICY,
            "messages": self.store.inbox(
                self.identity,
                since=since,
                limit=limit,
                message_id=message_id,
            ),
        }

    def report_to_feishu(self, text: str, title: str | None = None) -> dict[str, Any]:
        self._validate_text(text)
        self.rate_limiter.acquire()
        if not self.settings.feishu_webhook_url:
            raise StoreError("FEISHU_WEBHOOK_URL is not configured")
        safe_title = (title or "Agent update").strip()
        if len(safe_title) > 120:
            raise StoreError("title is too long")
        body = (
            f"{safe_title}\n"
            f"from: {self.identity.address}\n"
            f"role: {self.identity.role or '-'}\n"
            f"repo: {self.identity.repo}\n\n"
            f"{text}"
        )
        result = post_text(self.settings.feishu_webhook_url, body)
        return {"sent": result.ok, "detail": result.detail}

    def _validate_title(self, title: str) -> None:
        if not title or not title.strip():
            raise StoreError("title cannot be empty")
        if len(title.strip()) > TITLE_MAX_CHARS:
            raise StoreError(f"title exceeds {TITLE_MAX_CHARS} characters")
        if self.settings.contains_secret(title):
            raise StoreError("title contains a configured secret")

    def _validate_text(self, text: str) -> None:
        if not text or not text.strip():
            raise StoreError("text cannot be empty")
        if len(text) > self.settings.max_message_chars:
            raise StoreError(f"text exceeds {self.settings.max_message_chars} characters")
        if self.settings.contains_secret(text):
            raise StoreError("text contains a configured secret")
