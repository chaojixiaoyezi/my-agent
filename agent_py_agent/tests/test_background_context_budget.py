"""后台上下文总预算只估算将渲染的节：有历史种子时不渲染的最近消息不再挤占预算；估算节与渲染节逐一对应。"""
from __future__ import annotations

import pytest

from agent_py_agent.agent.conversation import context_budget
from agent_py_agent.agent.conversation.background_context import (
    _BACKGROUND_SECTIONS,
    PreparedBackgroundContext,
    background_rendered_payload_keys,
    render_background_context,
)
from agent_py_agent.agent.conversation.context_budget import (
    BackgroundContextPayloadRequest,
    bounded_background_context_payload,
)

_TITLE_KEYS = {title: key for title, key, _empty in _BACKGROUND_SECTIONS} | {"Audit Task Objective": "tasks"}


# LLM: 与审阅时的纯函数实测同一构造：6 个任务、12 条观察，另有 40 条每条约 3.2 万字的最近消息。
# 函数用途: 生成一份会让"不渲染的最近消息"撑爆总预算的后台 payload。
def _request(with_messages: bool = True) -> BackgroundContextPayloadRequest:
    messages = [{"role": "user" if index % 2 == 0 else "assistant", "content": f"原文{index}:" + "资料" * 16_000}
                for index in range(40)] if with_messages else []
    bundle = {
        "thread": {"thread_id": "thread-budget", "summary": ""},
        "messages": messages,
        "tasks": [{"task_id": f"task-{index}", "title": f"任务{index}", "status": "running",
                   "detail": "核对进度与下一步。" * 20} for index in range(6)],
        "observations": [{"kind": "progress", "text": f"观察{index}：" + "已完成部分核对。" * 30} for index in range(12)],
        "guidance": [], "channel_bindings": [],
    }
    return BackgroundContextPayloadRequest(bundle=bundle, active_wake_signal={"reason": "scheduled_progress_report"},
                                           pending_wake_signals=[], agent_tree={},
                                           task_runtime_state={"task_id": "task-0", "stage": "verify"})


# 函数用途: 以原准备结果形状构造一次待渲染的后台上下文。
def _prepared(*, include_recent_messages: bool, narrow_audit_event: bool = False) -> PreparedBackgroundContext:
    return PreparedBackgroundContext(payload=_request(), header=("[background-main-agent-context]",),
                                     control_policy={"mode": "test"}, control_actions=(), task_id="task-0",
                                     narrow_audit_event=narrow_audit_event,
                                     include_recent_messages=include_recent_messages)


def test_seeded_context_budget_ignores_unrendered_recent_messages():
    seeded = background_rendered_payload_keys(include_recent_messages=False, narrow_audit_event=False)
    payload = bounded_background_context_payload(_request(), rendered_keys=seeded)
    assert "messages" not in payload
    assert payload["_projection"]["shape_pass"] == 0 and payload["_projection"]["total_budget_applied"] is False
    assert len(payload["observations"]) == 12, "有种子时，不渲染的最近消息不能把观察挤掉"
    # 对照：同一材料在无种子时计入最近消息，总预算按原规则收缩。
    unseeded = background_rendered_payload_keys(include_recent_messages=True, narrow_audit_event=False)
    shrunk = bounded_background_context_payload(_request(), rendered_keys=unseeded)
    assert "messages" in shrunk and shrunk["_projection"]["shape_pass"] > 0


def test_rendering_keeps_recent_messages_only_without_history_seed():
    seeded = render_background_context(_prepared(include_recent_messages=False))
    unseeded = render_background_context(_prepared(include_recent_messages=True))
    assert "## Recent Messages" not in seeded and '"shape_pass": 0' in seeded
    assert "## Recent Messages" in unseeded and '"shape_pass": 0' not in unseeded


@pytest.mark.parametrize(("include_recent_messages", "narrow_audit_event"), [(False, False), (True, False), (True, True)])
def test_estimated_sections_equal_rendered_sections(monkeypatch, include_recent_messages, narrow_audit_event):
    estimated = []
    original = context_budget.estimate_tokens

    # 函数用途: 记录总预算第一次估算时实际计入的 payload 键。
    def spy(value):
        if isinstance(value, dict) and not estimated:
            estimated.append(frozenset(value) - {"_projection"})
        return original(value)

    monkeypatch.setattr(context_budget, "estimate_tokens", spy)
    rendered = render_background_context(_prepared(include_recent_messages=include_recent_messages,
                                                   narrow_audit_event=narrow_audit_event))
    titles = [line.removeprefix("## ") for line in rendered.splitlines() if line.startswith("## ")]
    rendered_keys = frozenset(_TITLE_KEYS[title] for title in titles if title in _TITLE_KEYS)
    assert estimated == [rendered_keys], "预算估算的节集合必须与实际渲染的节集合一致"
