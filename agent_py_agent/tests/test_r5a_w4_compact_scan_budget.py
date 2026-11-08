"""统计真实 DirEntry 消费与句柄生命周期，不以业务检查次数冒充目录读取预算。"""
from __future__ import annotations

import os
import shutil
import threading
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import compact_operations as compact


@pytest.fixture
def scans(monkeypatch):
    original = os.scandir
    monkeypatch.setattr(compact, "time", SimpleNamespace(time=time.time, monotonic=lambda: 0.0))
    state = SimpleNamespace(entries=0, opened=0, closed=0, live=set(), names=[])

    class Scan:
        def __init__(self, directory):
            self.inner = original(directory)
            state.opened += 1
            state.live.add(self)

        def __iter__(self):
            return self

        def __next__(self):
            entry = next(self.inner)
            state.entries += 1
            state.names.append(entry.name)
            return entry

        def close(self):
            if self.inner is not None:
                self.inner.close()
                self.inner = None
                state.closed += 1
                state.live.remove(self)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()

    monkeypatch.setattr(os, "scandir", Scan)
    state.scan_type = Scan
    yield state
    for stream in tuple(state.live):
        stream.close()


def _agent(root, *, entries=13, maximum=2):
    directory = root / "threads"
    directory.mkdir(parents=True)
    store = SimpleNamespace(storage=SimpleNamespace(threads_dir=directory))
    store.threads = SimpleNamespace(load_report=lambda tid: (
        SimpleNamespace(thread_id=tid, owner_home=str(root)), None,
    ))
    return SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=root), conversation_store=store,
        config=SimpleNamespace(memory_compact_idle_max_per_tick=maximum,
                               retention_scan_slice_entries=entries, retention_scan_slice_ms=25,
                               owner_maintenance_scan_interval_seconds=60),
    )


def _observe(monkeypatch):
    seen = []

    def inspect(agent, store, thread, **kwargs):
        seen.append(thread.thread_id)
        return {"compacted": False}

    monkeypatch.setattr(compact, "maybe_auto_compact_idle_session", inspect)
    return seen


def _release(agent, cursor):
    agent.home_paths.owner_home_dir = None
    compact.idle_compact_maintenance_tick(agent, cursor=cursor)


def _fd_count():
    directory = Path("/dev/fd") if Path("/dev/fd").is_dir() else Path("/proc/self/fd")
    return len(os.listdir(directory)) if directory.is_dir() else None


def test_actual_entry_budget_and_eventual_coverage_of_ten_thousand_threads(tmp_path, monkeypatch, scans):
    agent = _agent(tmp_path)
    directory = agent.conversation_store.storage.threads_dir
    expected = {f"thread-{number:08d}" for number in range(10_000)}
    for name in expected:
        (directory / f"{name}.json").touch()
    for number in range(31):
        (directory / f"thread-noise-{number}.json.bak").touch()
    seen, cursor = _observe(monkeypatch), {}
    for _ in range(10_100):
        before = scans.entries
        report = compact.idle_compact_maintenance_tick(agent, cursor=cursor)
        assert scans.entries - before <= agent.config.retention_scan_slice_entries
        assert report["checked"] <= agent.config.memory_compact_idle_max_per_tick
        if len(seen) == len(expected):
            break
    assert set(seen) == expected and len(seen) == len(expected)
    _release(agent, cursor)
    assert not scans.live and scans.opened == scans.closed


@pytest.mark.parametrize("budget", [1, 7, 128])
def test_noise_consumes_the_budget_and_resumes_without_reopening(tmp_path, monkeypatch, scans, budget):
    agent = _agent(tmp_path, entries=budget)
    directory = agent.conversation_store.storage.threads_dir
    for number in range(10_000):
        (directory / f"thread-{number:08d}.json.bak").touch()
    seen, cursor = _observe(monkeypatch), {}
    for turn in range(2):
        assert compact.idle_compact_maintenance_tick(agent, cursor=cursor)["checked"] == 0
        assert scans.entries == (turn + 1) * budget
        assert scans.opened == 1
    assert len(set(scans.names)) == 2 * budget and not seen
    _release(agent, cursor)
    assert not scans.live


def test_directory_mtime_churn_does_not_restart_the_prefix(tmp_path, monkeypatch, scans):
    agent = _agent(tmp_path, maximum=1)
    directory = agent.conversation_store.storage.threads_dir
    for number in range(24):
        (directory / f"thread-{number:08d}.json").touch()
    seen, cursor = _observe(monkeypatch), {}
    timestamp = directory.stat().st_mtime_ns
    for turn in range(24):
        os.utime(directory, ns=(timestamp + turn + 1, timestamp + turn + 1))
        compact.idle_compact_maintenance_tick(agent, cursor=cursor)
    assert len(seen) == len(set(seen)) == 24
    assert scans.opened == 1
    _release(agent, cursor)


def test_time_budget_is_checked_before_consuming_an_entry(tmp_path, monkeypatch, scans):
    agent = _agent(tmp_path)
    (agent.conversation_store.storage.threads_dir / "thread-one.json").touch()
    clock = iter((0.0, 1.0))
    monkeypatch.setattr(compact, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    seen, cursor = _observe(monkeypatch), {}
    assert compact.idle_compact_maintenance_tick(agent, cursor=cursor)["checked"] == 0
    assert scans.entries == scans.opened == 0 and not seen
    _release(agent, cursor)


def test_owner_departure_retires_only_departed_directory_handles(tmp_path, monkeypatch, scans):
    base, owner = _agent(tmp_path / "base"), _agent(tmp_path / "owner")
    for agent in (base, owner):
        for number in range(4):
            (agent.conversation_store.storage.threads_dir / f"thread-{number}.json").touch()
    active, pinned = [owner], []

    def pin(agent, *, touch):
        assert agent is owner and touch is False
        pinned.append(agent)
        return nullcontext()

    base._owner_pool = SimpleNamespace(active_agents=lambda: active, pin=pin)
    _observe(monkeypatch)
    cursor, before = {}, _fd_count()
    compact.idle_compact_maintenance_tick(base, cursor=cursor)
    assert len(scans.live) == 2 and pinned == [owner]
    active.clear()
    compact.idle_compact_maintenance_tick(base, cursor=cursor)
    assert len(scans.live) == 1
    _release(base, cursor)
    assert not scans.live and scans.opened == scans.closed
    assert _fd_count() == before


def test_missing_and_replaced_thread_directories_recover_without_old_entries(tmp_path, monkeypatch, scans):
    agent = _agent(tmp_path, maximum=1)
    directory = agent.conversation_store.storage.threads_dir
    for number in range(3):
        (directory / f"thread-old-{number}.json").touch()
    seen, cursor = _observe(monkeypatch), {}
    before = _fd_count()
    compact.idle_compact_maintenance_tick(agent, cursor=cursor)
    old = tmp_path / "retired"
    directory.rename(old)
    directory.mkdir()
    (directory / "thread-new.json").touch()
    compact.idle_compact_maintenance_tick(agent, cursor=cursor)
    assert seen[-1] == "thread-new" and scans.closed == 1
    shutil.rmtree(directory)
    assert compact.idle_compact_maintenance_tick(agent, cursor=cursor)["checked"] == 0
    assert not scans.live and _fd_count() == before
    directory.mkdir()
    (directory / "thread-restored.json").touch()
    compact.idle_compact_maintenance_tick(agent, cursor=cursor)
    assert seen[-1] == "thread-restored"
    _release(agent, cursor)
    assert not scans.live and _fd_count() == before


@pytest.mark.parametrize("failure", ["stat", "read"])
def test_cursor_errors_close_the_underlying_directory(tmp_path, monkeypatch, scans, failure):
    from agent_py_agent.agent.io.bounded_directory import DirectoryCursor

    (tmp_path / "first").touch()
    (tmp_path / "second").touch()
    cursor, before = DirectoryCursor(), _fd_count()
    assert cursor.next_entry(tmp_path) is not None
    assert len(scans.live) == 1

    def fail(*args, **kwargs):
        raise OSError("injected directory read failure")

    if failure == "stat":
        original = Path.stat

        def stat(path, **kwargs):
            return fail() if path == tmp_path else original(path, **kwargs)

        monkeypatch.setattr(Path, "stat", stat)
    else:
        monkeypatch.setattr(scans.scan_type, "__next__", fail)
    with pytest.raises(OSError, match="injected directory"):
        cursor.next_entry(tmp_path)
    assert not scans.live and _fd_count() == before
    cursor.close()


def test_exhausted_cursor_closes_and_reopens_for_new_files(tmp_path, scans):
    from agent_py_agent.agent.io.bounded_directory import DirectoryCursor

    cursor, before = DirectoryCursor(), _fd_count()
    (tmp_path / "first").touch()
    assert cursor.next_entry(tmp_path).name == "first"
    assert cursor.next_entry(tmp_path) is None
    assert not scans.live and _fd_count() == before
    (tmp_path / "second").touch()
    assert {cursor.next_entry(tmp_path).name, cursor.next_entry(tmp_path).name} == {"first", "second"}
    cursor.close()
    assert not scans.live and _fd_count() == before


@pytest.mark.parametrize("raises", [False, True])
def test_controller_exit_and_error_close_its_compact_cursors(tmp_path, monkeypatch, scans, raises):
    from agent_py_agent.cli import gateway_loops

    agent = _agent(tmp_path)
    for number in range(4):
        (agent.conversation_store.storage.threads_dir / f"thread-{number}.json").touch()
    _observe(monkeypatch)
    controller = gateway_loops._GatewayOwnerMaintenanceController(SimpleNamespace(agent=agent))
    before = _fd_count()

    def run(current, stop, site):
        compact.idle_compact_maintenance_tick(agent, cursor=current._idle_compact_cursor)
        assert scans.live
        if raises:
            raise RuntimeError("injected controller exit")

    monkeypatch.setattr(gateway_loops, "_run_hinted_controller", run)
    if raises:
        with pytest.raises(RuntimeError, match="injected controller"):
            controller.run(threading.Event())
    else:
        controller.run(threading.Event())
    assert not scans.live and _fd_count() == before
    assert not controller._idle_compact_cursor.get("directories")


def test_scan_still_rejects_wrong_owner_records(tmp_path, monkeypatch, scans):
    agent = _agent(tmp_path, maximum=1)
    (agent.conversation_store.storage.threads_dir / "thread-other.json").touch()
    agent.conversation_store.threads.load_report = lambda tid: (
        SimpleNamespace(thread_id=tid, owner_home="different-owner"), None,
    )
    seen, cursor = _observe(monkeypatch), {}
    assert compact.idle_compact_maintenance_tick(agent, cursor=cursor) == {
        "checked": 1, "compacted": 0, "failed": 1,
    }
    assert not seen
    _release(agent, cursor)


def test_missing_thread_metadata_loads_and_normal_activity_repairs_it(tmp_path, monkeypatch, scans):
    import json

    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.settings.config import AgentConfig

    agent = _agent(tmp_path / "owner")
    agent.config = AgentConfig()
    agent.home_paths.root = tmp_path / "owner"
    agent.home_paths.owner_provider = "feishu"
    agent.home_paths.owner_kind = "user"
    agent.home_paths.owner_id = "fixture"
    store = agent.conversation_store = ConversationStore(tmp_path / "owner" / "conversations")
    thread = store.threads.create(canonical_user_id="fixture")
    path = store.storage.thread_path(thread.thread_id)
    value = json.loads(path.read_text())
    value.pop("metadata", None)
    path.write_text(json.dumps(value), encoding="utf-8")
    monkeypatch.setattr(compact, "idle_compact_busy_state", lambda *_: "foreground_active")
    monkeypatch.setattr(compact, "prepare_conversation_context", lambda *_a, **_k: pytest.fail("busy thread compacted"))
    cursor = {}
    result = compact.idle_compact_maintenance_tick(agent, cursor=cursor, now=thread.updated_at + 10_000)
    assert result == {"checked": 1, "compacted": 0, "failed": 0}
    repaired = store.threads.record_user_activity(thread.thread_id, request_id="new-input")
    assert repaired.metadata["user_activity_generation"] == 1
    _release(agent, cursor)
