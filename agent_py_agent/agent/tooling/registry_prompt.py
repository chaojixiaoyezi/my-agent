
from __future__ import annotations


def render_tool_catalog_section(entries: list[str], content_transport_protocol: str) -> str:
    if not entries:
        entries = ["- none：当前执行上下文没有授权任何工具；缺能力时请上抛 capability_request。"]
    return (
        _tool_call_protocol()
        + "\n\n"
        + content_transport_protocol
        + "\n\n"
        "# Tool Catalog\n"
        + "\n".join(entries)
    )


def _tool_call_protocol() -> str:
    return (
        "# Tools\n"
        "当你需要看文件、改代码、查网页或测接口时，可以调用工具。\n"
        "工具调用格式必须严格写成：\n"
        "[TOOL_CALL]\n"
        '{"tool": "tool_name", "parameter_name": "parameter_value"}\n'
        "[/TOOL_CALL]\n"
        "必须把工具参数直接放在同一个 JSON 对象里；不要写 param_name、args、arguments 或其他包裹参数。\n"
        "必须使用 Tool Catalog 里该工具自己的参数名；不要把 path 当作所有工具的默认参数。\n"
        "可以连续写多个 [TOOL_CALL] 块。拿到工具结果后，再输出最终答案，不要把工具调用块留在最后回复里。"
    )
