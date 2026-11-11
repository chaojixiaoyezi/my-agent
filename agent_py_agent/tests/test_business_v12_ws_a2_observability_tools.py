"""WS-A2业务观察规范测试：涵盖BIZ-002空thread_id与空间过滤分页、BIZ-013工具回执投影及新增Reader。

LLM: 专职检验空thread_id容错、空间过滤扫描预算分页、耗时/生效结果/脱敏输入投影及missing readers。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.background_team_lifecycle import (
    BACKGROUND_IDENTITY_KEY,
)
from agent_py_agent.agent.conversation.business_access import BusinessActor, BusinessError
from agent_py_agent.agent.conversation.business_contracts import (
    BusinessContextIntent,
    BusinessExecutionContext,
)
from agent_py_agent.agent.conversation.business_observability import (
    BusinessObservationService,
)
from agent_py_agent.agent.conversation.models import ConversationThread
from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.conversation.store_threads import create_thread_atomic
from agent_py_agent.agent.gateway_parts.business_api_v13_readers import (
    default_observation_readers,
)
from agent_py_agent.agent.plugin_host.business_action_specs import (
    business_action_spec,
    validate_object,
)
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.tooling.business_spaces_tool import BusinessSpacesTool
from agent_py_agent.agent.tooling.executor import _bound_redacted_input


# LLM: 创建隔离测试环境，含ConversationStore、RuntimeRepository及基础线程。
# 函数用途: 初始化测试所需的只读观察服务及宿主夹具。
def _make_observability_env(tmp_path: Path, owner: str = 'local/ws-a2'):
    store = ConversationStore(tmp_path / 'conversations')
    for identifier in ('controller', 'worker'):
        metadata = (
            {'business_controller': True}
            if identifier == 'controller'
            else {
                BACKGROUND_IDENTITY_KEY: {
                    'agent_id': identifier,
                    'managed_by_thread_id': 'controller',
                    'space_id': 'space-a',
                    'project_ref': None,
                    'identity_state': 'available',
                    'after_task': 'reuse',
                    'identity_revision': 1,
                }
            }
        )
        create_thread_atomic(
            store.threads,
            ConversationThread(
                thread_id=identifier,
                owner_id=owner,
                canonical_user_id=owner,
                title=identifier,
                metadata=metadata,
            ),
        )
    intent = BusinessContextIntent(owner, 'space-a', None, None)
    task = store.session_tasks.create(
        SessionTaskDraft('controller', 'worker', 'WS-A2验收任务', dedupe_key='ws-a2-task-1'),
        business_context_intent=intent,
        business_context_factory=lambda tid: BusinessExecutionContext(
            owner, 'space-a', None, tid, 1, None, (), (), 'business-context.v1'
        ),
    )
    repo = RuntimeRepository(tmp_path / 'runtime.db')
    readers = default_observation_readers(tmp_path, owner, store, repo)
    service = BusinessObservationService(
        tmp_path, owner_id=owner, store=store, runtime=repo, readers=readers, cursor_key=b'k' * 32
    )
    actor = BusinessActor(owner, 'tester', service, lambda _act, _facts: True)
    return SimpleNamespace(
        store=store,
        repo=repo,
        task=task,
        service=service,
        actor=actor,
        owner=owner,
        tmp_path=tmp_path,
    )


# LLM: 验证BIZ-002中当tasks.thread_id为空字符串时，executions.list与events.list不报503 DOMAIN_RESULT_INVALID。
# 函数用途: 验证空thread_id被映射为NullableId的None或被安全解析/过滤，严格满足Schema。
def test_biz002_empty_thread_id_does_not_raise_domain_result_invalid(tmp_path: Path):
    env = _make_observability_env(tmp_path)
    run1 = env.repo.record_run_creation(
        owner_id=env.owner,
        run_id='run-with-empty-thread',
        thread_id='',
        task_run_metadata={
            'business_context': env.task.metadata['business_context'],
            'session_task_ref': env.task.task_id,
        },
    )
    run2 = env.repo.record_run_creation(
        owner_id=env.owner,
        run_id='run-with-valid-thread',
        thread_id='worker',
        task_run_metadata={
            'business_context': env.task.metadata['business_context'],
            'session_task_ref': env.task.task_id,
        },
    )

    with env.repo._runtime_connection() as conn:
        conn.execute(
            "UPDATE tasks SET thread_id='' WHERE task_id=?",
            (run1['task_id'],),
        )
        conn.commit()

    exec_res = env.service.query('executions.list', {'filter': {'space_id': 'space-a'}}, actor=env.actor)
    assert isinstance(exec_res, dict)
    assert 'items' in exec_res
    for item in exec_res['items']:
        assert item['thread_id'] != ''
        assert len(item['thread_id']) >= 1

    events_res = env.service.query('events.list', {'filter': {'space_id': 'space-a'}}, actor=env.actor)
    assert isinstance(events_res, dict)
    assert 'events' in events_res
    validate_object(business_action_spec('events.list').result_schema, events_res)
    for event in events_res['events']:
        node = event.get('node')
        if node is not None:
            assert node.get('thread_id') is None or len(node.get('thread_id', '')) >= 1


# LLM: 验证BIZ-002空间过滤分页时，游标批量扫描跳过非匹配空间，首页不被假空截断。
# 函数用途: 保证5000用户多空间混杂时，前置大量异空间数据不会使目标空间查询返回假空首页。
def test_biz002_space_filtered_pagination_skips_non_matching_rows(tmp_path: Path):
    env = _make_observability_env(tmp_path)
    intent_b = BusinessContextIntent(env.owner, 'space-b', None, None)
    task_b = env.store.session_tasks.create(
        SessionTaskDraft('controller', 'worker', '异空间任务', dedupe_key='ws-a2-task-b'),
        business_context_intent=intent_b,
        business_context_factory=lambda tid: BusinessExecutionContext(
            env.owner, 'space-b', None, tid, 1, None, (), (), 'business-context.v1'
        ),
    )

    for i in range(15):
        env.repo.record_run_creation(
            owner_id=env.owner,
            run_id=f'run-space-b-{i}',
            thread_id='worker',
            task_run_metadata={
                'business_context': task_b.metadata['business_context'],
                'session_task_ref': task_b.task_id,
            },
        )

    env.repo.record_run_creation(
        owner_id=env.owner,
        run_id='run-space-a-target',
        thread_id='worker',
        task_run_metadata={
            'business_context': env.task.metadata['business_context'],
            'session_task_ref': env.task.task_id,
        },
    )

    page_a = env.service.query('executions.list', {'filter': {'space_id': 'space-a'}, 'limit': 5}, actor=env.actor)
    assert isinstance(page_a, dict)
    assert len(page_a['items']) >= 1
    found_ids = {item['run_id'] for item in page_a['items']}
    assert 'run-space-a-target' in found_ids

    events_a = env.service.query('events.list', {'filter': {'space_id': 'space-a'}, 'limit': 5}, actor=env.actor)
    assert isinstance(events_a, dict)
    assert len(events_a['events']) >= 1


# LLM: 验证BIZ-013输入脱敏边界与大小上限裁剪（<= 16384字节）。
# 函数用途: 验证_bound_redacted_input对敏感键的脱敏及超长字典的深度及字节截断。
def test_biz013_bound_redacted_input_sanitization():
    sensitive_args = {
        'api_key': 'secret-token-12345678',
        'password': 'my-super-secret-password',
        'normal_field': 'hello world',
        'nested': {'token': 'nested-secret-value', 'safe': 'safe-data'},
    }
    redacted = _bound_redacted_input(sensitive_args)
    redacted_str = json.dumps(redacted)
    assert 'secret-token' not in redacted_str
    assert 'my-super-secret-password' not in redacted_str
    assert 'nested-secret-value' not in redacted_str
    assert redacted['normal_field'] == 'hello world'
    assert redacted['nested']['safe'] == 'safe-data'
    assert len(redacted_str.encode('utf-8')) <= 16384

    huge_args = {'data': 'a' * 30000, 'safe': 'ok'}
    bounded = _bound_redacted_input(huge_args, max_bytes=512)
    bounded_bytes = len(json.dumps(bounded).encode('utf-8'))
    assert bounded_bytes <= 1024


# LLM: 验证BIZ-013与V05-04中tool_calls.get读取真实outcome中的duration_ms、effect_outcome与redacted_input。
# 函数用途: 核对工具回执详细投影，确保耗时>=0、生效结果为规范枚举、脱敏输入被正确投射且符合Schema。
def test_biz013_tool_calls_get_canonical_projection(tmp_path: Path):
    env = _make_observability_env(tmp_path)
    run = env.repo.record_run_creation(
        owner_id=env.owner,
        run_id='run-tool-test',
        thread_id='worker',
        task_run_metadata={
            'business_context': env.task.metadata['business_context'],
            'session_task_ref': env.task.task_id,
        },
    )
    op = env.repo.create_tool_operation(
        agent_run_id=run['agent_run_id'],
        attempt_id=run['attempt_id'],
        operation_type='business_spaces',
    )

    outcome_data = {
        'result': {
            'schema_version': 'tool_execution_result.v1',
            'tool': 'business_spaces',
            'ok': True,
            'handler_executed': True,
            'effect_outcome': 'committed',
            'duration_ms': 125.5,
            'result_ref': 'res-biz-space-1',
            'redacted_input': {'action': 'spaces.create', 'space_id': 'sp-new'},
        }
    }
    with env.repo._runtime_connection() as conn:
        conn.execute(
            "UPDATE tool_operations SET status='SUCCEEDED', outcome_json=?, settled_at=100 WHERE operation_id=?",
            (json.dumps(outcome_data), op['operation_id']),
        )
        conn.commit()

    detail = env.service.query('tool_calls.get', {'operation_id': op['operation_id']}, actor=env.actor)
    spec = business_action_spec('tool_calls.get')
    validate_object(spec.result_schema, detail)

    assert detail['effect_outcome'] == 'committed'
    assert detail['tool_call']['duration_ms'] == 125.5
    assert detail['tool_call']['duration_ms'] >= 0
    assert detail['redacted_input'] == {'action': 'spaces.create', 'space_id': 'sp-new'}
    assert detail['result_ref'] == 'res-biz-space-1'


# LLM: 验证BusinessSpacesTool对只读动作标none、变更动作标committed、业务错误标failed、校验拒绝标not_started。
# 函数用途: 验证业务空间工具对effect_outcome的规范回填。
def test_biz013_business_spaces_tool_effect_outcome_canonical():
    class FakeService:
        owner_id = 'local/test'

        def authorize_call(self, action, params, actor):
            pass

        def call(self, action, params, actor, expected_revision=None, idempotency_key=None):
            if action == 'spaces.list':
                return {'spaces': []}
            if action == 'spaces.create':
                if params.get('name') == 'Error Space':
                    raise BusinessError('BUSINESS_SPACE_DUPLICATE')
                return {'space_id': 'sp-123'}
            return {'ok': True}

    service = FakeService()
    actor = BusinessActor('local/test', 'user', service, lambda a, f: True)
    tool = BusinessSpacesTool(service, actor=actor)

    res_list = tool.execute({'action': 'spaces.list', 'params': {}, 'request_id': 'req-list-1234'})
    assert res_list.ok is True
    assert res_list.effect_outcome == 'none'

    res_create = tool.execute({
        'action': 'spaces.create',
        'request_id': 'req-create-1234',
        'idempotency_key': 'idemp-key-12345678',
        'params': {'name': 'Valid Space'},
    })
    assert res_create.ok is True
    assert res_create.effect_outcome == 'committed'

    res_fail = tool.execute({
        'action': 'spaces.create',
        'request_id': 'req-create-1235',
        'idempotency_key': 'idemp-key-12345679',
        'params': {'name': 'Error Space'},
    })
    assert res_fail.ok is False
    assert res_fail.effect_outcome == 'failed'

    res_bad = tool.execute({'action': 'spaces.create', 'params': {}, 'request_id': 'req-bad'})
    assert res_bad.ok is False
    assert res_bad.effect_outcome == 'not_started'


# LLM: 验证missing reader changes.get的数据投射与Schema合规性。
# 函数用途: 验证changes.get能正确投射产物与任务变更事实，并满足json schema。
def test_changes_get_reader_conforms_to_schema(tmp_path: Path):
    env = _make_observability_env(tmp_path)
    run = env.repo.record_run_creation(
        owner_id=env.owner,
        run_id='run-changes-test',
        thread_id='worker',
        task_run_metadata={
            'business_context': env.task.metadata['business_context'],
            'session_task_ref': env.task.task_id,
        },
    )
    digest = hashlib.sha256(b'test artifact content').hexdigest()
    with env.repo._runtime_connection() as conn:
        conn.execute(
            'INSERT INTO artifact_records(artifact_record_id,attempt_id,agent_run_id,rel_path,content_digest,size,created_at) '
            'VALUES(?,?,?,?,?,?,?)',
            ('art-ws-a2-1', run['attempt_id'], run['agent_run_id'], 'out/report.md', digest, 21, 10),
        )
        conn.commit()

    changes_res = env.service.query('changes.get', {'task_id': run['task_id']}, actor=env.actor)
    spec = business_action_spec('changes.get')
    validate_object(spec.result_schema, changes_res)

    assert changes_res['coverage'] == 'complete'
    assert 'art-ws-a2-1' in changes_res['evidence_refs']
    assert any(c['target_ref'] == 'art-ws-a2-1' and c['field'] == 'content_digest' for c in changes_res['changes'])


# LLM: 验证missing reader permissions.get的数据投射与Schema合规性。
# 函数用途: 验证permissions.get对controller与subagent目标的权限有效范围与来源投影。
def test_permissions_get_reader_conforms_to_schema(tmp_path: Path):
    env = _make_observability_env(tmp_path)
    spec = business_action_spec('permissions.get')

    ctrl_perm = env.service.query(
        'permissions.get',
        {'target': {'kind': 'controller', 'id': 'controller'}},
        actor=env.actor,
    )
    validate_object(spec.result_schema, ctrl_perm)
    assert ctrl_perm['effective_scope']['owner_id'] == env.owner
    assert 'controller' in ctrl_perm['effective_scope']['thread_ids']

    subagent_perm = env.service.query(
        'permissions.get',
        {'target': {'kind': 'subagent', 'id': 'worker'}},
        actor=env.actor,
    )
    validate_object(spec.result_schema, subagent_perm)
    assert subagent_perm['effective_scope']['owner_id'] == env.owner


# LLM: 验证missing reader operations.get跨不同事实源的数据投射与Schema合规性。
# 函数用途: 验证operations.get跨tool_operations、receipts、threads、workflow runs查询并返回规范回执。
def test_operations_get_reader_conforms_to_schema(tmp_path: Path):
    env = _make_observability_env(tmp_path)
    spec = business_action_spec('operations.get')

    run = env.repo.record_run_creation(
        owner_id=env.owner,
        run_id='run-op-test',
        thread_id='worker',
        task_run_metadata={
            'business_context': env.task.metadata['business_context'],
            'session_task_ref': env.task.task_id,
        },
    )
    op = env.repo.create_tool_operation(
        agent_run_id=run['agent_run_id'],
        attempt_id=run['attempt_id'],
        operation_type='read_file',
    )
    with env.repo._runtime_connection() as conn:
        conn.execute(
            "UPDATE tool_operations SET status='SUCCEEDED', outcome_json=? WHERE operation_id=?",
            (
                json.dumps(
                    {
                        'result': {
                            'schema_version': 'tool_execution_result.v1',
                            'tool': 'read_file',
                            'ok': True,
                            'handler_executed': True,
                            'effect_outcome': 'none',
                            'duration_ms': 50,
                            'result_ref': 'ref-file-1',
                        }
                    }
                ),
                op['operation_id'],
            ),
        )
        conn.commit()

    op_res = env.service.query('operations.get', {'operation_id': op['operation_id']}, actor=env.actor)
    validate_object(spec.result_schema, op_res)
    assert op_res['operation_id'] == op['operation_id']
    assert op_res['state'] == 'completed'
    assert 'ref-file-1' in op_res['target_refs']

    receipts_dir = tmp_path / 'data' / 'business' / 'receipts'
    receipts_dir.mkdir(parents=True, exist_ok=True)
    receipt_file = receipts_dir / 'op-receipt-123.json'
    receipt_file.write_text(
        json.dumps(
            {
                'owner_id': env.owner,
                'receipt': {
                    'result': {
                        'space_id': 'target-rec-1',
                    }
                },
            }
        ),
        encoding='utf-8',
    )
    receipt_res = env.service.query('operations.get', {'operation_id': 'op-receipt-123'}, actor=env.actor)
    validate_object(spec.result_schema, receipt_res)
    assert receipt_res['state'] == 'completed'
    assert 'target-rec-1' in receipt_res['target_refs']

    with pytest.raises(BusinessError, match='OBSERVATION_NOT_FOUND'):
        env.service.query('operations.get', {'operation_id': 'non-existent-op'}, actor=env.actor)
