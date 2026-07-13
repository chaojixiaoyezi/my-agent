"""当前用户消息不能再被历史任务 goal 包装或递归污染。"""

from __future__ import annotations

from agent.gateway_parts.request_execution import (
    _GatewayConversationContext,
    _root_user_prompt,
)


def test_current_chat_prompt_ignores_garbage_active_goal():
    garbage = (
        "活跃任务原始需求：\n\n活跃任务原始需求：\n\n？\n\n"
        "当前用户后续消息：\n\n滴滴滴"
    )
    conv = _GatewayConversationContext(active_task_goal=garbage)
    assert _root_user_prompt("你好呀", conv) == "你好呀"


def test_current_task_lane_prompt_also_stays_current_message():
    conv = _GatewayConversationContext(
        lane="task",
        task_ref="task-1",
        active_task_id="task-1",
        active_task_goal="帮我盯安全日志",
    )
    assert _root_user_prompt("先暂停", conv) == "先暂停"


def test_audit_message_not_buried_by_old_goal():
    conv = _GatewayConversationContext(active_task_goal="旧任务")
    assert _root_user_prompt("/audit 6h 盯5个安全日志源", conv) == "/audit 6h 盯5个安全日志源"
