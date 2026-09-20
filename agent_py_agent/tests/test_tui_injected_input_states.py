"""插话(active turn input)的"排队 / 已提交 / 已确认"三段状态验收。

背景:模型已经引用插话、底部却还写"将在下一次工具调用后送入当前回合",而且可见历史里没有这条
用户消息。根因是展示层只有"入队"与"已确认(consumed)"两态,而 acknowledge 只发生在整次模型响应
返回之后——慢流/失败时用户看到的状态与实际送达事实相反。

本文件分区验证:
  ① 运行期:提供方调用前必须发"已提交"事实(身份=client message_id + provider_call_id),
     且**不得**在这一步写 consumed、不得结算回复欠账;
  ② 展示层:已提交后用户消息立刻按原提交位置进入历史,等待区改成"已送入当前回合,等待模型回应",
     绝不出现"将在下一次工具调用后送入";已确认只收起标记,不插第二行;
  ③ 慢流/失败/重连/主子视角:不丢、不重、身份只认 message_id,不按正文匹配。
"""

from __future__ import annotations

from dataclasses import replace

from agent_py_agent.agent.agent_core.runtime.guidance import (
    acknowledge_injected_turn_input,
    inject_pending_guidance,
    mark_injected_turn_input_submitted,
)
from agent_py_agent.agent.conversation.background_transcript import (
    BACKGROUND_TRANSCRIPT_EVENT_KINDS,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.cli.chat_parts.tui_block_renderer import (
    TuiRenderContext,
    render_tui_snapshot,
)
from agent_py_agent.cli.chat_parts.tui_events import TuiEventSequencer
from agent_py_agent.cli.chat_parts.tui_view_model import TuiStateStore
from agent_py_agent.tests.test_runtime_guidance import _tool_loop_params


def _lines(fragments) -> list[str]:
    from agent_py_agent.cli.chat_parts.tui_markdown import fragments_text

    return [fragments_text(line) for line in fragments]


def _status_lines(store: TuiStateStore) -> list[str]:
    frame = render_tui_snapshot(store.snapshot(), TuiRenderContext(width=80))
    return _lines(frame.input_status_lines)


def _transcript_lines(store: TuiStateStore) -> list[str]:
    frame = render_tui_snapshot(store.snapshot(), TuiRenderContext(width=80))
    return _lines(frame.transcript_lines)


def _steer_seq(store: TuiStateStore, name: str = "steer-states"):
    sequencer = TuiEventSequencer(name, clock=lambda: 50.0)
    store.publish(sequencer.emit("turn_started", "started", "turn"))
    return sequencer


def _count(text: str, lines: list[str]) -> int:
    return sum(1 for line in lines if text in line)


# --------------------------------------------------------------------------------------
# ① 运行期:已提交是独立事实,且不等于已确认
# --------------------------------------------------------------------------------------
class _RecordingSink:
    """记录运行期发给展示 sink 的两类边界事件(顺序敏感)。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, tuple[str, ...], str]] = []

    def __call__(self, _text: str) -> None:
        return None

    def begin_active_turn_input(self, client_message_ids: tuple[str, ...]) -> None:
        self.events.append(("begin", tuple(client_message_ids), ""))

    def submit_active_turn_input(
        self, client_message_ids: tuple[str, ...], *, provider_call_id="", client_messages=()
    ) -> None:
        del client_messages
        self.events.append(("submit", tuple(client_message_ids), str(provider_call_id)))

    def complete_active_turn_input(
        self, client_message_ids: tuple[str, ...], *, client_messages=()
    ) -> None:
        del client_messages
        self.events.append(("complete", tuple(client_message_ids), ""))


def _agent_with_steer(tmp_path, *, message_id: str = "steer-client-1"):
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    agent.conversation_store.guidance.append(
        {
            "target_type": "agent_run",
            "target_id": "main-run-1",
            "message": "顺便回答一句，原任务继续。",
            "now": 10.0,
            "metadata": {"channel_message_id": message_id},
        }
    )
    return agent


def test_submit_boundary_emits_submitted_fact_without_consuming(tmp_path) -> None:
    """提供方调用前发"已提交";这一步不许写 consumed,也不许结算回复欠账。"""
    agent = _agent_with_steer(tmp_path)
    sink = _RecordingSink()
    params = _tool_loop_params(run_id="main-run-1", effective_on_chunk=sink)
    assert inject_pending_guidance(agent, params, now=11.0) is True

    assert mark_injected_turn_input_submitted(
        agent, params, provider_call_id="call-7", now=11.5
    ) == 1

    assert sink.events[-1] == ("submit", ("steer-client-1",), "call-7")
    assert [kind for kind, _ids, _call in sink.events].count("complete") == 0
    # 账本只到 submitted:回执仍在(未 consumed),确认待办仍在,且**还没有**建立回复欠账
    # (回复欠账只在真正的 consumed 边界产生,已提交不得提前结算任何东西)。
    assert agent.conversation_store.guidance.pending("agent_run", "main-run-1") != []
    state = params.live_archive_state
    ack_ids = state.get("_guidance_ack_ids")
    assert isinstance(ack_ids, set) and len(ack_ids) == 1, state
    assert state.get("_active_turn_reply_required_ids") is None

    # 之后真正的确认才走 consumed,身份与已提交一致。
    assert acknowledge_injected_turn_input(agent, params, now=12.0) == 1
    assert sink.events[-1][0] == "complete"
    assert sink.events[-1][1] == ("steer-client-1",)


def test_submitted_fact_is_a_durable_transcript_kind() -> None:
    """已提交必须是子代理/后台账本认识的展示事件(重连据此重放)。"""
    assert "active_turn_input_submitted" in BACKGROUND_TRANSCRIPT_EVENT_KINDS


# --------------------------------------------------------------------------------------
# ② 展示层:三段状态
# --------------------------------------------------------------------------------------
def test_submitted_steer_is_visible_in_history_and_labeled_truthfully() -> None:
    store = TuiStateStore()
    seq = _steer_seq(store)
    store.publish(
        seq.emit(
            "steer_added", "queued", "steer:1", {"message_id": "steer-1", "text": "补充 A"}
        )
    )
    assert _status_lines(store) == [
        "• 将在下一次工具调用后送入当前回合",
        "  ↳ 补充 A",
    ]
    assert _count("补充 A", _transcript_lines(store)) == 0

    store.publish(
        seq.emit(
            "steer_submitted",
            "started",
            "steer:1",
            {"message_id": "steer-1", "text": "补充 A", "provider_call_id": "call-7"},
        )
    )

    status = _status_lines(store)
    assert status == ["• 已送入当前回合，等待模型回应"], status
    assert not any("将在下一次工具调用后送入" in line for line in status)
    # 正文按原提交位置进入可见历史,且等待区不再重复正文。
    transcript = _transcript_lines(store)
    assert _count("补充 A", transcript) == 1, transcript
    assert not any("补充 A" in line for line in status)
    pending = store.snapshot().pending_steers
    assert len(pending) == 1
    assert pending[0].state == "submitted"
    assert pending[0].provider_call_id == "call-7"


def test_consumed_after_submitted_keeps_exactly_one_user_row() -> None:
    store = TuiStateStore()
    seq = _steer_seq(store)
    store.publish(
        seq.emit("steer_added", "queued", "steer:1", {"message_id": "steer-1", "text": "补充 A"})
    )
    store.publish(
        seq.emit(
            "steer_submitted", "started", "steer:1", {"message_id": "steer-1", "text": "补充 A"}
        )
    )
    store.publish(
        seq.emit(
            "steer_promoted", "completed", "user:req-1:steer:steer-1", {"message_id": "steer-1"}
        )
    )

    assert store.snapshot().pending_steers == ()
    assert _count("补充 A", _transcript_lines(store)) == 1
    assert _status_lines(store) == []


def test_slow_stream_keeps_submitted_state_while_assistant_streams() -> None:
    """慢流:助手正文还在增量输出时,已提交项必须是"等待模型回应",绝不能倒退成"稍后送入"。"""
    store = TuiStateStore()
    seq = _steer_seq(store)
    store.publish(
        seq.emit("steer_added", "queued", "steer:1", {"message_id": "steer-1", "text": "补充 A"})
    )
    store.publish(
        seq.emit(
            "steer_submitted", "started", "steer:1", {"message_id": "steer-1", "text": "补充 A"}
        )
    )
    for index in range(3):
        store.publish(
            seq.emit("assistant_delta", "delta", "assistant", {"text": f"第{index}段"})
        )

    status = _status_lines(store)
    assert status == ["• 已送入当前回合，等待模型回应"]
    assert _count("补充 A", _transcript_lines(store)) == 1


def test_submitted_then_turn_failed_degrades_without_auto_resend() -> None:
    """失败:回合终态后不得再承诺"等待回应",也不得承诺自动重发;用户消息行必须保留。"""
    store = TuiStateStore()
    seq = _steer_seq(store)
    store.publish(
        seq.emit("steer_added", "queued", "steer:1", {"message_id": "steer-1", "text": "补充 A"})
    )
    store.publish(
        seq.emit(
            "steer_submitted", "started", "steer:1", {"message_id": "steer-1", "text": "补充 A"}
        )
    )
    store.publish(seq.emit("turn_failed", "failed", "turn", {"error": "provider timeout"}))

    status = _status_lines(store)
    assert status == ["• 已送入当前回合但未获模型确认；不会自动重发"], status
    assert _count("补充 A", _transcript_lines(store)) == 1


def test_identity_not_text_controls_submitted_state() -> None:
    """身份只认 message_id:正文相同的另一条插话不得被误标为已提交。"""
    store = TuiStateStore()
    seq = _steer_seq(store)
    for message_id in ("steer-1", "steer-2"):
        store.publish(
            seq.emit(
                "steer_added",
                "queued",
                f"steer:{message_id}",
                {"message_id": message_id, "text": "完全相同的正文"},
            )
        )
    store.publish(
        seq.emit(
            "steer_submitted",
            "started",
            "steer:steer-1",
            {"message_id": "steer-1", "text": "完全相同的正文"},
        )
    )

    states = {item.message_id: item.state for item in store.snapshot().pending_steers}
    assert states == {"steer-1": "submitted", "steer-2": "queued"}
    status = _status_lines(store)
    assert "• 将在下一次工具调用后送入当前回合" in status  # steer-2 仍如实显示未送入
    assert "• 已送入当前回合，等待模型回应" in status


# --------------------------------------------------------------------------------------
# ③ 重连/补放:不丢不重
# --------------------------------------------------------------------------------------
def test_duplicate_submitted_event_does_not_duplicate_row() -> None:
    store = TuiStateStore()
    seq = _steer_seq(store)
    store.publish(
        seq.emit("steer_added", "queued", "steer:1", {"message_id": "steer-1", "text": "补充 A"})
    )
    for _ in range(2):
        store.publish(
            seq.emit(
                "steer_submitted", "started", "steer:1", {"message_id": "steer-1", "text": "补充 A"}
            )
        )

    assert _count("补充 A", _transcript_lines(store)) == 1
    assert len(store.snapshot().pending_steers) == 1


def test_replayed_submitted_then_consumed_rebuilds_one_row() -> None:
    """重连:本会话没有等待项,只能靠持久事件重建;两次事件只应产生一行,最终标记清空。"""
    store = TuiStateStore()
    seq = _steer_seq(store)
    store.publish(
        seq.emit(
            "steer_submitted",
            "started",
            "steer:steer-9",
            {"message_id": "steer-9", "text": "重连补放", "provider_call_id": "call-9"},
        )
    )
    assert _count("重连补放", _transcript_lines(store)) == 1
    assert _status_lines(store) == ["• 已送入当前回合，等待模型回应"]

    store.publish(
        seq.emit(
            "steer_promoted",
            "completed",
            "user:req-1:steer:steer-9",
            {"message_id": "steer-9"},
        )
    )

    assert store.snapshot().pending_steers == ()
    assert _count("重连补放", _transcript_lines(store)) == 1
    assert store.snapshot().diagnostics == ()


def test_terminal_child_keeps_truthful_label_for_submitted_steer() -> None:
    """子代理视角:已提交但未确认的插话在子代理结束后不得说成"稍后送入"。"""
    store = TuiStateStore()
    seq = _steer_seq(store)
    store.publish(
        seq.emit("steer_added", "queued", "steer:1", {"message_id": "steer-1", "text": "补充 A"})
    )
    store.publish(
        seq.emit(
            "steer_submitted", "started", "steer:1", {"message_id": "steer-1", "text": "补充 A"}
        )
    )

    frame = render_tui_snapshot(
        store.snapshot(),
        TuiRenderContext(
            width=80, focused_agent_run_id="subagent-1", focused_agent_status="DONE"
        ),
    )
    lines = _lines(frame.input_status_lines)
    assert lines == ["• 子代理已结束；以下插话已送入但未获模型确认，不会自动重发"], lines


def test_render_snapshot_keeps_legacy_pending_steer_construction_working() -> None:
    """兼容:旧的三字段构造(queued)仍然有效,状态默认值必须是 queued。"""
    from agent_py_agent.cli.chat_parts.tui_view_model import TuiPendingSteer

    legacy = TuiPendingSteer(message_id="steer-1", text="补充 A", seq=1)
    assert legacy.state == "queued"
    assert replace(legacy, state="submitted").provider_call_id == ""


# --------------------------------------------------------------------------------------
# ④ 确认是终态:迟到/重放的"已提交"不得把已确认消息降级
# --------------------------------------------------------------------------------------
def test_confirmed_steer_is_not_demoted_by_late_submitted_event() -> None:
    """复现验收序列: added -> submitted -> promoted -> submitted,等待项数量必须 1/1/0/0。"""
    store = TuiStateStore()
    seq = _steer_seq(store)
    payload = {"message_id": "steer-1", "text": "补充 A"}

    store.publish(seq.emit("steer_added", "queued", "steer:1", dict(payload)))
    assert len(store.snapshot().pending_steers) == 1

    store.publish(seq.emit("steer_submitted", "started", "steer:1", dict(payload)))
    assert len(store.snapshot().pending_steers) == 1
    assert store.snapshot().pending_steers[0].state == "submitted"

    store.publish(
        seq.emit("steer_promoted", "completed", "user:req:steer:steer-1", {"message_id": "steer-1"})
    )
    assert len(store.snapshot().pending_steers) == 0

    store.publish(seq.emit("steer_submitted", "started", "steer:1", dict(payload)))

    assert len(store.snapshot().pending_steers) == 0, "已确认消息不得被迟到的已提交事件重新挂起"
    assert _count("补充 A", _transcript_lines(store)) == 1, "用户行不得重复也不得消失"
    assert _status_lines(store) == []
    confirmed_replays = [
        item for item in store.snapshot().diagnostics if item.code == "STEER_CONFIRMED_REPLAY"
    ]
    assert len(confirmed_replays) == 1


def test_confirmed_steer_blocks_late_added_and_removed_events() -> None:
    """确认后同 id 的 added/removed 都是陈旧重放:不得复活等待项。"""
    store = TuiStateStore()
    seq = _steer_seq(store)
    store.publish(
        seq.emit("steer_promoted", "completed", "user:req:steer:steer-7", {"message_id": "steer-7"})
    )
    before_blocks = len(store.snapshot().stable_blocks)

    store.publish(
        seq.emit("steer_added", "queued", "steer:steer-7", {"message_id": "steer-7", "text": "旧正文"})
    )
    store.publish(
        seq.emit("steer_removed", "removed", "steer:steer-7", {"message_id": "steer-7"})
    )

    assert store.snapshot().pending_steers == ()
    assert len(store.snapshot().stable_blocks) == before_blocks


def test_reconnect_confirmation_blocks_replayed_submitted() -> None:
    """重连/切视角:先收到持久已确认身份,再收到重放的已提交事件,不得降级也不得重复建行。"""
    store = TuiStateStore()
    seq = _steer_seq(store)

    store.publish(seq.emit("steer_confirmed", "completed", "steer:steer-9", {"message_id": "steer-9"}))
    store.publish(
        seq.emit(
            "steer_submitted",
            "started",
            "steer:steer-9",
            {"message_id": "steer-9", "text": "重连补放", "provider_call_id": "call-9"},
        )
    )
    store.publish(
        seq.emit("user_message", "completed", "user:req:steer:steer-9", {"message_id": "steer-9", "text": "重连补放"})
    )

    assert store.snapshot().pending_steers == ()
    assert _count("重连补放", _transcript_lines(store)) == 1


# --------------------------------------------------------------------------------------
# ⑤ 短屏压缩视图:与完整视图同源的真实终态
# --------------------------------------------------------------------------------------
def _flood_receipts(store: TuiStateStore, sequencer, *, kind: str, count: int = 8) -> None:
    for index in range(count):
        store.publish(
            sequencer.emit(
                "steer_submitted" if kind == "submitted" else "steer_added",
                "started" if kind == "submitted" else "queued",
                f"steer:{kind}-{index}",
                {"message_id": f"{kind}-{index}", "text": f"补充 {index}"},
            )
        )


def test_compact_receipts_submitted_reflects_terminal_main_turn() -> None:
    """短屏压缩视图:主回合失败后不得再显示"等待模型回应"。"""
    store = TuiStateStore()
    seq = _steer_seq(store)
    _flood_receipts(store, seq, kind="submitted")

    running = _status_lines(store)
    assert any("已送入当前回合（等待模型回应）" in line for line in running), running

    store.publish(seq.emit("turn_failed", "failed", "turn", {"error": "provider timeout"}))

    failed = _status_lines(store)
    assert any("已送入当前回合但未获模型确认" in line for line in failed), failed
    assert not any("等待模型回应" in line for line in failed)


def test_compact_receipts_queued_does_not_promise_tool_round_after_terminal() -> None:
    """短屏压缩视图:主回合终态后不得承诺"等待接收"或"下一次工具调用"。"""
    store = TuiStateStore()
    seq = _steer_seq(store)
    _flood_receipts(store, seq, kind="queued")

    running = _status_lines(store)
    assert any("等待当前回合接收" in line for line in running), running

    store.publish(seq.emit("turn_completed", "completed", "turn", {}))

    done = _status_lines(store)
    assert any("当前回合已结束；插话未获消费确认" in line for line in done), done
    assert not any("等待当前回合接收" in line for line in done)
    assert not any("下一次工具调用" in line for line in done)


def test_full_pending_view_queued_label_is_truthful_after_terminal() -> None:
    """完整等待视图同样不得在终态承诺"将在下一次工具调用后送入"。"""
    store = TuiStateStore()
    seq = _steer_seq(store)
    store.publish(
        seq.emit("steer_added", "queued", "steer:1", {"message_id": "steer-1", "text": "补充 A"})
    )
    store.publish(seq.emit("turn_interrupted", "interrupted", "turn", {}))

    status = _status_lines(store)
    assert status[0] == "• 当前回合已结束；以下插话未获模型消费确认", status
    assert not any("下一次工具调用" in line for line in status)


def test_background_input_keeps_active_receipt_until_background_ends() -> None:
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime

    runtime = TuiRuntime("background-receipt")
    runtime.enqueue_active_turn_input("background-message", "补充中文标签要求")
    runtime.update_background_activity(1, {"main_activity": {"task_id": "goal-task"}})
    active = _status_lines(runtime.store)
    assert any("下一次工具调用" in line for line in active), active
    assert not any("回合已结束" in line for line in active), active
    runtime.update_background_activity(0, {})
    assert any("回合已结束" in line for line in _status_lines(runtime.store))
