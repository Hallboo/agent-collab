from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _fake_client(path: Path, name: str) -> None:
    executable = path / name
    executable.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s %s\\n\' "$(basename "$0")" "$*" >> "$CALL_LOG"\n'
        'if [[ "${MCP_ALREADY_CONFIGURED:-0}" == 1 && "$2" == get ]]; then exit 0; fi\n'
        'if [[ "$2" == get ]]; then exit 1; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)


def test_configure_mcp_adds_both_clients(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _fake_client(fake_bin, "codex")
    _fake_client(fake_bin, "claude")
    env_file = tmp_path / "config.env"
    env_file.write_text("placeholder\n", encoding="utf-8")
    call_log = tmp_path / "calls.log"
    script = Path(__file__).resolve().parents[1] / "bin" / "configure-mcp"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "CALL_LOG": str(call_log),
        "AGENT_COLLAB_HOST": "dev-box",
        "COAGENT_CODEX_SKILLS_DIR": str(tmp_path / "codex-skills"),
        "COAGENT_CLAUDE_SKILLS_DIR": str(tmp_path / "claude-skills"),
    }

    result = subprocess.run(
        [str(script), str(env_file)],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    calls = call_log.read_text(encoding="utf-8").splitlines()
    launcher = script.parent / "agent-collab"
    assert calls == [
        "codex mcp get agent-collab",
        f"codex mcp add agent-collab -- {launcher} serve --env-file {env_file}",
        "claude mcp get agent-collab",
        (
            "claude mcp add --scope user agent-collab -- "
            f"{launcher} serve --env-file {env_file}"
        ),
    ]
    assert "Restart Codex/Claude sessions" in result.stdout
    skill_source = script.parents[1] / "skills" / "find-coagent"
    assert (tmp_path / "codex-skills" / "find-coagent").resolve() == skill_source
    assert (tmp_path / "claude-skills" / "find-coagent").resolve() == skill_source


def test_configure_mcp_replaces_existing_entries(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _fake_client(fake_bin, "codex")
    _fake_client(fake_bin, "claude")
    env_file = tmp_path / "config.env"
    env_file.write_text("placeholder\n", encoding="utf-8")
    call_log = tmp_path / "calls.log"
    script = Path(__file__).resolve().parents[1] / "bin" / "configure-mcp"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "CALL_LOG": str(call_log),
        "MCP_ALREADY_CONFIGURED": "1",
        "AGENT_COLLAB_HOST": "dev-box",
        "COAGENT_CODEX_SKILLS_DIR": str(tmp_path / "codex-skills"),
        "COAGENT_CLAUDE_SKILLS_DIR": str(tmp_path / "claude-skills"),
    }

    subprocess.run(
        [str(script), str(env_file)],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    launcher = script.parent / "agent-collab"
    assert call_log.read_text(encoding="utf-8").splitlines() == [
        "codex mcp get agent-collab",
        "codex mcp remove agent-collab",
        f"codex mcp add agent-collab -- {launcher} serve --env-file {env_file}",
        "claude mcp get agent-collab",
        "claude mcp remove --scope user agent-collab",
        (
            "claude mcp add --scope user agent-collab -- "
            f"{launcher} serve --env-file {env_file}"
        ),
    ]
