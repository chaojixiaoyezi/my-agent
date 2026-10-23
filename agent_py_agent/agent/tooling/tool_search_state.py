# LLM: 工具发现状态只从结构化搜索/凭据工具结果恢复；主、子、Gateway与Compact共用，不导入执行编排层或改变授权。
#   发现历史按首次发现顺序只增不减；活跃集只供显式任务/Compact 边界选择，不在普通轮内自动撤回。
# 模块用途: 找回本线程累计工具名称，并在凭据回执后展示 web_fetch，避免恢复时丢工具或工具表来回抖动。

from __future__ import annotations

from collections.abc import Collection, Sequence


# LLM: 新序列保留首次发现顺序；仅旧 set 输入用稳定排序兜底，不伪称能从集合恢复发现时间。
# 函数用途: 去掉无效名和重复名，得到不可变有序名称序列。
def ordered_tool_names(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    names = [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return tuple(dict.fromkeys(sorted(names) if isinstance(value, (set, frozenset)) else names))


# LLM: 只读循环有序状态及原快照；已从loaded集合移除的名字不会被历史恢复，缺序列时才稳定排序旧set。
# 函数用途: 在冻结循环参数的原可变状态中读取发现顺序，旧set只作确定性兜底。
def ordered_loop_loaded_tool_names(params: object) -> tuple[str, ...]:
    current = ordered_tool_names(getattr(params, "loaded_tool_names", ()))
    state = getattr(params, "live_archive_state", {})
    order = ordered_tool_names(state.get("loaded_tool_order", ())) if isinstance(state, dict) else ()
    snapshot = getattr(params, "tool_runtime_snapshot", None)
    seed = ordered_tool_names(getattr(snapshot, "loaded_tool_names", ()))
    return tuple(name for name in ordered_tool_names([*order, *seed, *current]) if name in current)


# LLM: 带轮号记录按原顺序累计，无轮号只读精确尾项；纯展示事实不改变工具权限或写线程。
# 函数用途: 恢复去重但不排序的真实工具发现序列。
def ordered_carried_loaded_tool_names(records: list[dict[str, object]]) -> tuple[str, ...]:
    rounded = [record for record in records if _carried_tool_round(record) is not None]
    selected = rounded if rounded else records[-1:]
    return ordered_tool_names([name for record in selected for name in _ordered_carried_names(record)])


# LLM: 保留旧循环依赖的 set 读口；需要前缀顺序的新调用方使用 ordered_carried_loaded_tool_names。
# 函数用途: 兼容读取本线程累计加载名集合，不写账或改变授权。
def pending_carried_loaded_tool_names(records: list[dict[str, object]]) -> set[str]:
    return set(ordered_carried_loaded_tool_names(records))


# LLM: 轮号只取归档的结构化tool_round，不从正文或位置推导；无效值保持未知，无副作用。
# 函数用途: 统一新旧归档的工具轮读取。
def _carried_tool_round(record: dict[str, object]) -> int | None:
    try:
        value = int(record.get("tool_round"))
    except (TypeError, ValueError):
        return None
    return max(0, value)


# LLM: 只消费正式搜索加载名和dict型凭据信封；后者仅补web_fetch展示，不解析正文或授予执行权限，无副作用。
# 函数用途: 从结构化回执取累计工具名，让单轮循环、线程持久化及Compact共用同一恢复规则。
def _carried_loaded_tool_names(record: dict[str, object]) -> set[str]:
    return set(_ordered_carried_names(record))


# LLM: 只读 typed 搜索/凭据信封，保留显式加载顺序；不从正文推断工具或扩大执行能力。
# 函数用途: 从单个正式回执提取有序名字，凭据就绪时补 web_fetch。
def _ordered_carried_names(record: dict[str, object]) -> tuple[str, ...]:
    envelope = record.get("tool_result_envelope")
    if not isinstance(envelope, dict):
        return ()
    credential = ["web_fetch"] if (
        isinstance(envelope.get("credential"), dict) or isinstance(envelope.get("credential_discovery"), dict)
    ) else []
    search = envelope.get("tool_search")
    names = search.get("loaded_tool_names") if isinstance(search, dict) else None
    return ordered_tool_names([*(names if isinstance(names, list) else []), *credential])


# LLM: 仅显式边界调用，历史不变；有序使用记录按旧到新选择，集合输入仅表达成员优先；固定工具不因预算撤回。
# 函数用途: 选择固定、最近使用及最近发现工具，并按原发现顺序输出活跃子集。
def resolve_active_loaded_tool_names(
    discovered_names: Sequence[str], *, recent_used_tool_names: Collection[str] = (),
    pinned_tool_names: Collection[str] = (), max_active_discovered: int = 8,
) -> tuple[str, ...]:
    if type(max_active_discovered) is not int or max_active_discovered < 0:
        raise ValueError("max_active_discovered must be a non-negative integer")
    history = ordered_tool_names(discovered_names)
    pins = ordered_tool_names(pinned_tool_names)
    selected = set(pins)
    if isinstance(recent_used_tool_names, (set, frozenset)):
        priority = [name for name in reversed(history) if name in recent_used_tool_names]
    else:
        recent = ordered_tool_names(list(reversed(tuple(recent_used_tool_names))))
        priority = [name for name in recent if name in history]
    priority.extend(reversed(history))
    for name in priority:
        if len(selected) >= max_active_discovered:
            break
        selected.add(name)
    return tuple(name for name in ordered_tool_names([*history, *pins]) if name in selected)
