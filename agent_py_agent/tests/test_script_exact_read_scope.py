"""脚本须消费宿主exact读取合同，普通policy模式与Full权限保持原边界。"""
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.agent_core.self_map_bootstrap import _script_runtime_options
from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, ensure_owner_home
from agent_py_agent.agent.workflows.script_runner import ScriptRunner, _sdk_native_read_files


def _case(tmp_path, *, full=False):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    agent = SimpleAgent(AgentConfig(my_agent_home=str(tmp_path / 'home'), memory_path='memory.jsonl',
        orphan_supervision_interval_seconds=0, access_mode='full-access' if full else 'workspace-only'), workspace)
    agent.effective_workspace_root = workspace
    owner = Path(agent.home_paths.owner_home_dir)
    foreign = ensure_owner_home(agent.home_paths.root, OwnerIdentity.provider_user('feishu', 'synthetic')).home_dir
    paths = {'allowed': workspace / 'allowed.txt', 'blocked': workspace / 'blocked.txt',
        'external': tmp_path / 'external.txt', 'foreign': foreign / 'synthetic.txt',
        'host_source': Path(agent.home_paths.root) / 'secrets/synthetic.txt'}
    for path in paths.values():
        path.parent.mkdir(exist_ok=True)
        path.write_text('SYNTHETIC ONLY')
    link = workspace / 'link.txt'
    link.symlink_to(paths['external'])
    paths['link'] = link
    code = f'''import json
from pathlib import Path
result={{}}
for name,path in {str({key: str(value) for key, value in paths.items()})} .items():
    try:
        Path(path).read_text()
        result[name]=True
    except PermissionError:
        result[name]=False
print(json.dumps({{"result":result}}))
'''
    ScriptStore(owner, workspace_root=workspace).save_script(
        AssetDescriptor('exact-scope', 'script', 'exact-scope', io_contract={'inputs': [], 'outputs': ['result']}), code)
    return agent, owner, workspace, paths


@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('full', [False, True])
def test_exact_script_scope_rejects_outside_and_links_without_breaking_allowed_file(tmp_path, native, full):
    agent, owner, workspace, paths = _case(tmp_path, full=full)
    agent._current_run_params = RunParams(task_attributes={}, write_boundary={
        'read_scope_mode': 'exact', 'allowed_read_roots': [str(paths['allowed'])], 'allowed_write_roots': []})
    options = _script_runtime_options(agent)
    assert options['read_scope_mode'] == 'exact'
    result = ScriptRunner(owner, workspace_root=workspace, use_sandbox=native, **options).run_script('exact-scope')
    assert result.ok, result.stderr_preview
    assert result.output_data['result'] == {'allowed': True, 'blocked': False, 'external': False,
                                          'foreign': False, 'host_source': False, 'link': False}


@pytest.mark.parametrize('full', [False, True])
def test_policy_mode_retains_public_external_and_explicit_full_behavior(tmp_path, full):
    agent, owner, workspace, _ = _case(tmp_path, full=full)
    agent._current_run_params = RunParams(task_attributes={})
    options = _script_runtime_options(agent)
    assert options['read_scope_mode'] == 'policy'
    # 单独验证原Python守卫的Full分支；原生sandbox另有跨owner硬墙，不把两种边界混为同一许可。
    outcome = ScriptRunner(owner, workspace_root=workspace, use_sandbox=False, **options).run_script('exact-scope')
    assert outcome.ok, outcome.stderr_preview
    results = outcome.output_data['result']
    assert results['external'] and results['link']
    assert results['foreign'] is full
    assert not results['host_source']


def test_registered_concurrent_script_calls_keep_each_exact_snapshot(tmp_path):
    agent, _, _, paths = _case(tmp_path)

    def run(key):
        agent._current_run_params = RunParams(task_attributes={}, write_boundary={
            'read_scope_mode': 'exact', 'allowed_read_roots': [str(paths[key])], 'allowed_write_roots': []})
        outcome = agent.tools.tools['scripts'].execute({'action': 'run', 'script_id': 'exact-scope'})
        assert outcome.ok, outcome.output
        return json.loads(outcome.output)['output_data']['result']

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(run, ['allowed', 'blocked']))
    assert first['allowed'] and not first['blocked']
    assert second['blocked'] and not second['allowed']
    assert not any(first[key] or second[key] for key in ('external', 'foreign', 'host_source', 'link'))


def test_sdk_native_grants_are_only_existing_file_leaves():
    import sys
    leaves = _sdk_native_read_files(sys.executable)
    assert all(path.is_file() and not path.is_dir() for path in leaves)
    if sys.platform != 'darwin':
        assert leaves == ()


def test_sdk_dependency_probe_never_inherits_host_plugins_or_grants_unloaded_link(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.workflows import script_runner

    native = tmp_path / 'runtime.dylib'
    native.write_bytes(b'synthetic')
    alias = tmp_path / 'runtime-alias.dylib'
    alias.symlink_to(native)
    unrelated = tmp_path / 'customer.txt'
    unrelated.write_bytes(b'synthetic customer data')
    calls = []

    def run(argv, **options):
        calls.append((argv, options))
        if '-c' in argv:
            return SimpleNamespace(returncode=0, stdout=json.dumps([str(native)]).encode(), stderr=b'')
        body = f'library:\n\t{alias} (compatibility version 1.0.0)\n\t{unrelated} (compatibility version 1.0.0)\n'
        return SimpleNamespace(returncode=0, stdout=body.encode(), stderr=b'')

    monkeypatch.setattr(script_runner.sys, 'platform', 'darwin')
    monkeypatch.setattr(script_runner.subprocess, 'run', run)
    _sdk_native_read_files.cache_clear()
    try:
        assert _sdk_native_read_files('/synthetic/python') == (native, alias)
        assert '-I' in calls[0][0] and '-S' in calls[0][0]
        assert all(set(options['env']) == {'PATH'} for _, options in calls)
        assert unrelated not in _sdk_native_read_files('/synthetic/python')
        assert len(calls) == 2
    finally:
        _sdk_native_read_files.cache_clear()


@pytest.mark.skipif(sys.platform not in {'darwin', 'linux'}, reason='原生脚本沙箱只支持 macOS/Linux')
@pytest.mark.sandbox_capability(*(('nested_sandbox_exec',) if sys.platform == 'darwin' else ('linux_bwrap_network',)))
def test_real_host_api_asset_execution_keeps_exact_roots_including_external_tmp(tmp_path):
    import hashlib
    import tempfile
    from types import SimpleNamespace

    from agent_py_agent.tests.test_plugin_host_gateway_composition import (
        production_call,
        production_host,
    )

    code = '''import json,sys
from pathlib import Path
args=json.load(sys.stdin)
result={}
for name,path in args['targets'].items():
    try:
        Path(path).read_text()
        result[name]=True
    except (PermissionError,FileNotFoundError):
        result[name]=False
Path(args['out']).write_text(json.dumps(result))
print(json.dumps({'ok':True}))
'''
    fixture = production_host.__wrapped__(tmp_path, SimpleNamespace(param={'asset_refs': [
        {'kind': 'script', 'id': 'script-one', 'revision': 1,
         'sha256': hashlib.sha256(code.encode()).hexdigest()}]}))
    host = next(fixture)
    try:
        production_call(host, 'users.local.create', {'user_id': 'alice'}, key='exact_alice_create_01', status=201)
        thread = production_call(host, 'conversations.threads.create', {}, user='alice', key='exact_thread_create_01', status=201)
        home = host[2].root / 'owners/local/alice'
        workspace = home / 'workspace'
        workspace.mkdir(exist_ok=True)
        allowed = workspace / 'allowed.txt'
        allowed.write_text('SYNTHETIC ONLY')
        foreign = host[2].root / 'owners/local/bob/workspace/synthetic.txt'
        foreign.parent.mkdir(parents=True, exist_ok=True)
        foreign.write_text('SYNTHETIC ONLY')
        ScriptStore(home, extra_roots=()).save_script(
            AssetDescriptor('script-one', 'script', 'script-one', metadata={'revision': 1}), code)
        with tempfile.TemporaryDirectory(prefix='hostapi-exact-external-') as external_root:
            external = Path(external_root) / 'synthetic.txt'
            external.write_text('SYNTHETIC ONLY')
            link = workspace / 'external-link.txt'
            link.symlink_to(external)
            out = workspace / 'observed.json'
            accepted = production_call(host, 'execute.script', {'thread_id': thread['thread_id'],
                'script_id': 'script-one', 'expected_revision': 1, 'inputs': {'out': str(out),
                    'targets': {'allowed': str(allowed), 'external': str(external),
                                'link': str(link), 'foreign': str(foreign)}}},
                user='alice', key='exact_execute_script_01', status=202)
            poll = production_call(host, 'conversations.requests.poll', {'request_id': accepted['request_id']}, user='alice')
            assert poll['request']['state'] == 'succeeded', poll
            assert json.loads(out.read_text()) == {'allowed': True, 'external': False, 'link': False, 'foreign': False}
            assert external.read_text() == 'SYNTHETIC ONLY'
    finally:
        next(fixture, None)


@pytest.mark.parametrize('bad_root', ['foreign', 'app_home', 'app_parent', 'host_source'])
def test_exact_native_roots_cannot_reopen_owner_wall_before_child_process(tmp_path, bad_root):
    agent, owner, workspace, paths = _case(tmp_path)
    agent._current_run_params = RunParams(task_attributes={}, write_boundary={})
    options = _script_runtime_options(agent)
    options['read_scope_mode'] = 'exact'
    selected = {'foreign': paths['foreign'].parent, 'app_home': Path(agent.home_paths.root),
                'app_parent': tmp_path, 'host_source': paths['host_source']}
    options['public_read_roots'] = (selected[bad_root],)
    with pytest.raises(ValueError, match='SCRIPT_EXACT_READ_ROOT_OWNER_DENIED'):
        ScriptRunner(owner, workspace_root=workspace, **options)
