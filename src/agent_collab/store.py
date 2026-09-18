from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import socket
import stat
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .auth import AuthenticationError, open_sealed, seal
from .config import ConfigurationError, Settings
from .identity import Identity, process_is_alive

ROOM_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"
ROOM_LONG_ID_LENGTH = 16
ROOM_SHORT_ID_LENGTH = 4
ROOM_EMPTY_TTL = timedelta(hours=2)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def agent_key(host: str, session_id: str) -> str:
    return hashlib.sha256(f"{host}\0{session_id}".encode()).hexdigest()[:32]


ARCHIVE_STATUSES = ("delivered", "queued", "failed", "rejected", "duplicate")


def short_host(host: str, limit: int = 12) -> str:
    host = host.strip()
    if len(host) <= limit and re.fullmatch(r"[A-Za-z0-9_.-]+", host):
        return host
    parts = [part for part in re.split(r"[^A-Za-z0-9]+", host) if part]
    candidate = parts[-1] if parts else "host"
    return candidate[-limit:]


def _identifier_part(value: str, fallback: str) -> str:
    part = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")
    return part or fallback


def agent_id(repo: str, client: str, host: str, pid: int) -> str:
    repo_name = Path(repo.rstrip("/")).name
    repo_part = _identifier_part(repo_name, "repo")
    if repo_part[0].isdigit():
        repo_part = f"repo_{repo_part}"
    client_name = "claude" if client == "cc" else client
    client_part = _identifier_part(client_name, "agent")
    host_part = _identifier_part(short_host(host), "host")
    return f"{repo_part}_{client_part}_{host_part}_{pid}"


class StoreError(RuntimeError):
    pass


class FileStore:
    def __init__(self, settings: Settings, local_host: str | None = None):
        self.settings = settings
        self.local_host = local_host or socket.gethostname()
        self.root = settings.state_dir
        self.agents_dir = self.root / "agents"
        self.spool_dir = self.root / "spool"
        self.archive_dir = self.root / "archive"
        self.rooms_dir = self.root / "rooms"
        self.lock_path = self.root / ".lock"

    def prepare(self) -> None:
        self._secure_directory(self.root)
        for path in (self.agents_dir, self.spool_dir, self.archive_dir, self.rooms_dir):
            self._secure_directory(path)
        if not self.lock_path.exists():
            try:
                fd = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
        self._require_secure_file(self.lock_path)

    @staticmethod
    def _secure_directory(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = path.lstat()
        if path.is_symlink() or not stat.S_ISDIR(info.st_mode):
            raise ConfigurationError(f"state path is not a real directory: {path}")
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ConfigurationError(f"state directory must be owned by current user with mode 0700: {path}")

    @staticmethod
    def _require_secure_file(path: Path) -> None:
        info = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(info.st_mode):
            raise ConfigurationError(f"state file is not a regular file: {path}")
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ConfigurationError(f"state file must be owned by current user with mode 0600: {path}")

    @contextmanager
    def _lock(self) -> Iterator[None]:
        with self.lock_path.open("r", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _write_json_atomic(self, path: Path, document: dict[str, Any], *, overwrite: bool) -> None:
        self._secure_directory(path.parent)
        data = (json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        tmp = Path(raw_tmp)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if not overwrite and path.exists():
                raise StoreError(f"refusing to overwrite state file: {path.name}")
            os.replace(tmp, path)
            self._fsync_directory(path.parent)
        finally:
            tmp.unlink(missing_ok=True)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _read_payload(self, path: Path) -> dict[str, Any]:
        self._require_secure_file(path)
        if path.stat().st_size > 128 * 1024:
            raise AuthenticationError("state document is too large")
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise AuthenticationError("invalid state JSON") from exc
        return open_sealed(document, self.settings.auth_key)

    def _agent_path(self, identity: Identity) -> Path:
        return self.agents_dir / f"{agent_key(identity.host, identity.session_id)}.json"

    def register(self, identity: Identity) -> None:
        path = self._agent_path(identity)
        with self._lock():
            now = utc_now()
            registered = now
            try:
                current = self._read_payload(path)
            except (FileNotFoundError, AuthenticationError, ConfigurationError):
                pass
            else:
                if (
                    current.get("pid") == identity.pid
                    and current.get("process_start") == identity.process_start
                ):
                    registered = str(current.get("registered", now))
            for other in self._iter_agent_payloads():
                if (
                    other["host"] == identity.host
                    and other["name"] == identity.name
                    and (other["session_id"] != identity.session_id or other["process_start"] != identity.process_start)
                    and self._is_online(other)
                ):
                    raise StoreError(f"online agent name already exists: {identity.address}")
            payload = {**identity.as_dict(), "registered": registered, "seen": now}
            self._write_json_atomic(path, seal(payload, self.settings.auth_key), overwrite=True)

    def unregister(self, identity: Identity) -> None:
        path = self._agent_path(identity)
        with self._lock():
            try:
                payload = self._read_payload(path)
            except (FileNotFoundError, AuthenticationError, ConfigurationError):
                return
            if payload.get("process_start") == identity.process_start and payload.get("pid") == identity.pid:
                path.unlink(missing_ok=True)

    def _iter_agent_payloads(self) -> Iterator[dict[str, Any]]:
        for path in sorted(self.agents_dir.glob("*.json")):
            try:
                payload = self._read_payload(path)
                required = {"name", "host", "session_id", "pid", "process_start", "client", "repo", "cwd", "role"}
                if required.issubset(payload):
                    yield payload
            except (OSError, AuthenticationError, ConfigurationError):
                continue

    def _is_online(self, payload: dict[str, Any]) -> bool:
        if payload.get("host") == self.local_host:
            try:
                if not process_is_alive(int(payload["pid"]), str(payload["process_start"])):
                    return False
            except (KeyError, TypeError, ValueError):
                return False
        try:
            seen = datetime.fromisoformat(str(payload["seen"]))
            if seen.tzinfo is None:
                return False
            age = datetime.now(UTC) - seen.astimezone(UTC)
            window = timedelta(seconds=self.settings.remote_stale_seconds)
            return -window <= age <= window
        except (KeyError, TypeError, ValueError, OverflowError):
            return False

    def used_name_suffixes(self, base: str, host: str) -> set[int]:
        """Numeric indices held by online same-host names of the form <base>-<n>."""
        pattern = re.compile(rf"^{re.escape(base)}-(\d+)$")
        used: set[int] = set()
        for payload in self._iter_agent_payloads():
            if payload["host"] != host or not self._is_online(payload):
                continue
            match = pattern.fullmatch(str(payload["name"]))
            if match:
                used.add(int(match.group(1)))
        return used

    def list_agents(self) -> list[dict[str, Any]]:
        agents: list[dict[str, Any]] = []
        for payload in self._iter_agent_payloads():
            if self._is_online(payload):
                agents.append(
                    {
                        "name": payload["name"],
                        "role": payload["role"],
                        "repo": payload["repo"],
                        "cwd": payload["cwd"],
                        "host": payload["host"],
                        "client": payload["client"],
                        "model": payload.get("model", ""),
                        "session_id": payload["session_id"],
                        "pid": payload["pid"],
                        "agent_id": agent_id(
                            str(payload["repo"]),
                            str(payload["client"]),
                            str(payload["host"]),
                            int(payload["pid"]),
                        ),
                    }
                )
        return agents

    def _resolve_target(self, target: str) -> dict[str, Any]:
        online = [item for item in self._iter_agent_payloads() if self._is_online(item)]
        by_agent_id = [
            item
            for item in online
            if agent_id(
                str(item["repo"]),
                str(item["client"]),
                str(item["host"]),
                int(item["pid"]),
            )
            == target
        ]
        exact = [item for item in online if f"{item['name']}@{item['host']}" == target]
        matches = by_agent_id or exact or [item for item in online if item["name"] == target]
        if not matches:
            raise StoreError(f"target is not online: {target}")
        if len(matches) > 1:
            raise StoreError(f"target is ambiguous; use name@host: {target}")
        return matches[0]

    def resolve_target(self, target: str) -> dict[str, Any]:
        return self._resolve_target(target)

    def _deliver_single(
        self,
        sender: Identity,
        recipient: dict[str, Any],
        msg_id: str,
        title: str,
        text: str,
        reply_to: str | None,
        room: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "msg_id": msg_id,
            "title": title,
            "from": sender.address,
            "from_session": sender.session_id,
            "from_instance": sender.instance,
            "from_role": sender.role,
            "from_name": sender.name,
            "to": f"{recipient['name']}@{recipient['host']}",
            "to_session": recipient["session_id"],
            "to_instance": f"{recipient['pid']}:{recipient['process_start']}",
            "time": utc_now(),
            "text": text,
        }
        if room:
            payload["room"] = room
        if reply_to:
            payload["reply_to"] = reply_to
        recipient_key = agent_key(str(recipient["host"]), str(recipient["session_id"]))
        path = self.spool_dir / recipient_key / f"{msg_id}.json"
        with self._lock():
            self._write_json_atomic(path, seal(payload, self.settings.auth_key), overwrite=False)
        return payload

    def send(
        self,
        sender: Identity,
        target: str,
        title: str,
        text: str,
        reply_to: str | None,
        room: str | None = None,
    ) -> dict[str, Any]:
        recipient = self._resolve_target(target)
        return self._deliver_single(sender, recipient, str(uuid.uuid4()), title, text, reply_to, room)

    def pending(
        self,
        identity: Identity,
        *,
        skip_ids: set[str] | None = None,
    ) -> list[tuple[Path, dict[str, Any]]]:
        directory = self.spool_dir / agent_key(identity.host, identity.session_id)
        if not directory.exists():
            return []
        skipped = skip_ids or set()
        messages: list[tuple[Path, dict[str, Any]]] = []
        for path in sorted(directory.glob("*.json")):
            if path.stem in skipped:
                continue
            try:
                payload = self._read_payload(path)
            except (OSError, AuthenticationError, ConfigurationError):
                self.archive(path, identity, "rejected")
                continue
            if path.stem != payload.get("msg_id"):
                self.archive(path, identity, "rejected")
                continue
            if payload.get("to_session") != identity.session_id:
                continue
            if self.was_processed(identity, str(payload["msg_id"])):
                self.archive(path, identity, "duplicate")
                continue
            messages.append((path, payload))
        return messages

    def sent_status(self, recipient_key: str, msg_id: str) -> str | None:
        if (self.spool_dir / recipient_key / f"{msg_id}.json").exists():
            return "pending"
        for status in ARCHIVE_STATUSES:
            if (self.archive_dir / recipient_key / status / f"{msg_id}.json").exists():
                return status
        return None

    def was_processed(self, identity: Identity, msg_id: str) -> bool:
        base = self.archive_dir / agent_key(identity.host, identity.session_id)
        return any(
            (base / status / f"{msg_id}.json").exists()
            for status in ARCHIVE_STATUSES
        )

    def requeue_failed(self, identity: Identity) -> int:
        key = agent_key(identity.host, identity.session_id)
        failed_dir = self.archive_dir / key / "failed"
        if not failed_dir.exists():
            return 0
        pending_dir = self.spool_dir / key
        self._secure_directory(pending_dir)
        moved = 0
        with self._lock():
            for source in sorted(failed_dir.glob("*.json")):
                try:
                    payload = self._read_payload(source)
                except (OSError, AuthenticationError, ConfigurationError):
                    continue
                if payload.get("to_session") != identity.session_id:
                    continue
                target = pending_dir / source.name
                if target.exists():
                    source.unlink()
                else:
                    os.replace(source, target)
                moved += 1
            if moved:
                self._fsync_directory(failed_dir)
                self._fsync_directory(pending_dir)
        return moved

    def archive(self, source: Path, identity: Identity, status: str) -> None:
        if status not in ARCHIVE_STATUSES:
            raise StoreError(f"invalid archive status: {status}")
        target_dir = self.archive_dir / agent_key(identity.host, identity.session_id) / status
        self._secure_directory(target_dir)
        target = target_dir / source.name
        with self._lock():
            if target.exists():
                source.unlink(missing_ok=True)
                self._fsync_directory(source.parent)
            elif source.exists():
                os.replace(source, target)
                self._fsync_directory(source.parent)
                self._fsync_directory(target_dir)

    def _room_members_path(self, long_id: str) -> Path:
        return self.rooms_dir / f"{long_id}.members.json"

    def _room_log_path(self, long_id: str) -> Path:
        return self.rooms_dir / f"{long_id}.jsonl"

    def _iter_room_records(self) -> Iterator[tuple[str, dict[str, Any]]]:
        for path in sorted(self.rooms_dir.glob("*.members.json")):
            try:
                payload = self._read_payload(path)
            except (OSError, AuthenticationError, ConfigurationError):
                continue
            if isinstance(payload.get("room_id"), str) and isinstance(payload.get("members"), list):
                yield path.name[: -len(".members.json")], payload

    def online_member(self, agent_id_value: str) -> dict[str, Any] | None:
        for payload in self._iter_agent_payloads():
            if not self._is_online(payload):
                continue
            if (
                agent_id(str(payload["repo"]), str(payload["client"]), str(payload["host"]), int(payload["pid"]))
                == agent_id_value
            ):
                return payload
        return None

    def _append_room_line(self, long_id: str, payload: dict[str, Any]) -> None:
        line = (
            json.dumps(seal(payload, self.settings.auth_key), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        path = self._room_log_path(long_id)
        self._secure_directory(path.parent)
        with self._lock():
            if not self._room_members_path(long_id).exists():
                return  # the room was swept; do not resurrect its log
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(descriptor, line)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        self._require_secure_file(path)

    def room_append_event(
        self,
        long_id: str,
        kind: str,
        room_id: str,
        sender: Identity,
        title: str,
        text: str,
        msg_id: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "kind": kind,
            "room": room_id,
            "from": sender.address,
            "from_name": sender.name,
            "title": title,
            "text": text,
            "time": utc_now(),
        }
        if msg_id:
            payload["msg_id"] = msg_id
        self._append_room_line(long_id, payload)

    def gc_rooms(self, empty_ttl: timedelta = ROOM_EMPTY_TTL) -> list[dict[str, Any]]:
        """Sweep rooms whose online membership has been empty beyond empty_ttl.

        The first observation of emptiness stamps `empty_since` and keeps the
        room listed and rejoinable; once the stamp exceeds the ttl the members
        file is removed and the sealed transcript is moved — never deleted — to
        archive/rooms/ with a final `closed` event.
        """
        removed: list[dict[str, Any]] = []
        now = datetime.now(UTC)
        for long_id, payload in list(self._iter_room_records()):
            if any(self.online_member(str(member["agent_id"])) is not None for member in payload["members"]):
                continue
            empty_since = str(payload.get("empty_since") or "")
            if not empty_since:
                stamped = {**payload, "members": [], "empty_since": utc_now()}
                with self._lock():
                    self._write_json_atomic(
                        self._room_members_path(long_id),
                        seal(stamped, self.settings.auth_key),
                        overwrite=True,
                    )
                continue
            try:
                since = datetime.fromisoformat(empty_since)
            except ValueError:
                continue
            if since.tzinfo is None or now - since.astimezone(UTC) < empty_ttl:
                continue
            self._append_room_line(
                long_id,
                {
                    "kind": "closed",
                    "room": str(payload["room_id"]),
                    "from": "system@agent-collab",
                    "from_name": "agent-collab",
                    "title": "房间清理",
                    "text": (
                        f"room closed automatically: no online members since {empty_since} "
                        f"(ttl {int(empty_ttl.total_seconds())}s); transcript kept"
                    ),
                    "time": utc_now(),
                },
            )
            with self._lock():
                members_path = self._room_members_path(long_id)
                if not members_path.exists():
                    continue  # swept concurrently; nothing left to do
                rooms_archive = self.archive_dir / "rooms"
                self._secure_directory(rooms_archive)
                log_path = self._room_log_path(long_id)
                if log_path.exists():
                    os.replace(log_path, rooms_archive / log_path.name)
                    self._fsync_directory(rooms_archive)
                    self._fsync_directory(log_path.parent)
                members_path.unlink()
                self._fsync_directory(members_path.parent)
            removed.append({"room_id": str(payload["room_id"]), "long_id": long_id})
        return removed

    def room_create(self, creator: Identity) -> dict[str, Any]:
        existing_shorts = {str(payload["room_id"]) for _, payload in self._iter_room_records()}
        long_id = ""
        for _ in range(32):
            candidate = "".join(secrets.choice(ROOM_ALPHABET) for _ in range(ROOM_LONG_ID_LENGTH))
            if candidate[:ROOM_SHORT_ID_LENGTH] not in existing_shorts and not self._room_members_path(candidate).exists():
                long_id = candidate
                break
        if not long_id:
            raise StoreError("cannot allocate a unique room id")
        short_id = long_id[:ROOM_SHORT_ID_LENGTH]
        payload = {
            "room_id": short_id,
            "long_id": long_id,
            "created": utc_now(),
            "creator": creator.address,
            "members": [
                {
                    "agent_id": agent_id(creator.repo, creator.client, creator.host, creator.pid),
                    "address": creator.address,
                    "name": creator.name,
                    "joined": utc_now(),
                }
            ],
        }
        with self._lock():
            self._write_json_atomic(
                self._room_members_path(long_id),
                seal(payload, self.settings.auth_key),
                overwrite=False,
            )
        self.room_append_event(long_id, "created", short_id, creator, "房间创建", f"room created by {creator.address}")
        return {"room_id": short_id, "long_id": long_id}

    def room_resolve(self, room_id: str) -> tuple[str, dict[str, Any]]:
        normalized = room_id.strip().lstrip("#").lower()
        if len(normalized) == ROOM_LONG_ID_LENGTH:
            for long_id, payload in self._iter_room_records():
                if long_id == normalized:
                    return long_id, payload
            raise StoreError(
                f"room not found: #{normalized[:ROOM_SHORT_ID_LENGTH]} — this input looks like a 16-char "
                f"long id (a rooms/ filename, not the address). The canonical address is the 4-char id "
                f"and no live room shares this prefix; see the rooms table in find_coagents()"
            )
        if len(normalized) != ROOM_SHORT_ID_LENGTH or any(char not in ROOM_ALPHABET for char in normalized):
            raise StoreError(
                f"room id must be {ROOM_SHORT_ID_LENGTH} characters from the alphabet "
                f"\"{ROOM_ALPHABET}\" (e.g. \"nzzd\"), or an existing {ROOM_LONG_ID_LENGTH}-char long id; "
                f"see the rooms table in find_coagents()"
            )
        matches = [
            (long_id, payload)
            for long_id, payload in self._iter_room_records()
            if str(payload["room_id"]) == normalized
        ]
        if not matches:
            raise StoreError(
                f"room not found: #{normalized} — see the rooms table in find_coagents(); "
                "an empty room is swept automatically once it has had no online members for 2 hours"
            )
        if len(matches) > 1:
            raise StoreError(f"room id is ambiguous: {normalized}")
        return matches[0]

    def room_prune(self, long_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        kept = [member for member in payload["members"] if self.online_member(str(member["agent_id"])) is not None]
        if len(kept) != len(payload["members"]):
            payload = {**payload, "members": kept}
            with self._lock():
                self._write_json_atomic(
                    self._room_members_path(long_id),
                    seal(payload, self.settings.auth_key),
                    overwrite=True,
                )
        return payload

    def room_join(self, room_id: str, identity: Identity) -> dict[str, Any]:
        long_id, payload = self.room_resolve(room_id)
        payload = self.room_prune(long_id, payload)
        agent_id_value = agent_id(identity.repo, identity.client, identity.host, identity.pid)
        if any(str(member["agent_id"]) == agent_id_value for member in payload["members"]):
            return {"room_id": payload["room_id"], "long_id": long_id, "members": payload["members"], "already": True}
        payload = {
            **payload,
            "members": [
                *payload["members"],
                {
                    "agent_id": agent_id_value,
                    "address": identity.address,
                    "name": identity.name,
                    "joined": utc_now(),
                },
            ],
        }
        payload.pop("empty_since", None)  # a live member again — restart the grace clock
        with self._lock():
            self._write_json_atomic(
                self._room_members_path(long_id),
                seal(payload, self.settings.auth_key),
                overwrite=True,
            )
        self.room_append_event(long_id, "join", str(payload["room_id"]), identity, "成员加入", f"{identity.name} joined")
        return {"room_id": payload["room_id"], "long_id": long_id, "members": payload["members"], "already": False}

    def room_leave(self, room_id: str, identity: Identity) -> dict[str, Any]:
        long_id, payload = self.room_resolve(room_id)
        agent_id_value = agent_id(identity.repo, identity.client, identity.host, identity.pid)
        members = [member for member in payload["members"] if str(member["agent_id"]) != agent_id_value]
        if len(members) == len(payload["members"]):
            raise StoreError(f"not a member of room {payload['room_id']}")
        payload = {**payload, "members": members}
        with self._lock():
            self._write_json_atomic(
                self._room_members_path(long_id),
                seal(payload, self.settings.auth_key),
                overwrite=True,
            )
        self.room_append_event(long_id, "leave", str(payload["room_id"]), identity, "成员离开", f"{identity.name} left")
        return {"room_id": payload["room_id"], "long_id": long_id, "members": members}

    def rooms_of(self, identity: Identity) -> list[str]:
        agent_id_value = agent_id(identity.repo, identity.client, identity.host, identity.pid)
        return [
            str(payload["room_id"])
            for _, payload in self._iter_room_records()
            if any(str(member["agent_id"]) == agent_id_value for member in payload["members"])
        ]

    def send_room(
        self,
        sender: Identity,
        room_id: str,
        title: str,
        text: str,
        reply_to: str | None,
    ) -> dict[str, Any]:
        long_id, payload = self.room_resolve(room_id)
        payload = self.room_prune(long_id, payload)
        sender_agent_id = agent_id(sender.repo, sender.client, sender.host, sender.pid)
        if not any(str(member["agent_id"]) == sender_agent_id for member in payload["members"]):
            raise StoreError(f"not a member of room {payload['room_id']}")
        msg_id = str(uuid.uuid4())
        short_id = str(payload["room_id"])
        recipients: list[dict[str, str]] = []
        for member in payload["members"]:
            if str(member["agent_id"]) == sender_agent_id:
                continue
            online = self.online_member(str(member["agent_id"]))
            if online is None:
                continue
            self._deliver_single(
                sender,
                online,
                msg_id,
                title,
                text,
                reply_to,
                short_id,
            )
            recipients.append(
                {
                    "address": f"{online['name']}@{online['host']}",
                    "key": agent_key(str(online["host"]), str(online["session_id"])),
                    "agent_id": str(member["agent_id"]),
                }
            )
        self.room_append_event(long_id, "chat", short_id, sender, title, text, msg_id)
        return {"msg_id": msg_id, "room_id": short_id, "long_id": long_id, "recipients": recipients}

    def list_rooms(self) -> list[dict[str, Any]]:
        return [
            {
                "room_id": str(payload["room_id"]),
                "long_id": long_id,
                "created": payload.get("created", ""),
                "members": [
                    {
                        "agent_id": str(member.get("agent_id", "")),
                        "name": str(member.get("name", "")),
                        "address": str(member.get("address", "")),
                    }
                    for member in payload["members"]
                ],
            }
            for long_id, payload in self._iter_room_records()
        ]

    def inbox(
        self,
        identity: Identity,
        *,
        since: str | None,
        limit: int,
        message_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if message_id is not None:
            try:
                normalized_id = str(uuid.UUID(message_id))
            except (ValueError, AttributeError) as exc:
                raise StoreError("message_id must be a canonical UUID") from exc
            if normalized_id != message_id:
                raise StoreError("message_id must be a canonical UUID")
        key = agent_key(identity.host, identity.session_id)
        locations = [("pending", self.spool_dir / key)]
        locations.extend(
            (status, self.archive_dir / key / status)
            for status in ARCHIVE_STATUSES
        )
        found: list[dict[str, Any]] = []
        for status, directory in locations:
            if not directory.exists():
                continue
            paths = [directory / f"{message_id}.json"] if message_id else directory.glob("*.json")
            for path in paths:
                try:
                    payload = self._read_payload(path)
                except (OSError, AuthenticationError, ConfigurationError):
                    continue
                if message_id is None and since and str(payload.get("time", "")) <= since:
                    continue
                found.append({**payload, "status": status})
        found.sort(key=lambda item: str(item.get("time", "")), reverse=True)
        return found[:limit]
