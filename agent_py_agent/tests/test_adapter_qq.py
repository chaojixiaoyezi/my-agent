"""LLM: 测试 QQ 适配器 — 消息格式转换、轮询去重、发送逻辑。

给人看的解释：
测试 QQAdapter 的各项能力：消息解析、轮询去重、token 缓存。
不实际发起 QQ HTTP 请求，所有外部调用均 mock。
"""

from __future__ import annotations

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

        payload = {
            "d": {
                "author": {"id": "123"},
                "content": "hello",
                "msg_id": "dup_id",
                "timestamp": 1234,
            }
        }

        # 第一次 — 应该分发
        adapter._process_qq_message(payload)
        assert len(calls) == 1

        # 第二次同样 id — 应该忽略
        adapter._process_qq_message(payload)
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
            {"d": {"author": {"id": "1"}, "content": "a", "msg_id": "id1", "timestamp": 1}}
        )
        adapter._process_qq_message(
            {"d": {"author": {"id": "1"}, "content": "b", "msg_id": "id2", "timestamp": 2}}
        )
        assert len(received) == 2


class TestQQLifecycle:
    """测试 QQ 适配器启停。"""

    def test_start_stop_clean(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            adapter = QQAdapter(
                config={"qq_app_id": "", "qq_app_secret": ""},
                workspace_root=Path(td),
                poll_interval=60.0,
            )
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
