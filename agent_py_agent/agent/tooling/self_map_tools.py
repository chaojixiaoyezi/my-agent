# LLM: 工具目录沿main本人公开读口和冻结规格，不重复判权限、加载工具或裁决具体资源；新增类别沿原category公开。
# 模块用途: 为自我目录登记当前授权工具的用途、来源、展开及不可用原因。
from .self_map_registry import SectionDescriptor, SectionProjection, map_item, next_read


# LLM: 只登记领域读取器，查询时不执行原工具或修改原发现账。
# 函数用途: 将工具分块接入当前代理的自我目录。
def register_tool_section(registry):
    registry.register(SectionDescriptor("tools", "工具", 20, tool_projection, "当前工具用途、来源、权限与按需加载状态"))


# LLM: disabled/unavailable保留main原原因；投影仅用当前owner/run的公开tool_states，不补进其它owner或宿主隐藏工具。
# 函数用途: 按同一来源列工具和就绪、收起、不可用、禁用数量。
def tool_projection(context):
    specs = {spec.name: spec for spec in public_catalog_specs(context)}
    items, missing = [], []
    for state in context.public_context.get("tool_states", ()):
        name = state["name"]
        spec = specs.get(name)
        if spec is None and state["state"] == "disabled":
            tool = context.agent.tools.tools.get(name)
            spec = tool.model_spec if tool is not None else None
        if spec is None:
            missing.append(name)
            continue
        fields = {**public_tool_fields(spec, context.visible_names), "provider": spec.hints.provider_id,
                  "provider_kind": _provider_kind(spec.hints.provider_id),
                  "error_code": state["error_code"], "reason": state["reason"],
                  "summary": state["state"] + "：" + spec.description[:160]}
        if state["state"] == "deferred" and "tool_search" in context.visible_names:
            fields["expand"] = next_read("tool_search", {"query": name, "limit": 1})
        fields["next_read"] = next_read("tool_search", {"query": name, "limit": 1})
        items.append(map_item(name, name, state=state["state"], fields=fields))
    return SectionProjection(tuple(items), "partial" if missing else "complete",
        errors=("SELF_MAP_TOOL_METADATA_MISSING",) if missing else (), source="tool_self_context / ToolRuntimeSnapshot",
        counts={key: sum(row["state"] == state for row in items)
                for key, state in (("direct_callable", "ready"), ("deferred", "deferred"),
                                   ("unavailable", "unavailable"), ("disabled", "disabled"))},
        count_labels={"direct_callable": "直接可调", "deferred": "按需加载", "unavailable": "不可用", "disabled": "禁用"})


# LLM: provider_id是宿主来源字段；未知前缀保留unknown，不能由工具名猜提供方。
# 函数用途: 区分已知工具提供方，允许未来新来源。
def _provider_kind(provider_id):
    if not provider_id:
        return "builtin"
    return next((kind for prefix, kind in (("plugin:", "plugin"), ("mcp:", "mcp"), ("package:", "capability_pack"))
                 if provider_id.startswith(prefix)), "unknown")


# LLM: 只投影冻结规格的公共字段；category是开放元数据，不做封闭类别准入。
# 函数用途: 保留工具原用途、类别和本线程是否展开。
def public_tool_fields(spec, visible_names):
    return {"name": spec.name, "purpose": spec.description[:160], "group": spec.category,
            "expanded": spec.name in visible_names}


# LLM: 原runtime.exposure决定模型可见范围；不能把宿主工具计入目录，哈希核验沿snapshot.specs。
# 函数用途: 读取当前可公开的冻结工具规格。
def public_tool_specs(snapshot):
    visible = {runtime.model_spec.name for runtime in snapshot.runtimes if runtime.exposure.model_visible}
    return tuple(spec for spec in snapshot.specs if spec.name in visible)


# LLM: 不可用/禁用规格仅按main同次公开状态取原registry定义，状态和授权仍以快照为准，不增加备用执行runtime。
# 函数用途: 给工具、MCP和插件目录共用已公开名称的元数据，不维护第二份规格清单。
def public_catalog_specs(context):
    specs = {spec.name: spec for spec in public_tool_specs(context.snapshot)}
    for state in context.public_context.get("tool_states", ()):
        if state["state"] not in {"disabled", "unavailable"} or state["name"] in specs:
            continue
        tool = context.agent.tools.tools.get(state["name"])
        if tool is not None:
            specs[state["name"]] = tool.model_spec
    return tuple(specs.values())
