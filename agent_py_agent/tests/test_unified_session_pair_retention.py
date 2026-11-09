"""从原owner维护进入，清理的不是内存dict，也不是count的隐藏副作用。"""
from __future__ import annotations

import json
import os
import time

import pytest

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.session_pair_rate import WINDOW_SECONDS
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.agent.user_space.owner_maintenance import run_owner_retention_if_due
from agent_py_agent.agent.user_space.runtime_paths import runtime_paths_for_agent


def _fixture(root):
    home = ensure_my_agent_home(root)
    paths = runtime_paths_for_agent(None, home.owner_home_dir, home)
    store = ConversationStore(paths["conversation_workspace"])
    return home, store.session_pair_rate


def _seed(limiter, sender, now):
    limiter.record(sender, "target", now=now)
    path = limiter._path(sender, "target")
    os.utime(path.with_suffix(".json.lock"), (1, 1))
    return path


def _maintain(home, now):
    result = run_owner_retention_if_due(home, now=now, quota_max_entries=128, quota_max_ms=100)
    state = json.loads((home.owner_data_dir / "maintenance.json").read_text())
    return result, state


@pytest.fixture
def sidecar_first_pair_scandir(monkeypatch):
    """只调整指定目录的真实条目顺序，保留可移植枚举器的迭代与关闭合同。"""
    from agent_py_agent.agent.user_space import owner_quota

    original = owner_quota._quota_scandir
    opened = []

    class OrderedEntries:
        def __init__(self, descriptor):
            with os.scandir(descriptor) as entries:
                self.entries = iter(sorted(entries, key=lambda entry: (
                    not entry.name.endswith(".json.lock"), entry.name)))
            self.seen = []

        def __iter__(self):
            return self

        def __next__(self):
            entry = next(self.entries)
            self.seen.append(entry.name)
            return entry

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            self.close()

        def close(self):
            self.entries = iter(())

    def install(directory):
        info = directory.stat()
        identity = (info.st_dev, info.st_ino)

        def scandir(descriptor):
            current = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) != identity:
                return original(descriptor)
            entries = OrderedEntries(descriptor)
            opened.append(entries)
            return entries

        monkeypatch.setattr(owner_quota, "_quota_scandir", scandir)
        return opened

    try:
        yield install
    finally:
        for entries in opened:
            entries.close()


def test_owner_maintenance_removes_expired_bucket_and_old_sidecar_not_live_window(tmp_path):
    home, limiter = _fixture(tmp_path / "host")
    now = time.time()
    expired = _seed(limiter, "expired", now - WINDOW_SECONDS * 4)
    live = _seed(limiter, "live", now)
    assert limiter.count("expired", "target", now=now) == 0
    assert expired.exists(), "count必须是纯查询"
    _result, state = _maintain(home, now)
    for _ in range(8):
        if not expired.exists():
            break
        _result, state = _maintain(home, now + 1)
    assert not expired.exists()
    assert not expired.with_suffix(".json.lock").exists()
    assert live.exists() and limiter.count("live", "target", now=now) == 1
    assert "session_pair_rate" in state["storage_maintenance"]["cursors"]


@pytest.mark.parametrize("hold", [False, True])
def test_grace_and_legal_hold_preserve_files(tmp_path, hold):
    home, limiter = _fixture(tmp_path / "host")
    now = time.time()
    recent = _seed(limiter, "grace", now - WINDOW_SECONDS)
    old = _seed(limiter, "old", now - WINDOW_SECONDS * 4)
    if hold:
        policy = json.loads(home.owner_retention_json.read_text())
        policy["legal_hold"] = True
        home.owner_retention_json.write_text(json.dumps(policy))
    _maintain(home, now)
    # 维护按预算分片；慢机器可能首拍尚未扫到旧桶，沿原游标有界续作，不放宽保留断言。
    for step in range(1, 9):
        if hold or not old.exists():
            break
        _maintain(home, now + step)
    assert recent.exists()
    assert old.exists() is hold


@pytest.mark.parametrize("sidecars_first", [False, True], ids=["native", "sidecars-first-portable"])
def test_cold_restart_maintenance_cursor_drains_over_128_without_recursing_other_data(
        tmp_path, request, sidecar_first_pair_scandir, sidecars_first):
    home, limiter = _fixture(tmp_path / "host")
    now = time.time()
    old = [_seed(limiter, f"old-{index}", now - WINDOW_SECONDS * 4) for index in range(180)]
    live = _seed(limiter, "live", now)
    sentinel = home.owner_home_dir / "unregistered/session_pair_rate/protected.json"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("keep")
    streams = sidecar_first_pair_scandir(old[0].parent) if sidecars_first else []
    removed, observations = set(), []

    def check_slice(result, state):
        nonlocal removed
        assert result.ran
        storage = state["storage_maintenance"]
        assert storage["errors"] == []
        # state来自本次维护之后的磁盘JSON，不能拿上次的内存游标代替本次回执。
        cursor = storage["cursors"].get("session_pair_rate")
        current = {path for path in old if not path.exists()}
        assert removed <= current
        assert 0 <= len(current - removed) <= 128
        removed = current
        if cursor is not None:
            for field in ("items", "deleted"):
                assert type(cursor["last_slice"][field]) is int
                assert 0 <= cursor["last_slice"][field] <= 128
        if len(removed) < len(old):
            assert cursor is None or cursor["complete"] is False
        if cursor is None or cursor["complete"] is False:
            assert "session_pair_rate" in storage["pending_kinds"]
            assert storage["continuation_pending"] is True
            assert state["maintenance_partial"] is True
        assert live.exists() and limiter.count("live", "target", now=now) == 1
        assert sentinel.read_text() == "keep"
        observations.append({"removed": len(removed), "cursor": cursor,
                             "pending": "session_pair_rate" in storage["pending_kinds"]})
        return cursor

    _result, state = _maintain(home, now)
    cursor = check_slice(_result, state)
    assert 0 <= sum(not path.exists() for path in old) <= 128
    assert state["maintenance_partial"] is True
    if sidecars_first:
        assert not removed
    for _ in range(16):
        _result, state = _maintain(home, now + 1)
        cursor = check_slice(_result, state)
        if not any(path.exists() for path in old) and cursor is not None and cursor["complete"]:
            break
    assert not any(path.exists() for path in old)
    assert cursor is not None and cursor["complete"] is True
    assert sentinel.read_text() == "keep"
    assert live.exists() and limiter.count("live", "target", now=now) == 1
    if sidecars_first:
        first_scan = next(row for row in observations
                          if row["cursor"] is not None and row["cursor"].get("pair_scan", {}).get("visited", 0))
        assert first_scan["removed"] == 0 and first_scan["pending"] is True
        assert first_scan["cursor"]["complete"] is False
        assert first_scan["cursor"]["last_slice"]["deleted"] == 0
        assert any(entries.seen for entries in streams)
        assert all(not entries.seen or entries.seen[0].endswith(".json.lock") for entries in streams)
    request.node.user_properties.append(("r4_retention", json.dumps({
        "sidecars_first": sidecars_first, "first_slice": observations[0],
        "first_pair_scan": first_scan if sidecars_first else None,
        "maintenance_calls": len(observations), "continuation_calls": len(observations) - 1,
        "expired_removed": len(removed), "live_count": limiter.count("live", "target", now=now),
        "live_exists": live.exists(), "sentinel": sentinel.read_text(),
    })))


def test_count_never_creates_locks_or_removes_expired_bucket(tmp_path):
    _home, limiter = _fixture(tmp_path / "host")
    now = time.time()
    path = _seed(limiter, "old", now - WINDOW_SECONDS * 4)
    before = {entry.name: entry.stat().st_ino for entry in path.parent.iterdir()}
    for _ in range(5):
        assert limiter.count("old", "target", now=now) == 0
    assert {entry.name: entry.stat().st_ino for entry in path.parent.iterdir()} == before


def _slice(home, now, cursor=None, **options):
    from agent_py_agent.agent.conversation.session_pair_rate import (
        SessionPairRetentionRequest,
        cleanup_session_pair_rates,
    )

    return cleanup_session_pair_rates(SessionPairRetentionRequest(home, now, cursor or {}, **options))


def test_portable_cursor_progresses_past_live_prefix_with_bounded_replay(tmp_path, monkeypatch):
    from agent_py_agent.agent.user_space import owner_quota

    home, limiter = _fixture(tmp_path / "host")
    now = time.time()
    live = [_seed(limiter, f"live-{i}", now) for i in range(90)]
    expired = [_seed(limiter, f"expired-{i}", now - WINDOW_SECONDS * 4) for i in range(90)]
    monkeypatch.setattr(owner_quota, "_quota_scandir", os.scandir)
    cursor = {}
    for _ in range(16):
        result = _slice(home, now, cursor)
        assert result["items"] <= 128
        cursor = json.loads(json.dumps(result["cursor"]))
        if result["complete"]:
            break
    assert not any(path.exists() for path in expired)
    assert all(path.exists() for path in live)


def test_future_bucket_schema_generation_is_never_deleted(tmp_path):
    home, limiter = _fixture(tmp_path / "host")
    now = time.time()
    path = _seed(limiter, "future", now - WINDOW_SECONDS * 4)
    value = json.loads(path.read_text())
    value["schema_generation"] = 99
    path.write_text(json.dumps(value))
    _slice(home, now)
    assert path.exists()


def test_busy_bucket_has_resumable_cursor_and_recovery(tmp_path):
    from agent_py_agent.agent.gateway_parts.io import locked_file_transition

    home, limiter = _fixture(tmp_path / "host")
    now = time.time()
    path = _seed(limiter, "busy", now - WINDOW_SECONDS * 4)
    with locked_file_transition(path):
        result = _slice(home, now)
        assert result["busy"] == 1 and not result["complete"] and path.exists()
    result = _slice(home, now, result["cursor"])
    assert not path.exists()


def test_record_refresh_between_scan_and_original_lock_is_retained(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from agent_py_agent.agent.gateway_parts import io

    home, limiter = _fixture(tmp_path / "host")
    now = time.time()
    path = _seed(limiter, "refresh", now - WINDOW_SECONDS * 4)
    original = io.try_existing_file_transition_at
    refreshed = []

    @contextmanager
    def refresh(descriptor, target):
        if target.name == path.name and not refreshed:
            limiter.record("refresh", "target", now=now)
            refreshed.append(True)
        with original(descriptor, target) as acquired:
            yield acquired

    monkeypatch.setattr(io, "try_existing_file_transition_at", refresh)
    _slice(home, now)
    assert refreshed and path.exists() and limiter.count("refresh", "target", now=now) == 1


def test_eio_failure_keeps_original_cursor_responsibility_and_recovers(tmp_path, monkeypatch):
    import errno
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation import session_pair_rate

    home, limiter = _fixture(tmp_path / "host")
    now = time.time()
    path = _seed(limiter, "eio", now - WINDOW_SECONDS * 4)
    original = session_pair_rate.os.unlink
    failed = []

    def unlink(name, **options):
        if name == path.name and not failed:
            failed.append(True)
            raise OSError(errno.EIO, "injected storage failure")
        return original(name, **options)

    # 只注入本写叶，不能改全局os.unlink的对象身份而意外关闭supports_dir_fd能力探测。
    monkeypatch.setattr(session_pair_rate, "os", SimpleNamespace(**{**vars(os), "unlink": unlink}))
    cursor = {}
    with pytest.raises(OSError) as caught:
        _slice(home, now, cursor)
    assert caught.value.errno == errno.EIO and cursor == {} and path.exists()
    _slice(home, now, cursor)
    assert not path.exists()


@pytest.mark.parametrize("history", [100, 1000, 10000])
@pytest.mark.parametrize("quota_mb", [100, 102400])
def test_history_and_quota_configurations_keep_per_slice_io_bounded(tmp_path, request, history, quota_mb):
    """100MB/100GB是配额配置；测实际历史文件的元数据/读写次数，不伪称填满100GB。"""
    home, limiter = _fixture(tmp_path / "host")
    quota = json.loads(home.owner_quota_json.read_text())
    quota["max_disk_mb"] = quota_mb
    home.owner_quota_json.write_text(json.dumps(quota))
    now = time.time()
    # 用原writer建立首桶，历史样本只复制其合法格式及私有sidecar，不测生成历史的速度。
    sample = _seed(limiter, "sample", now - WINDOW_SECONDS * 4)
    body = sample.read_bytes()
    for index in range(history - 1):
        path = limiter._path(f"history-{index}", "target")
        path.write_bytes(body)
        os.chmod(path, 0o600)
        sidecar = path.with_suffix(".json.lock")
        sidecar.touch(mode=0o600)
        os.utime(sidecar, (1, 1))
    cursor, reports = {}, []
    for _ in range(history // 32 + 16):
        report = _slice(home, now, cursor)
        assert report["items"] <= 128 and report["bytes"] <= 8 * 1024 * 1024
        reports.append({key: report[key] for key in ("items", "bytes", "deleted", "locks_deleted")})
        cursor = json.loads(json.dumps(report["cursor"]))
        if report["complete"]:
            break
    assert report["complete"]
    assert sum(row["deleted"] for row in reports) == history
    assert sum(row["locks_deleted"] for row in reports) == history
    assert not list(sample.parent.glob("*.json")) and not list(sample.parent.glob("*.json.lock"))
    observation = {"history": history, "quota_mb": quota_mb, "slices": len(reports),
                   "totals": {key: sum(row[key] for row in reports) for key in reports[0]},
                   "max_slice_items": max(row["items"] for row in reports)}
    request.node.user_properties.append(("retention_measurement", json.dumps(observation)))
    output = os.environ.get("W8_MEASUREMENT_OUTPUT")
    if output:
        from pathlib import Path

        root = Path(output)
        root.mkdir(parents=True, exist_ok=True)
        (root / f"retention-{history}-{quota_mb}.json").write_text(json.dumps(observation, indent=2))


def test_small_and_large_original_quota_admission_paths_remain_live(tmp_path):
    from agent_py_agent.agent.user_space.owner_quota import (
        OwnerQuotaChange,
        OwnerQuotaEnforcer,
        reconcile_owner_quota_slice,
    )

    for count in (4, 180):
        directory = tmp_path / f"owner-{count}"
        directory.mkdir()
        for index in range(count):
            (directory / f"{index}.txt").write_text("history")
        enforcer = OwnerQuotaEnforcer(directory, max_bytes=100 * 1024 * 1024)
        for _ in range(32):
            if reconcile_owner_quota_slice(directory, max_entries=128, max_ms=100)["complete"]:
                break
        target = directory / "new.txt"
        with enforcer.admission() as admission:
            result = admission.check((OwnerQuotaChange(target, 3),))
            assert result is not None and result.projected_bytes <= result.max_bytes
            target.write_text("new")
        assert target.read_text() == "new"


def test_completed_cursor_starts_new_pass_when_live_window_later_expires(tmp_path):
    home, limiter = _fixture(tmp_path / "host")
    now = time.time()
    path = _seed(limiter, "later", now)
    result = _slice(home, now)
    assert result["complete"] and path.exists()
    _slice(home, now + WINDOW_SECONDS * 4, result["cursor"])
    assert not path.exists()


def test_data_fd_is_closed_if_fdopen_fails_before_ownership_transfer(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation import session_pair_rate as module

    home, limiter = _fixture(tmp_path / "host")
    now = time.time()
    path = _seed(limiter, "fd-error", now - WINDOW_SECONDS * 4)
    opened = []

    def refuse(descriptor, mode):
        assert mode == "rb"
        opened.append(descriptor)
        raise OSError("injected fdopen failure")

    monkeypatch.setattr(module, "os", SimpleNamespace(**{**vars(os), "fdopen": refuse}))
    with pytest.raises(OSError):
        _slice(home, now)
    assert opened and path.exists()
    try:
        with pytest.raises(OSError):
            os.fstat(opened[0])
    finally:
        # 红版本也释放本测试取得的描述符；不把测试自身泄漏带进其它用例。
        try:
            os.close(opened[0])
        except OSError:
            pass


def test_retention_flushes_one_directory_page_before_receipt(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation import session_pair_rate as module

    home, limiter = _fixture(tmp_path / "host")
    now = time.time()
    paths = [_seed(limiter, str(index), now - WINDOW_SECONDS * 4) for index in range(12)]
    flushed = []
    original = os.fsync

    def sync(descriptor):
        # 真正执行刷盘，同时证明一页全部删除先于唯一耐久提交。
        flushed.append(sum(path.exists() for path in paths))
        return original(descriptor)

    monkeypatch.setattr(module, "os", SimpleNamespace(**{**vars(os), "fsync": sync}))
    report = _slice(home, now)
    assert report["deleted"] == 12 and report["locks_deleted"] == 12
    assert flushed == [0]


def test_retention_failed_page_sync_does_not_publish_cursor(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation import session_pair_rate as module

    home, limiter = _fixture(tmp_path / "host")
    now = time.time()
    path = _seed(limiter, "expired", now - WINDOW_SECONDS * 4)
    original, flushed = os.fsync, []

    def sync(descriptor):
        original(descriptor)
        flushed.append(not path.exists())
        raise OSError("page sync failed")

    monkeypatch.setattr(module, "os", SimpleNamespace(**{**vars(os), "fsync": sync}))
    cursor = {}
    with pytest.raises(OSError, match="page sync failed"):
        _slice(home, now, cursor)
    assert cursor == {} and flushed == [True]
    monkeypatch.setattr(module, "os", os)
    assert _slice(home, now, cursor)["complete"]


def test_retention_partial_page_error_still_flushes_deleted_entries(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation import session_pair_rate as module

    home, limiter = _fixture(tmp_path / "host")
    now = time.time()
    for index in range(4):
        _seed(limiter, str(index), now - WINDOW_SECONDS * 4)
    remove, sync = module._remove_expired_pair, os.fsync
    deleted, flushed = [], []
    failure = OSError("later entry failed")

    def remove_one(*args):
        if deleted:
            raise failure
        result = remove(*args)
        deleted.append(result)
        return result

    def flush(descriptor):
        sync(descriptor)
        flushed.append(True)

    monkeypatch.setattr(module, "_remove_expired_pair", remove_one)
    monkeypatch.setattr(module, "os", SimpleNamespace(**{**vars(os), "fsync": flush}))
    cursor = {}
    with pytest.raises(OSError) as caught:
        _slice(home, now, cursor)
    assert caught.value is failure and cursor == {}
    assert deleted[0][0] == "deleted" and flushed == [True]
