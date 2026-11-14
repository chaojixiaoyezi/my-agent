# 模块用途: BIZ-012/BIZ-011/V10-03 的 W3 原生回执回归：工作流写入前确定的拒绝（版本/幂等冲突、引用或覆盖未知、
#   重复创建、未分配范围运行）经 /client/business 返回结构化 409 与 not_started，状态读回不变；
#   真正写入路径上的 OSError 仍保留 OUTCOME_UNKNOWN。只用 tmp 隔离 home，不是线上证据。
from __future__ import annotations

import pytest

from agent_py_agent.agent.assets.workflow_store import WorkflowStore
from agent_py_agent.tests.test_business_v12_background_scheduling import (  # noqa: F401 - pytest fixture
    call,
    native,
    ok,
)
from agent_py_agent.tests.test_business_v12_workflow_foreach_schema import flow


def definition(name):
    value = flow(name, None)
    value['steps'] = [{'step_id': 'calc', 'name': 'calc', 'mode': 'deterministic_script', 'script_id': 'calc'}]
    return value


def refused(native, action, params, code, **extra):  # noqa: F811
    status, payload = call(native, action, params, **extra)
    assert (status, payload['error']['code'], payload['effect_outcome']) == (409, code, 'not_started'), payload
    return payload


def test_space_name_conflict_is_not_retryable_and_preserves_original(native):  # noqa: F811
    original = ok(native, 'spaces.create', {'name': '同名空间'}, idempotency_key='space-conflict-create-001')
    payload = refused(native, 'spaces.create', {'name': '同名空间'}, 'SPACE_NAME_CONFLICT',
                      idempotency_key='space-conflict-create-002')
    assert payload['error']['retryable'] is False
    after = ok(native, 'spaces.get', {'space_id': original['space_id']})
    assert after['space']['revision'] == original['revision']
    assert after['space']['name'] == original['name']


@pytest.mark.parametrize('code', ['SPACE_NAME_CONFLICT', 'ASSET_VERSION_CONFLICT', 'BINDING_CONFLICT',
                                  'SOURCE_INTENT_CONFLICT'])
def test_deterministic_catalog_conflicts_keep_not_started_outcome(code):
    from agent_py_agent.agent.conversation.business_access import BusinessError
    from agent_py_agent.agent.gateway_parts.business_api import _error_code

    assert _error_code(BusinessError(code)) == (code, 409, 'not_started')


def test_deterministic_workflow_refusals_are_not_started_and_leave_state_unchanged(native):  # noqa: F811
    ok(native, 'workflows.create', {'definition': definition('receipt-flow')}, idempotency_key='receipt-create-000001')
    refused(native, 'workflows.create', {'definition': definition('receipt-flow')}, 'WORKFLOW_ALREADY_EXISTS',
            idempotency_key='receipt-create-000002')
    unassigned = {'scope_mode': 'unassigned', 'space_ids': []}
    refused(native, 'workflows.scope.set', {'workflow_ref': 'receipt-flow', 'scope': unassigned}, 'REVISION_CONFLICT',
            idempotency_key='receipt-scope-0000001', expected_revision=9)
    ok(native, 'workflows.scope.set', {'workflow_ref': 'receipt-flow', 'scope': unassigned},
       idempotency_key='receipt-scope-0000002', expected_revision=1)
    refused(native, 'workflows.scope.set', {'workflow_ref': 'receipt-flow',
            'scope': {'scope_mode': 'owner_global', 'space_ids': []}}, 'IDEMPOTENCY_CONFLICT',
            idempotency_key='receipt-scope-0000002', expected_revision=2)
    before = ok(native, 'workflows.get', {'workflow_ref': 'receipt-flow'})
    # 默认宿主已有引用读口；显式损坏真实调度来源，验证未知覆盖仍在写入前拒绝。
    _, agent = native
    scheduler_source = agent.home_paths.owner_home_dir / 'data' / 'scheduler' / 'store.json'
    scheduler_source.parent.mkdir(parents=True, exist_ok=True)
    scheduler_source.write_text('{broken-reference-source', encoding='utf-8')
    payload = refused(native, 'workflows.recycle', {'workflow_ref': 'receipt-flow'},
                      'WORKFLOW_REFERENCED_OR_COVERAGE_UNKNOWN', idempotency_key='receipt-recycle-00001',
                      expected_revision=before['workflow']['definition_revision'])
    assert {'key': 'blocker_reason_code', 'value': 'REFERENCE_COVERAGE_UNKNOWN'} in payload['error']['details']
    after = ok(native, 'workflows.get', {'workflow_ref': 'receipt-flow'})
    assert after['workflow'] == before['workflow'] and after['workflow']['lifecycle'] == 'active'
    assert after['scope'] == unassigned and after['workflow']['scope_revision'] == 2


def test_write_path_failure_keeps_outcome_unknown(native, monkeypatch):  # noqa: F811
    def broken_save(self, definition):
        raise OSError('disk unavailable')

    monkeypatch.setattr(WorkflowStore, '_save', broken_save)
    status, payload = call(native, 'workflows.create', {'definition': definition('receipt-write')},
                           idempotency_key='receipt-write-0000001')
    assert (status, payload['error']['code'], payload['effect_outcome']) == (503, 'OUTCOME_UNKNOWN', 'unknown')


def test_unassigned_run_is_an_explicit_policy_refusal_when_the_run_port_is_wired(native):  # noqa: F811
    from agent_py_agent.agent.conversation.business_context import resolve_business_context
    from agent_py_agent.agent.workflows.engine import WorkflowEngine

    server, agent = native
    created_engines = []

    def engine_factory(owner_home, **options):
        created_engines.append(owner_home)
        return WorkflowEngine(owner_home, **options)

    # 生产组合当前没有这两个端口（见 HANDOFF G5）；这里按现有测试组合方式接入，只验证范围策略的回执。
    agent.workflow_engine_factory = engine_factory
    agent.business_context_provider = lambda actor, request: resolve_business_context(
        actor=actor, task_id='native-workflow-source', requested_space_id=request.get('space_id'),
        project_ref=None, binding_id=None)
    ok(native, 'workflows.create', {'definition': definition('receipt-run'),
       'scope': {'scope_mode': 'unassigned', 'space_ids': []}}, idempotency_key='receipt-run-create-01')
    refused(native, 'workflows.run', {'workflow_ref': 'receipt-run'}, 'WORKFLOW_UNASSIGNED',
            idempotency_key='receipt-run-00000001')
    assert created_engines == [] and ok(native, 'workflows.get', {'workflow_ref': 'receipt-run'})['run_refs'] == []
