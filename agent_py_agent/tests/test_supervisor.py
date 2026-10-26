# LLM: 平台可选信号及诊断API只在测试内模拟；文本IO显式UTF-8，不用跳过Windows合同或向真实PID发信号。
# 模块用途: 回归监护器健康、代际强停和跨平台诊断退化行为。
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


# LLM: 监护状态入口复用正式CLI配置解析；真实YAML固定UTF-8，不给生产增加dummy路径兼容。
# 函数用途: 给原PID/stop fake创建无网络宿主配置，原刺激及断言保持。
def _public_supervisor_config(tmp_path):
    path = tmp_path / "agent.yaml"
    path.write_text(f'model_backend: echo\nmy_agent_home: "{tmp_path / "home"}"\ngateway_worker_process_count: 0\n', encoding="utf-8")
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


# LLM: 使用UTF-8真实状态文件与替身身份检查，不能把有心跳本身当作就绪。
# 函数用途: 验证同代状态与新鲜dispatcher心跳同时成立时健康。
def test_is_gateway_healthy_with_heartbeat(supervisor_instance, tmp_path):
    """测试有心跳且未过期时网关健康。"""
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    paths = gateway_paths_from_root(tmp_path)
    now = time.time()
    identity = {"host_id": "test-host", "pid": 12345, "start_time": "12345"}
    paths.pid.write_text(json.dumps({**identity, "kind": "my-agent-gateway"}), encoding="utf-8")
    paths.state.write_text(json.dumps({"pid": 12345, "process_identity": identity,
                                      "started_at": now - 20, "status": "running"}), encoding="utf-8")
    paths.heartbeat.write_text(json.dumps({"pid": 12345, "started_at": now - 20,
        "updated_at": now - 10, "status": "running", "dispatcher_alive": True, "dispatcher_state": "running"}), encoding="utf-8")
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


# LLM: 真文件固定UTF-8，POSIX诊断能力仅在夹具补齐；信号/等待均替身，保留真实kill_pid供缺信号回归复用，不操作真实Gateway。
# 函数用途: 构造绑定原代身份的停止与启动测试环境。
@pytest.fixture
def incident_gateway(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

    monkeypatch.setattr(signal, "SIGUSR1", getattr(signal, "SIGUSR1", 30), raising=False)
    paths = gateway_paths_from_root(tmp_path)
    identity = {"host_id": "test-host", "pid": 12345, "start_time": "original"}
    paths.pid.write_text(json.dumps({**identity, "kind": "my-agent-gateway"}), encoding="utf-8")
    paths.stop_request.write_text(json.dumps({"target_process": identity}), encoding="utf-8")
    paths.state.write_text(json.dumps({"pid": 12345, "process_identity": identity}), encoding="utf-8")
    agent = SimpleNamespace(config=SimpleNamespace(config_path="test.yaml", gateway_worker_process_count=0))
    supervisor = sv.GatewaySupervisor("test.yaml", agent=agent)
    supervisor._paths = paths
    events = []
    real_kill_pid = sv.kill_pid
    monkeypatch.setattr(sv, "get_running_pid_report", lambda *a, **k: RunningPidReport(12345))
    monkeypatch.setattr(sv, "process_identity_is_live", lambda value: value == identity)
    monkeypatch.setattr(sv, "_write_supervisor_stop_request", lambda *a: {"target_process": identity})
    monkeypatch.setattr(sv, "terminate_pid", lambda pid: events.append(("TERM", pid)))
    monkeypatch.setattr(sv, "kill_pid", lambda pid: events.append(("KILL", pid)), raising=False)
    monkeypatch.setattr(sv.os, "kill", lambda pid, sig: events.append((sig, pid)))
    monkeypatch.setattr(supervisor, "_log_warn", lambda text: events.append(("WARN", text)))
    return SimpleNamespace(supervisor=supervisor, paths=paths, identity=identity, events=events, real_kill_pid=real_kill_pid)


# LLM: 删除可选POSIX能力而非跳过；恢复真实kill_pid但os.kill仍由夹具记录，不向真实PID发信号。
# 函数用途: 验证Windows式缺信号环境能安装/恢复停止处理，并按TERM到强停后清理旧代。
def test_incident_windows_without_sigusr1_or_faulthandler_register(incident_gateway, monkeypatch):
    import faulthandler

    from agent_py_agent.cli import gateway_process as gp

    case = incident_gateway
    for name in ("SIGUSR1", "SIGKILL"):
        monkeypatch.delattr(signal, name, raising=False)
    for name in ("register", "unregister"):
        monkeypatch.delattr(faulthandler, name, raising=False)
    handlers = []
    monkeypatch.setattr(signal, "getsignal", lambda _: "previous")
    monkeypatch.setattr(signal, "signal", lambda *args: handlers.append(args))
    monkeypatch.setattr(faulthandler, "enable", lambda **kwargs: None)
    previous = gp._install_gateway_signal_handlers(case.paths, case.identity)
    assert set(previous) == {signal.SIGTERM, signal.SIGINT}
    assert not previous.diagnostic_published
    assert json.loads(case.paths.state.read_text(encoding="utf-8")).get("diagnostic_sigusr1") is not True
    gp._restore_gateway_signal_handlers(previous)
    assert handlers[-2:] == [(signal.SIGTERM, "previous"), (signal.SIGINT, "previous")]
    assert json.loads(case.paths.state.read_text(encoding="utf-8")).get("diagnostic_sigusr1") is not True
    monkeypatch.setattr(sv, "kill_pid", case.real_kill_pid)
    wait = MagicMock(side_effect=[False, False, True])
    monkeypatch.setattr(sv, "_wait_gateway_exit", wait)
    assert sv.stop_gateway(case.supervisor)
    assert [call.args[1:] for call in wait.call_args_list] == [
        (12345, case.identity, 20.0), (12345, case.identity, 5), (12345, case.identity, 5)]
    assert case.events.index(("TERM", 12345)) < case.events.index((signal.SIGTERM, 12345))
    assert not case.paths.pid.exists() and not case.paths.stop_request.exists()


# LLM: 只在两个等待失败且原代仍活时允许Count0强停；三次等待沿同一identity。
# 函数用途: 验证停止请求、TERM、KILL的顺序和旧代文件清理。
def test_incident_stop_escalates_same_generation_and_cleans_only_old_records(incident_gateway, monkeypatch):
    case = incident_gateway
    waits = iter([False, False, True])
    monkeypatch.setattr(sv, "_wait_gateway_exit", lambda *a: next(waits))
    assert sv.stop_gateway(case.supervisor)
    assert ("TERM", 12345) in case.events and ("KILL", 12345) in case.events
    assert case.events.index(("TERM", 12345)) < case.events.index(("KILL", 12345))
    assert not case.paths.pid.exists() and not case.paths.stop_request.exists()


# LLM: 末次身份核验失败不是原代存活，既不能强杀也不能把记录清成空槽。
# 函数用途: 验证PID复用和无法证明出生时不升级KILL。
@pytest.mark.parametrize("last_live", [False, None])
def test_incident_stop_never_kills_changed_or_unknown_generation(incident_gateway, monkeypatch, last_live):
    case = incident_gateway
    checks = []

    # LLM: 第四次观测是诊断后TERM末核之后的KILL末核，不用早期拒止冒充升级闸。
    # 函数用途: 在真正准备KILL时模拟身份变化。
    def live(identity):
        checks.append(identity)
        return True if len(checks) < 4 else last_live

    monkeypatch.setattr(sv, "process_identity_is_live", live)
    monkeypatch.setattr(sv, "_wait_gateway_exit", lambda *a: False)
    assert not sv.stop_gateway(case.supervisor)
    assert len(checks) >= 4 and ("KILL", 12345) not in case.events
    assert case.paths.pid.exists() and case.paths.stop_request.exists()


# LLM: 多进程代次和未退出目标均保留原责任，不能以主PID信号伪造整代关闭。
# 函数用途: 验证CountN禁强停及KILL后未退出不报成功。
@pytest.mark.parametrize("count", [0, 2])
def test_incident_stop_retains_unconfirmed_process_records(incident_gateway, monkeypatch, count):
    case = incident_gateway
    case.supervisor._agent.config.gateway_worker_process_count = count
    monkeypatch.setattr(sv, "_wait_gateway_exit", lambda *a: False)
    assert not sv.stop_gateway(case.supervisor)
    assert (("KILL", 12345) in case.events) is (count == 0)
    assert case.paths.pid.exists() and case.paths.stop_request.exists()


# LLM: 强停等待期间出现的后继记录不属于旧代，清理必须保持原目标绑定。
# 函数用途: 确认强停后不删除已换代的PID和stop请求。
def test_incident_stop_preserves_successor_files(incident_gateway, monkeypatch):
    case = incident_gateway
    replacement = {**case.identity, "start_time": "successor"}
    waits = iter([False, False, True])

    # LLM: 仅替身等待边界发布后继，验证原清理不能跨代删文件。
    # 函数用途: 在KILL退出确认时切换目标文件代次。
    def wait(*args):
        result = next(waits)
        if result:
            case.paths.pid.write_text(json.dumps({**replacement, "kind": "my-agent-gateway"}), encoding="utf-8")
            case.paths.stop_request.write_text(json.dumps({"target_process": replacement}), encoding="utf-8")
        return result

    monkeypatch.setattr(sv, "_wait_gateway_exit", wait)
    assert sv.stop_gateway(case.supervisor)
    assert json.loads(case.paths.pid.read_text(encoding="utf-8"))["start_time"] == "successor"
    assert json.loads(case.paths.stop_request.read_text(encoding="utf-8"))["target_process"] == replacement


# LLM: 使用真实未就绪健康链；只替换时间/OS和末次ready，不启动服务。
# 函数用途: 验证启动瞬态不刷WARN，成功就绪保持原返回PID。
def test_incident_start_wait_suppresses_transient_unconfirmed(incident_gateway, monkeypatch):
    from agent_py_agent.agent.gateway_parts import supervisor_generation

    case = incident_gateway
    case.paths.pid.unlink()
    original = case.supervisor._is_gateway_healthy
    calls = []

    # LLM: 前两轮仍走真实健康拒绝链，第三轮才给可控ready。
    # 函数用途: 模拟启动时健康状态逐步成立。
    def healthy(**kwargs):
        calls.append(kwargs)
        return original(**kwargs) if len(calls) < 3 else True

    monkeypatch.setattr(case.supervisor, "_is_gateway_healthy", healthy)
    monkeypatch.setattr(sv.time, "sleep", lambda _: None)
    monkeypatch.setattr(supervisor_generation, "complete_gateway_start", lambda *a: 12345)
    assert sv._wait_for_gateway_start(case.supervisor, SimpleNamespace(pid=12345, poll=lambda: None)) == 12345
    assert len(calls) == 3
    assert not [e for e in case.events if e[0] == "WARN"]


# LLM: 超时最终做一次非静音原健康核验，不抹掉结构化拒绝原因。
# 函数用途: 验证启动期多轮未就绪只留一条最终原因告警。
def test_incident_start_timeout_logs_one_final_reason(incident_gateway, monkeypatch):
    case = incident_gateway
    case.paths.pid.unlink()
    ticks = iter([0, 1, 2, 121])
    monkeypatch.setattr(sv.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(sv.time, "sleep", lambda _: None)
    assert sv._wait_for_gateway_start(case.supervisor, SimpleNamespace(pid=12345, poll=lambda: None)) is None
    warnings = [e[1] for e in case.events if e[0] == "WARN"]
    assert len(warnings) == 1 and "gateway_role_or_pid_unconfirmed" in warnings[0]


# LLM: 环境默认值不能覆盖服务管理器已设值，Popen边界完全受控。
# 函数用途: 验证低碎片默认值注入并保留显式值。
@pytest.mark.parametrize("override", [False, True])
def test_incident_spawn_sets_allocator_defaults(incident_gateway, monkeypatch, override):
    case = incident_gateway
    case.supervisor._agent.home_paths = SimpleNamespace(root=case.paths.root)
    for name in ("MALLOC_ARENA_MAX", "MALLOC_TRIM_THRESHOLD_"):
        monkeypatch.delenv(name, raising=False)
    if override:
        monkeypatch.setenv("MALLOC_ARENA_MAX", "4")
        monkeypatch.setenv("MALLOC_TRIM_THRESHOLD_", "262144")
    captured = []
    monkeypatch.setattr(sv.subprocess, "Popen", lambda *a, **k: captured.append(k))
    sv._spawn_gateway_process(case.supervisor, ["python"], 0, True)
    assert captured[0]["env"]["MALLOC_ARENA_MAX"] == ("4" if override else "2")
    assert captured[0]["env"]["MALLOC_TRIM_THRESHOLD_"] == ("262144" if override else "131072")


# LLM: 替换全局信号和faulthandler边界，注册本身不对测试进程安装真实handler。
# 函数用途: 验证诊断注册、身份绑定公告与恢复注销。
def test_incident_gateway_registers_and_restores_diagnostic_signal(incident_gateway, monkeypatch):
    import faulthandler

    from agent_py_agent.cli import gateway_process as gp

    case = incident_gateway
    events = []
    monkeypatch.setattr(signal, "getsignal", lambda _: "previous")
    monkeypatch.setattr(signal, "signal", lambda *a: None)
    monkeypatch.setattr(faulthandler, "enable", lambda **k: events.append(("enable", k)))
    monkeypatch.setattr(faulthandler, "register", lambda sig, **k: events.append(("register", sig, k)), raising=False)
    monkeypatch.setattr(faulthandler, "unregister", lambda sig: events.append(("unregister", sig)), raising=False)
    previous = gp._install_gateway_signal_handlers(case.paths, case.identity)
    assert ("enable", {"all_threads": True}) in events
    if hasattr(signal, "SIGUSR1"):
        assert any(e[0] == "register" and e[1] == signal.SIGUSR1 and e[2] == {
            "file": sys.stderr, "all_threads": True, "chain": False} for e in events)
        assert json.loads(case.paths.state.read_text(encoding="utf-8"))["diagnostic_sigusr1"] is True
    gp._restore_gateway_signal_handlers(previous)
    if hasattr(signal, "SIGUSR1"):
        assert ("unregister", signal.SIGUSR1) in events
        assert json.loads(case.paths.state.read_text(encoding="utf-8"))["diagnostic_sigusr1"] is False


# LLM: AST覆盖本文件所有state写点，避免仅修running但终态仍截断；不把语法存在当业务通过。
# 函数用途: 锁住Gateway状态发布必须走原子写入口的合同。
def test_incident_gateway_state_never_uses_truncating_writer():
    import ast

    from agent_py_agent.cli import gateway_process as gp

    tree = ast.parse(Path(gp.__file__).read_text(encoding="utf-8"))
    bad = [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Call)
           and isinstance(node.func, ast.Name) and node.func.id == "write_json_file"
           and node.args and isinstance(node.args[0], ast.Attribute) and node.args[0].attr == "state"]
    assert bad == []


# LLM: proc输入在临时目录，信号/等待全替换；覆盖真实停止入口而不是仅测辅助函数存在。
# 函数用途: 验证Linux摘要最多16线程且采栈信号先于TERM。
def test_incident_stop_captures_bounded_linux_diagnostics_before_term(incident_gateway, monkeypatch, tmp_path):
    case = incident_gateway
    proc = tmp_path / "proc-fixture"
    proc.mkdir()
    (proc / "status").write_text("State:\tS\nVmRSS:\t1234 kB\nVmSwap:\t0 kB\nThreads:\t20\n", encoding="utf-8")
    (proc / "wchan").write_text("futex_wait", encoding="utf-8")
    for tid in range(20):
        task = proc / "task" / str(tid)
        task.mkdir(parents=True)
        (task / "wchan").write_text("futex_wait" * 100, encoding="utf-8")
    real_path = Path
    monkeypatch.setattr(sv, "Path", lambda name: proc if str(name) == "/proc/12345" else real_path(name))
    monkeypatch.setattr(sv.sys, "platform", "linux")
    case.paths.state.write_text(json.dumps({"pid": 12345, "process_identity": case.identity, "diagnostic_sigusr1": True}), encoding="utf-8")
    waits = iter([False, True])
    monkeypatch.setattr(sv, "_wait_gateway_exit", lambda *a: next(waits))
    assert sv.stop_gateway(case.supervisor)
    assert case.events.index((signal.SIGUSR1, 12345)) < case.events.index(("TERM", 12345))
    summary = json.loads(next(e[1].split("：", 1)[1] for e in case.events
                              if e[0] == "WARN" and "停止超时诊断" in e[1]))
    assert {k: summary[k] for k in ("State", "VmRSS", "VmSwap", "Threads")} == {
        "State": "S", "VmRSS": "1234 kB", "VmSwap": "0 kB", "Threads": "20"}
    assert len(summary["task_wchan"]) == 16
    assert all(len(value) <= 128 for value in summary["task_wchan"].values())


# LLM: 注册公告不足以授信号权，发送前出生核验失败必须同时阻止诊断及后续TERM。
# 函数用途: 验证采集期间换代或不可核验时不向新目标发信号。
@pytest.mark.parametrize("observed", [False, None])
def test_incident_diagnostics_refuses_signal_after_identity_changes(incident_gateway, monkeypatch, observed):
    case = incident_gateway
    case.paths.state.write_text(json.dumps({"pid": 12345, "process_identity": case.identity, "diagnostic_sigusr1": True}), encoding="utf-8")
    checks = iter([True, True, observed])
    monkeypatch.setattr(sv, "process_identity_is_live", lambda _: next(checks))
    monkeypatch.setattr(sv, "_wait_gateway_exit", lambda *a: False)
    assert not sv.stop_gateway(case.supervisor)
    assert (signal.SIGUSR1, 12345) not in case.events
    assert ("TERM", 12345) not in case.events and ("KILL", 12345) not in case.events


# LLM: 诊断能力不是旧版默认能力；没有同代公告时不发可致退出的SIGUSR1。
# 函数用途: 验证旧版和异代诊断公告不会触发盲发。
@pytest.mark.parametrize("fault", ["missing", "other_identity", "disabled"])
def test_incident_diagnostics_never_blindly_signals_legacy_gateway(incident_gateway, monkeypatch, fault):
    case = incident_gateway
    state = {"pid": 12345, "process_identity": case.identity, "diagnostic_sigusr1": fault != "disabled"}
    if fault == "missing":
        state.pop("diagnostic_sigusr1")
    elif fault == "other_identity":
        state["process_identity"] = {**case.identity, "start_time": "other"}
    case.paths.state.write_text(json.dumps(state), encoding="utf-8")
    waits = iter([False, True])
    monkeypatch.setattr(sv, "_wait_gateway_exit", lambda *a: next(waits))
    assert sv.stop_gateway(case.supervisor)
    assert ("TERM", 12345) in case.events and (signal.SIGUSR1, 12345) not in case.events


# LLM: faulthandler失败只降低诊断能力，不能破坏原TERM/INT安装或公告不存在的注册。
# 函数用途: 覆盖各诊断注册/恢复接口的预期平台异常。
@pytest.mark.parametrize("operation", ["enable", "register", "unregister"])
@pytest.mark.parametrize("error", [OSError, RuntimeError, ValueError])
def test_incident_fault_handler_errors_preserve_stop_handlers(incident_gateway, monkeypatch, operation, error):
    import faulthandler

    from agent_py_agent.cli import gateway_process as gp

    case, calls = incident_gateway, []
    monkeypatch.setattr(signal, "getsignal", lambda _: "old")
    monkeypatch.setattr(signal, "signal", lambda sig, handler: calls.append(("signal", sig)))

    # LLM: 仅诊断API替身按选定异常失败，原停止处理仍由产品安装。
    # 函数用途: 模拟平台诊断接口失败。
    def maybe_fail(name, *args, **kwargs):
        calls.append((name, args))
        if name == operation:
            raise error("synthetic diagnostic failure")

    monkeypatch.setattr(faulthandler, "enable", lambda **k: maybe_fail("enable", **k))
    monkeypatch.setattr(faulthandler, "register", lambda *a, **k: maybe_fail("register", *a, **k), raising=False)
    monkeypatch.setattr(faulthandler, "unregister", lambda *a: maybe_fail("unregister", *a), raising=False)
    previous = gp._install_gateway_signal_handlers(case.paths, case.identity)
    assert signal.SIGTERM in previous and signal.SIGINT in previous
    if operation == "register":
        assert json.loads(case.paths.state.read_text(encoding="utf-8")).get("diagnostic_sigusr1") is not True
    gp._restore_gateway_signal_handlers(previous)
    assert any(name == operation for name, _ in calls)


# LLM: 不注册诊断的旧版也会耗时采集；TERM必须在采集返回后再核原代，不能依赖采集前观测。
# 函数用途: 复现并锁住无公告路径的采集后身份变化拒止。
@pytest.mark.parametrize("observed", [False, None])
def test_incident_term_rechecks_identity_after_unregistered_diagnostics(incident_gateway, monkeypatch, observed):
    case = incident_gateway
    live = [True]
    monkeypatch.setattr(sv, "process_identity_is_live", lambda _: live[0])
    monkeypatch.setattr(sv, "_wait_gateway_exit", lambda *a: False)
    monkeypatch.setattr(sv, "_capture_unresponsive_gateway_diagnostics", lambda *a: live.__setitem__(0, observed))
    assert not sv.stop_gateway(case.supervisor)
    assert ("TERM", 12345) not in case.events and ("KILL", 12345) not in case.events


# LLM: 串联真实注册公告及终态写入，cleanup未完成时诊断handler仍在；OS及日志边界替身。
# 函数用途: 复现停止已受理但排空卡住时丢栈公告的问题。
@pytest.mark.parametrize("mode", ["normal", "interrupted", "failed"])
@pytest.mark.parametrize("same_generation", [True, False])
def test_incident_termination_retains_diagnostics_until_cleanup_finishes(incident_gateway, monkeypatch, mode, same_generation):
    from agent_py_agent.cli import gateway_process as gp

    case = incident_gateway
    monkeypatch.setattr(signal, "getsignal", lambda _: signal.SIG_DFL)
    monkeypatch.setattr(signal, "signal", lambda *a: None)
    monkeypatch.setattr(gp.faulthandler, "enable", lambda **k: None)
    monkeypatch.setattr(gp.faulthandler, "register", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(gp.faulthandler, "unregister", lambda *a: None, raising=False)
    monkeypatch.setattr(gp, "log_gateway_event", lambda *a: None)
    context = SimpleNamespace(process_identity=case.identity, process_started_at=1, control_plane=None)
    previous = gp._install_gateway_signal_handlers(case.paths, case.identity)
    try:
        if not same_generation:
            case.paths.state.write_text(json.dumps({"pid": 12345, "process_identity": {
                **case.identity, "start_time": "other"}, "diagnostic_sigusr1": True}), encoding="utf-8")
        if mode == "normal":
            termination = gp._GatewayServiceTermination("stopped", "stop_requested", "test", "test", 0)
            gp._record_gateway_service_termination(case.paths, object(), 12345, termination, context=context)
        elif mode == "interrupted":
            gp._record_gateway_run_interrupted(case.paths, object(), 12345, context=context)
        else:
            gp._record_gateway_run_failed(case.paths, object(), 12345, RuntimeError("test"), context=context)
        assert bool(json.loads(case.paths.state.read_text(encoding="utf-8")).get("diagnostic_sigusr1")) is same_generation
        sv._capture_unresponsive_gateway_diagnostics(case.supervisor, 12345, case.identity)
        assert ((signal.SIGUSR1, 12345) in case.events) is same_generation
    finally:
        gp._restore_gateway_signal_handlers(previous)


# LLM: 仅持续健康运行满600秒才重置重启预算，未满窗口保留已耗次数防震荡。
# 函数用途: 验证5/5恢复成功后稳定运行自动回收监护重启配额。
def test_incident_restart_budget_resets_after_stable_healthy_window(incident_gateway, monkeypatch):
    case = incident_gateway
    sup = case.supervisor
    sup._restart_count = 5
    sup._last_restart_at = 1000.0
    monkeypatch.setattr(sup, "_is_gateway_healthy", lambda **k: True)
    monkeypatch.setattr(sup, "_check_adapter_health", lambda: True)
    # 新合同须真实记录窗口首个健康观察，不能把未监测的重启后时间当作连续健康。
    monkeypatch.setattr(sv.time, "time", lambda: 1000.0)
    assert sv._run_health_check(sup, 0) == 0 and sup._restart_count == 5
    monkeypatch.setattr(sv.time, "time", lambda: 1599.0)
    assert sv._run_health_check(sup, 2) == 0
    assert sup._restart_count == 5
    monkeypatch.setattr(sv.time, "time", lambda: 1600.0)
    assert sv._run_health_check(sup, 0) == 0
    assert sup._restart_count == 0


# LLM: 原incident夹具的OS边界仍替身；以真实文件为计划交接证据，不启动Gateway或修改生产Home。
# 函数用途: 验证三处自动干预入口都让原计划重启完成交接，不消耗重启预算。
@pytest.mark.parametrize("source", ["request", "completed", "turn_wait", "tool_drain", "exiting", "stopped", "stopped_fallback", "starting"])
@pytest.mark.parametrize("entry", ["health", "restart", "initial"])
def test_batch4_planned_handover_suppresses_automatic_intervention(incident_gateway, monkeypatch, source, entry):
    from agent_py_agent.agent.gateway_parts import restart_service as restart

    case, now = incident_gateway, 2000.0
    sup = case.supervisor
    monkeypatch.setattr(sv.time, "time", lambda: now)
    if source == "request":
        path, payload = restart.restart_request_path(case.paths), {"status": "pending", "requested_at": now - 10}
    elif source == "completed":
        path, payload = restart.restart_completed_path(case.paths), {"drained_at": now - 10}
    else:
        path, payload = case.paths.state, {"restart_drain": {"phase": source, "updated_at": now - 10}}
        if source in {"stopped", "stopped_fallback"}:
            payload = {"status": "stopped", "termination_kind": "planned_restart"}
            if source == "stopped":
                payload["stopped_at"] = now - 10
        if source in {"starting", "stopped_fallback"}:
            payload.update({"status": "starting" if source == "starting" else "stopped", "started_at": now - 5})
            restart.restart_state_path(case.paths).write_text(json.dumps({"last_drained_at": now - 10}), encoding="utf-8")
    path.write_text(json.dumps(payload), encoding="utf-8")
    healthy, adapter = MagicMock(return_value=False), MagicMock(return_value=False)
    monkeypatch.setattr(sup, "_is_gateway_healthy", healthy)
    monkeypatch.setattr(sup, "_check_adapter_health", adapter)
    start, stop, recover = MagicMock(return_value=12345), MagicMock(return_value=True), MagicMock(return_value=True)
    monkeypatch.setattr(sup, "_start_gateway", start)
    monkeypatch.setattr(sup, "_stop_gateway", stop)
    monkeypatch.setattr(sup, "_restart_gateway", recover)
    if entry == "health":
        assert sv._run_health_check(sup, 2) == 0
        recover.assert_not_called()
        adapter.assert_not_called()
    elif entry == "restart":
        assert sv.restart_gateway(sup) is False
    else:
        for name in ("_prepare_supervisor_run", "_monitor_until_stopped", "_finish_supervisor_run"):
            monkeypatch.setattr(sv, name, MagicMock())
        assert sv.run_supervisor_loop(sup) == 0
        healthy.assert_not_called()
    start.assert_not_called()
    stop.assert_not_called()
    assert sup._restart_count == 0


# LLM: 原文件读口和健康检查真实运行；坏时间及未来记录不能抑制非计划故障，边界含180秒。
# 函数用途: 检查计划交接时间窗口与坏记录的安全退化。
@pytest.mark.parametrize("age,active", [(0, True), (180, True), (180.01, False), (-1, False),
    ("bad", False), ({"bad": 1}, False), (float("nan"), False), (float("inf"), False)])
def test_batch4_handover_window_is_bounded_and_bad_timestamps_do_not_crash(incident_gateway, monkeypatch, age, active):
    from agent_py_agent.agent.gateway_parts.restart_service import restart_request_path

    case, now = incident_gateway, 2000.0
    timestamp = now - age if isinstance(age, (int, float)) else age
    restart_request_path(case.paths).write_text(json.dumps({"status": "pending", "requested_at": timestamp}), encoding="utf-8")
    monkeypatch.setattr(sv.time, "time", lambda: now)
    monkeypatch.setattr(case.supervisor, "_is_gateway_healthy", lambda: False)
    monkeypatch.setattr(case.supervisor, "_check_adapter_health", lambda: True)
    assert sv._run_health_check(case.supervisor, 0) == (0 if active else 1)


# LLM: 假PID不发真实信号；early unlink后消费原Count0完整出生身份，None不能视为已退出。
# 函数用途: 验证PID文件先移除时仍等待旧进程，未知观察明确拒绝停止成功。
@pytest.mark.parametrize("live", [True, False, None])
def test_batch4_unlinked_pid_uses_three_valued_standalone_identity(incident_gateway, monkeypatch, live):
    case = incident_gateway
    case.paths.pid.unlink()
    monkeypatch.setattr(sv, "get_running_pid_report", lambda *a, **k: RunningPidReport(None))
    monkeypatch.setattr(sv, "process_identity_is_live", lambda identity: live)
    wait = MagicMock(return_value=True)
    monkeypatch.setattr(sv, "_wait_gateway_exit", wait)
    assert sv.stop_gateway(case.supervisor, timeout=9) is (live is not None)
    assert wait.call_count == (1 if live is True else 0)
    if live is True:
        assert wait.call_args.args == (case.supervisor, 12345, case.identity, 9)
    assert not [event for event in case.events if event[0] in {"TERM", "KILL"}]


# LLM: 同一原Count0身份在等待后必须重新核验，复用既有强停函数和退出确认；不走MP代际旁路。
# 函数用途: 验证残留旧进程的TERM/KILL顺序和未知/换代时零信号。
@pytest.mark.parametrize("after_wait", [True, False, None])
def test_batch4_unlinked_pid_rechecks_before_escalation(incident_gateway, monkeypatch, after_wait):
    case = incident_gateway
    case.paths.pid.unlink()
    monkeypatch.setattr(sv, "get_running_pid_report", lambda *a, **k: RunningPidReport(None))
    checks = iter([True, after_wait, after_wait])
    monkeypatch.setattr(sv, "process_identity_is_live", lambda identity: next(checks))
    wait = MagicMock(side_effect=[False, False, True])
    monkeypatch.setattr(sv, "_wait_gateway_exit", wait)
    assert sv.stop_gateway(case.supervisor, timeout=9) is (after_wait is True)
    signals = [event for event in case.events if event[0] in {"TERM", "KILL"}]
    assert signals == ([("TERM", 12345), ("KILL", 12345)] if after_wait is True else [])


# LLM: 无PID时不从正count、C标记或畸形出生资料恢复信号权；保持原expected_identity分支。
# 函数用途: 验证新残留身份读口不能误接MP或非法PID。
@pytest.mark.parametrize("damage", ["workers", "coordinator", "boolean_pid", "missing_host", "missing_birth", "expected"])
def test_batch4_unlinked_pid_rejects_non_standalone_or_invalid_identity(incident_gateway, monkeypatch, damage):
    case = incident_gateway
    case.paths.pid.unlink()
    state = {"pid": 12345, "process_identity": dict(case.identity), "worker_count": 0}
    if damage == "workers":
        state["worker_count"] = 2
    elif damage == "coordinator":
        state["c_run_id"] = ""
    elif damage == "boolean_pid":
        state["process_identity"]["pid"] = True
    elif damage == "missing_host":
        state["process_identity"].pop("host_id")
    elif damage == "missing_birth":
        state["process_identity"].pop("start_time")
    case.paths.state.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(sv, "get_running_pid_report", lambda *a, **k: RunningPidReport(None))
    live, wait = MagicMock(return_value=None), MagicMock(return_value=True)
    monkeypatch.setattr(sv, "process_identity_is_live", live)
    monkeypatch.setattr(sv, "_wait_gateway_exit", wait)
    options = {"expected_identity": case.identity} if damage == "expected" else {}
    assert sv.stop_gateway(case.supervisor, **options) is False
    wait.assert_not_called()
    assert live.call_count == (1 if damage == "expected" else 0)


# LLM: 假停止/启动边界保原代际捕获与预算；仅有真实上次尝试且过300秒才发慢探针。
# 函数用途: 验证预算耗尽后的节流恢复，以及max<=0/last=0原红线。
@pytest.mark.parametrize("limit,last,now,allowed", [(3, 1000, 1299, False), (3, 1000, 1300, True),
    (3, 0, 2000, False), (0, 1000, 2000, False), (-1, 1000, 2000, False)])
def test_batch4_slow_backoff_preserves_fast_budget_redlines(incident_gateway, monkeypatch, limit, last, now, allowed):
    case = incident_gateway
    sup = case.supervisor
    sup.max_restart_attempts, sup._restart_count, sup._last_restart_at = limit, max(0, limit), last
    monkeypatch.setattr(sv.time, "time", lambda: now)
    stop, start = MagicMock(return_value=True), MagicMock(return_value=23456)
    monkeypatch.setattr(sup, "_stop_gateway", stop)
    monkeypatch.setattr(sup, "_start_gateway", start)
    assert sv.restart_gateway(sup) is allowed
    assert stop.call_count == int(allowed) and start.call_count == int(allowed)
    assert sup._restart_count == max(0, limit) + int(allowed)
    if allowed:
        assert sup._last_restart_at == now
        assert sv.restart_gateway(sup) is False
        assert stop.call_count == 1


# LLM: 只改假墙钟和健康观察，不等待600秒；不健康必须中断连续窗口，旧稳定窗口回归仍保留。
# 函数用途: 复现“距上次重启已久但刚恢复就清预算”，并检查第二次不健康会重新计时。
def test_batch4_restart_budget_requires_continuous_healthy_observations(incident_gateway, monkeypatch):
    sup = incident_gateway.supervisor
    sup._restart_count, sup._last_restart_at = 5, 1000.0
    healthy, clock = [False], [1700.0]
    monkeypatch.setattr(sv.time, "time", lambda: clock[0])
    monkeypatch.setattr(sup, "_is_gateway_healthy", lambda: healthy[0])
    monkeypatch.setattr(sup, "_check_adapter_health", lambda: True)
    assert sv._run_health_check(sup, 0) == 1
    healthy[0], clock[0] = True, 1800.0
    assert sv._run_health_check(sup, 1) == 0
    assert sup._restart_count == 5, "刚恢复不能把距上次重启的时间误当作连续健康"
    healthy[0], clock[0] = False, 2300.0
    assert sv._run_health_check(sup, 0) == 1
    healthy[0], clock[0] = True, 2400.0
    assert sv._run_health_check(sup, 1) == 0
    clock[0] = 2999.0
    assert sv._run_health_check(sup, 0) == 0 and sup._restart_count == 5
    clock[0] = 3000.0
    assert sv._run_health_check(sup, 0) == 0 and sup._restart_count == 0
    assert sup._last_unhealthy_at == 0.0 and sup._healthy_since_at == 0.0


# LLM: 真实state文件及mtime，只替换身份观察；已过期的phase不能永久压住恢复，活身份仍保护正在排空的原进程。
# 函数用途: 覆盖新插话的排空阶段时限、ISO/mtime锚点和三值存活分支。
@pytest.mark.parametrize('phase', ['turn_wait', 'tool_drain', 'exiting'])
@pytest.mark.parametrize('source,live,active', [('fresh', False, True), ('stale', False, False),
    ('stale', None, False), ('stale', True, True), ('mtime', False, True), ('iso', False, True)])
def test_steer_drain_phase_is_fresh_or_original_process_is_live(incident_gateway, monkeypatch, phase, source, live, active):
    from datetime import datetime, timezone

    case, now = incident_gateway, 2000.0
    drain = {'phase': phase}
    if source in {'fresh', 'stale'}:
        drain['updated_at'] = now - (10 if source == 'fresh' else 1000)
    elif source == 'iso':
        drain['started_at'] = datetime.fromtimestamp(now - 10, timezone.utc).isoformat()
    case.paths.state.write_text(json.dumps({'pid': 12345, 'process_identity': case.identity,
        'worker_count': 0, 'restart_drain': drain}), encoding='utf-8')
    os.utime(case.paths.state, (now - 10 if source == 'mtime' else now - 1000,) * 2)
    monkeypatch.setattr(sv, 'process_identity_is_live', lambda identity: live)
    assert sv._planned_restart_handover_active(case.supervisor, now=now) is active


# LLM: 缺PID/state或坏JSON不是死亡证明；stopped且缺身份才可用原终态退化，完整活身份即使stopped也必须等待。
# 函数用途: 复现状态损坏被说成已停止，以及坏身份/CountN不能获得信号权。
@pytest.mark.parametrize('damage,stopped', [('missing_file', False), ('corrupt', False), ('missing_identity', False),
    ('missing_pid', False), ('mismatched_pid', False), ('boolean_pid', False), ('missing_birth', False),
    ('boolean_count', False), ('stopped_without_identity', True)])
def test_steer_unlinked_pid_preserves_unknown_state(incident_gateway, monkeypatch, damage, stopped):
    case = incident_gateway
    case.paths.pid.unlink()
    state = {'pid': 12345, 'worker_count': 0, 'status': 'running', 'process_identity': dict(case.identity)}
    if damage == 'missing_identity':
        state.pop('process_identity')
    elif damage == 'missing_pid':
        state.pop('pid')
    elif damage == 'mismatched_pid':
        state['pid'] = 67890
    elif damage == 'boolean_pid':
        state['pid'] = True
    elif damage == 'missing_birth':
        state['process_identity'].pop('start_time')
    elif damage == 'boolean_count':
        state['worker_count'] = False
    elif damage == 'stopped_without_identity':
        state = {'status': 'stopped'}
    case.paths.state.write_text('{' if damage == 'corrupt' else json.dumps(state), encoding='utf-8')
    if damage == 'missing_file':
        case.paths.state.unlink()
    monkeypatch.setattr(sv, 'get_running_pid_report', lambda *a, **k: RunningPidReport(None))
    live, wait = MagicMock(return_value=True), MagicMock(return_value=True)
    monkeypatch.setattr(sv, 'process_identity_is_live', live)
    monkeypatch.setattr(sv, '_wait_gateway_exit', wait)
    assert sv.stop_gateway(case.supervisor) is stopped
    live.assert_not_called()
    wait.assert_not_called()
    assert not [event for event in case.events if event[0] in {'TERM', 'KILL'}]


# LLM: 维护期没有健康采样，不得计入600秒；原维护读口和真实有限文件由产品处理，不直接调用预算reset helper。
# 函数用途: 验证维护暂停会截断窗口，恢复后首次健康重新从now起算。
def test_steer_maintenance_pause_restarts_healthy_window(incident_gateway, monkeypatch):
    sup, clock = incident_gateway.supervisor, [1000.0]
    sup._restart_count, sup._last_restart_at = 3, 1000.0
    monkeypatch.setattr(sv.time, 'time', lambda: clock[0])
    monkeypatch.setattr(sup, '_is_gateway_healthy', lambda: True)
    monkeypatch.setattr(sup, '_check_adapter_health', lambda: True)
    assert sv._run_health_check(sup, 0) == 0 and sup._restart_count == 3
    clock[0] = 1500.0
    sv.write_maintenance_lock(sup._paths, reason='synthetic maintenance', ttl_seconds=200)
    assert sv._run_health_check(sup, 2) == 0
    assert sup._healthy_since_at == 0.0
    clock[0] = 1800.0
    assert sv._run_health_check(sup, 0) == 0 and sup._restart_count == 3
    clock[0] = 2399.0
    assert sv._run_health_check(sup, 0) == 0 and sup._restart_count == 3
    clock[0] = 2400.0
    assert sv._run_health_check(sup, 0) == 0 and sup._restart_count == 0


# LLM: 旧健康起点不能越过更晚的实际重启或不健康锚点；max合同用真实health入口核验。
# 函数用途: 锁住最新重启后的600秒，以及首个健康采样不回填未监测的旧时间。
@pytest.mark.parametrize('old_since', [0.0, 500.0])
def test_steer_healthy_anchor_does_not_reuse_unmonitored_or_pre_restart_time(incident_gateway, monkeypatch, old_since):
    sup, clock = incident_gateway.supervisor, [2000.0]
    sup._restart_count, sup._last_restart_at, sup._healthy_since_at = 3, 1900.0, old_since
    monkeypatch.setattr(sv.time, 'time', lambda: clock[0])
    monkeypatch.setattr(sup, '_is_gateway_healthy', lambda: True)
    monkeypatch.setattr(sup, '_check_adapter_health', lambda: True)
    assert sv._run_health_check(sup, 0) == 0 and sup._restart_count == 3
    clock[0] = 2599.0 if old_since == 0.0 else 2499.0
    assert sv._run_health_check(sup, 0) == 0 and sup._restart_count == 3
    clock[0] += 1
    assert sv._run_health_check(sup, 0) == 0 and sup._restart_count == 0
