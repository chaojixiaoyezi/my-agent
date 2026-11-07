"""新进程跑真实 count=0 CLI、HTTP 与正常停止，导入集合来自实际包对象。"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize('legacy', [False, True])
def test_count0_serves_http_without_offline_migration_modules(tmp_path, monkeypatch, legacy):
    from agent_py_agent.agent.gateway_parts.legacy_mp_migration import run_single_process_migration
    from agent_py_agent.tests.test_s2w6_single_process_migration import legacy_home

    paths = None
    if legacy:
        paths = legacy_home(tmp_path, monkeypatch)
        assert run_single_process_migration(paths, dry_run=False)['state'] == 'complete'
    home = tmp_path / 'home'
    project = tmp_path / 'project'
    project.mkdir()
    config = tmp_path / 'agent.yaml'
    config.write_text(f'model_backend: echo\nmy_agent_home: "{home}"\ngateway_worker_process_count: 0\n')
    report = tmp_path / 'closure.json'
    script = r'''
import builtins, http.client, json, os, socket, sys
from argparse import Namespace
from pathlib import Path
seen = set()
original_import = builtins.__import__
def trace(name, *args, **kwargs):
    value = original_import(name, *args, **kwargs)
    seen.update(sys.modules)
    return value
builtins.__import__ = trace
from agent_py_agent.agent.gateway_parts import http_service
from agent_py_agent.agent.gateway_parts.legacy_mp_migration import migration_module_names
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.cli import gateway_process as api, gateway_coordinated_run
config, project, report, legacy_root = map(Path, sys.argv[1:])
with socket.socket() as reserve:
    reserve.bind(('127.0.0.1', 0))
    port = reserve.getsockname()[1]
with config.open('a') as output:
    output.write(f'gateway_port: {port}\n')
if str(legacy_root) != '.':
    gateway_coordinated_run._cold_gateway_paths = lambda _: gateway_paths_from_root(legacy_root)
    real_paths = api.gateway_paths
    api.gateway_paths = lambda agent: gateway_paths_from_root(legacy_root)
observed = []
def observe(context):
    server = http_service._server_instance
    connection = http.client.HTTPConnection('127.0.0.1', server.server.server_address[1], timeout=5)
    try:
        connection.request('GET', '/status', headers={'X-Gateway-Token': server._local_client_credential})
        response = connection.getresponse()
        observed.append({'status': response.status, 'body': json.loads(response.read()),
                         'count': context.coordinator_worker_count})
    finally:
        connection.close()
    api._write_gateway_stop_request(context.paths, reason='closure check', target_pid=os.getpid())
    return {'stop_request': api.read_json_file(context.paths.stop_request)}
api._run_gateway_service_loop = observe
rc = api.cmd_gateway_run(Namespace(config=config, workspace_root=str(project)))
offline = migration_module_names()
loaded = sorted(name for name in sys.modules if name in offline)
missing_sources = sorted(name for name, module in tuple(sys.modules.items())
    if name.startswith('agent_py_agent.agent.gateway_parts.multiprocess_')
    and getattr(module, '__file__', None) and not Path(module.__file__).exists())
report.write_text(json.dumps({'rc': rc, 'observed': observed, 'offline_loaded': loaded,
                             'missing_sources': missing_sources, 'imports': sorted(seen)}))
assert rc == 0, rc
assert observed and observed[0]['status'] == 200 and observed[0]['count'] == 0, observed
assert not loaded and not missing_sources, (loaded, missing_sources)
'''
    env = {**os.environ, 'HOME': str(home), 'MY_AGENT_HOME': str(home), 'PYTHONPATH': '.:agent_py_agent'}
    result = subprocess.run([sys.executable, '-c', script, str(config), str(project), str(report),
                             str(paths.root) if paths else '.'], env=env, capture_output=True, text=True, timeout=55)
    assert result.returncode == 0, (result.stdout, result.stderr, report.read_text() if report.exists() else '')
    row = json.loads(report.read_text())
    assert row['rc'] == 0 and row['offline_loaded'] == [] and row['missing_sources'] == []
    assert row['observed'][0]['status'] == 200
