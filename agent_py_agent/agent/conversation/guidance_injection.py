# LLM: 这里只维护原live_archive_state中本次已注入guidance ID的集合；不读取持久账、不确认ACK、不决定调度或权限，runtime与Gateway必须共用此实现。
# 模块用途: 让模型安全点和输入观察使用同一份已注入状态，避免Gateway反向依赖执行循环。
from typing import Any


# LLM: 纯读原参数的已注入集合；未提交/未ACK与未注入不同，调用方仍须沿原reservation和batch合同判断。
# 函数用途: 从同一次输入条目中筛出尚未放入模型上下文的项目，不修改条目或状态。
def guidance_not_yet_injected(params: object, entries: list[Any]) -> list[Any]:
    state = getattr(params, "live_archive_state", None)
    seen = state.get("_injected_guidance_ids") if isinstance(state, dict) else None
    seen_ids = seen if isinstance(seen, set) else set()
    return [
        entry
        for entry in entries
        if str(getattr(entry, "guidance_id", "") or "") not in seen_ids
    ]


# LLM: 只写原参数内存集合，不写投递账或ACK；同一guidance_id仍由原canonical/批次谱系负责幂等和确认。
# 函数用途: 记录实际注入的条目，供后续安全点与输入观察共同去重。
def remember_injected_guidance(params: object, entries: list[Any]) -> None:
    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict):
        return
    seen = state.get("_injected_guidance_ids")
    if not isinstance(seen, set):
        seen = set()
        state["_injected_guidance_ids"] = seen
    seen.update(
        str(getattr(entry, "guidance_id", "") or "")
        for entry in entries
        if str(getattr(entry, "guidance_id", "") or "")
    )
