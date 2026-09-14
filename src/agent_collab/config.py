from __future__ import annotations

import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ENV_FILE = Path.home() / ".config" / "agent-collab" / "config.env"
DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "agent-collab"
ALLOWED_KEYS = {
    "AGENT_COLLAB_AUTH_KEY",
    "AGENT_COLLAB_STATE_DIR",
    "AGENT_COLLAB_GUIDE_PATH",
    "FEISHU_WEBHOOK_URL",
}


class ConfigurationError(RuntimeError):
    pass


def _parse_env_text(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ConfigurationError(f"invalid env line {line_number}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key not in ALLOWED_KEYS:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if not value:
            raise ConfigurationError(f"empty value for {key}")
        values[key] = value
    return values


def _require_secure_dir(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
        raise ConfigurationError(f"configuration parent is not a real directory: {path}")
    if info.st_uid != os.geteuid():
        raise ConfigurationError(f"configuration parent owner mismatch: {path}")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise ConfigurationError(f"configuration parent must have mode 0700: {path}")


def read_env_file(path: Path, *, require_secure: bool = True) -> dict[str, str]:
    path = path.expanduser()
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise ConfigurationError(f"env file is not a regular file: {path}")
    if require_secure:
        _require_secure_dir(path.parent)
        if info.st_uid != os.geteuid():
            raise ConfigurationError(f"env file owner mismatch: {path}")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise ConfigurationError(f"env file must have mode 0600: {path}")
    return _parse_env_text(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Settings:
    env_file: Path
    state_dir: Path
    auth_key: bytes
    feishu_webhook_url: str | None
    guide_path: str | None = None
    max_message_chars: int = 16_000
    send_limit_per_minute: int = 30
    send_wait_seconds: float = 3.0
    poll_seconds: float = 1.0
    heartbeat_seconds: float = 5.0
    remote_stale_seconds: float = 30.0

    @classmethod
    def load(cls, env_file: Path = DEFAULT_ENV_FILE) -> Settings:
        values = read_env_file(env_file)
        raw_key = values.get("AGENT_COLLAB_AUTH_KEY", "")
        if len(raw_key.encode("utf-8")) < 32:
            raise ConfigurationError("AGENT_COLLAB_AUTH_KEY must be at least 32 bytes")
        state_dir = Path(values.get("AGENT_COLLAB_STATE_DIR", str(DEFAULT_STATE_DIR))).expanduser()
        if not state_dir.is_absolute():
            raise ConfigurationError("AGENT_COLLAB_STATE_DIR must be absolute")
        guide_path = values.get("AGENT_COLLAB_GUIDE_PATH", "").strip()
        if len(guide_path) > 400 or any(ord(char) < 32 for char in guide_path):
            raise ConfigurationError("AGENT_COLLAB_GUIDE_PATH must be a short single-line path or URL")
        return cls(
            env_file=env_file,
            state_dir=state_dir,
            auth_key=raw_key.encode("utf-8"),
            feishu_webhook_url=values.get("FEISHU_WEBHOOK_URL"),
            guide_path=guide_path or None,
        )

    def contains_secret(self, text: str) -> bool:
        values = [
            self.auth_key.decode("utf-8"),
            self.feishu_webhook_url,
        ]
        return any(value and value in text for value in values)


def initialize_env(
    target: Path,
    source_env: Path | None = None,
    state_dir: Path = DEFAULT_STATE_DIR,
) -> dict[str, bool]:
    target = target.expanduser()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _require_secure_dir(target.parent)
    if target.exists() or target.is_symlink():
        raise ConfigurationError(f"refusing to overwrite existing env file: {target}")

    source_values: dict[str, str] = {}
    if source_env is not None:
        source_values = read_env_file(source_env.expanduser(), require_secure=False)

    state_dir = state_dir.expanduser()
    if not state_dir.is_absolute():
        raise ConfigurationError("state directory must be absolute")
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_info = state_dir.lstat()
    if state_info.st_uid != os.geteuid() or stat.S_IMODE(state_info.st_mode) != 0o700:
        raise ConfigurationError(f"state directory must be owned by current user with mode 0700: {state_dir}")

    lines = [
        f"AGENT_COLLAB_AUTH_KEY={secrets.token_urlsafe(32)}",
        f"AGENT_COLLAB_STATE_DIR={state_dir}",
    ]
    copied_webhook = False
    webhook = source_values.get("FEISHU_WEBHOOK_URL")
    if webhook:
        lines.append(f"FEISHU_WEBHOOK_URL={webhook}")
        copied_webhook = True

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(target, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return {"created": True, "copied_feishu_webhook": copied_webhook}
