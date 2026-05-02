"""LLM: 测试 QQ 适配器 — WebSocket 模式。

给人看的解释：
测试 QQAdapter 的各项能力：消息解析、WebSocket 处理、心跳、token 缓存。
不实际发起 QQ HTTP 请求，所有外部调用均 mock。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent_py_agent.agent.adapter.protocol import IncomingMessage, OutgoingMessage
from agent_py_agent.agent.adapter.qq import QQAdapter


class TestQQMessageDeduplication:
    """测试 QQ 消息去重机制。"""

    def test_same_message_id_not_processed_twice(self) -> None:
        adapter = QQAdapter(
            config={"qq_app_id": "", "qq_app_secret": ""},
            workspace_root=Path(tempfile.gettempdir()),
        )

        calls: list[IncomingMessage] = []

        def cb(msg: IncomingMessage) -> None:
            calls.append(msg)

        adapter.on_message(cb)

        # QQ WebSocket MESSAGE_CREATE 的 d 字段内容
        msg_d = {
            "author": {"user_openid": "123"},
            "content": "hello",
            "id": "dup_id",
            "timestamp": "2024-01-01T00:00:00+08:00",
        }

        # 第一次 — 应该分发
        adapter._process_qq_message(msg_d)
        assert len(calls) == 1

        # 第二次同样 id — 应该忽略
        adapter._process_qq_message(msg_d)
        assert len(calls) == 1  # 没有增加

    def test_different_message_ids_both_processed(self) -> None:
        adapter = QQAdapter(
            config={"qq_app_id": "", "qq_app_secret": ""},
            workspace_root=Path(tempfile.gettempdir()),
        )
        received: list[IncomingMessage] = []

        def cb(msg: IncomingMessage) -> None:
            received.append(msg)

        adapter.on_message(cb)

        adapter._process_qq_message(
            {"author": {"user_openid": "1"}, "content": "a", "id": "id1", "timestamp": 1}
        )
        adapter._process_qq_message(
            {"author": {"user_openid": "1"}, "content": "b", "id": "id2", "timestamp": 2}
        )
        assert len(received) == 2


class TestQQLifecycle:
    """测试 QQ 适配器启停。"""

    def test_start_stop_clean(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            adapter = QQAdapter(
                config={"qq_app_id": "", "qq_app_secret": ""},
                workspace_root=Path(td),
            )
            # Mock _run_ws_loop so it doesn't actually connect
            with patch.object(adapter, '_run_ws_loop'):
                adapter.start()
                assert adapter.running is True
                adapter.stop()
                assert adapter.running is False

    def test_stop_when_not_running(self) -> None:
        adapter = QQAdapter(
            config={"qq_app_id": "", "qq_app_secret": ""},
            workspace_root=Path(tempfile.gettempdir()),
        )
        adapter.stop()  # 不应报错
        assert adapter.running is False


class TestQQSendMessage:
    """测试 QQ 发送消息（mock）。"""

    def test_send_without_app_id_fails(self) -> None:
        adapter = QQAdapter(
            config={"qq_app_id": "", "qq_app_secret": ""},
            workspace_root=Path(tempfile.gettempdir()),
        )
        msg = OutgoingMessage(channel="qq", user_id="777", content="hello")
        result = adapter.send_message("777", msg)
        assert result is False


class TestQQTokenCaching:
    """测试 QQ token 缓存。"""

    def test_token_not_fetched_until_needed(self) -> None:
        adapter = QQAdapter(
            config={"qq_app_id": "id", "qq_app_secret": "secret"},
            workspace_root=Path(tempfile.gettempdir()),
        )
        assert adapter._access_token is None
        assert adapter._token_expires_at == 0


class TestQQIntents:
    """测试 intents 计算。"""

    def test_intents_guild_messages(self) -> None:
        adapter = QQAdapter(
            config={"qq_app_id": "id", "qq_app_secret": "secret"},
            workspace_root=Path(tempfile.gettempdir()),
        )
        intents = adapter._intents_for_qq()
        # (1 << 25) | (1 << 30) | (1 << 12) = C2C_GROUP_AT + PUBLIC_GUILD + DIRECT
        assert intents == (1 << 25) | (1 << 30) | (1 << 12)


class TestQQWebSocketMessageHandling:
    """测试 WebSocket 消息处理。"""

    def test_handle_message_create(self) -> None:
        adapter = QQAdapter(
            config={"qq_app_id": "id", "qq_app_secret": "secret"},
            workspace_root=Path(tempfile.gettempdir()),
        )
        received: list[IncomingMessage] = []

        def cb(msg: IncomingMessage) -> None:
            received.append(msg)

        adapter.on_message(cb)

        raw = json.dumps({
            "op": 0,
            "t": "MESSAGE_CREATE",
            "d": {
                "id": "msg123",
                "channel_id": "ch1",
                "guild_id": "g1",
                "content": "test message",
                "author": {"user_openid": "user1"},
                "timestamp": "2024-01-01T00:00:00+08:00",
            }
        })
        adapter._handle_ws_message(raw)
        assert len(received) == 1
        assert received[0].content == "test message"
        assert received[0].user_id == "user1"

    def test_handle_ready_saves_session(self) -> None:
        adapter = QQAdapter(
            config={"qq_app_id": "id", "qq_app_secret": "secret"},
            workspace_root=Path(tempfile.gettempdir()),
        )
        raw = json.dumps({
            "op": 0,
            "t": "READY",
            "d": {
                "session_id": "sess_abc",
                "heartbeat_interval": 30000,
            }
        })
        adapter._handle_ws_message(raw)
        assert adapter._session_id == "sess_abc"
        assert adapter._heartbeat_interval == 30000.0

    def test_handle_hello_starts_heartbeat(self) -> None:
        adapter = QQAdapter(
            config={"qq_app_id": "id", "qq_app_secret": "secret"},
            workspace_root=Path(tempfile.gettempdir()),
        )
        raw = json.dumps({
            "op": 1,
            "d": {
                "heartbeat_interval": 30000,
            }
        })
        adapter._heartbeat_interval = 0
        adapter._heartbeat_thread = None
        adapter._handle_ws_message(raw)
        assert adapter._heartbeat_interval == 30000.0

    def test_invalid_session_triggers_reconnect(self) -> None:
        adapter = QQAdapter(
            config={"qq_app_id": "id", "qq_app_secret": "secret"},
            workspace_root=Path(tempfile.gettempdir()),
        )
        adapter._stop_event.clear()
        raw = json.dumps({
            "op": 0,
            "t": "INVALID_SESSION",
            "d": {},
        })
        adapter._handle_ws_message(raw)
        assert adapter._session_id is None
        assert adapter._stop_event.is_set()