from __future__ import annotations

import ast
from pathlib import Path

import pytest


def test_recovery_action_registry_accepts_enum_and_known_values() -> None:
    from agent_py_agent.agent.contracts.recovery import (
        RecoveryAction,
        known_recovery_action_values,
        recovery_action_value,
    )

    assert RecoveryAction.CLOSEOUT.value in known_recovery_action_values()
    assert recovery_action_value(RecoveryAction.REPAIR) == "repair"
    assert recovery_action_value("repair_structured_checkpoint_json") == "repair_structured_checkpoint_json"


def test_recovery_action_registry_uses_single_action_names() -> None:
    from agent_py_agent.agent.contracts.error_taxonomy import ERROR_CONTRACTS
    from agent_py_agent.agent.contracts.recovery import RECOVERY_ACTIONS

    assert all("_or_" not in action for action in RECOVERY_ACTIONS)
    assert "request_approval_or_stop" not in RECOVERY_ACTIONS
    assert "change_strategy_or_stop" not in RECOVERY_ACTIONS
    assert "repair_or_request_capability" not in RECOVERY_ACTIONS

    taxonomy_actions = {contract.recommended_action for contract in ERROR_CONTRACTS.values()}
    assert taxonomy_actions.issubset(set(RECOVERY_ACTIONS))
    assert all("_or_" not in action for action in taxonomy_actions)


def test_recovery_action_registry_rejects_unknown_strings() -> None:
    from agent_py_agent.agent.contracts.recovery import recovery_action_value

    with pytest.raises(ValueError, match="unknown recovery action"):
        recovery_action_value("invented_action")


def test_literal_recommended_actions_use_unified_vocabulary() -> None:
    from agent_py_agent.agent.contracts.recovery import RECOVERY_ACTIONS

    allowed = set(RECOVERY_ACTIONS)
    assert _unknown_literal_recommended_actions(allowed) == []


@pytest.mark.parametrize("code,category,retryable,action", [
    ("FILE_SNAPSHOT_STALE", "state", True, "change_strategy"),
    ("SCHEDULER_HISTORY_READ_FAILED", "state", False, "report_blocker"),
    ("PARENT_TOOL_ACTION_EVIDENCE_MISSING", "capability", False, "report_blocker"),
])
def test_e905_read_and_authority_failures_have_safe_recovery_contracts(code, category, retryable, action):
    from agent_py_agent.agent.contracts.error_taxonomy import ERROR_CONTRACTS

    contract = ERROR_CONTRACTS.get(code)
    assert contract is not None
    assert (contract.code, contract.category, contract.retryable, contract.recommended_action) == (
        code, category, retryable, action,
    )
    assert contract.recovery_hint


def _unknown_literal_recommended_actions(allowed: set[str]) -> list[tuple[str, int, str]]:
    roots = (
        Path("agent_py_agent/agent/contracts"),
        Path("agent_py_agent/agent/agent_core"),
        Path("agent_py_agent/agent/tooling"),
    )
    unknown: list[tuple[str, int, str]] = []
    for root in roots:
        for path in root.rglob("*.py"):
            unknown.extend(_unknown_literal_recommended_actions_in_file(path, allowed))
    return unknown


def _unknown_literal_recommended_actions_in_file(path: Path, allowed: set[str]) -> list[tuple[str, int, str]]:
    unknown: list[tuple[str, int, str]] = []
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        value = _literal_recommended_action(node)
        if value and value not in allowed:
            unknown.append((str(path), node.lineno, value))
    return unknown


def _literal_recommended_action(node: ast.AST) -> str:
    if not isinstance(node, ast.Call):
        if isinstance(node, ast.Dict):
            return _literal_dict_recommended_action(node)
        return ""
    for keyword in node.keywords:
        if keyword.arg != "recommended_action":
            continue
        return _constant_string(keyword.value)
    return ""


def _literal_dict_recommended_action(node: ast.Dict) -> str:
    for key, value in zip(node.keys, node.values, strict=False):
        if not isinstance(key, ast.Constant) or key.value != "recommended_action":
            continue
        return _constant_string(value)
    return ""


def _constant_string(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


@pytest.mark.parametrize('code,action', [
    ('GATEWAY_HISTORY_INDEX_PENDING', 'wait_for_local_progress'),
    ('WORKFLOW_ARTIFACT_RUN_LOCATOR_INCOMPLETE', 'read_artifact_ref'),
    ('WORKFLOW_COMPLETION_CLOCK_FAILED', 'wait_for_existing_operation'),
    ('WORKFLOW_ISOLATION_UNAVAILABLE', 'change_strategy'),
    ('WORKFLOW_REPLAN_READY', 'repair_against_acceptance_findings'),
    ('WORKFLOW_REPLAN_FAILED', 'report_blocker'),
    ('WORKFLOW_REWORK_LIMIT_REACHED', 'request_user_input'),
    ('WORKFLOW_REWORK_NO_PROGRESS', 'request_user_input'),
])
def test_exact_recovery_state_never_grants_automatic_effect_replay(code, action):
    from agent_py_agent.agent.contracts.error_taxonomy import error_contract
    from agent_py_agent.agent.contracts.recovery import recovery_action_value

    contract = error_contract(code)
    assert contract.code == code and contract.recommended_action == action
    assert contract.retryable is False and contract.recovery_hint
    assert recovery_action_value(contract.recommended_action) == action
    assert not hasattr(contract, 'effect_outcome')  # 分类本身不能补造执行事实。


def test_read_projection_pending_preserves_partial_facts_and_original_code():
    from agent_py_agent.agent.contracts.error_taxonomy import error_contract
    from agent_py_agent.agent.workflows.run_list_index import WorkflowRunIndexRecoveryPending

    error = WorkflowRunIndexRecoveryPending(1)
    contract = error_contract(error.error_code)
    assert contract.code == 'WORKFLOW_RUN_INDEX_RECOVERY_PENDING'
    assert contract.retryable and contract.recommended_action == 'wait_for_local_progress'
    assert error.details['coverage'] == 'partial' and error.details['canonical_writes'] is False
    assert error.details['projection_may_have_advanced'] is True


def test_review_permission_refusal_keeps_action_and_human_hint_separate():
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.self_map_bootstrap import _WorkflowToolDispatcher
    from agent_py_agent.agent.workflows.business_scope import WorkflowPolicyRefusal

    reads = []
    snapshot = SimpleNamespace(runtime=lambda _name: None)
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_id='local/main'),
        tools=SimpleNamespace(runtime_snapshot=lambda allowed_tools=None, run_id="": snapshot))
    dispatcher = _WorkflowToolDispatcher(agent, None, lambda read: reads.append(read))
    step = SimpleNamespace(step_id='step', input_snapshot={})
    with pytest.raises(WorkflowPolicyRefusal) as refused:
        dispatcher.workflow_review_precondition(run=SimpleNamespace(owner_id='local/main'), step=step,
            definition={'steps': [{'step_id': 'step', 'tool_spec': {'tool_name': 'missing', 'arguments': {}}}]},
            engine=SimpleNamespace(), outputs={})
    assert refused.value.error_code == 'WORKFLOW_TOOL_PERMISSION_DENIED'
    assert refused.value.details == {'effect_outcome': 'not_started', 'recommended_action': 'request_permission',
        'recovery_hint': '未读取资源；请由原执行者核对当轮工具授权与可用性。'}
    assert reads == []


def test_busy_control_and_reconciliation_keep_original_effect_uncertainty():
    from agent_py_agent.agent.contracts.error_taxonomy import error_contract
    from agent_py_agent.agent.workflows.governance import WorkflowReconciliationRequired
    from agent_py_agent.agent.workflows.run_store import _RunBusy

    busy = _RunBusy('原运行仍占用')
    assert busy.code == 'WORKFLOW_RUN_BUSY' and busy.outcome == 'not_started'
    assert busy.details['active_execution_may_have_effects'] is True
    assert busy.details['recommended_action'] == 'wait_for_existing_operation'
    assert busy.details['recovery_hint']
    pending = WorkflowReconciliationRequired('unconfirmed', workflow_run_id='run',
        step_id='step', prior_effect_outcome='unknown', resolution_available=False)
    assert pending.details['prior_effect_outcome'] == 'unknown'
    assert pending.details['resolution_available'] is False
    assert pending.details['recommended_action'] == error_contract(pending.error_code).recommended_action
    assert pending.details['recovery_hint']
    assert error_contract('WORKFLOW_NOTIFICATION_RECEIPT_STORE_UNAVAILABLE').retryable is False


def test_pending_review_keeps_specific_machine_action_and_original_review_fields():
    from agent_py_agent.agent.workflows.native_governance import WorkflowReviewPending

    binding = {'requirement_id': 'requirement', 'input_version': 'version'}
    error = WorkflowReviewPending(binding)
    assert error.error_code == 'WORKFLOW_REVIEW_REQUIRED' and error.binding == binding
    assert error.details['requirement_id'] == 'requirement' and error.details['input_version'] == 'version'
    assert error.details['recommended_action'] == 'request_approval'
    assert 'manage_requirement_provenance' in error.details['recovery_hint']


def test_history_scope_denial_remains_precise_without_retry_or_authority_ticket():
    from agent_py_agent.agent.contracts.error_taxonomy import error_contract
    from agent_py_agent.agent.tooling.models import ToolHandlerOutcome

    result = ToolHandlerOutcome('session_search', False, '范围内不能核验记录',
        error_code='HISTORY_SCOPE_NOT_VISIBLE', effect_outcome='not_started')
    contract = error_contract(result.error_code)
    assert result.error_code == 'HISTORY_SCOPE_NOT_VISIBLE'
    assert result.effect_outcome == 'not_started' and result.retryable is False
    assert contract.category == 'permission' and contract.retryable is False
    assert contract.recommended_action == 'change_strategy'
    assert '当前范围检索返回的引用' in contract.recovery_hint
    assert '不能换ID、声明ticket、扩大scope或冒充owner绕过' in contract.recovery_hint
    assert '不能据此判断记录是否存在' in contract.recovery_hint


@pytest.mark.parametrize('code,category,action', [
    ('CASE_OWNER_MISMATCH', 'permission', 'change_strategy'),
    ('CASE_SCOPE_MISMATCH', 'contract', 'repair_tool_arguments'),
])
def test_handoff_identity_denials_keep_distinct_recovery_contracts(code, category, action):
    from agent_py_agent.agent.contracts.error_taxonomy import error_contract
    from agent_py_agent.agent.tooling.models import ToolHandlerOutcome

    result = ToolHandlerOutcome('manage_handoff_board', False, '未创建协作单',
        error_code=code, effect_outcome='not_started')
    contract = error_contract(result.error_code)
    assert result.error_code == code and result.retryable is False
    assert result.effect_outcome == 'not_started'
    assert contract.category == category and contract.recommended_action == action
    if code == 'CASE_OWNER_MISMATCH':
        assert '不能改owner参数、借ticket或替换来源绕过' in contract.recovery_hint
    else:
        assert '省略这两个可选字段并沿原运行绑定' in contract.recovery_hint


@pytest.mark.parametrize('code,category,retryable,action', [
    ('CURSOR_EXPIRED', 'state', False, 'change_strategy'),
    ('ARTIFACT_CHANGED', 'state', False, 'change_strategy'),
    ('BINARY_CONTENT', 'contract', False, 'change_strategy'),
    ('VERSION_SCOPE_MISMATCH', 'permission', False, 'repair_tool_arguments'),
    ('ASSET_SCOPE_MISMATCH', 'permission', False, 'change_strategy'),
    ('HISTORY_STORAGE_BUSY', 'storage', True, 'retry'),
    ('HISTORY_SOURCE_CORRUPT', 'storage', False, 'change_strategy'),
    ('HISTORY_QUERY_UNAVAILABLE', 'resource', False, 'change_strategy'),
])
def test_public_reader_failures_keep_specific_recovery_and_not_started(code, category, retryable, action):
    from agent_py_agent.agent.tooling.models import ToolHandlerOutcome

    result = ToolHandlerOutcome('read-only-host-tool', False, '有界纯读失败',
        error_code=code, effect_outcome='not_started', failure_stage='execution', phase='history_query')
    assert result.error_code == result.reported_error_code == code
    assert result.error_category == category and result.retryable is retryable
    assert result.recommended_action == action and result.effect_outcome == 'not_started'
    rendered = result.render_for_prompt()
    assert code in rendered and 'effect_outcome=not_started' in rendered
    assert result.recovery_hint in rendered
    if code == 'VERSION_SCOPE_MISMATCH':
        assert '不能修改冻结版本、扩大pins或用新版本替代旧版本' in rendered
    if code == 'HISTORY_STORAGE_BUSY':
        assert '有限重试同一只读查询' in rendered and '不要重放写操作' in rendered
