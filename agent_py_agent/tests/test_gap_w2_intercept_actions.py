"""W2 命令策略：真实 V13 调用、事务回执、owner 隔离及只读缓存回归。"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.business_spaces import gap158_w2_policy_store as policy
from agent_py_agent.agent.business_spaces.v13_store import V13Store
from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.agent.gateway_parts import business_api_v13 as v13
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.plugin_host.model_settings_v2_handlers import ModelSettingsV2Handlers
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    resolve_owner_home,
)
from agent_py_agent.tests.test_decision_subagent import prepared as prepared


class Harness:
    """只组装真实端口；测试不替换业务处理器或伪造返回 DTO。"""

    def __init__(self, root, name='main'):
        paths = home_paths(root)
        identity = OwnerIdentity.local_main() if name == 'main' else OwnerIdentity.local_user(name)
        paths = home_paths_with_owner(paths, resolve_owner_home(paths.root, identity))
        self.host = SimpleNamespace(home_paths=paths, workspace_root=paths.owner_home_dir / 'workspace',
            effective_workspace_root=paths.owner_home_dir / 'workspace',
            config=SimpleNamespace(business_spaces_enabled=True), business_v13_clock=lambda: 1794000000.0)
        self.context = api.bind_business_context(self.host, subject_id='w2-tester', authorize=lambda action, facts: True)
        self.services = api.BusinessServices(self.host)

    @property
    def home(self):
        return self.services.owner_home

    def call(self, action, params=None, *, key=None, revision=None):
        return self.services.invoke(action, params or {}, self.context,
            idempotency_key=key or 'w2-' + uuid4().hex, expected_revision=revision, request_id='w2-' + uuid4().hex)

    def invocation(self, action, params=None, *, key=None, revision=None):
        return v13.V13Call(action, params or {}, 'w2-direct', self.services.owner_id,
            self.context.subject_id, self.context, self.services, self.home,
            key or 'w2-' + uuid4().hex, revision, lambda: 1794000000.0)


@pytest.fixture
def w2(tmp_path, monkeypatch):
    # W0 尚未合入时，仅测试环境通过真实 loader 装入分片；产品不自注册。
    module = 'agent_py_agent.agent.gateway_parts.business_api_v13_gap158_w2'
    modules = v13.V13_SLICE_MODULES if module in v13.V13_SLICE_MODULES else (*v13.V13_SLICE_MODULES, module)
    tables = v13._load_registry(modules)
    for name, value in zip(('_ACTIONS', 'ALIASES', 'V13_DEFS', 'JOB_KINDS', 'COLLECTIONS'), tables):
        monkeypatch.setattr(v13, name, value)
    monkeypatch.setenv('HOME', str(tmp_path / 'os-home'))
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'agent-home'))
    policy._snapshot_cache.clear()
    policy._warning_generations.clear()
    return Harness(tmp_path / 'root')


def error_code(code, function, *args, **kwargs):
    with pytest.raises((BusinessError, HostAPIError)) as caught:
        function(*args, **kwargs)
    assert caught.value.code == code
    return caught.value


@pytest.fixture
def native_owner_runtime(tmp_path, monkeypatch):
    """真实 HTTP/owner 池与原租约；资源完整退出后核对描述符平衡。"""
    from agent_py_agent.agent.gateway_parts.request_worker import _owner_pool
    from agent_py_agent.tests.test_v13_w0_harness import v13_call, v13_native
    fd_root = '/dev/fd' if os.path.isdir('/dev/fd') else '/proc/self/fd'

    def descriptors():
        result = set()
        for name in os.listdir(fd_root):
            try:
                info = os.fstat(int(name))
            except OSError:
                continue  # listdir 自己的临时目录描述符已经关闭。
            result.add((int(name), info.st_dev, info.st_ino, info.st_mode))
        return result
    generator = v13_native.__wrapped__(tmp_path, monkeypatch)
    server, base = native = next(generator)
    base.config.side_questions_enabled = True
    base.config.acp_bridge_enabled = True
    identity = OwnerIdentity.provider_user('feishu', 'business-alice')
    pool = _owner_pool(base)
    host = pool.get(identity)
    workspace = host.home_paths.owner_home_dir / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    host.config.workspace_root = str(workspace)
    thread = host.conversation_store.threads.create(title='原生请求', owner_id=host.home_paths.owner_id,
        owner_home=str(host.home_paths.owner_home_dir), workspace_root=str(workspace), cwd=str(workspace))
    leases, acquire = [], pool.acquire_pinned

    @contextmanager
    def tracked(owner, **kwargs):
        with acquire(owner, **kwargs) as agent:
            leases.append(('acquired', agent))
            try:
                yield agent
            finally:
                leases.append(('released', agent))

    monkeypatch.setattr(pool, 'acquire_pinned', tracked)

    def call(action, params=None, *, key=None, revision=None, user='business-alice'):
        if key is None and api.resolve_business_action_spec(action).metadata['effect'] != 'read':
            key = uuid4().hex
        key = hashlib.sha256(key.encode()).hexdigest() if key is not None else None
        return v13_call(native, action, params or {}, user=user,
            idempotency_key=key, expected_revision=revision)

    # 只核调用新增且收尾后仍存活的描述符；宿主自身监听/事件句柄的关闭不算负向泄漏。
    before = descriptors()
    try:
        yield SimpleNamespace(native=native, server=server, base=base, host=host, pool=pool,
            identity=identity, thread=thread, workspace=workspace, call=call, leases=leases)
    finally:
        generator.close()
        assert not pool._pins
        assert sum(kind == 'acquired' for kind, _ in leases) == sum(kind == 'released' for kind, _ in leases)
        fd_delta = len(descriptors() - before)
        assert fd_delta == 0, 'fd_delta must be zero after native teardown'


def native_pending(runtime, turn_id='native-turn'):
    """通过原任务、子代理及审批发布入口准备真实 pending。"""
    import time

    from agent_py_agent.agent.business_spaces import gap158_w2_judge as judge
    from agent_py_agent.agent.contracts.tool_approval import build_tool_approval_request
    from agent_py_agent.agent.conversation.agent_tool_approval import publish_agent_tool_approval
    from agent_py_agent.agent.tooling.runtime_contracts import ToolCall

    host, thread = runtime.host, runtime.thread
    root_id = 'approval-root-' + uuid4().hex
    host.conversation_store.tasks.bind({'thread_id': thread.thread_id, 'task_id': root_id,
        'goal': '确认工具请求', 'status': 'active', 'now': time.time()})
    child = host.subagents.create_run(goal='确认工具请求', root_id=root_id, parent_id=root_id)
    child = host.subagents.lifecycle.prepare_runner_attempt(child.id)
    tool = ToolCall(call_id='native-call', tool_name='run_command', arguments={'command': 'git status'},
        source_protocol='native', schema_hash='sha256:native', run_id=child.id,
        turn_id=turn_id, attempt_id='native-attempt')
    request = SimpleNamespace(call=tool, owner_scope_root=str(host.home_paths.owner_home_dir))
    policy = SimpleNamespace(runtime_policy=SimpleNamespace(
        approval_policy=SimpleNamespace(owner_grant_parameters=())))
    row = judge.add_pending(request, policy, SimpleNamespace(decision='ask', rule_id='native-rule'))
    approval = build_tool_approval_request(tool, request_id=tool.turn_id, round_number=0,
        call_index=0, description='确认工具请求')
    handle = publish_agent_tool_approval(host, run_id=child.id,
        thread_id=child.agent_thread_id, request_value=approval)
    return row, handle


@pytest.mark.parametrize('decision', ['approved', 'denied'])
def test_native_pending_decision_uses_original_owner_runtime_and_replays(native_owner_runtime, decision):
    from agent_py_agent.agent.common.json_io import read_json_object_report

    runtime = native_owner_runtime
    row, handle = native_pending(runtime)
    status, listed = runtime.call('intercept.pending.list')
    assert status == 200 and listed['data']['items'][0]['approval_id'] == row['approval_id']
    params = {'approval_id': row['approval_id'], 'decision': decision}
    status, foreign = runtime.call('intercept.pending.decide', params, key='foreign-decision', user='business-bob')
    assert status == 404 and foreign['error']['code'] == 'RESOURCE_NOT_FOUND'
    status, result = runtime.call('intercept.pending.decide', params, key='native-decision')
    assert status == 200 and result['data']['state'] == decision, result
    canonical = read_json_object_report(handle.path, context='r1.native.approval').payload
    assert canonical['status'] == 'decided' and canonical['decision']['decision'] == decision
    replay_status, replay = runtime.call('intercept.pending.decide', params, key='native-decision')
    assert replay_status == 200 and replay['data'] == result['data']
    assert [kind for kind, _ in runtime.leases] == ['acquired', 'released']
    assert not runtime.pool._pins


def test_native_pending_acquisition_failure_preserves_pending_and_can_retry(native_owner_runtime, monkeypatch):
    from agent_py_agent.agent.common.json_io import read_json_object_report

    runtime = native_owner_runtime
    row, handle = native_pending(runtime)

    def unavailable(*args, **kwargs):
        raise RuntimeError('owner runtime unavailable')

    params = {'approval_id': row['approval_id'], 'decision': 'approved'}
    with monkeypatch.context() as patch:
        patch.setattr(runtime.pool, 'acquire_pinned', unavailable)
        status, body = runtime.call('intercept.pending.decide', params, key='retry-decision')
    assert status == 503 and body['error']['code'] == 'HOST_NOT_READY', body
    assert V13Store(runtime.host.home_paths.owner_home_dir).get('policy_pending_index', row['approval_id'])['state'] == 'pending'
    assert read_json_object_report(handle.path, context='r1.native.pending').payload['status'] == 'pending'
    status, body = runtime.call('intercept.pending.decide', params, key='retry-decision')
    assert status == 200 and body['data']['state'] == 'approved', body


def test_stream_steer_native_approval_stop_new_next_turn(prepared, native_owner_runtime, monkeypatch):
    from agent_py_agent.agent.common.json_io import read_json_object_report
    from agent_py_agent.cli.chat_parts import tui_actions
    from agent_py_agent.tests.test_gap_w10_headless_sequence import (
        test_spawn_compact_stream_steer_stop_new_next_turn,
    )

    runtime = native_owner_runtime
    row, handle = native_pending(runtime, turn_id='gwreq-turn-1')
    original = tui_actions._tui_submit_active_turn_input
    views, decisions = [], []

    def with_approval(params, *args, **kwargs):
        accepted = original(params, *args, **kwargs)
        if accepted:
            view = params.tui_runtime
            views.append(view)
            turn = view._turns['chat-turn-1']

            def decide(approval, decision):
                assert approval.permission_id == row['approval_id']
                status, body = runtime.call('intercept.pending.decide', {'approval_id': approval.permission_id,
                    'decision': decision.decision}, key='headless-approval')
                assert status == 200, body
                decisions.append(body['data']['state'])

            turn.configure_gateway_permission_sink(decide)
            assert turn.on_gateway_event({'kind': 'permission_requested', 'permission': handle.request.to_dict()})
            assert view.store.snapshot().permission.permission_id == row['approval_id']
            assert view.resolve_permission(row['approval_id'], 'approved')
        return accepted

    monkeypatch.setattr(tui_actions, '_tui_submit_active_turn_input', with_approval)
    test_spawn_compact_stream_steer_stop_new_next_turn(prepared, monkeypatch, 'before_stop')
    assert decisions == ['approved'] and views[0].store.snapshot().permission is None
    assert read_json_object_report(handle.path, context='r1.headless.canonical').payload['status'] == 'decided'
    assert not runtime.pool._pins


def create_rule(w2, **changes):
    return w2.call('intercept.rules.create', {'name': '命令规则', 'tool': 'run_command',
        'decision': 'ask', **changes})


def test_real_crud_whole_collection_cas_and_durable_replay(w2):
    assert w2.call('intercept.rules.list')['revision'] == 0
    params = {'name': '命令规则', 'tool': 'run_command', 'decision': 'ask'}
    created = w2.call('intercept.rules.create', params, key='w2-create-idempotent')
    assert created['revision'] == 1
    assert created['rule']['priority'] == 0 and created['rule']['enabled'] is True
    assert created['rule']['created_at'] == created['rule']['updated_at'] == 1794000000.0
    assert w2.call('intercept.rules.create', params, key='w2-create-idempotent') == created
    error_code('IDEMPOTENCY_CONFLICT', w2.call, 'intercept.rules.create',
               {**params, 'decision': 'deny'}, key='w2-create-idempotent')
    rule_id = created['rule']['rule_id']
    updated = w2.call('intercept.rules.update', {'rule_id': rule_id, 'priority': 19}, revision=1)
    assert updated['revision'] == 2 and updated['rule']['priority'] == 19
    error_code('REVISION_CONFLICT', w2.call, 'intercept.rules.update',
               {'rule_id': rule_id, 'priority': 20}, revision=1)
    toggled = w2.call('intercept.rules.toggle', {'rule_id': rule_id, 'enabled': False}, revision=2)
    assert toggled['revision'] == 3 and toggled['rule']['enabled'] is False
    listed = w2.call('intercept.rules.list', {'enabled': False})
    assert listed['revision'] == 3 and listed['items'] == [toggled['rule']]
    assert w2.call('intercept.rules.list', {'enabled': True})['items'] == []
    deleted = w2.call('intercept.rules.delete', {'rule_id': rule_id}, revision=3, key='w2-delete-idempotent')
    assert deleted == {'deleted': True, 'revision': 4}
    assert w2.call('intercept.rules.delete', {'rule_id': rule_id}, revision=3,
                   key='w2-delete-idempotent') == deleted
    error_code('RESOURCE_NOT_FOUND', w2.call, 'intercept.rules.update', {'rule_id': rule_id}, revision=0)
    assert w2.call('intercept.rules.list')['items'] == []


def test_cursor_is_bound_to_snapshot_owner_and_filters(w2):
    first = create_rule(w2, name='one')
    second = create_rule(w2, name='two')
    page = w2.call('intercept.rules.list', {'limit': 1})
    assert page['revision'] == 2 and page['coverage'] == 'partial' and page['next_cursor']
    final = w2.call('intercept.rules.list', {'limit': 1, 'cursor': page['next_cursor']})
    assert final['next_cursor'] == '' and final['coverage'] == 'complete'
    assert {row['rule_id'] for row in page['items'] + final['items']} == {
        first['rule']['rule_id'], second['rule']['rule_id']}
    error_code('CURSOR_EXPIRED', w2.call, 'intercept.rules.list',
               {'cursor': page['next_cursor'], 'tool': 'read_file'})
    create_rule(w2, name='three')
    error_code('CURSOR_EXPIRED', w2.call, 'intercept.rules.list', {'cursor': page['next_cursor']})


def test_owner_and_call_identity_cannot_be_supplied_by_json(w2, tmp_path):
    rule = create_rule(w2)
    other = Harness(tmp_path / 'root', 'other')
    assert other.call('intercept.rules.list')['items'] == []
    error_code('RESOURCE_NOT_FOUND', other.call, 'intercept.rules.update',
               {'rule_id': rule['rule']['rule_id']}, revision=1)
    error_code('INVALID_SCHEMA', w2.call, 'intercept.rules.create',
               {'name': '伪造', 'tool': 'x', 'decision': 'allow', 'owner_id': other.services.owner_id})
    forged = replace(w2.invocation('intercept.rules.list'), owner_id=other.services.owner_id)
    error_code('RESOURCE_NOT_FOUND', policy.dispatch, forged)


def test_config_defaults_and_sets_do_not_load_judge_or_run_a_model(w2, monkeypatch):
    real_import = importlib.import_module

    def no_judge(name, *args, **kwargs):
        assert not name.endswith('gap158_w2_judge')
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, 'import_module', no_judge)
    assert w2.call('intercept.tool_config.get', {'tool': 'run_command'}) == {
        'config': {'tool': 'run_command', 'default_decision': 'inherit', 'judge_enabled': False}, 'revision': 0}
    assert w2.call('intercept.judge.get') == {
        'config': {'enabled': False, 'max_calls_per_hour': 0, 'timeout_seconds': 30}, 'revision': 0}
    assert not (w2.home / 'data' / 'business_v13').exists()
    params = {'tool': 'run_command', 'default_decision': 'deny', 'judge_enabled': False}
    saved = w2.call('intercept.tool_config.set', params, revision=0, key='w2-tool-config-replay')
    assert saved == {'config': params, 'revision': 1}
    assert w2.call('intercept.tool_config.set', params, revision=0, key='w2-tool-config-replay') == saved
    assert w2.call('intercept.tool_config.get', {'tool': 'run_command'}) == saved
    config = {'enabled': False, 'max_calls_per_hour': 0, 'timeout_seconds': 30}
    assert w2.call('intercept.judge.set', config, revision=0) == {'config': config, 'revision': 1}
    error_code('REVISION_CONFLICT', w2.call, 'intercept.judge.set', config, revision=0)


def test_judge_profile_reference_uses_frozen_real_model_catalog(w2):
    handlers = ModelSettingsV2Handlers(w2.host.home_paths)
    catalog = handlers.save_provider({'provider_id': 'w2-test', 'display_name': '只读目录测试',
        'api_base': 'https://unused.invalid/v1', 'models': [{'model_name': 'test-model',
        'model_backend': 'openai_compatible', 'capability': 'agentic'}]})
    profile_id = catalog['providers'][0]['models'][0]['profile_id']
    config = {'enabled': True, 'max_calls_per_hour': 1, 'timeout_seconds': 2, 'model_profile_id': profile_id}
    saved = w2.call('intercept.judge.set', config, revision=0, key='w2-judge-profile-replay')
    assert saved['config'] == config and saved['revision'] == 1
    error_code('RESOURCE_NOT_FOUND', w2.call, 'intercept.judge.set',
               {**config, 'model_profile_id': 'nonexistent-profile'}, revision=1)
    assert w2.call('intercept.judge.get') == saved
    handlers.delete_provider_or_profile({'profile_id': profile_id})
    assert w2.call('intercept.judge.set', config, revision=0, key='w2-judge-profile-replay') == saved
    error_code('RESOURCE_NOT_FOUND', w2.call, 'intercept.judge.set', config, revision=1)


def test_configured_cap_is_live_and_counts_the_whole_collection(w2, monkeypatch):
    name = 'agent_py_agent.agent.business_spaces.gap158_limits'
    monkeypatch.setitem(sys.modules, name, SimpleNamespace(POLICY_RULES_MAX_PER_OWNER=1,
        SCOPE_RULES_MAX_PER_OWNER=1, SCOPE_TASK_RULES_MAX_PER_TASK=1))
    create_rule(w2)
    error = error_code('LIMIT_EXCEEDED', create_rule, w2)
    assert error.details['limit'] == 1
    assert w2.call('intercept.rules.list')['revision'] == 1


def test_concurrent_create_returns_its_own_committed_revision(w2):
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda name: create_rule(w2, name=name), ['one', 'two']))
    assert {result['revision'] for result in results} == {1, 2}
    assert len({result['rule']['rule_id'] for result in results}) == 2
    assert w2.call('intercept.rules.list')['revision'] == 2


def test_list_and_point_get_revision_do_not_read_a_second_snapshot(w2, monkeypatch):
    original = V13Store._record_view
    pending = {'collection': 'policy_command_rules'}

    def concurrent_write_after_read(store, collection, record_id, **kwargs):
        observed = original(store, collection, record_id, **kwargs)
        if pending.get('collection') == collection:
            pending.clear()
            if collection == 'policy_command_rules':
                create_rule(w2)
            else:
                w2.call('intercept.tool_config.set', {'tool': 'x', 'default_decision': 'deny',
                    'judge_enabled': False}, revision=0)
        return observed

    monkeypatch.setattr(V13Store, '_record_view', concurrent_write_after_read)
    assert w2.call('intercept.rules.list') == {'revision': 0, 'items': [], 'next_cursor': '', 'coverage': 'complete'}
    assert w2.call('intercept.rules.list')['revision'] == 1
    pending['collection'] = 'policy_tool_config'
    assert w2.call('intercept.tool_config.get', {'tool': 'x'})['revision'] == 0
    assert w2.call('intercept.tool_config.get', {'tool': 'x'})['revision'] == 1


def test_command_paths_resolve_symlinks_and_never_escape_owner(w2, tmp_path):
    directory = w2.host.workspace_root / 'safe'
    directory.mkdir(parents=True)
    (w2.host.workspace_root / 'alias').symlink_to(directory, target_is_directory=True)
    saved = create_rule(w2, path_prefix='alias/sub')
    assert saved['rule']['path_prefix'] == str(directory / 'sub')
    for path in ('../outside', str(tmp_path / 'outside')):
        error_code('INVALID_SCHEMA', create_rule, w2, path_prefix=path)
    (w2.host.workspace_root / 'escape').symlink_to(tmp_path, target_is_directory=True)
    error_code('INVALID_SCHEMA', create_rule, w2, path_prefix='escape/file')
    assert len(w2.call('intercept.rules.list')['items']) == 1


def test_snapshot_reads_real_v13_records_and_isolated_owner(w2, tmp_path):
    saved = create_rule(w2)
    w2.call('intercept.tool_config.set', {'tool': 'x', 'default_decision': 'ask', 'judge_enabled': False}, revision=0)
    snapshot = policy.get_snapshot(w2.home, 'command')
    assert snapshot['rules'] == [saved['rule']]
    assert snapshot['tool_configs'][0]['tool'] == 'x' and snapshot['judge_config'] == {}
    snapshot['rules'][0]['decision'] = 'allow'
    assert policy.get_snapshot(w2.home, 'command')['rules'][0]['decision'] == 'ask'
    other = Harness(tmp_path / 'root', 'other')
    before = sorted(str(path.relative_to(other.home)) for path in other.home.rglob('*'))
    assert policy.get_snapshot(other.home, 'command') == {'rules': [], 'tool_configs': [], 'judge_config': {}}
    assert sorted(str(path.relative_to(other.home)) for path in other.home.rglob('*')) == before
    missing = tmp_path / 'never-created'
    assert policy.get_snapshot(missing, 'command') == {'rules': [], 'tool_configs': [], 'judge_config': {}}
    assert not missing.exists()


def test_snapshot_missing_and_corrupt_warn_once_per_generation(w2, caplog):
    with caplog.at_level('WARNING', logger=policy.__name__):
        policy.get_snapshot(w2.home, 'command')
        first = len(caplog.records)
        assert first == 3
        policy.get_snapshot(w2.home, 'command')
        assert len(caplog.records) == first
        create_rule(w2)
        file = w2.home / 'data' / 'business_v13' / 'policy_command_rules.json'
        file.write_text('{corrupt')
        assert policy.get_snapshot(w2.home, 'command')['rules'] == []
        changed = len(caplog.records)
        policy.get_snapshot(w2.home, 'command')
        assert len(caplog.records) == changed
        file.write_text('[]')
        policy.get_snapshot(w2.home, 'command')
        assert len(caplog.records) == changed + 1


def test_snapshot_cache_hits_old_generation_and_refreshes_atomic_replacement(w2, monkeypatch):
    saved = create_rule(w2)
    real_signature = policy.cache_stat_signature
    signatures = []

    def tracked(info):
        signatures.append(real_signature(info))
        return signatures[-1]

    monkeypatch.setattr(policy, 'cache_stat_signature', tracked)
    monkeypatch.setattr(policy, 'cache_entry_trustworthy', lambda mtime: True)
    policy.get_snapshot(w2.home, 'command')
    reads = []
    original = V13Store._record_view

    def read(*args, **kwargs):
        reads.append(args[1])
        return original(*args, **kwargs)

    monkeypatch.setattr(V13Store, '_record_view', read)
    assert policy.get_snapshot(w2.home, 'command')['rules'] == [saved['rule']]
    assert reads == [] and signatures
    file = w2.home / 'data' / 'business_v13' / 'policy_command_rules.json'
    payload = json.loads(file.read_text())
    payload['records'][saved['rule']['rule_id']]['decision'] = 'deny'
    candidate = file.with_suffix('.replacement')
    candidate.write_text(json.dumps(payload))
    os.replace(candidate, file)
    assert policy.get_snapshot(w2.home, 'command')['rules'][0]['decision'] == 'deny'
    assert 'policy_command_rules' in reads
    assert len(policy._snapshot_cache) <= 128


def test_snapshot_cache_actually_evicts_old_owner_entries(w2, tmp_path, monkeypatch):
    monkeypatch.setattr(policy, 'cache_entry_trustworthy', lambda mtime: True)
    for index in range(130):
        owner = tmp_path / ('cache-owner-' + str(index))
        assert policy._snapshot_rows(V13Store.read_only(owner), 'policy_command_rules', 'PolicyRule') == []
    assert len(policy._snapshot_cache) == 128
    assert not any(key[0].endswith('/cache-owner-0') for key in policy._snapshot_cache)
    assert any(key[0].endswith('/cache-owner-129') for key in policy._snapshot_cache)


def test_missing_collection_warning_is_not_repeated_for_unrelated_transactions(w2, caplog):
    with caplog.at_level('WARNING', logger=policy.__name__):
        assert policy.get_snapshot(w2.home, 'command')['judge_config'] == {}
        count = sum('policy_judge_config' in row.message for row in caplog.records)
        assert count == 1
        create_rule(w2)
        assert policy.get_snapshot(w2.home, 'command')['judge_config'] == {}
        assert sum('policy_judge_config' in row.message for row in caplog.records) == count


def test_snapshot_observes_committed_journal_and_idempotent_recovery(w2, monkeypatch):
    from agent_py_agent.agent.business_spaces import v13_store

    original = v13_store.write_bytes_atomic_beneath

    def interrupt_materialization(root, parts, raw, **kwargs):
        if tuple(parts) == ('data', 'business_v13', 'policy_command_rules.json'):
            raise OSError('测试：journal 已提交，目标快照物化中断')
        return original(root, parts, raw, **kwargs)

    monkeypatch.setattr(v13_store, 'write_bytes_atomic_beneath', interrupt_materialization)
    params = {'name': '恢复验证', 'tool': 'run_command', 'decision': 'deny'}
    with pytest.raises(OSError):
        w2.call('intercept.rules.create', params, key='w2-journal-recovery')
    assert not (w2.home / 'data' / 'business_v13' / 'policy_command_rules.json').exists()
    snapshot = policy.get_snapshot(w2.home, 'command')
    assert len(snapshot['rules']) == 1 and snapshot['rules'][0]['decision'] == 'deny'
    assert w2.call('intercept.rules.list')['revision'] == 1
    monkeypatch.setattr(v13_store, 'write_bytes_atomic_beneath', original)
    recovered = w2.call('intercept.rules.create', params, key='w2-journal-recovery')
    assert recovered['revision'] == 1 and recovered['rule'] == snapshot['rules'][0]
    assert len(w2.call('intercept.rules.list')['items']) == 1


def test_concurrent_update_delete_same_revision_has_one_commit_without_partial_write(w2, monkeypatch):
    import threading

    saved = create_rule(w2)
    rule_id = saved['rule']['rule_id']
    barrier = threading.Barrier(2)
    transaction = V13Store.transaction

    def synchronized(store, collection, *args, **kwargs):
        if store.action in {'intercept.rules.update', 'intercept.rules.delete'}:
            barrier.wait(5)
        return transaction(store, collection, *args, **kwargs)

    monkeypatch.setattr(V13Store, 'transaction', synchronized)

    def run(action):
        params = {'rule_id': rule_id}
        if action.endswith('update'):
            params['priority'] = 91
        try:
            return w2.call(action, params, revision=1)
        except (BusinessError, HostAPIError) as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run, ['intercept.rules.update', 'intercept.rules.delete']))
    assert results.count('REVISION_CONFLICT') == 1
    assert sum(isinstance(row, dict) for row in results) == 1
    listed = w2.call('intercept.rules.list')
    assert listed['revision'] == 2
    assert listed['items'] == [] or listed['items'][0]['priority'] == 91


def test_500_rules_cached_snapshot_has_no_per_rule_filesystem_resolution(w2, monkeypatch):
    from pathlib import Path

    from agent_py_agent.agent.business_spaces.v13_store import v13_store_for
    from agent_py_agent.agent.tooling.policy_rules import evaluate_command_rules

    call = w2.invocation('intercept.rules.create')
    store = v13_store_for(call)
    prefix = str(w2.host.workspace_root / 'safe')

    def populate(records):
        for index in range(500):
            identity = 'rule-' + str(index)
            records[identity] = {'owner_id': call.owner_id, 'rule_id': identity,
                'name': 'cached', 'tool': 'run_command', 'decision': 'deny',
                'path_prefix': prefix, 'priority': index, 'enabled': True,
                'created_at': 10, 'updated_at': 10}
        return {}

    store.transaction('policy_command_rules', populate, idempotency_key='populate-500-rules', params_digest='500')
    monkeypatch.setattr(policy, 'cache_entry_trustworthy', lambda mtime: True)
    policy.get_snapshot(w2.home, 'command')

    def forbidden(*args, **kwargs):
        pytest.fail('cached policy must not resolve every rule or reload a collection')

    monkeypatch.setattr(Path, 'resolve', forbidden)
    monkeypatch.setattr(V13Store, '_record_view', forbidden)
    snapshot = policy.get_snapshot(w2.home, 'command')
    assert len(snapshot['rules']) == 500
    result = evaluate_command_rules(snapshot, {'tool': 'run_command', 'paths': [prefix + '/file']})
    assert result.decision == 'deny' and result.rule_id == 'rule-499'
