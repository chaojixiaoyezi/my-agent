from __future__ import annotations

import json

import pytest

from agent_py_agent.agent.adapter.protocol import feishu_to_incoming


# LLM: 合成供应商事件保留原key/open_id或post at，不替产品补推断名字或目标身份。
# 函数用途: 构造显式授权的文本/富文本样本，仅供原入站协议解析测试。
def _event(*, text="@_user_1 /group 监控 @_user_2", kind="text", mentions=None, post=None):
    rows = mentions if mentions is not None else [
        {"key": "@_user_1", "id": {"open_id": "ou_bot"}},
        {"key": "@_user_2", "id": {"open_id": "ou_target"}},
    ]
    return {"event": {"sender": {"sender_id": {"open_id": "ou_sender"}}, "message": {
        "message_id": "om_group_grant", "chat_id": "oc_group", "chat_type": "group",
        "message_type": kind, "mentions": rows,
        "content": json.dumps({"text": text} if kind == "text" else {"content": post}),
    }}}


def test_real_text_mention_becomes_precise_target_without_changing_actor():
    message = feishu_to_incoming(_event(), bot_open_id="ou_bot")
    assert message.content == "/group 监控 member:ou_target"
    assert message.user_id == "ou_sender" and message.conversation_id == "oc_group"


@pytest.mark.parametrize("target", ["张三", "@张三", "@ou_target", "@_user_99"])
def test_hand_typed_name_or_identifier_is_not_invented_as_platform_mention(target):
    message = feishu_to_incoming(_event(text=f"@_user_1 /group 监控 {target}"), bot_open_id="ou_bot")
    assert message.content == f"/group 监控 {target}"


def test_ambiguous_duplicate_mention_key_is_not_resolved():
    event = _event()
    event["event"]["message"]["mentions"].append({"key": "@_user_2", "id": {"open_id": "ou_other"}})
    message = feishu_to_incoming(event, bot_open_id="ou_bot")
    assert message.content == "/group 监控 @_user_2"


def test_post_structured_at_is_resolved_without_name_guessing():
    event = _event(kind="post", post=[[
        {"tag": "at", "user_id": "ou_bot"}, {"tag": "text", "text": " /group 监控 "},
        {"tag": "at", "user_id": "ou_target"},
    ]])
    message = feishu_to_incoming(event, bot_open_id="ou_bot")
    assert message.content == "/group 监控 member:ou_target"


def test_post_at_mention_key_uses_exact_platform_mapping():
    event = _event(kind="post", post=[[
        {"tag": "at", "user_id": "ou_bot"}, {"tag": "text", "text": " /group 监控 "},
        {"tag": "at", "user_id": "@_user_2"},
    ]])
    message = feishu_to_incoming(event, bot_open_id="ou_bot")
    assert message.content == "/group 监控 member:ou_target"


@pytest.mark.parametrize("target", ["全群", "@_all", "@all", "member:ou_target"])
def test_whole_group_and_exact_control_identifiers_are_left_to_control_parser(target):
    event = _event(text=f"@_user_1 /group 监控 {target}")
    message = feishu_to_incoming(event, bot_open_id="ou_bot")
    assert message.content == f"/group 监控 {target}"


def test_ordinary_conversation_with_the_same_words_is_not_a_control_command():
    message = feishu_to_incoming(_event(text="@_user_1 请解释 /group 监控 @_user_2"), bot_open_id="ou_bot")
    assert message.content == "请解释 /group 监控 @_user_2"


def test_trailing_bot_mention_and_target_are_independent():
    message = feishu_to_incoming(_event(text="/group 监控 @_user_2 @_user_1"), bot_open_id="ou_bot")
    assert message.content == "/group 监控 member:ou_target"
