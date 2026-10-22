# LLM: 只使用临时YAML/原JSON与fake进程边界核服务路径和角色身份；不启动服务/模型，不把fake结果当systemd实际通过。
# 模块用途: 验证服务管理器与监护启动不会丢home/config，监护者不能冒充Gateway健康。
import json
import os
import shlex
import time
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import supervisor as sv
from agent_py_agent.agent.gateway_parts.daemon_control import RunningPidReport
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.cli import gateway_service


@pytest.fixture
def service_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_service, '_detect_venv_dir', lambda: None)
    monkeypatch.setattr(gateway_service, '_resolve_node_path', lambda: None)
    monkeypatch.setattr(gateway_service, 'get_python_path', lambda: '/runtime path/python')
    return tmp_path


@pytest.mark.parametrize('manager', ['systemd', 'launchd'])
@pytest.mark.parametrize('home_source', ['config', 'environment'])
@pytest.mark.parametrize('suffix', ['plain', 'space & xml', 'percent% dollar$ quote" back\\slash'])
def test_service_manager_freezes_selected_config_and_original_home(service_paths, monkeypatch, manager, home_source, suffix):
    root = service_paths / suffix
    root.mkdir()
    home = root / 'actual-home'
    config = root / 'agent.yaml'
    configured = str(home) if home_source == 'config' else '~/.my-agent'
    config.write_text('model_backend: echo\nmy_agent_home: '+configured+'\n')
    monkeypatch.setenv('MY_AGENT_HOME', str(home if home_source == 'environment' else root / 'wrong-env'))
    monkeypatch.setenv('MY_AGENT_CONFIG', str(root / 'wrong-config.yaml'))
    if manager == 'systemd':
        text = gateway_service.generate_systemd_unit_text(config_path=config)
        environment = {}
        for line in text.splitlines():
            if line.startswith('Environment='):
                for assignment in shlex.split(line.removeprefix('Environment=')):
                    key, value = assignment.split('=', 1)
                    environment[key] = value.replace('%%', '%')
        command = next(line.removeprefix('ExecStart=') for line in text.splitlines() if line.startswith('ExecStart='))
        arguments = [part.replace('%%', '%').replace('$$', '$') for part in shlex.split(command)]
        cwd = next(line.removeprefix('WorkingDirectory=') for line in text.splitlines() if line.startswith('WorkingDirectory=')).replace('%%', '%')
    else:
        document = ET.fromstring(gateway_service.generate_launchd_plist_text(config_path=config))
        items = list(document.find('dict'))
        values = {items[i].text: items[i+1] for i in range(0, len(items), 2)}
        arguments = [node.text for node in values['ProgramArguments']]
        entries = list(values['EnvironmentVariables'])
        environment = {entries[i].text: entries[i+1].text for i in range(0, len(entries), 2)}
        cwd = values['WorkingDirectory'].text
    assert environment['MY_AGENT_CONFIG'] == str(config.resolve())
    assert environment['MY_AGENT_HOME'] == str(home.resolve())
    assert arguments[arguments.index('--config')+1] == str(config.resolve())
    assert arguments[0] == '/runtime path/python'
    assert cwd == str(home.resolve() / 'service-cwd')
    assert not home.exists(), 'rendering cannot create the service home'


@pytest.mark.parametrize('manager', ['systemd', 'launchd'])
def test_invalid_selected_service_config_never_uses_unrelated_default(service_paths, monkeypatch, manager):
    other = service_paths / 'other.yaml'
    other.write_text('model_backend: echo\n')
    monkeypatch.setattr(gateway_service, '_get_config_path', lambda: str(other))
    render = gateway_service.generate_systemd_unit_text if manager == 'systemd' else gateway_service.generate_launchd_plist_text
    with pytest.raises(FileNotFoundError):
        render(config_path=service_paths / 'missing.yaml')


# LLM: 身份核验的OS边界用假读者，文件/role/生年代及ready仍走真实产品读取；不向任何PID发信号。
# 函数用途: 建同一Gateway角色记录，逐字段破坏时确认不能仅凭新鲜心跳健康。
@pytest.fixture
def health(tmp_path, monkeypatch):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    paths.root.mkdir()
    identity = {'host_id': 'synthetic-host', 'pid': 12345, 'start_time': '123456'}
    now = time.time()
    record = {**identity, 'kind': 'my-agent-gateway', 'updated_at': '2026-10-20T00:00:00+00:00'}
    state = {'status': 'running', 'pid': identity['pid'], 'process_identity': identity, 'started_at': now-3}
    heartbeat = {'status': 'running', 'pid': identity['pid'], 'started_at': now-3, 'updated_at': now,
                 'dispatcher_alive': True, 'dispatcher_state': 'running'}
    for path, value in [(paths.pid, record), (paths.state, state), (paths.heartbeat, heartbeat)]:
        path.write_text(json.dumps(value))
    agent = SimpleNamespace(config=SimpleNamespace(gateway_worker_process_count=0, config_path='synthetic.yaml'))
    supervisor = sv.GatewaySupervisor('synthetic.yaml', agent=agent)
    supervisor._paths = paths
    monkeypatch.setattr(sv, 'get_running_pid_report', lambda path, **kwargs: RunningPidReport(12345))
    monkeypatch.setattr(sv, 'process_identity_is_live', lambda value: value == identity, raising=False)
    return SimpleNamespace(paths=paths, identity=identity, record=record, state=state,
                           heartbeat=heartbeat, supervisor=supervisor, now=now)


def test_supervisor_health_accepts_only_original_gateway_ready_identity(health):
    before = {p: p.read_bytes() for p in (health.paths.pid,health.paths.state,health.paths.heartbeat)}
    assert health.supervisor._is_gateway_healthy()
    assert all(path.read_bytes() == raw for path,raw in before.items())


@pytest.mark.parametrize('fault', ['role','pid','birth','host','state_pid','state_birth','state_failed',
    'heartbeat_pid','heartbeat_start','heartbeat_missing_start','heartbeat_not_running','stale','future',
    'dispatcher_dead','dispatcher_missing','missing_identity','legacy_pid'])
def test_fresh_supervisor_or_unbound_gateway_cannot_report_healthy(health, fault):
    record, state, heartbeat = health.record, health.state, health.heartbeat
    if fault == 'role':
        record['kind'] = 'my-agent-supervisor'
    elif fault in {'pid','birth','host'}:
        key = {'pid':'pid','birth':'start_time','host':'host_id'}[fault]
        record[key] = 999 if key == 'pid' else 'another-value'
    elif fault == 'state_pid':
        state['pid'] = 999
    elif fault == 'state_birth':
        state['process_identity'] = {**health.identity, 'start_time':'another-value'}
    elif fault == 'state_failed':
        state['status'] = 'failed'
    elif fault == 'heartbeat_pid':
        heartbeat['pid'] = 999
    elif fault == 'heartbeat_start':
        heartbeat['started_at'] -= 60
    elif fault == 'heartbeat_missing_start':
        heartbeat.pop('started_at')
    elif fault == 'heartbeat_not_running':
        heartbeat['status'] = 'starting'
    elif fault == 'stale':
        heartbeat['updated_at'] = health.now-500
    elif fault == 'future':
        heartbeat['updated_at'] = health.now+500
    elif fault == 'dispatcher_dead':
        heartbeat['dispatcher_alive'] = False
        heartbeat['dispatcher_state'] = 'exited'
    elif fault == 'dispatcher_missing':
        heartbeat.pop('dispatcher_alive')
    elif fault == 'missing_identity':
        record.pop('start_time')
    else:
        record = 12345
    for path, value in [(health.paths.pid,record),(health.paths.state,state),(health.paths.heartbeat,heartbeat)]:
        path.write_text(json.dumps(value))
    before = {p:p.read_bytes() for p in (health.paths.pid,health.paths.state,health.paths.heartbeat)}
    assert not health.supervisor._is_gateway_healthy()
    assert health.supervisor._last_health_load_errors
    assert all(p.read_bytes() == raw for p,raw in before.items())


def test_supervisor_lifecycle_never_overwrites_gateway_state_or_pid(tmp_path, monkeypatch):
    paths = gateway_paths_from_root(tmp_path)
    paths.state.write_text('{"status":"running","pid":777}')
    paths.pid.write_text('{"pid":777}')
    before = paths.state.read_bytes(), paths.pid.read_bytes()
    agent = SimpleNamespace(config=SimpleNamespace(config_path='synthetic.yaml'))
    supervisor = sv.GatewaySupervisor('synthetic.yaml', agent=agent)
    supervisor._paths = paths
    supervisor._stop_gateway = lambda **kwargs: True
    monkeypatch.setattr(sv.signal, 'signal', lambda *args: None)
    identity = {'host_id':'synthetic-host','pid':os.getpid(),'start_time':'123'}
    monkeypatch.setattr(sv, 'build_process_identity', lambda: identity, raising=False)
    sv._prepare_supervisor_run(supervisor, paths.root / 'supervisor.pid')
    assert (paths.state.read_bytes(),paths.pid.read_bytes()) == before
    assert json.loads((paths.root/'supervisor.pid').read_text())['kind'] == 'my-agent-supervisor'
    sv._finish_supervisor_run(supervisor,paths.root/'supervisor.pid')
    assert (paths.state.read_bytes(),paths.pid.read_bytes()) == before
    status = json.loads((paths.root/'supervisor_state.json').read_text())
    assert status['role'] == 'supervisor' and status['process_identity'] == identity


def test_supervisor_spawn_uses_original_resolved_home_and_config(tmp_path, monkeypatch):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    home, config = tmp_path/'actual-home', tmp_path/'actual config.yaml'
    agent = SimpleNamespace(home_paths=SimpleNamespace(root=home),config=SimpleNamespace(config_path=str(config)))
    supervisor = sv.GatewaySupervisor(str(config), agent=agent)
    supervisor._paths = paths
    monkeypatch.setenv('MY_AGENT_HOME',str(tmp_path/'wrong-home'))
    monkeypatch.setenv('MY_AGENT_CONFIG',str(tmp_path/'wrong.yaml'))
    observed = []
    monkeypatch.setattr(sv.subprocess,'Popen',lambda argv,**kwargs: observed.append((argv,kwargs)) or object())
    sv._spawn_gateway_process(supervisor,['python','-m','agent_py_agent','--config',str(config),'gateway','run'],0,True)
    [(_, kwargs)] = observed
    assert kwargs['env']['MY_AGENT_HOME'] == str(home.resolve())
    assert kwargs['env']['MY_AGENT_CONFIG'] == str(config.resolve())


@pytest.mark.parametrize('record_kind', ['live', 'unknown', 'legacy'])
def test_start_refuses_existing_or_unconfirmed_gateway_without_spawn(health, monkeypatch, record_kind):
    report = RunningPidReport(12345) if record_kind == 'live' else RunningPidReport(None, {'reason': record_kind})
    monkeypatch.setattr(sv, 'get_running_pid_report', lambda *args, **kwargs: report)
    effects = []
    monkeypatch.setattr(sv, '_spawn_gateway_process', lambda *args: effects.append('spawn'))
    assert sv.start_gateway(health.supervisor) is None
    assert effects == []


@pytest.mark.parametrize('stop_result', [False, True])
def test_restart_requires_old_gateway_stop_success(health, monkeypatch, stop_result):
    effects = []
    monkeypatch.setattr(health.supervisor, '_stop_gateway', lambda **kwargs: stop_result)
    monkeypatch.setattr(health.supervisor, '_start_gateway', lambda: effects.append('spawn') or 12346)
    assert sv.restart_gateway(health.supervisor) is stop_result
    assert effects == (['spawn'] if stop_result else [])


# LLM: 新增采集后末核仍只增加同代观察，不增等待次数；换代/未知刺激与不发信号断言保持。
# 函数用途: 验证停止请求及TERM仅作用于原Gateway角色与出生身份。
@pytest.mark.parametrize('mode', ['graceful', 'same_generation_term', 'reused_pid', 'unknown_birth', 'wrong_role', 'unknown_record'])
def test_stop_gateway_signals_only_verified_original_generation(health, monkeypatch, mode):
    effects = []
    lives = iter([True, False] if mode == 'reused_pid' else [True, None] if mode == 'unknown_birth' else [True, True, True])
    monkeypatch.setattr(sv, 'process_identity_is_live', lambda identity: next(lives))
    if mode == 'wrong_role':
        health.record['kind'] = 'my-agent-supervisor'
        health.paths.pid.write_text(json.dumps(health.record))
    if mode == 'unknown_record':
        monkeypatch.setattr(sv, 'get_running_pid_report', lambda *args, **kwargs: RunningPidReport(None, {'reason': 'unknown'}))
    monkeypatch.setattr(sv, '_write_supervisor_stop_request', lambda *args: effects.append('request') or {'accepted': True})
    waits = iter([True] if mode == 'graceful' else [False, True])
    monkeypatch.setattr(sv, 'wait_for_pid_exit', lambda *args: next(waits))
    monkeypatch.setattr(sv, 'terminate_pid', lambda pid: effects.append(('TERM', pid)))
    assert sv.stop_gateway(health.supervisor) is (mode in {'graceful', 'same_generation_term'})
    assert effects == (['request'] if mode in {'graceful','reused_pid','unknown_birth'} else
                       ['request', ('TERM', 12345)] if mode == 'same_generation_term' else [])


# LLM: health替身接受新的quiet关键字，原ready/超时/子进程退出/异PID断言不变。
# 函数用途: 验证启动等待仍不得把未就绪Popen PID冒充成功。
@pytest.mark.parametrize('mode', ['ready', 'timeout', 'child_exited', 'other_pid'])
def test_start_wait_never_projects_unready_popen_pid_as_success(health, monkeypatch, mode):
    counter = iter(range(0, 1000, 61))
    monkeypatch.setattr(sv.time, 'monotonic', lambda: next(counter))
    monkeypatch.setattr(sv.time, 'sleep', lambda _: None)
    monkeypatch.setattr(health.supervisor, '_is_gateway_healthy', lambda **kwargs: mode in {'ready', 'other_pid'})
    process = SimpleNamespace(pid=12346 if mode == 'other_pid' else 12345, poll=lambda: 0 if mode == 'child_exited' else None)
    assert sv._wait_for_gateway_start(health.supervisor, process) == (12345 if mode == 'ready' else None)


@pytest.mark.parametrize('shape', ['typed', 'legacy', 'wrong_role', 'birth_unknown', 'dead'])
def test_supervisor_identity_reader_is_readonly_and_legacy_blocks_new_start(tmp_path, monkeypatch, shape):
    path = tmp_path / 'supervisor.pid'
    identity = {'host_id': 'synthetic-host', 'pid': 12345, 'start_time': '123456'}
    path.write_text('12345' if shape == 'legacy' else json.dumps({**identity, 'kind': 'other' if shape == 'wrong_role' else 'my-agent-supervisor'}))
    before = path.read_bytes()
    monkeypatch.setattr(sv, 'is_pid_alive', lambda pid: True)
    monkeypatch.setattr(sv, 'process_identity_is_live', lambda value: None if shape == 'birth_unknown' else shape != 'dead')
    report = sv.read_supervisor_process_report(path)
    assert path.read_bytes() == before
    if shape == 'typed':
        assert report.load_error is None and report.payload['identity_verified'] is True
    elif shape == 'dead':
        assert report.load_error is None and report.payload['alive'] is False
    else:
        assert report.load_error is not None
        assert not (report.payload or {}).get('identity_verified')


@pytest.mark.parametrize('count', [0, 2])
def test_health_accepts_original_count0_and_coordinator_readiness(health, count):
    health.state['worker_process_count'] = count
    health.paths.state.write_text(json.dumps(health.state))
    assert health.supervisor._is_gateway_healthy()


@pytest.mark.parametrize('stamp', [float('nan'), float('inf'), True, 'fresh', 10 ** 400, -10 ** 400])
def test_untrusted_heartbeat_time_never_reports_fresh(health, stamp):
    assert not health.supervisor._heartbeat_is_fresh({'updated_at': stamp})


@pytest.mark.parametrize('change', ['none', 'pid', 'birth', 'host', 'role', 'unknown', 'dead'])
def test_frozen_stop_writer_refuses_changed_record_or_unverified_birth(tmp_path, monkeypatch, change):
    from agent_py_agent.agent.gateway_parts import daemon_control as dc
    pid_path, stop_path = tmp_path / 'gateway.pid', tmp_path / 'stop.request'
    old = {'host_id': 'test-host', 'pid': 12345, 'start_time': '123456'}
    current = {**old, 'kind': 'my-agent-gateway'}
    if change in {'pid','birth','host'}:
        current[{'pid':'pid','birth':'start_time','host':'host_id'}[change]] = {'pid':12346,'birth':'654321','host':'other-host'}[change]
    if change == 'role':
        current['kind'] = 'my-agent-supervisor'
    pid_path.write_text(json.dumps(current))
    before = pid_path.read_bytes()
    monkeypatch.setattr(dc, 'build_process_identity', lambda pid: dict(old))
    monkeypatch.setattr(dc, 'process_identity_is_live', lambda value: None if change == 'unknown' else change != 'dead')
    result = dc.write_targeted_gateway_stop_request(pid_path, stop_path, reason='test', target_pid=12345, expected_identity=old)
    assert pid_path.read_bytes() == before
    if change == 'none':
        assert result['target_process'] == old
        assert json.loads(stop_path.read_text())['target_process'] == old
    else:
        assert result is None and not stop_path.exists()


@pytest.mark.parametrize('directory', ['/tmp/systemd-fixture/plain/service-cwd', '/tmp/systemd fixture/quote" back\\slash %p/service-cwd'])
def test_working_directory_uses_native_single_path_field(directory):
    text = gateway_service._format_systemd_unit_section('/bin/true', directory, '/usr/bin')
    actual = next(line.removeprefix('WorkingDirectory=') for line in text.splitlines() if line.startswith('WorkingDirectory='))
    assert actual == directory.replace('%', '%%')


@pytest.mark.parametrize('separator', ['\r', '\n', '\0'])
def test_working_directory_rejects_line_or_nul_injection(separator):
    with pytest.raises(ValueError):
        gateway_service._format_systemd_unit_section('/bin/true', '/tmp/root'+separator+'injected', '/usr/bin')


# LLM: 仅替换真实模块的系统读取边界，不读真实proc、不执行ps；其余路径读者保持原样。
# 函数用途: 为进程出生解析回归提供可控stat原文与ps回退结果。
@pytest.fixture
def proc_stat_reader(monkeypatch):
    from agent_py_agent.agent.gateway_parts import daemon_metadata as dm

    reader = SimpleNamespace(module=dm, raw='', fallback='', ps_calls=[], read_paths=[])
    original_read = dm.Path.read_text

    # LLM: 只截获合成PID的stat路径，避免影响pytest和产品其它文件读取。
    # 函数用途: 将合成stat原文交给真实出生时间解析器。
    def read_stat(path, *args, **kwargs):
        if str(path) == '/proc/12345/stat':
            reader.read_paths.append(str(path))
            if isinstance(reader.raw, Exception):
                raise reader.raw
            return reader.raw
        return original_read(path, *args, **kwargs)

    # LLM: ps回退只记录调用并返回合成stdout或异常，禁止实际创建子进程。
    # 函数用途: 模拟现有ps回退的成功、空值与失败边界。
    def run_ps(*args, **kwargs):
        reader.ps_calls.append((args, kwargs))
        if isinstance(reader.fallback, Exception):
            raise reader.fallback
        return SimpleNamespace(stdout=reader.fallback)

    monkeypatch.setattr(dm.sys, 'platform', 'linux')
    monkeypatch.setattr(dm.Path, 'read_text', read_stat)
    monkeypatch.setattr(dm.subprocess, 'run', run_ps)
    return reader


# LLM: comm内空白与括号都是合法名称内容，必须从最后右括号后按内核字段定位，不能全行split。
# 函数用途: 确认真实模块在复杂进程名下仍返回第22字段，且不误走ps。
@pytest.mark.parametrize('comm', ['worker pool', 'worker (nested) pool', 'worker) pool(', 'worker(nested)'])
def test_proc_stat_start_time_uses_last_comm_parenthesis(proc_stat_reader, comm):
    reader = proc_stat_reader
    fields = ['S', *map(str, range(4, 22)), '424242', '987654']
    reader.raw = f"12345 ({comm}) {' '.join(fields)}\n"
    reader.fallback = 'must not replace valid proc ticks'
    assert reader.module._get_process_start_time(12345) == 424242
    assert reader.read_paths == ['/proc/12345/stat']
    assert reader.ps_calls == []


# LLM: 无右括号不能靠split恰好读出数字；短字段和坏整数也必须沿原ps回退或保持未知。
# 函数用途: 核对损坏stat时ps回退的返回类型、空值和异常处理保持原合同。
@pytest.mark.parametrize('fault', ['missing_parenthesis', 'short_fields', 'bad_integer', 'missing_file', 'unreadable'])
@pytest.mark.parametrize('fallback_kind', ['value', 'empty', 'error'])
def test_proc_stat_invalid_input_preserves_ps_fallback(proc_stat_reader, fault, fallback_kind):
    reader = proc_stat_reader
    fields = ['S', *map(str, range(4, 22)), '424242', '987654']
    if fault == 'missing_parenthesis':
        reader.raw = f"12345 (unterminated {' '.join(fields)}"
    elif fault == 'short_fields':
        reader.raw = '12345 (worker) S 1 2'
    elif fault == 'bad_integer':
        fields[19] = 'not-an-integer'
        reader.raw = f"12345 (worker) {' '.join(fields)}"
    else:
        reader.raw = FileNotFoundError('synthetic stat missing') if fault == 'missing_file' else PermissionError('synthetic stat denied')
    reader.fallback = '  Mon Oct 19 12:34:56 2026\n' if fallback_kind == 'value' else OSError('synthetic ps failure') if fallback_kind == 'error' else ' \n'
    expected = 'Mon Oct 19 12:34:56 2026' if fallback_kind == 'value' else None
    assert reader.module._get_process_start_time(12345) == expected
    assert reader.ps_calls == [
        ((['ps', '-o', 'lstart=', '-p', '12345'],), {'capture_output': True, 'text': True, 'timeout': 5})
    ]


# LLM: 身份核验仍调用真实解析器；仅以假host和假kill边界模拟存活，不向任何真实PID发信号。
# 函数用途: 确认复杂comm解析修复不改变同代真、异代假、无法观察为未知的三态语义。
@pytest.mark.parametrize(('recorded', 'ticks', 'expected'), [(424242, '424242', True), (424241, '424242', False), (424242, 'bad', None)])
def test_proc_stat_identity_keeps_three_state_semantics(proc_stat_reader, monkeypatch, recorded, ticks, expected):
    reader = proc_stat_reader
    dm = reader.module
    fields = ['S', *map(str, range(4, 22)), ticks, '987654']
    reader.raw = f"12345 (worker (nested) pool) {' '.join(fields)}"
    signals = []
    monkeypatch.setattr(dm, 'process_host_id', lambda: 'synthetic-host')
    monkeypatch.setattr(dm.os, 'kill', lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(dm, '_process_start_observation', lambda pid, recorded_start=None: dm._get_process_start_time(pid))
    assert dm.process_identity_is_live({'host_id': 'synthetic-host', 'pid': 12345, 'start_time': recorded}) is expected
    assert signals == [(12345, 0)]
