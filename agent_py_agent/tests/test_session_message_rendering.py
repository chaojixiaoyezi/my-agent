"""会话消息的呈现不得冒充用户原话（dev 审阅点 2）。

`_render_guidance_user_input` 决定 guidance 注入当前回合时的文本。用户插话原样渲染成 steer；
带 `origin_kind=session_message` 的会话消息必须渲染成宿主事件，写明来源会话，
不能出现在用户原话位置。这两条都必须有断言。
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.runtime.guidance import (
    _is_session_message_entry,
    _render_guidance_user_input,
)
from agent_py_agent.agent.conversation.session_messaging import (
    SESSION_MESSAGE_HOST_EVENT_MARKER,
    SESSION_MESSAGE_ORIGIN_KIND,
)


def _entry(message: str, *, metadata: dict | None = None, guidance_id: str = "g-1"):
    return SimpleNamespace(
        guidance_id=guidance_id,
        message=message,
        metadata=metadata or {},
        target_type="thread",
        target_id="thread-x",
        priority="normal",
        sender="thread-a",
    )


def test_session_message_rendered_as_host_event_not_user_text() -> None:
    entry = _entry(
        "构建完成后把产物路径发我。",
        metadata={"origin_kind": SESSION_MESSAGE_ORIGIN_KIND, "origin_thread_id": "thread-a"},
    )
    assert _is_session_message_entry(entry) is True
    rendered = _render_guidance_user_input([entry])
    # 带宿主事件标记，明确不是用户原话。
    assert SESSION_MESSAGE_HOST_EVENT_MARKER in rendered
    # 写明来源会话。
    assert "thread-a" in rendered
    # 正文仍在，但被宿主事件包裹，不是裸的用户原话。
    assert "构建完成后把产物路径发我。" in rendered
    assert rendered.strip() != "构建完成后把产物路径发我。"


def test_user_steering_renders_as_plain_user_text() -> None:
    entry = _entry("请把输出写到我的目录。", metadata={})
    assert _is_session_message_entry(entry) is False
    rendered = _render_guidance_user_input([entry])
    # 用户插话保持原样，不带会话消息标记。
    assert rendered == "请把输出写到我的目录。"
    assert SESSION_MESSAGE_HOST_EVENT_MARKER not in rendered


def test_unknown_origin_kind_treated_as_user_text() -> None:
    # 只按结构化 origin_kind 判定；其它值一律按既有用户插话处理（行为不变）。
    entry = _entry("别的来源", metadata={"origin_kind": "something_else"})
    assert _is_session_message_entry(entry) is False
    assert _render_guidance_user_input([entry]) == "别的来源"


def test_mixed_entries_keep_session_message_marked() -> None:
    session_entry = _entry(
        "来自另一个会话的消息",
        metadata={"origin_kind": SESSION_MESSAGE_ORIGIN_KIND, "origin_thread_id": "thread-b"},
        guidance_id="g-session",
    )
    user_entry = _entry("用户自己的补充", guidance_id="g-user")
    rendered = _render_guidance_user_input([session_entry, user_entry])
    assert SESSION_MESSAGE_HOST_EVENT_MARKER in rendered
    assert "thread-b" in rendered
    assert "用户自己的补充" in rendered
    # 会话消息段在用户插话之前。
    assert rendered.index(SESSION_MESSAGE_HOST_EVENT_MARKER) < rendered.index("用户自己的补充")


def test_missing_origin_thread_still_marked() -> None:
    entry = _entry("没有来源信息", metadata={"origin_kind": SESSION_MESSAGE_ORIGIN_KIND})
    rendered = _render_guidance_user_input([entry])
    assert SESSION_MESSAGE_HOST_EVENT_MARKER in rendered
    assert "unknown" in rendered
