# LLM: 加载名是线程展示事实，不是权限；只读正式搜索信封并原子并入线程，不从摘要正文反推或写Agent全局状态。
# 模块用途: 压缩后恢复同线程工具表，并从当前授权快照提供不改原生定义的参数速查。
from collections.abc import Callable, Iterable
from copy import deepcopy
from dataclasses import replace

from ..settings.reasoning_effort import run_thread_id
from ..tooling.tool_search_state import (
    MAX_SESSION_DISCOVERED_TOOLS,
    ordered_carried_loaded_tool_names,
    ordered_loop_loaded_tool_names,
    ordered_tool_names,
)


# LLM: 只读当前可用、可见且在allowed_tools内的ToolRuntimeSnapshot；不读全局handler或catalog_specs兜底。
#   名称保序去重，max_tools限制成功条目；只复制required/类型/枚举，不携描述或热改原生schema。
# 函数用途: 为尾部速查生成已授权工具的精简参数表，未知来源不返回定义。
def build_discovered_tool_micro_schemas(
    tools_registry: object, tool_names: tuple[str, ...] | list[str], *, max_tools: int = 12,
) -> list[dict[str, object]]:
    from ..tooling.models import ToolRuntimeSnapshot

    if type(max_tools) is not int or max_tools < 0:
        raise ValueError("max_tools must be a non-negative integer")
    snapshot = tools_registry
    if not isinstance(snapshot, ToolRuntimeSnapshot):
        factory = getattr(tools_registry, "runtime_snapshot", None)
        snapshot = factory() if callable(factory) else None
    if not isinstance(snapshot, ToolRuntimeSnapshot) or not max_tools:
        return []
    rows = []
    for name in ordered_tool_names(tool_names):
        if snapshot.allowed_tools is not None and name not in snapshot.allowed_tools:
            continue
        runtime = snapshot.runtime(name)
        if runtime is None or not runtime.availability.available or not runtime.exposure.model_visible:
            continue
        schema = runtime.model_spec.input_schema
        rows.append({"name": name, "required": list(schema.get("required", ())), "properties": {
            key: _micro_property_summary(value) for key, value in schema.get("properties", {}).items()
        }})
        if len(rows) >= max_tools:
            break
    return rows


# LLM: 摘要不是校验器；联合类型/数组/引用保留结构，递归有界；大枚举明确标省略，不伪造枚举子集。
# 函数用途: 丢弃长描述和示例，仅小型枚举完整显示，完整参数始终以原生定义为准。
def _micro_property_summary(value: object, depth: int = 0) -> object:
    if not isinstance(value, dict) or depth >= 3:
        return "schema"
    if isinstance(value.get("enum"), list):
        values = value["enum"]
        if len(values) > 8 or any(isinstance(item, (dict, list)) or (isinstance(item, str) and len(item) > 64) for item in values):
            return {"type": value.get("type", "enum"), "enum_count": len(values), "enum_omitted": True}
        return {"enum": deepcopy(values)}
    for key in ("anyOf", "oneOf", "allOf"):
        if isinstance(value.get(key), list):
            parts = [_micro_property_summary(item, depth + 1) for item in value[key]]
            return "|".join(parts) if key != "allOf" and all(isinstance(item, str) for item in parts) else {key: parts}
    kind = value.get("type")
    if isinstance(kind, list):
        return "|".join(str(item) for item in kind)
    if kind == "array":
        item = _micro_property_summary(value.get("items"), depth + 1)
        return f"array<{item}>" if isinstance(item, str) else {"type": "array", "items": item}
    return kind if isinstance(kind, str) else {"$ref": value["$ref"]} if "$ref" in value else "schema"


# LLM: 新序列保留首次发现顺序，旧集合使用统一稳定兜底；损坏字段不变成新工具，授权仍由当前快照过滤。
# 函数用途: 归一线程的有序工具名，不执行工具。
def normalized_loaded_tools(value: object) -> tuple[str, ...]:
    return ordered_tool_names(value)


# LLM: 兼容旧set读口；真实顺序只从同一线程的正式字段读取，不改变授权。
# 函数用途: 为旧调用方返回本线程已加载工具名集合。
def thread_loaded_tools(agent: object, params: object) -> set[str]:
    return set(ordered_thread_loaded_tools(agent, params))


# LLM: 与路由身份共用子线程优先级；无线程或尚未创建时不借用其它线程、归档或Agent集合。
# 函数用途: 为新回合读取本线程的首次发现顺序。
def ordered_thread_loaded_tools(agent: object, params: object) -> tuple[str, ...]:
    thread_id = run_thread_id(params)
    store = getattr(agent, "conversation_store", None)
    if not thread_id or store is None:
        return ()
    thread = store.threads.load(thread_id)
    return normalized_loaded_tools(getattr(thread, "loaded_tool_names", ()))


# LLM: 每个循环仅在首次渲染时取本线程顺序；只写原展示状态，名称仍由loaded set及当前授权快照过滤。
# 函数用途: 防止跨回合重建的旧集合把持久发现顺序重新按字母排列。
def seed_loop_discovery_order(agent: object, params: object) -> None:
    state = getattr(params, "live_archive_state", None)
    if isinstance(state, dict) and "loaded_tool_order" not in state:
        state["loaded_tool_order"] = list(ordered_tool_names([
            *ordered_thread_loaded_tools(agent, params), *ordered_loop_loaded_tool_names(params),
        ]))


# LLM: 只清洗结构化名称；已有名字重现时移到尾部，成员不丢失，不改调用方输入或执行权限。
# 函数用途: 按最后出现顺序刷新发现新近性，供下一次Compact挑选活跃集。
def refresh_discovery_recency(existing_history: Iterable[object], newly_active: Iterable[object]) -> tuple[str, ...]:
    existing = normalized_loaded_tools(list(existing_history))
    # 同一批内重复调用也以末次出现为准。
    names = tuple(reversed(normalized_loaded_tools(list(newly_active)[::-1])))
    return (*tuple(name for name in existing if name not in names), *names)


# LLM: 版本只来自本线程宿主metadata；坏版本忽略，旧线程为空，不从文字或时间推断，不改变授权。
# 函数用途: 读取可跨Compact基线比较的逐工具发现版本，识别尾项重搜等顺序不变的事件。
def discovery_revisions(thread: object) -> dict[str, int]:
    metadata = getattr(thread, "metadata", None)
    raw = metadata.get("discovered_tool_revisions") if isinstance(metadata, dict) else None
    if not isinstance(raw, dict):
        return {}
    return {name: version for name, version in raw.items()
            if isinstance(name, str) and name and name == name.strip() and type(version) is int and version > 0}


# LLM: 仅消费正式搜索信封和宿主schema_reload事实；盲调补表的持久写仍在原结果IR归档之后，失败语义不变。
# 函数用途: 在原post-archive边界持久记录搜索或自动补表，不让辅助写入提前阻断工具结果。
def persist_discovered_tools(agent: object, params: object, record: dict[str, object]) -> None:
    names = ordered_carried_loaded_tool_names([record])
    reload = record.get("schema_reload")
    if isinstance(reload, dict) and reload.get("schema_auto_activated_for_next_turn") is True:
        schema = reload.get("loaded_micro_schema")
        if isinstance(schema, dict) and schema.get("name") == record.get("tool"):
            names = ordered_tool_names([*names, schema["name"]])
    record_session_discovered_tools(agent, names, params=params)


# LLM: 调用方已用当前快照裁决展示资格；此入口仅记录名称，不授权或执行，不伪造一次tool_search调用。
# 函数用途: 将搜索或宿主自动补表的名称原子并入当前线程，刷新同一份新近性与版本。
def record_session_discovered_tools(agent: object, tool_names: Iterable[str], *, params: object) -> None:
    names = ordered_tool_names(list(tool_names))
    thread_id = run_thread_id(params)
    store = getattr(agent, "conversation_store", None)
    if not names or not thread_id or store is None:
        return

    # LLM: 回调读 update_atomic 提供的最新线程，拷贝 metadata 后合并，不热改传入对象。
    # 函数用途: 原子保留wire追加顺序并刷新发现新近性和版本，不覆盖其它线程字段。
    def merge(thread):
        loaded = normalized_loaded_tools((*thread.loaded_tool_names, *names))
        metadata = dict(thread.metadata)
        history = normalized_loaded_tools(metadata.get("discovered_tool_history", ()))
        complete = normalized_loaded_tools([*history, *thread.loaded_tool_names])
        metadata["discovered_tool_history"] = list(refresh_discovery_recency(complete, names))
        revisions = discovery_revisions(thread)
        version = max(revisions.values(), default=0) + 1
        metadata["discovered_tool_revisions"] = {**revisions, **dict.fromkeys(names, version)}
        return replace(thread, loaded_tool_names=loaded, metadata=metadata) if (
            loaded != thread.loaded_tool_names or metadata != thread.metadata) else thread

    try:
        store.threads.update_atomic(thread_id, merge)
    except FileNotFoundError:
        # 尚未创建或已经移除的线程不补造；其它存储失败仍显式传播。
        return


# LLM: 仅从正式原生消息抽取assistant/tool_use名；普通正文、用户块与工具结果不成为使用信号，无副作用。
# 函数用途: 在CAS锁外冻结近期实际使用顺序，避免锁内扫描原始消息。
def recent_used_tool_names_from_messages(messages: Iterable[object]) -> tuple[str, ...]:
    from .native_history import iter_provider_history_messages_from_rows

    recent = []
    for message in iter_provider_history_messages_from_rows(messages):
        content = message.get("content")
        if message.get("role") == "assistant" and isinstance(content, list):
            recent.extend(block.get("name") for block in content if block.get("type") == "tool_use")
    return refresh_discovery_recency((), recent)


# LLM: CAS内纯计算；默认容量沿唯一32项常量，显式小限额和基线后重搜保护不变；pins须已发现，不扩权。
# 函数用途: 压缩时保持会话内已发现工具的wire顺序，按明确限额才收缩。
def active_tools_for_compact(
    thread: object, *, recent_used_tool_names: tuple[str, ...] = (),
    baseline_discovery_revisions: dict[str, int] | None = None,
    max_active_discovered: int = MAX_SESSION_DISCOVERED_TOOLS,
) -> tuple[str, ...]:
    from ..tooling.tool_search_state import resolve_active_loaded_tool_names

    loaded = normalized_loaded_tools(getattr(thread, "loaded_tool_names", ()))
    metadata = dict(getattr(thread, "metadata", {}) or {})
    history = normalized_loaded_tools([*metadata.get("discovered_tool_history", ()), *loaded])
    revisions = discovery_revisions(thread)
    fresh = () if baseline_discovery_revisions is None else tuple(
        name for name in history if revisions.get(name, 0) > baseline_discovery_revisions.get(name, 0))
    pins = tuple(name for name in normalized_loaded_tools([
        *normalized_loaded_tools(metadata.get("pinned_tool_names", ())), *fresh,
    ]) if name in history)
    return resolve_active_loaded_tool_names([*loaded, *history], recent_used_tool_names=recent_used_tool_names,
                                          pinned_tool_names=pins, max_active_discovered=max_active_discovered)


# LLM: 调用方在CAS前调用；锁外冻结消息使用序列和发现版本，返回的selector锁内只读最新线程，无I/O。
# 函数用途: 给普通和live Compact共用并发基线，不把锁外准备散落到提交函数。
def compact_tool_selector(thread: object, messages: Iterable[object]) -> Callable[[object], tuple[str, ...]]:
    from functools import partial

    return partial(active_tools_for_compact, recent_used_tool_names=recent_used_tool_names_from_messages(messages),
                   baseline_discovery_revisions=discovery_revisions(thread))


# LLM: 仅在Compact CAS边界收缩；保留基线后新发现的成员及完整新近性历史，不能凭空新增工具。
# 函数用途: 计算压缩提交后的线程 metadata 与活跃 loaded_tool_names 字段更新。
def compact_discovered_tools(
    thread: object,
    active_loaded_tool_names: tuple[str, ...] | None,
    *, baseline_loaded_tool_names: tuple[str, ...] | None = None,
) -> dict[str, object]:
    if active_loaded_tool_names is None:
        return {}
    metadata = dict(getattr(thread, "metadata", {}) or {})
    loaded = getattr(thread, "loaded_tool_names", ()) or ()
    history = normalized_loaded_tools([*metadata.get("discovered_tool_history", ()), *loaded])
    selected = normalized_loaded_tools(active_loaded_tool_names)
    if not set(selected) <= set(history):
        raise ValueError("active selection must not invent discovered tools")
    metadata["discovered_tool_history"] = list(history)
    baseline = set(loaded if baseline_loaded_tool_names is None else baseline_loaded_tool_names)
    concurrent = tuple(name for name in loaded if name not in baseline)
    return {"metadata": metadata, "loaded_tool_names": normalized_loaded_tools([*selected, *concurrent])}
