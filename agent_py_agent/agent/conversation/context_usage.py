# LLM: 这里只持久/投影模型 preflight 的数值；不参与预算、计费、完成或恢复决策，主/子共用 exact thread。
# 模块用途: 让最近上下文数字在空闲和重连后仍可见，成功压缩后不再显示旧代快照。
from __future__ import annotations

import logging
from collections.abc import Mapping

_LOGGER = logging.getLogger(__name__)
_SCHEMA = "model_visible_context_usage.v1"
_TOKEN_FIELDS = (
    "context_window_tokens", "compact_trigger_tokens", "current_tokens", "prompt_tokens",
    "messages_tokens", "runtime_guidance_tokens", "tool_schema_tokens",
)


# LLM: 计数字段不接受布尔值或无限值；展示无效数按零处理，不把任意正文带入持久快照。
# 函数用途: 清洗一个上下文数值，不将坏数据传播到 TUI。
def _count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


# LLM: 该白名单供持久化和主/子公开投影共用；原始 prompt、工具参数和未知字段不保留。
# 函数用途: 复制已有 preflight 快照中的合法数字，不重新估算模型上下文。
def public_context_usage(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or value.get("schema") != _SCHEMA:
        return {}
    protocol = value.get("protocol")
    return {
        "schema": _SCHEMA,
        "estimated": value.get("estimated") is True,
        **{key: _count(value.get(key)) for key in _TOKEN_FIELDS},
        "protocol": protocol if protocol in ("native", "text") else "unknown",
    }


# LLM: 只读已加载的 exact thread，旧代或缺失记录保持未知；不回读 child 属性、累计账本或校准值。
# 函数用途: 取得该代理最近一次调用前的上下文数字，供空闲/运行/恢复页面共用。
def context_usage_from_thread(thread: object) -> dict[str, object]:
    value = getattr(thread, "model_context_usage", None)
    if not isinstance(value, Mapping) or type(value.get("compact_generation")) is not int:
        return {}
    if value["compact_generation"] != getattr(thread, "compact_generation", 0):
        return {}
    return public_context_usage(value)


# LLM: 宿主 agent_thread_id 优先于 conversation_thread_id，隔离表达轮不写；best-effort 遥测失败不得中断模型。
# 函数用途: 调用模型前将同一数值快照保存到主或子代理自己的会话，不写聊天历史或更新活跃时间。
def record_model_context_usage(agent: object, params: object, usage: object) -> bool:
    if str(getattr(params, "context_scope", "") or "").strip().lower() == "isolated":
        return False
    attrs = getattr(params, "task_attributes", None)
    attrs = attrs if isinstance(attrs, Mapping) else {}
    thread_id = str(attrs.get("agent_thread_id") or attrs.get("conversation_thread_id") or "").strip()
    store = getattr(agent, "conversation_store", None)
    loader = getattr(store, "load_thread_report", None)
    updater = getattr(store, "update_model_context_usage", None)
    public = public_context_usage(usage)
    if not thread_id or not public or not callable(loader) or not callable(updater):
        return False
    try:
        thread, error = loader(thread_id)
        if error is not None or thread is None:
            return False
        generation = thread.compact_generation
        updated = updater(thread_id, public, expected_compact_generation=generation)
        return updated.compact_generation == generation and context_usage_from_thread(updated) == public
    except (OSError, RuntimeError, TypeError, ValueError):
        _LOGGER.warning("context usage telemetry could not be saved", exc_info=False)
        return False
