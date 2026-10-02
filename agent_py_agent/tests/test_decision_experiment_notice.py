"""J6：真实晋升回执经原宿主提示、TUI 与 IM 持久回复链；仅模型/供应商传输用隔离替身。"""
from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.adapter.delivery import PendingGatewayReply
from agent_py_agent.agent.adapter.manager import ChannelManager
from agent_py_agent.agent.conversation.history_display import conversation_history_display_events
from agent_py_agent.agent.conversation.host_notices import (
    host_notice,
    pending_host_notices,
    queue_host_notice,
    take_host_notices,
)
from agent_py_agent.agent.conversation.native_history import provider_history_messages_from_rows
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts import request_execution
from agent_py_agent.agent.gateway_parts.http_handlers import _public_result
from agent_py_agent.agent.gateway_parts.stream_writer import BufferedChunkStreamWriter
from agent_py_agent.cli.chat_parts.tui_block_renderer import TuiRenderContext, _render_block
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime, TuiTurnEventAdapter
from agent_py_agent.tests.test_adapter_manager import DummyAdapter
from agent_py_agent.tests.test_decision_capability_http import (
    capability_http as capability_http,  # noqa: F401
)
from agent_py_agent.tests.test_decision_experiment_gateway_turn import (
    experiment_request,
    fake_main_model,
    prior_samples,
    stored,
)
from agent_py_agent.tests.test_decision_experiment_gateway_turn import (
    gateway_lab as gateway_lab,  # noqa: F401
)
from agent_py_agent.tests.test_decision_experiment_promotion import (
    finish,
    lane_at,
    ready_turn,
    stored_block,
    view,
)
from agent_py_agent.tests.test_gateway_capability_compact import (
    gateway_surface as gateway_surface,  # noqa: F401
)
from agent_py_agent.tests.test_tool_presentation_projection import (
    prepared as tool_surface,  # noqa: F401
)


# LLM: 不启动 Gateway；在原执行入口跑真实授权、样本补写、设置 CAS 和 final 提交，chunk writer 也用产品实现。
# 函数用途: 完成一轮隔离实验并返回 final 与 TUI 实际读取的事件文件。
def run_turn(lab, monkeypatch, mode="apply", count=2):
    prior_samples(lab, count)
    experiment_request(lab, mode)
    fake_main_model(monkeypatch, "presentation_optional_a")
    chunk = lab.context.request_path.with_suffix(".chunks.jsonl")
    lab.context = replace(lab.context, on_chunk=BufferedChunkStreamWriter(chunk, rich_transcript=True))
    result = request_execution._run_gateway_ask(lab.context)
    events = [json.loads(line) for line in chunk.read_text(encoding="utf-8").splitlines()]
    return result, events


def notice_rows(events):
    return [event["notice"] for event in events if event.get("kind") == "host_notice"]


def test_promotion_receipt_freezes_identity_and_evidence_rule(tmp_path):
    lane = lane_at(tmp_path)
    turn = ready_turn(lane)
    finish(turn)
    receipt = stored_block(turn)["promotion"]
    assert receipt.get("promotion_id") == turn.receipt["authorization_id"]
    assert receipt["evaluation"].get("rule") == {
        "min_comparable_samples": 3, "required_recall": 1.0,
        "required_outcome": "charged", "window_samples": 8,
    }


def test_promotion_reaches_current_tui_and_final_without_replacing_earlier_notices(gateway_lab, monkeypatch):  # noqa: F811
    store, thread_id = gateway_lab.agent.conversation_store, gateway_lab.conversation.thread_id
    earlier = host_notice("goal_continuation", "GOAL_CONTINUATION_NO_PROGRESS", "先前的暂停提示")
    assert queue_host_notice(store, thread_id, earlier)
    result, events = run_turn(gateway_lab, monkeypatch)
    notices = notice_rows(events)
    assert len(notices) == 2, "晋升发生在收尾，也必须在当轮出现，不能等用户再发消息"
    assert notices[0] == earlier.to_dict()
    notice, receipt = notices[1], stored(gateway_lab)["experiment_records"]["promotion"]
    assert notice["notice_id"] == receipt["promotion_id"]
    assert notice["source"] == "decision_experiment" and notice["code"] == "promotion_applied"
    assert notice["details"]["promotion_id"] == receipt["promotion_id"]
    text = notice["text"]
    assert "skill_tool" in text and "本会话" in text
    assert "关闭（off）→正式使用（apply）" in text
    assert "可比较样本 3（门槛 ≥3）" in text
    assert "/model → 选择模型 → 决策模型 → 本会话临时设置 → 逐字段恢复继承" in text
    assert "points.skill_tool.mode" in text and "撤销实验授权不会回滚" in text
    assert result.channel_delivery["host_notices"] == notices and pending_host_notices(store, thread_id) == ()
    runtime = TuiRuntime("promotion-session")
    adapter = TuiTurnEventAdapter(runtime, gateway_lab.context.request_id)
    for event in events + events:
        adapter.on_gateway_event(event)
    blocks = [block for block in runtime.store.snapshot().stable_blocks if block.text == text]
    assert len(blocks) == 1 and blocks[0].role == "system"
    assert _render_block(blocks[0], TuiRenderContext(width=500))[0][0] == ("class:tui-muted", f"◇ {text}")
    rows = list(store.messages.page_after_offset_report(thread_id, after=0)[0])
    final = [row for row in rows if row.metadata.get("assistant_part_id") == "final"][-1]
    assert final.metadata["host_notices"] == notices and text not in final.content
    replay = conversation_history_display_events(rows)
    assert sum(event["payload"].get("text") == text for event in replay) == 1
    assert text not in json.dumps(provider_history_messages_from_rows(rows), ensure_ascii=False)


# LLM: 假 urlopen 只提供本轮真实产品 final 的 /result 响应，原 poll client、worker、DeliveryService、路由和 sent 账不替换。
# 函数用途: 在不调用真实飞书的前提下核对提示确实进入原会话最终发送动作，重建 worker 后也不重发。
def deliver_offline(tmp_path, monkeypatch, response):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(response, ensure_ascii=False).encode("utf-8")

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response())
    pending = PendingGatewayReply(request_id="promotion-request", channel="feishu", user_id="ou_test",
                                  message_id="om-test", conversation_id="oc-same-thread", progress_handle="typing-test")
    manager = ChannelManager(delivery_state_dir=tmp_path / "delivery")
    adapter = DummyAdapter()
    adapter.adapter_name = "feishu"
    manager.register_adapter(adapter)
    monkeypatch.setattr(manager._reply_delivery, "_poll_progress", lambda _pending: ([], 0))
    assert manager._reply_delivery.enqueue(pending)[1]
    assert manager._reply_delivery.run_once() == 1
    assert len(adapter._send_calls) == 1
    user, outgoing = adapter._send_calls[0]
    assert user == pending.user_id and outgoing.metadata["conversation_id"] == pending.conversation_id
    assert outgoing.metadata["reply_to"] == pending.message_id
    assert outgoing.metadata["delivery_idempotency_key"] == "gateway-reply:om-test:promotion-request:final"
    restarted = ChannelManager(delivery_state_dir=tmp_path / "delivery")
    restarted.register_adapter(adapter)
    assert not restarted._reply_delivery.enqueue(pending)[1]
    assert restarted._reply_delivery.run_once() == 0 and len(adapter._send_calls) == 1
    return outgoing.content


def test_promotion_final_delivers_identical_notice_to_same_im_conversation(gateway_lab, monkeypatch, tmp_path):  # noqa: F811
    result, events = run_turn(gateway_lab, monkeypatch)
    notices = notice_rows(events)
    assert len(notices) == 1
    response = {}
    request_execution._update_response_from_result(response, result, gateway_lab.context.request)
    public = _public_result(response)
    assert public["channel_delivery"]["host_notices"] == notices
    content = deliver_offline(tmp_path, monkeypatch, public)
    assert content == f"【提示】{notices[0]['text']}\n\n核对完成"


def test_duplicate_receipt_and_recreated_store_never_queue_notice_again(tmp_path):
    lane = lane_at(tmp_path)
    turn = ready_turn(lane)
    finish(turn)
    store, thread_id = lane.host.conversation_store, lane.thread.thread_id
    notices = pending_host_notices(store, thread_id)
    assert len(notices) == 1
    receipt = stored_block(turn)["promotion"]
    assert notices[0].notice_id == receipt["promotion_id"]
    finish(turn)
    assert pending_host_notices(store, thread_id) == notices
    assert take_host_notices(store, thread_id, [notices[0].notice_id]) == notices
    lane.host.conversation_store = ConversationStore(store.storage.root)
    payload = json.loads(turn.request_path.read_text(encoding="utf-8"))
    payload["execution_attempt_id"] = "exec-restarted"
    turn.request_path.write_text(json.dumps(payload), encoding="utf-8")
    restarted = SimpleNamespace(agent=lane.host, request=payload, request_path=turn.request_path, request_id=turn.request_id)
    finish(restarted)
    assert pending_host_notices(lane.host.conversation_store, thread_id) == ()
    assert stored_block(restarted)["promotion"] == receipt


@pytest.mark.parametrize("mode,count", [("observe", 2), ("apply", 0), (None, 0)])
def test_no_applied_promotion_has_no_tui_or_im_notice(gateway_lab, monkeypatch, mode, count):  # noqa: F811
    if mode is None:
        fake_main_model(monkeypatch, "presentation_optional_a")
        result = request_execution._run_gateway_ask(gateway_lab.context)
        events = []
    else:
        result, events = run_turn(gateway_lab, monkeypatch, mode, count)
    assert notice_rows(events) == [] and "host_notices" not in result.channel_delivery
    assert pending_host_notices(gateway_lab.agent.conversation_store, gateway_lab.conversation.thread_id) == ()


def test_skipped_and_uncertain_receipts_are_never_announced_as_applied(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import request_experiment_promotion as promotion

    lane = lane_at(tmp_path)
    turn = ready_turn(lane)
    original = promotion.execute_decision_settings_operation

    def failed_write(agent, operation, payload, *, thread_id=""):
        if operation == "patch":
            raise OSError("写入结果未知")
        return original(agent, operation, payload, thread_id=thread_id)

    monkeypatch.setattr(promotion, "execute_decision_settings_operation", failed_write)
    finish(turn)
    assert stored_block(turn)["promotion"]["status"] == "uncertain"
    assert pending_host_notices(lane.host.conversation_store, lane.thread.thread_id) == ()
    assert view(lane)["effective"]["points"]["skill_tool"]["effective_mode"] == "off"
