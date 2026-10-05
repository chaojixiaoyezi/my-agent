# LLM: memory_search 是主模型的只读长期记忆检索工具（P5-A 缺口 2，开关 enable_memory_search_tool，默认关）。
#   只调记忆模块的 search_scoped_candidates_report：与自动召回同一混合检索、同一 scope 规则（runtime_long_term_scope），
#   不登记访问信号、不写/删/改正式条目、不进自动召回（不改 memory 注入与 memory_refs）。结果只是线索；
#   检索方式（语义/关键词）原样取自检索层的结构化事实，不由本模块推断。改动时同步 test_memory_search_tool.py
#   与 DECISION_MODEL_INTEGRATION.md 的“P5-A 缺口 2”节。
# 模块用途: 让主模型在自动召回漏掉事实时，自己按当前 owner 与范围查一次正式长期记忆，拿到有界的条目摘录。
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..memory_store.recall import (
    formal_recall_suppressed,
    long_term_record_matches_scope,
    runtime_long_term_scope,
)
from ..tooling.models import (
    BaseTool,
    ConcurrencyPolicy,
    EffectResolverPolicy,
    ToolAvailability,
    ToolHandlerOutcome,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)

if TYPE_CHECKING:
    from ..core import SimpleAgent

# 不传 limit 时返回几条。
MEMORY_SEARCH_DEFAULT_COUNT = 5
# 一次最多返回几条（schema 上限与执行时截断共用）。
MEMORY_SEARCH_MAX_COUNT = 10
# 每条正文摘录最多多少字；超出标 excerpt_truncated。
MEMORY_SEARCH_EXCERPT_CHARS = 300


# LLM: schema 只开放 query/limit/kind 三个字段且 additionalProperties=false；写类参数由工具运行时在执行前拒绝。
#   说明里写清“只是线索、不改自动召回、要改记忆用 remember”，但这些只是给模型的软说明，机器边界在实现里。
# 函数用途: 生成 memory_search 的工具说明书（进工具目录）。
def build_memory_search_model_spec() -> ToolModelSpec:
    return ToolModelSpec(
        name="memory_search",
        description=(
            "只读检索当前用户自己的正式长期记忆（事实、事件、项目知识），按本轮适用范围过滤，"
            "返回条目编号、类型、范围、更新时间和有界正文摘录，并说明这次走的是语义检索还是关键词检索。"
            "自动召回漏掉、需要核对某个记住过的事实时用它。结果只是线索：不会改变自动召回，也不能写、删、改记忆；"
            "要修改记忆请用 remember。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "description": "检索文本，写要找的事实的关键词或一句话。"},
                "limit": {"type": "integer", "minimum": 1, "maximum": MEMORY_SEARCH_MAX_COUNT,
                          "description": f"最多返回几条，默认 {MEMORY_SEARCH_DEFAULT_COUNT}。"},
                "kind": {"type": "string", "description": "可选，只看某类条目，常见 fact、event、project。"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="capability",
            use_cases=(
                "用户问起以前让你记住的事实，而上下文里的记忆没有提到",
                "回答前想核对某条长期记忆的原文、范围或更新时间",
            ),
            avoid_when=("要新增、修改或删除记忆时用 remember", "查聊天历史原话用 session_search"),
            keywords=("记忆", "长期记忆", "记住过", "memory", "recall", "查记忆"),
            examples=(
                '{"tool":"memory_search","query":"图书借阅服务端口"}',
                '{"tool":"memory_search","query":"moneywise 时区","kind":"project","limit":3}',
            ),
            default_deferred=True,
            deferred_summary="按关键词检索自己的长期记忆（自动召回漏掉时补查）",
        ),
    )


# LLM: 效果声明 read_only、可并行；availability 与自动召回同一抑制判定（formal_recall_suppressed），
#   子代理 task_local / control_plane 回合与 owner 记忆总闸关闭时不可用。
# 类用途: 主模型的只读长期记忆检索工具，只在 enable_memory_search_tool 开着时由 core 注册。
class MemorySearchTool(BaseTool):
    model_spec = build_memory_search_model_spec()
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("read_only"),
        concurrency_policy=ConcurrencyPolicy("parallel_safe"),
    )

    # LLM: 只绑定当前 owner 的 agent；记忆仓库在执行时从 agent.memory 取，不缓存、不建备用实例。
    # 函数用途: 初始化工具，记下所属 agent。
    def __init__(self, agent: SimpleAgent):
        self.agent = agent

    # LLM: 只读结构化 context_scope 与 owner_policy；判定与 loop_support 的 recall_suppressed 同源。
    # 函数用途: 自动召回被抑制的回合里报工具不可用。
    def availability(self) -> ToolAvailability:
        current = getattr(self.agent, "_current_run_params", None)
        if formal_recall_suppressed(getattr(current, "context_scope", ""), getattr(self.agent, "owner_policy", None)):
            return ToolAvailability.unavailable("本轮不读正式长期记忆（隔离回合或 owner 已关闭记忆）")
        return ToolAvailability.ready()

    # LLM: 一次调用恰好一次检索；范围取本轮 task_id/task_attributes（宿主结构化字段），kind 只做精确过滤。
    #   无副作用：不写访问信号、不改正式条目；检索失败给结构化错误码，不逃逸。
    # 函数用途: 按查询文本在当前 owner 的适用范围内检索正式长期记忆，返回有界摘录和检索方式。
    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        memory = getattr(self.agent, "memory", None)
        search = getattr(memory, "search_scoped_candidates_report", None)
        if not callable(search):
            return _error("正式长期记忆不可用", "TOOL_UNAVAILABLE")
        query = str(params.get("query") or "").strip()
        if not query:
            return _error("query 不能为空", "TOOL_INVALID_ARGUMENTS")
        predicate = _scope_predicate(self.agent, memory, str(params.get("kind") or "").strip())
        try:
            records, retrieval = search(query, _bounded_limit(params.get("limit")), predicate)
        except (OSError, ValueError, RuntimeError) as exc:
            return _error(f"记忆检索失败: {type(exc).__name__}", "TOOL_EXECUTION_FAILED")
        entries = [_entry_payload(record) for record in records]
        payload = {
            "ok": True,
            "authority": "clue_only",
            "note": "只读线索：不改变自动召回，不写、删、改记忆；要修改请用 remember。",
            "retrieval": retrieval,
            "count": len(entries),
            "entries": entries,
        }
        return ToolHandlerOutcome("memory_search", True, json.dumps(payload, ensure_ascii=False))


# LLM: 范围与自动召回同一规则（runtime_long_term_scope）；kind 非空时只留 kind 完全相同的条目，不认别名。
# 函数用途: 生成本次检索的过滤条件（当前 owner 记忆 + 本轮适用范围 + 可选类型）。
def _scope_predicate(agent: object, memory: object, kind: str):
    current = getattr(agent, "_current_run_params", None)
    attributes = getattr(current, "task_attributes", None)
    scope = runtime_long_term_scope(
        memory,
        task_id=str(getattr(current, "task_id", "") or ""),
        task_attributes=attributes if isinstance(attributes, dict) else {},
    )
    return lambda record: long_term_record_matches_scope(record, scope) and (not kind or record.kind == kind)


# LLM: schema 已限 1..MEMORY_SEARCH_MAX_COUNT；这里再收一次，保证直接调用处理函数时条数也有界。
# 函数用途: 把模型给的条数收进 1..MEMORY_SEARCH_MAX_COUNT，缺省或非法时用默认值。
def _bounded_limit(value: object) -> int:
    try:
        number = int(value) if value is not None else MEMORY_SEARCH_DEFAULT_COUNT
    except (TypeError, ValueError):
        number = MEMORY_SEARCH_DEFAULT_COUNT
    return max(1, min(MEMORY_SEARCH_MAX_COUNT, number))


# LLM: 只投影结构化字段与有界摘录，不带候选、访问时间或内部来源；updated_at 缺失时退回 created_at。
# 函数用途: 把一条正式长期记忆转成工具结果里的一项。
def _entry_payload(record: object) -> dict[str, object]:
    attributes = getattr(record, "attributes", None)
    attributes = attributes if isinstance(attributes, dict) else {}
    content = str(getattr(record, "content", "") or "")
    stamp = float(getattr(record, "updated_at", 0.0) or getattr(record, "created_at", 0.0) or 0.0)
    return {
        "entry_id": str(getattr(record, "entry_id", "") or ""),
        "version": int(getattr(record, "version", 1) or 1),
        "kind": str(getattr(record, "kind", "") or ""),
        "scope": {"scope_type": str(attributes.get("scope_type") or ""),
                  "scope_key": str(attributes.get("scope_key") or "")},
        "subject_key": str(attributes.get("subject_key") or ""),
        "updated_at": datetime.fromtimestamp(stamp, timezone.utc).isoformat() if stamp > 0 else "",
        "excerpt": content[:MEMORY_SEARCH_EXCERPT_CHARS],
        "content_chars": len(content),
        "excerpt_truncated": len(content) > MEMORY_SEARCH_EXCERPT_CHARS,
    }


# LLM: 失败都发生在检索前或检索本身（只读），因此标 not_started，让模型可修正后重试。
# 函数用途: 统一构造 memory_search 的失败返回（带明确 error_code，未产生副作用）。
def _error(message: str, code: str) -> ToolHandlerOutcome:
    return ToolHandlerOutcome(
        "memory_search",
        False,
        json.dumps({"error": message}, ensure_ascii=False),
        error_code=code,
        effect_outcome="not_started",
    )


__all__ = ["MemorySearchTool", "build_memory_search_model_spec"]
