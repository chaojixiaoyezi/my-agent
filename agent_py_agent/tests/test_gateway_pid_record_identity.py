"""PID文件消费沿生产者出生来源；只用tmp文件及合成OS观测，不启动服务。"""
from __future__ import annotations

import errno
import json
import os
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common import executor_registration
from agent_py_agent.agent.gateway_parts import daemon_control as dc
from agent_py_agent.agent.gateway_parts import daemon_metadata as metadata
from agent_py_agent.agent.gateway_parts import multiprocess_executor_identity as native

PID = 43210
KERNEL_BIRTH = '1792436512:569397'
PS_BIRTH = 'Mon Oct 19 12:01:52 2026'
HOST = 'synthetic-local-host'


# LLM: 只替换无副作用OS观测；真实write_pid_record/读取器/原私有原子写仍执行，避免镜像生产判据。
# 函数用途: 固定本轮kernel/PS/host/liveness输入，不触碰宿主进程或真实home。
@pytest.fixture
def pid_case(tmp_path, monkeypatch):
    state = {'managed': True, 'kernel': KERNEL_BIRTH, 'ps': PS_BIRTH, 'alive': True, 'error': None}
    monkeypatch.setattr(executor_registration, 'managed_request_execution_required', lambda: state['managed'])
    monkeypatch.setattr(metadata, 'sys', SimpleNamespace(platform='darwin', argv=['synthetic-gateway']))
    monkeypatch.setattr(metadata, 'process_host_id', lambda: HOST)
    monkeypatch.setattr(dc, 'process_host_id', lambda: HOST, raising=False)
    monkeypatch.setattr(metadata, 'os', SimpleNamespace(**{**vars(os), 'getpid': lambda: PID}))
    monkeypatch.setattr(native, 'process_birth', lambda _pid: state['kernel'])
    monkeypatch.setattr(metadata, '_get_process_start_time', lambda _pid: state['ps'])
    monkeypatch.setattr(dc, '_get_process_start_time', lambda _pid: state['ps'])
    monkeypatch.setattr(dc, '_is_pid_alive', lambda _pid: state['alive'])

    def probe(_pid, signal):
        assert _pid == PID and signal == 0
        if state['error'] is not None:
            raise OSError(state['error'], 'synthetic probe')

    # dc中仅getpid/kill/name被用到；不替换全局os.name，不让fake Windows污染pytest路径类型。
    monkeypatch.setattr(dc, 'os', SimpleNamespace(name='posix', kill=probe, getpid=lambda: PID))
    return tmp_path / 'gateway.pid', state


@pytest.mark.parametrize('cleanup', [False, True])
@pytest.mark.parametrize('managed_reader', [False, True])
def test_managed_darwin_writer_record_survives_same_kernel_thin_reader(pid_case, cleanup, managed_reader):
    path, state = pid_case
    dc.write_pid_record(path)
    before = path.read_bytes()
    assert json.loads(before)['start_time'] == KERNEL_BIRTH
    state['managed'] = managed_reader
    report = dc.get_running_pid_report(path, cleanup_stale=cleanup)
    assert report.pid == PID and report.load_error is None
    assert path.read_bytes() == before


@pytest.mark.parametrize('cleanup', [False, True])
@pytest.mark.parametrize('birth', ['1792436513:569397', None])
def test_same_source_reuse_or_unreadable_birth_has_distinct_cleanup(pid_case, cleanup, birth):
    path, state = pid_case
    dc.write_pid_record(path)
    before = path.read_bytes()
    state['managed'] = False
    state['kernel'] = birth
    report = dc.get_running_pid_report(path, cleanup_stale=cleanup)
    assert report.load_error is None
    if birth is None:
        assert report.pid == PID and path.read_bytes() == before
    else:
        assert report.pid is None and path.exists() is (not cleanup)
        if not cleanup:
            assert path.read_bytes() == before


@pytest.mark.parametrize('managed', [False, True])
def test_dot_fingerprint_is_not_normalized_into_managed_colon_identity(pid_case, managed):
    path, state = pid_case
    path.write_text(json.dumps({'pid': PID, 'host_id': HOST, 'start_time': '1792436512.569397'}))
    before = path.read_bytes()
    state['managed'] = managed
    report = dc.get_running_pid_report(path)
    # 两来源不可比保原活PID发现；这不把dot等同colon，也不授同代stop身份。
    assert report.pid == PID and report.load_error is None
    assert path.read_bytes() == before
    assert metadata.process_identity_is_live({'pid': PID, 'host_id': 'foreign', 'start_time': KERNEL_BIRTH}) is None


@pytest.mark.parametrize('platform,managed,birth', [
    ('darwin', False, PS_BIRTH), ('linux', False, 123456), ('linux', True, 123456),
    ('linux', False, '123456'), ('linux', True, '123456'), ('win32', False, None),
])
def test_legacy_platform_birth_discovery_is_preserved(pid_case, monkeypatch, platform, managed, birth):
    path, state = pid_case
    monkeypatch.setattr(metadata, 'sys', SimpleNamespace(platform=platform, argv=['synthetic-gateway']))
    if platform == 'win32':
        monkeypatch.setattr(dc.os, 'name', 'nt')
    state.update(managed=managed, ps=None if platform == 'win32' else PS_BIRTH if platform == 'darwin' else 123456, kernel=123456)
    path.write_text(json.dumps({'pid': PID, 'start_time': birth}))
    before = path.read_bytes()
    report = dc.get_running_pid_report(path)
    assert report.pid == PID and report.load_error is None
    assert path.read_bytes() == before


def test_legacy_missing_birth_keeps_live_service_discovery(pid_case):
    path, state = pid_case
    path.write_text(json.dumps({'pid': PID}))
    before = path.read_bytes()
    state['managed'] = False
    assert dc.get_running_pid(path) == PID
    assert path.read_bytes() == before


@pytest.mark.parametrize('cleanup', [False, True])
@pytest.mark.parametrize('error', [errno.EPERM, errno.EIO])
def test_unverifiable_posix_liveness_preserves_record_and_blocks_start(pid_case, cleanup, error):
    path, state = pid_case
    dc.write_pid_record(path)
    before = path.read_bytes()
    state.update(alive=False, error=error)
    report = dc.get_running_pid_report(path, cleanup_stale=cleanup)
    assert report.pid is None
    assert report.load_error == {'context': 'gateway.pid_identity.read',
        'error_code': 'GATEWAY_PID_IDENTITY_UNVERIFIABLE', 'reason': 'liveness_unverifiable'}
    assert path.read_bytes() == before
    with pytest.raises(RuntimeError, match='无法核验原 Gateway 进程身份'):
        dc.get_running_pid(path, cleanup_stale=cleanup)
    assert path.read_bytes() == before


@pytest.mark.parametrize('cleanup', [False, True])
def test_only_confirmed_esrch_cleans_dead_posix_record(pid_case, cleanup):
    path, state = pid_case
    dc.write_pid_record(path)
    before = path.read_bytes()
    state.update(alive=False, error=errno.ESRCH)
    report = dc.get_running_pid_report(path, cleanup_stale=cleanup)
    assert report.pid is None and report.load_error is None
    assert path.exists() is (not cleanup)
    if not cleanup:
        assert path.read_bytes() == before


def test_transient_negative_bool_probe_does_not_erase_confirmed_live_pid(pid_case):
    path, state = pid_case
    dc.write_pid_record(path)
    before = path.read_bytes()
    state['alive'] = False
    assert dc.get_running_pid(path) == PID
    assert path.read_bytes() == before


@pytest.mark.parametrize('cleanup', [False, True])
def test_foreign_host_record_cannot_be_adopted_or_removed(pid_case, cleanup):
    path, _state = pid_case
    path.write_text(json.dumps({'pid': PID, 'host_id': 'synthetic-foreign-host', 'start_time': KERNEL_BIRTH}))
    before = path.read_bytes()
    report = dc.get_running_pid_report(path, cleanup_stale=cleanup)
    assert report.pid is None and report.load_error['reason'] == 'foreign_host'
    assert report.load_error['error_code'] == 'GATEWAY_PID_IDENTITY_UNVERIFIABLE'
    with pytest.raises(RuntimeError, match='勿重复启动'):
        dc.get_running_pid(path, cleanup_stale=cleanup)
    assert path.read_bytes() == before


@pytest.mark.parametrize('failure', ['json', 'read'])
def test_original_record_read_errors_are_preserved_without_new_raise(pid_case, monkeypatch, failure):
    path, _state = pid_case
    path.write_text('{bad json' if failure == 'json' else '{}')
    before = path.read_bytes()
    if failure == 'read':
        original_read = Path.read_text

        def unreadable(target, *args, **kwargs):
            if target == path:
                raise PermissionError('synthetic read')
            return original_read(target, *args, **kwargs)

        monkeypatch.setattr(Path, 'read_text', unreadable)
    report = dc.get_running_pid_report(path)
    assert report.pid is None and report.load_error['context'] == 'gateway.pid_record.read'
    assert dc.get_running_pid(path) is None
    assert path.read_bytes() == before


@pytest.mark.parametrize('caller', ['start', 'count0_setup', 'count2_setup'])
@pytest.mark.parametrize('reason', ['foreign_host', 'liveness_unverifiable'])
def test_original_start_callers_block_new_identity_unknown_before_business_effects(tmp_path, monkeypatch, caller, reason):
    from agent_py_agent.cli import gateway_control_plane as control
    from agent_py_agent.cli import gateway_coordinated_run as coordinated
    from agent_py_agent.cli import gateway_process as gateway

    path = tmp_path / 'gateway.pid'
    path.write_text(json.dumps({'pid': PID, 'host_id': HOST, 'start_time': KERNEL_BIRTH}))
    before = path.read_bytes()
    error = {'context': 'gateway.pid_identity.read', 'error_code': 'GATEWAY_PID_IDENTITY_UNVERIFIABLE', 'reason': reason}
    monkeypatch.setattr(dc, 'get_running_pid_report', lambda *_args, **_kwargs: dc.RunningPidReport(None, error))
    effects = []

    def forbidden(*_args, **_kwargs):
        effects.append('runtime_effect')
        pytest.fail('unknown identity reached runtime write/spawn')

    paths = SimpleNamespace(root=tmp_path/'gateway', pid=path, state=tmp_path/'state.json')
    monkeypatch.setattr(gateway, '_spawn_gateway_process', forbidden)
    monkeypatch.setattr(gateway, '_write_gateway_start_files', forbidden)
    monkeypatch.setattr(gateway, '_clear_gateway_stop_request', forbidden)
    monkeypatch.setattr(gateway, 'write_pid_record', forbidden)
    monkeypatch.setattr(control, 'write_pid_record', forbidden)
    monkeypatch.setattr(coordinated, 'publish_coordinated_state', forbidden)
    if caller == 'start':
        config_path = tmp_path / 'gateway-start.yaml'
        config_path.write_text('model_backend: echo\ngateway_worker_process_count: 0\n'
                               + f'my_agent_home: {tmp_path / "config-home"}\n', encoding='utf-8')
        monkeypatch.setattr(gateway, 'make_agent', lambda _args: SimpleNamespace())
        monkeypatch.setattr(gateway, 'gateway_paths', lambda _agent: paths)
        invoke = partial(gateway.cmd_gateway_start, SimpleNamespace(config=str(config_path), force=False))
    elif caller == 'count0_setup':
        invoke = partial(gateway._cmd_gateway_run_setup, SimpleNamespace(), paths)
    else:
        monkeypatch.setattr(coordinated, 'coordinated_state_path', lambda _context: paths.state)
        invoke = partial(control.setup_control_run, SimpleNamespace(paths=paths))
    with pytest.raises(RuntimeError, match='勿重复启动'):
        invoke()
    assert effects == [] and path.read_bytes() == before
    # start继承原exist_ok根目录准备；两个setup在拒绝前仍无mkdir，不能宣称三者均零mkdir。
    assert paths.root.exists() is (caller == 'start')


def test_real_readiness_consumes_same_managed_writer_record(pid_case):
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.gateway_parts.status_rendering import gateway_readiness

    path, state = pid_case
    paths = gateway_paths_from_root(path.parent)
    dc.write_pid_record(paths.pid)
    before = paths.pid.read_bytes()
    paths.state.write_text(json.dumps({'pid': PID, 'status': 'running', 'started_at': 2000000000.0}))
    state['managed'] = False
    ready = gateway_readiness(paths)
    assert ready.ready is True and ready.process_alive is True and ready.pid == PID
    assert paths.pid.read_bytes() == before


def test_windows_uses_original_liveness_probe_without_posix_signal(pid_case, monkeypatch):
    path, state = pid_case
    path.write_text(json.dumps({'pid': PID, 'start_time': None}))
    monkeypatch.setattr(dc.os, 'name', 'nt')
    monkeypatch.setattr(dc.os, 'kill', lambda *_args: pytest.fail('Windows must not use os.kill(pid,0)'))
    state['alive'] = False
    report = dc.get_running_pid_report(path)
    assert report.pid is None and report.load_error is None
    assert not path.exists()


@pytest.mark.parametrize('cleanup', [False, True])
@pytest.mark.parametrize('managed', [False, True])
@pytest.mark.parametrize('unknown', [
    '1792436512:56939', '1792436512:5693970', '1792436512:569397 suffix',
    'opaque-birth-format', '未知格式的出生指纹',
])
def test_unknown_text_or_noncanonical_kernel_never_proves_pid_reuse(pid_case, cleanup, managed, unknown):
    path, state = pid_case
    path.write_text(json.dumps({'pid': PID, 'host_id': HOST, 'start_time': unknown}))
    before = path.read_bytes()
    state['managed'] = managed
    report = dc.get_running_pid_report(path, cleanup_stale=cleanup)
    assert report.pid == PID and report.load_error is None
    assert path.read_bytes() == before


@pytest.mark.parametrize('cleanup', [False, True])
@pytest.mark.parametrize('same', [False, True])
def test_legacy_ps_reuse_requires_two_valid_ps_fingerprints(pid_case, cleanup, same):
    path, state = pid_case
    state['managed'] = False
    path.write_text(json.dumps({'pid': PID, 'start_time': PS_BIRTH}))
    before = path.read_bytes()
    state['ps'] = PS_BIRTH if same else 'Tue Oct 20 12:01:52 2026'
    report = dc.get_running_pid_report(path, cleanup_stale=cleanup)
    assert report.load_error is None
    assert report.pid == (PID if same else None)
    assert path.exists() is (same or not cleanup)
    if path.exists():
        assert path.read_bytes() == before


@pytest.mark.parametrize('cleanup', [False, True])
def test_locale_unparseable_ps_text_is_unknown_not_dead(pid_case, monkeypatch, cleanup):
    path, state = pid_case
    state.update(managed=False, ps='Tue Oct 20 12:01:52 2026')
    path.write_text(json.dumps({'pid': PID, 'start_time': PS_BIRTH}))
    before = path.read_bytes()

    def different_locale(_value, _format):
        raise ValueError('synthetic unsupported locale')

    monkeypatch.setattr(dc, 'time', SimpleNamespace(strptime=different_locale))
    report = dc.get_running_pid_report(path, cleanup_stale=cleanup)
    assert report.pid == PID and report.load_error is None
    assert path.read_bytes() == before


@pytest.mark.parametrize('cleanup', [False, True])
def test_noncanonical_kernel_observation_cannot_erase_original_record(pid_case, cleanup):
    path, state = pid_case
    dc.write_pid_record(path)
    before = path.read_bytes()
    state['kernel'] = 'future-observation-format'
    report = dc.get_running_pid_report(path, cleanup_stale=cleanup)
    assert report.pid == PID and report.load_error is None
    assert path.read_bytes() == before


@pytest.mark.parametrize('cleanup', [False, True])
@pytest.mark.parametrize('recorded', ['12345', 12345, 12345.0])
@pytest.mark.parametrize('current,same', [('12345', True), ('99999', False)])
def test_numeric_birth_kind_keeps_original_shared_relation(pid_case, monkeypatch, cleanup, recorded, current, same):
    path, state = pid_case
    monkeypatch.setattr(metadata, 'sys', SimpleNamespace(platform='linux', argv=['synthetic-gateway']))
    state.update(managed=False, ps=current, kernel=current)
    path.write_text(json.dumps({'pid': PID, 'start_time': recorded}))
    before = path.read_bytes()
    report = dc.get_running_pid_report(path, cleanup_stale=cleanup)
    assert report.load_error is None
    assert report.pid == (PID if same else None)
    assert path.exists() is (same or not cleanup)
    if path.exists():
        assert path.read_bytes() == before
