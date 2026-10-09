from __future__ import annotations

"""mc3-e11d: 网关重启后 curator 租约挂着不放——持有者确定死亡时提前接管。

覆盖（对应任务书测试清单）：
- 本进程持有 / 活着的子进程持有 → busy 不接管；
- 持有者 pid 已死（子进程 wait 回收）→ 立即接管，写 stale_lease_reclaimed 事实，后续提交正常；
- 主机不同 → 不接管等 expires_at；
- PermissionError（替身模拟）→ 不接管；
- 租约损坏 → 原 corrupt 路径不变；
- 旧状态文件（没有出生身份字段）能读，接管规则照常生效；
- 新 lease 带持有者出生身份，旧租约在无关更新后不被添加新字段。
"""

import json
import os
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_py_agent.agent.memory_store import curator_state as cs  # noqa: E402
from agent_py_agent.agent.memory_store.curator_models import (  # noqa: E402
    CURATOR_STATE_SCHEMA_VERSION,
)
from agent_py_agent.agent.memory_store.curator_state import (  # noqa: E402
    CuratorStateCorruptError,
    CuratorSuccessCommit,
    MemoryCuratorStateStore,
)

_LEASE_ID = "memory-curator-lease-" + "a" * 32
_RUN_ID = "memory-curator-run-" + "b" * 32


def _state_path(tmp_path: Path) -> Path:
    return tmp_path / "memory" / "curator" / "state.json"


def _write_state(path: Path, lease: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": CURATOR_STATE_SCHEMA_VERSION, "active_lease": lease}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _lease(**overrides) -> dict:
    now = datetime.now(timezone.utc)
    lease = {
        "lease_id": _LEASE_ID,
        "run_id": _RUN_ID,
        "reason": "interval",
        "reason_generation": 1,
        "acquired_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=600)).isoformat(),
        "pid": os.getpid(),
        "host": socket.gethostname(),
    }
    lease.update(overrides)
    return lease


def _acquire(store: MemoryCuratorStateStore):
    return store.acquire(reason="interval", config_revision="rev-1", lease_seconds=600)


def _reap_short_lived_child() -> int:
    """起一个短命子进程并 wait 回收，返回它已不存在的 pid。"""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.2)"])
    pid = proc.pid
    proc.wait(timeout=30)
    return pid


def test_live_own_process_lease_is_busy(tmp_path: Path) -> None:
    store = MemoryCuratorStateStore(_state_path(tmp_path))
    _write_state(store.path, _lease(pid=os.getpid()))
    assert _acquire(store) is None


def test_live_child_process_lease_is_busy(tmp_path: Path) -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        store = MemoryCuratorStateStore(_state_path(tmp_path))
        _write_state(store.path, _lease(pid=proc.pid))
        assert _acquire(store) is None
    finally:
        proc.terminate()
        proc.wait(timeout=30)


def test_dead_pid_is_reclaimed_with_fact_and_commit(tmp_path: Path) -> None:
    dead_pid = _reap_short_lived_child()
    store = MemoryCuratorStateStore(_state_path(tmp_path))
    _write_state(store.path, _lease(pid=dead_pid))
    acquired = _acquire(store)
    if os.name == "nt":
        # Windows 合同：os.kill(pid, 0) 会误杀目标进程，一律不提前判死；
        # 死 pid 也不接管，租约原样保留等 expires_at。
        assert acquired is None
        assert store.load().active_lease["lease_id"] == _LEASE_ID
        return
    assert acquired is not None
    _, lease = acquired
    recovery = lease["recovery"]
    assert recovery["kind"] == "stale_lease_reclaimed"
    assert recovery["previous_lease_id"] == _LEASE_ID
    assert recovery["previous_run_id"] == _RUN_ID
    assert recovery["previous_pid"] == dead_pid
    assert recovery["previous_expires_at"]
    assert recovery["reclaimed_at"]
    # 接管后照原逻辑运行：用新 lease 正常提交成功并释放。
    updated = store.commit_success(
        CuratorSuccessCommit(
            lease_id=lease["lease_id"],
            run_id=lease["run_id"],
            reason="interval",
            per_thread_cursors={"thread-1": "message-1"},
            last_processed_audit_event_id="",
            processed_messages=1,
            processed_audit_events=0,
            candidate_count=0,
            daily_event_count=0,
        )
    )
    assert updated.active_lease == {}
    assert updated.last_processed_message_id == "message-1"


def test_foreign_host_lease_waits_for_expiry(tmp_path: Path) -> None:
    store = MemoryCuratorStateStore(_state_path(tmp_path))
    _write_state(store.path, _lease(host="some-other-host", pid=999999))
    assert _acquire(store) is None


def test_pid_definitely_dead_classifies_os_errors(monkeypatch) -> None:
    if os.name == "nt":
        # Windows 合同：直接返回不确定，且不得调用 os.kill；替身只在被误用时让测试失败。
        monkeypatch.setattr(cs.os, "kill", lambda *args: pytest.fail("Windows must not probe"))
        assert cs._pid_definitely_dead(4321) is False
        return
    def _permission(pid, sig):
        raise PermissionError("exists but not ours")

    monkeypatch.setattr(cs.os, "kill", _permission)
    assert cs._pid_definitely_dead(4321) is False

    def _missing(pid, sig):
        raise ProcessLookupError("gone")

    monkeypatch.setattr(cs.os, "kill", _missing)
    assert cs._pid_definitely_dead(4321) is True

    monkeypatch.setattr(cs.os, "kill", lambda pid, sig: None)
    assert cs._pid_definitely_dead(4321) is False


def test_permission_error_lease_waits(tmp_path: Path, monkeypatch) -> None:
    store = MemoryCuratorStateStore(_state_path(tmp_path))
    _write_state(store.path, _lease(pid=4321))

    def _permission(pid, sig):
        raise PermissionError("exists but not ours")

    monkeypatch.setattr(cs.os, "kill", _permission)
    assert _acquire(store) is None


def test_corrupt_lease_keeps_corrupt_path(tmp_path: Path) -> None:
    store = MemoryCuratorStateStore(_state_path(tmp_path))
    lease = _lease()
    del lease["expires_at"]
    _write_state(store.path, lease)
    with pytest.raises(CuratorStateCorruptError) as err:
        _acquire(store)
    assert err.value.error_class == "lease_invalid_expires_at"


def test_legacy_lease_without_identity_reads_and_reclaims(tmp_path: Path) -> None:
    dead_pid = _reap_short_lived_child()
    store = MemoryCuratorStateStore(_state_path(tmp_path))
    lease = _lease(pid=dead_pid)
    assert "process_identity" not in lease
    _write_state(store.path, lease)
    acquired = _acquire(store)
    if os.name == "nt":
        # Windows 合同：一律不提前判死；旧租约照常读回、不接管。
        assert acquired is None
        assert store.load().active_lease["lease_id"] == _LEASE_ID
        return
    assert acquired is not None
    _, new_lease = acquired
    assert new_lease["recovery"]["kind"] == "stale_lease_reclaimed"
    identity = new_lease["process_identity"]
    assert identity["pid"] == os.getpid()
    assert identity["host_id"]
    assert "start_time" in identity


def test_new_lease_carries_process_identity(tmp_path: Path) -> None:
    store = MemoryCuratorStateStore(_state_path(tmp_path))
    acquired = _acquire(store)
    assert acquired is not None
    _, lease = acquired
    assert lease["process_identity"]["pid"] == os.getpid()
    assert lease["process_identity"]["host_id"]
    assert lease["host"] == socket.gethostname()


def test_legacy_lease_record_gains_no_new_field_on_unrelated_update(tmp_path: Path) -> None:
    store = MemoryCuratorStateStore(_state_path(tmp_path))
    _write_state(store.path, _lease())
    store.request("turn_threshold")
    state = store.load()
    assert "process_identity" not in state.active_lease
    assert state.active_lease["lease_id"] == _LEASE_ID
