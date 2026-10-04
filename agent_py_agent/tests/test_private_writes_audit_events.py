"""pwf：审计与事件组宿主数据私有写入用例。

验证范围：AuditLogger 审计账与旁路错误账、LocalStore events.jsonl、Gateway 历史流水与其
history transition 锁。统一在 umask 0o022 下断言：新文件 0600、新目录 0700；预置 0644
文件写一次后收紧到 0600；已有内容逐字节保留。
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from agent_py_agent.agent.audit.logger import AuditAction, AuditLogger, AuditStatus, LogParams
from agent_py_agent.agent.gateway_parts.io import append_gateway_history_once
from agent_py_agent.agent.gateway_parts.paths import GatewayPaths
from agent_py_agent.agent.local_storage import LocalStore


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture(autouse=True)
def _fixed_umask():
    previous = os.umask(0o022)
    yield
    os.umask(previous)


class _MockConfig:
    def __init__(self, root: Path) -> None:
        self.audit_log_path = str(root / "audit")


def _log_one(logger: AuditLogger, task_id: str):
    return logger.log(
        LogParams(
            action=AuditAction.CREATE_TASK,
            user_id="user1",
            channel="chat",
            target_type="task",
            target_id=task_id,
            status=AuditStatus.SUCCESS,
        )
    )


def test_audit_log_file_is_private_and_tightens(tmp_path: Path) -> None:
    logger = AuditLogger(_MockConfig(tmp_path))
    entry = _log_one(logger, "task-001")
    audit_file = logger._audit_file

    expected = json.dumps(entry.to_dict(), ensure_ascii=False).encode("utf-8") + b"\n"
    assert audit_file.read_bytes() == expected
    assert _mode(audit_file) == 0o600
    assert _mode(audit_file.parent) == 0o700

    # 预置宽权限后写第二条：文件收紧到 0600，旧行逐字节保留。
    os.chmod(audit_file, 0o644)
    before = audit_file.read_bytes()
    _log_one(logger, "task-002")
    assert audit_file.read_bytes().startswith(before)
    assert _mode(audit_file) == 0o600


def test_local_store_events_jsonl_is_private_and_tightens(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "local.db")
    store.record_event(event_type="demo", payload={"k": 1})
    path = store.events_path

    assert _mode(path) == 0o600
    assert _mode(path.parent) == 0o700

    os.chmod(path, 0o644)
    before = path.read_bytes()
    store.record_event(event_type="demo", payload={"k": 2})
    assert path.read_bytes().startswith(before)
    assert _mode(path) == 0o600


def _gateway_paths(tmp_path: Path) -> GatewayPaths:
    root = tmp_path / "gateway"
    return GatewayPaths(
        root=root,
        pid=root / "gateway.pid",
        adapter_pid=root / "adapter.pid",
        state=root / "state.json",
        heartbeat=root / "heartbeat.json",
        stop_request=root / "stop.request",
        log=root / "gateway.log",
        inbox=root / "requests" / "pending",
        processing=root / "requests" / "processing",
        done=root / "requests" / "done",
        failed=root / "requests" / "failed",
        responses=root / "responses",
        history=root / "gateway_requests.jsonl",
    )


def test_gateway_history_and_transition_lock_are_private(tmp_path: Path) -> None:
    paths = _gateway_paths(tmp_path)

    assert append_gateway_history_once(paths, {"id": "req-1", "status": "done"}) is True
    assert _mode(paths.history) == 0o600
    assert _mode(paths.history.parent) == 0o700

    transition_lock = paths.root / "history_transitions" / "history.transition.lock"
    assert transition_lock.exists()
    assert _mode(transition_lock) == 0o600
    assert _mode(transition_lock.parent) == 0o700

    # 预置宽权限后追加第二条：历史收紧到 0600，旧行逐字节保留。
    os.chmod(paths.history, 0o644)
    before = paths.history.read_bytes()
    assert append_gateway_history_once(paths, {"id": "req-2", "status": "done"}) is True
    assert paths.history.read_bytes().startswith(before)
    assert _mode(paths.history) == 0o600

    # 已存在的 0644 旧锁在下一次取用时被自愈收紧。
    os.chmod(transition_lock, 0o644)
    append_gateway_history_once(paths, {"id": "req-3", "status": "done"})
    assert _mode(transition_lock) == 0o600
