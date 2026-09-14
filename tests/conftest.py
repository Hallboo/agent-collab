from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_collab.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    env_dir = tmp_path / "envs"
    env_dir.mkdir(mode=0o700)
    env_file = env_dir / "agent-collab.env"
    state_dir = tmp_path / "state"
    env_file.write_text(
        "AGENT_COLLAB_AUTH_KEY=" + "k" * 48 + "\n" + f"AGENT_COLLAB_STATE_DIR={state_dir}\n",
        encoding="utf-8",
    )
    os.chmod(env_file, 0o600)
    return Settings.load(env_file)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
