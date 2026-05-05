"""supervisor 模块测试。

测试 supervisor.py 中的看门狗监控、自动重启、心跳检测功能。
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from agent_py_agent.agent.gateway_parts import supervisor as sv

# ── 测试夹具 ──────────────────────────────────────────────────────────────

@pytest.fixture
def mock_agent_and_paths(tmp_path):
    """创建模拟的 agent 和 paths 对象。"""
    # 创建临时配置目录
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "agent_config.yaml"
    config_file.write_text("user_id: test\n", encoding="utf-8")

    # 创建 workspace
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    return config_file, workspace


@pytest.fixture
def supervisor_instance(tmp_path, mock_agent_and_paths):
    """创建 GatewaySupervisor 实例用于测试。"""
    config_file, workspace = mock_agent_and_paths

    # 临时日志路径
    log_path = tmp_path / "supervisor.log"

    supervisor = sv.GatewaySupervisor(
        config_path=str(config_file),
        options=sv.SupervisorConfig(
            workspace_root=str(workspace),
            heartbeat_timeout=30.0,
            check_interval=1.0,
            max_restart_attempts=3,
            restart_cooldown=5.0,
            log_path=log_path,
        ),
    )
    return supervisor


# ── 初始化测试 ─────────────────────────────────────────────────────────────

def test_supervisor_init_defaults():
    """测试 GatewaySupervisor 默认参数初始化。"""
    supervisor = sv.GatewaySupervisor("dummy_config.yaml")
    assert supervisor.heartbeat_timeout == 120.0
    assert supervisor.check_interval == 10.0
    assert supervisor.max_restart_attempts == 5
    assert supervisor.restart_cooldown == 30.0


def test_supervisor_init_custom_params():
    """测试 GatewaySupervisor 自定义参数初始化。"""
    supervisor = sv.GatewaySupervisor(
        config_path="test.yaml",
        options=sv.SupervisorConfig(
            heartbeat_timeout=60.0,
            check_interval=5.0,
            max_restart_attempts=10,
            restart_cooldown=60.0,
        ),
    )
    assert supervisor.heartbeat_timeout == 60.0
    assert supervisor.check_interval == 5.0
    assert supervisor.max_restart_attempts == 10
    assert supervisor.restart_cooldown == 60.0


def test_supervisor_internal_state():
    """测试 GatewaySupervisor 内部状态初始化。"""
    supervisor = sv.GatewaySupervisor("test.yaml")
    assert supervisor._supervisor_pid == os.getpid()
    assert supervisor._gateway_pid is None
    assert supervisor._restart_count == 0
    assert supervisor._stop_requested is False


# ── 日志功能测试 ──────────────────────────────────────────────────────────

def test_log_info(supervisor_instance, tmp_path):
    """测试 INFO 级别日志输出。"""
    log_path = tmp_path / "test.log"
    supervisor_instance.log_path = log_path

    supervisor_instance._log_info("Test info message")

    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert "[INFO] Test info message" in content


def test_log_warn(supervisor_instance, tmp_path):
    """测试 WARN 级别日志输出。"""
    log_path = tmp_path / "warn.log"
    supervisor_instance.log_path = log_path

    supervisor_instance._log_warn("Test warning message")

    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert "[WARN] Test warning message" in content


def test_log_error(supervisor_instance, tmp_path):
    """测试 ERROR 级别日志输出。"""
    log_path = tmp_path / "error.log"
    supervisor_instance.log_path = log_path

    supervisor_instance._log_error("Test error message")

    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert "[ERROR] Test error message" in content


# ── 心跳读取测试 ──────────────────────────────────────────────────────────

def test_read_gateway_heartbeat_nonexistent(supervisor_instance):
    """测试读取不存在的网关心跳返回 None。"""
    supervisor_instance._paths = MagicMock()
    supervisor_instance._paths.heartbeat = Path("/nonexistent/heartbeat.json")

    result = supervisor_instance._read_gateway_heartbeat()
    assert result is None


def test_read_gateway_heartbeat_valid(supervisor_instance, tmp_path):
    """测试读取有效的网关心跳。"""
    heartbeat_file = tmp_path / "heartbeat.json"
    heartbeat_data = {"updated_at": time.time(), "status": "alive"}
    heartbeat_file.write_text(json.dumps(heartbeat_data), encoding="utf-8")

    # 模拟 paths 对象
    mock_paths = MagicMock()
    mock_paths.heartbeat = heartbeat_file

    # 需要先解析 agent 和 paths
    supervisor_instance._agent = MagicMock()
    supervisor_instance._paths = mock_paths

    result = supervisor_instance._read_gateway_heartbeat()
    assert result is not None
    assert result["status"] == "alive"


def test_read_gateway_heartbeat_invalid_json(supervisor_instance, tmp_path):
    """测试读取无效 JSON 心跳文件返回 None。"""
    heartbeat_file = tmp_path / "bad_heartbeat.json"
    heartbeat_file.write_text("not valid json", encoding="utf-8")

    mock_paths = MagicMock()
    mock_paths.heartbeat = heartbeat_file
    supervisor_instance._paths = mock_paths

    result = supervisor_instance._read_gateway_heartbeat()
    assert result is None


# ── 健康检测测试 ──────────────────────────────────────────────────────────

@patch.object(sv, "get_running_pid", return_value=None)
def test_is_gateway_healthy_no_pid(mock_get_pid, supervisor_instance):
    """测试无 PID 文件时网关不健康。"""
    mock_paths = MagicMock()
    mock_paths.pid = Path("/nonexistent/pid")
    mock_paths.heartbeat = Path("/nonexistent/heartbeat")
    supervisor_instance._paths = mock_paths

    result = supervisor_instance._is_gateway_healthy()
    assert result is False


@patch.object(sv, "get_running_pid", return_value=12345)
@patch.object(sv, "is_pid_alive", return_value=True)
@patch.object(sv.time, "time", return_value=time.time())
def test_is_gateway_healthy_with_heartbeat(mock_time, mock_alive, mock_get_pid, supervisor_instance, tmp_path):
    """测试有心跳且未过期时网关健康。"""
    heartbeat_file = tmp_path / "heartbeat.json"
    heartbeat_data = {"updated_at": time.time() - 10}  # 10秒前更新，未超过30秒超时
    heartbeat_file.write_text(json.dumps(heartbeat_data), encoding="utf-8")

    mock_paths = MagicMock()
    mock_paths.pid = Path("/pid")
    mock_paths.heartbeat = heartbeat_file
    supervisor_instance._paths = mock_paths

    result = supervisor_instance._is_gateway_healthy()
    assert result is True


@patch.object(sv, "get_running_pid", return_value=12345)
@patch.object(sv, "is_pid_alive", return_value=False)
def test_is_gateway_healthy_pid_dead(mock_alive, mock_get_pid, supervisor_instance):
    """测试 PID 存在但进程已死时网关不健康。"""
    mock_paths = MagicMock()
    mock_paths.pid = Path("/pid")
    mock_paths.heartbeat = Path("/nonexistent")
    supervisor_instance._paths = mock_paths

    result = supervisor_instance._is_gateway_healthy()
    assert result is False


@patch.object(sv, "get_running_pid", return_value=12345)
@patch.object(sv, "is_pid_alive", return_value=True)
@patch("agent_py_agent.agent.gateway_parts.supervisor.time.time", return_value=time.time())
def test_is_gateway_healthy_stale_heartbeat(mock_time, mock_alive, mock_get_pid, supervisor_instance, tmp_path):
    """测试心跳过期时网关不健康。"""
    heartbeat_file = tmp_path / "stale_heartbeat.json"
    heartbeat_data = {"updated_at": time.time() - 100}  # 100秒前，超过30秒超时
    heartbeat_file.write_text(json.dumps(heartbeat_data), encoding="utf-8")

    # 直接设置 _paths 和 _agent，避免调用 _resolve_agent_and_paths
    mock_paths = MagicMock()
    mock_paths.pid = Path("/pid")
    mock_paths.heartbeat = heartbeat_file
    supervisor_instance._paths = mock_paths
    supervisor_instance._agent = MagicMock()  # 设置 agent 避免重新解析

    result = supervisor_instance._is_gateway_healthy()
    assert result is False


# ── 适配器健康检测测试 ─────────────────────────────────────────────────────

@patch.object(sv, "get_running_pid", return_value=None)
def test_check_adapter_health_no_pid(mock_get_pid, supervisor_instance):
    """测试无适配器 PID 时返回不健康。"""
    mock_paths = MagicMock()
    mock_paths.adapter_pid = Path("/nonexistent/adapter.pid")
    supervisor_instance._paths = mock_paths

    result = supervisor_instance._check_adapter_health()
    assert result is False


@patch.object(sv, "get_running_pid", return_value=12345)
@patch.object(sv, "is_pid_alive", return_value=True)
def test_check_adapter_health_pid_alive_no_state(mock_alive, mock_get_pid, supervisor_instance, tmp_path):
    """测试适配器 PID 存活但无状态文件时返回健康（假设健康）。"""
    # 直接设置 _paths 和 _agent
    mock_paths = MagicMock()
    mock_paths.adapter_pid = tmp_path / "adapter.pid"
    mock_paths.root = tmp_path
    supervisor_instance._paths = mock_paths
    supervisor_instance._agent = MagicMock()  # 避免重新解析

    # adapter_state.json 不存在
    result = supervisor_instance._check_adapter_health()
    assert result is True


@patch.object(sv, "get_running_pid", return_value=12345)
@patch.object(sv, "is_pid_alive", return_value=True)
def test_check_adapter_health_running_state(mock_alive, mock_get_pid, supervisor_instance, tmp_path):
    """测试适配器状态为 running 时返回健康。"""
    state_file = tmp_path / "adapter_state.json"
    state_data = {"state": "running"}
    state_file.write_text(json.dumps(state_data), encoding="utf-8")

    mock_paths = MagicMock()
    mock_paths.adapter_pid = Path("/adapter.pid")
    mock_paths.root = tmp_path
    supervisor_instance._paths = mock_paths

    result = supervisor_instance._check_adapter_health()
    assert result is True


# ── 重启限制测试 ──────────────────────────────────────────────────────────

def test_restart_gateway_max_attempts_exceeded(supervisor_instance):
    """测试超过最大重启次数后不再重启。"""
    supervisor_instance._restart_count = supervisor_instance.max_restart_attempts
    supervisor_instance._last_restart_at = 0

    with patch.object(supervisor_instance, "_log_error") as mock_log:
        result = supervisor_instance._restart_gateway()

    assert result is False
    mock_log.assert_called()


def test_restart_gateway_cooldown_active(supervisor_instance):
    """测试重启冷却期内不重启。"""
    supervisor_instance._restart_count = 0
    supervisor_instance._last_restart_at = time.time()  # 刚刚重启

    with patch.object(supervisor_instance, "_log_info") as mock_log:
        result = supervisor_instance._restart_gateway()

    assert result is False
    mock_log.assert_called()


# ── 信号处理测试 ──────────────────────────────────────────────────────────

def test_handle_signal_sets_stop_requested(supervisor_instance):
    """测试信号处理设置停止标志。"""
    assert supervisor_instance._stop_requested is False

    supervisor_instance._handle_signal(signal.SIGTERM, None)

    assert supervisor_instance._stop_requested is True


# ── is_supervisor_running 测试 ────────────────────────────────────────────

def test_is_supervisor_running_no_pid_file():
    """测试无 PID 文件时返回未运行。"""
    mock_gp = MagicMock(root=Path("/tmp"))
    with patch.object(sv, "read_pid_file", return_value=None), \
         patch.object(sv, "is_pid_alive", return_value=False), \
         patch("agent_py_agent.agent.core.SimpleAgent"), \
         patch("agent_py_agent.agent.config.load_config"), \
         patch("agent_py_agent.agent.gateway_parts.gateway_paths", return_value=mock_gp):
        result = sv.is_supervisor_running("dummy.yaml")
    assert result is False


@patch.object(sv, "read_pid_file", return_value=12345)
@patch.object(sv, "is_pid_alive", return_value=True)
def test_is_supervisor_running_alive(mock_alive, mock_read):
    """测试 supervisor 进程存活时返回运行中。"""
    with patch("agent_py_agent.agent.core.SimpleAgent"):
        with patch("agent_py_agent.agent.config.load_config"):
            with patch("agent_py_agent.agent.gateway_parts.gateway_paths") as mock_gp:
                mock_gp.return_value = MagicMock(root=Path("/tmp"))
                result = sv.is_supervisor_running("dummy.yaml")
    assert result is True


# ── stop_supervisor 测试 ──────────────────────────────────────────────────

@patch.object(sv, "read_pid_file", return_value=None)
@patch.object(sv, "is_pid_alive", return_value=False)
def test_stop_supervisor_not_running(mock_alive, mock_read):
    """测试 supervisor 未运行时返回成功。"""
    with patch("agent_py_agent.agent.core.SimpleAgent"):
        with patch("agent_py_agent.agent.config.load_config"):
            with patch("agent_py_agent.agent.gateway_parts.gateway_paths") as mock_gp:
                mock_gp.return_value = MagicMock(root=Path("/tmp"))
                result = sv.stop_supervisor("dummy.yaml")
    assert result is True


# ── 异常场景测试 ──────────────────────────────────────────────────────────

def test_read_gateway_heartbeat_permission_error(supervisor_instance, tmp_path):
    """测试读取心跳文件权限错误时返回 None。"""
    heartbeat_file = tmp_path / "heartbeat.json"
    heartbeat_file.write_text("{}", encoding="utf-8")

    # 直接设置 _paths 和 _agent
    mock_paths = MagicMock()
    mock_paths.heartbeat = heartbeat_file
    supervisor_instance._paths = mock_paths
    supervisor_instance._agent = MagicMock()  # 避免重新解析

    # 模拟读取时权限错误 - 通过 mock json.loads 让 read_text 返回空
    with patch("pathlib.Path.read_text", side_effect=PermissionError):
        result = supervisor_instance._read_gateway_heartbeat()
    assert result is None


def test_write_pid_record_creates_directory(tmp_path):
    """测试写入 PID 记录时自动创建父目录。"""
    from agent_py_agent.agent.gateway_parts import daemon_control as dc
    pid_path = tmp_path / "subdir" / "gateway.pid"
    dc.write_pid_record(pid_path)
    assert pid_path.exists()


def test_write_pid_file(tmp_path):
    """测试写入纯文本 PID 文件。"""
    from agent_py_agent.agent.gateway_parts import daemon_control as dc
    pid_path = tmp_path / "plain.pid"
    dc.write_pid_file(pid_path, 54321)
    assert pid_path.read_text(encoding="utf-8") == "54321"