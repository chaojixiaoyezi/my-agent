"""独立进程验证显式内存追踪及原诊断线程，不触及运行中的Gateway。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _child(tmp_path, source, *, external=False):
    home = tmp_path / 'home'
    home.mkdir()
    env = {**os.environ, 'HOME': str(home), 'MY_AGENT_HOME': str(home)}
    command = [sys.executable, '-I']
    if external:
        command += ['-X', 'tracemalloc=1']
    command += ['-c', f'import sys; sys.path.insert(0, {str(ROOT)!r})\n' + textwrap.dedent(source).replace("{{", "{").replace("}}", "}")]
    result = subprocess.run(command, cwd=tmp_path, env=env, text=True, capture_output=True, timeout=40)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_revoke_failure_keeps_stack_port_without_retaining_memory_owner(tmp_path):
    assert _child(tmp_path, '''
        import gc,io,json,signal,threading,time,weakref
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch
        from agent_py_agent.cli import gateway_process as gp
        called=threading.Event()
        class Reporter:
            def __call__(self):
                called.set()
                return {'completed':True}
        reporter=Reporter()
        reference=weakref.ref(reporter)
        output=io.StringIO()
        paths=SimpleNamespace(root=Path.cwd(),state=Path.cwd()/'state.json')
        identity={'pid':123}
        previous=gp._GatewaySignalHandlers(paths,identity)
        previous.diagnostic_published=True
        dumper=gp._GatewayStackDumper()
        previous.stack_dumper=dumper
        dumper.memory_report=reporter
        with patch.object(gp.sys,'stderr',output),patch.object(gp.hang_diagnostics,'close'):
            dumper.thread.start()
            try:
                dumper.pending.set()
                assert called.wait(3)
                deadline=time.monotonic()+3
                while 'Gateway SIGUSR1 memory ' not in output.getvalue():
                    assert time.monotonic()<deadline
                    time.sleep(.01)
                with patch.object(gp,'_publish_gateway_diagnostic_signal',return_value=False):
                    gp._restore_gateway_signal_handlers(previous)
                assert dumper.thread.is_alive()
                assert dumper.memory_report is None
                del reporter
                gc.collect()
                assert reference() is None
            finally:
                dumper.close()
        print(json.dumps({'ok':True}))
    ''') == {'ok': True}


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('external', [False, True])
@pytest.mark.parametrize('fail', [False, True])
def test_cmd_gateway_trace_starts_before_agent_and_releases_only_owned_trace(tmp_path, enabled, external, fail):
    result = _child(tmp_path, f'''
        import tracemalloc
        from contextlib import ExitStack, nullcontext
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch
        import json
        from agent_py_agent.cli import gateway_process as gp
        from agent_py_agent.cli import common, gateway_coordinated_run as coordinated
        from agent_py_agent.agent.gateway_parts import standalone_lifecycle as lifecycle
        from agent_py_agent.agent.gateway_parts import multiprocess_generation_retirement as retirement
        from agent_py_agent.agent.gateway_parts import standalone_sources as sources
        from agent_py_agent.agent.observability import log_volume_rotation as rotation
        args = SimpleNamespace(_gateway_worker_count=0, after_pid=0,
            _gateway_frozen_config=SimpleNamespace(enable_gateway_memory_diagnostics={enabled!r}))
        observed = {{'start_calls':0,'stop_calls':0}}
        real_start,real_stop=tracemalloc.start,tracemalloc.stop
        def start(*a,**k):
            observed['start_calls']+=1
            return real_start(*a,**k)
        def stop():
            observed['stop_calls']+=1
            return real_stop()
        tracemalloc.start,tracemalloc.stop=start,stop
        class ExpectedFailure(Exception): pass
        def make_agent(arguments):
            observed['tracing_at_agent'] = tracemalloc.is_tracing()
            marker = bytearray(4096)
            observed['marker_tracked'] = tracemalloc.get_object_traceback(marker) is not None
            observed['metadata'] = arguments._gateway_memory_trace
            if {fail!r}: raise ExpectedFailure()
            return marker
        real_run = gp._run_standalone_gateway
        def run(arguments, guard):
            if {fail!r}: return real_run(arguments, guard)
            # 保留真实cmd入口的追踪上下文，只替换正式服务的外部生命周期边界。
            gp.make_agent(arguments)
            return 37
        with ExitStack() as stack:
            replacements = [(common,'frozen_gateway_arguments',lambda value:value),
                (gp,'mark_hosting_gateway_process',lambda:None),
                (rotation,'install_daemon_log_rotation',lambda *a:None),
                (gp,'wait_for_predecessor_exit',lambda *a:True),
                (coordinated,'_cold_gateway_paths',lambda *a:SimpleNamespace(root=Path.cwd())),
                (lifecycle,'standalone_lifecycle_guard',lambda *a:nullcontext(object())),
                (retirement,'retire_dead_multiprocess_generations',lambda *a,**k:None),
                (sources,'require_standalone_sources',lambda *a:None),
                (gp,'_heal_runtime_db_before_agent',lambda *a:None),
                (gp,'make_agent',make_agent), (gp,'_run_standalone_gateway',run)]
            for target,name,value in replacements: stack.enter_context(patch.object(target,name,value))
            try:
                assert gp.cmd_gateway_run(args)==37
                assert not {fail!r}
            except ExpectedFailure:
                assert {fail!r}
        observed['tracing_after_exit']=tracemalloc.is_tracing()
        print(json.dumps(observed))
    ''', external=external)
    assert result['tracing_at_agent'] is (enabled or external)
    assert result['marker_tracked'] is (enabled or external)
    assert result['tracing_after_exit'] is external
    assert result['start_calls'] == int(enabled and not external)
    assert result['stop_calls'] == int(enabled and not external)
    if enabled or external:
        assert result['metadata']['tracemalloc_scope'] == ('existing_trace' if external else 'gateway_before_agent')
    else:
        assert result['metadata'] is None


def test_formal_signal_callback_only_marks_existing_dumper_event(tmp_path):
    assert _child(tmp_path, '''
        import json,signal
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch
        from agent_py_agent.cli import gateway_process as gp
        calls=[]
        with patch.object(gp.hang_diagnostics,'install',return_value=True), \
             patch.object(gp.hang_diagnostics,'close'), \
             patch.object(gp,'_publish_gateway_diagnostic_signal',return_value=True), \
             patch.object(gp.threading.Thread,'start'):
            previous=gp._install_gateway_signal_handlers(SimpleNamespace(root=Path.cwd()), {{'pid':123}},
                memory_report=lambda:calls.append('unexpected'))
            try:
                if hasattr(signal,'SIGUSR1'):
                    previous.diagnostic_handler(signal.SIGUSR1,None)
                    assert previous.stack_dumper.pending.is_set()
                else:
                    dumper=gp._GatewayStackDumper()
                    dumper.pending.set()
                    assert dumper.pending.is_set()
                assert calls==[]
            finally: gp._restore_gateway_signal_handlers(previous)
        print(json.dumps({{'ok':True}}))
    ''') == {'ok': True}


def test_real_signal_reports_prior_allocations_and_survives_callback_failure(tmp_path):
    result = _child(tmp_path, '''
        import io,json,os,queue,signal,sys,threading,tracemalloc
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch
        from agent_py_agent.cli import gateway_process as gp
        from agent_py_agent.agent.gateway_parts.background_resource_report import gateway_memory_tracing,collect_runtime_memory_diagnostics
        reports=queue.Queue(); failures=threading.Event(); attempts=[0]
        class Capture(io.StringIO):
            def write(self,text):
                if text.startswith('Gateway SIGUSR1 memory '):
                    reports.put(json.loads(text.split(' memory ',1)[1]))
                return super().write(text)
        output=Capture()
        def report():
            attempts[0]+=1
            if attempts[0]==1: raise ValueError('fixture callback failed')
            return collect_runtime_memory_diagnostics(top_n=20)
        with patch.object(gp.hang_diagnostics,'install',return_value=True), \
             patch.object(gp.hang_diagnostics,'close'), \
             patch.object(gp,'_publish_gateway_diagnostic_signal',return_value=True), \
             patch.object(gp,'_print_gateway_loop_error',side_effect=lambda *a:failures.set()), \
             patch.object(sys,'stderr',output), \
             gateway_memory_tracing(SimpleNamespace(enable_gateway_memory_diagnostics=True)):
            previous=gp._install_gateway_signal_handlers(SimpleNamespace(root=Path.cwd()), {{'pid':os.getpid()}},memory_report=report)
            dumper=previous.stack_dumper
            if dumper is None:
                dumper=gp._GatewayStackDumper();dumper.memory_report=report;dumper.thread.start()
            def trigger():
                if hasattr(signal,'SIGUSR1'): os.kill(os.getpid(),signal.SIGUSR1)
                else: dumper.pending.set()
            try:
                trigger();assert failures.wait(10)
                secret='diagnostic-private-body-do-not-emit'
                first=bytearray(secret.encode()*32768)
                trigger();a=reports.get(timeout=10)
                second=bytearray(secret.encode()*65536)
                trigger();b=reports.get(timeout=10)
                assert dumper.thread.is_alive()
            finally:
                gp._restore_gateway_signal_handlers(previous)
                dumper.close()
        assert any(row['location'].startswith('<string>:') and row['size_bytes'] >= len(first)
            for row in a['tracemalloc_top'])
        assert any(row['location'].startswith('<string>:') and row['size_bytes'] >= len(second)
            for row in b['tracemalloc_top'])
        rendered=json.dumps([a,b])
        assert secret not in rendered
        assert 'bytearray(' not in rendered and str(Path.cwd()) not in rendered
        for report_value in (a,b):
            for row in report_value['tracemalloc_top']:
                assert set(row)=={{'location','size_bytes','block_count'}}
                assert '/' not in row['location'] and '\\\\' not in row['location']
        print(json.dumps({{'first':a['tracemalloc_current_bytes'],'second':b['tracemalloc_current_bytes'],
            'still_tracing':tracemalloc.is_tracing(),'attempts':attempts[0]}}))
    ''')
    assert result['second'] - result['first'] > 1024 * 1024
    assert result['attempts'] == 3
    assert result['still_tracing'] is False


def test_busy_owner_pool_returns_unknown_without_waiting_or_accessing_properties(tmp_path):
    assert _child(tmp_path, '''
        import json,threading
        from agent_py_agent.agent.gateway_parts.background_resource_report import collect_runtime_memory_diagnostics
        class Pool:
            def __init__(self):self._lock=threading.Lock()
            @property
            def hard_count(self):raise AssertionError('business property accessed')
        pool=Pool();entered=threading.Event();release=threading.Event()
        def hold():
            with pool._lock: entered.set();release.wait(10)
        worker=threading.Thread(target=hold);worker.start();assert entered.wait(10)
        try:
            # 原业务锁仍由另一线程持有；collector须立即返回未知而非等待释放。
            result=collect_runtime_memory_diagnostics(owner_pool=pool)
            assert result['owner_pool']=={{}} and not release.is_set()
        finally:release.set();worker.join(10)
        print(json.dumps({{'ok':True}}))
    ''') == {'ok': True}


def test_standalone_service_passes_real_collector_and_start_metadata_to_signal_port(tmp_path):
    assert _child(tmp_path, '''
        import json,os,threading,tracemalloc
        from contextlib import ExitStack
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch
        from agent_py_agent.cli import gateway_process as gp
        from agent_py_agent.agent.gateway_parts.background_resource_report import gateway_memory_tracing
        config=SimpleNamespace(enable_gateway_memory_diagnostics=True,gateway_port=0)
        args=SimpleNamespace()
        class ReachedSignalPort(BaseException):pass
        observed={}
        retained=[]
        def make_agent(arguments):
            assert tracemalloc.is_tracing()
            retained.append(bytearray(4096))
            return SimpleNamespace(config=config,_owner_pool=None)
        def install(paths,identity,**kwargs):
            observed.update(kwargs['memory_report']())
            raise ReachedSignalPort()
        with gateway_memory_tracing(config) as metadata, ExitStack() as stack:
            args._gateway_memory_trace=metadata
            replacements=[('make_agent',make_agent),
                ('gateway_paths',lambda agent:SimpleNamespace(root=Path.cwd())),
                ('_gateway_run_context_from_args',lambda request:object()),
                ('_preflight_required_local_credential',lambda agent:None),
                ('take_restart_marker',lambda paths:None),
                ('_cmd_gateway_run_setup',lambda *a,**k:(0,os.getpid())),
                ('_cmd_gateway_run_threads',lambda *a,**k:(threading.Event(),None,None,None,None)),
                ('_gateway_context_process_identity',lambda context:{'pid':os.getpid()}),
                ('_install_gateway_signal_handlers',install)]
            for name,value in replacements:stack.enter_context(patch.object(gp,name,value))
            try:gp._run_standalone_gateway(args,object())
            except ReachedSignalPort:pass
            else:raise AssertionError('formal signal install port was not reached')
            assert observed['schema']=='gateway-memory.v1'
            assert observed['pid']==os.getpid()
            assert observed['tracemalloc_scope']=='gateway_before_agent'
            assert observed['tracemalloc_started_at']==metadata['tracemalloc_started_at']
            assert observed['tracemalloc_current_bytes'] >= len(retained[0])
        assert not tracemalloc.is_tracing()
        print(json.dumps({'ok':True}))
    ''') == {'ok': True}
