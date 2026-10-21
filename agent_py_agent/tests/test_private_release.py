# LLM: 安装测试只在临时prefix和合成proc运行，服务命令与信号全部可控，不更新本机真实安装。
# 模块用途: 验证离线安装数据保留、更新服务维护、详细诊断和遗留进程清理边界。
from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import py_compile
import signal
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.private_release import build, configure, installer


def _bundle(root: Path, version="test.1") -> Path:
    package = root / "runtime/lib/python3.11/site-packages/agent_py_agent/config"
    package.mkdir(parents=True)
    (package / "agent_config.yaml").write_text('workspace_root: ""\ngateway_port: 8420\n', encoding="utf-8")
    files = {str(path.relative_to(root)): {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
             for path in root.rglob("*") if path.is_file()}
    (root / "release.json").write_text(json.dumps({"schema": installer.SCHEMA, "architecture": os.uname().machine, "version": version, "files": files}))
    return root


def test_install_and_upgrade_keep_explicit_data_and_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "active_processes", lambda _: [])
    prefix = tmp_path / "app" / "custom install"
    installer.install(_bundle(tmp_path / "first"), prefix)
    config = prefix / "data/config/agent_config.yaml"
    assert 'gateway_port: 18420' in config.read_text()
    assert 'workspace_root: ""' in config.read_text()
    from agent_py_agent.agent.settings import load_config
    from agent_py_agent.cli.workspace_resolution import (
        resolve_workspace_roots,
        validate_requested_workspace_roots,
    )

    monkeypatch.setenv("MY_AGENT_HOME", str(prefix / "data"))
    loaded = load_config(config)
    roots = resolve_workspace_roots(loaded, config)
    validate_requested_workspace_roots(loaded, roots)
    assert roots == [installer.workspace_path(prefix)]
    config.write_text("private configuration\n", encoding="utf-8")
    history = prefix / "data/history.keep"
    history.write_bytes(b"persistent\x00history")
    work = installer.workspace_path(prefix) / "keep.txt"
    work.write_text("user work")
    installer.install(_bundle(tmp_path / "second", "test.2"), prefix)
    assert (prefix / "current").resolve() == prefix / "releases/test.2"
    assert config.read_text() == "private configuration\n"
    assert history.read_bytes() == b"persistent\x00history"
    assert work.read_text() == "user work"
    launcher = (prefix / "bin/my-agent-preview").read_text()
    assert "$HOME" not in launcher
    assert "-X utf8" in launcher
    assert " -I -B " in launcher


def test_bad_payload_does_not_switch_installed_release(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "active_processes", lambda _: [])
    prefix = tmp_path / "install"
    installer.install(_bundle(tmp_path / "first"), prefix)
    damaged = _bundle(tmp_path / "second", "test.2")
    (damaged / "runtime/lib/python3.11/site-packages/agent_py_agent/config/agent_config.yaml").write_text("corrupted")
    with pytest.raises(ValueError, match="校验失败"):
        installer.install(damaged, prefix)
    assert (prefix / "current").resolve().name == "test.1"


def test_busy_installation_is_not_stopped_or_switched(tmp_path, monkeypatch):
    prefix = tmp_path / "install"
    monkeypatch.setattr(installer, "active_processes", lambda _: [])
    installer.install(_bundle(tmp_path / "first"), prefix)
    monkeypatch.setattr(installer, "active_processes", lambda _: [12345])
    with pytest.raises(ValueError, match="12345"):
        installer.install(_bundle(tmp_path / "second", "test.2"), prefix)
    assert (prefix / "current").resolve().name == "test.1"


def test_unmanaged_or_extra_payload_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "active_processes", lambda _: [])
    bundle = _bundle(tmp_path / "bundle")
    prefix = tmp_path / "existing"
    prefix.mkdir()
    (prefix / "keep").write_text("private")
    with pytest.raises(ValueError, match="非空"):
        installer.install(bundle, prefix)
    (bundle / "unexpected.py").write_text("bad")
    with pytest.raises(ValueError, match="额外文件"):
        installer.verify(bundle)
    assert (prefix / "keep").read_text() == "private"


def test_model_configuration_preserves_key_privacy_and_real_field(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.yaml"
    config.write_text('model_backend: ""\nmodel_name: ""\napi_base: ""\napi_key: ""\n')
    replies = iter(["https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen3.7-max"])
    monkeypatch.setattr("builtins.input", lambda _: next(replies))
    monkeypatch.setattr(configure.getpass, "getpass", lambda _: "test-placeholder-private")
    monkeypatch.setattr(sys, "argv", ["configure", str(config)])
    assert configure.main() == 0
    text = config.read_text()
    assert 'model_name: qwen3.7-max' in text or 'model_name: "qwen3.7-max"' in text
    assert "openai_compatible" in text
    assert "test-placeholder-private" not in capsys.readouterr().out
    assert config.stat().st_mode & 0o777 == 0o600


def test_sourceless_standalone_launcher_executes_utf8(tmp_path):
    script = tmp_path / "launch.py"
    script.write_text("print('中文测试')\n", encoding="utf-8")
    target = script.with_suffix(".pyc")
    py_compile.compile(str(script), cfile=str(target), doraise=True)
    script.unlink()
    result = subprocess.run([sys.executable, "-I", "-X", "utf8", str(target)], capture_output=True, check=True)
    assert result.stdout.decode("utf-8") == "中文测试\n"


def test_parameter_readers_snapshot_matches_source(tmp_path, monkeypatch):
    from agent_py_agent.agent.settings import parameter_metadata as metadata

    metadata.field_readers.cache_clear()
    original = metadata.field_readers()
    (tmp_path / "config").mkdir()
    (tmp_path / "config/parameter_readers.json").write_text(json.dumps({"schema": "my-agent.parameter-readers.v1", "readers": original}))
    monkeypatch.setattr(metadata, "_PACKAGE", tmp_path)
    metadata.field_readers.cache_clear()
    try:
        assert metadata.field_readers() == original
    finally:
        metadata.field_readers.cache_clear()


def test_relocated_console_script_runs_after_directory_move(tmp_path):
    runtime = tmp_path / "build-runtime"
    (runtime / "bin").mkdir(parents=True)
    (runtime / "bin/python3").symlink_to(sys.executable)
    script = runtime / "bin/example"
    script.write_text("#!/unavailable/build/python3\nprint('中文 relocatable')\n", encoding="utf-8")
    script.chmod(0o755)
    build.relocate_console_scripts(runtime)
    moved = tmp_path / "final runtime"
    runtime.rename(moved)
    result = subprocess.run([str(moved / "bin/example")], capture_output=True, check=True)
    assert result.stdout.decode("utf-8") == "中文 relocatable\n"


def test_auto_update_uses_original_marker_and_rejects_typo(tmp_path, monkeypatch):
    monkeypatch.setattr(installer,'active_processes',lambda _:[])
    prefix=tmp_path/'app'/'.my-agent'
    installer.install(_bundle(tmp_path/'bundle'),prefix)
    assert installer.resolve_install_prefix(None,roots=[tmp_path/'app'])==prefix
    with pytest.raises(ValueError,match='第二个 Gateway'):
        installer.resolve_install_prefix(tmp_path/'app'/'my-agent',roots=[tmp_path/'app'])
    assert not (tmp_path/'app'/'my-agent').exists()


def test_multiple_installations_require_explicit_selection(tmp_path, monkeypatch):
    monkeypatch.setattr(installer,'active_processes',lambda _:[])
    bundle=_bundle(tmp_path/'bundle')
    root=tmp_path/'app'
    for name in ('one','two'):
        installer.install(bundle,root/name)
    with pytest.raises(ValueError,match='多个安装'):
        installer.resolve_install_prefix(None,roots=[root])
    assert installer.resolve_install_prefix(root/'two',roots=[root])==root/'two'


def test_explicit_registered_nested_prefix_updates_without_environment_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, 'active_processes', lambda _: [])
    shallow = tmp_path / 'app' / '.my-agent'
    nested = tmp_path / 'custom' / 'deep' / 'install'
    bundle = _bundle(tmp_path / 'bundle')
    installer.install(bundle, shallow)
    installer.install(bundle, nested)
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'unrelated-home'))
    assert installer.resolve_install_prefix(nested, roots=[tmp_path / 'app']) == nested
    with pytest.raises(ValueError, match='第二个 Gateway'):
        installer.resolve_install_prefix(tmp_path / 'custom' / 'typo', roots=[tmp_path / 'app'])


@pytest.mark.parametrize("workspace", ["", "custom/gateway", "/tmp/explicit-update-service"])
@pytest.mark.parametrize("protocol", [None, "drain.v1"])
def test_update_services_uses_canonical_prefix_paths_and_restores_old_channel(tmp_path, monkeypatch, workspace, protocol):
    from agent_py_agent.agent.gateway_parts.daemon_control import write_pid_record

    prefix = tmp_path / "install"
    monkeypatch.setattr(installer, "active_processes", lambda _: [])
    installer.install(_bundle(tmp_path / "bundle"), prefix)
    config = prefix / "data/config/agent_config.yaml"
    config.write_text('my_agent_home: ""\ngateway_workspace: ' + json.dumps(workspace) + '\n')
    owner = installer.workspace_path(prefix)
    service = Path(workspace) if Path(workspace).is_absolute() else owner / "workspace" / (workspace or "runtime/services/gateway")
    # 绝对覆盖使用 tmp_path，不能让测试往真正 /tmp 公共服务路径写 PID。
    if workspace.startswith("/"):
        service = tmp_path / "absolute-service"
        config.write_text('my_agent_home: ""\ngateway_workspace: ' + json.dumps(str(service)) + '\n')
    service.mkdir(parents=True)
    write_pid_record(service / "gateway.pid")
    write_pid_record(service / "adapter.pid")
    (service / "adapter_state.json").write_text(json.dumps({"channels": [{"name": "feishu"}]}))
    manifest = prefix / "current/release.json"
    release = json.loads(manifest.read_text())
    if protocol:
        release["protocols"] = {"offline_update": protocol}
    manifest.write_text(json.dumps(release))
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "other-home"))
    commands = []
    original_run = subprocess.run

    def run(command, **kwargs):
        if command[0] == str(prefix / "bin/my-agent-preview"):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0)
        return original_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    original = installer.update_services(prefix)
    assert original == (True, True, "feishu")
    assert [row[1:3] for row in commands] == [["adapter", "stop"], ["gateway", "stop"]]
    assert ("--drain" in commands[-1]) == bool(protocol)
    installer.update_services(prefix, restore=original)
    assert commands[-2][1:3] == ["gateway", "start"]
    assert commands[-1][1:] == ["adapter", "start", "--daemon", "--channel", "feishu"]


@pytest.mark.parametrize("state,expected", [
    ({"channels": [{"name": "feishu"}]}, "feishu"),
    ({"channels": [{"name": "qq"}]}, "qq"),
    ({"channels": [{"name": "feishu"}, {"name": "qq"}]}, "all"),
    ({"requested_channel": "all", "channels": [{"name": "feishu"}]}, "all"),
    ({"channels": []}, None),
    ({"channels": [{"name": "unknown"}]}, None),
    ({"channels": [{"name": "feishu"}, {"name": "feishu"}]}, None),
    ({"requested_channel": "qq", "channels": [{"name": "feishu"}]}, None),
    ({"requested_channel": "invalid", "channels": [{"name": "feishu"}]}, None),
    ({"channels": [{"name": []}]}, None),
    ({"requested_channel": [], "channels": [{"name": "feishu"}]}, None),
])
def test_update_channel_uses_structured_old_and_current_state(state, expected):
    if expected is None:
        with pytest.raises(ValueError, match="通道记录"):
            installer.restore_channel(state)
    else:
        assert installer.restore_channel(state) == expected


# LLM: proc目录是普通临时文件；exe仅指向测试文件，不执行二进制，stat保留ppid/tty/starttime位置。
# 函数用途: 建立一条可复查的Linux进程观察样本。
def _proc_entry(root, pid, executable, *, ppid=1, tty=0, argv=()):
    entry = root / str(pid)
    entry.mkdir()
    (entry / 'exe').symlink_to(executable)
    fields = ['S', str(ppid), '0', '0', str(tty), *(['0'] * 14), str(pid)]
    (entry / 'stat').write_text(f'{pid} (synthetic process) ' + ' '.join(fields))
    (entry / 'cmdline').write_bytes('\0'.join(argv).encode() + b'\0')
    return entry


# LLM: 只重定位安装器自己的/proc读口；标准Path和宿主真实proc不改动。
# 函数用途: 提供可模拟退出、PID复用及父子关系的隔离进程树。
@pytest.fixture
def fake_install_proc(tmp_path, monkeypatch):
    proc = tmp_path / 'proc'
    proc.mkdir()
    prefix = tmp_path / 'install'
    executable = prefix / 'releases/test/runtime/bin/python3'
    executable.parent.mkdir(parents=True)
    executable.write_text('synthetic executable')

    # LLM: 仅路由精确/proc根和其子路径，其它文件保留真实临时Path。
    # 函数用途: 将安装器的Linux观察点映射为本例临时样本。
    def mapped_path(value='.'):
        text = str(value)
        return proc if text == '/proc' else proc / text[6:] if text.startswith('/proc/') else Path(value)

    monkeypatch.setattr(installer, 'Path', mapped_path)
    handles = {}

    # LLM: 普通临时文件FD模拟pidfd，映射本例合成PID；真实os.close负责关闭描述符。
    # 函数用途: 验证安全句柄发信号分支，不依赖测试主机的Linux版本。
    def pidfd_open(pid):
        descriptor = os.open(proc / str(pid) / 'exe', os.O_RDONLY)
        handles[descriptor] = pid
        return descriptor

    monkeypatch.setattr(installer.os, 'pidfd_open', pidfd_open, raising=False)
    monkeypatch.setattr(installer.signal, 'pidfd_send_signal', lambda descriptor, sig:
        installer.os.kill(handles[descriptor], sig), raising=False)
    return SimpleNamespace(proc=proc, prefix=prefix, executable=executable)


def test_inspect_installed_processes_reports_sorted_tty_and_parent(fake_install_proc):
    case = fake_install_proc
    parent = case.proc.parent / 'bash'
    parent.write_text('shell')
    _proc_entry(case.proc, 701, parent, argv=['bash', 'tools/hourly_loop.sh'])
    _proc_entry(case.proc, 703, case.executable, ppid=701, argv=['python3', 'tools/fetch.py'])
    _proc_entry(case.proc, 702, case.executable, tty=34816, argv=['python3', '-m', 'agent_py_agent'])
    _proc_entry(case.proc, 704, parent, argv=['bash', 'unrelated.sh'])
    rows = installer.inspect_active_processes(case.prefix)
    assert rows == [
        {'pid': 702, 'ppid': 1, 'tty_nr': 34816, 'interactive': True,
         'cmdline': 'python3 -m agent_py_agent', 'parent_cmdline': ''},
        {'pid': 703, 'ppid': 701, 'tty_nr': 0, 'interactive': False,
         'cmdline': 'python3 tools/fetch.py', 'parent_cmdline': 'bash tools/hourly_loop.sh'}]
    assert installer.active_processes(case.prefix) == [702, 703]


def test_interactive_update_error_preserves_prefix_and_adds_command(fake_install_proc):
    case = fake_install_proc
    _proc_entry(case.proc, 702, case.executable, tty=34816, argv=['python3', '-m', 'agent_py_agent'])
    with pytest.raises(ValueError, match='请先退出本安装 TUI/交互进程再更新：PID 702') as error:
        installer.assert_interactive_processes_exited(case.prefix)
    assert '(python3 -m agent_py_agent)' in str(error.value)


def test_busy_installation_keeps_exact_prefix_and_adds_process_details(tmp_path, monkeypatch):
    prefix = tmp_path / 'install'
    monkeypatch.setattr(installer, 'active_processes', lambda _: [])
    installer.install(_bundle(tmp_path / 'first'), prefix)
    monkeypatch.setattr(installer, 'active_processes', lambda _: [703])
    monkeypatch.setattr(installer, 'inspect_active_processes', lambda _: [
        {'pid': 703, 'ppid': 701, 'tty_nr': 0, 'interactive': False, 'cmdline': 'python3 tools/fetch.py', 'parent_cmdline': 'bash loop.sh'}], raising=False)
    with pytest.raises(ValueError) as error:
        installer.install(_bundle(tmp_path / 'second', 'test.2'), prefix)
    text = str(error.value)
    assert text.startswith('请先退出本安装的 TUI 并停止 Gateway 后重试；活动 PID: [703]')
    assert 'PPID 701' in text and 'python3 tools/fetch.py' in text
    assert (prefix / 'current').resolve().name == 'test.1'


@pytest.mark.parametrize('stubborn', [False, True])
def test_orphan_cleanup_terminates_wrapper_and_child_not_tty(fake_install_proc, monkeypatch, capsys, stubborn):
    case = fake_install_proc
    parent = case.proc.parent / 'bash'
    parent.write_text('shell')
    parent_entry = _proc_entry(case.proc, 701, parent, argv=['bash', 'tools/hourly_loop.sh'])
    (parent_entry / 'cwd').symlink_to(case.prefix)
    script = case.prefix / 'tools/hourly_loop.sh'
    script.parent.mkdir()
    script.write_text('synthetic loop')
    _proc_entry(case.proc, 703, case.executable, ppid=701, argv=['python3', 'tools/fetch.py'])
    _proc_entry(case.proc, 702, case.executable, tty=34816, argv=['python3', '-m', 'agent_py_agent'])
    sent = []

    # LLM: 捕获信号且只改合成exe链接模拟退出；不能把信号发给真实主机PID。
    # 函数用途: 模拟TERM退出或必须KILL的遗留脚本。
    def kill(pid, sig):
        sent.append((pid, sig))
        if sig == signal.SIGKILL or not stubborn:
            (case.proc / str(pid) / 'exe').unlink(missing_ok=True)

    monkeypatch.setattr(installer.os, 'kill', kill)
    cleaned = installer.cleanup_orphan_background_processes(case.prefix, timeout=0)
    assert isinstance(cleaned, list)
    assert [row['pid'] for row in cleaned] == [703]
    assert sent[:2] == [(701, signal.SIGTERM), (703, signal.SIGTERM)]
    assert (703, signal.SIGKILL) in sent if stubborn else all(sig != signal.SIGKILL for _, sig in sent)
    assert all(pid != 702 for pid, _ in sent)
    text = capsys.readouterr().out
    assert 'PID 703' in text and 'PPID 701' in text and 'python3 tools/fetch.py' in text


# LLM: 服务路径来自原owner合同；真实安装只写临时数据，服务命令由每例显式拦截。
# 函数用途: 为维护锁和Supervisor更新创建空白安装与准确服务目录。
@pytest.fixture
def update_installation(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, 'active_processes', lambda _: [])
    monkeypatch.setattr(installer.shutil, 'which', lambda _: None)
    prefix = tmp_path / 'install'
    installer.install(_bundle(tmp_path / 'bundle'), prefix)
    service = installer.workspace_path(prefix) / 'workspace/runtime/services/gateway'
    service.mkdir(parents=True)
    return SimpleNamespace(prefix=prefix, service=service)


@pytest.mark.parametrize('systemd', [False, True])
def test_update_maintenance_lock_supervisor_stop_restore_and_order(update_installation, monkeypatch, systemd):
    from agent_py_agent.agent.gateway_parts.daemon_control import write_pid_record

    case = update_installation
    for name in ('gateway.pid', 'adapter.pid', 'supervisor.pid'):
        write_pid_record(case.service / name)
    (case.service / 'adapter_state.json').write_text(json.dumps({'channels': [{'name': 'qq'}]}))
    (case.service / 'supervisor_service.json').write_text(json.dumps({'unit_name': 'custom-agent-supervisor.service'}))
    lock = case.service / 'maintenance_lock.json'
    commands = []
    original_run = subprocess.run
    if systemd:
        monkeypatch.setattr(installer.shutil, 'which', lambda name: '/usr/bin/systemctl' if name == 'systemctl' else None)

    # LLM: 观察原服务命令前的维护锁，返回码不制造真实停启行为。
    # 函数用途: 捕获CLI/systemd顺序并证明锁先于任何停止命令。
    def run(command, **kwargs):
        if command[0] not in ('systemctl', str(case.prefix / 'bin/my-agent-preview')):
            return original_run(command, **kwargs)
        if 'stop' in command or 'supervisor-stop' in command:
            row = json.loads(lock.read_text())
            assert row['schema'] == 'my-agent.maintenance-lock.v1' and row['reason'] == 'offline_update'
            assert row['actor'] == 'installer' and row['pid'] == os.getpid()
            assert row['expires_at'] - row['created_at'] == 1800
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    # LLM: 代替杀进程，只检查stop全部完成后才进入清理。
    # 函数用途: 验证清理阶段的调用时机与维护锁仍然存在。
    def cleanup(prefix):
        assert prefix == case.prefix and lock.is_file()
        assert commands[-1][1:3] == ['gateway', 'stop']
        commands.append(['cleanup'])
        return []

    monkeypatch.setattr(installer.subprocess, 'run', run)
    monkeypatch.setattr(installer, 'cleanup_orphan_background_processes', cleanup, raising=False)
    original = installer.update_services(case.prefix)
    assert lock.is_file()
    assert any(command[1:3] == ['gateway', 'supervisor-stop'] for command in commands)
    assert commands[-1] == ['cleanup']
    commands.clear()
    installer.update_services(case.prefix, restore=original)
    assert not lock.exists()
    expected = ['systemctl', '--user', 'start', 'custom-agent-supervisor.service'] if systemd else [
        str(case.prefix / 'bin/my-agent-preview'), 'gateway', 'supervisor-start']
    assert commands[-1] == expected
    assert commands[0][1:3] == ['gateway', 'start']
    assert commands[1][1:] == ['adapter', 'start', '--daemon', '--channel', 'qq']


def test_legacy_restore_tuple_always_removes_maintenance_lock(update_installation, monkeypatch):
    case = update_installation
    lock = case.service / 'maintenance_lock.json'
    lock.write_text('{}')
    commands = []
    monkeypatch.setattr(installer.subprocess, 'run', lambda command, **kwargs:
        commands.append(command) or subprocess.CompletedProcess(command, 0))
    assert installer.update_services(case.prefix, restore=(True, False, 'feishu')) == (True, False, 'feishu')
    assert not lock.exists() and commands == [[str(case.prefix / 'bin/my-agent-preview'), 'gateway', 'start', '--ready-timeout', '60']]


@pytest.mark.parametrize('failed_component', ['gateway', 'adapter'])
def test_restore_attempts_all_components_after_one_start_failure(update_installation, monkeypatch, failed_component):
    case = update_installation
    lock = case.service / 'maintenance_lock.json'
    lock.write_text('{}')
    commands = []

    # LLM: 只对一个恢复命令返回失败，记录其它原组件是否仍被尝试启动。
    # 函数用途: 复现首个组件失败导致Supervisor永久未恢复的问题。
    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, int(command[1:3] == [failed_component, 'start']))

    monkeypatch.setattr(installer.subprocess, 'run', run)
    state = (True, True, 'qq', True, True, 'my-agent-gw-supervisor.service')
    with pytest.raises(ValueError, match='服务'):
        installer.update_services(case.prefix, restore=state)
    assert not lock.exists()
    assert [command[1:3] for command in commands[:2]] == [['gateway', 'start'], ['adapter', 'start']]
    assert commands[-1] == ['systemctl', '--user', 'start', 'my-agent-gw-supervisor.service']


@pytest.mark.parametrize('filename,payload', [
    ('supervisor_service.json', []), ('release.json', []), ('release.json', {'protocols': 'invalid'}),
    ('release.json', {'protocols': []}), ('release.json', {'protocols': False})])
def test_update_rejects_bad_object_metadata_without_leaving_maintenance(update_installation, monkeypatch, filename, payload):
    case = update_installation
    target = case.prefix / 'current/release.json' if filename == 'release.json' else case.service / filename
    target.write_text(json.dumps(payload))
    commands = []
    monkeypatch.setattr(installer.subprocess, 'run', lambda command, **kwargs:
        commands.append(command) or subprocess.CompletedProcess(command, 0))
    with pytest.raises(ValueError):
        installer.update_services(case.prefix)
    assert not (case.service / 'maintenance_lock.json').exists()
    assert not commands


def test_proc_stat_right_parenthesis_in_name_does_not_hide_tty(fake_install_proc):
    case = fake_install_proc
    entry = _proc_entry(case.proc, 702, case.executable, ppid=701, tty=34816, argv=['python3'])
    stat = entry / 'stat'
    stat.write_text(stat.read_text().replace('(synthetic process)', '(worker) helper)'))
    rows = installer.inspect_active_processes(case.prefix)
    assert len(rows) == 1 and rows[0]['tty_nr'] == 34816 and rows[0]['ppid'] == 701


def test_cleanup_without_safe_pid_handle_does_not_use_bare_kill(fake_install_proc, monkeypatch):
    case = fake_install_proc
    _proc_entry(case.proc, 703, case.executable, argv=['python3', 'tools/fetch.py'])
    monkeypatch.delattr(installer.os, 'pidfd_open', raising=False)
    sent = []
    monkeypatch.setattr(installer.os, 'kill', lambda *args: sent.append(args))
    with pytest.raises(ValueError, match='pidfd'):
        installer.cleanup_orphan_background_processes(case.prefix, timeout=0)
    assert not sent


# LLM: 仅第一参数确认为本安装脚本才允许停止shell，不把任意.sh参数或共享任务当专用包装器。
# 函数用途: 锁住跨安装父脚本及旧PPID复用边界；子解释器仍可精准清理。
@pytest.mark.parametrize('parent_case', ['shared', 'argument', 'reparented'])
def test_cleanup_does_not_signal_unowned_or_old_parent(fake_install_proc, monkeypatch, parent_case):
    case = fake_install_proc
    shell = case.proc.parent / 'bash'
    shell.write_text('shell')
    shared = case.proc.parent / 'shared-job'
    shared.write_text('synthetic shared task')
    parent = _proc_entry(case.proc, 701, shell, argv=['bash', str(shared), 'config.sh'])
    (parent / 'cwd').symlink_to(case.prefix)
    child = _proc_entry(case.proc, 703, case.executable, ppid=701, argv=['python3'])
    if parent_case == 'shared':
        (parent / 'cmdline').write_bytes(f'bash\0{shared}.sh\0'.encode())
    if parent_case == 'reparented':
        script = case.prefix / 'loop.sh'
        script.write_text('synthetic dedicated wrapper')
        (parent / 'cmdline').write_bytes(f'bash\0{script}\0'.encode())
        snapshots = installer.inspect_active_processes(case.prefix)
        stat = child / 'stat'
        stat.write_text(stat.read_text().replace('S 701 ', 'S 1 '))
        monkeypatch.setattr(installer, 'inspect_active_processes', lambda _: snapshots)
    sent = []

    # LLM: 本例只模拟所选PID退出，不允许任何信号到达主机。
    # 函数用途: 检查旧父进程或共享父脚本不会被清理。
    def kill(pid, sig):
        sent.append(pid)
        (case.proc / str(pid) / 'exe').unlink(missing_ok=True)

    monkeypatch.setattr(installer.os, 'kill', kill)
    installer.cleanup_orphan_background_processes(case.prefix, timeout=0)
    assert 701 not in sent and 703 in sent


def test_install_failure_and_restore_failure_both_reach_error_report(update_installation, monkeypatch, capsys):
    case = update_installation
    monkeypatch.setattr(sys, 'argv', ['installer', '--update'])
    monkeypatch.setattr(installer, 'resolve_install_prefix', lambda _: case.prefix)
    monkeypatch.setattr(installer, 'verify', lambda _: {'version': 'synthetic'})
    monkeypatch.setattr(installer, 'assert_interactive_processes_exited', lambda _: None)

    # LLM: 仅制造两次不同阶段错误，不调用真实服务控制。
    # 函数用途: 证明恢复错误不能掩盖最初安装失败原因。
    def update(prefix, *, restore=None):
        if restore is not None:
            raise ValueError('synthetic restore failure')
        return (True, False, 'feishu')

    # LLM: 只在安装入口抛合成错误，不修改发行版本或用户数据。
    # 函数用途: 重现切换失败后恢复也失败的报告路径。
    def install(*args):
        raise ValueError('synthetic install failure')

    monkeypatch.setattr(installer, 'update_services', update)
    monkeypatch.setattr(installer, 'install', install)
    assert installer.main() == 1
    error = capsys.readouterr().err
    assert 'synthetic install failure' in error and 'synthetic restore failure' in error


def test_pidfd_identity_change_skips_signal_and_closes_handle(fake_install_proc, monkeypatch):
    from unittest.mock import Mock

    case = fake_install_proc
    entry = _proc_entry(case.proc, 703, case.executable, argv=['python3'])
    identity = installer._process_identity(703)
    original_open = installer.os.pidfd_open
    handles = []

    # LLM: 在句柄捕获后改变合成出生时间，模拟PID复用；真实文件FD必须仍关闭。
    # 函数用途: 证明发信号前复核失败时不向新进程发送TERM。
    def changed_open(pid):
        descriptor = original_open(pid)
        handles.append(descriptor)
        stat = entry / 'stat'
        stat.write_text(stat.read_text().rsplit(' ', 1)[0] + ' 9999')
        return descriptor

    sender = Mock(side_effect=AssertionError('不能给新进程发信号'))
    monkeypatch.setattr(installer.os, 'pidfd_open', changed_open)
    monkeypatch.setattr(installer.signal, 'pidfd_send_signal', sender)
    installer._signal_process(703, identity, signal.SIGTERM)
    sender.assert_not_called()
    with pytest.raises(OSError):
        os.fstat(handles[0])


def test_foreign_uid_interpreter_is_not_automatically_stopped(fake_install_proc, monkeypatch):
    case = fake_install_proc
    _proc_entry(case.proc, 703, case.executable, argv=['python3'])
    identity = installer._process_identity(703)
    monkeypatch.setattr(installer, '_process_identity', lambda _: (identity[0] + 1, *identity[1:]))
    sent = []
    monkeypatch.setattr(installer.os, 'kill', lambda *args: sent.append(args))
    assert installer.cleanup_orphan_background_processes(case.prefix, timeout=0) == []
    assert not sent


def test_stop_failure_restores_all_from_captured_paths(update_installation, monkeypatch):
    from agent_py_agent.agent.gateway_parts.daemon_control import write_pid_record

    case = update_installation
    for name in ('gateway.pid', 'adapter.pid', 'supervisor.pid'):
        write_pid_record(case.service / name)
    (case.service / 'adapter_state.json').write_text(json.dumps({'channels': [{'name': 'feishu'}]}))
    paths = installer._update_service_paths(case.prefix)
    calls = []
    commands = []
    original_run = subprocess.run

    # LLM: 停止阶段仅第一次能解析路径；回滚必须消费已捕获的准确root。
    # 函数用途: 防止失败后重新读取损坏配置让维护锁留在原目录。
    def resolve(prefix):
        assert not calls
        calls.append(prefix)
        return paths

    # LLM: Gateway停止失败，其余停启回执成功；不执行真实CLI。
    # 函数用途: 验证失败回滚不会遗漏Adapter或Supervisor。
    def run(command, **kwargs):
        if command[0] != str(case.prefix / 'bin/my-agent-preview'):
            return original_run(command, **kwargs)
        commands.append(command)
        return subprocess.CompletedProcess(command, int(command[1:3] == ['gateway', 'stop']))

    monkeypatch.setattr(installer, '_update_service_paths', resolve)
    monkeypatch.setattr(installer.subprocess, 'run', run)
    with pytest.raises(ValueError, match='服务控制未完成'):
        installer.update_services(case.prefix)
    assert not (case.service / 'maintenance_lock.json').exists()
    assert [command[1:3] for command in commands[-3:]] == [
        ['gateway', 'start'], ['adapter', 'start'], ['gateway', 'supervisor-start']]


def test_installer_maintenance_pauses_real_supervisor_health_entry(update_installation, monkeypatch):
    from agent_py_agent.agent.gateway_parts import supervisor

    case = update_installation
    monkeypatch.setattr(installer.subprocess, 'run', lambda command, **kwargs: subprocess.CompletedProcess(command, 0))
    paths = installer._update_service_paths(case.prefix)
    checked = []
    watcher = SimpleNamespace(_paths=paths, _resolve_agent_and_paths=lambda: None,
        _is_gateway_healthy=lambda: checked.append('gateway') or True,
        _check_adapter_health=lambda: checked.append('adapter') or True)
    state = installer.update_services(case.prefix)
    lock = supervisor.read_maintenance_lock(paths)
    assert lock['actor'] == 'installer' and lock['reason'] == 'offline_update'
    assert supervisor._run_health_check(watcher, 2) == 0
    assert not checked
    installer.update_services(case.prefix, restore=state)
    assert supervisor.read_maintenance_lock(paths) is None
    assert supervisor._run_health_check(watcher, 2) == 0
    assert checked == ['gateway', 'adapter']


# LLM: 合成运行时只供真实build.main编译和归档，不执行伪原生文件或安装入口，不读取当前安装。
# 函数用途: 准备包含既有bwrap资源的最小发行构建目录。
@pytest.fixture
def c4_build_case(tmp_path, monkeypatch):
    root, helpers, output = tmp_path / 'bundle', tmp_path / 'helpers', tmp_path / 'output'
    package = root / 'runtime/lib/python3.11/site-packages/agent_py_agent'
    for path in (root / 'runtime/bin', root / 'native/lib', package / 'vendor/bin', package / 'vendor/bubblewrap', package / 'config', helpers):
        path.mkdir(parents=True, exist_ok=True)
    (root / 'runtime/bin/python3').write_bytes(b'synthetic runtime')
    (root / 'native/bwrap').write_bytes(b'synthetic bwrap')
    (root / 'native/lib/libfixture.so').write_bytes(b'synthetic library')
    (root / 'native/PROVENANCE.txt').write_text('synthetic provenance')
    (package / '__init__.py').write_text('VALUE = 1\n')
    for name in ('installer', 'configure'):
        (helpers / f'{name}.py').write_text('VALUE = 1\n')
    monkeypatch.setattr(sys, 'argv', ['build', '--bundle', str(root), '--helpers', str(helpers), '--output', str(output),
        '--version', 'test.cli', '--base-commit', 'fixture', '--source-sha256', '0' * 64])
    return SimpleNamespace(root=root, package=package, output=output)


@pytest.mark.parametrize('source', ['native', 'wheel', 'both'])
def test_c4_build_includes_cli_backend_and_lark_resources(c4_build_case, source):
    case = c4_build_case
    backend = case.package / 'vendor/bin/feishu-cli-host.linux-x86_64'
    old = case.package / 'vendor/lark_cli'
    old.mkdir()
    (old / 'existing.txt').write_text('preserved')
    if source in {'wheel', 'both'}:
        backend.write_bytes(b'wheel backend')
        backend.chmod(0o600)
    if source in {'native', 'both'}:
        native = case.root / 'native/feishu-cli-host.linux-x86_64'
        native.write_bytes(b'native backend')
        native.chmod(0o600)
        lark = case.root / 'native/lark_cli'
        lark.mkdir()
        (lark / 'release.json').write_text('{"version":"fixture"}')
    assert build.main() == 0
    expected = b'wheel backend' if source == 'wheel' else b'native backend'
    assert backend.read_bytes() == expected and backend.stat().st_mode & 0o777 == 0o755
    relative = backend.relative_to(case.root).as_posix()
    manifest = json.loads((case.root / 'release.json').read_text())
    assert manifest['files'][relative]['sha256'] == hashlib.sha256(expected).hexdigest()
    with tarfile.open(case.output / 'my-agent-test.cli-linux-x86_64.tar.gz') as archive:
        member = archive.getmember('bundle/' + relative)
        assert member.mode & 0o777 == 0o755 and archive.extractfile(member).read() == expected
        existing_member = 'bundle/' + (case.package / 'vendor/lark_cli/existing.txt').relative_to(case.root).as_posix()
        assert archive.extractfile(existing_member).read() == b'preserved'
        if source != 'wheel':
            lark_member = 'bundle/' + (case.package / 'vendor/lark_cli/release.json').relative_to(case.root).as_posix()
            assert archive.extractfile(lark_member).read() == b'{"version":"fixture"}'
            assert (case.package / 'vendor/lark_cli/existing.txt').read_text() == 'preserved'
    assert (case.output / 'my-agent-test.cli-linux-x86_64.run').stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize('missing', ['backend', 'lark_cli'])
def test_c4_build_refuses_missing_cli_backend_before_publishing(c4_build_case, missing):
    case = c4_build_case
    if missing == 'lark_cli':
        (case.package / 'vendor/bin/feishu-cli-host.linux-x86_64').write_bytes(b'wheel backend')
        match = '发行包缺少随包飞书 CLI 资源: vendor/lark_cli'
    else:
        lark = case.package / 'vendor/lark_cli'
        lark.mkdir()
        (lark / 'existing.txt').write_text('preserved')
        match = '发行包缺少随包飞书 CLI 后端: vendor/bin/feishu-cli-host.linux-x86_64'
    with pytest.raises(ValueError, match=match):
        build.main()
    assert not case.output.exists()
    assert (case.package / '__init__.py').is_file()


# LLM: 用合成proc及真实可关闭文件FD代替进程句柄；裸PID信号即失败，所有native/syscall发送由各例替换。
# 函数用途: 追踪发信号前的身份重核及句柄关闭，不触及真实进程。
@pytest.fixture
def c4_signal_case(fake_install_proc, monkeypatch):
    case = fake_install_proc
    _proc_entry(case.proc, 703, case.executable, argv=['python3'])
    case.identity, case.opened = installer._process_identity(703), []
    original_open = installer.os.pidfd_open

    # LLM: 捕获真实临时FD，调用后用fstat验证已关闭；不向内核申请真实进程pidfd。
    # 函数用途: 记录合成句柄打开情况。
    def opened(pid):
        descriptor = original_open(pid)
        case.opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(installer.os, 'pidfd_open', opened)
    monkeypatch.setattr(installer.os, 'kill', Mock(side_effect=AssertionError('不能回落裸PID信号')))
    return case


def test_c4_pidfd_native_signal_is_preferred(c4_signal_case, monkeypatch):
    case = c4_signal_case
    sender, loader = Mock(), Mock(side_effect=AssertionError('native接口存在时不能调用syscall'))
    monkeypatch.setattr(signal, 'pidfd_send_signal', sender, raising=False)
    monkeypatch.setattr(ctypes, 'CDLL', loader)
    installer._signal_process(703, case.identity, signal.SIGTERM)
    sender.assert_called_once_with(case.opened[0], signal.SIGTERM)
    loader.assert_not_called()
    with pytest.raises(OSError):
        os.fstat(case.opened[0])


@pytest.mark.parametrize('machine', ['x86_64', 'amd64'])
@pytest.mark.parametrize('error', [0, errno.ESRCH, errno.EPERM])
def test_c4_pidfd_syscall_fallback_and_errno(c4_signal_case, monkeypatch, machine, error):
    case = c4_signal_case
    monkeypatch.delattr(signal, 'pidfd_send_signal', raising=False)
    monkeypatch.setattr(sys, 'platform', 'linux')
    monkeypatch.setattr(os, 'uname', lambda: SimpleNamespace(machine=machine))
    syscall = Mock(return_value=-1 if error else 0)
    loader = Mock(return_value=SimpleNamespace(syscall=syscall))
    get_errno = Mock(return_value=error)
    monkeypatch.setattr(ctypes, 'CDLL', loader)
    monkeypatch.setattr(ctypes, 'get_errno', get_errno)
    if error == errno.EPERM:
        with pytest.raises(OSError) as caught:
            installer._signal_process(703, case.identity, signal.SIGTERM)
        assert caught.value.errno == error
    else:
        installer._signal_process(703, case.identity, signal.SIGTERM)
    loader.assert_called_once_with(None, use_errno=True)
    syscall.assert_called_once_with(424, case.opened[0], int(signal.SIGTERM), None, 0)
    assert get_errno.call_count == bool(error)
    installer.os.kill.assert_not_called()
    with pytest.raises(OSError):
        os.fstat(case.opened[0])


@pytest.mark.parametrize('platform,machine', [('darwin', 'x86_64'), ('linux', 'aarch64')])
def test_c4_pidfd_unsupported_fallback_closes_handle(c4_signal_case, monkeypatch, platform, machine):
    case = c4_signal_case
    monkeypatch.delattr(signal, 'pidfd_send_signal', raising=False)
    monkeypatch.setattr(sys, 'platform', platform)
    monkeypatch.setattr(os, 'uname', lambda: SimpleNamespace(machine=machine))
    loader = Mock(side_effect=AssertionError('不能在未支持架构执行424'))
    monkeypatch.setattr(ctypes, 'CDLL', loader)
    with pytest.raises(ValueError, match='pidfd'):
        installer._signal_process(703, case.identity, signal.SIGTERM)
    assert len(case.opened) == 1
    with pytest.raises(OSError):
        os.fstat(case.opened[0])
    loader.assert_not_called()
    installer.os.kill.assert_not_called()


@pytest.mark.parametrize('machine', ['x86_64', 'amd64'])
def test_c4_pidfd_open_syscall_fallback_when_os_pidfd_open_missing(fake_install_proc, monkeypatch, machine):
    from unittest.mock import Mock

    case = fake_install_proc
    _proc_entry(case.proc, 703, case.executable, argv=['python3'])
    identity = installer._process_identity(703)
    monkeypatch.delattr(installer.os, 'pidfd_open', raising=False)
    monkeypatch.delattr(signal, 'pidfd_send_signal', raising=False)
    monkeypatch.setattr(sys, 'platform', 'linux')
    monkeypatch.setattr(os, 'uname', lambda: SimpleNamespace(machine=machine))
    monkeypatch.setattr(installer.os, 'kill', Mock(side_effect=AssertionError('不能回落裸PID信号')))
    r_fd, w_fd = os.pipe()
    os.close(w_fd)
    calls = []

    def fake_syscall(number, *args):
        calls.append((number, args))
        if number == 434:
            return r_fd
        if number == 424:
            return 0
        raise AssertionError(f'unexpected syscall {number}')

    monkeypatch.setattr(ctypes, 'CDLL', Mock(return_value=SimpleNamespace(syscall=fake_syscall)))
    installer._signal_process(703, identity, signal.SIGTERM)
    assert calls == [(434, (703, 0)), (424, (r_fd, int(signal.SIGTERM), None, 0))]
    with pytest.raises(OSError):
        os.fstat(r_fd)
