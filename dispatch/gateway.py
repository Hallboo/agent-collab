#!/usr/bin/env python3
"""DISPATCH Feishu event gateway (resident process).

Replaces the old bash watch.sh listener chain and eliminates four classes
of recorded incidents:
- stdout pipe buffering held events until process exit (v1 latency up to
  1 hour) -> switched to --output-dir event files
- killing the listener on every event and relying on the agent to re-arm
  left a deaf window between events (v2 structural flaw) -> the gateway is
  resident, events only poke and never touch the listener: no deaf window,
  no re-arming
- pgrep/pkill string matching killed wrong processes, leftover
  subscriptions diverted events -> flock single-instance lock, process
  management only on its own children
- bash quoting/cwd/path-expansion traps -> exec with argument arrays, all
  paths derived from __file__

Responsibility: watch for new .json event files under events/ and poke the
tmux panel to wake the DISPATCH session; the listener itself is a
lark-cli event +subscribe subprocess restarted with backoff on death, and
a heartbeat pokes once per hour. supervisor.sh daemon spawns and keeps the
gateway alive, independent of the DISPATCH session's lifetime.
"""
from __future__ import annotations

import argparse
import fcntl
import logging
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

LOG = logging.getLogger("dispatch-gateway")

EVENT_POKE = (
    "New Feishu events landed on disk: process the .json files under "
    "dispatch/events/ one by one (parse the JSON, reply as needed, delete "
    "each file once handled). Listening is handled by the resident gateway; "
    "do not re-arm any listener."
)
HEARTBEAT_POKE = (
    "Gateway heartbeat: run one patrol round (find_coagents + inbox to "
    "settle overdue receipts), and handle any leftover .json files under "
    "dispatch/events/."
)

BACKOFF_CAP_SECONDS = 60.0
STABLE_RESET_SECONDS = 300.0


def discover_new(events_dir: Path, seen: set[Path]) -> list[Path]:
    """Return event files under events_dir not seen before, recording them in seen."""
    fresh: list[Path] = []
    for path in sorted(events_dir.glob("*.json")):
        if path not in seen:
            seen.add(path)
            fresh.append(path)
    return fresh


def tmux_poke(socket: str, session: str, text: str) -> bool:
    """Inject one instruction line plus Enter into a tmux panel; False if the session is absent."""
    base = ["tmux", "-L", socket]
    if subprocess.run(
        base + ["has-session", "-t", session],
        capture_output=True,
        check=False,
    ).returncode != 0:
        LOG.warning("tmux session %s not present; poke skipped", session)
        return False
    for argv in (
        base + ["send-keys", "-t", session, "-l", text],
        base + ["send-keys", "-t", session, "Enter"],
    ):
        if subprocess.run(argv, capture_output=True, check=False).returncode != 0:
            LOG.warning("tmux send-keys failed: %s", argv)
            return False
    return True


def next_backoff(failures: int) -> float:
    """Exponential backoff: starts at 1s, capped at 60s."""
    return min(2.0 ** max(failures - 1, 0), BACKOFF_CAP_SECONDS)


class Proc(Protocol):
    def poll(self) -> int | None: ...


class Gateway:
    """Listener-child supervision + event-file polling + heartbeat. tick() is single-step testable."""

    def __init__(
        self,
        events_dir: Path,
        socket: str,
        session: str,
        *,
        heartbeat_seconds: float = 3600.0,
        interval_seconds: float = 1.0,
        listener_factory: Callable[[], Proc] | None = None,
        poke: Callable[[str], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.events_dir = events_dir
        self.socket = socket
        self.session = session
        self.heartbeat_seconds = heartbeat_seconds
        self.interval_seconds = interval_seconds
        self._listener_factory = listener_factory or self._spawn_lark_cli
        self._poke = poke or (lambda text: tmux_poke(socket, session, text))
        self._clock = clock
        self.seen: set[Path] = set(events_dir.glob("*.json"))  # startup baseline, never replays old events
        self.listener: Proc | None = None
        self.failures = 0
        self.spawn_at = 0.0
        self.last_spawn = 0.0
        self.last_beat = self._clock()
        self.stopping = False

    def _spawn_lark_cli(self) -> Proc:
        lark = shutil.which("lark-cli")
        if lark is None:
            raise RuntimeError("lark-cli not found in PATH")
        LOG.info("listener starting: %s (output-dir %s)", lark, self.events_dir)
        return subprocess.Popen(
            [
                lark,
                "event",
                "+subscribe",
                "--event-types",
                "im.message.receive_v1",
                "--output-dir",
                ".",
                "--as",
                "bot",
            ],
            cwd=self.events_dir,
            stdout=subprocess.DEVNULL,
            stderr=None,  # status output goes to this process's stderr, kept by the supervisor's redirect
        )

    def tick(self) -> None:
        now = self._clock()
        # Listener supervision: dead -> count a failure, schedule restart on backoff; spawn when due
        if self.listener is not None and self.listener.poll() is not None:
            LOG.warning("listener exited rc=%s (failure #%d)", self.listener.poll(), self.failures + 1)
            self.listener = None
            self.failures += 1
            self.spawn_at = now + next_backoff(self.failures)
        if self.listener is None and now >= self.spawn_at:
            self.listener = self._listener_factory()
            self.last_spawn = now
            LOG.info("listener armed (failure count reset after spawn: %d)", self.failures)
        if self.listener is not None and self.failures and now - self.last_spawn >= STABLE_RESET_SECONDS:
            LOG.info("listener stable for %.0fs; failure count reset", STABLE_RESET_SECONDS)
            self.failures = 0
        # Events: new files -> poke once (batched); files are not deleted and the listener is not touched
        if discover_new(self.events_dir, self.seen):
            LOG.info("new event file(s); poking %s", self.session)
            self._poke(EVENT_POKE)
        # 心跳
        if now - self.last_beat >= self.heartbeat_seconds:
            self.last_beat = now
            LOG.info("heartbeat poke")
            self._poke(HEARTBEAT_POKE)

    def run(self) -> None:
        LOG.info(
            "gateway running: events=%s socket=%s session=%s heartbeat=%.0fs",
            self.events_dir, self.socket, self.session, self.heartbeat_seconds,
        )
        while not self.stopping:
            self.tick()
            time.sleep(self.interval_seconds)

    def stop(self) -> None:
        self.stopping = True
        listener = getattr(self.listener, "terminate", None)
        if callable(listener):
            listener()


def acquire_single_instance(lock_path: Path) -> int:
    """flock single instance: silently exit if already running (idempotent ensure); returns the lock fd."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        LOG.info("another gateway instance holds %s; exiting", lock_path)
        sys.exit(0)
    return fd


def main(argv: list[str] | None = None) -> int:
    default_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--events-dir", type=Path, default=default_dir / "events")
    parser.add_argument("--socket", default=os.environ.get("DISPATCH_TMUX_SOCKET", "dispatcher"))
    parser.add_argument("--session", default=os.environ.get("DISPATCH_SESSION", "dispatch"))
    parser.add_argument("--heartbeat", type=float, default=3600.0)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    args.events_dir.mkdir(parents=True, exist_ok=True)
    acquire_single_instance(args.events_dir / ".gateway.lock")
    gateway = Gateway(
        args.events_dir,
        args.socket,
        args.session,
        heartbeat_seconds=args.heartbeat,
    )

    def _stop(_signum: int, _frame: object) -> None:
        gateway.stop()

    import signal

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    gateway.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
