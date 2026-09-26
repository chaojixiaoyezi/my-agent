"""客户端侧的管理员命令。

- IM 适配器：可能带管理员密码的命令不进持久入站队列，Gateway 不可达只回“服务暂时不可用”。
- TUI/终端：/admin、/approve、/deny 在任何传输或持久化之前本地拒绝，也不写输入历史。
"""

from __future__ import annotations

import threading
import time
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent_py_agent.agent.adapter.manager import ChannelManager, GatewayAskSubmission
from agent_py_agent.agent.adapter.protocol import IncomingMessage
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.cli.chat_parts import control_runtime, tui_actions
from agent_py_agent.cli.chat_parts.tui_keybindings import _remember_input
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.tests.test_adapter_manager import DummyAdapter

_SECRET = "Correct-Horse-42"


def _manager(tmp_path: Path) -> tuple[ChannelManager, DummyAdapter]:
    manager = ChannelManager(delivery_state_dir=tmp_path / "delivery")
    adapter = DummyAdapter()
    adapter.adapter_name = "feishu"
    manager.register_adapter(adapter)
    return manager, adapter


def _message(content: str, message_id: str = "om-secret-1") -> IncomingMessage:
    return IncomingMessage(
        channel="feishu",
        user_id="ou_admin",
        content=content,
        message_id=message_id,
        conversation_id="oc_p2p",
        metadata={"feishu_chat_type": "p2p", "feishu_chat_id": "oc_p2p"},
    )


def _wait_for_reply(adapter: DummyAdapter) -> str:
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not adapter._send_calls:
        time.sleep(0.01)
    assert adapter._send_calls, "敏感命令应当收到一条直接回复"
    return adapter._send_calls[-1][1].content


def _files_containing(root: Path, needle: str) -> list[Path]:
    if not root.exists():
        return []
    return [path for path in root.rglob("*") if path.is_file() and needle.encode("utf-8") in path.read_bytes()]


@pytest.mark.parametrize("text", [f"/admin {_SECRET}", f"/approve {_SECRET}", "/deny"])
def test_sensitive_commands_skip_durable_ingress_and_reply_directly(tmp_path, text):
    manager, adapter = _manager(tmp_path)
    submitted: list[dict] = []

    def submit(payload, msg):
        submitted.append(dict(payload))
        return GatewayAskSubmission("", status="control", kind="admin", ok=True, message="控制结果", operation_id="gwctl-msg-" + "0" * 32)

    with patch.object(manager, "_submit_gateway_payload", side_effect=submit):
        assert manager.route_message(_message(text)) is True
        assert _wait_for_reply(adapter) == "控制结果"

    assert len(submitted) == 1 and submitted[0]["prompt"] == text
    assert submitted[0]["metadata"]["channel_chat_type"] == "p2p" and submitted[0]["metadata"]["message_id"] == "om-secret-1"
    assert manager._delivery_worker.store.records() == []
    assert manager._reply_delivery.store.pending() == []
    assert _files_containing(tmp_path, _SECRET) == []


def test_unreachable_gateway_gets_unavailable_reply_and_nothing_is_queued(tmp_path):
    manager, adapter = _manager(tmp_path)
    with patch.object(manager, "_submit_gateway_payload", side_effect=urllib.error.URLError("refused")) as submit:
        assert manager.route_message(_message(f"/admin {_SECRET}")) is True
        assert _wait_for_reply(adapter) == "服务暂时不可用，命令没有执行，请稍后重试。"
    assert submit.call_count == 1, "不自动重试"
    assert manager._delivery_worker.store.records() == []
    assert _files_containing(tmp_path, _SECRET) == []


def test_ordinary_message_still_enters_durable_ingress(tmp_path):
    manager, _adapter = _manager(tmp_path)
    with patch.object(manager, "_submit_gateway_payload") as submit:
        assert manager.route_message(_message("帮我看看 /admin 这个词", message_id="om-plain")) is True
        submit.assert_not_called()
    rows = manager._delivery_worker.store.records()
    assert [row.provider_message_id for row in rows] == ["om-plain"]


@pytest.mark.parametrize("text", [f"/admin {_SECRET}", f"/approve {_SECRET}", "/deny", "/admin status"])
def test_tui_refuses_im_only_commands_before_outbox_and_history(text):
    runtime = TuiRuntime("admin-refusal")

    class Reconciler:
        def enqueue(self, entry, *, on_persisted_before_dispatch=None) -> None:
            raise AssertionError("IM 专用命令不能进入控制 outbox")

    params = SimpleNamespace(
        media_importing=False,
        use_gateway=True,
        state_lock=threading.Lock(),
        is_running_ref=[False],
        running_request_id_ref=[""],
        tui_runtime=runtime,
        control_operation_reconciler=Reconciler(),
    )
    assert tui_actions._tui_submit_control_operation(params, text) is True
    shown = [block.text for block in runtime.store.snapshot().stable_blocks]
    assert any("只用于飞书等 IM 私聊" in item for item in shown)
    assert all(_SECRET not in item for item in shown)

    remembered: list[str] = []
    input_area = SimpleNamespace(buffer=SimpleNamespace(history=SimpleNamespace(append_string=remembered.append)))
    _remember_input(input_area, text)
    _remember_input(input_area, "/status")
    assert remembered == ["/status"]


def test_terminal_control_executor_never_sends_im_only_commands():
    def forbidden(*_args, **_kwargs):
        raise AssertionError("不能发往 Gateway")

    execution = control_runtime.ChatControlExecution(
        agent=SimpleNamespace(config=SimpleNamespace(gateway_port=8420), post_gateway_json=forbidden),
        use_gateway=True,
        state=control_runtime.ChatControlState(
            running=False, queued_count=0, prompt="", started_at=0.0, session_id="sess-local",
        ),
    )
    result = control_runtime.execute_chat_control(execution, parse_conversation_control(f"/approve {_SECRET}"))
    assert result.ok is False and result.error_code == "ADMIN_IDENTITY_SCOPE_INVALID"
    assert _SECRET not in result.message
