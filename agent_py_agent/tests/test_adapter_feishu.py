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


class TestFeishuProgressReaction:
    """测试飞书原生 typing reaction(收到给消息贴 OnIt→处理完撤掉再回复)。任何失败都跳过 typing、绝不影响回复。"""

    def _adapter(self) -> FeishuAdapter:
        return FeishuAdapter(config={"feishu_app_id": "id", "feishu_app_secret": "secret"}, callback_port=8421)

    def test_reaction_returns_message_and_reaction_handle(self) -> None:
        adapter = self._adapter()
        with patch.object(adapter, "_get_tenant_access_token", return_value="tok"), \
             patch("agent_py_agent.agent.adapter.feishu_typing._add_reaction", return_value={"code": 0, "data": {"reaction_id": "rxn1"}}):
            assert adapter.send_progress_placeholder("ou_1", "om_msg") == "om_msg:rxn1"

    def test_reaction_empty_without_message_id(self) -> None:
        # 没有消息 id(无处可贴)→ 返回""(typing 跳过)
        adapter = self._adapter()
        assert adapter.send_progress_placeholder("ou_1", "") == ""

    def test_reaction_empty_on_api_failure(self) -> None:
        # 无权限/接口失败 → 返回""(typing 跳过,不影响回复)
        adapter = self._adapter()
        with patch.object(adapter, "_get_tenant_access_token", return_value="tok"), \
             patch("agent_py_agent.agent.adapter.feishu_typing._add_reaction", return_value={"code": 230001, "msg": "no permission"}):
            assert adapter.send_progress_placeholder("ou_1", "om_msg") == ""

    def test_reaction_empty_when_no_token(self) -> None:
        adapter = self._adapter()
        with patch.object(adapter, "_get_tenant_access_token", return_value=None):
            assert adapter.send_progress_placeholder("ou_1", "om_msg") == ""

    def test_finalize_removes_reaction_then_sends(self) -> None:
        adapter = self._adapter()
        msg = OutgoingMessage(channel="feishu", user_id="ou_1", content="答案")
        with patch.object(adapter, "_get_tenant_access_token", return_value="tok"), \
             patch("agent_py_agent.agent.adapter.feishu_typing._remove_reaction", return_value={"code": 0}) as rm, \
             patch.object(adapter, "send_message", return_value=True) as send_msg:
            assert adapter.finalize_response("ou_1", "om_msg:rxn1", msg) is True
            rm.assert_called_once()  # 先撤掉"正在处理"reaction
            send_msg.assert_called_once_with("ou_1", msg)  # 再发回复

    def test_finalize_without_handle_just_sends(self) -> None:
        adapter = self._adapter()
        msg = OutgoingMessage(channel="feishu", user_id="ou_1", content="答案")
        with patch("agent_py_agent.agent.adapter.feishu_typing._remove_reaction") as rm, \
             patch.object(adapter, "send_message", return_value=True) as send_msg:
            assert adapter.finalize_response("ou_1", "", msg) is True
            rm.assert_not_called()  # 无句柄(没贴成 reaction)→不撤,直接发
            send_msg.assert_called_once_with("ou_1", msg)

    def test_finalize_remove_failure_still_sends(self) -> None:
        # 撤 reaction 异常也绝不影响回复
        adapter = self._adapter()
        msg = OutgoingMessage(channel="feishu", user_id="ou_1", content="答案")
        with patch.object(adapter, "_get_tenant_access_token", return_value="tok"), \
             patch("agent_py_agent.agent.adapter.feishu_typing._remove_reaction", side_effect=Exception("boom")), \
             patch.object(adapter, "send_message", return_value=True) as send_msg:
            assert adapter.finalize_response("ou_1", "om_msg:rxn1", msg) is True
            send_msg.assert_called_once_with("ou_1", msg)  # 撤失败也照常发回复
