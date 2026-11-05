"""W11：只在临时日志和真实子进程中验证FD重开，绝不改pytest进程的stdio。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name == 'nt', reason='daemon stdio rotation is POSIX only')

_SCRIPT = r'''
import json, os, sys, time
from pathlib import Path
from agent_py_agent.agent.observability import log_volume_rotation as rotation
rotation.DAEMON_LOG_VOLUME_MAX_BYTES = 2048
rotation.DAEMON_LOG_TOTAL_MAX_BYTES = 8192
rotation.DAEMON_LOG_CHECK_INTERVAL_SECONDS = 0.05
path = Path(os.environ['MY_AGENT_DAEMON_LOG_PATH'])
mode = os.environ['MODE']
result = {}
if mode == 'stream':
    assert rotation.install_daemon_log_rotation(str(path))
    assert rotation.install_daemon_log_rotation(str(path))
    import threading
    assert sum(t.name == 'daemon-log-rotation' for t in threading.enumerate()) == 1
    for i in range(220):
        print(os.environ.get('WRITER', 'one'), i, 'x' * 72, flush=True)
        time.sleep(0.01)
    time.sleep(0.15)
    print('CURRENT-FINAL', flush=True)
    a, b = os.fstat(1), path.stat()
    result = {'same': (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)}
elif mode == 'pipe':
    result = {'installed': rotation.install_daemon_log_rotation(str(path))}
elif mode == 'missing':
    path.rename(str(path) + '.orphan')
    result = rotation.rotate_log_volumes_once(path, now=time.time())
    print('AFTER-CRASH', flush=True)
elif mode == 'busy':
    import fcntl
    fd = os.open(str(path) + '.rotate.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = rotation.rotate_log_volumes_once(path, now=time.time())
    finally:
        os.close(fd)
elif mode == 'prune':
    result = rotation.rotate_log_volumes_once(path, now=float(os.environ['NOW']))
elif mode == 'partial-reopen':
    path.rename(str(path) + '.orphan')
    original = os.dup2
    def partial(source, target):
        if target == 2:
            raise OSError('synthetic second dup2 failure')
        return original(source, target)
    os.dup2 = partial
    try:
        try:
            rotation.rotate_log_volumes_once(path, now=time.time())
        except OSError:
            pass
    finally:
        os.dup2 = original
    assert rotation._identity(os.fstat(1)) == rotation._identity(path.stat())
    assert rotation._identity(os.fstat(2)) != rotation._identity(path.stat())
    result = rotation.rotate_log_volumes_once(path, now=time.time())
    result['stderr_same'] = rotation._identity(os.fstat(2)) == rotation._identity(path.stat())
    print('RECOVERED-STDERR', file=sys.stderr, flush=True)
elif mode == 'late-install':
    path.rename(str(path) + '.' + str(time.time_ns()))
    path.write_text('new-active\n')
    result = {'installed': rotation.install_daemon_log_rotation(str(path)),
              'same': rotation._identity(os.fstat(1)) == rotation._identity(path.stat())}
Path(os.environ['RESULT']).write_text(json.dumps(result))
'''


# LLM: 仅传临时日志、结果和故障场景，不覆盖外部生产配置。
# 函数用途: 构造真实子进程的测试环境。
def _env(log, result, mode, **extra):
    return {**os.environ, 'MY_AGENT_DAEMON_LOG_PATH': str(log), 'RESULT': str(result), 'MODE': mode, **extra}


# LLM: stdout/stderr只重定向新子进程，绝不dup2测试主进程。
# 函数用途: 启动持有真实追加FD的日志写者。
def _start(log, result, mode='stream', **extra):
    with log.open('ab') as handle:
        return subprocess.Popen([sys.executable, '-c', _SCRIPT], stdout=handle,
                                stderr=subprocess.STDOUT, env=_env(log, result, mode, **extra))


# LLM: 必须核对真实退出码；失败展示活动日志，超时后清理测试子进程。
# 函数用途: 等待并核验日志场景结束。
def _finish(child, log):
    try:
        assert child.wait(timeout=15) == 0, log.read_text()
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)


# LLM: 仅测试计量用，排除永久锁及其它非时间戳后缀。
# 函数用途: 列举当前存留的测试封卷。
def _sealed(log):
    return sorted(path for path in log.parent.glob(log.name + '.*') if path.name.rsplit('.', 1)[-1].isdigit())


# LLM: 同一实际PID持续写入，检查旧卷停止增长及最终FD身份，不以rename存在代替重开。
# 函数用途: 验证无需重启的连续日志轮转。
def test_live_child_rotates_without_restart_and_old_volumes_stop_growing(tmp_path):
    log, result = tmp_path / 'daemon.log', tmp_path / 'result.json'
    child = _start(log, result)
    try:
        deadline = time.monotonic() + 10
        while len(_sealed(log)) < 2 and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(_sealed(log)) >= 2, log.read_text()
        time.sleep(0.15)
        sizes = {path: path.stat().st_size for path in _sealed(log)
                 if int(path.name.rsplit('.', 1)[-1]) / 1e9 < time.time() - 0.1}
        assert sizes
        time.sleep(0.2)
        assert all(not path.exists() or path.stat().st_size == size for path, size in sizes.items())
        _finish(child, log)
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)
    assert json.loads(result.read_text()) == {'same': True}
    assert 'CURRENT-FINAL' in log.read_text()
    assert sum(path.stat().st_size for path in [log, *_sealed(log)]) <= 8192 + 4 * 2048
    assert (tmp_path / 'daemon.log.rotate.lock').exists()


# LLM: 两个独立实际进程继承同一活动路径，结束时均须回到当前inode。
# 函数用途: 验证共享日志写者都会重开。
def test_two_writers_both_reopen_shared_log(tmp_path):
    log = tmp_path / 'shared.log'
    outputs = [tmp_path / f'result-{i}.json' for i in range(2)]
    children = [_start(log, result, WRITER=str(i)) for i, result in enumerate(outputs)]
    try:
        for child in children:
            _finish(child, log)
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
    assert len(_sealed(log)) >= 2
    assert all(json.loads(path.read_text())['same'] for path in outputs)
    assert sum(path.stat().st_size for path in [log, *_sealed(log)]) <= 8192 + 4 * 2048


# LLM: stdout是实际管道，不允许安装或增加锁、改日志内容。
# 函数用途: 验证终端类前台入口不会被误重定向。
def test_non_daemon_pipe_is_noop(tmp_path):
    log, result = tmp_path / 'daemon.log', tmp_path / 'result.json'
    log.write_text('unchanged')
    process = subprocess.run([sys.executable, '-c', _SCRIPT], capture_output=True,
                             env=_env(log, result, 'pipe'), timeout=10)
    assert process.returncode == 0, process.stderr
    assert json.loads(result.read_text()) == {'installed': False}
    assert log.read_text() == 'unchanged'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['daemon.log', 'result.json']


# LLM: 同时预置到期和宽限卷，核对真实删除字节及锁权限。
# 函数用途: 验证退休口径与至少两个间隔的重开宽限。
def test_recent_volume_grace_and_exact_ttl_pruning(tmp_path):
    log, result = tmp_path / 'daemon.log', tmp_path / 'result.json'
    now = time.time()
    log.write_bytes(b'active')
    ancient = tmp_path / f'daemon.log.{int((now - 31 * 86400) * 1e9):019d}'
    recent = tmp_path / f'daemon.log.{int((now - 0.02) * 1e9):019d}'
    ancient.write_bytes(b'a' * 9000)
    recent.write_bytes(b'r' * 9000)
    _finish(_start(log, result, 'prune', NOW=str(now)), log)
    report = json.loads(result.read_text())
    assert not ancient.exists() and recent.exists()
    assert report == {'rotated': False, 'reopened': False, 'deleted': 1,
                      'freed_bytes': 9000, 'total_bytes': 9006}
    assert log.read_bytes() == b'active'
    assert (tmp_path / 'daemon.log.rotate.lock').stat().st_mode & 0o777 == 0o600


# LLM: 使用真实flock占用永久锁，忙时不得rename或删除任何日志。
# 函数用途: 验证非阻塞轮转避让其它写者。
def test_busy_rotation_lock_skips_pass(tmp_path):
    log, result = tmp_path / 'daemon.log', tmp_path / 'result.json'
    log.write_bytes(b'x' * 4096)
    lock = tmp_path / 'daemon.log.rotate.lock'
    _finish(_start(log, result, 'busy'), log)
    assert json.loads(result.read_text()) == {'rotated': False, 'reopened': False,
                                             'deleted': 0, 'freed_bytes': 0, 'total_bytes': 0}
    assert log.stat().st_size == 4096 and not _sealed(log) and lock.exists()


# LLM: 在子进程中制造rename后缺失路径，核对新旧文件实际内容。
# 函数用途: 验证崩溃中断后的下次检查会补建并重开。
def test_missing_path_after_rename_reopens_and_writes(tmp_path):
    log, result = tmp_path / 'daemon.log', tmp_path / 'result.json'
    log.write_text('before crash')
    _finish(_start(log, result, 'missing'), log)
    report = json.loads(result.read_text())
    assert report['rotated'] is False and report['reopened'] is True
    assert log.read_text() == 'AFTER-CRASH\n'
    assert (tmp_path / 'daemon.log.orphan').read_text() == 'before crash'


# LLM: 故障与dup2都只在真实子进程中，首次失败不允许用外层成功伪装，下一拍须恢复两条流。
# 函数用途: 验证stdout已切换而stderr尚未切换时仍能重试恢复。
def test_partial_dup2_failure_recovers_stderr_on_next_pass(tmp_path):
    log, result = tmp_path / 'daemon.log', tmp_path / 'result.json'
    log.write_text('before crash')
    _finish(_start(log, result, 'partial-reopen'), log)
    report = json.loads(result.read_text())
    assert report['reopened'] and report['stderr_same']
    assert log.read_text() == 'RECOVERED-STDERR\n'
    assert (tmp_path / 'daemon.log.orphan').read_text() == 'before crash'


# LLM: 这是冻结身份门禁的已知限制证据，不把False解释成该写者已受轮转保护。
# 函数用途: 记录安装前已换卷的写者会被严格门禁拒绝，交由集成者修订合同。
def test_frozen_install_gate_rejects_pre_rotated_writer(tmp_path):
    log, result = tmp_path / 'daemon.log', tmp_path / 'result.json'
    _finish(_start(log, result, 'late-install'), log)
    assert json.loads(result.read_text()) == {'installed': False, 'same': False}
    assert log.read_text() == 'new-active\n'


@pytest.mark.parametrize('site', ['gateway', 'gateway-supervisor', 'adapter', 'supervisor'])
# LLM: 捕获四个真实生产函数的Popen参数，不启动服务，逐个对照stdout目标。
# 函数用途: 验证daemon环境变量指向真正的日志文件。
def test_spawners_export_the_actual_stdout_target(tmp_path, monkeypatch, site):
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts import supervisor as service_supervisor
    from agent_py_agent.cli import adapter_daemon, gateway_process, supervisor

    calls = []
    def capture(command, **kwargs):
        calls.append((command, kwargs))
        assert kwargs['stdout'].name == str(tmp_path / 'gateway.log')
        return SimpleNamespace(pid=123)

    monkeypatch.setattr(subprocess, 'Popen', capture)
    paths = SimpleNamespace(root=tmp_path, log=tmp_path / 'gateway.log')
    agent = SimpleNamespace(root=tmp_path, home_paths=SimpleNamespace(root=tmp_path),
                            config=SimpleNamespace(config_path=tmp_path / 'config.yaml'))
    if site == 'gateway':
        gateway_process._spawn_gateway_process(paths, ['python'], cwd=tmp_path)
    elif site == 'gateway-supervisor':
        service_supervisor._spawn_gateway_process(SimpleNamespace(
            _paths=paths, _agent=agent, config_path=agent.config.config_path), ['python'], 0, True)
    elif site == 'adapter':
        adapter_daemon._start_adapter_daemon_process(agent, paths, SimpleNamespace(channel='synthetic'))
    else:
        supervisor._spawn_daemon(['python'], paths.log)
    assert len(calls) == 1
    assert calls[0][1]['env']['MY_AGENT_DAEMON_LOG_PATH'] == str(paths.log)
    assert calls[0][1]['stderr'] == subprocess.STDOUT


# LLM: 在进业务前的安装点抛出专用哨兵，不启动任何真实Gateway/Adapter服务或改FD。
# 函数用途: 验证四个生产子入口实际消费继承的日志变量，而不是只检查源码字样。
@pytest.mark.parametrize('site', ['gateway', 'adapter', 'supervisor', 'worker'])
def test_child_entry_invokes_installer_before_business(tmp_path, monkeypatch, site):
    from types import SimpleNamespace

    from agent_py_agent.agent.observability import log_volume_rotation as rotation
    from agent_py_agent.cli import adapter, common, gateway_process, gateway_worker, supervisor

    class Installed(Exception):
        pass

    seen = []
    def capture(path):
        seen.append(path)
        raise Installed()

    monkeypatch.setenv('MY_AGENT_DAEMON_LOG_PATH', str(tmp_path / 'daemon.log'))
    monkeypatch.setattr(rotation, 'install_daemon_log_rotation', capture)
    with pytest.raises(Installed):
        if site == 'gateway':
            monkeypatch.setattr(gateway_process, '_isolate_gateway_package_path', lambda: None)
            monkeypatch.setattr(gateway_process, 'mark_hosting_gateway_process', lambda: None)
            monkeypatch.setattr(common, 'frozen_gateway_arguments', lambda args: args)
            gateway_process.cmd_gateway_run(SimpleNamespace())
        elif site == 'supervisor':
            supervisor.cmd_supervisor_run(SimpleNamespace())
        elif site == 'adapter':
            agent = SimpleNamespace(root=tmp_path, config=SimpleNamespace(
                adapters=SimpleNamespace(default_channel='synthetic'), config_path=tmp_path / 'config.yaml'))
            monkeypatch.setattr(adapter, 'make_lightweight_cli_agent_context', lambda args, **kwargs: agent)
            monkeypatch.setattr(adapter, '_adapter_options_from_args', lambda args: SimpleNamespace(daemon=False, pid_file=None))
            monkeypatch.setattr(adapter, 'gateway_paths', lambda agent: SimpleNamespace(root=tmp_path, adapter_pid=tmp_path / 'adapter.pid'))
            adapter.cmd_adapter_start(SimpleNamespace(channel='synthetic', daemon=False))
        else:
            monkeypatch.setattr(gateway_worker, '_worker_parser', lambda: SimpleNamespace(parse_args=lambda args: None))
            gateway_worker.dispatch_worker_entry(['gateway', 'worker'])
    assert seen == [str(tmp_path / 'daemon.log')]
