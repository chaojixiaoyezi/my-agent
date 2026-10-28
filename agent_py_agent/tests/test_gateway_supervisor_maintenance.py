"""维护锁、适配器恢复与服务安装；所有写入隔离在 tmp_path，不操作真实服务。"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.gateway_parts import supervisor as sv
from agent_py_agent.agent.gateway_parts.daemon_control import RunningPidReport
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.cli import supervisor as cli
from agent_py_agent.cli.subcommands_gateway import add_gateway_subcommands


# LLM: 入口不存在须明确断言失败，不把导入错误伪装成功；仅供本文件新接口测试。
# 函数用途: 取得待实现的公开函数。
def api(module, name):
    value = getattr(module, name, None)
    assert callable(value), f"missing API: {name}"
    return value


# LLM: 仅构造配置/路径合同；不初始化真实Agent或服务。
# 函数用途: 提供可隔离监护实例和受控进程读口。
@pytest.fixture
def case(tmp_path, monkeypatch):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    paths.root.mkdir()
    config = tmp_path / "config" / "agent.yaml"
    agent = SimpleNamespace(config=SimpleNamespace(config_path=str(config)), home_paths=SimpleNamespace(root=tmp_path))
    value = sv.GatewaySupervisor(str(config), agent=agent)
    value._paths = paths
    monkeypatch.setattr(sv, "get_running_pid_report", Mock(return_value=RunningPidReport(None)))
    return value


# LLM: 新路径只派生，不创建或授权文件；保留原构造字段。
# 函数用途: 验证维护锁和服务记录的只读地址。
def test_paths_are_readonly(tmp_path):
    paths = gateway_paths_from_root(tmp_path / "absent")
    assert getattr(paths, "maintenance_lock", None) == paths.root / "maintenance_lock.json"
    assert getattr(paths, "supervisor_service", None) == paths.root / "supervisor_service.json"
    with pytest.raises(AttributeError):
        paths.maintenance_lock = tmp_path
    assert not paths.root.exists()


# LLM: 真实原子JSON往返；时间替身只固定TTL，不操作真实维护状态。
# 函数用途: 核对锁字段、到期边界、零时间和幂等解锁。
def test_lock_roundtrip_expiry_clear(case, monkeypatch):
    monkeypatch.setattr(sv.time, "time", lambda: 100.0)
    lock = api(sv, "write_maintenance_lock")(case._paths, reason=123, actor="tester", ttl_seconds=5)
    assert lock == {"schema": "my-agent.maintenance-lock.v1", "reason": "123", "actor": "tester",
                    "created_at": 100.0, "expires_at": 105.0, "pid": os.getpid()}
    read = api(sv, "read_maintenance_lock")
    assert read(case._paths, now=0.0) == lock
    assert read(case._paths, now=104.99) == lock
    assert read(case._paths, now=105) is None
    assert json.loads(case._paths.maintenance_lock.read_text()) == lock
    clear = api(sv, "clear_maintenance_lock")
    assert clear(case._paths) is True
    assert clear(case._paths) is False
    assert read(case._paths) is None


# LLM: TTL至少一秒；非有限数不能变成永久维护开关。
# 函数用途: 核对负TTL收敛和非法TTL拒绝。
def test_lock_ttl_validation(case, monkeypatch):
    monkeypatch.setattr(sv.time, "time", lambda: 100.0)
    write = api(sv, "write_maintenance_lock")
    assert write(case._paths, reason="test", ttl_seconds=-5)["expires_at"] == 101
    for ttl in (float("inf"), float("nan")):
        with pytest.raises(ValueError):
            write(case._paths, reason="test", ttl_seconds=ttl)


# LLM: 坏锁只读且视为无有效锁；不将NaN、布尔或字符串有效期当数字。
# 函数用途: 覆盖损坏、缺字段和非对象记录。
@pytest.mark.parametrize("raw", ["{bad", "[]", "null", "{}", '{"expires_at":true}',
                                 '{"expires_at":"99999"}', '{"expires_at":NaN}', '{"expires_at":Infinity}',
                                 json.dumps({"expires_at": 10**400})])
def test_bad_locks_are_inactive(case, raw):
    path = case._paths.root / "maintenance_lock.json"
    path.write_text(raw)
    assert api(sv, "read_maintenance_lock")(case._paths, now=1) is None
    assert path.read_text() == raw


# LLM: 驱动真实初启/健康循环，只替代进程和等待边界，不启动Gateway。
# 函数用途: 验证锁阻止初启与两类恢复，解除后恢复原三次失败策略。
def test_loop_pauses_and_resumes(case, monkeypatch):
    (case._paths.root / "maintenance_lock.json").write_text(json.dumps({"expires_at": 10**12}))
    start, restart, adapter = Mock(), Mock(), Mock()
    monkeypatch.setattr(case, "_is_gateway_healthy", lambda: False)
    monkeypatch.setattr(case, "_check_adapter_health", lambda: False)
    monkeypatch.setattr(case, "_start_gateway", start)
    monkeypatch.setattr(case, "_restart_gateway", restart)
    monkeypatch.setattr(case, "_restart_adapter", adapter, raising=False)
    monkeypatch.setattr(sv, "_prepare_supervisor_run", Mock())
    monkeypatch.setattr(sv, "_finish_supervisor_run", Mock())
    monitor = Mock()
    monkeypatch.setattr(sv, "_monitor_until_stopped", monitor)
    assert sv.run_supervisor_loop(case) == 0
    assert start.call_count == 0
    assert sv._run_health_check(case, 2) == 0
    restart.assert_not_called()
    adapter.assert_not_called()
    api(sv, "clear_maintenance_lock")(case._paths)
    assert sv._run_health_check(case, 2) == 0
    restart.assert_called_once()
    sv.run_supervisor_loop(case)
    start.assert_called_once()


# LLM: 真实恢复方法必须守锁，不能仅依赖主循环先验检查。
# 函数用途: 直接调用Gateway/Adapter恢复仍不得绕过维护。
def test_direct_restarts_obey_lock(case, monkeypatch):
    api(sv, "write_maintenance_lock")(case._paths, reason="update")
    stop = Mock()
    monkeypatch.setattr(case, "_stop_gateway", stop)
    assert case._restart_gateway() is False
    assert api(case, "_restart_adapter")() is False
    assert case._start_gateway() is None
    stop.assert_not_called()


# LLM: 自动恢复只在Gateway健康且适配器原running意图存在时触发。
# 函数用途: 区分从未启动、用户停止和崩溃。
@pytest.mark.parametrize("state,healthy,expected", [(None, True, 0), ("stopped", True, 0),
                                                   ("starting", True, 0), ("running", False, 0), ("running", True, 1)])
def test_health_only_heals_intended_running_adapter(case, monkeypatch, state, healthy, expected):
    if state:
        (case._paths.root / "adapter_state.json").write_text(json.dumps({"state": state}))
    monkeypatch.setattr(case, "_is_gateway_healthy", lambda: healthy)
    monkeypatch.setattr(case, "_check_adapter_health", lambda: False)
    restart = Mock()
    monkeypatch.setattr(case, "_restart_adapter", restart, raising=False)
    sv._run_health_check(case, 0)
    assert restart.call_count == expected


# LLM: 只替代Popen系统边界，真实组装参数/env/日志和冷却逻辑运行。
# 函数用途: 覆盖通道优先级、退回all、30秒边界及进程启动故障。
@pytest.mark.parametrize("extra,channel", [({"requested_channel": "qq", "channels": ["feishu"]}, "qq"),
    ({"requested_channel": "all"}, "all"), ({"channels": ["feishu"]}, "feishu"),
    ({"channels": ["qq"]}, "qq"), ({"channels": ["feishu", "qq"]}, "all"),
    ({"requested_channel": "bad", "channels": ["unknown"]}, "all")])
def test_adapter_spawn_environment_cooldown(case, monkeypatch, extra, channel):
    (case._paths.root / "adapter_state.json").write_text(json.dumps({"state": "running", **extra}))
    clock = [100.0]
    monkeypatch.setattr(sv.time, "monotonic", lambda: clock[0])
    spawn = Mock()
    monkeypatch.setattr(sv.subprocess, "Popen", spawn)
    restart = api(case, "_restart_adapter")
    assert restart() is True
    args, kwargs = spawn.call_args
    assert args[0] == [sys.executable, "-m", "agent_py_agent", "--config", case.config_path,
                       "adapter", "start", "--daemon", "--channel", channel]
    assert kwargs["env"]["MY_AGENT_HOME"] == str(case._agent.home_paths.root.resolve())
    assert kwargs["env"]["MY_AGENT_CONFIG"] == case.config_path
    clock[0] = 129.99
    assert restart() is False
    assert spawn.call_count == 1
    clock[0] = 130.0
    spawn.side_effect = OSError("cannot spawn")
    assert restart() is False
    assert restart() is False
    assert spawn.call_count == 2


# LLM: 未知PID身份不是死亡证明，活PID或坏记录均不得重复启动。
# 函数用途: 验证恢复入口保守处理PID和用户停用记录。
@pytest.mark.parametrize("pid,error,state", [(123, None, "running"), (None, {"error": "bad"}, "running"),
                                            (None, None, "stopped"), (None, None, None)])
def test_adapter_restart_refuses_live_unknown_or_stopped(case, monkeypatch, pid, error, state):
    if state:
        (case._paths.root / "adapter_state.json").write_text(json.dumps({"state": state}))
    monkeypatch.setattr(sv, "get_running_pid_report", lambda *a, **kw: RunningPidReport(pid, error))
    spawn = Mock()
    monkeypatch.setattr(sv.subprocess, "Popen", spawn)
    assert api(case, "_restart_adapter")() is False
    spawn.assert_not_called()


# LLM: 稳定启动器只能来自真实安装标记和bin文件，不凭releases目录名猜测。
# 函数用途: 验证config或home任一定位安装根，缺文件退回当前解释器。
def test_stable_command_prefers_private_prefix(tmp_path):
    prefix = tmp_path / "private install"
    launcher = prefix / "bin/my-agent"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n")
    marker = prefix / ".my-agent-private-install.json"
    marker.write_text("{}")
    config = prefix / "config/agent.yaml"
    resolve = api(cli, "resolve_stable_supervisor_command")
    assert resolve(config) == [str(launcher), "--config", str(config), "gateway", "supervisor"]
    external = tmp_path / "external.yaml"
    assert resolve(external, prefix / "data")[:1] == [str(launcher)]
    launcher.unlink()
    assert resolve(config)[:3] == [sys.executable, "-m", "agent_py_agent"]


# LLM: 安装写入仅临时HOME；systemctl被系统边界替身捕获，不接用户总线。
# 函数用途: 验证unit内容、结构化记录及按需启动/卸载。
@pytest.mark.parametrize("start", [False, True])
def test_service_install_uninstall(case, monkeypatch, tmp_path, start):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(cli, "shutil", SimpleNamespace(which=lambda name: "/bin/systemctl"), raising=False)
    run = Mock(return_value=SimpleNamespace(returncode=0, stderr="", stdout=""))
    monkeypatch.setattr(cli.subprocess, "run", run)
    result = api(cli, "install_supervisor_systemd_service")(
        case._paths, config_path=case.config_path, home_root=tmp_path, start_now=start)
    assert isinstance(result, dict)
    unit = tmp_path / ".config/systemd/user/my-agent-gw-supervisor.service"
    text = unit.read_text()
    assert "Restart=always" in text and "RestartSec=15s" in text
    assert f'WorkingDirectory={tmp_path}\n' in text
    assert "ExecStart=" in text and "gateway" in text and "supervisor" in text
    record = json.loads(case._paths.supervisor_service.read_text())
    assert record["schema"] == "my-agent.supervisor-service.v1"
    assert record["command"] == api(cli, "resolve_stable_supervisor_command")(case.config_path, tmp_path)
    assert record["unit_path"] == str(unit)
    assert run.call_count == (2 if start else 0)
    api(cli, "uninstall_supervisor_systemd_service")(case._paths)
    assert not unit.exists() and not case._paths.supervisor_service.exists()
    assert any("disable" in args[0] and "--now" in args[0] for args, kwargs in run.call_args_list)


# LLM: 非法unit不得写文件或把选项传给systemctl。
# 函数用途: 防止路径穿越、换行和命令选项注入。
@pytest.mark.parametrize("unit", ["../x.service", "x\\y.service", "x", "x.service\nBad=yes", "-x.service"])
def test_invalid_unit_rejected(case, tmp_path, monkeypatch, unit):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with pytest.raises(ValueError):
        api(cli, "install_supervisor_systemd_service")(case._paths, config_path=case.config_path,
                                                     home_root=tmp_path, unit_name=unit)
    with pytest.raises(ValueError):
        api(cli, "uninstall_supervisor_systemd_service")(case._paths, unit_name=unit)
    assert not (tmp_path / ".config").exists()


# LLM: 系统总线缺失/命令超时必须返回错误，不崩溃或伪称已启用。
# 函数用途: 覆盖systemctl失败回执。
@pytest.mark.parametrize("failure", [OSError("missing bus"), subprocess.TimeoutExpired("systemctl", 15)])
def test_systemctl_error_captured(case, tmp_path, monkeypatch, failure):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(cli, "shutil", SimpleNamespace(which=lambda name: "/bin/systemctl"), raising=False)
    monkeypatch.setattr(cli.subprocess, "run", Mock(side_effect=failure))
    result = api(cli, "install_supervisor_systemd_service")(case._paths, config_path=case.config_path, home_root=tmp_path)
    assert result["errors"]
    assert result["started"] is False


# LLM: 通过正式解析器注册的handler执行真实隔离文件操作，不只测函数存在。
# 函数用途: 验证四个新CLI入口及参数运输。
def test_cli_commands_roundtrip(case, monkeypatch, tmp_path):
    parser = argparse.ArgumentParser()
    add_gateway_subcommands(parser.add_subparsers())
    monkeypatch.setattr(cli, "make_agent", lambda args: case._agent)
    monkeypatch.setattr(cli, "gateway_paths", lambda agent: case._paths)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cli.subprocess, "run", Mock(return_value=SimpleNamespace(returncode=0, stderr="", stdout="")))
    commands = [["maintenance-lock", "--reason", "upgrade", "--ttl", "30"], ["maintenance-unlock"],
                ["supervisor-install-service", "--unit", "test.service", "--no-start"],
                ["supervisor-uninstall-service", "--unit", "test.service"]]
    for command in commands:
        args = parser.parse_args(["gateway", *command])
        assert args.func(args) == 0
    assert not case._paths.maintenance_lock.exists()
    assert not case._paths.supervisor_service.exists()


# LLM: 真进程已wait退出后读取原PID合同；仅替代恢复Popen边界，避免启动真实适配器。
# 函数用途: 用实际死亡PID验证健康检查会自动拉起适配器。
def test_real_dead_adapter_pid_triggers_heal(case, monkeypatch):
    from agent_py_agent.agent.gateway_parts.daemon_control import get_running_pid_report

    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=10)
    case._paths.adapter_pid.write_text(json.dumps({"pid": dead.pid}))
    (case._paths.root / "adapter_state.json").write_text(json.dumps({"state": "running", "requested_channel": "qq"}))
    monkeypatch.setattr(sv, "get_running_pid_report", get_running_pid_report)
    monkeypatch.setattr(case, "_is_gateway_healthy", lambda: True)
    spawn = Mock()
    monkeypatch.setattr(sv.subprocess, "Popen", spawn)
    assert sv._run_health_check(case, 2) == 0
    spawn.assert_called_once()
    assert spawn.call_args.args[0][-1] == "qq"


# LLM: 非零systemctl回执与字面参数编码均需保留；写文件不等于服务已启用。
# 函数用途: 核对总线拒绝回执和systemd控制字符保护。
def test_service_nonzero_and_escaping(case, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/bin/systemctl")
    monkeypatch.setattr(cli.subprocess, "run", Mock(return_value=SimpleNamespace(returncode=1, stderr="No bus", stdout="")))
    result = cli.install_supervisor_systemd_service(case._paths, config_path=tmp_path / 'a $X%q".yaml', home_root=tmp_path)
    assert result["started"] is False and "No bus" in result["errors"][0]
    text = Path(result["unit_path"]).read_text()
    assert '$X%%q\\".yaml"' in text
    assert "ExecStart=:" in text
    with pytest.raises(ValueError):
        cli.install_supervisor_systemd_service(case._paths, config_path="bad\nfile", home_root=tmp_path)


# LLM: 依据systemd原解析合同，WorkingDirectory不去引号，ExecStart用冒号禁用变量展开。
# 函数用途: 防止普通目录引号和含美元符号的可执行文件路径失真。
def test_unit_directory_and_dollar_launcher_are_literal(case, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    prefix = tmp_path / 'cash${BOX}%box with space'
    launcher = prefix / 'bin/my-agent'
    launcher.parent.mkdir(parents=True)
    launcher.write_text('#!/bin/sh\n')
    (prefix / '.my-agent-private-install.json').write_text('{}')
    result = cli.install_supervisor_systemd_service(case._paths, config_path=prefix / 'config/agent.yaml',
                                                  home_root=prefix, start_now=False)
    text = Path(result['unit_path']).read_text()
    assert f'WorkingDirectory={str(prefix).replace("%", "%%")}\n' in text
    executable = str(launcher).replace('%', '%%')
    assert f'ExecStart=:"{executable}" ' in text
    assert '$$' not in text


# LLM: 墙钟跳变不应跳过30秒冷却；不改变维护锁用墙钟到期的合同。
# 函数用途: 在系统时间前跳/后跳时仍按真实单调间隔恢复。
def test_adapter_cooldown_ignores_wall_clock_jumps(case, monkeypatch):
    (case._paths.root / 'adapter_state.json').write_text('{"state":"running"}')
    wall, clock = [1000.0], [100.0]
    monkeypatch.setattr(sv.time, 'time', lambda: wall[0])
    monkeypatch.setattr(sv.time, 'monotonic', lambda: clock[0])
    spawn = Mock()
    monkeypatch.setattr(sv.subprocess, 'Popen', spawn)
    assert case._restart_adapter() is True
    clock[0], wall[0] = 110.0, 100000.0
    assert case._restart_adapter() is False
    clock[0], wall[0] = 130.0, 1.0
    assert case._restart_adapter() is True
    assert spawn.call_count == 2
