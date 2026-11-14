"""Consolidated tests for shard c3:
- test_r8_gateway_memory_diagnostics.py: 独立进程验证显式内存追踪及原诊断线程，不触及运行中的Gateway。
- test_r8_idle_notice_http.py: 真实Client→HTTP→原通知投影：无hub空页复用不隐藏内存、任务、跨进程写或撤权。
- test_r8_observation_thread_pending.py: 线程待处理观察按活动集读取，不被普通近期事件挤掉。
- test_r8_owner_observer_installer.py: 固定资产构建器的完整性与解包边界，使用临时 zip，不下载替身二进制。
- test_r8_owner_snapshot_postgres.py: 复用项目真实 PostgreSQL 测试入口；每个用例仅操作新建的随机测试库/角色。
- test_r8_projection_generation_recovery.py: 可重建定位库共享代次锁：先保全，再按原canonical游标追平，未知来源不可当空。
- test_r8_task_context_window.py: 热上下文只读取有界近期和结构化必需任务；完整历史导出仍独立保留。
- test_wave9_host_substrate_apis.py: LLM: A2返工的真实认证磁盘和Host HTTP回归，只使用临时Owner及合成种子，不触碰生产认证资料。 | 模块用途: 守住TOTP密文、单步显式副作用许可与包摘要错误码。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import textwrap
import threading
import zipfile
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from agent_py_agent.agent.assets.workflow_store import WorkflowStore
from agent_py_agent.agent.conversation import ConversationStore, store_tasks, task_history
from agent_py_agent.agent.conversation.background_transcript import (
    TranscriptSource,
    append_background_transcript_event,
)
from agent_py_agent.agent.conversation.models import ThreadTaskLink
from agent_py_agent.agent.conversation.session_list_index import SessionListIndex
from agent_py_agent.agent.conversation.usage_index import UsageIndex
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.client_credentials import GatewayClientCredentials
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.owner_object_store import (
    OwnerObjectConfig,
    OwnerSnapshotLeaseLost,
    OwnerSnapshotStore,
)
from agent_py_agent.agent.pg_rls import tenant_session
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.runtime_db.recovery_guard import (
    RuntimeDatabaseDeferred,
    close_runtime_connection,
    guarded_runtime_connect,
)
from agent_py_agent.agent.runtime_schema import (
    apply_runtime_migrations,
    require_runtime_schema_current,
)
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.storage_backend import StorageBackend
from agent_py_agent.agent.user_space.totp_sso_auth import TotpSSOAuthService, totp_code
from agent_py_agent.cli.chat_parts.gateway_client import NetworkGatewayObserver
from agent_py_agent.tests.test_gateway_conversation_control import _free_port, _request
from agent_py_agent.tests.test_pg_rls import _pg
from agent_py_agent.tests.test_plugin_host_gateway_composition import production_call
from agent_py_agent.tests.test_plugin_host_v2_grants import http_request
from agent_py_agent.tests.test_plugin_package import _bundle
from agent_py_agent.tests.test_r8_owner_snapshot_changes import Journal
from agent_py_agent.tests.test_s1w2_owner_snapshot_delta import _Objects, _write_old
from agent_py_agent.tests.test_wave9_host_substrate_plugins_workflow_auth import (
    definition,
    local_user,
    script,
)
from agent_py_agent.tests.test_workflow_engine import assert_debug_contract, observe_script_runs
from deploy.install_watchman import install

pytest_plugins = ('agent_py_agent.tests.test_plugin_host_gateway_composition',)


# ============================================================================
# Source: agent_py_agent/tests/test_r8_gateway_memory_diagnostics.py
# ============================================================================

ROOT = Path(__file__).resolve().parents[2]


def _child(tmp_path, source, *, external=False, supported_mode=True):
    home = tmp_path / 'home'
    home.mkdir()
    env = {**os.environ, 'HOME': str(home), 'MY_AGENT_HOME': str(home)}
    command = [sys.executable, '-I']
    if supported_mode and getattr(sys, '_is_gil_enabled', lambda: True)() is False:
        # 旧分配成功合同明确在带GIL子进程验证；不把它算作free-threaded追踪通过。
        command += ['-X', 'gil=1']
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
            observed['metadata'] = arguments._gateway_memory_trace.metadata
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
        with gateway_memory_tracing(config) as tracing, ExitStack() as stack:
            metadata=tracing.metadata
            args._gateway_memory_trace=tracing
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


@pytest.mark.parametrize('external', [False, True])
def test_service_tick_applies_real_config_file_switches_without_snapshot_polling(tmp_path, external):
    result = _child(tmp_path, '''
        import json,queue,threading,tracemalloc
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch
        from agent_py_agent.cli import gateway_process as gp
        from agent_py_agent.agent.settings.config import load_config
        from agent_py_agent.agent.settings.live_switches import register_live_config
        from agent_py_agent.agent.gateway_parts.background_resource_report import gateway_memory_tracing
        path=Path.cwd()/'agent.yaml'
        def write(value):
            pending=path.with_suffix('.pending')
            pending.write_text('enable_gateway_memory_diagnostics: '+str(value).lower()+'\\n',encoding='utf-8')
            pending.replace(path)
        write(False);config=load_config(path);register_live_config(config)
        context=SimpleNamespace(config=config,paths=SimpleNamespace(stop_request=Path.cwd()/'no-stop'),process_started_at=0)
        states=queue.Queue();advance=threading.Event();errors=[];snapshots=[];warnings=[]
        original_snapshot=tracemalloc.take_snapshot
        def snapshot():
            snapshots.append(1)
            return original_snapshot()
        with gateway_memory_tracing(config) as tracing:
            prior=bytearray(4096)
            def drain(*args):
                states.put((tracemalloc.is_tracing(),tracing.metadata))
                assert advance.wait(10)
                advance.clear()
                return {'summary':'fixture complete'} if len(observed)==6 else None
            def run():
                try:gp._run_gateway_service_loop(context,memory_tracing=tracing)
                except BaseException as error:errors.append(error)
            observed=[]
            with patch.object(gp,'_gateway_context_process_identity',return_value={}), \
                 patch.object(gp,'drain_for_requested_restart',side_effect=drain), \
                 patch.object(gp.time,'sleep',return_value=None), \
                 patch.object(gp,'_print_gateway_loop_error',side_effect=lambda *a:warnings.append(str(a[2]))), \
                 patch.object(tracemalloc,'take_snapshot',side_effect=snapshot):
                thread=threading.Thread(target=run);thread.start()
                try:
                    for enabled_next in (True,None,None,False,True,True):
                        observed.append(states.get(timeout=10))
                        if len(observed)==2:
                            current=bytearray(4096)
                            assert tracemalloc.get_object_traceback(current) is not None
                            if not bool(__import__("sys")._xoptions.get("tracemalloc")):
                                assert tracemalloc.get_object_traceback(prior) is None
                        assert snapshots==[], 'service tick took a diagnostic heap snapshot'
                        if enabled_next is None:
                            path.unlink(missing_ok=True)
                        else:
                            write(enabled_next)
                        advance.set()
                    thread.join(10)
                    assert not thread.is_alive() and not errors
                    assert config.enable_gateway_memory_diagnostics is True
                    assert warnings==['FileNotFoundError']
                    marker=bytearray(1024*1024)
                    report=tracing.report()
                    assert report['tracemalloc_current_bytes']>=len(marker)
                    assert len(snapshots)==1
                    assert report['tracemalloc_scope'] in {'gateway_live_window','existing_trace'}
                finally:
                    advance.set();thread.join(10)
        print(json.dumps({'states':observed,'after_close':tracemalloc.is_tracing()}))
    ''', external=external)
    assert [state[0] for state in result['states']] == ([True] * 6 if external else [False, True, True, True, False, True])
    assert result['after_close'] is external
    if external:
        assert all(state[1]['tracemalloc_scope'] == 'existing_trace' for state in result['states'])
    else:
        assert result['states'][0][1] is None and result['states'][4][1] is None
        assert result['states'][1][1]['tracemalloc_scope'] == 'gateway_live_window'
        assert result['states'][5][1]['tracemalloc_scope'] == 'gateway_live_window'
        assert result['states'][5][1]['tracemalloc_started_at'] >= result['states'][1][1]['tracemalloc_started_at']


def test_live_disable_serializes_with_snapshot_and_closed_state_never_reopens(tmp_path):
    assert _child(tmp_path, '''
        import json,threading,tracemalloc
        from types import SimpleNamespace
        from unittest.mock import patch
        from agent_py_agent.agent.gateway_parts.background_resource_report import gateway_memory_tracing
        entered=threading.Event();release=threading.Event();disabling=threading.Event();disabled=threading.Event()
        failures=[];reports=[]
        with gateway_memory_tracing(SimpleNamespace(enable_gateway_memory_diagnostics=True)) as tracing:
            original=tracemalloc.take_snapshot
            def snapshot():
                entered.set();assert release.wait(10)
                return original()
            def read():
                try:reports.append(tracing.report())
                except BaseException as error:failures.append(error)
            def disable():
                disabling.set()
                try:tracing.refresh(SimpleNamespace(enable_gateway_memory_diagnostics=False))
                except BaseException as error:failures.append(error)
                disabled.set()
            with patch.object(tracemalloc,'take_snapshot',side_effect=snapshot):
                reader=threading.Thread(target=read);reader.start();assert entered.wait(10)
                writer=threading.Thread(target=disable);writer.start();assert disabling.wait(10)
                try:
                    assert not disabled.is_set() and tracemalloc.is_tracing()
                finally:
                    release.set();reader.join(10);writer.join(10)
                assert disabled.is_set() and not failures and reports
                assert not tracemalloc.is_tracing()
                assert tracing.report() is None
        tracing.close()
        assert tracing.report() is None
        try:tracing.refresh(SimpleNamespace(enable_gateway_memory_diagnostics=True))
        except RuntimeError:pass
        else:raise AssertionError('closed controller reopened tracing')
        assert not tracemalloc.is_tracing()
        print(json.dumps({'ok':True}))
    ''') == {'ok': True}


def test_unavailable_runtime_never_calls_tracemalloc_and_keeps_numeric_diagnostics(tmp_path):
    assert _child(tmp_path, '''
        import json,sys,tracemalloc
        from types import SimpleNamespace
        from unittest.mock import patch
        from contextlib import ExitStack
        from agent_py_agent.agent.gateway_parts.background_resource_report import (
            GatewayMemoryTracing, collect_runtime_memory_diagnostics)
        def forbidden(*args,**kwargs):raise AssertionError('unsafe tracemalloc API called')
        with ExitStack() as patches:
            patches.enter_context(patch.object(sys,'_is_gil_enabled',return_value=False,create=True))
            for name in ('start','stop','is_tracing','take_snapshot','get_traced_memory',
                         'get_tracemalloc_memory','get_traceback_limit'):
                patches.enter_context(patch.object(tracemalloc,name,side_effect=forbidden))
            tracing=GatewayMemoryTracing()
            assert tracing.refresh(SimpleNamespace(enable_gateway_memory_diagnostics=False)) is None
            assert tracing.report() is None
            assert tracing.refresh(SimpleNamespace(enable_gateway_memory_diagnostics=True))['tracemalloc_scope']=='unavailable_free_threaded'
            for report in (tracing.report(),collect_runtime_memory_diagnostics(include_tracemalloc=True)):
                assert report['tracemalloc_unavailable_reason']=='free_threaded_runtime'
                assert report['tracemalloc_started_at'] is None
                assert len(report['gc_counts'])==3 and 'rss_peak_bytes' in report
                assert 'tracemalloc_top' not in report and 'tracemalloc_current_bytes' not in report
            tracing.close()
        print(json.dumps({'ok':True}))
    ''', supported_mode=False) == {'ok': True}


def test_native_runtime_guard_survives_concurrent_standard_library_imports(tmp_path):
    result = _child(tmp_path, '''
        import importlib,json,sys,threading
        from types import SimpleNamespace
        from agent_py_agent.agent.gateway_parts.background_resource_report import gateway_memory_tracing
        config=SimpleNamespace(enable_gateway_memory_diagnostics=True)
        barrier=threading.Barrier(8)
        errors=[]
        def worker(name):
            try:
                barrier.wait(10)
                importlib.import_module(name)
            except BaseException as error:errors.append(type(error).__name__)
        with gateway_memory_tracing(config) as tracing:
            threads=[threading.Thread(target=worker,args=(name,)) for name in (
                'http.server','concurrent.futures','asyncio','pathlib','email.policy',
                'urllib.request','unittest.mock','xml.dom.minidom')]
            for thread in threads:thread.start()
            for thread in threads:thread.join(10)
            assert not errors and all(not thread.is_alive() for thread in threads)
            report=tracing.report()
            native_free_threaded=getattr(sys,'_is_gil_enabled',lambda:True)() is False
            if native_free_threaded:
                assert report['tracemalloc_scope']=='unavailable_free_threaded'
                assert report['tracemalloc_unavailable_reason']=='free_threaded_runtime'
                assert 'tracemalloc_top' not in report
            else:
                assert report['tracemalloc_scope']=='gateway_before_agent' and report['tracemalloc_top']
            assert len(report['gc_counts'])==3 and 'rss_peak_bytes' in report
        print(json.dumps({'native_free_threaded':native_free_threaded,'ok':True}))
    ''', supported_mode=False)
    assert result == {'native_free_threaded': getattr(sys, '_is_gil_enabled', lambda: True)() is False, 'ok': True}


def test_native_runtime_diagnostic_gateway_reaches_ready_and_formally_stops(tmp_path):
    import time

    home = tmp_path / 'native-home'
    home.mkdir()
    config = tmp_path / 'native.yaml'
    config.write_text(f'model_backend: echo\nmy_agent_home: {home}\nenable_plugins: false\n'
                      'gateway_worker_process_count: 0\ngateway_port: 0\n'
                      'enable_gateway_memory_diagnostics: true\n', encoding='utf-8')
    env = {**os.environ, 'HOME': str(home), 'MY_AGENT_HOME': str(home)}
    prefix = f'import sys;sys.path.insert(0,{str(ROOT)!r});'
    run = (prefix + 'import faulthandler,runpy;faulthandler.dump_traceback_later(25,exit=True);'
           f'sys.argv=["my-agent","--config",{str(config)!r},"gateway","run"];'
           'runpy.run_module("agent_py_agent",run_name="__main__")')
    log = tmp_path / 'native-run.log'
    with log.open('w') as output:
        process = subprocess.Popen([sys.executable, '-I', '-c', run], cwd=tmp_path, env=env,
                                   stdout=output, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 20
            while process.poll() is None and 'status=running' not in log.read_text():
                assert time.monotonic() < deadline, log.read_text()
                time.sleep(.02)
            assert process.poll() is None, log.read_text()
            stop = prefix + (f'from agent_py_agent.__main__ import main;'
                             f'raise SystemExit(main(["--config",{str(config)!r},"gateway","stop"]))')
            with subprocess.Popen([sys.executable, '-I', '-c', stop], cwd=tmp_path, env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as stopping:
                # 本测试正是Gateway的父进程，必须并行回收退出证明，不能让stop等待未回收僵尸。
                assert process.wait(timeout=20) == 0, log.read_text()
                stdout, stderr = stopping.communicate(timeout=10)
                assert stopping.returncode == 0, stderr + stdout
            assert 'drain_complete=true' in log.read_text()
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


# ============================================================================
# Source: agent_py_agent/tests/test_r8_idle_notice_http.py
# ============================================================================

def test_default_hubless_http_rechecks_live_sources_and_crossprocess_append(tmp_path, monkeypatch):
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'home'))
    agent = SimpleAgent(AgentConfig(model_backend='echo', gateway_per_user_owner_scoping=False), tmp_path / 'workspace')
    assert agent.config.gateway_event_hub_ring_events == 0
    assert agent.config.gateway_idle_notice_reuse_seconds > 0
    store = agent.conversation_store
    thread = store.threads.get_or_create(_request('request')['conversation'])
    page_calls = []
    original_page = store.messages.page_after_offset_report
    def page(*args, **kwargs):
        page_calls.append(kwargs)
        return original_page(*args, **kwargs)
    monkeypatch.setattr(store.messages, 'page_after_offset_report', page)
    paths = gateway_paths(agent)
    server = GatewayHTTPServer(_free_port(), paths, params=GatewayHTTPServerParams(agent=agent))
    server.start()
    headers = GatewayClientCredentials(agent.home_paths.root).headers()
    identity = {'user_id': 'u-1', 'channel': 'feishu'}
    client = NetworkGatewayObserver(SimpleNamespace(config=agent.config,
        gateway_request_identity=lambda: dict(identity),
        gateway_network_headers=lambda: headers), f'http://127.0.0.1:{server.port}')
    body = {'conversation_id': 'c-1', 'after': 0, 'event_after': 0, 'client_capabilities': {'tool_approval': True}}
    def poll():
        status, payload = client.request_json('POST', '/client/notices', body)
        assert status == 200 and payload['ok'], (status, payload)
        return payload
    try:
        assert server.request_event_hub is None
        assert poll()['notices'] == poll()['notices'] == []
        assert len(page_calls) == 1
        store.tasks.bind({'thread_id': thread.thread_id, 'task_id': 'task-active', 'goal': 'test', 'now': 2})
        from agent_py_agent.agent.conversation.agent_activity import (
            MainActivitySource,
            publish_main_activity,
        )
        publish_main_activity(agent, MainActivitySource(thread.thread_id, 'task-active', 2, 'projection'), phase='running', begin=True)
        assert poll()['active_task_count'] == 1
        store.tasks.update_status({'task_id': 'task-active', 'status': 'cancelled', 'expected_status': 'active'})
        assert poll()['active_task_count'] == 0
        append_background_transcript_event(agent, source=TranscriptSource(thread.thread_id, '', 'notice-run'),
            kind='system_message', phase='completed', block_id='new-event', payload={'text': 'memory event'})
        assert [event['block_id'] for event in poll()['transcript_events']] == ['new-event']
        assert len(page_calls) == 1  # 以上逐请求重核并未把空消息页重复打开。
        script = '''
import sys
from agent_py_agent.agent.conversation.store import ConversationStore
store=ConversationStore(sys.argv[1],initialize=False)
store.messages.append({'thread_id':sys.argv[2],'role':'assistant','content':'new crossprocess notice',
'metadata':{'background_delivery_reason':'root_subagents_terminal','assistant_part_id':'final'}})
'''
        subprocess.run([sys.executable, '-c', script, str(store.storage.root), thread.thread_id], check=True, timeout=30)
        assert [notice['content'] for notice in poll()['notices']] == ['new crossprocess notice']
        assert len(page_calls) == 2
        identity['user_id'] = 'u-2'
        other_actor = poll()
        assert other_actor['notices'] == [] and other_actor['thread_id'] == ''
        identity['user_id'] = 'u-1'
        server.auth_middleware.set_local_client_credential('revoked-original-local-credential')
        status, payload = client.request_json('POST', '/client/notices', body)
        assert status == 403
        # 原Client保留结构化拒绝，调用者无需把403猜成网络失败或空数据。
        assert payload == {'error': 'forbidden', 'error_code': 'LOCAL_CREDENTIAL_REQUIRED',
                           'message': 'untrusted source:不可信来源不可提交任务'}
        assert len(page_calls) == 2
    finally:
        server.stop()


@pytest.mark.parametrize('retained', [False, True])
def test_default_http_activity_uses_one_display_thread_generation(tmp_path, monkeypatch, retained, request):
    import inspect
    import json
    import threading
    from dataclasses import replace

    from agent_py_agent.agent.agent_core.runtime.task_identity import task_path_progress_ledger_id
    from agent_py_agent.agent.gateway_parts import http_handlers
    from agent_py_agent.agent.task_progress import (
        with_task_progress_display_plan,
        write_task_progress,
    )
    from agent_py_agent.tests.test_conversation_context_usage import usage

    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'home'))
    agent = SimpleAgent(AgentConfig(model_backend='echo', gateway_per_user_owner_scoping=False), tmp_path / 'workspace')
    store = agent.conversation_store
    thread = store.threads.get_or_create(_request('request')['conversation'])
    for generation in (1, 2):
        task_id = f'task-{generation}'
        task_path = str(tmp_path / f'task-{generation}')
        store.tasks.bind({'thread_id': thread.thread_id, 'task_id': task_id, 'goal': 'display',
                          'task_path': task_path, 'now': generation})
        write_task_progress(agent.home_paths.owner_home_dir, task_path_progress_ledger_id(task_path),
            with_task_progress_display_plan({'items': [{'id': 'item', 'title': 'display', 'status': 'pending'}]},
                                            generation_id=f'turn-{generation}', item_ids=['item']))
        if retained:
            store.tasks.update_status({'task_id': task_id, 'status': 'completed', 'expected_status': 'active'})
    base = store.threads.load(thread.thread_id)

    def version(generation):
        return replace(base, workspace_task_id=f'task-{generation}', compact_generation=generation,
            model_context_usage={**usage(42_100 + generation), 'compact_generation': generation},
            model_metrics={'schema': 'model_runtime_metrics.v1', 'model_rounds': generation})

    store.threads.write(version(1))
    projection = threading.local()
    observations = []
    original_activity = http_handlers.conversation_agent_activity
    original_load = store.threads.load_report
    display_callers = {'_conversation_context_state', 'model_metrics_from_thread',
                       '_conversation_workspace_task_id', '_retained_workspace_task_link',
                       '_conversation_display_thread'}
    changed = []

    def load(thread_id):
        result = original_load(thread_id)
        if getattr(projection, 'active', False) and thread_id == thread.thread_id:
            observations[-1]['all'] += 1
            if inspect.currentframe().f_back.f_code.co_name in display_callers:
                observations[-1]['display'] += 1
        return result

    def activity(*args, **kwargs):
        observations.append({'all': 0, 'display': 0})
        projection.active = True
        if not changed:
            changed.append(True)
            store.threads.write(version(2))
        try:
            return original_activity(*args, **kwargs)
        finally:
            projection.active = False

    monkeypatch.setattr(store.threads, 'load_report', load)
    monkeypatch.setattr(http_handlers, 'conversation_agent_activity', activity)
    server = GatewayHTTPServer(_free_port(), gateway_paths(agent), params=GatewayHTTPServerParams(agent=agent))
    server.start()
    headers = GatewayClientCredentials(agent.home_paths.root).headers()
    client = NetworkGatewayObserver(SimpleNamespace(config=agent.config,
        gateway_request_identity=lambda: {'user_id': 'u-1', 'channel': 'feishu'},
        gateway_network_headers=lambda: headers), f'http://127.0.0.1:{server.port}')
    body = {'conversation_id': 'c-1', 'after': 0, 'event_after': 0}
    try:
        assert server.request_event_hub is None and 'wait' not in body
        for generation in (1, 2):
            status, payload = client.request_json('POST', '/client/notices', body)
            assert status == 200 and payload['ok'], (status, payload)
            shown = payload['agent_activity']
            assert shown['compact_count'] == generation
            assert shown['context_usage']['current_tokens'] == 42_100 + generation
            assert shown['model_metrics']['model_rounds'] == generation
            assert shown['task_progress_generation_id'] == f'turn-{generation}'
            assert observations[-1]['display'] == 0  # 复用本拍绑定已核线程，不再重读展示正文。
            assert observations[-1]['all'] > 1  # 原任务/claim授权仍独立读取，不借展示快照授权。
        store.storage.thread_path(thread.thread_id).write_bytes(b'{broken')
        status, payload = client.request_json('POST', '/client/notices', body)
        assert status == 200 and payload['ok'] is False
        assert payload.get('agent_activity', {}).get('model_metrics', {}) == {}
    finally:
        request.node.user_properties.append(('activity_thread_load_counts', json.dumps(observations)))
        server.stop()


def test_activity_reuses_only_same_thread_display_record(tmp_path, monkeypatch):
    from dataclasses import replace

    from agent_py_agent.agent.conversation.agent_activity import conversation_agent_activity
    from agent_py_agent.agent.conversation.store import ConversationStore

    store = ConversationStore(tmp_path / 'conversations')
    thread = store.threads.get_or_create({'canonical_user_id': 'alice'})
    current = replace(thread, compact_generation=3)
    store.threads.write(current)
    reads = []
    original = store.threads.load_report

    def load(thread_id):
        reads.append(thread_id)
        return original(thread_id)

    monkeypatch.setattr(store.threads, 'load_report', load)
    agent = SimpleNamespace()
    snapshot = replace(current, compact_generation=2)
    # 本拍展示可取已核快照；任务活动仍读取当前规范账，下一拍不复用旧值。
    shown = conversation_agent_activity(agent, store, thread.thread_id, display_thread=snapshot)
    snapshot_reads = len(reads)
    assert shown.compact_count == 2
    reads.clear()
    shown = conversation_agent_activity(agent, store, thread.thread_id)
    assert shown.compact_count == 3
    assert len(reads) == snapshot_reads + 1
    reads.clear()
    foreign = replace(snapshot, thread_id='thread-foreign')
    shown = conversation_agent_activity(agent, store, thread.thread_id, display_thread=foreign)
    assert shown.compact_count == 3
    assert len(reads) == snapshot_reads + 1


# ============================================================================
# Source: agent_py_agent/tests/test_r8_observation_thread_pending.py
# ============================================================================

def _store(tmp_path):
    store = ConversationStore(tmp_path / 'conversations')
    first = store.threads.create(canonical_user_id='first')
    second = store.threads.create(canonical_user_id='second')
    return store, first, second


def _append(store, thread, number, *, needs_main=True):
    return store.observations.append({
        'thread_id': thread.thread_id, '_observation_id': f'event-{thread.thread_id}-{number}',
        'event_type': 'subagent_runner_finished' if needs_main else 'progress',
        'requires_main_agent': needs_main, 'now': float(number + 1),
        'root_task_id': 'old-root', 'parent_agent_id': 'old-root', 'source_agent_id': f'child-{number}',
        'metadata': {'task_id': f'child-{number}', 'completion_schema_version': 'subagent-completion.v1',
                     'status': 'DONE', 'completion_message': '已完成'},
    })


def _ready(store):
    for _ in range(32):
        report = store.observations._index.maintenance()
        if report['coverage'] == 'complete':
            return
    raise AssertionError(report)


def test_completion_survives_ordinary_tail_and_ack_removes_only_exact_thread_event(tmp_path):
    store, first, second = _store(tmp_path)
    completion = _append(store, first, 0)
    other = _append(store, second, 0)
    for number in range(1, 140):
        _append(store, first, number, needs_main=False)
    _ready(store)
    cold = ConversationStore(store.storage.root, initialize=False)
    tail, errors, _coverage = cold.observations.recent_window_report(first.thread_id)
    assert not errors and completion.observation_id not in {event.observation_id for event in tail}
    events, errors = cold.observations.unhandled_requiring_main_report(first.thread_id)
    assert not errors and events == [completion]
    assert cold.observations._index.last_work['candidate_rows'] == 1
    assert cold.observations._index.last_work['json_decodes'] == 1
    cold.observations.mark_handled([completion.observation_id], now=1000)
    assert cold.observations.unhandled_requiring_main_report(first.thread_id) == ([], [])
    assert cold.observations.unhandled_requiring_main_report(second.thread_id) == ([other], [])


def test_thread_filter_is_applied_before_limit_and_zero_keeps_all_active(tmp_path):
    store, first, second = _store(tmp_path)
    _append(store, second, 0)
    pending = [_append(store, first, number) for number in range(1, 151)]
    _ready(store)
    events, errors = store.observations.unhandled_requiring_main_report(first.thread_id, limit=1)
    assert not errors and events == pending[:1]
    events, errors = store.observations.unhandled_requiring_main_report(first.thread_id)
    assert not errors and events == pending
    assert store.observations._index.last_work['candidate_rows'] == 150
    assert store.observations._index.last_work['json_decodes'] == 150
    # 旧global/wake读取语义保持，thread只增加可选交集。
    assert len(store.observations._index.pending(0)) == 151
    assert len(store.observations._index.pending(0, wake='')) == 151
    assert store.observations._index.pending(0, wake='', thread_id=first.thread_id) == pending


def test_cold_partial_never_claims_complete_empty_and_recovery_continues(tmp_path):
    store, first, _second = _store(tmp_path)
    for number in range(160):
        _append(store, first, number, needs_main=False)
    completion = _append(store, first, 161)
    cold = ConversationStore(store.storage.root, initialize=False)
    events, errors = cold.observations.unhandled_requiring_main_report(first.thread_id)
    assert events == [] and errors
    assert errors[0]['error_type'] == 'ObservationIndexIncomplete'
    assert errors[0]['coverage'] == 'partial'
    _ready(cold)
    assert cold.observations.unhandled_requiring_main_report(first.thread_id) == ([completion], [])


def test_bad_legacy_confirmation_stays_typed_unknown_not_empty(tmp_path):
    store, first, _second = _store(tmp_path)
    _append(store, first, 1)
    store.storage.observation_handled_path.write_bytes(b'{broken')
    events, errors = store.observations.unhandled_requiring_main_report(first.thread_id)
    assert events == [] and errors
    assert errors[0]['error_type'] == 'ObservationIndexError'
    assert errors[0]['coverage'] == 'partial'
    assert store.storage.observation_handled_path.read_bytes() == b'{broken'


def test_thread_projection_never_returns_foreign_canonical_event(tmp_path):
    store, first, second = _store(tmp_path)
    foreign = _append(store, second, 1)
    _ready(store)
    index = store.observations._index
    with index._connection() as conn:
        conn.execute('UPDATE pending SET thread=? WHERE id=?', (first.thread_id, foreign.observation_id))
    events, errors = store.observations.unhandled_requiring_main_report(first.thread_id)
    assert events == [] and errors[0]['error_type'] == 'ObservationIndexIncomplete'
    assert errors[0]['coverage'] == 'partial'
    _ready(store)
    assert store.observations.unhandled_requiring_main_report(first.thread_id) == ([], [])
    assert store.observations.unhandled_requiring_main_report(second.thread_id) == ([foreign], [])


# ============================================================================
# Source: agent_py_agent/tests/test_r8_owner_observer_installer.py
# ============================================================================

def _archive(path, entries):
    with zipfile.ZipFile(path, 'w') as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_pinned_archive_installs_binary_and_absolute_dependency_library(tmp_path):
    archive = tmp_path / 'archive.zip'
    digest = _archive(archive, [('watchman-release/bin/watchman', b'executable'),
                               ('watchman-release/lib/libexample.so.1', b'library')])
    install(archive, tmp_path / 'prefix', tmp_path / 'system-libs', digest)
    assert (tmp_path / 'prefix/bin/watchman').read_bytes() == b'executable'
    assert (tmp_path / 'prefix/bin/watchman').stat().st_mode & 0o111
    assert (tmp_path / 'system-libs/libexample.so.1').read_bytes() == b'library'


def test_archive_digest_mismatch_never_installs_bytes(tmp_path):
    archive = tmp_path / 'archive.zip'
    _archive(archive, [('release/bin/watchman', b'wrong')])
    with pytest.raises(RuntimeError, match='SHA256'):
        install(archive, tmp_path / 'prefix', tmp_path / 'libs', '0' * 64)
    assert not (tmp_path / 'prefix').exists()


@pytest.mark.parametrize('kind', ['parent', 'absolute', 'symlink', 'backslash'])
def test_unsafe_archive_layout_is_rejected(tmp_path, kind):
    name = {'parent': '../escape', 'absolute': '/escape', 'symlink': 'release/bin/watchman',
            'backslash': 'release/bin/..\\escape'}[kind]
    item = zipfile.ZipInfo(name)
    if kind == 'symlink':
        item.external_attr = (stat.S_IFLNK | 0o777) << 16
    archive = tmp_path / 'archive.zip'
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.writestr(item, b'not executable')
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError, match='危险'):
        install(archive, tmp_path / 'prefix', tmp_path / 'libs', digest)
    assert not (tmp_path / 'escape').exists()


# ============================================================================
# Source: agent_py_agent/tests/test_r8_owner_snapshot_postgres.py
# ============================================================================

@pytest.fixture
def snapshot_pg():
    admin = _pg()
    import os

    original = make_url(os.environ.get('TEST_POSTGRES_URL', 'postgresql+psycopg://localhost:5432/postgres'))
    name, role, password = 'r8_snapshot_' + uuid4().hex[:12], 'r8_app_' + uuid4().hex[:12], uuid4().hex
    created_database = created_role = False
    db = app = None
    try:
        with admin.connect() as connection:
            connection = connection.execution_options(isolation_level='AUTOCOMMIT')
            connection.execute(text(f'CREATE DATABASE {name}'))
            created_database = True
            connection.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'"))
            created_role = True
        db = StorageBackend(original.set(database=name).render_as_string(hide_password=False))
        apply_runtime_migrations(db, include_scale_data=True, app_role=role)
        app = StorageBackend(original.set(database=name, username=role, password=password).render_as_string(hide_password=False))
        require_runtime_schema_current(app, include_scale_data=True, app_role=role)
        store = OwnerSnapshotStore(app, _Objects(), OwnerObjectConfig('test'))
        store._changes = Journal()
        yield db, app, store
    finally:
        for backend in (app, db):
            if backend is not None:
                backend.engine.dispose()
        with admin.connect() as connection:
            connection = connection.execution_options(isolation_level='AUTOCOMMIT')
            if created_database:
                connection.execute(text(f'DROP DATABASE {name} WITH (FORCE)'))
            if created_role:
                connection.execute(text(f'DROP ROLE {role}'))
        admin.engine.dispose()


def _seed_file(store, tenant, relative, data):
    digest = hashlib.sha256(data).hexdigest()
    key = store._object_key(tenant, digest)
    store._client.values[key] = data
    with tenant_session(store._backend, tenant) as conn:
        conn.execute(text('''INSERT INTO owner_file_manifest VALUES(:tenant,'user','owner',:path,:key,:digest,:size,384,1)
            ON CONFLICT(tenant,owner_kind,owner_id,relative_path) DO UPDATE SET
            object_key=EXCLUDED.object_key,content_sha256=EXCLUDED.content_sha256,size_bytes=EXCLUDED.size_bytes'''),
                     {'tenant': tenant, 'path': relative, 'key': key, 'digest': digest, 'size': len(data)})


@contextmanager
def _checkout(store, tmp_path):
    warm = tmp_path / 'warm'
    home = warm / 'home/owners/u1'
    with store.checkout_delta('acme', 'user', 'owner', home, warm_dir=warm, lease_ttl_seconds=30,
                              hold_advisory_lock=True):
        yield home


def test_real_postgres_legacy_manifest_writes_feed_incremental_warm_restore(snapshot_pg, tmp_path):
    db, app, store = snapshot_pg
    _seed_file(store, 'acme', 'a', b'old')
    _seed_file(store, 'acme', 'b', b'delete')
    _seed_file(store, 'globex', 'other', b'private')
    with _checkout(store, tmp_path):
        pass
    _seed_file(store, 'acme', 'a', b'new')
    with tenant_session(app, 'acme') as conn:
        conn.execute(text("DELETE FROM owner_file_manifest WHERE relative_path='b'"))
    store._read_manifest = lambda *_: pytest.fail('remote change used complete manifest')
    store._warm_paths = lambda *_: pytest.fail('remote change used complete local scan')
    with _checkout(store, tmp_path) as home:
        assert (home / 'a').read_bytes() == b'new'
        assert not (home / 'b').exists()
    with tenant_session(app, 'acme') as conn:
        rows = conn.execute(text('SELECT tenant FROM owner_file_changes')).all()
        assert rows and {row.tenant for row in rows} == {'acme'}
    with tenant_session(app, 'globex') as conn:
        assert conn.execute(text('SELECT count(*) FROM owner_file_changes')).scalar_one() == 1
    from agent_py_agent.agent.owner_snapshot_cache import OwnerSnapshotIndex
    with OwnerSnapshotIndex.open(tmp_path / 'warm/snapshot-index.sqlite3') as index:
        with tenant_session(app, 'acme') as conn:
            version = conn.execute(text("SELECT version FROM owner_snapshot_head WHERE owner_id='owner'")).scalar_one()
        assert index.metadata()['version'] == version
        assert index.metadata()['mode'] == 'incremental'


def test_real_postgres_generation_fences_out_of_protocol_concurrent_manifest_write(snapshot_pg, tmp_path):
    db, app, store = snapshot_pg
    _seed_file(store, 'acme', 'a', b'old')
    with _checkout(store, tmp_path):
        pass
    with pytest.raises(OwnerSnapshotLeaseLost):
        with _checkout(store, tmp_path) as home:
            _write_old(home / 'a', b'local uncommitted')
            store._changes.record('a')
            _seed_file(store, 'acme', 'a', b'remote winning write')
    assert store._read_manifest('acme', 'user', 'owner')[0].content_sha256 == hashlib.sha256(b'remote winning write').hexdigest()


def test_real_postgres_projection_floor_is_partial_and_trigger_readiness_is_mandatory(snapshot_pg):
    db, app, store = snapshot_pg
    for number in range(100):
        _seed_file(store, 'acme', str(number), str(number).encode())
    with tenant_session(app, 'acme') as conn:
        conn.execute(text("UPDATE owner_snapshot_head SET version=65599 WHERE owner_id='owner'"))
    _seed_file(store, 'acme', 'latest', b'latest')
    delta = store._read_manifest_delta('acme', 'user', 'owner', after=0, through=65600)
    assert not delta.complete and delta.reason == 'remote_change_cursor_expired'
    with tenant_session(app, 'acme') as conn:
        assert conn.execute(text('SELECT count(*) FROM owner_file_changes')).scalar_one() == 37
        assert conn.execute(text('SELECT count(*) FROM owner_file_manifest')).scalar_one() == 101
    with db.begin() as conn:
        conn.execute(text('ALTER TABLE owner_file_manifest DISABLE TRIGGER owner_manifest_generation'))
    try:
        with pytest.raises(RuntimeError, match='触发器'):
            require_runtime_schema_current(app, include_scale_data=True, app_role=make_url(str(app.engine.url)).username)
    finally:
        with db.begin() as conn:
            conn.execute(text('ALTER TABLE owner_file_manifest ENABLE TRIGGER owner_manifest_generation'))


# ============================================================================
# Source: agent_py_agent/tests/test_r8_projection_generation_recovery.py
# ============================================================================

def _case(tmp_path, kind):
    store = ConversationStore(tmp_path / 'conversations')
    if kind == 'observation':
        index = store.observations._index
        index.ack(['known'], 10)
    elif kind == 'guidance':
        index = store.guidance.ledger.pending_index()
        index.mark_delivered(['known'], 10)
    elif kind == 'usage':
        source = tmp_path / 'usage.jsonl'
        source.touch()
        index = UsageIndex(source, 'thread')
    else:
        root = tmp_path / 'threads'
        root.mkdir()
        index = SessionListIndex(root, kind='thread', reader=lambda path: ({}, None))
    with index._connection() as conn:
        conn.execute('SELECT 1').fetchone()
    return index


@pytest.mark.parametrize('kind', ['observation', 'guidance', 'usage', 'list'])
def test_projection_corruption_preserves_bytes_then_replays_original_sources(tmp_path, kind):
    index = _case(tmp_path, kind)
    damage = b'not a sqlite database with evidence'
    index.path.write_bytes(damage)
    with index._connection() as conn:
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    result = json.loads(Path(str(index.path) + '.recovery-result.json').read_text())
    assert result['receipt'] == {'projection_rebuild': True, 'canonical_replay_required': True}
    assert (index.path.parent / result['backup']).read_bytes() == damage
    assert index.path.stat().st_ino != result['identity'][1]
    if kind == 'observation':
        assert index.handled(['known']) == {'known': 10}
    elif kind == 'guidance':
        assert index.delivered_many(['known']) == {'known': 10}
    elif kind == 'usage':
        with index._connection() as conn:
            assert index.prepare(conn)


def test_live_physical_shared_lease_fences_projection_replacement(tmp_path):
    index = _case(tmp_path, 'observation')
    damage = b'not sqlite and must not be truncated'
    index.path.write_bytes(damage)
    entered, release = threading.Event(), threading.Event()
    errors = []
    def reader():
        conn = None
        try:
            conn = guarded_runtime_connect(index.path, lambda: sqlite3.connect(index.path))
            entered.set()
            assert release.wait(5)
        except BaseException as error:
            errors.append(error)
        finally:
            if conn is not None:
                close_runtime_connection(conn)
    thread = threading.Thread(target=reader)
    thread.start()
    try:
        assert entered.wait(5)
        with pytest.raises(RuntimeDatabaseDeferred):
            with index._connection():
                pytest.fail('active generation was replaced')
        assert index.path.read_bytes() == damage
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and errors == []
    assert index.handled(['known']) == {'known': 10}


def test_missing_canonical_usage_source_blocks_empty_complete_rebuild(tmp_path):
    index = _case(tmp_path, 'usage')
    index.source.unlink()
    damage = b'not sqlite'
    index.path.write_bytes(damage)
    with pytest.raises(RuntimeDatabaseDeferred) as caught:
        with index._connection():
            pytest.fail('missing source cannot prove empty canonical history')
    assert index.path.read_bytes() == damage
    assert 'canonical_sources_unverified' in str(caught.value)
    assert any(path.read_bytes() == damage for path in tmp_path.glob('*.damaged-*'))


def test_unreadable_canonical_usage_is_not_complete_after_projection_rebuild(tmp_path):
    from agent_py_agent.agent.runtime_errors import DataCorruptionError

    index = _case(tmp_path, 'usage')
    index.source.write_text('not json\n')
    index.path.write_bytes(b'not sqlite')
    with index._connection() as conn:
        with pytest.raises(DataCorruptionError):
            index.prepare(conn)


def test_projection_publication_crash_rechecks_canonical_source_before_resume(tmp_path, monkeypatch):
    from agent_py_agent.agent.io import sqlite_recovery
    from agent_py_agent.agent.runtime_db import salvage

    index = _case(tmp_path, 'usage')
    index.path.write_bytes(b'damaged projection')
    original = salvage._publish_candidate
    def interrupted(db, state):
        raise KeyboardInterrupt('before projection publication')
    monkeypatch.setattr(salvage, '_publish_candidate', interrupted)
    with pytest.raises(KeyboardInterrupt):
        with index._connection():
            pytest.fail('publication must be interrupted')
    index.source.unlink()
    monkeypatch.setattr(salvage, '_publish_candidate', original)
    report = sqlite_recovery.recover_sqlite_database(index.path, automatic=True,
        rebuild_projection=index._projection_rebuild_proof)
    assert report['status'] == 'requires_recovery'
    assert 'canonical_sources_unverified' in report['reason']
    assert index.path.read_bytes() == b'damaged projection'


def test_projection_preserves_original_db_wal_shm_and_cold_journal(tmp_path):
    index = _case(tmp_path, 'observation')
    files = {'': b'original invalid database', '-wal': b'original wal',
             '-shm': b'original shm', '-journal': b'\0' * 8 + b'original journal'}
    for suffix, raw in files.items():
        Path(str(index.path) + suffix).write_bytes(raw)
    with index._connection():
        pass
    result = json.loads(Path(str(index.path) + '.recovery-result.json').read_text())
    backup = index.path.parent / result['backup']
    assert {suffix: Path(str(backup) + suffix).read_bytes() for suffix in files} == files
    assert index.handled(['known']) == {'known': 10}


def test_unknown_factory_error_never_resets_list_projection(tmp_path, monkeypatch):
    index = _case(tmp_path, 'list')
    original = index.path.read_bytes()
    def fail():
        raise sqlite3.OperationalError('unknown schema or environmental error')
    monkeypatch.setattr(index, '_open_projection', fail)
    with pytest.raises(sqlite3.OperationalError):
        with index._connection():
            pytest.fail('factory failure must propagate')
    assert index.path.read_bytes() == original
    assert not Path(str(index.path) + '.recovery-result.json').exists()


# ============================================================================
# Source: agent_py_agent/tests/test_r8_task_context_window.py
# ============================================================================

def _history(tmp_path, count):
    store = ConversationStore(tmp_path / 'conversations')
    thread = store.threads.get_or_create({'canonical_user_id': 'isolated', 'channel': 'internal'})
    ids = tuple(f'task-{number}' for number in range(count))
    for number, identifier in enumerate(ids):
        link = ThreadTaskLink(thread.thread_id, identifier, '历史任务', status='active' if number == 0 else 'completed')
        store.storage.task_path(identifier).write_text(json.dumps(link.to_dict()))
    task_history.initialize(store.storage.thread_path(thread.thread_id), replace(thread, task_ids=ids))
    thread = replace(thread, task_ids=ids[-128:], active_task_ids=(ids[0],), workspace_task_id=ids[1], task_history_version=1)
    store.threads.write(thread)
    return store, thread, ids


@pytest.mark.parametrize('count', [140, 1000])
def test_model_context_task_link_reads_do_not_grow_with_completed_history(tmp_path, monkeypatch, count):
    store, thread, ids = _history(tmp_path, count)
    original, reads = store_tasks._read_task_link, []

    def read(path, identifier):
        reads.append(identifier)
        return original(path, identifier)

    monkeypatch.setattr(store_tasks, '_read_task_link', read)
    for current in (store, ConversationStore(store.storage.root, initialize=False)):
        reads.clear()
        bundle, errors = current.context_bundle_report(thread.thread_id, recent_limit=20)
        print(json.dumps({'history': count, 'context_link_reads': len(reads)}))
        assert len(reads) <= 22
        assert not errors and {row['task_id'] for row in bundle['tasks']} == {*ids[-20:], ids[0], ids[1]}
        assert bundle['task_coverage']['status'] == 'window' and bundle['task_coverage']['complete'] is False
    reads.clear()
    links, errors = store.tasks.list_report(thread.thread_id)
    assert not errors and tuple(row.task_id for row in links) == ids
    assert len(reads) == count


def test_required_old_terminal_task_is_selected_without_loading_other_history(tmp_path):
    store, thread, ids = _history(tmp_path, 140)
    links, errors, warnings, coverage = store.tasks.context_report(
        thread.thread_id, recent_limit=3, required_task_ids=(ids[2],))
    assert not errors and not warnings
    assert {row.task_id for row in links} == {*ids[-3:], *ids[:3]}
    assert coverage['history_complete'] is False


@pytest.mark.parametrize('selection', ['recent', 'active', 'workspace', 'required'])
def test_every_selected_task_must_belong_to_requested_thread(tmp_path, selection):
    store, thread, ids = _history(tmp_path, 140)
    selected = {'recent': ids[-1], 'active': ids[0], 'workspace': ids[1], 'required': ids[2]}[selection]
    path = store.storage.task_path(selected)
    payload = json.loads(path.read_text())
    payload['thread_id'] = 'foreign-thread'
    path.write_text(json.dumps(payload))
    bundle, errors = store.context_bundle_report(thread.thread_id, recent_limit=3, required_task_ids=(ids[2],))
    assert selected not in {row['task_id'] for row in bundle['tasks']}
    assert any(row.get('task_id') == selected and row.get('scope_resolution') == 'thread_mismatch' for row in errors)
    assert bundle['task_coverage']['status'] == 'unknown'


@pytest.mark.parametrize('damage', ['missing', 'unknown', 'incomplete'])
def test_context_does_not_guess_or_repair_unproven_history(tmp_path, damage):
    store, thread, _ids = _history(tmp_path, 140)
    path = task_history.history_root(store.storage.thread_path(thread.thread_id)) / 'index.json'
    if damage == 'missing':
        path.unlink()
    else:
        data = json.loads(path.read_text())
        data['version' if damage == 'unknown' else 'incomplete'] = 99 if damage == 'unknown' else True
        path.write_text(json.dumps(data))
    before = path.read_bytes() if path.exists() else None
    bundle, errors = store.context_bundle_report(thread.thread_id)
    assert errors and bundle['tasks'] == []
    assert bundle['task_coverage']['status'] == 'unknown'
    assert (path.read_bytes() if path.exists() else None) == before


def test_background_keeps_explicit_old_task_and_reads_lineage_once(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.background_context import (
        BackgroundContextLoad,
        load_context_bundle,
    )

    store, thread, ids = _history(tmp_path, 140)
    calls = []
    manager = SimpleNamespace(
        load=lambda _identifier: SimpleNamespace(root_id=ids[2]),
        list_runs_for_root_report=lambda identifier: (
            calls.append(identifier) or SimpleNamespace(load_errors=[], runs=[SimpleNamespace(id=ids[3], root_id=ids[2])])),
    )
    state = BackgroundContextLoad(SimpleNamespace(subagents=manager), store, thread, ids[2], None, None, [])
    bundle = load_context_bundle(state)
    assert not state.load_errors
    assert {row['task_id'] for row in bundle['tasks']} == {ids[2], ids[3]}
    assert calls == [ids[2]]
    assert bundle['task_coverage']['selection'] == 'explicit_task_scope'
    assert 'history_positions' not in bundle['task_coverage']


def test_coverage_distinguishes_complete_history_from_zero_window(tmp_path):
    store, thread, ids = _history(tmp_path, 3)
    links, errors, warnings, coverage = store.tasks.context_report(thread.thread_id)
    assert {row.task_id for row in links} == set(ids) and not errors and not warnings
    assert coverage['status'] == 'complete' and coverage['history_complete'] is True
    links, errors, warnings, coverage = store.tasks.context_report(thread.thread_id, recent_limit=0)
    assert {row.task_id for row in links} == {ids[0], ids[1]} and not errors and not warnings
    assert coverage['status'] == 'window' and coverage['history_complete'] is False


@pytest.mark.parametrize('conflict', [False, True])
def test_named_bind_checks_only_active_members_after_long_terminal_history(tmp_path, monkeypatch, conflict):
    store, thread, ids = _history(tmp_path, 1000)
    path = store.storage.task_path(ids[0])
    data = json.loads(path.read_text())
    data.update(work_kind='audit', work_name='same' if conflict else 'other')
    path.write_text(json.dumps(data))
    monkeypatch.setattr(store.tasks, 'list_report', lambda *_args, **_kwargs: pytest.fail('full history read'))
    request = {'thread_id': thread.thread_id, 'task_id': 'new-named', 'work_kind': 'audit', 'work_name': 'same'}
    if conflict:
        with pytest.raises(ValueError, match='already exists'):
            store.tasks.bind(request)
        assert not store.storage.task_path('new-named').exists()
    else:
        assert store.tasks.bind(request).task_id == 'new-named'


@pytest.mark.parametrize('narrow', [False, True])
def test_task_coverage_survives_real_budget_and_rendering(tmp_path, narrow):
    from agent_py_agent.agent.conversation.background_context import (
        PreparedBackgroundContext,
        render_background_context,
    )
    from agent_py_agent.agent.conversation.context_budget import (
        BackgroundContextBudget,
        BackgroundContextPayloadRequest,
    )

    store, thread, ids = _history(tmp_path, 140)
    bundle, errors = store.context_bundle_report(thread.thread_id, required_task_ids=(ids[2],))
    assert not errors and bundle['tasks'][0]['task_id'] == ids[2]
    payload = BackgroundContextPayloadRequest(bundle, None, [], {}, budget=BackgroundContextBudget(max_total_tokens=1))
    prepared = PreparedBackgroundContext(payload, (), {}, (), ids[2], narrow, False)
    rendered = render_background_context(prepared)
    assert 'Task Coverage' in rendered
    assert '"history_complete": false' in rendered and '"status": "window"' in rendered


def test_named_bind_keeps_active_read_error_blocking(tmp_path):
    from agent_py_agent.agent.runtime_errors import DataCorruptionError

    store, thread, ids = _history(tmp_path, 140)
    path = store.storage.task_path(ids[0])
    path.write_bytes(b'{broken')
    with pytest.raises(DataCorruptionError, match='unavailable'):
        store.tasks.bind({'thread_id': thread.thread_id, 'task_id': 'new-named', 'work_kind': 'audit', 'work_name': 'same'})
    assert path.read_bytes() == b'{broken' and not store.storage.task_path('new-named').exists()


@pytest.mark.parametrize('active_count', [0, 1, 100])
def test_active_report_reads_only_active_members_and_keeps_terminal_history_out(tmp_path, monkeypatch, active_count):
    store, thread, ids = _history(tmp_path, 140)
    active = ids[:active_count]
    for identifier in active:
        path = store.storage.task_path(identifier)
        data = json.loads(path.read_text())
        data['status'] = 'active'
        path.write_text(json.dumps(data))
    store.threads.write(replace(thread, active_task_ids=active))
    original, reads = store_tasks._read_task_link, []

    def read(path, identifier):
        reads.append(identifier)
        return original(path, identifier)

    monkeypatch.setattr(store_tasks, '_read_task_link', read)
    monkeypatch.setattr(store.tasks, 'iter_task_ids', lambda *_args: pytest.fail('history traversal'))
    links, errors = store.tasks.active_report(thread.thread_id)
    assert not errors and tuple(row.task_id for row in links) == active
    assert reads == list(active)
    assert not ({row.task_id for row in links} & set(ids[active_count:]))


def test_foreign_active_link_is_rejected_by_active_control_and_named_consumers(tmp_path):
    from agent_py_agent.agent.gateway_parts.control_service import _session_protected_task_ids
    from agent_py_agent.agent.runtime_errors import DataCorruptionError

    store, thread, ids = _history(tmp_path, 140)
    foreign = store.threads.create(canonical_user_id='foreign')
    assert foreign.thread_id != thread.thread_id
    path = store.storage.task_path(ids[0])
    data = json.loads(path.read_text())
    data.update(thread_id=foreign.thread_id, work_kind='audit', work_name='same')
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    links, errors = store.tasks.active_report(thread.thread_id)
    assert links == [] and len(errors) == 1
    assert errors[0]['error_type'] == 'DataCorruptionError'
    assert errors[0]['scope_resolution'] == 'thread_mismatch'
    assert errors[0]['task_id'] == ids[0] and errors[0]['expected_thread_id'] == thread.thread_id
    assert store.tasks.link_report(thread.thread_id, ids[0]) == (None, None)
    with pytest.raises(ValueError, match='不能确认'):
        _session_protected_task_ids(store, thread.thread_id)
    with pytest.raises(DataCorruptionError, match='unavailable'):
        store.tasks.bind({'thread_id': thread.thread_id, 'task_id': 'new-named', 'work_kind': 'audit', 'work_name': 'same'})
    assert path.read_bytes() == before and not store.storage.task_path('new-named').exists()


# ============================================================================
# Source: agent_py_agent/tests/test_wave9_host_substrate_apis.py
# ============================================================================

# LLM: 重建服务实例强制从磁盘读回；不使用私有解密器生成预期动态码。
# 函数用途: 验证待激活和已激活JSON均无明文且可跨重载认证。
def test_totp_ciphertext_pending_active_and_reloaded_confirmation(tmp_path):
    local_user(tmp_path)
    clock = [1700000010]
    service = TotpSSOAuthService(tmp_path, clock=lambda: clock[0])
    setup = service.setup_totp('alice')
    path = tmp_path / 'local_users_auth/alice.json'
    assert setup['secret'] not in path.read_text()
    assert 'secret' not in json.loads(path.read_text())
    assert json.loads(path.read_text())['secret_ciphertext']
    service = TotpSSOAuthService(tmp_path, clock=lambda: clock[0])
    assert service.confirm_totp('alice', totp_code(setup['secret'], clock[0] // 30))['enabled']
    assert setup['secret'] not in path.read_text()
    assert 'secret' not in json.loads(path.read_text())
    clock[0] += 30
    assert TotpSSOAuthService(tmp_path, clock=lambda: clock[0]).verify_totp(
        'alice', totp_code(setup['secret'], clock[0] // 30))['enabled']
    keys = list((tmp_path / 'local_users_auth/.keys').glob('*.key'))
    assert len(keys) == 1 and len(keys[0].read_bytes()) == 32
    assert keys[0].stat().st_mode & 0o777 == 0o600
    assert keys[0].parent.stat().st_mode & 0o777 == 0o700


# LLM: 篡改真实密文/认证标签或移植不同UID，产品必须在任何计数消费前失败关闭。
# 函数用途: 防止未认证加密、跨Owner拷贝和丢失密钥后的静默重新生成。
@pytest.mark.parametrize('attack', ['ciphertext', 'tag', 'owner', 'missing_key', 'bad_key', 'linked_key'])
def test_totp_envelope_tampering_and_key_loss_fail_closed(tmp_path, attack):
    local_user(tmp_path)
    local_user(tmp_path, 'bob')
    clock = 1700000010
    service = TotpSSOAuthService(tmp_path, clock=lambda: clock)
    setup = service.setup_totp('alice')
    service.setup_totp('bob')
    path = tmp_path / 'local_users_auth/alice.json'
    row = json.loads(path.read_text())
    assert 'secret_ciphertext' in row
    keys = tmp_path / 'local_users_auth/.keys'
    key = keys / 'alice.key'
    original_key = key.read_bytes()
    if attack in ('ciphertext', 'tag'):
        raw = bytearray(base64.b64decode(row['secret_ciphertext'], altchars=b'-_', validate=True))
        raw[20 if attack == 'ciphertext' else -1] ^= 1
        row['secret_ciphertext'] = base64.urlsafe_b64encode(raw).decode()
        path.write_text(json.dumps(row))
    elif attack == 'owner':
        shutil.copyfile(path, path.with_name('bob.json'))
        shutil.copyfile(key, keys / 'bob.key')
    elif attack == 'missing_key':
        key.unlink()
    elif attack == 'bad_key':
        key.write_bytes(b'bad')
    else:
        target = tmp_path / 'external.key'
        target.write_bytes(original_key)
        key.unlink()
        key.symlink_to(target)
    with pytest.raises(HostAPIError) as error:
        TotpSSOAuthService(tmp_path, clock=lambda: clock).verify_totp(
            'bob' if attack == 'owner' else 'alice', totp_code(setup['secret'], clock // 30))
    assert error.value.code == 'STORE_UNAVAILABLE'
    assert not key.exists() if attack == 'missing_key' else True


# LLM: 旧版本记录由合成数据构造；迁移不重置防重放或epoch，错误验证码也不能再次保存明文。
# 函数用途: 验证旧认证记录首次消费时原位密文化且保留原MFA身份。
def test_totp_legacy_plaintext_is_migrated_without_resetting_epoch(tmp_path):
    local_user(tmp_path)
    clock = 1700000010
    service = TotpSSOAuthService(tmp_path, clock=lambda: clock)
    setup = service.setup_totp('alice')
    path = tmp_path / 'local_users_auth/alice.json'
    row = json.loads(path.read_text())
    row.pop('secret_ciphertext', None)
    row.update(secret=setup['secret'], enabled=True, auth_epoch='original-epoch')
    path.write_text(json.dumps(row))
    with pytest.raises(HostAPIError):
        service.verify_totp('alice', 'not-a-code')
    current = json.loads(path.read_text())
    assert setup['secret'] not in path.read_text()
    assert 'secret' not in current and current['auth_epoch'] == 'original-epoch'
    assert service.verify_totp('alice', totp_code(setup['secret'], clock // 30))['enabled']


# LLM: 独立复核指出的setup旁路；已有pending密文不能因丢钥、换钥或损坏被新设置静默覆盖。
# 函数用途: 保存并对照原记录，要求重新setup在坏密钥/封套时无写入失败。
@pytest.mark.parametrize('attack', ['missing_key', 'changed_key', 'ciphertext'])
def test_totp_pending_setup_does_not_hide_key_loss_or_corruption(tmp_path, attack):
    local_user(tmp_path)
    service = TotpSSOAuthService(tmp_path)
    service.setup_totp('alice')
    path = tmp_path / 'local_users_auth/alice.json'
    key = tmp_path / 'local_users_auth/.keys/alice.key'
    if attack == 'missing_key':
        key.unlink()
    elif attack == 'changed_key':
        key.write_bytes(b'x' * 32)
    else:
        row = json.loads(path.read_text())
        row['secret_ciphertext'] = 'invalid-envelope'
        path.write_text(json.dumps(row))
    before = path.read_bytes()
    with pytest.raises(HostAPIError) as error:
        service.setup_totp('alice')
    assert error.value.code == 'STORE_UNAVAILABLE' and path.read_bytes() == before
    if attack == 'missing_key':
        assert not key.exists()


# LLM: 使用真实Gateway授权、闭合解码和回环HTTP；摘要失败必须早于安装副作用。
# 函数用途: 两种摘要字段的坏格式及错误摘要必须在领域层统一400，不能先被schema改成另一错误码。
@pytest.mark.parametrize('field', ['sha256', 'expected_sha256'])
@pytest.mark.parametrize('value', ['', 'bad', 'g' * 64, 'a' * 63, 'a' * 65, '0' * 64])
def test_host_package_sha256_mismatch_code(production_host, field, value):
    status, body = http_request(production_host[0], production_host[1], {
        'schema': 'plugin_host_call.v2', 'action': 'plugins.packages.install',
        'idempotency_key': 'round3-sha256-mismatch',
        'payload': {'package_base64': base64.b64encode(_bundle()).decode(), field: value}})
    assert status == 400
    assert body['error']['code'] == 'PACKAGE_SHA256_MISMATCH'


# LLM: 真实HTTP透传同一沙箱执行器，诊断不写只读Owner；不mock执行结果或外层Seatbelt判断。
# 函数用途: 核验默认零真实调用，显式许可只调用一次且返回真实stderr/输出，不写正式run。
@pytest.mark.parametrize('mode', ['deterministic_script', 'script'])
def test_host_debug_default_preview_and_explicit_execution(production_host, monkeypatch, mode):
    home = production_host[2].home_dir
    calls = observe_script_runs(monkeypatch)
    script(home, 'marker', 'import sys; print("MARKER_EXECUTED", file=sys.stderr); print(\'{"value":3}\')')
    WorkflowStore(home).save_workflow(definition({'step_id': 'one', 'mode': mode, 'script_id': 'marker'}))
    data = {'workflow_id': 'debug-flow', 'step_id': 'one', 'mock_inputs': {'n': 4}}
    preview = production_call(production_host, 'workflows.steps.debug', data)
    assert calls == [] and 'MARKER_EXECUTED' not in '\n'.join(preview['logs'])
    assert preview['dry_run'] and preview['side_effects_blocked']
    assert preview['rendered_input'] == {'n': 4} and preview['condition_branch'] is None
    actual = production_call(production_host, 'workflows.steps.debug', {**data, 'allow_side_effects': True})
    assert calls == ['marker'] and 'MARKER_EXECUTED' in '\n'.join(actual['logs'])
    assert actual['output_data'] == {'value': 3} and actual['status'] == 'succeeded'
    assert not actual['dry_run'] and not (home / 'data/workflow_runs').exists()


# LLM: 无live LLM时只返回可辨认的确定性mock预览，不能把echo说成模型已执行。
# 函数用途: 验证Host分析节点默认可调试、可mock，且不发外部请求。
@pytest.mark.parametrize('mode', ['llm_prompt', 'llm_skill_analysis'])
def test_host_debug_llm_fallback_is_safe_deterministic_and_mock_capable(production_host, mode):
    home = production_host[2].home_dir
    WorkflowStore(home).save_workflow(definition({'step_id': 'one', 'mode': mode,
        'config': {'mock_output': {'verdict': 'fixture'}}}))
    result = production_call(production_host, 'workflows.steps.debug',
        {'workflow_id': 'debug-flow', 'step_id': 'one', 'mock_inputs': {'n': 2}})
    assert result['status'] == 'succeeded' and result['dry_run'] and result['side_effects_blocked']
    assert result['output_data'] == {'mock': True, 'llm_called': False, 'output': {'verdict': 'fixture'}}
    assert result['rendered_input'] == {'n': 2} and result['condition_branch'] is None
    assert not (home / 'data/workflow_runs').exists()


# LLM: 经真实Host在临时Owner安装合成包；两个摘要别名及大小写必须校验同一原字节，不放宽安装门。
# 函数用途: 验证正确SHA256用任一或两个字段都能通过，并读回实际安装摘要。
@pytest.mark.parametrize('field', ['sha256', 'expected_sha256', 'both'])
def test_host_package_digest_aliases_accept_valid_hex(production_host, field):
    raw = _bundle()
    digest = hashlib.sha256(raw).hexdigest()
    fields = {'sha256': digest, 'expected_sha256': digest.upper()} if field == 'both' else {field: digest.upper()}
    result = production_call(production_host, 'plugins.packages.install',
        {'package_base64': base64.b64encode(raw).decode(), **fields}, key='round3-valid-digest')
    assert result['sha256'] == digest


# LLM: 两摘要字段同时提供时逐个校验；正确字段不能覆盖另一字段的坏格式或错误摘要，失败早于安装。
# 函数用途: 验证双字段冲突始终返回400及PACKAGE_SHA256_MISMATCH，不能靠参数顺序绕过。
@pytest.mark.parametrize('bad', ['0' * 64, 'bad'])
@pytest.mark.parametrize('bad_field', ['sha256', 'expected_sha256'])
def test_host_package_digest_aliases_cannot_override_each_other(production_host, bad_field, bad):
    raw = _bundle()
    fields = dict.fromkeys(('sha256', 'expected_sha256'), hashlib.sha256(raw).hexdigest())
    fields[bad_field] = bad
    status, body = http_request(production_host[0], production_host[1], {
        'schema': 'plugin_host_call.v2', 'action': 'plugins.packages.install', 'idempotency_key': 'round3-digest-conflict',
        'payload': {'package_base64': base64.b64encode(raw).decode(), **fields}})
    assert status == 400 and body['error']['code'] == 'PACKAGE_SHA256_MISMATCH'


# LLM: 从真实Host入口核对条件/审批/通知的新旧DTO及副作用门；仅临时Owner，不把响应当实际外部送达。
# 函数用途: 确认新增控制节点可安全预览、显式执行且不创建正式工作流运行账。
@pytest.mark.parametrize('mode', ['condition_branch', 'approval_wait', 'notification'])
def test_host_debug_contract_aliases_and_new_control_modes(production_host, mode):
    home = production_host[2].home_dir
    WorkflowStore(home).save_workflow(definition({'step_id': 'one', 'mode': mode,
        'config': {'expression': "inputs['n'] > 3", 'title': 'fixture', 'message': 'notice'}}))
    data = {'workflow_id': 'debug-flow', 'step_id': 'one', 'mock_inputs': {'n': 4}}
    preview = production_call(production_host, 'workflows.steps.debug', data)
    assert_debug_contract(preview, mode)
    assert preview['rendered_input'] == {'n': 4}
    if mode == 'condition_branch':
        assert preview['condition_branch'] is True and preview['output'] == {'matched': True}
        assert not preview['side_effects_suppressed'] and not preview['dry_run']
    else:
        assert preview['side_effects_suppressed'] and preview['dry_run'] and preview['output'] == {}
        result = production_call(production_host, 'workflows.steps.debug', {**data, 'allow_side_effects': True})
        assert_debug_contract(result, mode)
        assert result['status'] == 'succeeded' and not result['side_effects_suppressed']
    assert not (home / 'data/workflow_runs').exists()
