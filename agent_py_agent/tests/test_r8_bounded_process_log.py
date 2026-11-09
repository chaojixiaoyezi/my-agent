"""隔离真实worker：native FD写入有界、父退出独立性、继承输出与计量均可核实。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from agent_py_agent.agent.io.bounded_process_log import (
    BOUNDED_STDIO_LIMIT_ENV,
    BOUNDED_STDIO_PATH_ENV,
    capture_path,
)


def _worker(tmp_path, script, *, limit=4096):
    log = tmp_path / 'worker.log'
    with log.open('wb', buffering=0) as output:
        identity = os.fstat(output.fileno())
        env = {**os.environ, BOUNDED_STDIO_LIMIT_ENV: str(limit), BOUNDED_STDIO_PATH_ENV: str(log)}
        process = subprocess.Popen([sys.executable, '-u', '-c', script], stdout=output, stderr=subprocess.STDOUT, env=env)
    assert process.wait(timeout=15) == 0
    assert (log.stat().st_dev, log.stat().st_ino) == (identity.st_dev, identity.st_ino)
    return log


def test_native_stdout_stderr_cap_is_stable_inode_and_exact_discard_count(tmp_path):
    data = b'actual native stdout\n' * 10_000
    error = b'actual native stderr\n' * 10_000
    script = '''
import os
from agent_py_agent.agent.io.bounded_process_log import bounded_stdio_from_environment
with bounded_stdio_from_environment():
    os.write(1, b'actual native stdout\\n' * 10000)
    os.write(2, b'actual native stderr\\n' * 10000)
'''
    log = _worker(tmp_path, script)
    stats = json.loads(capture_path(log).read_text(encoding="utf-8"))
    assert log.read_bytes() == (data + error)[:4096]
    assert log.stat().st_size == stats['retained_bytes'] == 4096
    assert stats['seen_bytes'] == len(data) + len(error)
    assert stats['discarded_bytes'] == len(data) + len(error) - 4096
    assert stats['eof'] and stats['error_types'] == []


def test_zero_explicitly_disables_collector_without_idle_worker_thread(tmp_path):
    script = '''
import os,threading
from agent_py_agent.agent.io.bounded_process_log import bounded_stdio_from_environment
with bounded_stdio_from_environment():
    assert len(threading.enumerate()) == 1
    os.write(1,b'x'*100000)
'''
    log = _worker(tmp_path, script, limit=0)
    assert log.stat().st_size == 100000
    assert not capture_path(log).exists()


def test_inherited_stdout_is_drained_before_actual_worker_exits(tmp_path):
    script = '''
import subprocess,sys
from agent_py_agent.agent.io.bounded_process_log import bounded_stdio_from_environment
with bounded_stdio_from_environment():
    subprocess.Popen([sys.executable,'-c','import time,os;time.sleep(0.2);os.write(1,b"descendant output")'])
'''
    log = _worker(tmp_path, script)
    assert log.read_bytes() == b'descendant output'
    stats = json.loads(capture_path(log).read_text(encoding="utf-8"))
    assert stats['seen_bytes'] == len(b'descendant output') and stats['eof']


def test_durable_collector_survives_original_launcher_exit(tmp_path):
    log = tmp_path / 'durable.log'
    pidfile = tmp_path / 'child.json'
    child = '''
import os,time
from agent_py_agent.agent.io.bounded_process_log import bounded_stdio_from_environment
with bounded_stdio_from_environment():
    time.sleep(0.2)
    os.write(1,b'after parent exit'*1000)
'''
    launcher = '''
import os,sys,subprocess,json
with open(sys.argv[1],'wb',buffering=0) as out:
    child=subprocess.Popen([sys.executable,'-u','-c',sys.argv[3]],stdout=out,stderr=subprocess.STDOUT,start_new_session=True)
    open(sys.argv[2],'w').write(json.dumps({'pid':child.pid}))
'''
    env = {**os.environ, BOUNDED_STDIO_LIMIT_ENV: '1024', BOUNDED_STDIO_PATH_ENV: str(log)}
    subprocess.run([sys.executable, '-c', launcher, str(log), str(pidfile), child], env=env, check=True, timeout=10)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            stats = json.loads(capture_path(log).read_text(encoding="utf-8"))
            if stats['eof']:
                break
        except (OSError, ValueError):
            pass
        time.sleep(0.02)
    else:
        pytest.fail('durable child did not finish output after its launcher exited')
    assert pidfile.exists() and log.stat().st_size == 1024
    assert stats['seen_bytes'] == len(b'after parent exit') * 1000
    assert stats['discarded_bytes'] == stats['seen_bytes'] - 1024


def test_config_yaml_dataclass_and_registry_use_same_default():
    from agent_py_agent.agent.settings.config import AgentConfig
    from agent_py_agent.agent.settings.parameter_registry import parameter_registry
    from agent_py_agent.agent.settings.user_config_capability import packaged_config_path

    default = AgentConfig().background_dispatch_log_max_bytes
    assert default == 4 * 1024 * 1024
    spec = parameter_registry()['background_dispatch_log_max_bytes']
    assert spec.default == default and spec.description and not spec.writable
    assert f'background_dispatch_log_max_bytes: {default}' in packaged_config_path().read_text(encoding="utf-8")


def test_real_module_entry_wires_capture_before_cli_help_bootstrap(tmp_path):
    script = '''
import runpy,sys
sys.argv=['my-agent','--help']
runpy.run_module('agent_py_agent',run_name='__main__')
'''
    log = _worker(tmp_path, script, limit=1024)
    stats = json.loads(capture_path(log).read_text(encoding="utf-8"))
    assert log.stat().st_size == 1024 and stats['seen_bytes'] > 1024
    assert stats['discarded_bytes'] == stats['seen_bytes'] - 1024 and stats['eof']


def test_tiny_explicit_byte_limit_is_not_silently_raised(tmp_path):
    script = '''
import os
from agent_py_agent.agent.io.bounded_process_log import bounded_stdio_from_environment
with bounded_stdio_from_environment(): os.write(1,b'12345')
'''
    log = _worker(tmp_path, script, limit=1)
    stats = json.loads(capture_path(log).read_text(encoding="utf-8"))
    assert log.read_bytes() == b'1'
    assert stats['retained_bytes'] == 1 and stats['discarded_bytes'] == 4


def test_metadata_publications_are_clock_bounded_but_final_counts_exact(tmp_path, monkeypatch):
    from agent_py_agent.agent.io import bounded_process_log as module

    now = [100.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    published = []
    original = module.write_private_json_file_atomic_unlocked
    def publish(path, value):
        published.append(dict(value))
        original(path, value)
    monkeypatch.setattr(module, 'write_private_json_file_atomic_unlocked', publish)
    path = tmp_path / 'bounded.log'
    capture = module._BoundedLogCapture(path, os.open(path, os.O_CREAT | os.O_WRONLY, 0o600), 4)
    for _ in range(1000):
        capture.record_data('stdout', b'12345678')
    assert len(published) == 1  # 已超容量也不按每块触发元数据fsync。
    now[0] += 1.0
    capture.record_data('stdout', b'abc')
    assert len(published) == 2
    capture.record_error('stdout', OSError())
    assert len(published) == 3
    capture.record_error('stdout', OSError())
    assert len(published) == 3
    capture.record_eof('stdout')
    capture.record_eof('stderr')
    capture.close()
    assert len(published) == 4
    assert published[-1]['eof'] and published[-1]['seen_bytes'] == 8003
    assert published[-1]['retained_bytes'] == 4 and published[-1]['discarded_bytes'] == 7999


def test_positive_limit_above_one_gib_keeps_capture_enabled(tmp_path):
    script = '''
import os
from agent_py_agent.agent.io.bounded_process_log import bounded_stdio_from_environment
with bounded_stdio_from_environment(): os.write(1,b'small output')
'''
    limit = 1024 * 1024 * 1024 + 1
    log = _worker(tmp_path, script, limit=limit)
    stats = json.loads(capture_path(log).read_text(encoding="utf-8"))
    assert stats['max_bytes'] == limit and stats['seen_bytes'] == 12 and stats['eof']
    assert stats['discarded_bytes'] == 0 and log.read_bytes() == b'small output'


def test_pipe_pump_constructor_failure_closes_owned_stream(tmp_path):
    script = '''
import os
from agent_py_agent.agent.io import bounded_process_log as module
streams=[]
original_fdopen=module.os.fdopen
def fdopen(fd,*args,**kwargs):
    stream=original_fdopen(fd,*args,**kwargs)
    if args and args[0]=='rb' and kwargs.get('buffering')==0: streams.append(stream)
    return stream
module.os.fdopen=fdopen
def fail(*args,**kwargs): raise RuntimeError('collector construction failure')
if os.name=='posix': module.ProcessPipePump=fail
else: module.threading.Thread.start=fail
try:
    with module.bounded_stdio_from_environment(): raise AssertionError('must not enter')
except RuntimeError: pass
assert len(streams)==1 and streams[0].closed
os.write(1,b'failure remained visible')
'''
    log = _worker(tmp_path, script)
    assert log.read_bytes() == b'failure remained visible'


_NATIVE_WRITE = '''
import os
if os.name=='nt':
    import ctypes
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GetStdHandle.argtypes=(wintypes.DWORD,)
    kernel.GetStdHandle.restype=wintypes.HANDLE
    kernel.WriteFile.argtypes=(wintypes.HANDLE,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD),ctypes.c_void_p)
    kernel.WriteFile.restype=wintypes.BOOL
    def native(fd,data):
        handle=kernel.GetStdHandle((-11 if fd==1 else -12)&0xffffffff)
        count=wintypes.DWORD()
        buffer=ctypes.create_string_buffer(data)
        if not kernel.WriteFile(handle,buffer,len(data),ctypes.byref(count),None):
            raise ctypes.WinError(ctypes.get_last_error())
        assert count.value==len(data)
else:
    native=os.write
'''


@pytest.mark.parametrize('explicit_inheritance', [False, True])
@pytest.mark.parametrize('blocking_reader', [False, True])
def test_native_standard_handles_and_descendants_share_bounded_output(tmp_path, explicit_inheritance, blocking_reader):
    descendant = _NATIVE_WRITE + "\nimport time;time.sleep(0.1);native(1,b'child-out\\n');native(2,b'child-err\\n')"
    script = _NATIVE_WRITE + f'''
import subprocess,sys
from agent_py_agent.agent.io import bounded_process_log as module
if {blocking_reader!r}: module.ProcessPipePump=lambda *args,**kwargs:None
with module.bounded_stdio_from_environment():
    native(1,b'parent-out\\n')
    native(2,b'parent-err\\n')
    kwargs={{'stdout':sys.stdout,'stderr':subprocess.STDOUT}} if {explicit_inheritance!r} else {{}}
    subprocess.Popen([sys.executable,'-c',{descendant!r}],**kwargs)
'''
    expected = b'parent-out\nparent-err\nchild-out\nchild-err\n'
    log = _worker(tmp_path, script, limit=25)
    stats = json.loads(capture_path(log).read_text(encoding="utf-8"))
    assert log.read_bytes() == expected[:25]
    assert stats['seen_bytes'] == len(expected) and stats['retained_bytes'] == 25
    assert stats['discarded_bytes'] == len(expected) - 25 and stats['eof']
    assert stats['error_types'] == []


def test_reader_thread_start_failure_closes_stream_and_original_sink(tmp_path):
    script = '''
import os
from agent_py_agent.agent.io import bounded_process_log as module
streams=[]
original_fdopen=module.os.fdopen
original_close=module.os.close
closed=[]
def fdopen(fd,*args,**kwargs):
    stream=original_fdopen(fd,*args,**kwargs)
    if args and args[0]=='rb' and kwargs.get('buffering')==0: streams.append(stream)
    return stream
def close(fd):
    closed.append(fd)
    return original_close(fd)
def fail(*args,**kwargs): raise RuntimeError('thread start failure')
module.os.fdopen=fdopen
module.os.close=close
module.threading.Thread.start=fail
try:
    with module.bounded_stdio_from_environment(): raise AssertionError('must not enter')
except RuntimeError: pass
assert len(streams)==1 and streams[0].closed and len(closed)>=3
os.write(1,b'initialization failed visibly')
'''
    log = _worker(tmp_path, script)
    assert log.read_bytes() == b'initialization failed visibly'


def test_native_handle_sync_cleanup_failure_still_releases_pipe_reader(tmp_path):
    script = '''
import os,threading
from agent_py_agent.agent.io import bounded_process_log as module
original=module._redirect_stdio
calls=0
def redirect(fd):
    global calls
    calls+=1
    if calls==2: raise OSError('native standard handle synchronization failure')
    return original(fd)
module._redirect_stdio=redirect
try:
    with module.bounded_stdio_from_environment(): os.write(1,b'actual data')
except OSError: pass
assert len(threading.enumerate())==1
'''
    log = _worker(tmp_path, script)
    stats = json.loads(capture_path(log).read_text(encoding="utf-8"))
    assert log.read_bytes() == b'actual data' and stats['seen_bytes'] == 11 and stats['eof']
