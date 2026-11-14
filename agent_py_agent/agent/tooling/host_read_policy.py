"""宿主只读准备复用原工具ActionPolicy装配；不执行handler、不批准后续副作用。"""
from ..conversation.business_context import business_execution_policy_scope
from ..path_access_policy import effective_owner_scope_root
from .action_policy import ActionPolicy, ActionPolicyRequest
from .registry import _execution_workspace_roots
from .registry_workspace import effective_registry_cwd
from .runtime_contracts import ToolCall


def host_tool_entry_policy(agent, params, snapshot, tool_name, arguments, *, claim_id, boundary, idempotency_key=None):
    """按当前宿主原参数评估准确入口；ask/deny不是只读准备许可。"""
    registry = agent.tools
    runtime = snapshot.runtime(tool_name)
    if runtime is None:
        raise ValueError('HOST_TOOL_READ_UNAVAILABLE')
    call = ToolCall(call_id=f'host-read-policy:{claim_id}', tool_name=tool_name, arguments=arguments,
        source_protocol='native', schema_hash=runtime.model_spec.schema_hash,
        run_id=params.run_id, turn_id=params.conversation_turn_id or params.request_id,
        attempt_id=params.attempt_id, idempotency_key=idempotency_key)
    root = effective_registry_cwd(registry.workspace_root, boundary)
    mode = registry.approval_mode_reader() if registry.approval_mode_reader else 'ask'
    return ActionPolicy().decide(ActionPolicyRequest(call=call, runtime_snapshot=snapshot, workspace_root=root,
        workspace_roots=_execution_workspace_roots(root, registry.workspace_roots, boundary),
        path_access_mode=registry.path_access_mode, path_dangerous_roots=tuple(registry.path_dangerous_roots),
        owner_scope_root=effective_owner_scope_root(write_boundary=boundary, registry_scope=registry.owner_scope_root),
        write_boundary=boundary, runtime_guard_policy=registry.runtime_guard_policy, approval_mode=mode,
        **business_execution_policy_scope(agent)))
