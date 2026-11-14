"""仅原任务在真实审核门前用本轮权限派工。审核、恢复与发布各沿原入口。"""
from __future__ import annotations

import sqlite3

from ..capability.runtime_config_reload import capability_config_for_agent
from ..common.log_redaction import redact_sensitive_text
from ..conversation.business_contracts import BusinessExecutionContext
from ..runtime_db.managed_operation_store import ToolOperationAuthorityRequest
from ..tooling.runtime_contracts import ToolCall
from ..verification.repository import VerificationEvidenceRepository
from ..workflows.governance import pending_review_not_started
from ..workflows.native_governance import review_binding
from ..workflows.run_store import WorkflowRunStore
from .tool_runtime_ledger import write_boundary_with_runtime_ledger


def _receipt(state: str, code: str, next_action: str, *, operation_id: str | None = None,
             child_run_ids: list[str] | None = None, start_status: str | None = None,
             missing_tools: list[str] | None = None, effect_outcome: str | None = None,
             reason: str | None = None, error_type: str | None = None) -> dict[str, object]:
    facts = {key: value for key, value in (
        ('operation_id', operation_id), ('child_run_ids', child_run_ids), ('start_status', start_status),
        ('missing_tools', missing_tools), ('effect_outcome', effect_outcome),
        ('reason', reason), ('error_type', error_type),
    ) if value is not None}
    return {'state': state, 'code': code, 'next_action': next_action, **facts}


def _recorded_dispatch(result, key):
    ids = result.get('recorded_run_ids', [])
    started = result.get('start_status', '')
    if ids and isinstance(ids, list) and all(isinstance(item, str) for item in ids):
        return _receipt('dispatched' if started == 'accepted' else 'recorded',
            'REVIEW_DISPATCH_RECORDED', 'wait_for_original_reviewers_then_resume_same_workflow',
            operation_id=key, child_run_ids=ids[:32], start_status=started)
    return _receipt('unconfirmed', 'REVIEW_DISPATCH_RESULT_UNCONFIRMED',
        'inspect_original_create_operation_before_retry', operation_id=key)


def dispatch_pending_review(agent, workflow_run_id, invocation):
    params = getattr(agent, '_current_run_params', None)
    frozen = getattr(params, 'business_context', None)
    if frozen is not None and not isinstance(frozen, BusinessExecutionContext):
        return None
    home = agent.home_paths.owner_home_dir
    store = WorkflowRunStore(home)
    try:
        run = store.load_run(workflow_run_id)
        step = next((item for item in run.steps if item.step_id == run.failed_step_id), None)
        if (run.status != 'failed' or step is None or step.error_code != 'WORKFLOW_REVIEW_REQUIRED'
                or not pending_review_not_started(run, step)):
            return None
        expected_context = frozen.to_dict() if frozen is not None else {}
        if (run.owner_id != agent.home_paths.owner_id or run.business_context != expected_context
                or frozen is None and run.scope_snapshot):
            return _receipt('not_started', 'REVIEW_TASK_SCOPE_MISMATCH', 'ask_original_task_to_dispatch')
        snapshot = store.load_workflow_snapshot(workflow_run_id)
        binding = step.escalation_result['pending_review']
        if binding != review_binding(run, step, snapshot):
            return _receipt('not_started', 'REVIEW_PENDING_BINDING_STALE', 'inspect_current_candidate')
        policy = snapshot['descriptor']['metadata']['workflow_review_policy']
        if (policy.get('auto_dispatch', True) is not True or
                getattr(capability_config_for_agent(agent), 'workflow_auto_dispatch_reviewers', False) is not True):
            return _receipt('manual', 'REVIEW_DISPATCH_MANUAL', 'arrange_independent_review_in_original_task')
        requirement = VerificationEvidenceRepository(home).get_requirement(run.owner_id, binding['requirement_id'])
        if (requirement is None or requirement.task_id != workflow_run_id or requirement.root != str(home)
                or requirement.input_version != binding['input_version']):
            return _receipt('not_started', 'REVIEW_PENDING_BINDING_STALE', 'inspect_current_candidate')
        runtime = agent.subagents.runtime_db
        tools = invocation.runtime_snapshot
        if (tools.run_id != params.run_id or runtime is None or
                runtime.business_context_for_execution(owner_id=run.owner_id, run_id=params.run_id,
                    attempt_id=params.attempt_id) != frozen):
            return _receipt('not_started', 'REVIEW_TASK_SCOPE_MISMATCH', 'ask_original_task_to_dispatch')
        # 原作者与候选都进入稳定的 operation 身份。续跑可以只读原回执，绝不在新根重建同一批。
        key = 'workflow-review-dispatch-' + binding['input_version']
        operations = agent.tools.operation_store
        operations.require_authority(ToolOperationAuthorityRequest(owner_id=run.owner_id, run_id=params.run_id,
            attempt_id=params.attempt_id, task_id=params.task_id, operation_id=key, tool_name='workflows'))
        prior = operations.get_tool_operation(owner_id=run.owner_id, run_id=requirement.author_principal_id,
            operation_id=key, tool_name='create_subagents')
        if prior is not None:
            old = prior.result or {}
            lifecycle = (old.get('result_envelope') or {}).get('schedule_lifecycle', {})
            if old.get('ok') is True:
                return _recorded_dispatch(lifecycle, key)
            return _receipt('unconfirmed' if not old else 'blocked', old.get('reported_error_code') or prior.status,
                'inspect_original_create_operation_before_retry', operation_id=key)
        if params.run_id != requirement.author_principal_id:
            return _receipt('not_started', 'REVIEW_TASK_SCOPE_MISMATCH', 'ask_original_task_to_dispatch')
        required = {'create_subagents', 'manage_requirement_provenance'}
        missing = sorted(required - tools.available_tool_names)
        if missing:
            return _receipt('not_started', 'REVIEW_DISPATCH_TOOLS_UNAVAILABLE', 'request_missing_parent_capabilities',
                            missing_tools=missing)
        count = step.escalation_result.get('reviewers_needed', policy.get('minimum_reviews', 1))
        if type(count) is not int or not 1 <= count <= policy.get('minimum_reviews', 1) <= 32:
            return _receipt('not_started', 'REVIEW_PENDING_BINDING_STALE', 'inspect_current_candidate')
        goal = (f"请独立审核候选 {requirement.requirement_id}。先通过 manage_requirement_provenance 的 "
                "query_provenance 提交原 requirement_id，省略 root。若返回材料分页，照 read_example 继续查询，"
                "后续用 next_cursor 作为同一查询的 material_cursor。核对冻结步骤、版本、上游输入输出和目标。"
                "review_gate 是当前正在征求你意见的独立审核，工具执行时宿主还会另行检查执行权限。"
                "如需核实可变资源版本，依据 binding.tool_precondition 中的摘要。再用 record_independent_review "
                "登记真实结论（review_scope=workflow_effect.v1，reviewed_input_version 取原绑定）。"
                "证据不足或需要修改时说明原因；不要执行候选。向原父任务返回审核编号和结论。")
        # 审核材料来自原候选的冻结读口。即使管理员定制 reviewer 模板，自动派工也只授本次核验所需工具。
        review_tools = sorted(tools.available_tool_names & {'tool_search', 'manage_requirement_provenance'})
        items = [{'role': 'reviewer', 'review_requirement_id': requirement.requirement_id,
                  'delegation_constraints': {'permitted_tools': review_tools, 'allow_descendants': False},
                  'agent_name': f'reviewer-{index + 1}-{binding["input_version"][:10]}', 'goal': goal,
                  **({'model': policy['reviewer_model']} if 'reviewer_model' in policy else {})}
                 for index in range(count)]
        call = ToolCall(key, 'create_subagents', {'items': items}, 'text',
            tools.runtime('create_subagents').model_spec.schema_hash, params.run_id,
            params.request_id or params.run_id, params.attempt_id, operation_id=key, idempotency_key=key)
        result = agent.tools.execute_tool(call, write_boundary=write_boundary_with_runtime_ledger(agent, params),
            runtime_snapshot=tools, cancellation_token=invocation.cancellation_token,
            trusted_run_context={'run_scope': {'run_id': params.run_id, 'task_id': params.task_id,
                'attempt_id': params.attempt_id}, 'task_attributes': dict(params.task_attributes or {})}).result
        if not result.ok:
            return _receipt('blocked', result.reported_error_code, 'inspect_original_create_operation_before_retry',
                operation_id=key, effect_outcome=result.effect_outcome, reason=result.recovery_hint)
        return _recorded_dispatch(result.metadata['handler_details'].get('schedule_lifecycle', {}), key)
    except (OSError, sqlite3.Error, ValueError, KeyError, RuntimeError) as exc:
        from ..common.cancellation import ToolCancelled
        if isinstance(exc, (InterruptedError, ToolCancelled)):
            raise
        return _receipt('unconfirmed', 'REVIEW_DISPATCH_EVIDENCE_UNAVAILABLE',
            'inspect_original_create_operation_before_retry', error_type=type(exc).__name__,
            reason=redact_sensitive_text(str(exc))[:250])
