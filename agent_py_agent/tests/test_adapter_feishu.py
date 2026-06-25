"""LLM: 测试飞书适配器 — 消息格式转换、webhook 验证、回调处理。

给人看的解释：
测试 FeishuAdapter 的各项能力：飞书消息解析、安全验证、token 获取。
不实际发起飞书 HTTP 请求，所有外部调用均 mock。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from agent_py_agent.agent.adapter.feishu import FeishuAdapter
from agent_py_agent.agent.adapter.protocol import IncomingMessage, OutgoingMessage


class TestFeishuSignatureVerification:
    """测试飞书签名验证逻辑。"""

    def test_verify_token_success(self) -> None:
        adapter = FeishuAdapter(
            config={
                "feishu_app_id": "app_id",
                "feishu_app_secret": "secret",
                "feishu_verification_token": "my_token",
            },
            callback_port=8421,
        )
        assert adapter.verify_feishu_signature("my_token", "123", "any_sig") is True

    def test_verify_token_failure(self) -> None:
        adapter = FeishuAdapter(
            config={
                "feishu_app_id": "app_id",
                "feishu_app_secret": "secret",
                "feishu_verification_token": "my_token",
            },
            callback_port=8421,
        )
        assert adapter.verify_feishu_signature("wrong_token", "123", "any_sig") is False

    def test_verify_fail_closed_when_unconfigured(self) -> None:
        # #4 fail-closed:既无 verification_token 也无 encrypt_key → 拒绝一切(不处理无验证事件)
        adapter = FeishuAdapter(
            config={"feishu_app_id": "app_id", "feishu_app_secret": "secret"},
            callback_port=8421,
        )
        assert adapter.verify_feishu_signature("anything", "123", "sig") is False
        assert adapter.verify_feishu_signature("", "", "") is False  # 缺 token 头也不放行

    def test_verify_with_encrypt_key(self) -> None:
        adapter = FeishuAdapter(
            config={
                "feishu_app_id": "app_id",
                "feishu_app_secret": "secret",
                "feishu_verification_token": "tok",
                "feishu_encrypt_key": "encrypt_key_123",
            },
            callback_port=8421,
        )
        # SHA256 方式验证
        source = "encrypt_key_123" + "456" + "tok"
        expected = hmac.new(source.encode(), b"", hashlib.sha256).hexdigest()
        assert adapter.verify_feishu_signature("tok", "456", expected) is True


class TestFeishuTokenCaching:
    """测试飞书 token 缓存逻辑。"""

    def test_token_not_fetched_until_needed(self) -> None:
        adapter = FeishuAdapter(
            config={"feishu_app_id": "id", "feishu_app_secret": "secret"},
            callback_port=8421,
        )
        assert adapter._tenant_access_token is None
        # 初始未获取
        assert adapter._token_expires_at == 0


class TestFeishuMessageDispatch:
    """测试飞书消息通过 on_message 回调分发。"""

    def test_dispatch_calls_registered_callback(self) -> None:
        adapter = FeishuAdapter(
            config={"feishu_app_id": "id", "feishu_app_secret": "secret"},
            callback_port=8421,
        )
        received: list[IncomingMessage] = []

        def cb(msg: IncomingMessage) -> None:
            received.append(msg)

        adapter.on_message(cb)

        test_msg = IncomingMessage(
            channel="feishu",
            user_id="ou_test",
            content="test content",
            message_id="om_123",
        )
        adapter._dispatch(test_msg)

        assert len(received) == 1
        assert received[0].content == "test content"


class TestFeishuSendMessage:
    """测试飞书发送消息逻辑（mock HTTP）。"""

    def test_send_message_without_app_id_fails(self) -> None:
        adapter = FeishuAdapter(
            config={"feishu_app_id": "", "feishu_app_secret": ""},
            callback_port=8421,
        )
        msg = OutgoingMessage(channel="feishu", user_id="ou_1", content="hello")
        result = adapter.send_message("ou_1", msg)
        assert result is False


class TestFeishuLifecycle:
    """测试飞书适配器启停。"""

    def test_start_stop_clean(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            adapter = FeishuAdapter(
                config={"feishu_app_id": "", "feishu_app_secret": ""},
                callback_port=0,  # 随机端口
                workspace_root=Path(td),
            )
            adapter.start()
            assert adapter.running is True

            adapter.stop()
            assert adapter.running is False

    def test_double_start_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            adapter = FeishuAdapter(
                config={"feishu_app_id": "", "feishu_app_secret": ""},
                callback_port=0,
                workspace_root=Path(td),
            )
            adapter.start()
            first_state = adapter.running
            adapter.start()  # 第二次启动
            assert first_state is True
            adapter.stop()

    def test_stop_when_not_running(self) -> None:
        adapter = FeishuAdapter(
            config={"feishu_app_id": "", "feishu_app_secret": ""},
            callback_port=8421,
        )
        # 未 start 就 stop 不应报错
        adapter.stop()
        assert adapter.running is False


class TestFeishuProgressPlaceholder:
    """测试"正在思考"占位卡片 + 完成后原地更新(typing 反馈)。任何失败都降级发新消息、绝不丢结果。"""

    def _adapter(self) -> FeishuAdapter:
        return FeishuAdapter(config={"feishu_app_id": "id", "feishu_app_secret": "secret"}, callback_port=8421)

    def test_placeholder_returns_message_id(self) -> None:
        adapter = self._adapter()
        with patch.object(adapter, "_get_tenant_access_token", return_value="tok"), \
             patch("agent_py_agent.agent.adapter.feishu_typing._post_card", return_value={"code": 0, "data": {"message_id": "om_card1"}}):
            assert adapter.send_progress_placeholder("ou_1") == "om_card1"

    def test_placeholder_returns_empty_on_api_failure(self) -> None:
        # 发卡片返回非 0 → 返回空串(route_message 据此降级为完成后直接发新消息)
        adapter = self._adapter()
        with patch.object(adapter, "_get_tenant_access_token", return_value="tok"), \
             patch("agent_py_agent.agent.adapter.feishu_typing._post_card", return_value={"code": 230001, "msg": "x"}):
            assert adapter.send_progress_placeholder("ou_1") == ""

    def test_placeholder_empty_when_no_token(self) -> None:
        adapter = self._adapter()
        with patch.object(adapter, "_get_tenant_access_token", return_value=None):
            assert adapter.send_progress_placeholder("ou_1") == ""

    def test_finalize_with_handle_patches_card_not_resend(self) -> None:
        adapter = self._adapter()
        msg = OutgoingMessage(channel="feishu", user_id="ou_1", content="最终答案")
        with patch.object(adapter, "_get_tenant_access_token", return_value="tok"), \
             patch("agent_py_agent.agent.adapter.feishu_typing._patch_card", return_value={"code": 0}) as patch_card, \
             patch.object(adapter, "send_message") as send_msg:
            assert adapter.finalize_response("ou_1", "om_card1", msg) is True
            patch_card.assert_called_once()
            send_msg.assert_not_called()  # 有句柄→原地更新占位卡片,不发新消息

    def test_finalize_without_handle_falls_back_to_send(self) -> None:
        adapter = self._adapter()
        msg = OutgoingMessage(channel="feishu", user_id="ou_1", content="答案")
        with patch.object(adapter, "send_message", return_value=True) as send_msg:
            assert adapter.finalize_response("ou_1", "", msg) is True
            send_msg.assert_called_once_with("ou_1", msg)  # 无句柄(占位没发成)→降级发新消息

    def test_finalize_patch_failure_falls_back_to_send(self) -> None:
        adapter = self._adapter()
        msg = OutgoingMessage(channel="feishu", user_id="ou_1", content="答案")
        with patch.object(adapter, "_get_tenant_access_token", return_value="tok"), \
             patch("agent_py_agent.agent.adapter.feishu_typing._patch_card", return_value={"code": 99, "msg": "fail"}), \
             patch.object(adapter, "send_message", return_value=True) as send_msg:
            assert adapter.finalize_response("ou_1", "om_x", msg) is True
            send_msg.assert_called_once()  # 更新失败→降级发新消息,绝不丢结果
