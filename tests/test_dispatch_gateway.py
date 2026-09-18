from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]


def _load_gateway():
    spec = importlib.util.spec_from_file_location(
        "dispatch_gateway.gateway", REPO / "dispatch" / "gateway.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


gateway = _load_gateway()


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeListener:
    def __init__(self, rc: int | None = None) -> None:
        self.rc = rc

    def poll(self) -> int | None:
        return self.rc


def test_discover_new_reports_each_file_once(tmp_path: Path) -> None:
    seen: set[Path] = set()
    first = tmp_path / "a.json"
    first.write_text("{}", encoding="utf-8")
    assert gateway.discover_new(tmp_path, seen) == [first]
    assert gateway.discover_new(tmp_path, seen) == []
    second = tmp_path / "b.json"
    second.write_text("{}", encoding="utf-8")
    assert gateway.discover_new(tmp_path, seen) == [second]


def test_next_backoff_grows_and_caps() -> None:
    assert gateway.next_backoff(1) == 1.0
    assert gateway.next_backoff(2) == 2.0
    assert gateway.next_backoff(3) == 4.0
    assert gateway.next_backoff(99) == gateway.BACKOFF_CAP_SECONDS


def test_tmux_poke_sends_text_then_enter(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(gateway.subprocess, "run", fake_run)
    assert gateway.tmux_poke("dispatcher", "dispatch", "hello") is True
    assert calls == [
        ["tmux", "-L", "dispatcher", "has-session", "-t", "dispatch"],
        ["tmux", "-L", "dispatcher", "send-keys", "-t", "dispatch", "-l", "hello"],
        ["tmux", "-L", "dispatcher", "send-keys", "-t", "dispatch", "Enter"],
    ]


def test_tmux_poke_skips_when_session_missing(monkeypatch) -> None:
    def fake_run(argv, **_kwargs):
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(gateway.subprocess, "run", fake_run)
    assert gateway.tmux_poke("dispatcher", "dispatch", "hello") is False


def test_gateway_pokes_on_new_event_file_once(tmp_path: Path) -> None:
    clock = FakeClock()
    pokes: list[str] = []
    gateway_obj = gateway.Gateway(
        tmp_path,
        "dispatcher",
        "dispatch",
        listener_factory=lambda: FakeListener(),
        poke=lambda text: pokes.append(text) or True,
        clock=clock,
    )
    gateway_obj.tick()  # 初始拉起监听,无事件
    (tmp_path / "evt.json").write_text("{}", encoding="utf-8")
    gateway_obj.tick()
    gateway_obj.tick()
    assert pokes == [gateway.EVENT_POKE]


def test_gateway_restarts_dead_listener_with_backoff(tmp_path: Path) -> None:
    clock = FakeClock()
    spawns: list[int] = []
    dead = FakeListener(rc=1)

    def factory() -> FakeListener:
        spawns.append(int(clock.now))
        return dead

    gateway_obj = gateway.Gateway(
        tmp_path,
        "dispatcher",
        "dispatch",
        listener_factory=factory,
        poke=lambda _text: True,
        clock=clock,
    )
    gateway_obj.tick()  # 首次拉起(t=1000)
    dead.rc = 1
    gateway_obj.tick()  # 发现死亡:failure=1,退避 1s 后才重启
    clock.now += 0.5
    gateway_obj.tick()  # 未到退避点,不重启
    assert len(spawns) == 1
    clock.now += 1.0
    gateway_obj.tick()  # 到点重启
    assert len(spawns) == 2


def test_gateway_heartbeats_on_schedule(tmp_path: Path) -> None:
    clock = FakeClock()
    pokes: list[str] = []
    gateway_obj = gateway.Gateway(
        tmp_path,
        "dispatcher",
        "dispatch",
        heartbeat_seconds=3600.0,
        listener_factory=lambda: FakeListener(),
        poke=lambda text: pokes.append(text) or True,
        clock=clock,
    )
    clock.now += 3599.0
    gateway_obj.tick()
    assert pokes == []
    clock.now += 2.0
    gateway_obj.tick()
    assert pokes == [gateway.HEARTBEAT_POKE]
