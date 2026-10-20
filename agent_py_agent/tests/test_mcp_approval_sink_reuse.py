"""真实canonical执行范围下，同一会话许可不因展示sink重建丢失。"""

import pytest

from agent_py_agent.tests import test_background_tool_approval as background_cases


@pytest.fixture
def main_background(tmp_path):
    yield from background_cases.background.__wrapped__(tmp_path)


def test_mcp_session_approval_survives_sink_rebuild_same_current_child_attempt(tmp_path, monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace

    from agent_py_agent.agent.contracts.tool_approval import (
        ToolApprovalDecision,
        build_tool_approval_request,
    )
    from agent_py_agent.agent.conversation import agent_tool_approval as bridge
    from agent_py_agent.agent.conversation.background_transcript import BackgroundTranscriptSink
    from agent_py_agent.agent.tooling.mcp_client import MCPToolInfo
    from agent_py_agent.agent.tooling.mcp_registration import build_proxy_tool
    from agent_py_agent.agent.tooling.models import ApprovalPolicy
    from agent_py_agent.agent.tooling.runtime_contracts import ToolCall
    from agent_py_agent.tests.test_gateway_agent_control_service import _bound_agent_tree

    agent, _, child = _bound_agent_tree(tmp_path)
    tool = build_proxy_tool(SimpleNamespace(is_running=lambda: True), "offline", MCPToolInfo("act", "offline", {"type": "object", "properties": {}}))
    tool.runtime_policy = replace(tool.runtime_policy, approval_policy=ApprovalPolicy("always"))
    agent.tools.register(tool)
    published = []
    monkeypatch.setattr(bridge, "publish_agent_tool_approval", lambda *args, **kwargs: published.append(kwargs["request_value"]) or object())
    monkeypatch.setattr(bridge, "wait_for_agent_tool_approval", lambda handle, **kwargs:
        ToolApprovalDecision(published[-1].permission_id, "approved_session"))
    def request(index):
        call = ToolCall(call_id=f"call-{index}", tool_name=tool.model_spec.name, arguments={}, source_protocol="native",
            schema_hash=tool.model_spec.schema_hash, run_id=child.id, turn_id="same-turn", attempt_id=child.runner_active_attempt_id)
        return build_tool_approval_request(call, request_id=f"request-{index}", round_number=1, call_index=1, description="offline")
    def sink():
        result = BackgroundTranscriptSink(agent, thread_id=child.agent_thread_id, task_id=child.id,
            request_id="same-current-attempt", event_writer=lambda *args, **kwargs: None)
        result.approval_interactive = True
        return result
    first, second = request(1), request(2)
    assert first.binding["args_hash"] == second.binding["args_hash"]
    assert sink().request_permission(first.to_dict())["decision"] == "approved_session"
    result = sink().request_permission(second.to_dict())
    assert result["decision"] == "approved" and len(published) == 1, (result, len(published))


# LLM: owner、child、currentattempt、MCP工具授予都走正式临时库；只替换用户审批的传输等待，不伪造执行scope或批准缓存。
# 函数用途: 创建同一孩子的实际权限和可重建sink，记录每次真正发布的审批。
def reuse_case(tmp_path, monkeypatch, *, decision="approved_session", forced=False):
    from dataclasses import replace
    from types import SimpleNamespace

    from agent_py_agent.agent.contracts.tool_approval import (
        ToolApprovalDecision,
        build_tool_approval_request,
    )
    from agent_py_agent.agent.conversation import agent_tool_approval as bridge
    from agent_py_agent.agent.conversation.background_transcript import BackgroundTranscriptSink
    from agent_py_agent.agent.core import ResolveCapabilityRequestsTool
    from agent_py_agent.agent.subagents.services.lifecycle import RecordCapabilityRequestParams
    from agent_py_agent.agent.tooling.mcp_client import MCPToolInfo
    from agent_py_agent.agent.tooling.mcp_registration import build_proxy_tool
    from agent_py_agent.agent.tooling.models import ApprovalPolicy
    from agent_py_agent.agent.tooling.runtime_contracts import ToolCall
    from agent_py_agent.tests.test_gateway_agent_control_service import _bound_agent_tree

    agent, _, child = _bound_agent_tree(tmp_path)
    tool = build_proxy_tool(SimpleNamespace(is_running=lambda: True), "offline", MCPToolInfo("act", "offline", {"type": "object", "properties": {}}))
    tool.runtime_policy = replace(tool.runtime_policy, approval_policy=ApprovalPolicy("always"))
    agent.tools.register(tool)
    request = agent.subagents.lifecycle.record_capability_request(child.id, RecordCapabilityRequestParams(
        problem="合成 MCP 授权", needed_capability="offline action", capability_type="mcp", requested_mcp_tools=[tool.model_spec.name]))
    assert ResolveCapabilityRequestsTool(agent).execute({"run_id": child.id, "request_id": request.id, "decision": "grant", "reason": "合成同任务授权"}).ok
    published = []
    monkeypatch.setattr(bridge, "publish_agent_tool_approval", lambda *args, **kwargs: published.append(kwargs["request_value"]) or object())
    monkeypatch.setattr(bridge, "wait_for_agent_tool_approval", lambda handle, **kwargs:
        ToolApprovalDecision(published[-1].permission_id, decision))
    def request_for(index):
        task = agent.subagents.load(child.id)
        call = ToolCall(call_id=f"call-{index}", tool_name=tool.model_spec.name, arguments={}, source_protocol="native",
            schema_hash=tool.model_spec.schema_hash, run_id=child.id, turn_id="same-turn", attempt_id=task.runner_active_attempt_id)
        result = build_tool_approval_request(call, request_id=f"request-{index}", round_number=1, call_index=1, description="offline")
        return replace(result, binding={**result.binding, "plugin_gate_ref": "forced-plugin-reference"}) if forced else result
    def sink():
        result = BackgroundTranscriptSink(agent, thread_id=child.agent_thread_id, task_id=child.id,
            request_id="same-current-attempt", event_writer=lambda *args, **kwargs: None)
        result.approval_interactive = True
        return result
    return SimpleNamespace(agent=agent, child=child, published=published, request=request_for, sink=sink)


def test_real_capability_grant_rebuilt_sink_reuses_without_time_limit(tmp_path, monkeypatch):
    from agent_py_agent.agent.user_space.approval_mode import execute_approval_mode_operation
    case = reuse_case(tmp_path, monkeypatch)
    assert case.sink().request_permission(case.request(1).to_dict())["decision"] == "approved_session"
    execute_approval_mode_operation(case.agent.home_paths, "set", "ask")
    assert case.sink().request_permission(case.request(2).to_dict())["decision"] == "approved"
    assert len(case.published) == 1


def test_canonical_attempt_change_requires_new_user_decision(tmp_path, monkeypatch):
    case = reuse_case(tmp_path, monkeypatch)
    case.sink().request_permission(case.request(1).to_dict())
    repo = case.agent.subagents.runtime_db
    row = repo.agent_run_for_run_id(case.child.id)
    assert repo.settle_agent_attempt(agent_run_id=row["agent_run_id"], attempt_id=case.child.runner_active_attempt_id)["settled"]
    case.agent.subagents.lifecycle.prepare_runner_attempt(case.child.id)
    assert case.sink().request_permission(case.request(2).to_dict())["decision"] == "approved_session"
    assert len(case.published) == 2


def test_canonical_scope_change_requires_new_decision(tmp_path, monkeypatch):
    case = reuse_case(tmp_path, monkeypatch)
    case.sink().request_permission(case.request(1).to_dict())
    def change(task):
        task.locked_files.append(str(case.agent.home_paths.owner_home_dir / "locked-business.txt"))
    case.agent.subagents.mutate(case.child.id, change)
    assert case.sink().request_permission(case.request(2).to_dict())["decision"] == "approved_session"
    assert len(case.published) == 2


def test_owner_mode_change_requires_new_decision(tmp_path, monkeypatch):
    from agent_py_agent.agent.user_space.approval_mode import execute_approval_mode_operation
    case = reuse_case(tmp_path, monkeypatch)
    case.sink().request_permission(case.request(1).to_dict())
    execute_approval_mode_operation(case.agent.home_paths, "set", "auto")
    assert case.sink().request_permission(case.request(2).to_dict())["decision"] == "approved_session"
    assert len(case.published) == 2


def test_cancelled_request_cannot_use_cached_session(tmp_path, monkeypatch):
    from agent_py_agent.agent.common.cancellation import CancellationToken
    case = reuse_case(tmp_path, monkeypatch)
    case.sink().request_permission(case.request(1).to_dict())
    token = CancellationToken()
    token.cancel("user_stop")
    assert case.sink().request_permission(case.request(2).to_dict(), cancellation_token=token)["decision"] == "cancelled"
    assert len(case.published) == 1


def test_expired_request_cannot_use_cached_session(tmp_path, monkeypatch):
    import time
    from dataclasses import replace
    case = reuse_case(tmp_path, monkeypatch)
    case.sink().request_permission(case.request(1).to_dict())
    request = replace(case.request(2), requested_at=time.time()-100, expires_at=time.time()-1)
    result = case.sink().request_permission(request.to_dict())
    assert result["decision"] == "denied" and result["reason_code"] == "APPROVAL_TIMEOUT"
    assert len(case.published) == 1


def test_failed_scope_projection_does_not_use_a_broader_cache(tmp_path, monkeypatch):
    case = reuse_case(tmp_path, monkeypatch)
    case.sink().request_permission(case.request(1).to_dict())
    def fail(*args):
        raise ValueError("synthetic scope unavailable")
    monkeypatch.setattr(case.agent.subagents.runner_context, "build_execution_context", fail)
    assert case.sink().request_permission(case.request(2).to_dict())["decision"] == "approved_session"
    assert len(case.published) == 2


def test_once_approval_is_never_promoted_to_session(tmp_path, monkeypatch):
    case = reuse_case(tmp_path, monkeypatch, decision="approved")
    assert case.sink().request_permission(case.request(1).to_dict())["decision"] == "approved"
    assert case.sink().request_permission(case.request(2).to_dict())["decision"] == "approved"
    assert len(case.published) == 2


def test_denial_is_never_promoted_to_session(tmp_path, monkeypatch):
    case = reuse_case(tmp_path, monkeypatch, decision="denied")
    assert case.sink().request_permission(case.request(1).to_dict())["decision"] == "denied"
    assert case.sink().request_permission(case.request(2).to_dict())["decision"] == "denied"
    assert len(case.published) == 2


def test_plugin_forced_gate_never_borrows_cached_session(tmp_path, monkeypatch):
    case = reuse_case(tmp_path, monkeypatch, forced=True)
    assert case.sink().request_permission(case.request(1).to_dict())["decision"] == "approved_session"
    assert case.sink().request_permission(case.request(2).to_dict())["decision"] == "approved_session"
    assert len(case.published) == 2


# LLM: 复用正式后台 fixture 的真实 owner、TaskLink、active claim 与原 core/publisher，不替换权限或缓存判定。
# 函数用途: 核验主代理会话键只在当前宿主执行来源完整时生成。
def test_main_original_background_publisher_has_narrow_session_scope(main_background):
    from agent_py_agent.agent.conversation.agent_tool_approval import _agent_approval_session_scope
    from agent_py_agent.agent.conversation.tool_approval_scope import tool_approval_scope
    from agent_py_agent.tests.test_background_tool_approval import _bind_background_authority

    ctx = main_background
    _bind_background_authority(ctx)
    scope = tool_approval_scope(ctx.agent, ctx.task_id)
    assert scope.active
    assert _agent_approval_session_scope(ctx.agent, scope)



@pytest.mark.parametrize("fault", ["none", "duck", "other_task", "other_run", "old_attempt", "foreign_params",
    "no_control", "duck_control", "unpublished_control", "closed_control", "claim_binding", "missing_binding"])
def test_main_missing_or_mismatched_source_disables_session_cache(main_background, tmp_path, fault):
    from dataclasses import replace
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.agent_core.runtime_mixin import _bind_main_agent_authority
    from agent_py_agent.agent.conversation.agent_tool_approval import _agent_approval_session_scope
    from agent_py_agent.agent.conversation.local_run_control import LocalRunControl
    from agent_py_agent.agent.conversation.tool_approval_scope import tool_approval_scope
    from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic
    from agent_py_agent.tests.test_background_tool_approval import _bind_background_authority
    from agent_py_agent.tests.test_tool_approval_foreground_scope import _foreground

    ctx = main_background
    original = _bind_background_authority(ctx)
    scope = tool_approval_scope(ctx.agent, ctx.task_id)
    assert scope.active and _agent_approval_session_scope(ctx.agent, scope)
    if fault == "none":
        ctx.agent._current_run_params = None
    elif fault == "duck":
        ctx.agent._current_run_params = SimpleNamespace(**vars(original))
    elif fault == "other_task":
        ctx.agent._current_run_params = replace(original, task_id="another-task")
    elif fault == "other_run":
        other = _bind_main_agent_authority(ctx.agent, RunParams(request_id="another-request", run_id="another-core",
            task_id="another-task", task_attributes={"conversation_thread_id": ctx.thread.thread_id}))
        ctx.agent._current_run_params = replace(other, task_id=ctx.task_id)
    elif fault == "old_attempt":
        row = ctx.agent.subagents.runtime_db.agent_run_for_run_id(original.run_id)
        ctx.agent.subagents.runtime_db.create_attempt(row["agent_run_id"])
    elif fault == "foreign_params":
        ctx.agent._current_run_params = _foreground(tmp_path / "foreign", kind="user").params
    elif fault == "no_control":
        ctx.agent._current_run_params = replace(original, conversation_task_binding_callback=None)
    elif fault == "duck_control":
        real = original.conversation_task_binding_callback
        ctx.agent._current_run_params = replace(original, conversation_task_binding_callback=SimpleNamespace(
            runtime_authority=real.runtime_authority, request_id=real.request_id))
    elif fault == "unpublished_control":
        ctx.agent._current_run_params = replace(original, conversation_task_binding_callback=LocalRunControl(original.request_id))
    elif fault == "closed_control":
        original.conversation_task_binding_callback.finish()
    else:
        def alter(claim):
            if fault == "missing_binding":
                return {key: value for key, value in claim.items() if key != "runtime_authority"}
            return {**claim, "runtime_authority": {**claim["runtime_authority"], "attempt_id": "other-attempt"}}
        update_json_file_atomic(ctx.store.storage.background_claim_path(ctx.thread.thread_id), alter, require_existing=True)
    # 原冻结scope仍active；hot显式冲突现在连fresh资格也拒，cold缺Params只读原已发布claim。
    if fault != "foreign_params":
        assert scope.active
        if fault == "none":
            assert tool_approval_scope(ctx.agent, ctx.task_id).active
        else:
            assert not tool_approval_scope(ctx.agent, ctx.task_id).active
    assert _agent_approval_session_scope(ctx.agent, scope) == ""


def test_main_foreground_original_writer_scope_is_reusable(tmp_path):
    from agent_py_agent.agent.conversation.agent_tool_approval import _agent_approval_session_scope
    from agent_py_agent.agent.conversation.tool_approval_scope import tool_approval_scope
    from agent_py_agent.tests.test_tool_approval_foreground_scope import _foreground

    world = _foreground(tmp_path)
    scope = tool_approval_scope(world.agent, "durable-task")
    assert _agent_approval_session_scope(world.agent, scope)
