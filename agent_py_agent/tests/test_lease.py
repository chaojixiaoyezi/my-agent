"""lease service 模块测试。

测试 lease_service.py 中的租约创建/刷新/过期、并发安全、stale 检测功能。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.gateway_parts import lease_service
from agent_py_agent.agent.gateway_parts import lease_service as lease_module

# ── 测试夹具 ──────────────────────────────────────────────────────────────

@pytest.fixture
def mock_agent(tmp_path):
    """创建模拟的 agent 配置。"""
    agent = MagicMock()
    agent.config.gateway_heartbeat_interval = 5
    agent.config.gateway_processing_timeout_seconds = 900
    return agent


@pytest.fixture
def request_path(tmp_path):
    """创建临时请求文件路径。"""
    path = tmp_path / "processing" / "test_request.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# ── 租约间隔计算测试 ──────────────────────────────────────────────────────

def test_lease_interval_normal(mock_agent):
    """测试正常配置的租约间隔计算。"""
    mock_agent.config.gateway_heartbeat_interval = 5
    mock_agent.config.gateway_processing_timeout_seconds = 900

    interval = lease_module._lease_interval(mock_agent)

    # 5 秒间隔不应超过 processing_timeout/3 = 300 秒
    assert interval == 5


def test_lease_interval_short_timeout(mock_agent):
    """测试短超时时间限制间隔。"""
    mock_agent.config.gateway_heartbeat_interval = 10
    mock_agent.config.gateway_processing_timeout_seconds = 30

    interval = lease_module._lease_interval(mock_agent)

    # 间隔应被限制到 max(0.2, 30/3) = 10 秒
    assert interval == 10


def test_lease_interval_very_long_timeout(mock_agent):
    """测试超长超时时间不影响短间隔。"""
    mock_agent.config.gateway_heartbeat_interval = 5
    mock_agent.config.gateway_processing_timeout_seconds = 86400  # 24小时

    interval = lease_module._lease_interval(mock_agent)

    # 间隔应保持 gateway_heartbeat_interval
    assert interval == 5


def test_lease_interval_minimum_enforced(mock_agent):
    """测试最小间隔 0.2 秒被强制执行。"""
    mock_agent.config.gateway_heartbeat_interval = 0.1
    mock_agent.config.gateway_processing_timeout_seconds = 1

    interval = lease_module._lease_interval(mock_agent)

    # 最小间隔应为 0.2 秒
    assert interval == 0.2


def test_lease_interval_zero_values(mock_agent):
    """测试零值配置使用默认值。"""
    mock_agent.config.gateway_heartbeat_interval = 0
    mock_agent.config.gateway_processing_timeout_seconds = 0

    interval = lease_module._lease_interval(mock_agent)

    # 应使用默认值 5.0 和 900.0
    assert interval == 5.0


def test_lease_interval_invalid_types(mock_agent):
    """测试无效类型配置使用默认值。"""
    mock_agent.config.gateway_heartbeat_interval = "invalid"
    mock_agent.config.gateway_processing_timeout_seconds = None

    interval = lease_module._lease_interval(mock_agent)

    # 异常被捕获，使用默认值
    assert interval == 5.0


# ── 心跳存活检测测试 ──────────────────────────────────────────────────────

def test_is_heartbeat_alive_true():
    """测试活跃心跳返回 True。"""
    request_id = "test-alive-request"
    lease_module._active_heartbeat_request_ids.add(request_id)

    try:
        result = lease_module.is_heartbeat_alive_for_request(request_id)
        assert result is True
    finally:
        lease_module._active_heartbeat_request_ids.discard(request_id)


def test_is_heartbeat_alive_false():
    """测试不活跃心跳返回 False。"""
    result = lease_module.is_heartbeat_alive_for_request("nonexistent-request")
    assert result is False


# ── 租约刷新测试 ──────────────────────────────────────────────────────────

def testrefresh_processing_lease_success(request_path):
    """测试成功刷新租约。"""
    from agent_py_agent.agent.gateway_parts.io import write_json_file

    request_id = "test-touch-request"
    write_json_file(request_path, {"id": request_id, "status": "processing"})

    result = lease_module.refresh_processing_lease(
        request_path, request_id=request_id
    )

    assert result is True


def testrefresh_processing_lease_mismatched_id(request_path):
    """测试 request_id 不匹配时返回 False。"""
    from agent_py_agent.agent.gateway_parts.io import write_json_file

    request_id = "test-mismatch"
    write_json_file(request_path, {"id": "different-id", "status": "processing"})

    result = lease_module.refresh_processing_lease(
        request_path, request_id=request_id
    )

    assert result is False


def testrefresh_processing_lease_nonexistent_file(request_path):
    """测试请求文件不存在时返回 False。"""
    result = lease_module.refresh_processing_lease(
        request_path, request_id="any-request"
    )

    assert result is False


def testrefresh_processing_lease_with_worker_id(request_path):
    """测试带 worker_id 的租约刷新。"""
    from agent_py_agent.agent.gateway_parts.io import write_json_file

    request_id = "test-worker-request"
    write_json_file(request_path, {"id": request_id, "status": "processing"})

    result = lease_module.refresh_processing_lease(
        request_path, request_id=request_id, worker_id="worker-001"
    )

    assert result is True


def testrefresh_processing_lease_updates_timestamp(request_path):
    """测试租约刷新更新 timestamp。"""
    from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file

    request_id = "test-timestamp"
    before = time.time() - 100
    write_json_file(request_path, {"id": request_id, "status": "processing"})

    lease_module.refresh_processing_lease(request_path, request_id=request_id)

    payload = read_json_file(request_path)
    assert payload["lease_heartbeat_at"] > before


def test_refresh_processing_lease_report_bad_json(request_path):
    """坏 request JSON 不能伪装成普通心跳失败。"""
    request_id = "test-bad-json-lease"
    request_path.write_text("{bad json", encoding="utf-8")

    report = lease_service.refresh_processing_lease_report(
        request_path,
        request_id=request_id,
        worker_id="worker-lease",
    )

    assert report.ok is False
    assert report.load_error is not None
    assert report.load_error["context"] == "gateway.lease.request.read"
    assert report.load_error["path"] == str(request_path)
    assert lease_service.refresh_processing_lease(
        request_path,
        request_id=request_id,
        worker_id="worker-lease",
    ) is False


# ── 心跳线程启动测试 ──────────────────────────────────────────────────────

def test_start_heartbeat_thread_adds_to_active(mock_agent, request_path):
    """测试启动心跳线程添加到活跃集合。"""
    from agent_py_agent.agent.gateway_parts.io import write_json_file

    request_id = "test-start-thread"
    write_json_file(request_path, {"id": request_id, "status": "processing"})

    stop_event, thread = lease_module.start_lease_heartbeat(
        mock_agent, request_path, request_id=request_id
    )

    try:
        time.sleep(0.1)
        assert lease_module.is_heartbeat_alive_for_request(request_id)
    finally:
        stop_event.set()
        thread.join(timeout=2)


def test_start_heartbeat_thread_removes_on_stop(mock_agent, request_path):
    """测试停止心跳线程从活跃集合移除。"""
    from agent_py_agent.agent.gateway_parts.io import write_json_file

    request_id = "test-stop-thread"
    write_json_file(request_path, {"id": request_id, "status": "processing"})

    stop_event, thread = lease_module.start_lease_heartbeat(
        mock_agent, request_path, request_id=request_id
    )

    time.sleep(0.1)
    stop_event.set()
    thread.join(timeout=2)

    assert not lease_module.is_heartbeat_alive_for_request(request_id)


def test_start_heartbeat_thread_returns_event_and_thread(mock_agent, request_path):
    """测试返回正确的 Event 和 Thread 对象。"""
    from agent_py_agent.agent.gateway_parts.io import write_json_file

    request_id = "test-return-values"
    write_json_file(request_path, {"id": request_id, "status": "processing"})

    stop_event, thread = lease_module.start_lease_heartbeat(
        mock_agent, request_path, request_id=request_id
    )

    assert isinstance(stop_event, threading.Event)
    assert isinstance(thread, threading.Thread)
    assert thread.daemon is True

    stop_event.set()
    thread.join(timeout=2)


def test_start_heartbeat_thread_name(mock_agent, request_path):
    """测试心跳线程名称包含 request_id。"""
    from agent_py_agent.agent.gateway_parts.io import write_json_file

    request_id = "test-thread-name-123"
    write_json_file(request_path, {"id": request_id, "status": "processing"})

    stop_event, thread = lease_module.start_lease_heartbeat(
        mock_agent, request_path, request_id=request_id
    )

    assert request_id in thread.name

    stop_event.set()
    thread.join(timeout=2)


# ── 并发安全测试 ──────────────────────────────────────────────────────────

def test_concurrent_heartbeat_requests():
    """测试多个请求的心跳可以独立运行。"""
    request_ids = ["concurrent-1", "concurrent-2", "concurrent-3"]

    for rid in request_ids:
        assert not lease_module.is_heartbeat_alive_for_request(rid)

    # 添加多个请求到活跃集合
    for rid in request_ids:
        lease_module._active_heartbeat_request_ids.add(rid)

    try:
        for rid in request_ids:
            assert lease_module.is_heartbeat_alive_for_request(rid)
    finally:
        for rid in request_ids:
            lease_module._active_heartbeat_request_ids.discard(rid)


# ── 异常场景测试 ──────────────────────────────────────────────────────────

def test_touch_lease_read_error(mock_agent, request_path, monkeypatch):
    """测试读取请求文件失败时的行为。"""
    request_id = "test-read-error"

    def failing_read(path):
        raise OSError("Simulated read error")

    monkeypatch.setattr(
        "agent_py_agent.agent.gateway_parts.io.read_json_file",
        failing_read
    )

    result = lease_module.refresh_processing_lease(
        request_path, request_id=request_id
    )

    assert result is False


def test_touch_lease_write_error(mock_agent, request_path, monkeypatch):
    """测试写入请求文件失败时的行为。"""
    from agent_py_agent.agent.gateway_parts.io import write_json_file

    request_id = "test-write-error"
    write_json_file(request_path, {"id": request_id, "status": "processing"})

    def mock_write_fail(path, payload):
        raise OSError("Simulated write error")

    monkeypatch.setattr(
        "agent_py_agent.agent.gateway_parts.lease_service.write_json_file_atomic",
        mock_write_fail
    )

    result = lease_module.refresh_processing_lease(
        request_path, request_id=request_id
    )

    # 写入失败时应该返回 False
    assert result is False


def test_heartbeat_thread_exception_handling(mock_agent, request_path, monkeypatch):
    """测试心跳线程内异常不会被抛出。"""
    from agent_py_agent.agent.gateway_parts.io import write_json_file

    request_id = "test-exception-handling"
    write_json_file(request_path, {"id": request_id, "status": "processing"})

    call_count = 0

    def failing_touch(path, *, request_id, worker_id=""):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise OSError("First call fails")
        return True

    monkeypatch.setattr(
        "agent_py_agent.agent.gateway_parts.lease_service.refresh_processing_lease",
        failing_touch
    )
    monkeypatch.setattr(
        "agent_py_agent.agent.gateway_parts.lease_service._lease_interval",
        lambda agent: 0.05
    )

    stop_event, thread = lease_module.start_lease_heartbeat(
        mock_agent, request_path, request_id=request_id
    )

    # 等待线程处理
    time.sleep(0.3)

    # 线程应该还在运行（异常被捕获）
    assert thread.is_alive()

    stop_event.set()
    thread.join(timeout=2)
