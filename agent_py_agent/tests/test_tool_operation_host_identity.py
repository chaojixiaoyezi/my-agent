"""工具操作持有者的同主机判定改用进程内主机身份（2026-10-02，承接网关 host_id 修复）。

tool_operations 与 managed_operation_store 原来拿 socket.gethostname() 判同主机。macOS 换网络后主机名会变，本机持有者会被当成
别的主机：进程死了也要等租约到期才能接管。现在持有者写入和三处比较都用 daemon_metadata.process_host_id
（进程内缓存的稳定主机身份）。锁定：
1. 新持有者记的是本机主机身份，不是主机名；主机名中途变化后，本机已死的持有者仍能立刻认出来（不活）；
2. 上一版写的主机名记录与本机身份不等，按“另一主机”处理：租约到期前当活着，到期后才可接管（过渡只等 TTL，不会误接管）；
3. 工具操作租约、本地核对标记、runtime.db 核对标记三处同一口径。
"""
from __future__ import annotations

import time

import pytest

from agent_py_agent.agent.gateway_parts import daemon_metadata
from agent_py_agent.agent.local_storage.tool_operations import (
    ToolOperationRecord,
    _operation_holder_is_live,
    _reconciliation_marker_is_live,
    new_tool_operation_holder,
    tool_operation_host_id,
)
from agent_py_agent.agent.runtime_db.managed_operation_store import (
    _managed_reconciliation_claim_is_live,
)

_DEAD_PID = 999_999_999


@pytest.fixture
def hostname_only(monkeypatch):
    # 模拟只剩主机名可用的机器（最容易出事的情况）；主机身份在本进程只算一次。
    daemon_metadata._cached_process_host_id.cache_clear()
    monkeypatch.setattr(daemon_metadata, "_stable_host_source", lambda: "")
    monkeypatch.setattr(daemon_metadata.socket, "gethostname", lambda: "mac-a.local")
    yield monkeypatch
    daemon_metadata._cached_process_host_id.cache_clear()


def _record(host: str, *, lease_seconds: float = 3600.0) -> ToolOperationRecord:
    return ToolOperationRecord(
        owner_id="owner-a", run_id="run-1", task_id="run-1", operation_id="op-1", tool="t", args_hash="h",
        idempotency_key="k", idempotency_scope="operation", idempotency_namespace="t", status="RUNNING",
        holder_id="holder-1", holder_host=host, holder_pid=_DEAD_PID, holder_process_start_token="not-running",
        generation=1, lease_expires_at=time.time() + lease_seconds,
    )


def _marker(host: str, *, lease_seconds: float = 3600.0) -> dict:
    return {
        "lease_expires_at": time.time() + lease_seconds,
        "holder": {"holder_id": "holder-1", "host": host, "pid": _DEAD_PID, "process_start_token": "not-running"},
    }


def _live_everywhere(host: str, *, lease_seconds: float = 3600.0) -> tuple[bool, bool, bool]:
    now = time.time()
    return (
        _operation_holder_is_live(_record(host, lease_seconds=lease_seconds), now),
        _reconciliation_marker_is_live(_marker(host, lease_seconds=lease_seconds), now),
        _managed_reconciliation_claim_is_live(_marker(host, lease_seconds=lease_seconds), now),
    )


def test_new_holder_records_the_host_identity_not_the_hostname(hostname_only):
    holder = new_tool_operation_holder()
    assert holder.host == tool_operation_host_id() == daemon_metadata.process_host_id()
    assert "mac-a.local" not in holder.holder_id


def test_local_dead_holder_is_recognised_after_the_hostname_changes(hostname_only):
    holder = new_tool_operation_holder()
    hostname_only.setattr(daemon_metadata.socket, "gethostname", lambda: "anonymous")  # 换网络后主机名变了
    # 本机、进程已死：三处都认得出来，可以立刻接管，不用等租约到期。
    assert _live_everywhere(holder.host) == (False, False, False)


def test_old_hostname_records_wait_for_the_lease(hostname_only):
    old_host = "mac-a.local"  # 上一版写入的是主机名，与本机主机身份不等
    assert _live_everywhere(old_host) == (True, True, True)  # 当作另一主机：租约内不接管
    assert _live_everywhere(old_host, lease_seconds=-1.0) == (False, False, False)  # 租约到期才接管
