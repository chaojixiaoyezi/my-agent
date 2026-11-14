"""业务动作只经原 WorkflowStore；范围/正文/幂等回执同次发表。"""
import errno
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.assets.workflow_store import WorkflowStore
from agent_py_agent.agent.conversation.business_contracts import WorkflowScope


def definition(name='daily', **changes):
    return {'descriptor': {'id': name, 'kind': 'workflow', 'name': name,
            'access_targets': [], 'data_sources': [], 'credential_refs': [], 'io_contract': {}},
            'steps': [], **changes}


def service(home, *, references=None, clock=None):
    from agent_py_agent.agent.workflows.business_scope import WorkflowScopeService
    actor = SimpleNamespace(owner_id='alice')
    authorized = {'value': True}
    instance = WorkflowScopeService(home, owner_id='alice',
        authorize=lambda current, action, ref: current is actor and authorized['value'],
        reference_reader=references, clock=clock)
    return instance, actor, authorized


def test_create_scope_same_commit_and_idempotency(tmp_path):
    api, actor, _ = service(tmp_path)
    request = {'definition': definition(), 'scope': {'scope_mode': 'spaces', 'space_ids': ['s1']}}
    first = api.execute('workflows.create', request, actor=actor, idempotency_key='create')
    assert first['scope'] == request['scope']
    assert api.execute('workflows.create', request, actor=actor, idempotency_key='create') == first
    with pytest.raises(ValueError, match='IDEMPOTENCY_CONFLICT'):
        api.execute('workflows.create', {**request, 'scope': {'scope_mode': 'unassigned', 'space_ids': []}},
                    actor=actor, idempotency_key='create')
    assert len(WorkflowStore(tmp_path).list_workflows()) == 1
    assert api.execute('workflows.list', {'space_id': 'other'}, actor=actor)['items'] == []
    assert len(api.execute('workflows.list', {'space_id': 's1'}, actor=actor)['items']) == 1


def test_create_failure_never_exposes_global(tmp_path, monkeypatch):
    import agent_py_agent.agent.assets as assets
    api, actor, _ = service(tmp_path)
    real = assets.write_text_atomic_beneath

    def fail(root, parts, text, **options):
        if parts[-1] == 'workflow.json':
            raise OSError(errno.ENOSPC, 'single canonical write failure')
        return real(root, parts, text, **options)

    with monkeypatch.context() as patch:
        patch.setattr(assets, 'write_text_atomic_beneath', fail)
        with pytest.raises(OSError):
            api.execute('workflows.create', {'definition': definition(),
                'scope': {'scope_mode': 'spaces', 'space_ids': ['s1']}}, actor=actor, idempotency_key='once')
    assert WorkflowStore(tmp_path).get_workflow('daily') is None
    result = api.execute('workflows.create', {'definition': definition(),
        'scope': {'scope_mode': 'spaces', 'space_ids': ['s1']}}, actor=actor, idempotency_key='once')
    assert result['scope']['scope_mode'] == 'spaces'


def test_scope_cas_does_not_change_execution_definition(tmp_path):
    api, actor, authorized = service(tmp_path)
    api.execute('workflows.create', {'definition': definition()}, actor=actor, idempotency_key='create')
    before = WorkflowStore(tmp_path).get_workflow('daily').execution_definition_ref
    current = api.execute('workflows.scope.get', {'workflow_ref': 'daily'}, actor=actor)
    changed = api.set_scope('daily', WorkflowScope('unassigned', (), current['scope_revision']),
                            actor=actor, expected_revision=current['scope_revision'], idempotency_key='scope')
    assert changed['scope']['scope_mode'] == 'unassigned'
    assert WorkflowStore(tmp_path).get_workflow('daily').execution_definition_ref == before
    assert api.set_scope('daily', WorkflowScope('unassigned', (), current['scope_revision']),
        actor=actor, expected_revision=current['scope_revision'], idempotency_key='scope') == changed
    with pytest.raises(ValueError, match='REVISION_CONFLICT'):
        api.set_scope('daily', WorkflowScope('owner_global', (), 0), actor=actor,
                      expected_revision=current['scope_revision'], idempotency_key='stale')
    authorized['value'] = False
    with pytest.raises(PermissionError):
        api.execute('workflows.get', {'workflow_ref': 'daily'}, actor=actor)


def test_legacy_workflow_global_and_read_only_preview(tmp_path):
    api, actor, _ = service(tmp_path)
    WorkflowStore(tmp_path).save_workflow(definition())
    scope = api.execute('workflows.scope.get', {'workflow_ref': 'daily'}, actor=actor)
    assert scope['scope'] == {'scope_mode': 'owner_global', 'space_ids': []}
    before = WorkflowStore(tmp_path).get_workflow('daily').to_dict()
    preview = api.execute('workflows.preview', {'workflow_ref': 'daily'}, actor=actor)
    assert preview['errors'] == [] and preview['plan'] == []
    assert before == WorkflowStore(tmp_path).get_workflow('daily').to_dict()


def test_unassigned_rejects_run_and_foreign_actor(tmp_path):
    api, actor, _ = service(tmp_path)
    api.execute('workflows.create', {'definition': definition(),
        'scope': {'scope_mode': 'unassigned', 'space_ids': []}}, actor=actor, idempotency_key='c')
    with pytest.raises(PermissionError):
        api.execute('workflows.run', {'workflow_ref': 'daily'}, actor=actor, idempotency_key='run')
    with pytest.raises(PermissionError):
        api.execute('workflows.scope.get', {'workflow_ref': 'daily'}, actor=SimpleNamespace(owner_id='bob'))
    assert not (tmp_path / 'data' / 'workflow_runs').exists()


# LLM: 治理授权须先于真实脚本副作用；测试只授予 effects 写根，不放宽生产沙箱或原断言。
# 函数用途: 验证治理拒绝、恢复与结果核对期间，真实脚本副作用只发生一次。
@pytest.mark.parametrize('invalid', ['candidate', 'self_review', 'revoked', 'attempt', 'unknown_effect', 'committed_resolution'])
# LLM: 真实脚本与原治理门不替换；未知现场只用显式恢复写入，不放宽普通终态守卫。
# 函数用途: 核对执行前治理及已提交结果的对账，禁止批准后重放副作用。
def test_governance_consumed_before_real_script_effect(tmp_path, invalid):
    from agent_py_agent.agent.workflows.engine import WorkflowEngine
    from agent_py_agent.agent.workflows.governance import (
        WorkflowGovernance,
        workflow_candidate_hash,
    )
    from agent_py_agent.agent.workflows.run_store import WorkflowRunStore
    from agent_py_agent.tests.test_workflow_script_engine import script, step, workflow

    store = WorkflowRunStore(tmp_path)
    allowed = [invalid != 'revoked']
    broken = [True]
    effects = tmp_path / 'effects'
    effects.mkdir()
    counter = effects / 'effect'
    script(tmp_path, f'from pathlib import Path\np=Path({str(counter)!r})\np.write_text((p.read_text() if p.exists() else "")+"executed")\nprint(\'{{"value":1}}\')')

    def receipt(ref, record, step_id):
        target = next(item for item in record.steps if item.step_id == step_id)
        value = {'receipt_ref': ref, 'owner_id': 'alice', 'workflow_run_id': record.workflow_run_id,
            'task_id': record.business_context.get('task_id', ''), 'step_id': step_id,
            'attempt_id': target.escalation_result['attempt_id'], 'candidate_hash': workflow_candidate_hash(
                store.load_workflow_snapshot(record.workflow_run_id)), 'author_id': 'writer',
            'reviewer_id': 'independent-reviewer', 'decision': 'approved', 'expires_at': 9999999999}
        if broken[0] and invalid == 'candidate':
            value['candidate_hash'] = '0' * 64
        if broken[0] and invalid == 'self_review':
            value['reviewer_id'] = 'writer'
        if broken[0] and invalid == 'attempt':
            value['attempt_id'] = 'other-attempt'
        return value

    def reconcile(record, target):
        return {'state': 'committed' if invalid == 'committed_resolution' else 'unknown',
            'resolution_ref': 'resolution-1', 'owner_id': record.owner_id,
            'workflow_run_id': record.workflow_run_id, 'task_id': record.business_context.get('task_id', ''),
            'step_id': target.step_id, 'attempt_id': target.escalation_result['attempt_id'],
            'candidate_hash': workflow_candidate_hash(store.load_workflow_snapshot(record.workflow_run_id)),
            'output': {'value': 1}}

    guard = WorkflowGovernance(store, policy_provider=lambda record, phase: {
        'author_id': 'writer', 'review_refs': ['review-1'], 'minimum_reviews': 1},
        receipt_reader=receipt, authorization_check=lambda receipt, record: allowed[0],
        effect_reader=reconcile)
    engine = WorkflowEngine(tmp_path, run_authorizer=guard.require_effect)
    from agent_py_agent.agent.workflows import ScriptRunner
    engine.runner = ScriptRunner(tmp_path, extra_write_roots=(effects,))
    first = engine.run_workflow(workflow(step()))
    if invalid in ('unknown_effect', 'committed_resolution'):
        # 已开始/结果未知的旧尝试只能核对，不允许凭新的“批准”重跑。
        assert first.status == 'succeeded'
        first.status, first.effect_outcome = 'paused_for_resume', 'unknown'
        first.steps[0].status = 'paused_for_resume'
        # 显式注入恢复现场；普通晚到写入仍不得把已成功的运行改回暂停。
        store.save_run(first, resuming=True)
        if invalid == 'unknown_effect':
            with pytest.raises(PermissionError, match='RECONCILIATION'):
                engine.resume_workflow_run(first.workflow_run_id)
        else:
            reconciled = engine.resume_workflow_run(first.workflow_run_id)
            assert reconciled.status == 'succeeded' and reconciled.steps[0].escalation_result['resolution_origin'] == 'system'
        assert counter.read_text() == 'executed'
    else:
        assert first.status == 'failed' and not counter.exists()
        broken[0], allowed[0] = False, True
        done = engine.resume_workflow_run(first.workflow_run_id)
        assert done.status == 'succeeded' and counter.read_text() == 'executed'
        assert done.steps[0].escalation_result['review_refs'] == ['review-1']


# LLM: 带冻结业务上下文的运行每步以当前Actor经共享复核（G5a-R1）；本测因此用真实空间服务、真实BusinessActor与真实
#   spaces.create（MSG-033 决定A），不再用假Actor与不存在的空间；授权身份比较、二参上下文端口与全部原断言不变。
# 函数用途: 验证原生工具消费业务服务、保留宿主引擎对象并在授权撤回与owner注入时如实未开始。
def test_native_tool_consumes_business_service_and_preserves_host_engine(tmp_path):
    from agent_py_agent.agent.conversation.business_access import BusinessActor
    from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
    from agent_py_agent.agent.conversation.business_spaces import BusinessSpaceService
    from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool
    from agent_py_agent.agent.workflows.business_scope import WorkflowScopeService
    from agent_py_agent.agent.workflows.engine import WorkflowEngine
    from agent_py_agent.tests.test_workflow_script_engine import script, step

    spaces = BusinessSpaceService(tmp_path, owner_id='alice', enabled=True)
    actor = BusinessActor('alice', 'f4-native-user', spaces, lambda action, facts: True)
    space = spaces.call('spaces.create', {'name': 'f4-native'}, actor=actor,
                        idempotency_key='f4-native-space-create')['space_id']
    authorized = {'value': True}
    api = WorkflowScopeService(tmp_path, owner_id='alice',
        authorize=lambda current, action, ref: current is actor and authorized['value'])
    api.context_provider = lambda current, request: BusinessExecutionContext(
        'alice', space, None, 'stask-original', 1, None, (), (), 'business.v1')
    engine = WorkflowEngine(tmp_path)
    original_runner = engine.runner
    tool = WorkflowsTool(tmp_path, engine=engine)
    tool.business_service, tool.business_actor_provider = api, lambda: actor
    script(tmp_path)
    created = tool.execute({'action': 'workflows.create', 'definition': definition(steps=[step()]),
        'scope': {'scope_mode': 'spaces', 'space_ids': [space]}, 'idempotency_key': 'native-create'})
    assert created.ok, created.output
    assert created.result_envelope['scope']['space_ids'] == [space]
    started = tool.execute({'action': 'workflows.run', 'workflow_ref': 'daily', 'space_id': space,
                            'idempotency_key': 'native-run'})
    assert started.ok, started.output
    saved = engine.store.load_run(started.result_envelope['workflow_run_id'])
    assert saved.status == 'succeeded' and saved.business_context['task_id'] == 'stask-original'
    assert saved.scope_revision == 1 and saved.final_output == {'value': 1}
    assert engine.runner is original_runner and engine.run_authorizer is None
    replay = tool.execute({'action': 'workflows.run', 'workflow_ref': 'daily', 'space_id': space,
                           'idempotency_key': 'native-run'})
    assert replay.ok and replay.result_envelope == started.result_envelope
    authorized['value'] = False
    denied = tool.execute({'action': 'workflows.get', 'workflow_ref': 'daily'})
    assert not denied.ok and denied.effect_outcome == 'not_started'
    injected = tool.execute({'action': 'workflows.get', 'workflow_ref': 'daily', 'owner_id': 'alice'})
    assert not injected.ok and injected.error_code == 'TOOL_INVALID_ARGUMENTS'


# LLM: 同一原运行保留已完成控制回执，取消结清终态不改写业务结果。
# 函数用途: 验证后续控制不会覆盖先前恢复的幂等回执。
def test_completed_control_replays_original_receipt_after_later_control(tmp_path):
    from agent_py_agent.agent.workflows.engine import WorkflowEngine
    from agent_py_agent.tests.test_workflow_script_engine import script, step, workflow
    script(tmp_path)
    engine = WorkflowEngine(tmp_path)
    first = engine.run_workflow(workflow(step()))
    expected = first.revision
    resumed = engine.resume_workflow_run(first.workflow_run_id, expected_revision=expected, operation_key='resume-once')
    engine.cancel_workflow_run(first.workflow_run_id, expected_revision=resumed.revision, operation_key='cancel-once')
    replay = engine.resume_workflow_run(first.workflow_run_id, expected_revision=expected, operation_key='resume-once')
    assert replay.to_dict() == resumed.to_dict()
    current = engine.store.load_run(first.workflow_run_id)
    assert current.status == 'succeeded'  # 取消已结清运行只确认控制，不改写原成功。
    assert engine.store._read(first.workflow_run_id, 'run.json')['control_operations']['cancel-once']['state'] == 'completed'


def test_legacy_delete_cannot_bypass_business_recycle_protection(tmp_path):
    api, actor, _ = service(tmp_path)
    api.execute('workflows.create', {'definition': definition()}, actor=actor, idempotency_key='c')
    with pytest.raises(PermissionError, match='RECYCLE_REQUIRED'):
        WorkflowStore(tmp_path).delete_workflow('daily')
    assert WorkflowStore(tmp_path).get_workflow('daily') is not None


def test_real_script_sees_durable_admission_before_process_effect(tmp_path):
    from agent_py_agent.agent.workflows.engine import WorkflowEngine
    from agent_py_agent.tests.test_workflow_script_engine import script, step, workflow
    record = tmp_path / 'data' / 'workflow_runs' / 'exact-running' / 'run.json'
    script(tmp_path, f'import json\nfrom pathlib import Path\nr=json.loads(Path({str(record)!r}).read_text())\n'
                     'print(json.dumps({"value":r["effect_outcome"]}))')
    done = WorkflowEngine(tmp_path).run_workflow(workflow(step()), workflow_run_id='exact-running')
    assert done.status == 'succeeded' and done.final_output == {'value': 'unknown'}


@pytest.mark.parametrize('status', ['succeeded', 'failed', 'skipped', 'cancelled'])
# LLM: 使用真实Store的终态DTO、CAS和步骤落盘路径，不以元数据更新重新授予执行权。
# 函数用途: 覆盖全部终态的元数据更新、晚到写拒绝及旧修订冲突。
def test_terminal_store_allows_metadata_but_rejects_late_execution(tmp_path, status):
    from dataclasses import replace

    from agent_py_agent.agent.workflows.engine import WorkflowEngine

    engine = WorkflowEngine(tmp_path, analysis_handler=lambda *args: {'value': 1})
    first = engine.run_workflow(definition(steps=[
        {'step_id': 'analyze', 'name': '分析', 'mode': 'llm_skill_analysis'}]))
    record = replace(first, workflow_run_id='terminal', status=status)
    engine.store.create_run(record, engine.store.load_workflow_snapshot(first.workflow_run_id))
    metadata = replace(record, workflow_name='同状态元数据更新')
    engine.store.save_run(metadata)
    before = engine.store.load_run(record.workflow_run_id).to_dict()
    with pytest.raises(ValueError, match='WORKFLOW_RUN_TERMINAL'):
        engine.store.save_run(replace(metadata, status='running'))
    with pytest.raises(ValueError, match='WORKFLOW_RUN_TERMINAL'):
        engine.store.save_step_result(record.workflow_run_id, replace(record.steps[0], status='running'))
    with pytest.raises(ValueError, match='REVISION_CONFLICT'):
        engine.store.save_run(record)
    assert engine.store.load_run(record.workflow_run_id).to_dict() == before


@pytest.mark.parametrize('status', ['succeeded', 'failed', 'skipped', 'cancelled'])
# LLM: 原执行锁内只确认已结清义务；控制重放必须返回同一结果且不再推进修订。
# 函数用途: 验证取消已结束运行不会把历史成功或失败改写掉。
def test_cancel_settled_terminal_acknowledges_control_without_rewriting_result(tmp_path, status):
    from dataclasses import replace

    from agent_py_agent.agent.workflows.engine import WorkflowEngine

    engine = WorkflowEngine(tmp_path, analysis_handler=lambda *args: {'value': 1})
    first = engine.run_workflow(definition(steps=[
        {'step_id': 'analyze', 'name': '分析', 'mode': 'llm_skill_analysis'}]))
    record = replace(first, workflow_run_id='terminal', status=status)
    engine.store.create_run(record, engine.store.load_workflow_snapshot(first.workflow_run_id))
    expected = record.revision
    assert engine.cancel_workflow_run(record.workflow_run_id, expected_revision=expected, operation_key='cancel') == []
    current = engine.store.load_run(record.workflow_run_id)
    assert current.status == status and current.steps == record.steps
    assert current.final_output == record.final_output
    operation = engine.store._read(record.workflow_run_id, 'run.json')['control_operations']['cancel']
    assert operation['state'] == 'completed' and operation['receipt']['status'] == status
    assert engine.cancel_workflow_run(record.workflow_run_id, expected_revision=expected, operation_key='cancel') == []
    assert engine.store.load_run(record.workflow_run_id).to_dict() == current.to_dict()


@pytest.mark.parametrize(('query', 'expected'), [
    ('probe-', {'probe-one', 'probe-three'}),
    ('中文巡检', {'probe-one', 'other-two'}),
    ('other-', {'other-two'}),
    ('不存在', set()),
    ('', {'probe-one', 'other-two', 'probe-three'}),
])
def test_native_and_business_workflow_query_share_name_or_id_matching(tmp_path, query, expected):
    import json

    from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool

    api, actor, _ = service(tmp_path)
    for identifier, display in [('probe-one', '中文巡检'), ('other-two', '中文巡检'), ('probe-three', '其它日报')]:
        body = definition(identifier)
        body['descriptor']['name'] = display
        api.execute('workflows.create', {'definition': body}, actor=actor, idempotency_key=identifier)
    tool = WorkflowsTool(tmp_path, store=api.store)
    result = tool.execute({'action': 'list', 'query': query})
    assert result.ok, result.output
    native = {item['descriptor']['id'] for item in json.loads(result.output)['workflows']}
    request = {'query': query, 'limit': 1}
    business = set()
    while True:
        page = api.execute('workflows.list', request, actor=actor)
        business.update(item['workflow_ref'] for item in page['items'])
        if not page['next_cursor']:
            break
        request = {**request, 'cursor': page['next_cursor']}
    assert native == business == expected
    assert len(api.store.list_workflows()) == 3, '既有无参Store接口保持兼容'


def test_workflow_query_keeps_native_hidden_and_business_authorization_filters(tmp_path):
    import json

    from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool

    api, actor, _ = service(tmp_path)
    for identifier, hidden in [('probe-visible', False), ('probe-hidden', True)]:
        body = definition(identifier)
        body['descriptor'].update(name='中文巡检', metadata={'hidden': hidden})
        api.execute('workflows.create', {'definition': body}, actor=actor, idempotency_key=identifier)
    result = WorkflowsTool(tmp_path, store=api.store).execute({'action': 'list', 'query': 'probe-'})
    assert result.ok, result.output
    assert [item['descriptor']['id'] for item in json.loads(result.output)['workflows']] == ['probe-visible']
    api.authorize = lambda current, action, ref: current is actor and ref != 'probe-hidden'
    page = api.execute('workflows.list', {'query': 'probe-', 'limit': 1}, actor=actor)
    assert [item['workflow_ref'] for item in page['items']] == ['probe-visible']
    assert page['next_cursor'] is None


@pytest.mark.parametrize(('filters', 'expected'), [
    ({}, {'active-space', 'recycled-space', 'legacy'}),
    ({'lifecycle': 'active'}, {'active-space', 'legacy'}),
    ({'lifecycle': 'recycled'}, {'recycled-space'}),
    ({'lifecycle': 'all'}, {'active-space', 'recycled-space', 'legacy'}),
    ({'scope_mode': 'spaces'}, {'active-space', 'recycled-space'}),
    ({'scope_mode': 'owner_global'}, {'legacy'}),
    ({'space_id': 's1'}, {'active-space', 'legacy'}),
    ({'space_id': 's2', 'lifecycle': 'recycled'}, {'recycled-space'}),
    ({'space_id': 'other'}, {'legacy'}),
])
def test_native_list_explicit_state_filters_keep_legacy_hidden_and_default_semantics(tmp_path, filters, expected):
    import json

    from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool

    api, actor, _ = service(tmp_path, references=lambda _ref: {'coverage': 'complete', 'blockers': []})
    for identifier, lifecycle, space, hidden in [('active-space', 'active', 's1', False),
            ('recycled-space', 'recycled', 's2', False), ('hidden-recycled', 'recycled', 's2', True)]:
        body = definition(identifier)
        body['descriptor'].update(name='中文流程', metadata={'hidden': hidden})
        api.execute('workflows.create', {'definition': body,
            'scope': {'scope_mode': 'spaces', 'space_ids': [space]}}, actor=actor, idempotency_key=identifier)
        if lifecycle == 'recycled':
            api.execute('workflows.recycle', {'workflow_ref': identifier}, actor=actor,
                        expected_revision=1, idempotency_key='recycle-' + identifier)
    api.store.save_workflow(definition('legacy'))
    result = WorkflowsTool(tmp_path, store=api.store).execute({'action': 'list', **filters})
    assert result.ok, result.output
    assert {row['descriptor']['id'] for row in json.loads(result.output)['workflows']} == expected
    assert {row['workflow_ref'] for row in api.execute('workflows.list', {}, actor=actor)['items']} == {'active-space', 'legacy'}


def test_business_list_parses_workflow_state_once_per_authorized_definition(tmp_path, monkeypatch):
    from agent_py_agent.agent.workflows import business_scope

    api, actor, _ = service(tmp_path)
    api.execute('workflows.create', {'definition': definition('one')}, actor=actor, idempotency_key='one')
    actual, calls = business_scope.workflow_state, []
    def observed(value):
        calls.append(value.descriptor.id)
        return actual(value)
    monkeypatch.setattr(business_scope, 'workflow_state', observed)
    assert api.execute('workflows.list', {}, actor=actor)['items'][0]['workflow_ref'] == 'one'
    assert calls == ['one']


def test_create_definition_decode_error_is_not_started_but_store_error_remains_unknown(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.business_access import BusinessError
    from agent_py_agent.agent.gateway_parts.business_api import _error_code

    api, actor, _ = service(tmp_path)
    bad = definition('schema-version-probe')
    bad['descriptor']['schema_version'] = 'v1'
    called = []
    original = api.store.apply_business_operation
    monkeypatch.setattr(api.store, 'apply_business_operation', lambda *a, **k: called.append(True))
    with pytest.raises(BusinessError) as rejected:
        api.execute('workflows.create', {'definition': bad}, actor=actor, idempotency_key='version-probe')
    assert _error_code(rejected.value) == ('INVALID_SCHEMA', 400, 'not_started')
    assert rejected.value.details['field'] == 'definition.descriptor.schema_version'
    assert rejected.value.details['actual'] == 'v1'
    assert rejected.value.details['expected'] == 'asset_descriptor.v1'
    assert rejected.value.details['reason'] == '不支持的资产协议版本'
    assert rejected.value.details['effect_outcome'] == 'not_started' and not called
    assert api.store.get_workflow('schema-version-probe') is None
    monkeypatch.setattr(api.store, 'apply_business_operation', original)

    def write_unknown(*args, **kwargs):
        raise ValueError('write boundary failure')

    monkeypatch.setattr(api.store, 'apply_business_operation', write_unknown)
    with pytest.raises(ValueError, match='write boundary failure') as unknown:
        api.execute('workflows.create', {'definition': definition('write-probe')},
                    actor=actor, idempotency_key='write-probe')
    assert not isinstance(unknown.value, BusinessError), '写入边界异常不得误标为未执行'


def test_scope_set_empty_command_atomically_orphans_and_replays(tmp_path):
    api, actor, authorized = service(tmp_path)
    api.clock = lambda: 101.0
    api.execute('workflows.create', {'definition': definition(),
        'scope': {'scope_mode': 'spaces', 'space_ids': ['a', 'b']}}, actor=actor, idempotency_key='create')
    api.execute('workflows.scope.set', {'workflow_ref': 'daily',
        'scope': {'scope_mode': 'spaces', 'space_ids': ['b']}}, actor=actor,
        expected_revision=1, idempotency_key='remove-a')
    before = api.store.get_workflow('daily')
    request = {'workflow_ref': 'daily', 'scope': {'scope_mode': 'spaces', 'space_ids': []}}
    authorized['value'] = False
    with pytest.raises(PermissionError):
        api.execute('workflows.scope.set', request, actor=actor, expected_revision=2, idempotency_key='denied')
    authorized['value'] = True
    with pytest.raises(ValueError):
        api.execute('workflows.scope.set', request, actor=actor, expected_revision=1, idempotency_key='stale')
    assert api.store.get_workflow('daily').to_dict() == before.to_dict()
    api.clock = lambda: 202.0
    result = api.execute('workflows.scope.set', request, actor=actor,
        expected_revision=2, idempotency_key='remove-last')
    saved = api.store.get_workflow('daily')
    state = saved.descriptor.metadata['business_workflow']
    assert result['scope'] == {'scope_mode': 'unassigned', 'space_ids': []}
    assert result['scope_revision'] == 3
    assert state['orphan_since'] == 202.0
    assert state['scope']['scope_mode'] == 'unassigned' and state['scope']['space_ids'] == []
    assert saved.execution_definition_ref == before.execution_definition_ref
    authorized['value'] = False
    with pytest.raises(PermissionError):
        api.execute('workflows.scope.set', request, actor=actor,
            expected_revision=2, idempotency_key='remove-last')
    authorized['value'] = True
    assert api.execute('workflows.scope.set', request, actor=actor,
        expected_revision=2, idempotency_key='remove-last') == result
    assert api.store.get_workflow('daily').to_dict() == saved.to_dict()
    with pytest.raises(PermissionError, match='WORKFLOW_UNASSIGNED'):
        api.execute('workflows.run', {'workflow_ref': 'daily'}, actor=actor, idempotency_key='cannot-run')


@pytest.mark.parametrize('old_mode', ['owner_global', 'unassigned'])
def test_empty_remove_command_requires_previously_bound_spaces(tmp_path, old_mode):
    api, actor, _ = service(tmp_path)
    api.execute('workflows.create', {'definition': definition(),
        'scope': {'scope_mode': old_mode, 'space_ids': []}}, actor=actor, idempotency_key='create')
    before = api.store.get_workflow('daily').to_dict()
    with pytest.raises(PermissionError, match='WORKFLOW_SCOPE_REMOVE_REQUIRES_BOUND_SPACES'):
        api.execute('workflows.scope.set', {'workflow_ref': 'daily',
            'scope': {'scope_mode': 'spaces', 'space_ids': []}}, actor=actor,
            expected_revision=1, idempotency_key='invalid-remove')
    assert api.store.get_workflow('daily').to_dict() == before


@pytest.mark.parametrize('action', ['workflows.create', 'workflows.restore'])
def test_empty_spaces_remains_invalid_outside_scope_set(tmp_path, action):
    from agent_py_agent.agent.workflows.business_scope import validate_business_request

    request = {'scope': {'scope_mode': 'spaces', 'space_ids': []},
               **({'definition': definition()} if action == 'workflows.create' else {'workflow_ref': 'daily'})}
    with pytest.raises(ValueError):
        validate_business_request(action, request, expected_revision=1, idempotency_key='unchanged')
    with pytest.raises(ValueError):
        WorkflowScope('spaces', (), 1)
