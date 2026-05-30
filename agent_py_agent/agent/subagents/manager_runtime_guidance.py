# LLM: Runtime guidance handoff keeps subagent context building from growing extra ledger code.
# 模块用途: 从长期会话 guidance 账本取出点名给当前 runner 的软提示，并放入 context reserved。

from __future__ import annotations


# LLM: runtime_guidance_context drains only the target runner's pending soft guidance.
# 函数用途: 子代理执行上下文构建时读取点名给当前 run 的补充提示，并标记已投递。
def runtime_guidance_context(manager: object, run_id: str) -> list[dict[str, object]]:
    store = getattr(manager, "conversation_store", None)
    if store is None:
        return []
    entries = store.pending_guidance("agent_run", run_id, limit=20)
    if not entries:
        return []
    store.mark_guidance_delivered([entry.guidance_id for entry in entries])
    return [entry.to_dict() for entry in entries]


# LLM: attach_runtime_guidance stores guidance under reserved so context bundle schema stays stable.
# 函数用途: 把 runtime guidance 放入 context_bundle.reserved，避免新增顶层字段破坏旧 reader。
def attach_runtime_guidance(bundle: dict[str, object], guidance: list[dict[str, object]]) -> None:
    if not guidance:
        return
    reserved = bundle.get("reserved")
    if not isinstance(reserved, dict):
        reserved = {}
    reserved["runtime_guidance"] = guidance
    bundle["reserved"] = reserved
