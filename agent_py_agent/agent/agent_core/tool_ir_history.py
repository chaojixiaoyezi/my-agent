
# LLM: typed IR是唯一历史；有界投影LRU冻结全部真实字段并保prior引用身份，不消费guidance；applied前置且调用/推理不拆对。
# 模块用途: 保存工具与模型历史，复用只读投影并隔离出站副本；须核对配对、媒体、缓存和取消回滚。
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
    UserTurn(text=运行中补充输入, input_ids=原插话身份)  # 留在到达时的准确时间位置
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

import hashlib
import json
import sys
import threading
from collections import OrderedDict
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import fields, is_dataclass
from types import SimpleNamespace
from typing import Any

from ..backends.cache_diagnostics import _strict_json_cache_key
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

_PROJECTION_LOCK = threading.Lock()
_MAX_PROJECTION_CACHE_ENTRIES = 64
_MAX_PROJECTION_CACHE_BYTES = 2 * 1024 * 1024
_MAX_SINGLE_PROJECTION_BYTES = 256 * 1024
_PROJECTION_CACHE: OrderedDict[tuple, list[Any]] = OrderedDict()
_PROJECTION_CACHE_SIZES: dict[tuple, int] = {}
_PROJECTION_CACHE_BYTES = 0
_PROJECTION_TLS = threading.local()


# LLM: 借用prior占位类型与普通消息字典/元组严格区分，避免缓存解码误判真实消息内容。
# 类用途: 标记缓存投影中借用外部prior_messages的下标槽位。
class _BorrowedPriorSlot(int):
    pass


# LLM: 冻结实际dataclass字段而非过时伪字段；包含UserTurn媒体及canonical结果所有渲染来源，未知值或累计超单条预算不缓存。
#   MappingProxyType按Mapping读取且保键序，标量沿严格JSON键保bool/数值类型、NaN及负零；递归有界。
# 函数用途: 将IR、历史和嵌套参数转成严格结构键，不序列化、不消费状态或复制prior。
def _freeze_projection_value(value: object, *, _depth: int = 0, _budget: list[int] | None = None) -> tuple | None:
    if _depth > 64:
        return None
    budget = [_MAX_SINGLE_PROJECTION_BYTES] if _budget is None else _budget
    budget[0] -= sys.getsizeof(value)
    if budget[0] < 0:
        return None
    if value is None or type(value) in (bool, int, float, str):
        return _strict_json_cache_key(value, _budget=budget)
    if is_dataclass(value) and not isinstance(value, type):
        values = [(item.name, getattr(value, item.name)) for item in fields(value)]
        if isinstance(value, AssistantTurn):
            values.append(("round_index", getattr(value, "round_index", getattr(value, "_tool_round", None))))
        frozen = _freeze_projection_value(dict(values), _depth=_depth + 1, _budget=budget)
        return None if frozen is None else (type(value).__module__, type(value).__qualname__, frozen)
    if isinstance(value, Mapping):
        pairs = []
        for key, item in value.items():
            ck = _freeze_projection_value(key, _depth=_depth + 1, _budget=budget)
            cv = _freeze_projection_value(item, _depth=_depth + 1, _budget=budget)
            if ck is None or cv is None:
                return None
            pairs.append((ck, cv))
        return ("map", tuple(pairs))
    if isinstance(value, (list, tuple)):
        items = []
        for item in value:
            ci = _freeze_projection_value(item, _depth=_depth + 1, _budget=budget)
            if ci is None:
                return None
            items.append(ci)
        return ("seq", tuple(items))
    return None


# LLM: 值键捕获原位改写；额外prior对象身份只守copy_prior=False借用合同，不能以身份代替值核对。
# 函数用途: 同时核对真实IR、旧消息内容和借用引用，不能冻结时回到原投影而不猜相同。
def _projection_cache_key(history: list, prior_list: list) -> tuple | None:
    frozen = _freeze_projection_value((history, prior_list))
    return None if frozen is None else (frozen, tuple(id(item) for item in prior_list))


# LLM: 锁外沿唯一适配器对隔离快照投影，只缓存独立派生块并用专用槽位替代借用prior，不强引用外部可变prior字典。
#   原位可变arguments或content_blocks必须脱离缓存；含MappingProxy等不可证明可复制值的派生块旁路缓存。
# 函数用途: 建立隔离派生块的缓存表示；不能安全隔离时返回原投影及明确不可缓存标记。
def _build_cached_projection(history: list, prior_list: list) -> tuple[list[Any], bool]:
    from ..backends.message_adapter import project_native_history_messages

    observed = _projection_cache_key(history, prior_list)
    _PROJECTION_TLS.observed_key = observed
    snapshot = history
    if observed is not None:
        try:
            snapshot = deepcopy(history)
        except TypeError:
            _PROJECTION_TLS.observed_key = None
            snapshot = history
        else:
            if _projection_cache_key(snapshot, prior_list) != observed:
                _PROJECTION_TLS.observed_key = None
                snapshot = history
    messages = project_native_history_messages(snapshot, prior_list, copy_prior=False)
    _PROJECTION_TLS.post_key = (
        _projection_cache_key(history, prior_list) if _PROJECTION_TLS.observed_key is not None else None
    )
    prior_index_by_id = {id(item): index for index, item in enumerate(prior_list)}
    if any(id(item) not in prior_index_by_id and _strict_json_cache_key(item) is None for item in messages):
        return messages, False
    return [
        _BorrowedPriorSlot(prior_index_by_id[id(item)]) if id(item) in prior_index_by_id else deepcopy(item)
        for item in messages
    ], True


# LLM: 锁外估算投影键与派生消息的保留字节，供单条上限和总字节LRU双重淘汰使用。
# 函数用途: 计量缓存键与消息树的内存占用，避免长文本历史驻留进程缓存。
def _projection_entry_bytes(key: tuple, messages: list[Any]) -> int:
    total, seen, pending = 0, set(), [key, messages]
    while pending:
        item = pending.pop()
        obj_id = id(item)
        if obj_id in seen:
            continue
        seen.add(obj_id)
        total += sys.getsizeof(item)
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
    return total


# LLM: 锁内写入单条投影缓存并按64项与2MiB双预算淘汰旧项，不触碰外部状态。
# 函数用途: 原子登记一条已验证的投影缓存并在超预算时按FIFO淘汰。
def _store_projection_cache_entry(key: tuple, messages: list[Any], entry_bytes: int) -> None:
    global _PROJECTION_CACHE_BYTES
    with _PROJECTION_LOCK:
        if not _PROJECTION_CACHE:
            _PROJECTION_CACHE_SIZES.clear()
            _PROJECTION_CACHE_BYTES = 0
        prev_bytes = _PROJECTION_CACHE_SIZES.pop(key, 0) if _PROJECTION_CACHE.pop(key, None) is not None else 0
        _PROJECTION_CACHE_BYTES = max(0, _PROJECTION_CACHE_BYTES - prev_bytes)
        _PROJECTION_CACHE[key] = messages
        _PROJECTION_CACHE_SIZES[key] = entry_bytes
        _PROJECTION_CACHE_BYTES += entry_bytes
        while _PROJECTION_CACHE and (
            len(_PROJECTION_CACHE) > _MAX_PROJECTION_CACHE_ENTRIES
            or _PROJECTION_CACHE_BYTES > _MAX_PROJECTION_CACHE_BYTES
        ):
            old_key, _ = _PROJECTION_CACHE.popitem(last=False)
            _PROJECTION_CACHE_BYTES = max(0, _PROJECTION_CACHE_BYTES - _PROJECTION_CACHE_SIZES.pop(old_key, 0))


# LLM: 仅LRU操作持锁，冻结/投影/复制在锁外；发布前复核构造时与前后快照键，finally清理TLS防止驻留，按64项与2MiB双预算淘汰。
# 函数用途: 复用当前真实结构的消息；并发改写（含内层ABA）、超大项或不可安全隔离结果不进入缓存也不滞留TLS。
def _cached_native_projection(history: list, prior_list: list) -> tuple[list[Any], bool]:
    key = _projection_cache_key(history, prior_list)
    if key is not None:
        with _PROJECTION_LOCK:
            cached = _PROJECTION_CACHE.get(key)
            if cached is not None:
                _PROJECTION_CACHE.move_to_end(key)
                return cached, True
    _PROJECTION_TLS.observed_key = None
    _PROJECTION_TLS.post_key = None
    try:
        messages, cacheable = _build_cached_projection(history, prior_list)
        if (
            key is not None
            and cacheable
            and getattr(_PROJECTION_TLS, "observed_key", None) == key
            and getattr(_PROJECTION_TLS, "post_key", None) == key
            and _projection_cache_key(history, prior_list) == key
        ):
            entry_bytes = _projection_entry_bytes(key, messages)
            if entry_bytes <= _MAX_SINGLE_PROJECTION_BYTES:
                _store_projection_cache_entry(key, messages, entry_bytes)
        return messages, cacheable
    finally:
        _PROJECTION_TLS.observed_key = None
        _PROJECTION_TLS.post_key = None


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


# LLM: 插话身份及媒体由原 guidance packet 传入并与正文一起留在 IR；此处不读取图片或更改授权。
# 函数用途: 把运行中用户补充输入追加到当前 turn 的原生消息历史，不在这里按正文去重。
def record_user_turn_ir(params: object, text: str, *, input_ids: tuple[str, ...] = (), media: tuple = ()) -> None:
    """把运行中用户补充输入永久插入当前 turn 的原生消息历史。"""
    content = str(text or "")
    if not content.strip():
        return
    native_tool_ir_history(params).append(UserTurn(content, input_ids=tuple(input_ids), media=tuple(media)))


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


# LLM: 预检查与发送共享同一投影；显式 conversation_state 是冻结事实，None 才读取原 params 的内存快照。
# 函数用途: 在历史副本追加变化状态并移除 prompt 重复尾部，不改原 IR、用量账或会话状态。
def project_native_prompt_history(
    params: object, prompt: str, *, conversation_state: str | None = None,
) -> tuple[str, list[Any]]:
    history = list(getattr(params, "tool_ir_history", None) or [])
    layout = prompt_cache_layout(prompt)
    if layout is None:
        return prompt, history
    projected = SimpleNamespace(tool_ir_history=history)
    sections = layout.volatile_sections or (("prompt.volatile", layout.volatile_suffix),)
    for source, text in sections:
        record_runtime_facts_turn_ir(projected, text, source=source)
    record_runtime_facts_turn_ir(
        projected,
        conversation_runtime_state_section(params) if conversation_state is None else conversation_state,
        source="conversation.runtime",
    )
    provider_prompt = CacheStructuredPrompt(
        layout.stable_prefix,
        stable_user_prefix=layout.stable_user_prefix,
        canonical_user_turn=layout.canonical_user_turn,
    )
    return provider_prompt, history


# LLM: 真实guidance先在副本投影再冻键；唯一布局及applied来源保持。只读借用返回新外列表，嵌套不得改写。
#   默认发送逐项调用message_adapter.deepcopy，隔离prior及缓存的派生消息，不消费seen或写原IR。
# 函数用途: 让容量、指纹及发送复用未变化的消息投影，默认发送仍是独立深副本。
def project_native_provider_messages(
    history: object, *, prior_messages: object = (),
    tool_context: object = (), forwarded_guidance: object = (), copy_prior: bool = True,
) -> list[dict[str, Any]]:
    from ..backends import message_adapter
    from .tool_ir_guidance import unforwarded_runtime_guidance

    prior_list = list(prior_messages or ())
    projected = SimpleNamespace(tool_ir_history=list(history or ()))
    guidance = unforwarded_runtime_guidance(list(tool_context or ()), set(forwarded_guidance or ()))
    if guidance:
        record_runtime_facts_turn_ir(projected, "\n\n".join(guidance), source="runtime.guidance")
    messages, cacheable = _cached_native_projection(projected.tool_ir_history, prior_list)
    if not cacheable:
        return [message_adapter.deepcopy(item) for item in messages] if copy_prior else list(messages)
    if copy_prior:
        return [
            message_adapter.deepcopy(prior_list[int(item)] if isinstance(item, _BorrowedPriorSlot) else item)
            for item in messages
        ]
    return [prior_list[int(item)] if isinstance(item, _BorrowedPriorSlot) else deepcopy(item) for item in messages]


# LLM: 开轮与 live 候选统一 generation 标题，检查点仍存原始 summary；不写状态或发请求。
# 函数用途: 构造跨回合逐字一致的摘要项，兼容服务端密文及纯文本路径。
def applied_compact_summary_item(text: str, generation: int, provider_compaction: str = "") -> CompactionSummary:
    return CompactionSummary(
        f"# Earlier Conversation Summary (generation {generation})\n{text}",
        source="applied_compact", provider_compaction=provider_compaction,
    )


# LLM: 逻辑回合身份要覆盖"实际出站内容"：native 下 tool_context/IR 变化不反映在渲染 prompt 上，
#   只认 prompt 指纹会把内容真变化的重跑误并进旧回合。这里对与真实出站同源的纯投影结果做规范化
#   指纹（只读：显式借用prior，不消费seen、不写IR或修改投影）。同一请求的断流重试里，未转发指引先经
#   tool_context、再经 IR 投影，两次输出一致，因此身份稳定。判定或投影失败时返回空串，身份退化为
#   prompt 指纹，绝不中断模型主链。改动时同步核对 call_runtime.logical_model_call_id 与
#   tool_model_generation._native_provider_messages 的出站口径。
# 函数用途: 计算此刻会出站的 native provider messages 指纹；text 模式或不可判定时返回空串。
def provider_messages_fingerprint(params: object) -> str:
    from .native_tool_protocol import native_tool_use_active

    try:
        if not native_tool_use_active(params):
            return ""
        state = getattr(params, "live_archive_state", None)
        seen = state.get("_forwarded_runtime_guidance") if isinstance(state, dict) else None
        messages = project_native_provider_messages(
            getattr(params, "tool_ir_history", None),
            prior_messages=getattr(params, "provider_history_messages", None),
            tool_context=getattr(params, "tool_context", None),
            forwarded_guidance=seen if isinstance(seen, set) else (),
            copy_prior=False,
        )
        encoded = json.dumps(messages, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        return ""
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


# LLM: compact 摘要与真实 UserTurn 类型分开；每次安装只替换 thread/live summary，
# writer-owned摘要标记applied_compact；carried handoff原位保留，重跑不得重复恢复摘要前缀。
# 函数用途: 在最近工具尾部之前安装唯一一条当前 turn 压缩摘要，并保留运行交接摘要。
def replace_compaction_summary_ir(params: object, text: str, *, provider_compaction: str = "") -> bool:
    content = str(text or "").strip()
    if not content:
        return False
    history = native_tool_ir_history(params)
    replacement = CompactionSummary(content, source="applied_compact", provider_compaction=provider_compaction)
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
    _remember_pending_observation_screenshot(params, result)


# 宿主附加的观察截图引用在运行状态里的键：下一次出站投影注入一次后消费；不进 IR、不进 transcript。
OBSERVATION_SCREENSHOT_PENDING_KEY = "_pending_observation_screenshot"


# LLM: 观察截图引用只挂在宿主 metadata（tool_call_archive_record 落盘后写入）；这里只把它登记到本回合运行状态，
#   由下一次出站投影注入并消费。新观察覆盖旧值（每次只给最新一张），不解析正文、不改 IR。
# 函数用途: 把工具结果携带的观察截图引用登记为"下一次模型请求待注入"。
def _remember_pending_observation_screenshot(params: object, result: ToolResult) -> None:
    metadata = getattr(result, "metadata", None)
    ref = metadata.get("observation_screenshot") if isinstance(metadata, dict) else None
    state = getattr(params, "live_archive_state", None)
    if isinstance(ref, dict) and str(ref.get("sha256") or "") and isinstance(state, dict):
        state[OBSERVATION_SCREENSHOT_PENDING_KEY] = dict(ref)


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
# 函数用途: 将已摘要调用转换为覆盖位置，复用联合恢复的旧状态退休规则，保留每个来源的最新状态。
def _summarized_runtime_fact_indexes(history: list[Any], call_ids: set[str]) -> set[int]:
    positions = {
        index for index, item in enumerate(history)
        if (isinstance(item, ToolResult) and item.call_id in call_ids)
        or (isinstance(item, AssistantTurn) and item.tool_calls
            and all(call.call_id in call_ids for call in item.tool_calls))
    }
    return summarized_runtime_fact_indexes(history, positions)


# LLM: 位置集合必须来自调用方已证明覆盖的完整工具组；这里只看 typed 顺序，不按正文或裸调用名猜覆盖。
# 函数用途: 为运行中及联合恢复共用连续退休前缀规则，始终保留每个来源的最新状态和全部带媒体的原事件，工具摘要不证明图片已被覆盖。
def summarized_runtime_fact_indexes(history: list[Any], retired_positions: set[int]) -> set[int]:
    retired_through = -1
    for index, item in enumerate(history):
        is_tool_item = isinstance(item, ToolResult) or (isinstance(item, AssistantTurn) and item.tool_calls)
        if is_tool_item and index not in retired_positions:
            break
        if isinstance(item, ToolResult):
            retired_through = index
    latest_by_source = {
        item.source: index for index, item in enumerate(history) if isinstance(item, RuntimeFactsTurn)
    }
    return {
        index for index, item in enumerate(history)
        if isinstance(item, RuntimeFactsTurn)
        and not item.media
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


# LLM: 工具摘除使thinking签名失效；保原安全text/source，工具仍从剩余canonical calls重建，联测Compact与阶段回放。
# 函数用途: 删除指定工具调用及失效推理，同时保留正文的消息边界和来源。
def _assistant_turn_without(turn: AssistantTurn, call_ids: set[str]) -> AssistantTurn:
    kept = [call for call in turn.tool_calls if call.call_id not in call_ids]
    if len(kept) == len(turn.tool_calls):
        return turn
    from ..backends.assistant_messages import text_only_blocks

    safe_blocks = text_only_blocks(turn.content_blocks)
    if isinstance(turn, _MarkedAssistantTurn):
        return _MarkedAssistantTurn(
            text=turn.text,
            tool_calls=kept,
            content_blocks=safe_blocks,
            _tool_round=turn._tool_round,
        )
    return AssistantTurn(text=turn.text, tool_calls=kept, content_blocks=safe_blocks)


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
    "provider_messages_fingerprint",
    "replace_compaction_summary_ir",
    "record_tool_call_ir",
    "record_unexecuted_response_ir",
]
