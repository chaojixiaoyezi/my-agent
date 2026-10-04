"""pwf：记忆应用组宿主数据私有写入用例。

验证范围：memory retention 审计追加（_append_retention_audit / record_retention_report）。
统一在 umask 0o022 下断言：新文件 0600、新目录 0700；预置 0644 文件写一次后收紧到
0600；已有内容逐字节保留。
"""
from __future__ import annotations

import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.memory_store.retention_apply import record_retention_report
from agent_py_agent.agent.memory_store.retention_models import MemoryRetentionReport


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture(autouse=True)
def _fixed_umask():
    previous = os.umask(0o022)
    yield
    os.umask(previous)


def test_retention_audit_append_is_private_and_tightens(tmp_path: Path) -> None:
    home = SimpleNamespace(
        owner_audit_log_jsonl=str(tmp_path / "owner" / "audit_log.jsonl"),
        owner_id="owner-1",
    )
    report = MemoryRetentionReport(applied=True, actions=())

    record_retention_report(
        home=home,
        report=report,
        now=datetime(2026, 10, 3, tzinfo=timezone.utc),
    )

    path = Path(home.owner_audit_log_jsonl)
    assert _mode(path) == 0o600
    assert _mode(path.parent) == 0o700

    # 预置宽权限后追加第二条：文件收紧到 0600，旧行逐字节保留。
    os.chmod(path, 0o644)
    before = path.read_bytes()
    record_retention_report(
        home=home,
        report=report,
        now=datetime(2026, 10, 4, tzinfo=timezone.utc),
    )
    assert path.read_bytes().startswith(before)
    assert _mode(path) == 0o600
    assert _mode(path.parent) == 0o700
