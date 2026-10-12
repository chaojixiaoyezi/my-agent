# LLM: 会话间派活的取消要"真的叫停目标上正在执行的回合"，这里复用 /stop 的那条结构化控制通道
#   （gateway 控制服务按精确 expected_turn_id 选回合），所以只停这个任务对应的那一轮，不会停目标会话里别的工作。
#   停止是否生效只认控制结果：未确认时调用方不得声称"已停止"。目标渠道身份缺失、Gateway 路径不可用时一律
#   返回未确认（fail closed），不猜回合、不猜渠道。
# 模块用途: 让会话任务取消工具从目标会话外部请求它停止正在执行的那一轮。
from __future__ import annotations

from dataclasses import dataclass

from ..conversation.control_commands import ConversationControlCommand
from .control_service import GatewayControlScope, execute_gateway_conversation_control
from .paths import gateway_paths


# LLM: confirmed 只表示控制服务已受理并标记了这个精确回合；message 是控制服务的原话，调用方据此如实回报。
# 类用途: 保存一次"停止某条会话任务所在回合"的实际结果。
@dataclass(frozen=True)
class SessionTaskStopOutcome:
    confirmed: bool
    message: str
    request_id: str = ""
    error_code: str = ""


# LLM: 只按结构化身份构造作用域：渠道、会话、渠道用户来自目标会话的 channel_binding，回合用 expected_turn_id
#   精确绑定。任何一步拿不到结构化事实都返回未确认，绝不退化成"停掉目标会话当前任意回合"。
# 函数用途: 请求停止一条会话任务绑定的那个目标回合。
def stop_session_task_turn(agent: object, *, target_thread: object, turn_id: str) -> SessionTaskStopOutcome:
    request_id = str(turn_id or "").strip()
    if not request_id:
        return SessionTaskStopOutcome(
            False, "任务还没有绑定到执行中的回合，未发送停止控制。"
        )
    from ..conversation.execution_threads import IM_TASK_EXECUTION_KEY

    if target_thread is not None and IM_TASK_EXECUTION_KEY in target_thread.metadata:
        return _stop_delegated_task(agent, target_thread, request_id)
    binding = _primary_binding(target_thread)
    if binding is None:
        return SessionTaskStopOutcome(
            False, "目标会话没有可用的渠道身份，未发送停止控制。", request_id=request_id
        )
    try:
        paths = gateway_paths(agent)
    except Exception as exc:  # noqa: BLE001 - 拿不到 Gateway 路径就不能确认停止
        return SessionTaskStopOutcome(
            False,
            "当前运行环境没有 Gateway 队列，停止控制未发送。",
            request_id=request_id,
            error_code=_unconfirmed_code(exc),
        )
    scope = GatewayControlScope(
        user_id=str(binding.channel_user_id or binding.canonical_user_id or "").strip(),
        channel=str(binding.channel or "").strip(),
        conversation_id=str(binding.channel_conversation_id or "").strip(),
        metadata={"expected_turn_id": request_id},
    )
    if not (scope.user_id and scope.channel and scope.conversation_id):
        return SessionTaskStopOutcome(
            False, "目标会话的渠道身份不完整，未发送停止控制。", request_id=request_id
        )
    try:
        result = execute_gateway_conversation_control(
            agent, paths, ConversationControlCommand("stop"), scope
        )
    except Exception as exc:  # noqa: BLE001 - 异常不等于目标已停止
        return SessionTaskStopOutcome(
            False,
            "停止控制执行失败；目标是否已停止未确认。",
            request_id=request_id,
            error_code=_unconfirmed_code(exc),
        )
    return SessionTaskStopOutcome(
        bool(getattr(result, "ok", False)),
        str(getattr(result, "message", "") or ""),
        request_id=str(getattr(result, "request_id", "") or request_id),
        error_code=str(getattr(result, "error_code", "") or ""),
    )


# LLM: 目标 thread 的第一个渠道绑定就是控制作用的会话身份；没有绑定就没有可确认的目标身份。
# 函数用途: 取目标会话的渠道绑定。
def _primary_binding(thread: object) -> object | None:
    bindings = getattr(thread, "channel_bindings", ()) or ()
    for binding in bindings:
        return binding
    return None


# LLM: 委派目标不复制渠道绑定；按原 SessionTask/source/owner 和准确根/claim 调原主任务停止事务，不能去停原聊天。
# 函数用途: 取消隔离后台任务的实际父子资源，复用现有停止账和锁序，部分结果如实返回未确认。
def _stop_delegated_task(agent, target_thread, turn_id):
    from ..concurrency.interrupt import interrupt_by_name
    from ..conversation.control_commands import session_task_interrupt_name
    from ..conversation.execution_threads import (
        IM_TASK_EXECUTION_KEY,
        delegated_session_task,
        execution_origin_thread,
    )
    from .control_service import (
        _cancel_request_execution,
        _GatewayRequestRecord,
        _prepare_main_task_stop,
    )
    from .execution_target import read_gateway_execution_target

    store = agent.conversation_store
    try:
        task = delegated_session_task(store, target_thread.thread_id)
        if (task is None or task.task_id != turn_id or target_thread.owner_id != str(agent.home_paths.owner_id)):
            return SessionTaskStopOutcome(False, "后台停止身份不一致。", turn_id)
        current = store.threads.require(target_thread.thread_id)
        root_id = current.metadata[IM_TASK_EXECUTION_KEY].get("root_task_id", "")
        link = store.tasks.load(root_id) if root_id else None
        if link is None or link.thread_id != current.thread_id:
            return SessionTaskStopOutcome(False, "后台父任务尚未可靠绑定。", turn_id)
        source = execution_origin_thread(store.threads.require, current)
        binding = _primary_binding(source)
        if binding is None:
            return SessionTaskStopOutcome(False, "后台来源身份不可读。", turn_id)
        target = read_gateway_execution_target(gateway_paths(agent), store, current.thread_id)
        scope = GatewayControlScope(user_id=binding.channel_user_id, channel=binding.channel,
                                    conversation_id=binding.channel_conversation_id,
                                    metadata={"expected_execution": target.to_dict()} if target is not None else {})
        active = _GatewayRequestRecord(store.storage.task_path(link.task_id), {
            "id": link.task_id, "conversation_thread_id": link.thread_id, "conversation_task_link_status": link.status})
        prepared = _prepare_main_task_stop(agent, active, link.task_id, scope)
        if prepared.task_link is None:
            return SessionTaskStopOutcome(False, "后台执行已变化，请刷新任务状态。", turn_id)
        interrupt_by_name(session_task_interrupt_name(turn_id))
        _cancel_request_execution(link.task_id, resources=prepared.resources)
        confirmed = not prepared.preparation_errors and not (prepared.resources is not None and prepared.resources.unconfirmed)
        return SessionTaskStopOutcome(confirmed, "后台父子任务已请求停止。" if confirmed else "后台资源停止部分未确认。", turn_id)
    except (OSError, ValueError, RuntimeError):
        return SessionTaskStopOutcome(False, "后台资源停止未确认。", turn_id, "SESSION_TASK_STOP_UNCONFIRMED")


# LLM: 统一未确认错误码，避免把异常当成"已停止"；调用方只在 confirmed 为真时说已停止。
# 函数用途: 生成未确认的稳定错误码。
def _unconfirmed_code(exc: object) -> str:
    return "SESSION_TASK_STOP_UNCONFIRMED"


__all__ = ["SessionTaskStopOutcome", "stop_session_task_turn"]
