from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_collab.config import ConfigurationError, Settings, initialize_env


def test_rejects_missing_auth_key(tmp_path: Path) -> None:
    env_dir = tmp_path / "envs"
    env_dir.mkdir(mode=0o700)
    env_file = env_dir / "config.env"
    env_file.write_text("AGENT_COLLAB_STATE_DIR=/tmp/state\n", encoding="utf-8")
    os.chmod(env_file, 0o600)
    with pytest.raises(ConfigurationError, match="at least 32 bytes"):
        Settings.load(env_file)


def test_rejects_world_readable_env(tmp_path: Path) -> None:
    env_dir = tmp_path / "envs"
    env_dir.mkdir(mode=0o700)
    env_file = env_dir / "config.env"
    env_file.write_text("AGENT_COLLAB_AUTH_KEY=" + "x" * 48 + "\n", encoding="utf-8")
    os.chmod(env_file, 0o644)
    with pytest.raises(ConfigurationError, match="0600"):
        Settings.load(env_file)


def test_initializes_without_exposing_secret(tmp_path: Path) -> None:
    source = tmp_path / "source.env"
    source.write_text("FEISHU_WEBHOOK_URL=https://example.invalid/hook\n", encoding="utf-8")
    target = tmp_path / "secure" / "config.env"
    state_dir = tmp_path / "state"
    result = initialize_env(target, source, state_dir)
    assert result == {"created": True, "copied_feishu_webhook": True}
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.parent.stat().st_mode & 0o777 == 0o700
    loaded = Settings.load(target)
    assert loaded.feishu_webhook_url == "https://example.invalid/hook"
    assert loaded.state_dir == state_dir


def test_loads_optional_guide_path(tmp_path: Path) -> None:
    env_dir = tmp_path / "envs"
    env_dir.mkdir(mode=0o700)
    env_file = env_dir / "config.env"
    env_file.write_text(
        "AGENT_COLLAB_AUTH_KEY=" + "x" * 48 + "\n"
        "AGENT_COLLAB_GUIDE_PATH=/shared/agent-collab/docs/interaction-guide.md\n",
        encoding="utf-8",
    )
    os.chmod(env_file, 0o600)
    loaded = Settings.load(env_file)
    assert loaded.guide_path == "/shared/agent-collab/docs/interaction-guide.md"


def test_rejects_multiline_guide_path(tmp_path: Path) -> None:
    env_dir = tmp_path / "envs"
    env_dir.mkdir(mode=0o700)
    env_file = env_dir / "config.env"
    env_file.write_text(
        "AGENT_COLLAB_AUTH_KEY=" + "x" * 48 + "\n"
        "AGENT_COLLAB_GUIDE_PATH=/shared/guide\tonline\n",
        encoding="utf-8",
    )
    os.chmod(env_file, 0o600)
    with pytest.raises(ConfigurationError, match="single-line"):
        Settings.load(env_file)
