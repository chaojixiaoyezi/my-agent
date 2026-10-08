# LLM: 工具发现状态只从结构化工具结果恢复；主、子、Gateway与Compact共用，不导入执行编排层或改变授权。
#   10-08（前缀纪律）：线程内已加载的工具只增不减。此前只保留最新一轮加载的名字，下一回合工具表就缩回去，
#   工具表一变整段缓存前缀失效（.16 真机：加载/撤回各一次全量未命中）；现在同一线程里加载过的工具一直可见，
#   工具表只会追加，压缩面（gateway_compact_context）与主请求走同一条规则。
# 模块用途: 找回本线程已加载、下一次模型调用仍要可见的临时工具名称，避免恢复时丢工具或工具表来回抖动。

from __future__ import annotations


# LLM: 带轮号的 typed tool_search 信封按线程累计（并集，只增不减）；旧无轮号记录只允许精确尾项，保持原缓存布局。
# 函数用途: 读取携带归档中本线程已加载的工具名；纯内存投影，不写账、派工或调用模型。
def pending_carried_loaded_tool_names(records: list[dict[str, object]]) -> set[str]:
    rounded = [record for record in records if _carried_tool_round(record) is not None]
    if rounded:
        return {name for record in rounded for name in _carried_loaded_tool_names(record)}
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
