"""Background main-agent tool policy helpers."""

from __future__ import annotations

DEFAULT_BACKGROUND_ALLOWED_TOOLS = (
    "inspect_agent_tree",
    "raise_event",
    "raise_collaboration",
    "inspect_collaboration",
    "submit_collaboration_result",
    "update_collaboration",
    "dispatch_subagents",
    "send_guidance",
    "create_subagents",
)

CONTROL_ACTION_DESCRIPTIONS = {
    "inspect_agent_tree": "只读查看主/子/孙代理状态树。",
    "raise_event": "记录普通进展、阻塞或需要主代理处理的事件。",
    "raise_collaboration": "发起协作；没有 case_id 时开 case，有 question/target 时同步发 request。",
    "inspect_collaboration": "只读查看协作 case 或待处理协作请求。",
    "submit_collaboration_result": "提交协作命中、未命中、证据引用和限制说明。",
    "update_collaboration": "更新协作 case 或 request；带 target_agent_ids 可改派请求。",
    "dispatch_subagents": "只有需要推进、恢复或调度时才调用。",
    "send_guidance": "给正在运行的代理追加软提示。",
    "create_subagents": "创建并启动新的下级代理。",
}


def background_allowed_tools(config: object | None = None) -> list[str]:
    configured = tool_names(getattr(config, "background_main_agent_allowed_tools", None))
    return configured or list(DEFAULT_BACKGROUND_ALLOWED_TOOLS)


def background_control_action_lines(config: object | None = None) -> list[str]:
    lines: list[str] = []
    for name in background_allowed_tools(config):
        description = CONTROL_ACTION_DESCRIPTIONS.get(name)
        if description:
            lines.append(f"- {name}: {description}")
    return lines


def tool_names(value: object) -> list[str]:
    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, (list, tuple)):
        raw_items = value
    else:
        return []
    return list(dict.fromkeys(str(item).strip() for item in raw_items if str(item).strip()))
