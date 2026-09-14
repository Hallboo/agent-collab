from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import DEFAULT_ENV_FILE, Settings, initialize_env
from .identity import detect_identity
from .server import run_server


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-collab")
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="run the stdio MCP sidecar")
    serve.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    serve.add_argument("--host-name")

    init_env = commands.add_parser("init-env", help="create the secure operator env file")
    init_env.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    init_env.add_argument("--source-env", type=Path)
    init_env.add_argument("--state-dir", type=Path)

    doctor = commands.add_parser("doctor", help="validate configuration without printing secrets")
    doctor.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    doctor.add_argument("--host-name")
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command == "serve":
        run_server(args.env_file, host_name=args.host_name)
        return
    if args.command == "init-env":
        if args.state_dir is None:
            result = initialize_env(args.env_file, args.source_env)
        else:
            result = initialize_env(args.env_file, args.source_env, args.state_dir)
        print(json.dumps(result, sort_keys=True))
        return
    if args.command == "doctor":
        settings = Settings.load(args.env_file)
        identity = detect_identity(host_name=args.host_name)
        print(
            json.dumps(
                {
                    "configured": True,
                    "env_file": str(settings.env_file),
                    "state_dir": str(settings.state_dir),
                    "feishu_configured": bool(settings.feishu_webhook_url),
                    "identity": {
                        "name": identity.name,
                        "host": identity.host,
                        "client": identity.client,
                        "session_id": identity.session_id,
                        "repo": identity.repo,
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
