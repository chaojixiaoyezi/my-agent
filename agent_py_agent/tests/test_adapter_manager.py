"""LLM: 测试通道管理器 — 注册、路由、启停。

给人看的解释：
测试 ChannelManager 的各项能力：适配器注册/查询/列表、启停管理。
gateway HTTP 调用使用 mock，不实际发起网络请求。
"""

from __future__ import annotations

import tempfile
import threading
import time
import urllib.error
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.adapter.base import BaseChannelAdapter
from agent_py_agent.agent.adapter.delivery import (
    GatewayControlReceiptResult,
    GatewayReplyDeliveryStore,
    GatewayReplyDeliveryWorker,
    GatewayReplyQuarantineError,
    GatewayReplyReceiptCallbacks,
    PendingGatewayReply,
)
from agent_py_agent.agent.adapter.manager import (
    ChannelManager,
    GatewayAskSubmission,
    _gateway_ask_payload,
    _gateway_submission_watch_kind,
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


class ProviderIdempotentDummyAdapter(DummyAdapter):
    provider_idempotent_delivery = True

    def __init__(
        self,
        provider_messages: dict[str, tuple[str, str]],
        attempts: list[str],
    ) -> None:
        super().__init__()
        self.provider_messages = provider_messages
        self.attempts = attempts

    def finalize_response(
        self,
        user_id: str,
        handle: str,
        message: OutgoingMessage,
    ) -> bool:
        del handle
        key = str(message.metadata.get("delivery_idempotency_key") or "")
        self.attempts.append(key)
        self.provider_messages.setdefault(key, (user_id, message.content))
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

    def test_start_all_sets_lifecycle_flag_only_after_adapter_start(self) -> None:
        manager = ChannelManager()
        adapter = DummyAdapter()
        observed_flags: list[bool] = []
        original_start = adapter.start

        def start() -> None:
            observed_flags.append(manager._lifecycle_started)
            original_start()

        adapter.start = start
        manager.register_adapter(adapter)

        manager.start_all()

        assert observed_flags == [False]
        assert manager._lifecycle_started is True
        manager.stop_all()

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
            # 回调线程只落 durable ingress，不在这里做媒体/Gateway/通道 IO。
            mock_urlopen.assert_not_called()
            assert manager._delivery_worker.run_once() == 1
            submitted = mock_urlopen.call_args_list[0].args[0]
            body = __import__("json").loads(submitted.data.decode("utf-8"))
            assert body["conversation_id"] == "oc_chat1"
            assert body["metadata"]["message_id"] == "m1"
            pending = manager._reply_delivery.store.pending()
            assert [item.request_id for item in pending] == ["req_1"]

    def test_gateway_ingress_returns_control_without_reply_queue(self) -> None:
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
            "_submit_gateway_payload",
            return_value=GatewayAskSubmission(
                "req-live",
                "control",
                kind="steer",
                ok=True,
                message="已补充到当前任务。",
            ),
        ) as submit_ask, patch.object(
            manager, "_send_gateway_reply", return_value=True
        ) as send_reply:
            assert manager.route_message(msg) is True
            assert manager._delivery_worker.run_once() == 1

        submit_ask.assert_called_once()
        assert submit_ask.call_args.args[1].content == msg.content
        send_reply.assert_called_once_with(msg, "req-live", "已补充到当前任务。")
        assert manager._reply_delivery.store.pending() == []

    def test_active_turn_ordinary_input_uses_durable_input_receipt_watcher(self) -> None:
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
            "_submit_gateway_payload",
            return_value=GatewayAskSubmission(
                "stable-input-1",
                "delivery_unknown",
                disposition="active_turn_input",
                delivery_status="unknown",
                input_state="active_pending",
                target_turn_id="req-live",
            ),
        ), patch.object(
            dummy,
            "send_progress_placeholder",
            return_value="typing-steer",
        ) as placeholder:
            assert manager.route_message(msg) is True
            assert manager._delivery_worker.run_once() == 1

        placeholder.assert_called_once_with("ou_123", "m-steer")
        pending = manager._reply_delivery.store.pending()
        assert len(pending) == 1
        assert pending[0].request_id == "stable-input-1"
        assert pending[0].watch_kind == "input_receipt"

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
            "_submit_gateway_payload",
            return_value=GatewayAskSubmission(
                "req-live",
                "control",
                kind="stop",
                ok=True,
                message="已收到停止请求。",
            ),
        ), patch.object(dummy, "clear_progress_placeholder") as clear, patch.object(
            manager, "_send_gateway_reply", return_value=True
        ) as send_reply:
            assert manager.route_message(msg) is True
            assert manager._delivery_worker.run_once() == 1

        assert manager._reply_delivery.store.pending() == []
        assert manager._reply_delivery.store.was_sent("req-live") is True
        clear.assert_called_once_with("ou_123", "typing-handle")
        send_reply.assert_not_called()

    def test_stop_without_live_turn_returns_structured_rejection(self) -> None:
        manager = ChannelManager(gateway_port=8420)
        dummy = DummyAdapter()
        dummy.adapter_name = "feishu"
        manager.register_adapter(dummy)
        msg = IncomingMessage(
            channel="feishu",
            user_id="ou_123",
            content="/stop",
            message_id="m-stop-idle",
            conversation_id="oc_chat1",
        )

        with patch.object(
            manager,
            "_submit_gateway_payload",
            return_value=GatewayAskSubmission(
                "",
                "control",
                kind="stop",
                ok=False,
                message="当前没有运行中的内容，无需停止。",
            ),
        ), patch.object(manager, "_send_gateway_reply", return_value=True) as send_reply:
            assert manager.route_message(msg) is True
            assert manager._delivery_worker.run_once() == 1

        send_reply.assert_called_once_with(
            msg,
            "control-m-stop-idle",
            "当前没有运行中的内容，无需停止。",
        )

    def test_stop_terminal_unknown_closes_as_durable_unknown(
        self,
        tmp_path: Path,
    ) -> None:
        manager = ChannelManager(
            gateway_port=8420,
            delivery_state_dir=tmp_path / "delivery",
        )
        dummy = DummyAdapter()
        dummy.adapter_name = "feishu"
        manager.register_adapter(dummy)
        msg = IncomingMessage(
            channel="feishu",
            user_id="ou_123",
            content="/stop",
            message_id="m-stop-unknown",
            conversation_id="oc_chat1",
        )
        submission = GatewayAskSubmission(
            request_id="",
            status="control",
            kind="stop",
            ok=False,
            message="停止操作的最终结果无法确认。",
            delivery_status="unknown",
            operation_id="gwctl-msg-stop-unknown",
            receipt_id="gwctl-msg-stop-unknown",
            control_state="terminal_unknown",
        )

        with patch.object(
            manager,
            "_submit_gateway_payload",
            return_value=submission,
        ), patch.object(
            dummy,
            "send_progress_placeholder",
            return_value="typing-stop",
        ), patch.object(
            manager._reply_delivery,
            "_poll_control_receipt",
            return_value=GatewayControlReceiptResult(
                control_state="terminal_unknown",
                delivery_status="unknown",
                control_kind="stop",
            ),
        ), patch.object(dummy, "clear_progress_placeholder") as clear, patch.object(
            manager,
            "_discard_interrupted_reply",
        ) as discard:
            assert manager.route_message(msg) is True
            assert manager._delivery_worker.run_once() == 1

        discard.assert_not_called()
        clear.assert_called_once_with("ou_123", "typing-stop")
        assert manager._reply_delivery.store.pending() == []
        receipt = GatewayReplyDeliveryStore(
            tmp_path / "delivery"
        ).terminal_receipt("gwctl-msg-stop-unknown")
        assert receipt is not None
        assert receipt["request_id"] == ""
        assert receipt["disposition"] == "unknown"
        assert receipt["reason"] == "control_stop_terminal_unknown"

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
            "_submit_gateway_payload",
            return_value=GatewayAskSubmission(
                "",
                "control",
                kind="steer",
                ok=False,
                message="用法：/btw 你的补充要求",
            ),
        ) as submit_ask, patch.object(
            manager, "_send_gateway_reply", return_value=True
        ):
            assert manager.route_message(msg) is True
            assert manager._delivery_worker.run_once() == 1

        submit_ask.assert_called_once()
        assert submit_ask.call_args.args[1].content == msg.content

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
                assert manager._delivery_worker.run_once() == 2
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
            "_submit_gateway_payload",
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

    def test_restart_recovers_pre_post_ingress_then_deduplicates_completed_delivery(
        self,
        tmp_path: Path,
    ) -> None:
        state_dir = tmp_path / "deliveries"
        first = ChannelManager(delivery_state_dir=state_dir, delivery_poll_interval=0.01)
        first_adapter = DummyAdapter()
        first_adapter.adapter_name = "feishu"
        first.register_adapter(first_adapter)
        with patch.object(
            first,
            "_submit_gateway_payload",
            return_value=GatewayAskSubmission("req_restart"),
        ) as submit:
            # 模拟回调落盘后、worker POST 前进程退出。
            assert first.route_message(self._message()) is True
            submit.assert_not_called()
        assert len(first._delivery_worker.store.pending()) == 1
        assert first._reply_delivery.store.pending() == []

        second = ChannelManager(delivery_state_dir=state_dir, delivery_poll_interval=0.01)
        second_adapter = DummyAdapter()
        second_adapter.adapter_name = "feishu"
        second.register_adapter(second_adapter)
        delivered = threading.Event()
        with patch.object(
            second,
            "_submit_gateway_payload",
            return_value=GatewayAskSubmission("req_restart"),
        ) as submit, patch.object(second, "_poll_gateway_once", return_value="恢复后的答案") as poll, \
             patch.object(second_adapter, "finalize_response", return_value=True) as finalizer:
            finalizer.side_effect = lambda *_args: delivered.set() or True
            second.start_all()
            assert delivered.wait(1.0)
            second.stop_all()
            submit.assert_called_once()
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
        with patch.object(third, "_poll_gateway_once") as poll, patch.object(
            third_adapter, "finalize_response"
        ) as finalizer, patch.object(third, "_submit_gateway_payload") as submit:
            third.start_all()
            time.sleep(0.05)
            third.stop_all()
            poll.assert_not_called()
            finalizer.assert_not_called()
            submit.assert_not_called()

    @staticmethod
    def _pending(*, watch_kind: str = "input_receipt") -> PendingGatewayReply:
        return PendingGatewayReply(
            request_id="stable-input-1",
            channel="feishu",
            user_id="ou-1",
            message_id="om-1",
            conversation_id="oc-1",
            progress_handle="typing-1",
            watch_kind=watch_kind,
            created_at=10.0,
        )

    def test_submission_keeps_gateway_input_disposition_fields(self) -> None:
        manager = ChannelManager(gateway_port=8420)
        msg = IncomingMessage(
            channel="feishu",
            user_id="ou-1",
            content="补充要求",
            message_id="om-1",
            conversation_id="oc-1",
        )
        response = MagicMock()
        response.read.return_value = (
            b'{"request_id":"stable-input-1","status":"delivery_unknown",'
            b'"disposition":"active_turn_input","delivery_status":"unknown",'
            b'"input_state":"active_pending","target_turn_id":"turn-9"}'
        )
        response.__enter__.return_value = response
        response.__exit__.return_value = False

        with patch("urllib.request.urlopen", return_value=response):
            submission = manager._submit_gateway_ask(msg)

        assert submission == GatewayAskSubmission(
            request_id="stable-input-1",
            status="delivery_unknown",
            disposition="active_turn_input",
            delivery_status="unknown",
            input_state="active_pending",
            target_turn_id="turn-9",
        )

    def test_submission_keeps_independent_control_receipt_identity(self) -> None:
        manager = ChannelManager(gateway_port=8420)
        msg = IncomingMessage(
            channel="feishu",
            user_id="ou-1",
            content="/btw 补充要求",
            message_id="om-control-1",
            conversation_id="oc-1",
        )
        response = MagicMock()
        response.read.return_value = (
            b'{"request_id":"turn-9","status":"control","kind":"steer",'
            b'"delivery_status":"unknown","operation_id":"gwctl-msg-abc",'
            b'"receipt_id":"gwctl-msg-abc","control_state":"completed"}'
        )
        response.__enter__.return_value = response
        response.__exit__.return_value = False

        with patch("urllib.request.urlopen", return_value=response):
            submission = manager._submit_gateway_ask(msg)

        assert submission.request_id == "turn-9"
        assert submission.operation_id == "gwctl-msg-abc"
        assert submission.receipt_id == "gwctl-msg-abc"
        assert submission.control_state == "completed"

    def test_unknown_btw_handoff_is_keyed_by_operation_not_target_turn(
        self,
        tmp_path: Path,
    ) -> None:
        manager = ChannelManager(
            gateway_port=8420,
            delivery_state_dir=tmp_path / "delivery",
        )
        dummy = DummyAdapter()
        dummy.adapter_name = "feishu"
        manager.register_adapter(dummy)
        msg = IncomingMessage(
            channel="feishu",
            user_id="ou-1",
            content="/btw 补充要求",
            message_id="om-control-1",
            conversation_id="oc-1",
            metadata={"feishu_chat_type": "group", "feishu_chat_id": "oc-group"},
        )
        submission = GatewayAskSubmission(
            request_id="turn-9",
            status="control",
            kind="steer",
            ok=False,
            message="控制操作的最终结果无法确认。",
            delivery_status="unknown",
            operation_id="gwctl-msg-operation-1",
            receipt_id="gwctl-msg-operation-1",
            control_state="completed",
        )

        with patch.object(
            manager,
            "_submit_gateway_payload",
            return_value=submission,
        ), patch.object(
            dummy,
            "send_progress_placeholder",
            return_value="typing-control",
        ), patch.object(
            manager._reply_delivery,
            "_poll_control_receipt",
            return_value=None,
        ):
            assert manager.route_message(msg) is True
            assert manager._delivery_worker.run_once() == 1

        pending = manager._reply_delivery.store.pending()
        assert len(pending) == 1
        assert pending[0].watch_kind == "control_receipt"
        assert pending[0].request_id == "turn-9"
        assert pending[0].stable_id == "gwctl-msg-operation-1"
        assert pending[0].conversation_id == "oc-1"
        assert pending[0].channel_chat_type == "group"
        assert pending[0].channel_chat_id == "oc-group"

        second = replace(
            pending[0],
            operation_id="gwctl-msg-operation-2",
            receipt_id="gwctl-msg-operation-2",
            claim_epoch=0,
        )
        assert manager._reply_delivery.store.put_if_absent(second) == (second, True)
        assert {item.stable_id for item in manager._reply_delivery.store.pending()} == {
            "gwctl-msg-operation-1",
            "gwctl-msg-operation-2",
        }
        assert len(list((tmp_path / "delivery" / "pending").glob("*.json"))) == 2

        first_control = next(
            item
            for item in manager._reply_delivery.store.pending()
            if item.stable_id == "gwctl-msg-operation-1"
        )
        claimed = manager._reply_delivery.store.claim(
            first_control,
            owner="process-a",
            now=time.time(),
            ttl=10.0,
        )
        assert claimed is not None
        assert manager._reply_delivery.store.mark_terminal_claimed(
            claimed,
            owner="process-a",
            epoch=claimed.claim_epoch,
            disposition="sent",
        )
        assert manager._reply_delivery.store.was_sent("gwctl-msg-operation-1")
        assert not manager._reply_delivery.store.was_sent("turn-9")
        assert [
            item.stable_id for item in manager._reply_delivery.store.pending()
        ] == ["gwctl-msg-operation-2"]

    def test_control_receipt_unknown_waits_then_completed_result_updates_placeholder(
        self,
    ) -> None:
        store = GatewayReplyDeliveryStore(None)
        delivered: list[tuple[str, str]] = []
        states = iter(
            (
                GatewayControlReceiptResult(
                    control_state="terminal_unknown",
                    delivery_status="unknown",
                ),
                GatewayControlReceiptResult(
                    control_state="completed",
                    delivery_status="accepted",
                    message="已补充到当前任务。",
                ),
            )
        )
        worker = GatewayReplyDeliveryWorker(
            store,
            poll_response=lambda _record: pytest.fail("must not poll task result"),
            deliver_response=lambda record, text: delivered.append(
                (record.stable_id, text)
            )
            is None,
            receipt_callbacks=GatewayReplyReceiptCallbacks(
                poll_control=lambda _record: next(states)
            ),
        )
        pending = PendingGatewayReply(
            request_id="turn-9",
            operation_id="gwctl-msg-operation-1",
            receipt_id="gwctl-msg-operation-1",
            control_kind="steer",
            channel="feishu",
            user_id="ou-1",
            message_id="om-control-1",
            conversation_id="oc-1",
            progress_handle="typing-control",
            watch_kind="control_receipt",
        )
        worker.enqueue(pending)

        assert worker.run_once() == 0
        assert store.pending()[0].stable_id == "gwctl-msg-operation-1"
        assert worker.run_once() == 1
        assert delivered == [
            ("gwctl-msg-operation-1", "已补充到当前任务。")
        ]
        assert store.pending() == []
        receipt = store.terminal_receipt("gwctl-msg-operation-1")
        assert receipt is not None
        assert receipt["request_id"] == "turn-9"
        assert receipt["operation_id"] == "gwctl-msg-operation-1"

    def test_control_receipt_first_binds_target_then_quarantines_drift(
        self,
        tmp_path: Path,
    ) -> None:
        store = GatewayReplyDeliveryStore(tmp_path / "delivery")
        cleared: list[str] = []
        states = iter(
            (
                GatewayControlReceiptResult(
                    control_state="completed",
                    delivery_status="unknown",
                    target_turn_id="turn-9",
                    control_kind="steer",
                ),
                GatewayControlReceiptResult(
                    control_state="completed",
                    delivery_status="accepted",
                    message="错误目标不应送达",
                    target_turn_id="turn-other",
                    control_kind="steer",
                ),
            )
        )
        deliver = MagicMock()
        worker = GatewayReplyDeliveryWorker(
            store,
            poll_response=lambda _record: pytest.fail("must not poll task result"),
            deliver_response=deliver,
            receipt_callbacks=GatewayReplyReceiptCallbacks(
                poll_control=lambda _record: next(states),
                clear_placeholder=lambda record: cleared.append(
                    record.progress_handle
                ),
            ),
        )
        original = PendingGatewayReply(
            request_id="",
            operation_id="gwctl-msg-bind-later",
            receipt_id="gwctl-msg-bind-later",
            control_kind="steer",
            channel="feishu",
            user_id="ou-1",
            message_id="om-control-bind",
            conversation_id="oc-1",
            progress_handle="typing-bind",
            watch_kind="control_receipt",
        )
        worker.enqueue(original)

        assert worker.run_once() == 0
        bound = GatewayReplyDeliveryStore(tmp_path / "delivery").pending()[0]
        assert bound.request_id == "turn-9"
        assert bound.stable_id == "gwctl-msg-bind-later"
        assert bound.claim_owner == ""
        replay, created = store.put_if_absent(original)
        assert (replay, created) == (bound, False)
        assert worker.run_once() == 0

        deliver.assert_not_called()
        assert cleared == ["typing-bind"]
        assert store.pending() == []
        receipt = store.terminal_receipt("gwctl-msg-bind-later")
        assert receipt is not None
        assert receipt["request_id"] == "turn-9"
        assert receipt["disposition"] == "quarantined"
        assert receipt["reason"] == "control_receipt_target_conflict"

    def test_control_receipt_poll_uses_operation_id_and_validates_target(self) -> None:
        manager = ChannelManager(gateway_port=8420)
        pending = PendingGatewayReply(
            request_id="turn-9",
            operation_id="gwctl-msg-operation-1",
            receipt_id="gwctl-msg-operation-1",
            control_kind="steer",
            channel="feishu",
            user_id="ou-1",
            message_id="om-control-1",
            conversation_id="oc-1",
            channel_chat_type="group",
            channel_chat_id="oc-group",
            watch_kind="control_receipt",
        )
        response = MagicMock()
        response.read.return_value = (
            b'{"request_id":"turn-9","operation_id":"gwctl-msg-operation-1",'
            b'"receipt_id":"gwctl-msg-operation-1","control_state":"completed",'
            b'"delivery_status":"accepted","message":"ok"}'
        )
        response.__enter__.return_value = response
        response.__exit__.return_value = False

        with patch("urllib.request.urlopen", return_value=response) as urlopen:
            result = manager._poll_gateway_control_receipt(pending)

        request = urlopen.call_args.args[0]
        assert request.full_url.endswith(
            "/control-status/gwctl-msg-operation-1"
        )
        assert not request.full_url.endswith("/control-status/turn-9")
        assert request.get_header("X-user-id") == "ou-1"
        assert request.get_header("X-channel") == "feishu"
        assert request.get_header("X-conversation-id") == "oc-1"
        assert request.get_header("X-channel-chat-type") == "group"
        assert request.get_header("X-channel-chat-id") == "oc-group"
        assert result == GatewayControlReceiptResult(
            control_state="completed",
            delivery_status="accepted",
            message="ok",
            target_turn_id="turn-9",
        )

    def test_submission_rejects_conflicting_structured_input_state(self) -> None:
        with pytest.raises(ValueError, match="conflicts with input_state"):
            _gateway_submission_watch_kind(
                GatewayAskSubmission(
                    request_id="stable-input-1",
                    status="steered",
                    disposition="queued",
                    delivery_status="accepted",
                    input_state="consumed",
                    target_turn_id="turn-9",
                )
            )

    def test_store_put_if_absent_and_cas_reject_route_conflicts(
        self,
        tmp_path: Path,
    ) -> None:
        state_dir = tmp_path / "delivery"
        store = GatewayReplyDeliveryStore(state_dir)
        record = self._pending()

        current, created = store.put_if_absent(record)
        assert (current, created) == (record, True)
        replay, created = store.put_if_absent(replace(record, created_at=20.0))
        assert (replay, created) == (record, False)
        with pytest.raises(ValueError, match="route conflict"):
            store.put_if_absent(replace(record, user_id="ou-other"))

        result_watcher = replace(record, watch_kind="request_result")
        assert store.compare_and_swap(record, result_watcher) is True
        with pytest.raises(ValueError, match="route conflict"):
            store.compare_and_swap(
                result_watcher,
                replace(result_watcher, watch_kind="input_receipt"),
            )
        assert store.put_if_absent(record) == (result_watcher, False)
        assert store.compare_and_swap(record, replace(record, progress_cursor=4)) is False
        assert store.pending() == [result_watcher]
        assert GatewayReplyDeliveryStore(state_dir).pending() == [result_watcher]
        with pytest.raises(ValueError, match="route conflict"):
            store.compare_and_swap(
                result_watcher,
                replace(result_watcher, conversation_id="oc-other"),
            )
        with pytest.raises(ValueError, match="route conflict"):
            store.compare_and_swap(
                result_watcher,
                replace(result_watcher, channel_chat_id="oc-other-group"),
            )

    def test_input_watcher_waits_then_cas_switches_to_same_result_id(self) -> None:
        store = GatewayReplyDeliveryStore(None)
        states = iter(("pending", "queued"))
        final = {"value": None}
        delivered: list[tuple[str, str]] = []
        worker = GatewayReplyDeliveryWorker(
            store,
            poll_response=lambda record: final["value"],
            deliver_response=lambda record, text: delivered.append(
                (record.request_id, text)
            )
            is None,
            receipt_callbacks=GatewayReplyReceiptCallbacks(
                poll_input=lambda _record: next(states)
            ),
        )
        worker.enqueue(self._pending())

        assert worker.run_once() == 0
        waiting = store.pending()[0]
        assert waiting.watch_kind == "input_receipt"
        assert waiting.claim_owner == ""
        assert waiting.claim_epoch == 1
        assert worker.run_once() == 0
        switched = store.pending()[0]
        assert switched.request_id == "stable-input-1"
        assert switched.watch_kind == "request_result"

        final["value"] = "排队任务的最终回复"
        assert worker.run_once() == 1
        assert delivered == [("stable-input-1", "排队任务的最终回复")]
        assert store.pending() == []

    def test_terminal_unknown_clears_placeholder_and_writes_durable_unknown(
        self,
        tmp_path: Path,
    ) -> None:
        store = GatewayReplyDeliveryStore(tmp_path / "delivery")
        cleared: list[tuple[str, str]] = []
        worker = GatewayReplyDeliveryWorker(
            store,
            poll_response=lambda _record: pytest.fail("must not poll result"),
            deliver_response=lambda _record, _text: pytest.fail("must not deliver"),
            receipt_callbacks=GatewayReplyReceiptCallbacks(
                poll_input=lambda _record: "terminal_unknown",
                clear_placeholder=lambda record: cleared.append(
                    (record.user_id, record.progress_handle)
                ),
            ),
        )
        worker.enqueue(self._pending())

        assert worker.run_once() == 0
        assert cleared == [("ou-1", "typing-1")]
        assert store.pending() == []
        receipt = GatewayReplyDeliveryStore(tmp_path / "delivery").terminal_receipt(
            "stable-input-1"
        )
        assert receipt is not None
        assert receipt["disposition"] == "unknown"
        assert receipt["reason"] == "active_turn_input_terminal_unknown"

    def test_consumed_input_discards_watcher_and_clears_placeholder(self) -> None:
        store = GatewayReplyDeliveryStore(None)
        cleared: list[tuple[str, str]] = []
        worker = GatewayReplyDeliveryWorker(
            store,
            poll_response=lambda _record: pytest.fail("must not poll result"),
            deliver_response=lambda _record, _text: pytest.fail("must not deliver"),
            receipt_callbacks=GatewayReplyReceiptCallbacks(
                poll_input=lambda _record: "consumed",
                clear_placeholder=lambda record: cleared.append(
                    (record.user_id, record.progress_handle)
                ),
            ),
        )
        worker.enqueue(self._pending())

        assert worker.run_once() == 0
        assert cleared == [("ou-1", "typing-1")]
        assert store.pending() == []
        assert store.was_sent("stable-input-1") is True

    def test_input_watcher_treats_transient_poll_error_as_wait(self) -> None:
        store = GatewayReplyDeliveryStore(None)
        worker = GatewayReplyDeliveryWorker(
            store,
            poll_response=lambda _record: pytest.fail("must not poll result"),
            deliver_response=lambda _record, _text: pytest.fail("must not deliver"),
            receipt_callbacks=GatewayReplyReceiptCallbacks(
                poll_input=MagicMock(
                    side_effect=OSError("temporary disconnect")
                )
            ),
        )
        worker.enqueue(self._pending())

        assert worker.run_once() == 0
        waiting = store.pending()[0]
        assert waiting.request_id == "stable-input-1"
        assert waiting.watch_kind == "input_receipt"
        assert waiting.claim_owner == ""
        assert waiting.claim_epoch == 1

    @pytest.mark.parametrize(
        ("status_code", "reason"),
        [
            (401, "gateway_result_auth_http_401"),
            (400, "gateway_result_config_http_400"),
        ],
    )
    def test_reply_poll_auth_or_config_error_is_typed_quarantine(
        self,
        status_code: int,
        reason: str,
    ) -> None:
        manager = ChannelManager(gateway_port=8420)
        error = urllib.error.HTTPError(
            "http://127.0.0.1:8420/result/stable-input-1",
            status_code,
            "rejected",
            hdrs=None,
            fp=None,
        )

        with patch("urllib.request.urlopen", side_effect=error), pytest.raises(
            GatewayReplyQuarantineError,
            match=reason,
        ):
            manager._poll_gateway_once(
                replace(self._pending(), watch_kind="request_result"),
                interval=0.0,
            )

    def test_typed_reply_quarantine_clears_placeholder_without_user_message(self) -> None:
        store = GatewayReplyDeliveryStore(None)
        cleared: list[str] = []
        deliver = MagicMock()
        worker = GatewayReplyDeliveryWorker(
            store,
            poll_response=MagicMock(
                side_effect=GatewayReplyQuarantineError(
                    "gateway_result_auth_http_401"
                )
            ),
            deliver_response=deliver,
            receipt_callbacks=GatewayReplyReceiptCallbacks(
                clear_placeholder=lambda record: cleared.append(
                    record.progress_handle
                )
            ),
        )
        worker.enqueue(self._pending(watch_kind="request_result"))

        assert worker.run_once() == 0
        deliver.assert_not_called()
        assert cleared == ["typing-1"]
        assert store.pending() == []
        receipt = store.terminal_receipt("stable-input-1")
        assert receipt is not None
        assert receipt["disposition"] == "quarantined"
        assert receipt["reason"] == "gateway_result_auth_http_401"

    def test_input_status_poll_uses_stable_id_and_trusted_owner(self) -> None:
        manager = ChannelManager(gateway_port=8420)
        response = MagicMock()
        response.read.return_value = b'{"input_state":"terminal_unknown"}'
        response.__enter__.return_value = response
        response.__exit__.return_value = False

        with patch("urllib.request.urlopen", return_value=response) as urlopen:
            state = manager._poll_gateway_input_receipt(self._pending())

        request = urlopen.call_args.args[0]
        assert request.full_url.endswith("/input-status/stable-input-1")
        assert request.get_header("X-user-id") == "ou-1"
        assert request.get_header("X-channel") == "feishu"
        assert state == "terminal_unknown"

    def test_restart_after_provider_accept_before_receipt_reuses_provider_key(
        self,
        tmp_path: Path,
    ) -> None:
        state_dir = tmp_path / "deliveries"
        provider_messages: dict[str, tuple[str, str]] = {}
        attempts: list[str] = []
        pending = PendingGatewayReply(
            request_id="req-provider-crash",
            channel="feishu",
            user_id="ou_late",
            message_id="om-provider-crash",
            conversation_id="oc-late",
        )
        first = ChannelManager(delivery_state_dir=state_dir)
        first_adapter = ProviderIdempotentDummyAdapter(
            provider_messages,
            attempts,
        )
        first_adapter.adapter_name = "feishu"
        first.register_adapter(first_adapter)
        first._reply_delivery.enqueue(pending)

        with patch.object(
            first,
            "_poll_gateway_once",
            return_value="崩溃点前平台已接收的回复",
        ), patch.object(
            first._reply_delivery.store,
            "mark_terminal_claimed",
            side_effect=OSError("simulated receipt write crash"),
        ):
            assert first._reply_delivery.run_once() == 0

        second = ChannelManager(delivery_state_dir=state_dir)
        second_adapter = ProviderIdempotentDummyAdapter(
            provider_messages,
            attempts,
        )
        second_adapter.adapter_name = "feishu"
        second.register_adapter(second_adapter)
        stale_claim = second._reply_delivery.store.pending()[0]
        assert stale_claim.claim_owner
        assert second._reply_delivery.store.compare_and_swap(
            stale_claim,
            replace(stale_claim, claim_expires_at=0.0),
        )
        with patch.object(
            second,
            "_poll_gateway_once",
            return_value="崩溃点前平台已接收的回复",
        ):
            assert second._reply_delivery.run_once() == 1

        assert attempts == [
            "gateway-reply:om-provider-crash:req-provider-crash:final",
            "gateway-reply:om-provider-crash:req-provider-crash:final",
        ]
        assert list(provider_messages.values()) == [
            ("ou_late", "崩溃点前平台已接收的回复")
        ]
        assert second._reply_delivery.store.pending() == []
