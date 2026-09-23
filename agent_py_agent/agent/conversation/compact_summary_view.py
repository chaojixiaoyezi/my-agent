# LLM: 本模块只读取原 committed checkpoint chain；提交指针和账本仍是唯一权威。
# 摘要替代关系沿 summary_base_checkpoint_id，不得把提交链的局部覆盖无条件合并。
# 模块用途: 为一个冻结作用域解析可用摘要及其真正覆盖的消息和工具来源。
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .compact_checkpoint import committed_compact_checkpoint_chain
from .compact_scope import THREAD_COMPACT_SCOPE, CompactScope, scope_can_inherit

if TYPE_CHECKING:
    from ..core import SimpleAgent
    from .models import ConversationThread


# LLM: 覆盖 refs 来自选中摘要的 base 链；legacy 消息只有终点，工具缺完整运行身份。
# 类用途: 把本次请求可用的摘要和精确/旧式覆盖证据交给历史与活动工具投影。
@dataclass(frozen=True)
class CompactSummaryView:
    checkpoint_id: str = ""
    summary: str = ""
    operation_evidence: dict[str, object] = field(default_factory=dict)
    source_message_ids: frozenset[str] = frozenset()
    source_tool_refs: tuple[dict[str, object], ...] = ()
    legacy_message_end_ids: tuple[str, ...] = ()
    generation: int = 0


# LLM: v1/v2 的摘要属于全线程，previous 兼任摘要 base；v3 读取严格显式字段。
# 函数用途: 将一条已提交 checkpoint 解释为其历史版本的摘要范围。
def _checkpoint_scope(row: dict[str, object]) -> CompactScope:
    schema = row.get("schema")
    if schema in {"conversation_compact_checkpoint.v1", "conversation_compact_checkpoint.v2"}:
        return THREAD_COMPACT_SCOPE
    if schema != "conversation_compact_checkpoint.v3":
        raise OSError("conversation compact checkpoint schema is unknown")
    try:
        return CompactScope.from_dict(row.get("scope"))
    except (TypeError, ValueError) as exc:
        raise OSError("conversation compact checkpoint scope is invalid") from exc


# LLM: 旧提交链没有独立摘要 base；只在 v1/v2 上显式采用 previous，不推断 v3 默认值。
# 函数用途: 读取一个摘要实际继承的 checkpoint 引用。
def _summary_base_id(row: dict[str, object]) -> str:
    if row.get("schema") in {"conversation_compact_checkpoint.v1", "conversation_compact_checkpoint.v2"}:
        value = row.get("previous_checkpoint_id")
    else:
        if "summary_base_checkpoint_id" not in row:
            raise OSError("conversation compact summary base is missing")
        value = row["summary_base_checkpoint_id"]
    if type(value) is not str:
        raise OSError("conversation compact summary base is invalid")
    return value


# LLM: 所有已知版本原writer均记录摘要哈希；旧版本不免除完整性检查，不能将坏旧摘要当新候选基础。
# 函数用途: 验证摘要正文与原哈希一致，拒绝缺失或损坏的语义基础。
def _validate_summary(row: dict[str, object]) -> None:
    summary = row.get("summary")
    digest = row.get("summary_sha256")
    if type(summary) is not str or not summary or type(digest) is not str:
        raise OSError("conversation compact summary is invalid")
    if hashlib.sha256(summary.encode("utf-8")).hexdigest() != digest:
        raise OSError("conversation compact summary digest mismatches")


# LLM: v3精确引用必须完整，不修补缺失身份；旧版本引用由各自读取合同解释。
# 函数用途: 校验新检查点的消息与工具覆盖，供同链摘要视图共用。
def _validated_v3_sources(row: dict[str, object]) -> tuple[tuple[str, ...], tuple[dict[str, object], ...]]:
    message_ids = row.get("source_message_ids")
    tool_refs = row.get("source_tool_refs")
    if type(message_ids) is not list or type(tool_refs) is not list:
        raise OSError("conversation compact source refs are missing")
    if any(type(item) is not str or not item for item in message_ids):
        raise OSError("conversation compact source message ids are invalid")
    if len(set(message_ids)) != len(message_ids):
        raise OSError("conversation compact source message ids repeat")
    result: list[dict[str, object]] = []
    seen: set[tuple[str, str, str, str]] = set()
    required = {"run_id", "attempt_id", "turn_id", "call_id"}
    for item in tool_refs:
        if not isinstance(item, dict) or not required <= set(item) or set(item) - required - {"request_id"}:
            raise OSError("conversation compact source tool ref shape is invalid")
        if any(type(item[key]) is not str or not item[key] for key in required):
            raise OSError("conversation compact source tool identity is invalid")
        if "request_id" in item and type(item["request_id"]) is not str:
            raise OSError("conversation compact source request id is invalid")
        identity = tuple(str(item[key]) for key in ("run_id", "attempt_id", "turn_id", "call_id"))
        if identity in seen:
            raise OSError("conversation compact source tool refs repeat")
        seen.add(identity)
        result.append(dict(item))
    if not message_ids and not result:
        raise OSError("conversation compact checkpoint has no source coverage")
    return tuple(message_ids), tuple(result)


# LLM: 旧 v2 无 run/turn 原始身份，保留原调用和请求证据并标记 legacy，消费者不可精确匹配。
# 函数用途: 提取一条 checkpoint 自己的覆盖，不把后续提交误认为这条摘要的来源。
def _row_sources(row: dict[str, object]) -> tuple[tuple[str, ...], tuple[dict[str, object], ...], tuple[str, ...]]:
    schema = row.get("schema")
    if schema == "conversation_compact_checkpoint.v3":
        messages, tools = _validated_v3_sources(row)
        return messages, tools, ()
    if schema == "conversation_compact_checkpoint.v1":
        if row.get("source_kind") != "transcript":
            raise OSError("legacy transcript checkpoint source kind is invalid")
        end = row.get("source_end_message_id")
        if type(end) is not str or not end:
            raise OSError("legacy transcript checkpoint boundary is invalid")
        return (), (), (end,)
    if row.get("source_kind") != "live_tool_ir":
        raise OSError("legacy live checkpoint source kind is invalid")
    call_ids = row.get("source_tool_call_ids")
    if type(call_ids) is not list or any(type(item) is not str or not item for item in call_ids):
        raise OSError("legacy live checkpoint source calls are invalid")
    request_id, attempt_id = row.get("request_id"), row.get("attempt_id")
    if type(request_id) is not str or type(attempt_id) is not str:
        raise OSError("legacy live checkpoint provenance is invalid")
    return (), tuple({
        "legacy": True, "call_id": call_id,
        "request_id": request_id, "attempt_id": attempt_id,
    } for call_id in call_ids), ()


# LLM: 提交链验证由原 reader 负责；这里额外验证摘要 base 较旧且对当前摘要作用域适用。
# 函数用途: 构造摘要基础索引并拒绝跨范围、未来代次或缺失的基础引用。
def _indexed_rows(chain: tuple[dict[str, object], ...]) -> dict[str, dict[str, object]]:
    by_id = {str(row["checkpoint_id"]): row for row in chain}
    for row in chain:
        scope = _checkpoint_scope(row)
        _validate_summary(row)
        base_id = _summary_base_id(row)
        created_at = row.get("created_at")
        if type(created_at) not in {int, float} or not math.isfinite(created_at) or created_at <= 0:
            raise OSError("conversation compact checkpoint creation time is invalid")
        if row.get("schema") == "conversation_compact_checkpoint.v3":
            _validated_v3_sources(row)
        if not base_id:
            continue
        base = by_id.get(base_id)
        if base is None or int(base["generation"]) >= int(row["generation"]):
            raise OSError("conversation compact summary base is missing or newer")
        base_created = base.get("created_at")
        if (type(base_created) not in {int, float} or not math.isfinite(base_created)
                or not scope_can_inherit(scope, _checkpoint_scope(base), base_created)):
            raise OSError("conversation compact summary base crosses scope")
    return by_id


# LLM: 仅沿选中摘要的语义基础链累计覆盖；链中其他局部提交不属于该摘要。
# 函数用途: 只读解析当前请求实际适用的摘要与来源，空范围返回空视图。
def resolve_compact_summary_view(
    agent: SimpleAgent,
    thread: ConversationThread,
    scope: CompactScope,
) -> CompactSummaryView:
    if not isinstance(scope, CompactScope):
        raise TypeError("Compact 作用域类型无效")
    chain = committed_compact_checkpoint_chain(agent, thread)
    by_id = _indexed_rows(chain)
    selected = next((row for row in reversed(chain) if scope_can_inherit(
        scope, _checkpoint_scope(row), row.get("created_at"),
    )), None)
    if selected is None:
        return CompactSummaryView()
    messages: set[str] = set()
    tools: list[dict[str, object]] = []
    ends: list[str] = []
    seen_tools: set[tuple[object, ...]] = set()
    path: list[dict[str, object]] = []
    row: dict[str, object] | None = selected
    while row is not None:
        path.append(row)
        base_id = _summary_base_id(row)
        row = by_id.get(base_id) if base_id else None
    for item in reversed(path):
        item_messages, item_tools, item_ends = _row_sources(item)
        messages.update(item_messages)
        ends.extend(item_ends)
        for ref in item_tools:
            identity = (ref.get("legacy", False), ref.get("run_id"), ref.get("attempt_id"),
                        ref.get("turn_id"), ref.get("call_id"), ref.get("request_id"))
            if identity not in seen_tools:
                seen_tools.add(identity)
                tools.append(ref)
    evidence = selected.get("operation_evidence")
    if not isinstance(evidence, dict):
        raise OSError("conversation compact operation evidence is invalid")
    return CompactSummaryView(
        checkpoint_id=str(selected["checkpoint_id"]),
        summary=str(selected["summary"]),
        operation_evidence=dict(evidence),
        source_message_ids=frozenset(messages),
        source_tool_refs=tuple(tools),
        legacy_message_end_ids=tuple(ends),
        generation=int(selected["generation"]),
    )


__all__ = ["CompactSummaryView", "resolve_compact_summary_view"]
