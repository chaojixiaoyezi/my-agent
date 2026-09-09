# LLM: 工具发现状态只从结构化工具结果恢复；主、子、Gateway与Compact共用，不导入执行编排层或改变授权。
# 模块用途: 找回下一次模型调用仍需要的临时工具名称，避免恢复时丢工具或重新暴露旧轮已消费的工具。

from __future__ import annotations


# LLM: 仅最新已知工具轮的typed tool_search信封能恢复临时schema；旧无轮号记录只允许精确尾项，保持原缓存布局。
# 函数用途: 读取携带归档中的未消费工具选择；纯内存投影，不写账、派工或调用模型。
def pending_carried_loaded_tool_names(records: list[dict[str, object]]) -> set[str]:
    rounded = [
        (round_no, record)
        for record in records
        if (round_no := _carried_tool_round(record)) is not None
    ]
    if rounded:
        latest_round = max(round_no for round_no, _ in rounded)
        return {
            name
            for round_no, record in rounded
            if round_no == latest_round
            for name in _carried_loaded_tool_names(record)
        }
    return _carried_loaded_tool_names(records[-1]) if records else set()


# LLM: 轮号只取归档的结构化tool_round，不从正文或位置推导；无效值保持未知，无副作用。
# 函数用途: 统一新旧归档的工具轮读取。
def _carried_tool_round(record: dict[str, object]) -> int | None:
    try:
        value = int(record.get("tool_round"))
    except (TypeError, ValueError):
        return None
    return max(0, value)


# LLM: 只消费tool_result_envelope.tool_search.loaded_tool_names，不解析用户或模型文本，也不授予工具执行权限。
# 函数用途: 从工具搜索的正式结果信封取出名称集合；损坏/缺失信封返回空集合。
def _carried_loaded_tool_names(record: dict[str, object]) -> set[str]:
    envelope = record.get("tool_result_envelope")
    if not isinstance(envelope, dict):
        return set()
    search = envelope.get("tool_search")
    if not isinstance(search, dict):
        return set()
    names = search.get("loaded_tool_names")
    if not isinstance(names, list):
        return set()
    return {str(item).strip() for item in names if str(item).strip()}
