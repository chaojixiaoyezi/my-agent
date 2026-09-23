# LLM: 归并同片Compact快照；捕获入口仅在typed overflow沿原mailbox释放未提交插话，其余恢复纯计算，不拥有提交权。
# 模块用途: 保留完整原生工具回执和精确插话身份，避免外层重跑退化为preview或复活已释放消息。
from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass, fields, replace

from .active_turn_input import exclude_active_turn_user_input_ids, merge_active_turn_user_inputs


# LLM: 最新非空完整归档替换旧快照；插话按原 ID 合并并排除已释放编号，不能从正文恢复或合并工具增量。
# 函数用途: 为同一轮压缩重试计算工具和插话携带内容，不修改传入快照；同步前后台超窗及插话恢复回归。
def compact_overflow_carry(
    *,
    carried_archive_tool_calls: list[dict[str, object]],
    carried_active_turn_user_inputs: list[dict[str, object]],
    result_archive_tool_calls: object,
    result_active_turn_user_inputs: object,
    released_input_ids: Iterable[str],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    result_archive = [dict(item) for item in list(result_archive_tool_calls or []) if isinstance(item, dict)]
    next_inputs = exclude_active_turn_user_input_ids(
        merge_active_turn_user_inputs(carried_active_turn_user_inputs, result_active_turn_user_inputs),
        released_input_ids,
    )
    return result_archive or carried_archive_tool_calls, next_inputs


# LLM: 只在同进程同宿主请求的overflow边界传递；旧attempt仅作来源，不授予当前执行权，不进入持久checkpoint。
# 类用途: 冻结真实原生历史及其适用身份，避免恢复时把完整回执退化为归档预览。
@dataclass(frozen=True)
class NativeCompactCarry:
    owner_id: str
    request_id: str
    run_id: str
    task_id: str
    source_attempt_id: str
    conversation_turn_id: str
    compact_context: object
    history: tuple[object, ...]
    tool_context: tuple[str, ...]
    forwarded_guidance: frozenset[str]
    released_input_ids: tuple[str, ...] = ()

    # LLM: 嵌套ToolCall/ToolResult和视图不得共享可变引用；身份与空历史按捕获事实保留，不补造执行标识。
    # 函数用途: 在创建携带快照时隔离原运行对象，之后的工具循环修改不影响待恢复材料。
    def __post_init__(self) -> None:
        for name in ("compact_context", "history", "tool_context", "forwarded_guidance"):
            object.__setattr__(self, name, deepcopy(getattr(self, name)))


# LLM: 只在typed overflow/native回传current_params，先沿原mailbox恢复器释放准确旧attempt的未提交插话；其它结束不执行此操作。
# 函数用途: 释放尚未提交的插话并冻结原IR及精确释放ID，供同一宿主下一次恢复，不改变执行权限。
def capture_native_compact_carry(agent: object, params: object, response: object) -> NativeCompactCarry | None:
    if (getattr(response, "runtime_status", "") != "context_overflow"
            or getattr(getattr(params, "tool_protocol_snapshot", None), "source_protocol", "") != "native"):
        return None
    from ..agent_core.runtime.guidance import release_reserved_turn_input_after_attempt

    released = release_reserved_turn_input_after_attempt(agent, params)
    return NativeCompactCarry(
        owner_id=str(getattr(getattr(agent, "home_paths", None), "owner_id", "") or ""),
        request_id=params.request_id, run_id=params.run_id, task_id=params.task_id,
        source_attempt_id=params.attempt_id, compact_context=params.compact_context,
        conversation_turn_id=params.conversation_turn_id,
        history=tuple(params.tool_ir_history), tool_context=tuple(params.tool_context),
        forwarded_guidance=frozenset(params.live_archive_state.get("_forwarded_runtime_guidance", ())),
        released_input_ids=released,
    )


# LLM: 释放只认原mailbox ID，不能按相同文字删除UserTurn；完整包部分释放无法拆正文，必须明确失败而非重复发送。
# 函数用途: 从一次overflow结果取得临时原生材料，剔除尚未提交给模型且已经退回mailbox的插话。
def native_compact_carry_from_result(result: object) -> NativeCompactCarry | None:
    from ..backends.tool_ir import UserTurn
    from .compact_guard import ConversationCompactError

    carry = getattr(result, "native_compact_carry", None)
    if carry is None:
        return None
    if not isinstance(carry, NativeCompactCarry) or getattr(result, "runtime_status", "") != "context_overflow":
        raise ConversationCompactError("原生恢复载体或结果状态无效", code="COMPACT_SOURCE_CHANGED")
    released = set(carry.released_input_ids)
    history = []
    for item in carry.history:
        ids = set(item.input_ids) if isinstance(item, UserTurn) else set()
        if ids & released:
            if not ids <= released:
                raise ConversationCompactError("插话仅部分释放，无法无损拆分", code="COMPACT_SOURCE_CHANGED")
            continue
        history.append(item)
    return replace(carry, history=tuple(history))


# LLM: 当前canonical身份及同scope/view必须匹配；主代理新attempt仍由DB创建，子代理同attempt延续，旧工具身份保持不改。
# 函数用途: 校验重跑载体并返回独立副本，工具/权限快照及provider历史前缀仍由正常准备重建。
def restore_native_compact_carry(agent: object, params: object) -> NativeCompactCarry | None:
    from ..backends.tool_ir import AssistantTurn
    from .compact_guard import ConversationCompactError

    carry = getattr(params, "native_compact_carry", None)
    if carry is None:
        return None
    owner = str(getattr(getattr(agent, "home_paths", None), "owner_id", "") or "")
    if (not isinstance(carry, NativeCompactCarry)
            or (carry.owner_id, carry.request_id, carry.run_id, carry.task_id)
            != (owner, params.request_id, params.run_id, params.task_id)
            or carry.conversation_turn_id != params.conversation_turn_id
            or carry.compact_context != params.compact_context
            or (params.context_scope == "task_local" and carry.source_attempt_id != params.attempt_id)):
        raise ConversationCompactError("原生恢复来源身份或摘要视图已变化", code="COMPACT_SOURCE_CHANGED")
    # 私有tool_round归并标记只属于上一循环；新循环不得把新模型响应并进旧AssistantTurn。
    history = tuple(AssistantTurn(**{field.name: deepcopy(getattr(item, field.name)) for field in fields(AssistantTurn)})
                    if isinstance(item, AssistantTurn) else deepcopy(item) for item in carry.history)
    return replace(carry, history=history)
