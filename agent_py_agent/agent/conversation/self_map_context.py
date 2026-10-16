# LLM: 只取当前 exact thread 的数值投影，不重新估算或读取压缩/聊天正文；旧代或模型换代保持未知。
# 模块用途: 登记上下文和 Compact 的只读元数据。
from ..tooling.self_map_registry import SectionDescriptor, SectionProjection, map_item
from .context_usage import context_usage_from_thread


# LLM: context声明为状态卡而非库存目录；固定用途不读取会话或估算运行状态。
# 函数用途: 接入上下文分块。
def register_context_section(registry):
    registry.register(SectionDescriptor("context", "上下文", 120, context_projection, "当前上下文估算与Compact代次", list_mode=False))


# LLM: load_report 不创建或更新线程，agent_thread_id 已在上下文优先解析；缺载入不能回退父线程。
# 函数用途: 返回最近 preflight 估算和精确压缩代。
def context_projection(context):
    store = context.agent.conversation_store
    thread, error = store.threads.load_report(context.thread_id) if context.thread_id else (None, None)
    usage = context_usage_from_thread(thread) if thread else {}
    if usage and thread.model_context_usage.get("model_selection_revision", 0) != thread.model_selection_revision:
        usage = {}
    if usage and usage.get("model_name") != getattr(context.agent.config, "model_name", ""):
        usage = {}
    state = "unavailable" if error else "known" if usage else "not_observed"
    item = map_item("current", "当前上下文", state=state, fields={"usage": usage or None,
                    "compact_generation": getattr(thread, "compact_generation", None),
                    "has_checkpoint": bool(getattr(thread, "compact_checkpoint_id", "")) if thread else None,
                    "observation_point": "last_model_preflight_estimate"})
    return SectionProjection((item,), "partial" if error else "complete", state,
                             errors=("SELF_MAP_THREAD_UNAVAILABLE",) if error else (), source="context_usage_from_thread")
