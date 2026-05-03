"""LLM: 测试通道适配器基类和协议 — 消息格式转换、基类接口。

给人看的解释：
测试 IncomingMessage / OutgoingMessage 的序列化、
feishu_to_incoming / qq_to_incoming 转换函数、outgoing_to_feishu / outgoing_to_qq 转换函数。
"""

from __future__ import annotations

import time

from agent_py_agent.agent.adapter.protocol import (
    IncomingMessage,
    OutgoingMessage,
    feishu_to_incoming,
    outgoing_to_feishu,
    outgoing_to_qq,
    qq_to_incoming,
)


class TestIncomingOutgoingMessages:
    """测试统一消息格式的创建和属性。"""

    def test_incoming_message_fields(self) -> None:
        msg = IncomingMessage(
            channel="feishu",
            user_id="ou_123",
            content="hello",
            message_id="om_abc",
            timestamp=1234567890.0,
            metadata={"key": "value"},
        )
        assert msg.channel == "feishu"
        assert msg.user_id == "ou_123"
        assert msg.content == "hello"
        assert msg.message_id == "om_abc"
        assert msg.timestamp == 1234567890.0
        assert msg.metadata == {"key": "value"}

    def test_outgoing_message_fields(self) -> None:
        msg = OutgoingMessage(
            channel="feishu",
            user_id="ou_456",
            content="world",
            format="markdown",
            metadata={"ref": "xyz"},
        )
        assert msg.channel == "feishu"
        assert msg.user_id == "ou_456"
        assert msg.content == "world"
        assert msg.format == "markdown"
        assert msg.metadata == {"ref": "xyz"}

    def test_incoming_message_default_timestamp(self) -> None:
        before = time.time()
        msg = IncomingMessage(channel="qq", user_id="777", content="test", message_id="q1")
        after = time.time()
        assert before <= msg.timestamp <= after


class TestFeishuConversion:
    """测试飞书消息格式转换。"""

    def test_feishu_text_message_converted(self) -> None:
        payload = {
            "schema": "2.0",
            "header": {"event_id": "ev_1", "event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user1"}, "sender_type": "user"},
                "message": {
                    "message_id": "om_msg1",
                    "create_time": "1234567890",
                    "chat_id": "oc_chat1",
                    "content": '{"text":"你好"}',
                    "msg_type": "text",
                },
            },
        }
        msg = feishu_to_incoming(payload)
        assert msg is not None
        assert msg.channel == "feishu"
        assert msg.user_id == "ou_user1"
        assert msg.content == "你好"
        assert msg.message_id == "om_msg1"
        assert msg.metadata["feishu_chat_id"] == "oc_chat1"
        assert msg.metadata["feishu_msg_type"] == "text"

    def test_feishu_non_text_returns_none(self) -> None:
        payload = {
            "event": {
                "sender": {"sender_id": {"open_id": "ou_x"}},
                "message": {
                    "message_id": "om_y",
                    "content": '{"text":"ignored"}',
                    "msg_type": "image",  # 非文本
                },
            },
        }
        assert feishu_to_incoming(payload) is None

    def test_feishu_empty_content_returns_none(self) -> None:
        payload = {
            "event": {
                "sender": {"sender_id": {"open_id": "ou_x"}},
                "message": {
                    "message_id": "om_y",
                    "content": '{"text":""}',
                    "msg_type": "text",
                },
            },
        }
        assert feishu_to_incoming(payload) is None

    def test_feishu_url_verification_event_returns_none(self) -> None:
        # url_verification 事件没有标准 message 结构
        payload = {"type": "url_verification", "challenge": "abc"}
        assert feishu_to_incoming(payload) is None

    def test_feishu_malformed_content_handled(self) -> None:
        payload = {
            "event": {
                "sender": {"sender_id": {"open_id": "ou_bad"}},
                "message": {
                    "message_id": "om_bad",
                    "content": "not-json",  # 非 JSON content
                    "msg_type": "text",
                },
            },
        }
        msg = feishu_to_incoming(payload)
        assert msg is not None
        assert msg.content == "not-json"

    def test_outgoing_to_feishu(self) -> None:
        msg = OutgoingMessage(channel="feishu", user_id="ou_1", content="回复内容")
        result = outgoing_to_feishu(msg)
        assert result["msg_type"] == "text"
        assert result["content"] == {"text": "回复内容"}


class TestQQConversion:
    """测试 QQ 消息格式转换。"""

    def test_qq_new_format_converted(self) -> None:
        payload = {
            "d": {
                "author": {"id": "123456"},
                "content": "hello qq",
                "msg_id": "msg_abc",
                "timestamp": 1234567890.5,
                "guild_id": "guild_1",
                "channel_id": "chn_2",
            },
        }
        msg = qq_to_incoming(payload)
        assert msg is not None
        assert msg.channel == "qq"
        assert msg.user_id == "123456"
        assert msg.content == "hello qq"
        assert msg.message_id == "msg_abc"
        assert msg.metadata["qq_guild_id"] == "guild_1"
        assert msg.metadata["qq_channel_id"] == "chn_2"

    def test_qq_legacy_format_converted(self) -> None:
        payload = {
            "event": {
                "user_id": 999,
                "content": "legacy msg",
                "msg_id": "old_id",
                "timestamp": 1111111,
                "guild_id": "g1",
                "channel_id": "c1",
            },
        }
        msg = qq_to_incoming(payload)
        assert msg is not None
        assert msg.user_id == "999"
        assert msg.content == "legacy msg"
        assert msg.message_id == "old_id"

    def test_qq_empty_content_returns_none(self) -> None:
        payload = {"d": {"author": {"id": "1"}, "content": "  ", "msg_id": "x"}}
        assert qq_to_incoming(payload) is None

    def test_outgoing_to_qq(self) -> None:
        msg = OutgoingMessage(channel="qq", user_id="777", content="qq 回复")
        result = outgoing_to_qq(msg)
        assert result["content"] == "qq 回复"


class TestBaseChannelAdapter:
    """测试 BaseChannelAdapter 基类接口。"""

    def test_adapter_name_must_be_overridden(self) -> None:
        from agent_py_agent.agent.adapter.base import BaseChannelAdapter

        # 直接实例化会报抽象类错误（如果正确实现了 ABC）
        # 验证 adapter_name 类属性存在
        assert hasattr(BaseChannelAdapter, "adapter_name")

    def test_message_callback_registration(self) -> None:
        from agent_py_agent.agent.adapter.base import BaseChannelAdapter

        class DummyAdapter(BaseChannelAdapter):
            adapter_name = "dummy"

            def start(self) -> None:
                self._running = True

            def stop(self) -> None:
                self._running = False

            def send_message(self, user_id: str, message) -> bool:
                return True

        calls: list[IncomingMessage] = []

        def cb(msg: IncomingMessage) -> None:
            calls.append(msg)

        adapter = DummyAdapter({})
        adapter.on_message(cb)

        # 模拟收到消息并分发
        test_msg = IncomingMessage(
            channel="test", user_id="u1", content="hello", message_id="m1"
        )
        adapter.dispatch(test_msg)

        assert len(calls) == 1
        assert calls[0].content == "hello"
        assert adapter.running is False  # 未调用 start
