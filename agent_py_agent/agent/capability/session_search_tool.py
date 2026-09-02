# LLM: session_search 模型工具(对标 长期助手 tools/session_search_tool.py 三模式)。
#   底子早就现成:LocalStore 已有 FTS5 全文检索(search)、最近列表(list_recent)、
#   按锚时间窗(records_around)、records/timeline——只是从没暴露成模型可调用的工具。
#   本工具把这些封装成单一工具、三种调用形态(由参数推断,无显式 mode 参数),
#   零 LLM 成本、纯读 SQLite/FTS5:
#     ① DISCOVERY  —— 传 query:trigram FTS5 全文检索历史记录,每条带 snippet。
#     ② SCROLL     —— 传 around_id:以某条记录为锚,按时间序取前后窗口(无 FTS)。
#     ③ BROWSE     —— 不传参:按时间倒序列出最近记录(标题/预览/时间)。
#   与 长期助手 的差异:my-agent 的事实单元是 record(非逐条 message),没有
#   session 血缘/连续 message-id,故 scroll 用 updated_at 时间序当滚动轴、不做
#   血缘去重/rebind;模式名(discover/scroll/browse)与返回字段(snippet/window/
#   messages_before/after/count)对齐 长期助手 语义。Gateway 记录额外投影 host-authored
#   task_ref，供“回到上次项目”拿到精确路径，不从自然语言猜 cwd。契约:只读零副作用;
#   空库/无命中返回结构化提示不报错；给精确 input_schema（对齐原生 tool_use 改造规范）。
# 模块用途: 让模型能检索/翻看本地历史记录(普通聊天、记忆、任务产物、归档),回答"我们之前
#   对 X 怎么处理的/在哪聊过 Y",而不用把全部历史塞进 prompt。
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from ..tooling.models import (
    BaseTool,
    ConcurrencyPolicy,
    EffectResolverPolicy,
    ResourceScopePolicy,
    ToolHandlerOutcome,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)

if TYPE_CHECKING:
    from ..core import SimpleAgent
    from ..local_storage import LocalSearchResult, LocalStore

_SNIPPET_CHARS = 240
_PREVIEW_CHARS = 160
_DEFAULT_DISCOVER_LIMIT = 5
_DEFAULT_BROWSE_LIMIT = 10
_DEFAULT_WINDOW = 5
_MAX_LIMIT = 20
_MAX_WINDOW = 20


# LLM: Tool instructions may explain how to consume typed task_ref, but must never infer a path
# from natural-language history or turn a search hit into write authorization.
# 函数用途: session_search 的工具说明书(进工具目录,引导模型先查历史再上网/翻盘)。
def build_session_search_model_spec() -> ToolModelSpec:
    return ToolModelSpec(
        name="session_search",
        description=(
            "检索/翻看当前用户自己的本地历史记录(普通聊天、记忆、任务产物、归档),零成本纯读。"
            "三种形态由参数推断:"
            "①传 query=全文检索(FTS5,支持中文子串);②传 around_id=以某条记录为锚翻看前后上下文;"
            "③都不传=按时间倒序列出最近记录。回答'我们之前对X怎么处理/在哪记过Y'优先用它,先于上网/翻文件。"
            "Gateway 任务命中可能带 task_ref；其中 task_path 是该历史任务的精确目录，用户要求续作时优先使用。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "全文检索词；留空则浏览最近记录。"},
                "around_id": {"type": "string", "description": "记录 ID；以它为锚返回前后窗口，优先于 query。"},
                "window": {"type": "integer", "minimum": 1, "maximum": _MAX_WINDOW, "description": "锚点两侧各取几条，默认 5。"},
                "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_LIMIT, "description": "检索或浏览返回条数。"},
                "source_type": {"type": "string", "description": "限定来源类型，如 memory。"},
            },
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="capability",
            use_cases=(
                "用户问之前怎么解决或上次聊到哪时先检索历史",
                "拿到一条命中后用 around_id 翻看前后上下文",
                "用户问最近在做什么时浏览最近记录",
            ),
            avoid_when=("当前世界状态要使用对应实时工具", "已经知道确切答案时无需翻历史"),
            keywords=("历史", "检索", "回顾", "之前", "上次", "记录", "session", "history", "search", "recall", "翻看", "找一下", "查一下"),
            examples=(
                '{"tool":"session_search","query":"记忆推送模式怎么落地的"}',
                '{"tool":"session_search","around_id":"rec-abc123","window":8}',
                '{"tool":"session_search"}',
            ),
        ),
    )


class SessionSearchTool(BaseTool):
    model_spec = build_session_search_model_spec()
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("read_only"),
        concurrency_policy=ConcurrencyPolicy("parallel_safe"),
        resource_scopes=ResourceScopePolicy(parameter_names=("around_id", "query"),
            parameter_kinds={"around_id": "logical", "query": "logical"}),
    )

    # 类用途: 把 LocalStore 的历史检索/翻看暴露成模型可调用的只读工具(三模式)。
    def __init__(self, agent: SimpleAgent):
        self.agent = agent

    # 函数用途: 校验 store 后把三模式分派交给 _dispatch;store 不可用/检索异常都给结构化码。
    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        store = _store_for(self.agent)
        if store is None:
            return _error("本地历史存储不可用", "当前运行环境未配置 local_store", "TOOL_UNAVAILABLE")
        try:
            payload = _dispatch(store, params)
        except Exception as exc:  # noqa: BLE001 — 检索失败要给明确可重试码,不逃逸成 UNKNOWN_ERROR
            return _error(f"历史检索失败: {exc}", "可原样重试一次", "TOOL_EXECUTION_FAILED")
        return ToolHandlerOutcome("session_search", True, json.dumps(payload, ensure_ascii=False))


# 函数用途: 按参数推断并执行三模式(scroll 优先 > discovery > browse)。
def _dispatch(store: LocalStore, params: dict[str, object]) -> dict[str, Any]:
    source_type = _clean_str(params.get("source_type")) or None
    around_id = _clean_str(params.get("around_id"))
    query = _clean_str(params.get("query"))
    # scroll 优先:显式锚点压过 query(模型明确要翻看某条的上下文)。
    if around_id:
        window = _safe_int(params.get("window"), _DEFAULT_WINDOW, _MAX_WINDOW)
        return _scroll(store, around_id, window, source_type)
    if query:
        limit = _safe_int(params.get("limit"), _DEFAULT_DISCOVER_LIMIT, _MAX_LIMIT)
        return _discover(store, query, limit, source_type)
    return _browse(store, _safe_int(params.get("limit"), _DEFAULT_BROWSE_LIMIT, _MAX_LIMIT), source_type)


# 函数用途: 统一构造 session_search 的失败返回(带明确 error_code)。
def _error(message: str, hint: str, code: str) -> ToolHandlerOutcome:
    return ToolHandlerOutcome(
        "session_search",
        False,
        json.dumps({"error": message, "hint": hint}, ensure_ascii=False),
        error_code=code,
    )


# 函数用途: discovery 形态——FTS5 全文检索,每条命中带 snippet 与定位 id(可继续 scroll)。
def _discover(store: LocalStore, query: str, limit: int, source_type: str | None) -> dict[str, Any]:
    hits = store.search(query, limit=limit, source_type=source_type)
    results = [_shape_hit(hit, snippet_for=query) for hit in hits]
    return {
        "mode": "discover",
        "query": query,
        "results": results,
        "count": len(results),
        "hint": (
            "用某条结果的 around_id 翻看它前后的上下文,或换关键词。"
            if results
            else "没有命中的历史记录;可换关键词,或不传参浏览最近记录。"
        ),
    }


# 函数用途: scroll 形态——以 around_id 为锚,按时间序返回前后窗口(无 FTS,纯翻看)。
def _scroll(store: LocalStore, around_id: str, window: int, source_type: str | None) -> dict[str, Any]:
    records, before, after = store.records_around(around_id, window=window, source_type=source_type)
    if not records:
        return {
            "mode": "scroll",
            "around_id": around_id,
            "messages": [],
            "count": 0,
            "hint": f"未找到记录 id={around_id};它可能不存在或不属于该 source_type。",
        }
    return {
        "mode": "scroll",
        "around_id": around_id,
        "window": window,
        "messages": [_shape_record(rec, anchor_id=around_id) for rec in records],
        "count": len(records),
        "messages_before": before,
        "messages_after": after,
        "hint": (
            "向后翻:用最后一条的 id 当 around_id 再调;向前翻:用第一条的 id。"
            " before/after 小于 window 说明已到历史的一端。"
        ),
    }


# 函数用途: browse 形态——不带话题时按时间倒序列出最近记录(标题/预览/时间)。
def _browse(store: LocalStore, limit: int, source_type: str | None) -> dict[str, Any]:
    recents = store.list_recent(limit=limit, source_type=source_type)
    results = [_shape_record(rec, preview=True) for rec in recents]
    return {
        "mode": "browse",
        "results": results,
        "count": len(results),
        "hint": (
            f"最近 {len(results)} 条记录。传 query= 检索具体话题,或 around_id= 翻看某条上下文。"
            if results
            else "本地还没有历史记录。"
        ),
    }


# LLM: Search hits may expose a typed task_ref only for host-indexed gateway_request rows; never
# derive it from snippets, titles, or arbitrary memory metadata.
# 函数用途: 把一条 FTS 命中整形成发现结果，并附上可用的精确历史任务引用。
def _shape_hit(hit: LocalSearchResult, *, snippet_for: str) -> dict[str, Any]:
    entry = {
        "around_id": hit.id,
        "title": hit.title,
        "source_type": hit.source_type,
        "source_id": hit.source_id,
        "snippet": _snippet(hit.content, snippet_for),
        "when": _round_ts(hit.updated_at),
        "score": round(hit.score, 3),
        "scope": _history_scope(hit.metadata),
    }
    task_ref = _history_task_ref(hit.metadata, source_type=hit.source_type)
    if task_ref:
        entry["task_ref"] = task_ref
    return entry


# LLM: Scroll/browse must use the same gateway task-ref projection as discovery so navigation
# mode cannot silently discard an exact continuation path.
# 函数用途: 把一条记录整形成 scroll/browse 条目，并保留可用的历史任务引用。
def _shape_record(rec: LocalSearchResult, *, anchor_id: str | None = None, preview: bool = False) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": rec.id,
        "title": rec.title,
        "source_type": rec.source_type,
        "when": _round_ts(rec.updated_at),
        "scope": _history_scope(rec.metadata),
    }
    body = rec.content or ""
    entry["preview" if preview else "content"] = body[:_PREVIEW_CHARS] if preview else body
    task_ref = _history_task_ref(rec.metadata, source_type=rec.source_type)
    if task_ref:
        entry["task_ref"] = task_ref
    if anchor_id is not None and rec.id == anchor_id:
        entry["anchor"] = True
    return entry


# LLM: Search projections must preserve typed memory scope from the nested canonical attributes.
# Physical owner isolation is already enforced by the selected LocalStore; this metadata only
# lets the model distinguish current-session memory from cross-session personal memory.
# 函数用途: 从历史索引元数据提取会话定位和记忆召回范围，避免检索命中被模型误判成全局记忆。
def _history_scope(metadata: object) -> dict[str, str]:
    if not isinstance(metadata, dict):
        return {}
    allowed = ("thread_id", "message_id", "role", "channel")
    result = {
        key: str(metadata.get(key) or "")
        for key in allowed
        if str(metadata.get(key) or "").strip()
    }
    attributes = metadata.get("attributes")
    if isinstance(attributes, dict):
        for key in ("scope_type", "scope_key", "applies_when", "excludes_when"):
            value = str(attributes.get(key) or "").strip()
            if value:
                result[key] = value
    return result


# LLM: conversation_runtime is a Gateway index projection of canonical host state. Requiring the
# gateway_request source and an absolute task_path prevents arbitrary chat/memory rows from posing
# as executable cwd authority; the write tool still performs its own exact-path rebind checks.
# 函数用途: 从 Gateway 历史元数据提取精确任务目录，供模型按用户要求继续旧项目。
def _history_task_ref(
    metadata: object,
    *,
    source_type: str,
) -> dict[str, str]:
    if source_type != "gateway_request" or not isinstance(metadata, dict):
        return {}
    runtime = metadata.get("conversation_runtime")
    if not isinstance(runtime, dict):
        return {}
    task_id = str(runtime.get("task_id") or "").strip()
    task_path = str(runtime.get("task_path") or "").strip()
    if not task_id or not task_path.startswith("/"):
        return {}
    result = {
        "task_id": task_id,
        "task_path": task_path,
    }
    for key in ("request_id", "thread_id"):
        value = str(runtime.get(key) or "").strip()
        if value:
            result[key] = value
    status = str(metadata.get("status") or "").strip()
    if status:
        result["status"] = status
    return result


# 函数用途: 截取围绕首个 query 词的正文片段(命中不到就取开头),控制 payload 体积。
def _snippet(content: str, query: str) -> str:
    if not content:
        return ""
    needle = _first_cjk_or_word(query)
    lowered = content.lower()
    pos = lowered.find(needle.lower()) if needle else -1
    if pos < 0:
        return content[:_SNIPPET_CHARS].strip()
    start = max(0, pos - _SNIPPET_CHARS // 3)
    end = min(len(content), pos + (_SNIPPET_CHARS * 2) // 3)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(content) else ""
    return f"{prefix}{content[start:end].strip()}{suffix}"


def _first_cjk_or_word(query: str) -> str:
    import re

    match = re.search(r"[一-鿿]+|[\w]+", query)
    return match.group(0) if match else ""


def _round_ts(ts: float) -> float:
    try:
        return round(float(ts), 3)
    except (TypeError, ValueError):
        return 0.0


# 函数用途: 取 agent 上挂的 LocalStore(历史事实源);没有则返回 None(工具优雅降级)。
def _store_for(agent) -> LocalStore | None:
    store = getattr(agent, "local_store", None)
    if store is None:
        return None
    # 鸭子类型校验:必须具备本工具依赖的三个只读检索入口。
    if all(hasattr(store, name) for name in ("search", "list_recent", "records_around")):
        return store
    return None


def _clean_str(value: object) -> str:
    return str(value or "").strip()


def _safe_int(value: object, default: int, ceiling: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, ceiling))


__all__ = ["SessionSearchTool", "build_session_search_model_spec"]
