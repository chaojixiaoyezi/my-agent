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
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from agent_py_agent.agent.gateway_parts import supervisor as sv
from agent_py_agent.agent.gateway_parts.daemon_control import RunningPidReport


# LLM: 监护状态入口现在复用正式CLI配置解析；这里显式补真实YAML，不给生产增加dummy路径兼容。
# 函数用途: 给原PID/stop fake创建无网络宿主配置，原刺激及断言保持。
def _public_supervisor_config(tmp_path):
    path = tmp_path / "agent.yaml"
    path.write_text(f'model_backend: echo\nmy_agent_home: "{tmp_path / "home"}"\ngateway_worker_process_count: 0\n')
    return str(path)


# ── 测试夹具 ──────────────────────────────────────────────────────────────

@pytest.fixture
def mock_agent_and_paths(tmp_path):
    """创建模拟的 agent 和 paths 对象。"""
    # 创建临时配置目录
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "agent_config.yaml"
    config_file.write_text(f'user_id: test\nmodel_backend: echo\nmy_agent_home: "{tmp_path / "home"}"\ngateway_worker_process_count: 0\n', encoding="utf-8")

    # 创建 workspace
    workspace = tmp_path / "home/owners/local/main/workspace"
    workspace.mkdir(parents=True)

    return config_file, workspace


@pytest.fixture
def supervisor_instance(tmp_path, mock_agent_and_paths):
    """创建 GatewaySupervisor 实例用于测试。"""
    config_file, workspace = mock_agent_and_paths

    # 临时日志路径
    log_path = tmp_path / "supervisor.log"

    from agent_py_agent.cli.common import make_agent

    agent = make_agent(Namespace(config=str(config_file), workspace_root=str(workspace)))
    supervisor = sv.GatewaySupervisor(
        config_path=str(config_file), agent=agent,
        options=sv.SupervisorConfig(
            heartbeat_timeout=30.0,
            check_interval=1.0,
            max_restart_attempts=3,
            restart_cooldown=5.0,
            log_path=log_path,
        ),
    )
    return supervisor


# ── supervisor 守护进程命令(回归) ──────────────────────────────────────────

def test_supervisor_daemon_cmd_includes_gateway():
    """回归:supervisor 守护进程启动命令必须是 'gateway supervisor'(顶层无 supervisor 命令)。
    曾经 start-all 漏 'gateway' → invalid choice: supervisor → 看门狗起不来、gateway 无崩溃自愈。"""
    from agent_py_agent.cli.supervisor import _supervisor_daemon_cmd
    cmd = _supervisor_daemon_cmd("/path/cfg.yaml")
    assert cmd[cmd.index("supervisor") - 1] == "gateway"  # gateway 紧跟在 supervisor 前
    assert "run" not in cmd  # 不是错误的 'supervisor run'


# ── 初始化测试 ─────────────────────────────────────────────────────────────

def test_supervisor_init_defaults():
    """测试 GatewaySupervisor 默认参数初始化。"""
    supervisor = sv.GatewaySupervisor("dummy_config.yaml", agent=SimpleNamespace(config=SimpleNamespace(config_path="dummy_config.yaml")))
    assert supervisor.heartbeat_timeout == 120.0
    assert supervisor.check_interval == 10.0
    assert supervisor.max_restart_attempts == 5
    assert supervisor.restart_cooldown == 30.0


def test_supervisor_init_custom_params():
    """测试 GatewaySupervisor 自定义参数初始化。"""
    supervisor = sv.GatewaySupervisor(
        config_path="test.yaml", agent=SimpleNamespace(config=SimpleNamespace(config_path="test.yaml")),
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
    supervisor = sv.GatewaySupervisor("test.yaml", agent=SimpleNamespace(config=SimpleNamespace(config_path="test.yaml")))
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


def test_read_gateway_heartbeat_report_invalid_json(supervisor_instance, tmp_path):
    """坏 heartbeat JSON 要有结构化读取错误。"""
    heartbeat_file = tmp_path / "bad_heartbeat.json"
    heartbeat_file.write_text("{bad heartbeat", encoding="utf-8")

    mock_paths = MagicMock()
    mock_paths.heartbeat = heartbeat_file
    supervisor_instance._paths = mock_paths

    report = supervisor_instance._read_gateway_heartbeat_report()

    assert report.payload is None
    assert report.load_error is not None
    assert report.load_error["context"] == "gateway.supervisor.heartbeat.read"
    assert report.load_error["path"] == str(heartbeat_file)


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


def test_is_gateway_healthy_with_heartbeat(supervisor_instance, tmp_path):
    """测试有心跳且未过期时网关健康。"""
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    paths = gateway_paths_from_root(tmp_path)
    now = time.time()
    identity = {"host_id": "test-host", "pid": 12345, "start_time": "12345"}
    paths.pid.write_text(json.dumps({**identity, "kind": "my-agent-gateway"}))
    paths.state.write_text(json.dumps({"pid": 12345, "process_identity": identity,
                                      "started_at": now - 20, "status": "running"}))
    paths.heartbeat.write_text(json.dumps({"pid": 12345, "started_at": now - 20,
        "updated_at": now - 10, "status": "running", "dispatcher_alive": True, "dispatcher_state": "running"}))
    from types import SimpleNamespace

    supervisor_instance._paths = paths
    supervisor_instance._agent = SimpleNamespace(config=SimpleNamespace(gateway_worker_process_count=0))
    with patch.object(sv, "get_running_pid_report", return_value=RunningPidReport(12345)), \
         patch.object(sv, "process_identity_is_live", return_value=True), \
         patch.object(sv.time, "time", return_value=now):
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


def test_is_gateway_healthy_stale_heartbeat(supervisor_instance, tmp_path):
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

    with patch.object(sv, "get_running_pid", return_value=12345), \
         patch.object(sv, "is_pid_alive", return_value=True), \
         patch("agent_py_agent.agent.gateway_parts.supervisor.time.time", return_value=time.time()):
        result = supervisor_instance._is_gateway_healthy()
    assert result is False


def test_is_gateway_healthy_bad_heartbeat_is_not_startup_phase(supervisor_instance, tmp_path):
    """坏 heartbeat 不能被当成 gateway 启动期健康。"""
    heartbeat_file = tmp_path / "bad_heartbeat.json"
    heartbeat_file.write_text("{bad heartbeat", encoding="utf-8")

    mock_paths = MagicMock()
    mock_paths.pid = tmp_path / "gateway.pid"
    mock_paths.heartbeat = heartbeat_file
    supervisor_instance._paths = mock_paths
    supervisor_instance._agent = MagicMock()

    with patch.object(sv, "get_running_pid_report", return_value=RunningPidReport(12345)), \
         patch.object(sv, "is_pid_alive", return_value=True):
        result = supervisor_instance._is_gateway_healthy()

    assert result is False
    assert supervisor_instance._last_health_load_errors
    assert supervisor_instance._last_health_load_errors[0]["context"] == "gateway.supervisor.heartbeat.read"


# ── 适配器健康检测测试 ─────────────────────────────────────────────────────

@patch.object(sv, "get_running_pid", return_value=None)
def test_check_adapter_health_no_pid(mock_get_pid, supervisor_instance):
    """测试无适配器 PID 时返回不健康。"""
    mock_paths = MagicMock()
    mock_paths.adapter_pid = Path("/nonexistent/adapter.pid")
    supervisor_instance._paths = mock_paths

    result = supervisor_instance._check_adapter_health()
    assert result is False


@patch.object(sv, "get_running_pid_report", return_value=RunningPidReport(12345))
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


@patch.object(sv, "get_running_pid_report", return_value=RunningPidReport(12345))
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

def test_is_supervisor_running_no_pid_file(tmp_path):
    config_path = _public_supervisor_config(tmp_path)
    """测试无 PID 文件时返回未运行。"""
    mock_gp = MagicMock(root=Path(config_path).parent, supervisor_pid=Path(config_path).parent / "supervisor.pid")
    with patch.object(sv, "read_pid_file", return_value=None), \
         patch.object(sv, "is_pid_alive", return_value=False), \
         patch("agent_py_agent.agent.core.SimpleAgent"), \
         patch("agent_py_agent.agent.gateway_parts.gateway_paths", return_value=mock_gp):
        result = sv.is_supervisor_running(mock_gp)
    assert result is False


@patch.object(sv, "read_pid_file", return_value=12345)
@patch.object(sv, "is_pid_alive", return_value=True)
def test_is_supervisor_running_alive(mock_alive, mock_read, tmp_path):
    config_path = _public_supervisor_config(tmp_path)
    """测试 supervisor 进程存活时返回运行中。"""
    (tmp_path / "supervisor.pid").write_text("12345", encoding="utf-8")
    with patch("agent_py_agent.agent.core.SimpleAgent"), \
         patch.object(sv, "gateway_paths") as mock_gp:
        mock_gp.return_value = MagicMock(root=tmp_path, supervisor_pid=tmp_path / "supervisor.pid")
        result = sv.is_supervisor_running(mock_gp.return_value)
    assert result is True


# ── stop_supervisor 测试 ──────────────────────────────────────────────────

@patch.object(sv, "read_pid_file", return_value=None)
@patch.object(sv, "is_pid_alive", return_value=False)
def test_stop_supervisor_not_running(mock_alive, mock_read, tmp_path):
    config_path = _public_supervisor_config(tmp_path)
    """测试 supervisor 未运行时返回成功。"""
    with patch("agent_py_agent.agent.core.SimpleAgent"), \
         patch("agent_py_agent.agent.gateway_parts.gateway_paths") as mock_gp:
        mock_gp.return_value = MagicMock(root=Path(config_path).parent, supervisor_pid=Path(config_path).parent / "supervisor.pid")
        result = sv.stop_supervisor(mock_gp.return_value)
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
