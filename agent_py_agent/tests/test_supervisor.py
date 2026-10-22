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


# LLM: 真文件及原Supervisor执行链，所有信号/等待均替身；不操作真实Gateway或模型。
# 函数用途: 构造绑定原代身份的停止与启动测试环境。
@pytest.fixture
def incident_gateway(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

    paths = gateway_paths_from_root(tmp_path)
    identity = {"host_id": "test-host", "pid": 12345, "start_time": "original"}
    paths.pid.write_text(json.dumps({**identity, "kind": "my-agent-gateway"}))
    paths.stop_request.write_text(json.dumps({"target_process": identity}))
    paths.state.write_text(json.dumps({"pid": 12345, "process_identity": identity}))
    agent = SimpleNamespace(config=SimpleNamespace(config_path="test.yaml", gateway_worker_process_count=0))
    supervisor = sv.GatewaySupervisor("test.yaml", agent=agent)
    supervisor._paths = paths
    events = []
    monkeypatch.setattr(sv, "get_running_pid_report", lambda *a, **k: RunningPidReport(12345))
    monkeypatch.setattr(sv, "process_identity_is_live", lambda value: value == identity)
    monkeypatch.setattr(sv, "_write_supervisor_stop_request", lambda *a: {"target_process": identity})
    monkeypatch.setattr(sv, "terminate_pid", lambda pid: events.append(("TERM", pid)))
    monkeypatch.setattr(sv, "kill_pid", lambda pid: events.append(("KILL", pid)), raising=False)
    monkeypatch.setattr(sv.os, "kill", lambda pid, sig: events.append((sig, pid)))
    monkeypatch.setattr(supervisor, "_log_warn", lambda text: events.append(("WARN", text)))
    return SimpleNamespace(supervisor=supervisor, paths=paths, identity=identity, events=events)


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
            case.paths.pid.write_text(json.dumps({**replacement, "kind": "my-agent-gateway"}))
            case.paths.stop_request.write_text(json.dumps({"target_process": replacement}))
        return result

    monkeypatch.setattr(sv, "_wait_gateway_exit", wait)
    assert sv.stop_gateway(case.supervisor)
    assert json.loads(case.paths.pid.read_text())["start_time"] == "successor"
    assert json.loads(case.paths.stop_request.read_text())["target_process"] == replacement


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
    monkeypatch.setattr(faulthandler, "register", lambda sig, **k: events.append(("register", sig, k)))
    monkeypatch.setattr(faulthandler, "unregister", lambda sig: events.append(("unregister", sig)))
    previous = gp._install_gateway_signal_handlers(case.paths, case.identity)
    assert ("enable", {"all_threads": True}) in events
    if hasattr(signal, "SIGUSR1"):
        assert any(e[0] == "register" and e[1] == signal.SIGUSR1 and e[2] == {
            "file": sys.stderr, "all_threads": True, "chain": False} for e in events)
        assert json.loads(case.paths.state.read_text())["diagnostic_sigusr1"] is True
    gp._restore_gateway_signal_handlers(previous)
    if hasattr(signal, "SIGUSR1"):
        assert ("unregister", signal.SIGUSR1) in events
        assert json.loads(case.paths.state.read_text())["diagnostic_sigusr1"] is False


# LLM: AST覆盖本文件所有state写点，避免仅修running但终态仍截断；不把语法存在当业务通过。
# 函数用途: 锁住Gateway状态发布必须走原子写入口的合同。
def test_incident_gateway_state_never_uses_truncating_writer():
    import ast

    from agent_py_agent.cli import gateway_process as gp

    tree = ast.parse(Path(gp.__file__).read_text())
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
    (proc / "status").write_text("State:\tS\nVmRSS:\t1234 kB\nVmSwap:\t0 kB\nThreads:\t20\n")
    (proc / "wchan").write_text("futex_wait")
    for tid in range(20):
        task = proc / "task" / str(tid)
        task.mkdir(parents=True)
        (task / "wchan").write_text("futex_wait" * 100)
    real_path = Path
    monkeypatch.setattr(sv, "Path", lambda name: proc if str(name) == "/proc/12345" else real_path(name))
    monkeypatch.setattr(sv.sys, "platform", "linux")
    case.paths.state.write_text(json.dumps({"pid": 12345, "process_identity": case.identity, "diagnostic_sigusr1": True}))
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
    case.paths.state.write_text(json.dumps({"pid": 12345, "process_identity": case.identity, "diagnostic_sigusr1": True}))
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
    case.paths.state.write_text(json.dumps(state))
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
    monkeypatch.setattr(faulthandler, "register", lambda *a, **k: maybe_fail("register", *a, **k))
    monkeypatch.setattr(faulthandler, "unregister", lambda *a: maybe_fail("unregister", *a))
    previous = gp._install_gateway_signal_handlers(case.paths, case.identity)
    assert signal.SIGTERM in previous and signal.SIGINT in previous
    if operation == "register":
        assert json.loads(case.paths.state.read_text()).get("diagnostic_sigusr1") is not True
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
    monkeypatch.setattr(gp.faulthandler, "register", lambda *a, **k: None)
    monkeypatch.setattr(gp.faulthandler, "unregister", lambda *a: None)
    monkeypatch.setattr(gp, "log_gateway_event", lambda *a: None)
    context = SimpleNamespace(process_identity=case.identity, process_started_at=1, control_plane=None)
    previous = gp._install_gateway_signal_handlers(case.paths, case.identity)
    try:
        if not same_generation:
            case.paths.state.write_text(json.dumps({"pid": 12345, "process_identity": {
                **case.identity, "start_time": "other"}, "diagnostic_sigusr1": True}))
        if mode == "normal":
            termination = gp._GatewayServiceTermination("stopped", "stop_requested", "test", "test", 0)
            gp._record_gateway_service_termination(case.paths, object(), 12345, termination, context=context)
        elif mode == "interrupted":
            gp._record_gateway_run_interrupted(case.paths, object(), 12345, context=context)
        else:
            gp._record_gateway_run_failed(case.paths, object(), 12345, RuntimeError("test"), context=context)
        assert bool(json.loads(case.paths.state.read_text()).get("diagnostic_sigusr1")) is same_generation
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
    monkeypatch.setattr(sv.time, "time", lambda: 1599.0)
    assert sv._run_health_check(sup, 2) == 0
    assert sup._restart_count == 5
    monkeypatch.setattr(sv.time, "time", lambda: 1600.0)
    assert sv._run_health_check(sup, 0) == 0
    assert sup._restart_count == 0
