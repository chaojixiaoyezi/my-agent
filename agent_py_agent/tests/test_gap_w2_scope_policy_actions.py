"""W2 范围策略：真实冻结任务读口、任务归属、工作区约束与有界集合。"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.business_spaces import gap158_w2_policy_store as policy
from agent_py_agent.agent.business_spaces.v13_store import v13_store_for
from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.agent.gateway_parts import business_api_v13 as v13
from agent_py_agent.tests import test_gap_w2_intercept_actions as support

w2 = support.w2
error_code = support.error_code


def task_for(harness, name='任务'):
    """只种入 canonical 任务，不派发 runner、不写假的 tasks.get 结果。"""
    workspace = harness.host.workspace_root
    workspace.mkdir(parents=True, exist_ok=True)
    store = getattr(harness.host, 'conversation_store', None)
    if store is None:
        store = ConversationStore(harness.home / 'conversations')
        harness.host.conversation_store = store
    args = {'owner_id': harness.services.owner_id, 'owner_home': str(harness.home),
            'workspace_root': str(workspace), 'cwd': str(workspace)}
    sender = store.threads.create(title='发送方', **args)
    target = store.threads.create(title='接收方', **args)
    task = store.session_tasks.create(SessionTaskDraft(sender.thread_id, target.thread_id, name))
    harness.services = api.BusinessServices(harness.host)
    return task.task_id


def rule(w2, *, task_id=None, **changes):
    params = {'kind': 'path_deny', 'pattern': 'secret', **changes}
    if task_id is not None:
        return w2.call('scope_policy.task_rules.create', {'task_id': task_id, **params})
    return w2.call('scope_policy.rules.create', params)


def test_global_scope_rule_crud_normalizes_domains_and_paths(w2):
    assert w2.call('scope_policy.rules.list')['revision'] == 0
    domain = rule(w2, kind='domain_allow', pattern='.BÜCHER.Example.')
    assert domain['rule']['pattern'] == '.xn--bcher-kva.example'
    assert domain['rule']['priority'] == 0 and domain['rule']['enabled']
    path = rule(w2, pattern='safe/../secret')
    assert path['rule']['pattern'] == str(w2.host.workspace_root / 'secret')
    assert path['revision'] == 2
    listed = w2.call('scope_policy.rules.list', {'kind': 'domain_allow'})
    assert listed['revision'] == 2 and listed['items'] == [domain['rule']]
    updated = w2.call('scope_policy.rules.update', {'rule_id': domain['rule']['rule_id'],
        'pattern': 'EXAMPLE.COM.', 'priority': 23, 'note': '明确后缀'}, revision=2)
    assert updated['rule']['pattern'] == 'example.com' and updated['revision'] == 3
    disabled = w2.call('scope_policy.rules.toggle', {'rule_id': domain['rule']['rule_id'],
        'enabled': False}, revision=3)
    assert disabled['revision'] == 4 and not disabled['rule']['enabled']
    deleted = w2.call('scope_policy.rules.delete', {'rule_id': domain['rule']['rule_id']}, revision=4)
    assert deleted == {'deleted': True, 'revision': 5}
    error_code('RESOURCE_NOT_FOUND', w2.call, 'scope_policy.rules.toggle',
        {'rule_id': domain['rule']['rule_id'], 'enabled': True}, revision=0)
    assert w2.call('scope_policy.rules.list')['items'] == [path['rule']]


@pytest.mark.parametrize('pattern', ['*.example.com', 'https://example.com', 'a..b',
                                     '.example.com/path', 'a:b', 'user@example.com'])
def test_domain_wildcards_urls_and_ambiguous_hosts_are_rejected(w2, pattern):
    error_code('INVALID_SCHEMA', rule, w2, kind='domain_allow', pattern=pattern)
    assert w2.call('scope_policy.rules.list')['revision'] == 0


def test_global_path_rules_reject_absolute_parent_and_symlink_escape(w2, tmp_path):
    w2.host.workspace_root.mkdir(parents=True, exist_ok=True)
    (w2.host.workspace_root / 'escape').symlink_to(tmp_path, target_is_directory=True)
    for pattern in (str(tmp_path / 'absolute'), '../outside', 'escape/outside'):
        error_code('INVALID_SCHEMA', rule, w2, pattern=pattern)
    safe = w2.host.workspace_root / 'safe'
    safe.mkdir()
    (w2.host.workspace_root / 'alias').symlink_to(safe, target_is_directory=True)
    assert rule(w2, pattern='alias')['rule']['pattern'] == str(safe)


def test_task_rules_use_frozen_task_lookup_and_match_record_task_id(w2, monkeypatch):
    first, second = task_for(w2, 'first'), task_for(w2, 'second')
    frozen = v13.v13_call_frozen
    calls = []

    def tracked(call, action, params, **kwargs):
        calls.append((action, params))
        return frozen(call, action, params, **kwargs)

    monkeypatch.setattr(v13, 'v13_call_frozen', tracked)
    created = rule(w2, task_id=first)
    other = rule(w2, task_id=second, pattern='other')
    assert created['rule']['task_id'] == first
    assert ('tasks.get', {'task_id': first}) in calls
    assert w2.call('scope_policy.task_rules.list', {'task_id': first})['items'] == [created['rule']]
    assert w2.call('scope_policy.task_rules.list', {'task_id': second})['items'] == [other['rule']]
    for revision in (0, 2):
        error_code('RESOURCE_NOT_FOUND', w2.call, 'scope_policy.task_rules.update',
            {'task_id': second, 'rule_id': created['rule']['rule_id'], 'pattern': 'wrong-task'}, revision=revision)
    updated = w2.call('scope_policy.task_rules.update', {'task_id': first,
        'rule_id': created['rule']['rule_id'], 'note': '任务收窄'}, revision=2)
    assert updated['revision'] == 3 and updated['rule']['note'] == '任务收窄'
    deleted = w2.call('scope_policy.task_rules.delete', {'task_id': first,
        'rule_id': created['rule']['rule_id']}, revision=3, key='w2-task-rule-delete')
    assert deleted['revision'] == 4
    assert w2.call('scope_policy.task_rules.delete', {'task_id': first,
        'rule_id': created['rule']['rule_id']}, revision=3, key='w2-task-rule-delete') == deleted
    assert w2.call('scope_policy.task_rules.list', {'task_id': first})['items'] == []


def test_missing_foreign_tasks_and_owner_rules_are_uniformly_absent(w2, tmp_path):
    own_task = task_for(w2)
    other = support.Harness(tmp_path / 'root', 'other')
    foreign_task = task_for(other)
    for task_id in ('does-not-exist', foreign_task):
        error_code('RESOURCE_NOT_FOUND', w2.call, 'scope_policy.task_scope.get', {'task_id': task_id})
        error_code('RESOURCE_NOT_FOUND', rule, w2, task_id=task_id)
    created = rule(w2)
    error_code('RESOURCE_NOT_FOUND', other.call, 'scope_policy.rules.update',
        {'rule_id': created['rule']['rule_id'], 'pattern': 'secret'}, revision=1)
    assert w2.call('scope_policy.task_scope.get', {'task_id': own_task}) == {'revision': 0}


def test_task_scope_get_set_delete_and_true_workspace_root(w2):
    task_id = task_for(w2)
    workspace = w2.host.workspace_root
    (workspace / 'safe').mkdir()
    (workspace / 'alias').symlink_to(workspace / 'safe', target_is_directory=True)
    params = {'task_id': task_id, 'workspace_subpaths': ['alias/sub', 'safe/sub'], 'network': 'none'}
    saved = w2.call('scope_policy.task_scope.set', params, revision=0, key='w2-task-scope-set')
    assert saved['scope'] == {**params, 'workspace_subpaths': ['safe/sub']}
    assert saved['revision'] == 1
    assert w2.call('scope_policy.task_scope.set', params, revision=0, key='w2-task-scope-set') == saved
    assert w2.call('scope_policy.task_scope.get', {'task_id': task_id}) == saved
    assert w2.call('scope_policy.task_scope.delete', {'task_id': task_id}, revision=1) == {
        'deleted': True, 'revision': 2}
    assert w2.call('scope_policy.task_scope.get', {'task_id': task_id}) == {'revision': 2}


@pytest.mark.parametrize('path', ['/absolute', '../outside', 'safe/../../outside',
                                  '-option', 'a\x00b', 'C:\\outside', 'a\\b'])
def test_task_scope_unsafe_paths_have_path_outside_workspace_code(w2, path):
    task_id = task_for(w2)
    error_code('PATH_OUTSIDE_WORKSPACE', w2.call, 'scope_policy.task_scope.set',
        {'task_id': task_id, 'workspace_subpaths': [path], 'network': 'inherit'}, revision=0)
    assert w2.call('scope_policy.task_scope.get', {'task_id': task_id}) == {'revision': 0}


def test_task_scope_symlink_cannot_use_owner_root_as_workspace(w2):
    task_id = task_for(w2)
    (w2.host.workspace_root / 'escape').symlink_to(w2.home, target_is_directory=True)
    error_code('PATH_OUTSIDE_WORKSPACE', w2.call, 'scope_policy.task_scope.set',
        {'task_id': task_id, 'workspace_subpaths': ['escape/secret'], 'network': 'allowlist'}, revision=0)
    error_code('INVALID_SCHEMA', w2.call, 'scope_policy.task_scope.set',
        {'task_id': task_id, 'workspace_subpaths': ['safe'], 'network': 'inherit',
         'workspace_root': str(w2.home)}, revision=0)
    w2.host.workspace_root = None
    error_code('HOST_NOT_READY', w2.call, 'scope_policy.task_scope.set',
        {'task_id': task_id, 'workspace_subpaths': ['safe'], 'network': 'inherit'}, revision=0)


def test_task_constraints_crud_cas_and_exact_task_binding(w2):
    first, second = task_for(w2, 'first'), task_for(w2, 'second')
    params = {'task_id': first, 'kind': 'tool_allowlist', 'tools': ['read_file', 'list_files']}
    saved = w2.call('scope_policy.task_constraints.create', params, key='w2-constraint-create')
    assert saved['revision'] == 1 and saved['constraint']['task_id'] == first
    assert w2.call('scope_policy.task_constraints.create', params, key='w2-constraint-create') == saved
    constraint_id = saved['constraint']['constraint_id']
    assert w2.call('scope_policy.task_constraints.list', {'task_id': second})['items'] == []
    assert w2.call('scope_policy.task_constraints.list', {'task_id': first})['items'] == [saved['constraint']]
    error_code('RESOURCE_NOT_FOUND', w2.call, 'scope_policy.task_constraints.update',
        {'task_id': second, 'constraint_id': constraint_id, 'tools': []}, revision=1)
    updated = w2.call('scope_policy.task_constraints.update', {'task_id': first,
        'constraint_id': constraint_id, 'tools': ['read_file']}, revision=1)
    assert updated['revision'] == 2 and updated['constraint']['tools'] == ['read_file']
    error_code('REVISION_CONFLICT', w2.call, 'scope_policy.task_constraints.delete',
        {'task_id': first, 'constraint_id': constraint_id}, revision=1)
    assert w2.call('scope_policy.task_constraints.delete', {'task_id': first,
        'constraint_id': constraint_id}, revision=2) == {'deleted': True, 'revision': 3}


def test_all_scope_collections_have_live_owner_and_task_caps(w2, monkeypatch):
    first, second = task_for(w2, 'first'), task_for(w2, 'second')
    monkeypatch.setitem(sys.modules, 'agent_py_agent.agent.business_spaces.gap158_limits',
        SimpleNamespace(POLICY_RULES_MAX_PER_OWNER=2, SCOPE_RULES_MAX_PER_OWNER=2,
                        SCOPE_TASK_RULES_MAX_PER_TASK=1))
    rule(w2, task_id=first)
    assert error_code('LIMIT_EXCEEDED', rule, w2, task_id=first).details['limit'] == 1
    rule(w2, task_id=second)
    assert error_code('LIMIT_EXCEEDED', rule, w2, task_id=second).details['limit'] == 2
    rule(w2)
    rule(w2)
    error_code('LIMIT_EXCEEDED', rule, w2)
    params = {'task_id': first, 'kind': 'tool_denylist', 'tools': ['run_command']}
    w2.call('scope_policy.task_constraints.create', params)
    error_code('LIMIT_EXCEEDED', w2.call, 'scope_policy.task_constraints.create', params)


def test_foreign_records_inside_owner_collection_remain_absent(w2):
    task_id = task_for(w2)
    call = w2.invocation('scope_policy.task_constraints.list', {'task_id': task_id})
    store = v13_store_for(call, 'owner')

    def seed(records):
        records['injected'] = {'owner_id': 'local/foreign', 'constraint_id': 'injected',
            'task_id': task_id, 'kind': 'tool_allowlist', 'tools': ['run_command']}
        return {'seeded': True}

    store.transaction('scope_task_constraints', seed, idempotency_key='w2-foreign-seed', params_digest='seed')
    assert w2.call('scope_policy.task_constraints.list', {'task_id': task_id})['items'] == []
    error_code('RESOURCE_NOT_FOUND', w2.call, 'scope_policy.task_constraints.update',
        {'task_id': task_id, 'constraint_id': 'injected', 'tools': []}, revision=1)
    assert policy.get_snapshot(w2.home, 'scope', task_id)['constraints'] == []


def test_scope_snapshot_projects_real_rules_task_rules_constraints_and_scope(w2):
    first, second = task_for(w2, 'first'), task_for(w2, 'second')
    global_rule = rule(w2)
    task_rule = rule(w2, task_id=first, kind='domain_deny', pattern='EXAMPLE.COM.')
    rule(w2, task_id=second, pattern='other')
    constraint = w2.call('scope_policy.task_constraints.create',
        {'task_id': first, 'kind': 'tool_denylist', 'tools': ['run_command']})
    explicit = w2.call('scope_policy.task_scope.set',
        {'task_id': first, 'workspace_subpaths': ['safe'], 'network': 'none'}, revision=0)
    snapshot = policy.get_snapshot(w2.home, 'scope', first, w2.host.workspace_root)
    assert snapshot == {'rules': [global_rule['rule']], 'task_rules': [task_rule['rule']],
        'constraints': [constraint['constraint']], 'task_scope': explicit['scope']}
    assert policy.get_snapshot(w2.home, 'scope', second)['task_scope'] is None


def test_cached_rule_path_is_rechecked_after_symlink_retarget_without_json_change(w2, tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling.policy_rules import evaluate_scope_rules

    safe = w2.host.workspace_root / 'safe'
    safe.mkdir(parents=True)
    saved = rule(w2, kind='path_allow', pattern='safe')
    monkeypatch.setattr(policy, 'cache_entry_trustworthy', lambda mtime: True)
    assert policy.get_snapshot(w2.home, 'scope')['rules'] == [saved['rule']]
    safe.rmdir()
    safe.symlink_to(tmp_path, target_is_directory=True)
    snapshot = policy.get_snapshot(w2.home, 'scope')
    assert snapshot['rules'] == [saved['rule']]
    assert evaluate_scope_rules(snapshot, {'paths': [str((safe / 'file').resolve())]}).decision == 'deny'
