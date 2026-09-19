
# LLM: 本模块维护模型可见的 typed IR；普通请求只追加，摘要覆盖后的回收保留用户输入和当前运行事实。
# 模块用途: 保存原生工具往返及无工具续跑响应；须核对配对、缓存前缀、Compact 和取消回滚。
from __future__ import annotations

"""原生 tool_use 协议下的 IR 历史维护（Step 2 接线层）。

这一层把工具循环运行时已经产出的「调用 payload + 执行结果」翻译进 Step 1 的结构化
IR（``AssistantTurn`` / ``ToolResult`` / ``UserTurn`` / ``CompactionSummary``），追加到
``params.tool_ir_history`` 上，再由 ``AnthropicMessageAdapter`` 翻成厂商原生
``messages``。它与现有 ``tool_context: list[str]``
文本链路**共存**（灰度双轨）——只有 ``native_tool_use_active(agent)`` 为真时才写 IR，
text 协议路径一字不动。

历史项序列（与 ``message_adapter.HistoryItem`` 对齐）：

    AssistantTurn(text=该轮模型文本, tool_calls=[ToolCall, ...])
    ToolResult, ToolResult, ...        # 紧随其后、与上面调用一一配对的回执
    UserTurn(text=运行中补充输入)       # 留在到达时的准确时间位置
    CompactionSummary(text=续接摘要)    # thread/live 摘要最多一条；carried handoff 独立保留

实现要点：
- 每个工具调用先 ``_ensure_assistant_turn`` 拿到/新建「本轮」的 AssistantTurn，把
  ``ToolCall.from_payload`` 追加进它的 ``tool_calls``；再把对应 ``ToolResult`` 追加到
  历史尾。assistant 文本取该轮 ``ModelResponse.text``（首个调用时落定）。
- 「同一轮」用 ``tool_rounds`` 标定：同轮的多个工具调用共享一条 AssistantTurn（对齐
  Anthropic「一条 assistant 消息里多个 tool_use」）。轮号变化即新开一条 AssistantTurn。
- ``ToolResult`` 是 ``frozen`` 不可变值；要为 Step 3/4 的「ToolCall+ToolResult 整对增删」
  铺路，这里提供 ``drop_tool_call_pairs`` 按 ``tool_call_id`` 集合整对摘除（assistant
  turn 里删 ToolCall、历史里删配对的 ToolResult），保持配对不变量。
- ``UserTurn`` 不属于工具结果窗口，工具 compact 不得删除；需要缩短时由上层 active-turn/thread
  compact 处理。
- ``RuntimeFactsTurn`` 是宿主状态快照，不是真实用户输入；完整摘要覆盖的旧工具前缀回收时，可一并
  删除其中旧快照，但每个来源最新一份和保留工具尾部的快照不动。普通无摘要整理不能删除快照。
- ``CompactionSummary`` 不属于第二条 compact 路线；thread/live summary 是同一 IR 历史里旧工具
  往返的 replacement item，每次压缩原位替换；带稳定 schema marker 的 carried handoff 是另一条
  当前 turn 交接事实，不能被 replacement 连带删除。

为什么 content 用「结构化结果的精简文本」而不是原始 ``output`` 全文：tool_result 的
``content`` 仍是字符串，必须既保留模型可读的结果，又不把几十 KB 正文塞回 messages。
这里复用与 text 链路同源的 ``render_tool_result_for_live_prompt``（外置时只留摘要+恢复
锚点，内联时保留有界正文），保证 IR 与文本两轨的「给模型看到的结果」口径一致。
"""

from copy import deepcopy
from types import SimpleNamespace
from typing import Any

from ..backends.response_completion import has_reasoning_content, without_tool_blocks
from ..backends.tool_ir import (
    AssistantTurn,
    CompactionSummary,
    RuntimeFactsTurn,
    UserTurn,
)
from ..prompting_parts.cache_layout import CacheStructuredPrompt, prompt_cache_layout
from ..tooling.runtime_contracts import ToolCall, ToolResult
from .runtime.conversation_state import conversation_runtime_state_section


def native_tool_ir_history(params: object) -> list[Any]:
    """返回 params 上的 IR 历史 list（缺失时建空 list 并挂回）。"""
    history = getattr(params, "tool_ir_history", None)
    if not isinstance(history, list):
        history = []
        try:
            params.tool_ir_history = history  # type: ignore[attr-defined]
        except (AttributeError, TypeError):
            return history
    return history


def record_user_turn_ir(params: object, text: str) -> None:
    """把运行中用户补充输入永久插入当前 turn 的原生消息历史。"""
    content = str(text or "")
    if not content.strip():
        return
    native_tool_ir_history(params).append(UserTurn(content))


# LLM: A dynamic prompt suffix becomes an append-only typed history item before provider
# submission. Compare only the latest surviving item of the same host source: A→B→A must append
# A again. IR is the sole baseline, so cancellation/Compact rollback cannot leave a stale seen set.
# 函数用途: 同来源状态有变化才追加，未变化留在原历史位置；不修改或去重真实用户输入。
def record_runtime_facts_turn_ir(params: object, text: str, *, source: str = "") -> bool:
    content = str(text or "")
    if not content.strip():
        return False
    history = native_tool_ir_history(params)
    for item in reversed(history):
        if isinstance(item, RuntimeFactsTurn) and item.source == source:
            if item.text == content:
                return False
            break
    history.append(RuntimeFactsTurn(content, source=source))
    return True


# LLM: 预检查与实际发送共用同一个无副作用投影；来源来自布局字段，IR 副本是唯一去重基线。
# 函数用途: 在历史浅副本中追加变化状态，返回去掉重复尾部的 prompt；不改原 IR、用量账或会话状态。
def project_native_prompt_history(params: object, prompt: str) -> tuple[str, list[Any]]:
    history = list(getattr(params, "tool_ir_history", None) or [])
    layout = prompt_cache_layout(prompt)
    if layout is None:
        return prompt, history
    projected = SimpleNamespace(tool_ir_history=history)
    sections = layout.volatile_sections or (("prompt.volatile", layout.volatile_suffix),)
    for source, text in sections:
        record_runtime_facts_turn_ir(projected, text, source=source)
    record_runtime_facts_turn_ir(
        projected, conversation_runtime_state_section(params), source="conversation.runtime",
    )
    provider_prompt = CacheStructuredPrompt(
        layout.stable_prefix,
        stable_user_prefix=layout.stable_user_prefix,
        canonical_user_turn=layout.canonical_user_turn,
    )
    return provider_prompt, history


# LLM: compact 摘要与真实 UserTurn 类型分开；每次安装只替换 thread/live summary，
# schema-marked carried handoff 是当前 turn 的独立事实，必须原位保留到后续压缩代次。
# 函数用途: 在最近工具尾部之前安装唯一一条当前 turn 压缩摘要，并保留运行交接摘要。
def replace_compaction_summary_ir(params: object, text: str) -> bool:
    content = str(text or "").strip()
    if not content:
        return False
    history = native_tool_ir_history(params)
    replacement = CompactionSummary(content)
    rebuilt: list[Any] = []
    installed = False
    for item in history:
        if isinstance(item, CompactionSummary) and not _is_carried_tool_handoff_summary(item):
            if not installed:
                rebuilt.append(replacement)
                installed = True
            continue
        rebuilt.append(item)
    history[:] = rebuilt
    if installed:
        return True
    insert_at = next(
        (
            index
            for index, item in enumerate(history)
            if isinstance(item, (AssistantTurn, ToolResult))
        ),
        len(history),
    )
    history.insert(insert_at, replacement)
    return True


# LLM: Multiple projections still share CompactionSummary for compatibility. The stable schema
# marker, not natural-language content, separates a carried active-turn handoff from replaceable
# thread/live summaries until the IR gains a dedicated typed variant.
# 函数用途: 识别 compact 续跑带入的工具交接摘要，避免第二次压缩把它当旧 thread summary 删除。
def _is_carried_tool_handoff_summary(item: CompactionSummary) -> bool:
    return str(item.text or "").startswith("[active-turn-tool-handoff]")


# LLM: 开轮时要把后端清洗后的有序 content blocks 与可见 text 一起落到同一 AssistantTurn，避免下一工具轮丢失 reasoning 签名。
# 函数用途: 为当前模型轮建立 assistant 历史，并保存后续原生请求需要续接的内部内容块。
def open_assistant_turn_ir(
    params: object,
    *,
    tool_rounds: int,
    response_text: str,
    response_content_blocks: list[dict[str, Any]] | None = None,
) -> None:
    """为「这一轮」开一条 AssistantTurn 并落定其可见文本（每轮调用一次）。

    在本轮第一个工具结果落历史前调用，保证 assistant 文本来自该轮真实
    ``ModelResponse.text``。同一轮内多个工具调用随后由 ``record_tool_call_ir`` 追加进
    这条 turn 的 ``tool_calls``。
    """
    _ensure_assistant_turn(
        native_tool_ir_history(params),
        tool_rounds,
        response_text,
        response_content_blocks,
    )


# LLM: 只在继续生成且本轮零工具执行时调用；追加独立 AssistantTurn，不能按未变的工具轮号合并。
# 函数用途: 保存继续生成前的正文和原生思考，滤掉未执行工具；不产生公开答复或工具执行事实。
def record_unexecuted_response_ir(
    params: object, *, response_text: str, response_content_blocks: list[dict[str, Any]],
) -> None:
    blocks = deepcopy(without_tool_blocks(response_content_blocks))
    if response_text or has_reasoning_content(blocks):
        native_tool_ir_history(params).append(AssistantTurn(text=response_text, content_blocks=blocks))


def record_tool_call_ir(
    params: object,
    *,
    tool_rounds: int,
    call: ToolCall,
    result: ToolResult,
) -> None:
    """Append one canonical call/result pair to native history."""

    if call.call_id != result.call_id or call.tool_name != result.tool_name:
        raise ValueError("canonical tool call/result pair mismatch")
    history = native_tool_ir_history(params)
    turn = _ensure_assistant_turn(history, tool_rounds, "")
    turn.tool_calls.append(call)
    history.append(result)


# LLM: 成对回收不改变原始账本；完整摘要存在时才能删除已无工具的 assistant 轮及连续退休前缀内的旧
# RuntimeFactsTurn。各来源最新快照、真实 UserTurn 和保留工具尾部必须保持；探针与事务回滚复用此规则。
# 函数用途: 摘除已覆盖的工具往返、旧思考与过期状态快照，避免长任务压缩后仍被旧状态塞满。
def drop_tool_call_pairs(
    params: object,
    call_ids: set[str],
    *,
    drop_completed_tool_turns: bool = False,
) -> int:
    """按 tool_use id 集合「整对」摘除 ToolCall 与配对的 ToolResult。

    Step 3/4 的 compact 在丢弃最老工具往返时调用这里，保证 assistant 消息里不留下
    没有对应 tool_result 的孤儿 tool_use（Anthropic 会拒绝），也不留下指向已删
    tool_use 的孤儿 tool_result。只有 ``drop_completed_tool_turns=True`` 且调用方已经持有
    完整替代摘要时，才连同已无保留调用的旧 assistant 正文、连续退休工具前缀中的旧状态一起删除。
    各来源最新快照、真实用户输入和未退休工具尾部始终保留。返回实际摘除的「对」数（以 ToolResult 计）。
    """
    if not call_ids:
        return 0
    history = native_tool_ir_history(params)
    removed = sum(
        1 for item in history if isinstance(item, ToolResult) and item.call_id in call_ids
    )
    retired_facts = (
        _summarized_runtime_fact_indexes(history, call_ids)
        if drop_completed_tool_turns
        else set()
    )
    rebuilt = [
        _rewritten_history_item(
            item,
            call_ids,
            drop_completed_tool_turns=drop_completed_tool_turns,
        )
        for index, item in enumerate(history)
        if index not in retired_facts
    ]
    history[:] = [
        item
        for item in rebuilt
        if item is not None and not _is_empty_assistant_turn(item)
    ]
    return removed


# LLM: 仅从 typed 调用/结果的顺序计算连续退休前缀；跳过较新的任意调用不授权删除其之前仍在用的状态。
# 函数用途: 找出摘要可替代的旧状态；不越过尚有保留调用的并行轮，每个来源的最新状态始终保留。
def _summarized_runtime_fact_indexes(history: list[Any], call_ids: set[str]) -> set[int]:
    retired_through = -1
    for index, item in enumerate(history):
        if isinstance(item, AssistantTurn) and any(
            call.call_id not in call_ids for call in item.tool_calls
        ):
            break
        if isinstance(item, ToolResult):
            if item.call_id not in call_ids:
                break
            retired_through = index
    latest_by_source = {
        item.source: index for index, item in enumerate(history) if isinstance(item, RuntimeFactsTurn)
    }
    return {
        index for index, item in enumerate(history)
        if isinstance(item, RuntimeFactsTurn)
        and index <= retired_through
        and index != latest_by_source[item.source]
    }


# LLM: This is a structural IR rewrite; prose never decides whether a turn is covered or removable.
# 函数用途: 重写一条原生历史记录，并按调用方的摘要覆盖事实决定是否整轮删除旧 assistant 内容。
def _rewritten_history_item(
    item: Any,
    call_ids: set[str],
    *,
    drop_completed_tool_turns: bool = False,
) -> Any:
    """摘除命中的工具对；完整替代摘要存在时也可删除其所属旧 assistant 轮。"""
    if isinstance(item, ToolResult):
        return None if item.call_id in call_ids else item
    if isinstance(item, AssistantTurn):
        matched = any(call.call_id in call_ids for call in item.tool_calls)
        rewritten = _assistant_turn_without(item, call_ids)
        if drop_completed_tool_turns and matched and not rewritten.tool_calls:
            return None
        return rewritten
    return item


# LLM: 同轮归并只允许补齐先到的真实响应字段；不得用后续工具结果覆盖已经保存的 assistant 块。
# 函数用途: 找到当前轮的 AssistantTurn，必要时新建，或为防御性空 turn 补齐正文和内容块。
def _ensure_assistant_turn(
    history: list[Any],
    tool_rounds: int,
    response_text: str,
    response_content_blocks: list[dict[str, Any]] | None = None,
) -> AssistantTurn:
    """拿到「本轮」的 AssistantTurn；轮号未变且尾项就是它则复用，否则新开一条。

    复用判据是「历史尾部最后一个 AssistantTurn 的轮号 == 当前轮号」。同一轮里第二个
    工具调用追加时，中间已经插了上一调用的 ToolResult，所以这里按「最近的 turn 标记」
    判断，而非「尾项必须是 turn」。
    """
    marker = _last_turn_marker(history)
    if marker is not None and marker[0] == tool_rounds:
        turn = marker[1]
        # 文本以「先到的非空值」为准：open_assistant_turn_ir 通常先带文本开 turn，
        # 后续 record 以空文本复用时不覆盖；反之若 turn 先被空文本补开，这里回填。
        if not turn.text and response_text:
            object.__setattr__(turn, "text", str(response_text))
        if not turn.content_blocks and response_content_blocks:
            object.__setattr__(turn, "content_blocks", deepcopy(response_content_blocks))
        return turn
    turn = _MarkedAssistantTurn(
        text=str(response_text or ""),
        content_blocks=deepcopy(response_content_blocks or []),
        _tool_round=tool_rounds,
    )
    history.append(turn)
    return turn


def _last_turn_marker(history: list[Any]) -> tuple[int, AssistantTurn] | None:
    for item in reversed(history):
        if isinstance(item, _MarkedAssistantTurn):
            return item._tool_round, item
        if isinstance(item, AssistantTurn):
            # 普通 AssistantTurn（无轮号标记）不参与同轮归并，保守新开一条。
            return None
    return None


# LLM: 删除任何 tool_use 都会破坏原厂 thinking 签名与块完整性，因此重写后的 turn 必须退回 text+剩余 canonical calls，不能保留旧 content_blocks。
# 函数用途: 从一轮 assistant 历史中删除指定工具调用，并在发生结构变化时清掉失效的厂商原生块。
def _assistant_turn_without(turn: AssistantTurn, call_ids: set[str]) -> AssistantTurn:
    kept = [call for call in turn.tool_calls if call.call_id not in call_ids]
    if len(kept) == len(turn.tool_calls):
        return turn
    if isinstance(turn, _MarkedAssistantTurn):
        return _MarkedAssistantTurn(
            text=turn.text,
            tool_calls=kept,
            content_blocks=[],
            _tool_round=turn._tool_round,
        )
    return AssistantTurn(text=turn.text, tool_calls=kept, content_blocks=[])


# LLM: thinking-only assistant turn 仍是有效协议历史，不能因没有可见文字或工具调用被 compact 当空项删除。
# 函数用途: 判断 AssistantTurn 是否真的没有正文、调用和可回放内容。
def _is_empty_assistant_turn(item: Any) -> bool:
    return (
        isinstance(item, AssistantTurn)
        and not item.text
        and not item.tool_calls
        and not item.content_blocks
    )


# LLM: 该运行时子类必须完整承载 AssistantTurn 的所有协议字段；新增字段时同步构造器和 compact 重写路径。
# 类用途: 在 AssistantTurn 上附加当前工具轮编号，供同一轮多个调用归并。
class _MarkedAssistantTurn(AssistantTurn):
    """带轮号标记的 AssistantTurn，仅供本模块按 tool_rounds 归并同轮调用。

    ``AssistantTurn`` 是 frozen dataclass；这里用普通子类承载一个非冻结的轮号属性，
    适配器只读 ``text`` / ``tool_calls``，对它一视同仁，不感知这个标记。
    """

    __slots__ = ("_tool_round",)

    def __init__(
        self,
        *,
        text: str = "",
        tool_calls: list[ToolCall] | None = None,
        content_blocks: list[dict[str, Any]] | None = None,
        _tool_round: int = 0,
    ) -> None:
        object.__setattr__(self, "text", str(text or ""))
        object.__setattr__(self, "tool_calls", list(tool_calls or []))
        object.__setattr__(self, "content_blocks", list(content_blocks or []))
        object.__setattr__(self, "_tool_round", int(_tool_round))


__all__ = [
    "drop_tool_call_pairs",
    "native_tool_ir_history",
    "open_assistant_turn_ir",
    "record_runtime_facts_turn_ir",
    "project_native_prompt_history",
    "replace_compaction_summary_ir",
    "record_tool_call_ir",
    "record_unexecuted_response_ir",
]
