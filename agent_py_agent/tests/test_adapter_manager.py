"""LLM: 测试通道管理器 — 注册、路由、启停。

给人看的解释：
测试 ChannelManager 的各项能力：适配器注册/查询/列表、启停管理。
gateway HTTP 调用使用 mock，不实际发起网络请求。
"""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent_py_agent.agent.adapter.base import BaseChannelAdapter
from agent_py_agent.agent.adapter.delivery import PendingGatewayReply
from agent_py_agent.agent.adapter.manager import (
    ChannelManager,
    GatewayAskSubmission,
    _gateway_ask_payload,
)
from agent_py_agent.agent.adapter.protocol import IncomingMessage, OutgoingMessage


class DummyAdapter(BaseChannelAdapter):
    """测试用伪适配器。"""

    adapter_name = "dummy"

    def __init__(self) -> None:
        super().__init__({})
        self._start_called = False
        self._stop_called = False
        self._send_calls: list[tuple[str, OutgoingMessage]] = []

    def start(self) -> None:
        self._running = True
        self._start_called = True

    def stop(self) -> None:
        self._running = False
        self._stop_called = True

    def send_message(self, user_id: str, message: OutgoingMessage) -> bool:
        self._send_calls.append((user_id, message))
        return True


def test_gateway_payload_preserves_structured_group_identity() -> None:
    message = IncomingMessage(
        channel="feishu",
        user_id="ou_sender",
        content="开始工作",
        message_id="om_1",
        conversation_id="oc_group:thread-1",
        metadata={"feishu_chat_type": "group", "feishu_chat_id": "oc_group"},
    )

    payload = _gateway_ask_payload(message)

    assert payload["metadata"]["channel_chat_type"] == "group"
    assert payload["metadata"]["channel_chat_id"] == "oc_group"


class TestChannelManagerRegistration:
    """测试通道管理器的适配器注册接口。"""

    def test_register_and_get(self) -> None:
        manager = ChannelManager()
        dummy = DummyAdapter()
        manager.register_adapter(dummy)
        assert manager.get_adapter("dummy") is dummy

    def test_list_adapters(self) -> None:
        manager = ChannelManager()
        a1 = DummyAdapter()
        a1.adapter_name = "one"
        a2 = DummyAdapter()
        a2.adapter_name = "two"
        manager.register_adapter(a1)
        manager.register_adapter(a2)
        assert sorted(manager.list_adapters()) == ["one", "two"]

    def test_get_nonexistent_returns_none(self) -> None:
        manager = ChannelManager()
        assert manager.get_adapter("notexist") is None

    def test_replace_existing_adapter(self) -> None:
        manager = ChannelManager()
        a1 = DummyAdapter()
        a1.adapter_name = "dup"
        a2 = DummyAdapter()
        a2.adapter_name = "dup"
        manager.register_adapter(a1)
        manager.register_adapter(a2)
        assert manager.get_adapter("dup") is a2


class TestChannelManagerLifecycle:
    """测试通道管理器启停。"""

    def test_start_all(self) -> None:
        manager = ChannelManager()
        a = DummyAdapter()
        manager.register_adapter(a)
        manager.start_all()
        assert a._start_called is True
        assert a.running is True
        status = manager.runtime_channel_statuses()[0]
        assert status["health"]["state"] == "healthy"
        a.stop()

    def test_stop_all(self) -> None:
        manager = ChannelManager()
        a = DummyAdapter()
        manager.register_adapter(a)
        a.start()
        manager.stop_all()
        assert a._stop_called is True
        assert a.running is False
        status = manager.runtime_channel_statuses()[0]
        assert status["health"]["state"] == "stopped"

    def test_start_all_then_stop_all(self) -> None:
        manager = ChannelManager()
        for name in ("x", "y"):
            a = DummyAdapter()
            a.adapter_name = name
            manager.register_adapter(a)
        manager.start_all()
        manager.stop_all()
        for name in manager.list_adapters():
            adapter = manager.get_adapter(name)
            assert adapter is not None
            assert adapter.running is False


class TestChannelManagerActiveChannel:
    """测试活跃通道查询。"""

    def test_no_session_file_returns_none(self) -> None:
        manager = ChannelManager()
        manager.session_channel_file = Path("/nonexistent/file.json")
        assert manager.get_active_channel("user_1") is None

    def test_update_and_query_active_channel(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            manager = ChannelManager()
            session_file = Path(td) / "sessions.json"
            manager.session_channel_file = session_file

            manager.update_active_channel("user_1", "feishu")
            assert manager.get_active_channel("user_1") == "feishu"

            manager.update_active_channel("user_1", "qq")
            assert manager.get_active_channel("user_1") == "qq"


class TestChannelManagerRouteMessage:
    """测试消息路由到 gateway。"""

    def test_route_calls_gateway_ask(self) -> None:
        manager = ChannelManager(gateway_port=8420)

        dummy = DummyAdapter()
        dummy.adapter_name = "feishu"
        manager.register_adapter(dummy)

        msg = IncomingMessage(
            channel="feishu",
            user_id="ou_123",
            content="hello",
            message_id="m1",
            conversation_id="oc_chat1",
        )

        with patch("urllib.request.urlopen") as mock_urlopen:
            # Mock gateway /ask 响应
            mock_resp = MagicMock()
            mock_resp.__enter__ = MagicMock(
                return_value=MagicMock(
                    read=MagicMock(return_value=b'{"request_id": "req_1"}')
                )
            )
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            assert manager.route_message(msg) is True
            submitted = mock_urlopen.call_args_list[0].args[0]
            body = __import__("json").loads(submitted.data.decode("utf-8"))
            assert body["conversation_id"] == "oc_chat1"
            assert body["metadata"]["message_id"] == "m1"
            pending = manager._reply_delivery.store.pending()
            assert [item.request_id for item in pending] == ["req_1"]

    def test_control_command_bypasses_ordinary_ask_queue(self) -> None:
        manager = ChannelManager(gateway_port=8420)
        dummy = DummyAdapter()
        dummy.adapter_name = "feishu"
        manager.register_adapter(dummy)
        msg = IncomingMessage(
            channel="feishu",
            user_id="ou_123",
            content="/btw 先确认事实",
            message_id="m-control",
            conversation_id="oc_chat1",
        )

        with patch.object(
            manager,
            "_submit_gateway_control",
            return_value={"ok": True, "message": "已补充到当前任务。", "request_id": "req-live"},
        ) as submit_control, patch.object(manager, "_submit_gateway_ask") as submit_ask, patch.object(
            manager, "_send_gateway_reply", return_value=True
        ) as send_reply:
            assert manager.route_message(msg) is True

        submit_control.assert_called_once_with(msg)
        submit_ask.assert_not_called()
        send_reply.assert_called_once_with(msg, "req-live", "已补充到当前任务。")
        assert manager._reply_delivery.store.pending() == []

    def test_active_turn_ordinary_input_reuses_live_delivery_instead_of_queueing_again(self) -> None:
        manager = ChannelManager(gateway_port=8420)
        dummy = DummyAdapter()
        dummy.adapter_name = "feishu"
        manager.register_adapter(dummy)
        msg = IncomingMessage(
            channel="feishu",
            user_id="ou_123",
            content="顺便回答一句，原任务继续",
            message_id="m-steer",
            conversation_id="oc_chat1",
        )

        with patch.object(
            manager,
            "_submit_gateway_ask",
            return_value=GatewayAskSubmission("req-live", "steered"),
        ), patch.object(dummy, "send_progress_placeholder") as placeholder:
            assert manager.route_message(msg) is True

        placeholder.assert_not_called()
        assert manager._reply_delivery.store.pending() == []

    def test_stop_discards_old_pending_reply_and_progress_placeholder(self) -> None:
        manager = ChannelManager(gateway_port=8420)
        dummy = DummyAdapter()
        dummy.adapter_name = "feishu"
        manager.register_adapter(dummy)
        manager._reply_delivery.enqueue(
            PendingGatewayReply(
                "req-live",
                "feishu",
                "ou_123",
                "m-original",
                conversation_id="oc_chat1",
                progress_handle="typing-handle",
            )
        )
        msg = IncomingMessage(
            channel="feishu",
            user_id="ou_123",
            content="/stop",
            message_id="m-stop",
            conversation_id="oc_chat1",
        )

        with patch.object(
            manager,
            "_submit_gateway_control",
            return_value={
                "kind": "stop",
                "ok": True,
                "message": "已收到停止请求。",
                "request_id": "req-live",
            },
        ), patch.object(dummy, "clear_progress_placeholder") as clear, patch.object(
            manager, "_send_gateway_reply", return_value=True
        ):
            assert manager.route_message(msg) is True

        assert manager._reply_delivery.store.pending() == []
        assert manager._reply_delivery.store.was_sent("req-live") is True
        clear.assert_called_once_with("ou_123", "typing-handle")

    def test_removed_btw_clear_is_not_sent_to_model(self) -> None:
        manager = ChannelManager(gateway_port=8420)
        dummy = DummyAdapter()
        dummy.adapter_name = "feishu"
        manager.register_adapter(dummy)
        msg = IncomingMessage(
            channel="feishu",
            user_id="ou_123",
            content="/btw-clear",
            message_id="m-control",
            conversation_id="oc_chat1",
        )

        with patch.object(
            manager,
            "_submit_gateway_control",
            return_value={"ok": False, "message": "用法：/btw 你的补充要求"},
        ), patch.object(manager, "_submit_gateway_ask") as submit_ask, patch.object(
            manager, "_send_gateway_reply", return_value=True
        ):
            assert manager.route_message(msg) is True

        submit_ask.assert_not_called()

    def test_route_falls_back_when_adapter_not_found(self) -> None:
        manager = ChannelManager()
        # 不注册任何适配器

        msg = IncomingMessage(
            channel="feishu",
            user_id="ou_123",
            content="hello",
            message_id="m1",
        )

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.__enter__ = MagicMock(
                return_value=MagicMock(
                    read=MagicMock(return_value=b'{"request_id": "req_1"}')
                )
            )
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            result = manager.route_message(msg)
            assert result is False

    def test_route_sends_placeholder_then_finalizes_with_handle(self) -> None:
        """提交后发占位拿句柄,完成后把句柄+结果交给 finalize_response 原地更新(typing 流程接线)。"""
        manager = ChannelManager(gateway_port=8420)
        dummy = DummyAdapter()
        dummy.adapter_name = "feishu"
        manager.register_adapter(dummy)
        msg = IncomingMessage(channel="feishu", user_id="ou_123", content="hi", message_id="m1")

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.__enter__ = MagicMock(
                return_value=MagicMock(read=MagicMock(return_value=b'{"request_id": "req_1"}'))
            )
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            with patch.object(manager, "_poll_gateway_once", return_value="答案"), \
                 patch.object(dummy, "send_progress_placeholder", return_value="om_card") as ph, \
                 patch.object(dummy, "finalize_response", return_value=True) as fin:
                assert manager.route_message(msg) is True
                assert manager._reply_delivery.run_once() == 1
                ph.assert_called_once_with("ou_123", "m1")  # 提交后立即给"处理中"反馈(带消息id供贴reaction)
                fin.assert_called_once()
                # 把占位句柄 + 最终结果交给 finalize_response 原地更新
                assert fin.call_args.args[1] == "om_card"
                assert fin.call_args.args[2].content == "答案"

    def test_final_reply_never_exposes_internal_tool_protocol_or_host_path(self) -> None:
        manager = ChannelManager(gateway_port=8420)
        dummy = DummyAdapter()
        dummy.adapter_name = "feishu"
        manager.register_adapter(dummy)
        msg = IncomingMessage(
            channel="feishu",
            user_id="ou_123",
            content="发我",
            message_id="m1",
        )
        raw = (
            "报告已经整理好，文件位于 /root/private/report.pdf。\n"
            '[TOOL_CALL]\n{"tool":"send_message","target":"ou_123"}\n[/TOOL_CALL]'
        )

        with patch.object(dummy, "finalize_response", return_value=True) as finalize:
            assert manager._send_gateway_reply(msg, "gw-1", raw, "typing") is True

        outgoing = finalize.call_args.args[2]
        assert outgoing.content == "报告已经整理好，文件位于 report.pdf。"
        assert "TOOL_CALL" not in outgoing.content
        assert "/root/private" not in outgoing.content
        assert outgoing.metadata["projection_status"] == "internal_protocol_removed"

    def test_progress_and_final_use_distinct_stable_provider_idempotency_keys(self) -> None:
        manager = ChannelManager(gateway_port=8420)
        dummy = DummyAdapter()
        dummy.adapter_name = "feishu"
        manager.register_adapter(dummy)
        pending = PendingGatewayReply(
            request_id="req-7",
            channel="feishu",
            user_id="ou_7",
            message_id="om_7",
            conversation_id="oc_7",
            progress_cursor=3,
        )

        with patch.object(dummy, "finalize_response", return_value=True) as finalize:
            assert manager._deliver_gateway_progress(pending, "第一批进度") is True
            assert manager._deliver_gateway_reply(pending, "最终回复") is True
            assert manager._deliver_gateway_progress(pending, "第一批进度") is True

        keys = [
            call.args[2].metadata["delivery_idempotency_key"]
            for call in finalize.call_args_list
        ]
        assert keys == [
            "gateway-reply:om_7:req-7:progress:3",
            "gateway-reply:om_7:req-7:final",
            "gateway-reply:om_7:req-7:progress:3",
        ]
        assert keys[0] != keys[1]

    def test_maybe_download_media_injects_path(self) -> None:
        """入站带 media → fetch_media_to 下载,content 注入路径(供 agent 看图/读文件)。"""
        from pathlib import Path
        manager = ChannelManager(gateway_port=8420)
        dummy = DummyAdapter()
        dummy.adapter_name = "feishu"
        dummy.workspace_root = Path("/tmp/wsroot")
        dummy.fetch_media_to = MagicMock(return_value="om_1-img.png")
        manager.register_adapter(dummy)
        msg = IncomingMessage(channel="feishu", user_id="ou_1", content="[图片]",
                              message_id="om_1", metadata={"media": {"image_key": "k"}})
        manager._maybe_download_media(msg)
        dummy.fetch_media_to.assert_called_once()
        assert "已下载到" in msg.content

    def test_maybe_download_media_skips_without_media(self) -> None:
        manager = ChannelManager(gateway_port=8420)
        dummy = DummyAdapter()
        dummy.adapter_name = "feishu"
        dummy.fetch_media_to = MagicMock()
        manager.register_adapter(dummy)
        msg = IncomingMessage(channel="feishu", user_id="ou_1", content="纯文本", message_id="om_1")
        manager._maybe_download_media(msg)
        dummy.fetch_media_to.assert_not_called()  # 无 media → 跳过
        assert msg.content == "纯文本"


class TestChannelManagerDurableDelivery:
    """通道回调只提交；长结果由可恢复 worker 最终且只回送一次。"""

    @staticmethod
    def _message() -> IncomingMessage:
        return IncomingMessage(
            channel="feishu",
            user_id="ou_late",
            content="做一个长任务",
            message_id="om_late",
            conversation_id="oc_late",
        )

    def test_route_returns_immediately_and_worker_keeps_polling_past_sixty_misses(self, tmp_path: Path) -> None:
        manager = ChannelManager(
            gateway_port=8420,
            delivery_state_dir=tmp_path / "deliveries",
            delivery_poll_interval=0.01,
        )
        dummy = DummyAdapter()
        dummy.adapter_name = "feishu"
        manager.register_adapter(dummy)
        delivered = threading.Event()
        poll_count = 0

        def late_poll(_pending: PendingGatewayReply, interval: float) -> str | None:
            nonlocal poll_count
            assert interval == 0.0
            poll_count += 1
            return "超过旧等待窗后的真实答案" if poll_count > 65 else None

        def finalize(_user_id: str, _handle: str, outgoing: OutgoingMessage) -> bool:
            assert outgoing.content == "超过旧等待窗后的真实答案"
            delivered.set()
            return True

        with patch.object(
            manager,
            "_submit_gateway_ask",
            return_value=GatewayAskSubmission("req_late"),
        ), \
             patch.object(manager, "_poll_gateway_once", side_effect=late_poll), \
             patch.object(manager._reply_delivery, "_poll_progress", return_value=([], 0)), \
             patch.object(dummy, "finalize_response", side_effect=finalize) as finalizer:
            manager.start_all()
            started = time.monotonic()
            assert manager.route_message(self._message()) is True
            assert time.monotonic() - started < 0.1
            assert delivered.wait(2.0)
            # 旧实现约 60 次轮询后发一条假超时并永远丢掉真结果；现在没有这个终止窗。
            assert poll_count > 60
            assert finalizer.call_count == 1
            time.sleep(0.05)
            assert finalizer.call_count == 1
            manager.stop_all()

    def test_reply_poll_uses_inbound_owner_identity_and_only_returns_public_error(self) -> None:
        manager = ChannelManager(gateway_port=8420)
        pending = PendingGatewayReply(
            "req_failed",
            "feishu",
            "ou_private",
            "om_1",
            conversation_id="oc_private",
        )
        response = MagicMock()
        response.status = 200
        response.read.return_value = (
            b'{"ok":false,"error_code":"GATEWAY_WORKER_UNHANDLED_ERROR",'
            b'"error":"\\u4efb\\u52a1\\u5904\\u7406\\u5931\\u8d25\\uff0c\\u8bf7\\u7a0d\\u540e\\u91cd\\u8bd5\\u3002"}'
        )
        response.__enter__.return_value = response
        response.__exit__.return_value = False

        with patch("urllib.request.urlopen", return_value=response) as urlopen:
            text = manager._poll_gateway_once(pending, interval=0.0)

        request = urlopen.call_args.args[0]
        assert request.get_header("X-user-id") == "ou_private"
        assert request.get_header("X-channel") == "feishu"
        assert text == "错误: 任务处理失败，请稍后重试。"
        assert "GATEWAY_WORKER_UNHANDLED_ERROR" not in text

    def test_restart_recovers_pending_reply_without_resubmitting_or_resending(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "deliveries"
        first = ChannelManager(delivery_state_dir=state_dir, delivery_poll_interval=0.01)
        first_adapter = DummyAdapter()
        first_adapter.adapter_name = "feishu"
        first.register_adapter(first_adapter)
        with patch.object(
            first,
            "_submit_gateway_ask",
            return_value=GatewayAskSubmission("req_restart"),
        ) as submit:
            # 模拟已提交后进程退出：未 start 生命周期，所以 worker 尚未消费，但路由信息已原子落盘。
            assert first.route_message(self._message()) is True
            submit.assert_called_once()
        assert len(first._reply_delivery.store.pending()) == 1

        second = ChannelManager(delivery_state_dir=state_dir, delivery_poll_interval=0.01)
        second_adapter = DummyAdapter()
        second_adapter.adapter_name = "feishu"
        second.register_adapter(second_adapter)
        delivered = threading.Event()
        with patch.object(second, "_poll_gateway_once", return_value="恢复后的答案") as poll, \
             patch.object(second_adapter, "finalize_response", return_value=True) as finalizer:
            finalizer.side_effect = lambda *_args: delivered.set() or True
            second.start_all()
            assert delivered.wait(1.0)
            second.stop_all()
            poll.assert_called()
            finalizer.assert_called_once()
            outgoing = finalizer.call_args.args[2]
            assert outgoing.metadata["gateway_request_id"] == "req_restart"
        assert second._reply_delivery.store.pending() == []

        # sent receipt 跨重启去重；第三个进程不会再轮询，更不会再发送。
        third = ChannelManager(delivery_state_dir=state_dir, delivery_poll_interval=0.01)
        third_adapter = DummyAdapter()
        third_adapter.adapter_name = "feishu"
        third.register_adapter(third_adapter)
        with patch.object(third, "_poll_gateway_once") as poll, \
             patch.object(third_adapter, "finalize_response") as finalizer:
            third.start_all()
            time.sleep(0.05)
            third.stop_all()
            poll.assert_not_called()
            finalizer.assert_not_called()
