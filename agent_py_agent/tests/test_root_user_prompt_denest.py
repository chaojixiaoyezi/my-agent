"""根治 _root_user_prompt 无限嵌套(真机 bug:飞书多轮后 goal 滚成
'活跃任务原始需求：活跃任务原始需求：…？…当前用户后续消息：滴滴滴…' 的垃圾,把模型带偏、
派工卡壳/把启动读成停止)。修复=包装前先解嵌套,使函数幂等。"""

from __future__ import annotations

from agent.gateway_parts.request_execution import (
    _denest_active_goal,
    _GatewayConversationContext,
    _root_user_prompt,
)


def test_denest_recovers_original_from_garbage():
    garbage = (
        "活跃任务原始需求：\n\n活跃任务原始需求：\n\n活跃任务原始需求：\n\n？\n\n"
        "当前用户后续消息：\n\n滴滴滴\n\n当前用户后续消息：\n\n你好呀"
    )
    assert _denest_active_goal(garbage) == "？"


def test_denest_passthrough_clean_goal():
    assert _denest_active_goal("帮我盯安全日志") == "帮我盯安全日志"
    assert _denest_active_goal("") == ""


def test_audit_message_not_buried_by_garbage_goal():
    """垃圾 goal + 新 /audit 消息:原始需求解出是占位 '？' → 直接用当前消息当任务,不再包垃圾。"""
    garbage = "活跃任务原始需求：\n\n？\n\n当前用户后续消息：\n\n滴滴滴"
    conv = _GatewayConversationContext(active_task_goal=garbage)
    out = _root_user_prompt("/audit 6h 盯5个安全日志源", conv)
    assert out == "/audit 6h 盯5个安全日志源"


def test_wrap_is_single_level_and_idempotent():
    """正常任务 + 后续消息包一层;把包好的再当 active_goal 喂回去,仍是单层、原始需求不变
    (根治递归累积)。"""
    conv = _GatewayConversationContext(active_task_goal="帮我盯安全日志")
    w1 = _root_user_prompt("停一下", conv)
    assert w1.count("活跃任务原始需求") == 1
    assert "帮我盯安全日志" in w1 and "停一下" in w1

    conv2 = _GatewayConversationContext(active_task_goal=w1)
    w2 = _root_user_prompt("继续", conv2)
    assert w2.count("活跃任务原始需求") == 1  # 没有二层嵌套
    assert "帮我盯安全日志" in w2  # 原始需求还在
    assert "停一下" not in w2  # 旧 follow-up 被丢(在会话历史里另有)
    assert "继续" in w2


def test_no_active_goal_returns_prompt():
    conv = _GatewayConversationContext(active_task_goal="")
    assert _root_user_prompt("你好", conv) == "你好"
