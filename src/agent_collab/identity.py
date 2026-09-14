from __future__ import annotations

import os
import re
import secrets
import socket
import subprocess
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path

SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
NAME_SUFFIX_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ"
CODEX_ROLLOUT = re.compile(
    r"^rollout-.*-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$"
)
CODEX_THREAD_LOCK = re.compile(
    r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.lock$"
)


class IdentityError(RuntimeError):
    pass


def process_start(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError as exc:
        raise IdentityError(f"client process {pid} is not available") from exc
    closing = raw.rfind(")")
    fields = raw[closing + 2 :].split()
    if closing < 0 or len(fields) <= 19:
        raise IdentityError(f"cannot read start time for process {pid}")
    return fields[19]


def process_is_alive(pid: int, expected_start: str) -> bool:
    try:
        return process_start(pid) == expected_start
    except IdentityError:
        return False


def _codex_session_from_process(pid: int) -> str | None:
    process_dir = Path(f"/proc/{pid}")
    try:
        if process_dir.stat().st_uid != os.geteuid():
            return None
        if (process_dir / "exe").resolve().name != "codex":
            return None
        codex_dir = (Path.home() / ".codex").resolve()
        sessions_dir = codex_dir / "sessions"
        locks_dir = codex_dir / "thread-writer-locks"
        candidates: set[str] = set()
        for descriptor in (process_dir / "fd").iterdir():
            try:
                target = descriptor.resolve()
            except OSError:
                continue
            match = None
            try:
                target.relative_to(sessions_dir)
                match = CODEX_ROLLOUT.fullmatch(target.name)
            except ValueError:
                try:
                    target.relative_to(locks_dir)
                    match = CODEX_THREAD_LOCK.fullmatch(target.name)
                except ValueError:
                    pass
            if match:
                candidates.add(match.group(1))
    except OSError:
        return None
    if len(candidates) == 1:
        return candidates.pop()
    return None


def _codex_identity_from_parent() -> tuple[str, int] | None:
    pid = os.getppid()
    session_id = _codex_session_from_process(pid)
    return (session_id, pid) if session_id is not None else None


def _repo_for(cwd: Path) -> tuple[str, str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
        root = Path(result.stdout.strip()).resolve()
        return str(root), root.name
    except (OSError, subprocess.SubprocessError):
        resolved = cwd.resolve()
        return str(resolved), resolved.name


def _model_abbrev(model: str) -> str:
    text = model.strip().strip("\"'").lower()
    tokens = [token for token in re.split(r"[^a-z0-9.]+", text) if token]
    if not tokens:
        return ""
    name = tokens[1] if tokens[0] == "claude" and len(tokens) > 1 else tokens[0]
    if not name.isalpha():
        name = next((token for token in tokens if token.isalpha()), "")
    if not name:
        return ""
    remainder = text.split(name, 1)[1]
    version = re.search(r"\d+(?:\.\d+)?", remainder)
    version_digits = version.group(0).replace(".", "") if version else ""
    return (f"{name}{version_digits}")[:12]


def _default_name(client: str, model: str) -> str:
    abbrev = _model_abbrev(model) or ("cc" if client == "cc" else client)
    suffix = secrets.choice(NAME_SUFFIX_ALPHABET)
    return f"{abbrev.upper()}-{suffix}"


def _clean_name(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 64 or "@" in value or any(ord(char) < 32 for char in value):
        raise IdentityError("name must be 1-64 visible characters and cannot contain @")
    return value


def _clean_role(value: str) -> str:
    value = value.strip()
    if len(value) > 160 or any(ord(char) < 32 for char in value):
        raise IdentityError("role must be at most 160 visible characters")
    return value


def _clean_model(value: str | None) -> str:
    if value is None:
        return ""
    value = value.strip().strip("\"'")
    if len(value) > 160 or any(ord(char) < 32 for char in value):
        return ""
    return value


def _process_arguments(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode(errors="replace") for part in raw.split(b"\0") if part]


def _argument_model(client: str, pid: int) -> str:
    arguments = _process_arguments(pid)
    expected_executable = "claude" if client == "cc" else client
    if not arguments or Path(arguments[0]).name != expected_executable:
        return ""
    for index, argument in enumerate(arguments):
        if argument in {"--model", "-m"} and index + 1 < len(arguments):
            return _clean_model(arguments[index + 1])
        if argument.startswith("--model="):
            return _clean_model(argument.split("=", 1)[1])
        if client == "codex" and argument in {"--config", "-c"} and index + 1 < len(arguments):
            key, separator, value = arguments[index + 1].partition("=")
            if separator and key.strip() == "model":
                return _clean_model(value)
    return ""


def _codex_config_model() -> str:
    try:
        data = tomllib.loads((Path.home() / ".codex" / "config.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    value = data.get("model")
    return _clean_model(value if isinstance(value, str) else None)


def _detect_model(client: str, pid: int, env: Mapping[str, str]) -> str:
    explicit = _clean_model(env.get("AGENT_COLLAB_MODEL"))
    if explicit:
        return explicit
    argument_model = _argument_model(client, pid)
    if argument_model:
        return argument_model
    if client == "cc":
        return _clean_model(env.get("ANTHROPIC_MODEL") or env.get("CLAUDE_MODEL"))
    if client == "codex":
        return _clean_model(env.get("CODEX_MODEL")) or _codex_config_model()
    return ""


@dataclass(frozen=True)
class Identity:
    name: str
    role: str
    cwd: str
    repo: str
    host: str
    client: str
    session_id: str
    pid: int
    process_start: str
    model: str = ""
    name_generated: bool = False

    @property
    def address(self) -> str:
        return f"{self.name}@{self.host}"

    @property
    def instance(self) -> str:
        return f"{self.pid}:{self.process_start}"

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    def with_identity(self, *, role: str, name: str | None = None) -> Identity:
        return replace(
            self,
            name=_clean_name(name) if name is not None else self.name,
            role=_clean_role(role),
            name_generated=False if name is not None else self.name_generated,
        )


def detect_identity(host_name: str | None = None) -> Identity:
    env = os.environ
    if env.get("CLAUDE_CODE_SESSION_ID"):
        client = "cc"
        session_id = env["CLAUDE_CODE_SESSION_ID"]
        pid = int(env.get("CLAUDE_PID", os.getppid()))
    elif env.get("CODEX_THREAD_ID") or env.get("CODEX_SESSION_ID"):
        client = "codex"
        session_id = env.get("CODEX_THREAD_ID") or env["CODEX_SESSION_ID"]
        pid = int(env.get("AGENT_COLLAB_CLIENT_PID", os.getppid()))
    elif env.get("AGENT_COLLAB_CLIENT") and env.get("AGENT_COLLAB_SESSION_ID"):
        client = env["AGENT_COLLAB_CLIENT"]
        session_id = env["AGENT_COLLAB_SESSION_ID"]
        pid = int(env.get("AGENT_COLLAB_CLIENT_PID", os.getppid()))
    elif codex_identity := _codex_identity_from_parent():
        client = "codex"
        session_id, pid = codex_identity
    else:
        raise IdentityError("not running inside a supported cc or codex session")

    if not SAFE_ID.fullmatch(session_id):
        raise IdentityError("client session ID contains unsupported characters")
    cwd = Path(env.get("AGENT_COLLAB_CWD", os.getcwd()))
    repo_root, repo_name = _repo_for(cwd)
    host = (host_name or env.get("AGENT_COLLAB_HOST") or socket.gethostname()).strip()
    if not SAFE_ID.fullmatch(host):
        raise IdentityError("host name contains unsupported characters")
    model = _detect_model(client, pid, env)
    explicit_name = env.get("AGENT_COLLAB_NAME", "")
    name = _clean_name(explicit_name or _default_name(client, model))
    role = _clean_role(env.get("AGENT_COLLAB_ROLE", ""))
    return Identity(
        name=name,
        role=role,
        cwd=repo_root,
        repo=repo_name,
        host=host,
        client=client,
        session_id=session_id,
        pid=pid,
        process_start=process_start(pid),
        model=model,
        name_generated=not explicit_name,
    )
