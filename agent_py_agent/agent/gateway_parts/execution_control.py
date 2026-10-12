# LLM: 本模块只适配原控制服务和执行投影；认证 scope、会话 claim、请求/task 原账保持唯一权威。
# 模块用途: 将 TUI 和 IM 的插话、停止定位到实际执行者，并核对客户端携回的原绑定。
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace

from .execution_target import (
    GatewayExecutionTarget,
    execution_target_guard,
    execution_target_matches,
    read_gateway_execution_target,
)
from .io import read_json_file_report


# LLM: 不创建线程或 owner；无执行权返回空，调用者区分空闲任务控制与活动回合控制。
# 函数用途: 解析当前认证会话的真实执行快照和对应控制记录。
def resolve_execution_control(base_agent, paths, scope):
    from .control_service import (
        _active_conversation_task,
        _GatewayRequestRecord,
        _linked_active_task_record,
        _linked_conversation_task_id,
        _request_agent_for_scope,
        _request_matches_scope,
    )

    owner_agent = _request_agent_for_scope(base_agent, scope)
    store = owner_agent.conversation_store
    thread = store.threads.resolve(
        channel=scope.channel, channel_conversation_id=scope.conversation_id, channel_user_id=scope.user_id,
    )
    target = read_gateway_execution_target(paths, store, thread.thread_id) if thread else None
    if target is None:
        return None, None
    if target.kind == "task":
        active = _active_conversation_task(base_agent, scope, task_id=target.task_id, ordinary_only=True)
        if active is not None:
            active = replace(active, payload={**active.payload, "execution_turn_id": target.turn_id})
        return target, active
    path = paths.processing / f"{target.turn_id}.json"
    report = read_json_file_report(path, context="gateway.execution_control.request")
    if report.load_error or not _request_matches_scope(report.payload, scope):
        return None, None
    processing = _GatewayRequestRecord(path, report.payload)
    task_id = _linked_conversation_task_id(processing)
    active = _linked_active_task_record(base_agent, processing, scope, task_id) if task_id else None
    return target, active or processing


# LLM: expected_execution 出现但损坏必须拒绝；旧客户端只带 expected_turn_id 时仍绑定本次服务器选出的真实 claim。
# 函数用途: 固定一次活动输入或控制的目标，迟到操作不能自动改投其他轮。
def bind_execution_scope(scope, target):
    expected_id = str(scope.metadata.get("expected_turn_id") or "")
    if expected_id and expected_id != target.turn_id:
        return None
    if "expected_execution" in scope.metadata:
        expected = GatewayExecutionTarget.from_dict(scope.metadata["expected_execution"])
        if expected is None or not execution_target_matches(expected, target):
            return None
    return replace(scope, metadata={
        **scope.metadata, "expected_turn_id": target.turn_id,
        "expected_execution": target.to_dict(),
    })


# LLM: 调用方已持 T，以及需要的 guidance/task 锁；C 放在最后且锁内不调用 claim 写入口。
#   没有客户端快照只供明确的空闲任务控制；普通活动输入必须先经过 bind_execution_scope。
# 函数用途: 提交前固定同一执行代次，返回空快照兼容入口或精确比较结果。
@contextmanager
def execution_scope_guard(paths, store, scope):
    if "expected_execution" not in scope.metadata:
        yield True
        return
    expected = GatewayExecutionTarget.from_dict(scope.metadata["expected_execution"])
    if expected is None:
        yield False
        return
    with execution_target_guard(paths, store, expected) as matches:
        yield matches


# LLM: 已关闭的前台请求沿 _fresh_stop_runtime_binding 核对 RuntimeDB 代次；后台在 task 锁内最后持 C 核对。
# 函数用途: 冻结后台任务资源前复核原车道，防止迟到停止打中同任务的新工作片。
@contextmanager
def background_stop_guard(store, scope):
    raw = scope.metadata.get("expected_execution")
    expected = GatewayExecutionTarget.from_dict(raw)
    if raw is None or (expected is not None and expected.kind == "request"):
        yield True
        return
    with execution_scope_guard(None, store, scope) as matches:
        yield matches


# LLM: 优先实际执行者；旧客户端显式指定等待请求仍由原 request 取消入口处理，不传播到后台任务。
# 函数用途: 选择真实前台/后台进行停止，无执行者时返回空交给原空闲任务与遗留资源控制。
def execute_running_stop(agent, paths, scope, command):
    from .control_service import (
        _execution_changed_result,
        _stop_active_task,
        _stop_live_window_request,
    )

    target, active = resolve_execution_control(agent, paths, scope)
    if target is None or active is None:
        return _execution_changed_result("stop", str(scope.metadata.get("expected_turn_id") or "")) if "expected_execution" in scope.metadata else None
    bound = bind_execution_scope(scope, target)
    if bound is None:
        if "expected_execution" in scope.metadata:
            return _execution_changed_result("stop", str(scope.metadata.get("expected_turn_id") or ""))
        return None
    if target.kind == "request":
        return _stop_live_window_request(
            agent, paths, active.linked_request or active, bound, interrupt_only=command.operation == "interrupt",
        )
    return _stop_active_task(agent, active, bound, interrupt_only=command.operation == "interrupt")


__all__ = [
    "resolve_execution_control", "bind_execution_scope", "execution_scope_guard",
    "background_stop_guard", "execute_running_stop",
]
