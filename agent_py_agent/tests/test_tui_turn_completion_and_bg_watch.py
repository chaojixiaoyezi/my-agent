# LLM: 只断言程序事实、渲染与只读边界；缓存回归以真实读取次数/文件快照验证，临时数据不发唤醒或停止服务。
# 模块用途: 回归回合尾标、后台只读缓存、按会话隔离坏文件与告警恢复。
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from agent_py_agent.agent.conversation import background_process_view as bg
from agent_py_agent.agent.tooling.process_session_store import process_session_store_root
from agent_py_agent.cli.chat_parts.tui_agent_navigation import TuiAgentNavigationState
from agent_py_agent.cli.chat_parts.tui_block_renderer import (
    TuiRenderContext,
    fragments_text,
    render_tui_snapshot,
)
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime, TuiTurnSummary
from agent_py_agent.cli.turn_completion import format_turn_completion_line, format_turn_duration


@pytest.mark.parametrize('corrupt_index', [0, 1])
def test_round4_cold_notice_any_corrupt_store_fails_closed(tmp_path, corrupt_index):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.message_stream import NoticeDisplayCapabilities
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
    from agent_py_agent.agent.gateway_parts.http_handlers import _read_cold_owner_notice_projection
    from agent_py_agent.agent.owner_wake_discovery import existing_conversation_store_roots
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    host = tmp_path / 'host'
    home = host / 'owners/local/alice'
    stores = [ConversationStore(home / name) for name in ('conversations', 'data/conversations')]
    for store in stores:
        store.threads.get_or_create({'channel': 'chat', 'channel_conversation_id': 'session-fixture',
            'channel_user_id': 'alice', 'canonical_user_id': 'alice', 'owner_id': 'local/alice',
            'owner_home': str(home), 'workspace_root': str(home)})
    assert len(existing_conversation_store_roots(home)) == 2
    # 本版真实通道线程索引是channel_bindings.json，而非任务描述中的threads.json。
    stores[corrupt_index].threads.storage.bindings_path.write_text('{broken')
    before = {str(path): path.read_bytes() for path in home.rglob('*') if path.is_file()}
    payload = _read_cold_owner_notice_projection(SimpleNamespace(home_paths=SimpleNamespace(root=host)),
        scope=GatewayControlScope('alice', 'chat', 'session-fixture', resolved_owner=OwnerIdentity.local_user('alice')),
        after=0, event_after=0, event_stream_id='fixture-stream', display=NoticeDisplayCapabilities())
    assert payload['ok'] is False and payload['error'] == 'thread resolve failed'
    assert payload['thread_id'] == '' and payload['notices'] == []
    assert {str(path): path.read_bytes() for path in home.rglob('*') if path.is_file()} == before


# LLM: 只构造真实typed IR和冻结原生协议；缓存与物化运行产品入口，不替换指纹函数。
# 函数用途: 为H13准备可原位修改的嵌套工具参数。
def _h13_params(value=-1):
    from types import SimpleNamespace

    from agent_py_agent.agent.backends.tool_ir import AssistantTurn, ToolCall
    from agent_py_agent.agent.tooling.runtime_contracts import (
        ProviderToolCapability,
        ToolProtocolSnapshot,
    )

    call = ToolCall("h13-call", "probe", {"nested": [value]}, "native", "sha256:" + "a" * 64, "run", "turn", "attempt")
    capability = ProviderToolCapability("test", "local://fixture", "fixture", False, True, "fixture")
    return SimpleNamespace(tool_ir_history=[AssistantTurn(tool_calls=[call])], live_archive_state={},
                           session_id="h13", run_id="run", request_id="request",
                           tool_protocol_snapshot=ToolProtocolSnapshot("run", "native", capability))


# LLM: 原生预检先温缓存，随后canonical参数原位改变；真实物化入口不能把旧投影写回IR。
# 函数用途: 复现-1/-2整数及嵌套负数的hash碰撞污染。
@pytest.mark.parametrize("before,after", [(-1, -2), (-1.0, -2.0)])
def test_h13_materialization_preserves_changed_arguments(before, after):
    from agent_py_agent.agent.agent_core.model.context_pressure import (
        project_request_prompt_history,
    )
    from agent_py_agent.agent.agent_core.tool_model_generation import (
        ModelGenerateParams,
        _materialize_native_prompt_facts,
    )

    params = _h13_params(before)
    project_request_prompt_history(params, "prompt")
    params.tool_ir_history[0].tool_calls[0].arguments["nested"][0] = after
    request = ModelGenerateParams(object(), params, "prompt", 0)
    materialized = _materialize_native_prompt_facts(request)
    actual = materialized.params.tool_ir_history[0].tool_calls[0].arguments["nested"][0]
    assert actual == after and type(actual) is type(after)


# LLM: 调用方可能持有上次返回的投影；热命中前仍须验证缓存结果没有被原位改变。
# 函数用途: 拦住缓存输出被碰撞值污染后反向覆盖未变canonical IR。
def test_h13_cached_projection_output_is_structurally_revalidated():
    from agent_py_agent.agent.agent_core.model.context_pressure import (
        project_request_prompt_history,
    )

    params = _h13_params()
    _, first = project_request_prompt_history(params, "prompt")
    first[0].tool_calls[0].arguments["nested"][0] = -2
    _, second = project_request_prompt_history(params, "prompt")
    assert params.tool_ir_history[0].tool_calls[0].arguments["nested"] == [-1]
    assert second[0].tool_calls[0].arguments["nested"] == [-1]


# LLM: 类型、负零、容器种类都属于内容键；即使Python的hash或数值==相同，也不能当作同一输入。
# 函数用途: 锁定结构键在标量和容器上的精确区分。
@pytest.mark.parametrize("left,right", [(-1, -2), (-1.0, -2.0), (True, 1), (1, 1.0),
                                       (0.0, -0.0), ([1], (1,))])
def test_h13_projection_fingerprint_preserves_types_and_values(left, right):
    from agent_py_agent.agent.agent_core.model.context_pressure import _context_value_fingerprint

    assert _context_value_fingerprint({"nested": [left]}) != _context_value_fingerprint({"nested": [right]})


# LLM: 修复不能直接禁用缓存；同结构输入仍复用纯投影，只有实际结构变化才重算。
# 函数用途: 验证精确结构核对保留热缓存收益。
def test_h13_unchanged_projection_keeps_cache_hit(monkeypatch):
    from agent_py_agent.agent.agent_core import tool_ir_history
    from agent_py_agent.agent.agent_core.model.context_pressure import (
        project_request_prompt_history,
    )

    calls = []
    original = tool_ir_history.project_native_prompt_history

    # LLM: 包装原投影器只统计执行次数，不改变输出、输入或缓存键。
    # 函数用途: 观察冷计算与热命中。
    def observe(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(tool_ir_history, "project_native_prompt_history", observe)
    params = _h13_params()
    for _ in range(3):
        project_request_prompt_history(params, "prompt")
    assert len(calls) == 1


# LLM: 稳定列表模拟较旧工具晚完成，检查真实reducer与renderer的created_seq/updated_seq排序，而非列表顺序猜测。
# 函数用途: 复现完成计时重排翻转无关旧工具与新用户消息，并检查重复重放幂等。
@pytest.mark.parametrize("same_created_slot", [False, True])
def test_h14_completion_alignment_preserves_unrelated_render_order(same_created_slot):
    from agent_py_agent.cli.chat_parts.tui_block_renderer import _block_sort_key
    from agent_py_agent.cli.chat_parts.tui_events import TuiEvent
    from agent_py_agent.cli.chat_parts.tui_view_model import TuiBlock, TuiViewModelReducer

    request = "bg-main:thread:h14"
    final_id, timing_id = "history:thread:h14:assistant", f"{request}:completion"
    model = TuiViewModelReducer()
    model.stable_blocks = [
        TuiBlock("new-user", "user_message", "user", "completed", text="NEW_USER",
                 created_seq=10 if same_created_slot else 20, updated_seq=20),
        TuiBlock("old-tool", "tool_completed", "tool", "completed", title="probe", detail="OLD_TOOL",
                 created_seq=10, updated_seq=10),
        TuiBlock(timing_id, "system_message", "system", "completed", created_seq=30, updated_seq=30,
                 metadata={"source_kind": "turn_timing", "display": {"status": "completed",
                     "completed_at": 1700000000.0, "elapsed_seconds": 1.0}}),
        TuiBlock(final_id, "assistant_completed", "assistant", "completed", text="FINAL_BODY",
                 created_seq=40, updated_seq=40),
    ]
    expected = [block.block_id for block in sorted(model.stable_blocks, key=_block_sort_key)
                if block.block_id != timing_id]
    event = TuiEvent("h14-reorder", 100, "h14-stream", "h14-reorder", "history_blocks_reordered", "completed",
                     {"block_ids": [final_id, timing_id], "final_block_id": final_id}, request_id=request)
    model.apply(event)
    once = model.snapshot().stable_blocks
    for _ in range(2):
        model.apply(event)
    assert model.snapshot().stable_blocks == once
    ordered = [block.block_id for block in sorted(once, key=_block_sort_key)]
    assert [key for key in ordered if key != timing_id] == expected
    assert ordered.index(timing_id) == ordered.index(final_id) + 1
    frame = render_tui_snapshot(model.snapshot(), TuiRenderContext(width=180))
    rendered = [key for key, _offset in frame.block_line_offsets]
    assert rendered.index("old-tool") < rendered.index("new-user")
    assert rendered.index(timing_id) == rendered.index(final_id) + 1


# LLM: 独立复核给出源代码反例；先运行真实reducer证实，不能用静态意见替代动态失败证据。
# 函数用途: 检查早到计时不能拖走final，以及重复created槽位仍须按真实渲染键紧邻。
@pytest.mark.parametrize("tied_slots", [False, True])
def test_h14_review_counterexamples_preserve_final_anchor(tied_slots):
    from agent_py_agent.cli.chat_parts.tui_block_renderer import _block_sort_key
    from agent_py_agent.cli.chat_parts.tui_events import TuiEvent
    from agent_py_agent.cli.chat_parts.tui_view_model import TuiBlock, TuiViewModelReducer

    request, final_id = "bg-main:review", "history:review:assistant"
    timing_id = f"{request}:completion"
    model = TuiViewModelReducer()
    final = TuiBlock(final_id, "assistant_completed", "assistant", "completed",
                     created_seq=10 if tied_slots else 30, updated_seq=20 if tied_slots else 30)
    timing = TuiBlock(timing_id, "system_message", "system", "completed", created_seq=10, updated_seq=10)
    user = TuiBlock("user", "user_message", "user", "completed", created_seq=20, updated_seq=20)
    model.stable_blocks = [final, timing] if tied_slots else [timing, user, final]
    expected = [block.block_id for block in sorted(model.stable_blocks, key=_block_sort_key)
                if block.block_id != timing_id]
    event = TuiEvent("review", 100, "review", "review", "history_blocks_reordered", "completed",
                     {"block_ids": [final_id, timing_id], "final_block_id": final_id}, request_id=request)
    for _ in range(2):
        model.apply(event)
        actual = [block.block_id for block in sorted(model.stable_blocks, key=_block_sort_key)]
        assert [key for key in actual if key != timing_id] == expected
        assert actual.index(timing_id) == actual.index(final_id) + 1


# LLM: 稳定块的显示槽位若需调整，活动块仍须保持原相对位置，随后真实新事件也必须落在正确尾部。
# 函数用途: 保护旧活动工具、稳定用户和后续新用户的全局显示次序。
def test_h14_alignment_preserves_active_blocks_and_later_events():
    from agent_py_agent.cli.chat_parts.tui_block_renderer import _block_sort_key
    from agent_py_agent.cli.chat_parts.tui_events import TuiEvent
    from agent_py_agent.cli.chat_parts.tui_view_model import TuiBlock, TuiViewModelReducer

    request, final_id = "bg-main:active", "history:active:assistant"
    timing_id = f"{request}:completion"
    model = TuiViewModelReducer()
    model.stable_blocks = [
        TuiBlock(timing_id, "system_message", "system", "completed", created_seq=10, updated_seq=10),
        TuiBlock("user", "user_message", "user", "completed", created_seq=20, updated_seq=20),
        TuiBlock(final_id, "assistant_completed", "assistant", "completed", created_seq=30, updated_seq=30),
    ]
    model.active_blocks["old-active"] = TuiBlock("old-active", "tool_started", "tool", "running",
                                               created_seq=15, updated_seq=15)
    event = TuiEvent("active", 100, "active", "active", "history_blocks_reordered", "completed",
                     {"block_ids": [final_id, timing_id], "final_block_id": final_id}, request_id=request)
    model.apply(event)
    model.apply(TuiEvent("new-user", 101, "active", "new-user", "user_message", "completed", {"text": "new"}))
    all_blocks = [*model.stable_blocks, *model.active_blocks.values()]
    assert [block.block_id for block in sorted(all_blocks, key=_block_sort_key)] == [
        "old-active", "user", final_id, timing_id, "new-user"]


# LLM: 参数覆盖秒、分钟、小时和进位边界，不调用任何时钟或外部进程。
# 函数用途: 核对时长格式合同。
@pytest.mark.parametrize("seconds,expected", [(0, "0s"), (0.2, "1s"), (12, "12s"), (106, "1m 46s"), (120, "2m 0s"), (3725, "1h 2m 5s"), (59.9, "1m 0s")])
def test_duration(seconds, expected):
    assert format_turn_duration(seconds) == expected


# LLM: 固定动词只隔离选词变量；成功、中断与失败措辞必须沿同一格式入口。
# 函数用途: 核对三类终态尾标。
@pytest.mark.parametrize("status,expected", [("done", "✻ Baked for 1m 46s"), ("interrupted", "✻ Interrupted after 1m 46s"), ("failed", "✻ Failed after 1m 46s")])
def test_completion_line(status, expected):
    assert format_turn_completion_line(106, status=status, verb="Baked") == expected


# LLM: 缺省时刻只供实时格式化；历史生产必须传冻结的原时刻。
# 函数用途: 锁定任务要求的固定Worked及日期时区格式。
@pytest.mark.parametrize("ended", [None, "", 1700000000.0])
def test_completion_formatter_always_has_timestamp(ended):
    from agent_py_agent.cli import turn_completion

    formatter = getattr(turn_completion, "format_completion_line", None)
    assert callable(formatter)
    line = formatter(106, completed_at=ended)
    assert line.startswith("✻ Worked for 1m 46s · 完成于 ")
    assert "2023" in line if ended else len(line.split("完成于 ")[1].split()) == 3


# LLM: 真实快照生产、Gateway元数据和历史恢复共用本测试；只替换时钟，不伪造计时事件。
# 函数用途: 证明后台/前台完成事实冻结并在最终回复之后恢复，重复回放不新增时钟。
@pytest.mark.parametrize("foreground", [False, True])
def test_frozen_completion_survives_canonical_history_and_reorder(monkeypatch, foreground):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.background_history import BackgroundTurnHistory
    from agent_py_agent.agent.conversation.history_display import (
        conversation_history_display_events,
    )
    from agent_py_agent.agent.gateway_parts.request_history import GatewayAssistantTurn

    clock = [10.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    request = "bg-main:thread-one:timed"
    history = BackgroundTurnHistory("thread-one", "task-one", request)
    history.record("tool_completed", "completed", f"{request}:tool:1", {"tool": "read_file", "output": "ok", "ok": True})
    clock[0] = 116.0
    history.finish()
    frozen = history.snapshot()
    assert frozen.get("duration_s") == 106.0
    assert isinstance(frozen.get("completed_at"), (int, float))
    clock[0] = 900.0
    history.finish()
    assert history.snapshot() == frozen
    if foreground:
        frozen["gateway_request_id"] = "gateway-one"
    turn = GatewayAssistantTurn(end_reason="completed", display_snapshot=frozen)
    metadata = {**turn.metadata(), "assistant_part_id": "final", "task_id": "task-one"}
    metadata.update({"gateway_request_id": "gateway-one"} if foreground else {"background_delivery_reason": "continuation"})
    row = SimpleNamespace(thread_id="thread-one", message_id="message-one", role="assistant", content="最终后台回复",
                          metadata=metadata, channel="local", channel_message_id="", created_at=1700000000.0)
    events = conversation_history_display_events((row,))
    assert [item["block_id"] for item in events][-2:] == ["history:thread-one:message-one:assistant", f"{request}:completion"]
    runtime = TuiRuntime("frozen-timing")
    # 先收到计时，随后补完整过程；重排仍须保持正文→计时。
    runtime.publish_recovered_history((), display_events=(events[-1],))
    for _ in range(2):
        runtime.publish_recovered_history((), display_events=events)
    ids = [block.block_id for block in runtime.store.snapshot().stable_blocks]
    assert ids[-2:] == ["history:thread-one:message-one:assistant", f"{request}:completion"]
    frame = render_tui_snapshot(runtime.store.snapshot(), TuiRenderContext(width=180))
    text = [fragments_text(line) for line in frame.transcript_lines]
    assert sum("Worked for 1m 46s" in line for line in text) == 1
    assert "完成于" not in fragments_text(frame.footer) and not runtime.completions.clocks


# LLM: 失败与取消也可能冻结显示片，但不能以此宣称成功；旧快照没有时间不补造。
# 函数用途: 拦住失败/取消和旧历史的假完成时间。
@pytest.mark.parametrize("reason", ["error", "aborted", "interrupted", "max-tokens", "blocked", ""])
def test_unsuccessful_or_legacy_snapshot_never_fabricates_completion(reason):
    from agent_py_agent.agent.conversation.background_history import BackgroundTurnHistory
    from agent_py_agent.agent.gateway_parts.request_history import GatewayAssistantTurn

    history = BackgroundTurnHistory("t", "task", "bg-main:t:test")
    history.finish()
    snapshot = history.snapshot()
    metadata = GatewayAssistantTurn(end_reason=reason, display_snapshot=snapshot).metadata()
    assert not any(item["block_id"].endswith(":completion") for item in metadata["background_display_turn"]["events"])
    snapshot.pop("duration_s", None)
    snapshot.pop("completed_at", None)
    metadata = GatewayAssistantTurn(end_reason="completed", display_snapshot=snapshot).metadata()
    assert not any(item["block_id"].endswith(":completion") for item in metadata["background_display_turn"]["events"])


# LLM: 受损旧计时事件不能从renderer补当前墙钟或令整个正文渲染崩溃。
# 函数用途: 验证已有历史块缺失/非法时标时不显示假完成日期。
@pytest.mark.parametrize("stamp", [None, "", 1e100, True, float("nan")])
def test_invalid_frozen_timing_is_omitted_not_replaced_with_now(stamp):
    runtime = TuiRuntime("invalid-history-time")
    request = "history:thread-one:message-one"
    event = {"schema": "conversation_history_display.v1", "request_id": request, "block_id": f"{request}:completion",
             "kind": "system_message", "phase": "completed", "payload": {"source_kind": "turn_timing", "display": {
                 "status": "completed", "completed_at": stamp, "elapsed_seconds": 1.0}}}
    runtime.publish_recovered_history((), display_events=(event,))
    frame = render_tui_snapshot(runtime.store.snapshot(), TuiRenderContext(width=180))
    assert not any("完成于" in fragments_text(line) or "✻" in fragments_text(line) for line in frame.transcript_lines)


# LLM: 有限浮点也可能超出平台datetime范围；不能因附加展示信息失败丢掉canonical正文。
# 函数用途: 验证metadata构造安全拒绝不可表示时刻。
def test_out_of_range_completion_timestamp_does_not_break_gateway_metadata():
    from agent_py_agent.agent.gateway_parts.request_history import GatewayAssistantTurn

    snapshot = {"request_id": "bg-main:t:test", "complete": True, "duration_s": 1.0, "completed_at": 1e100, "events": []}
    metadata = GatewayAssistantTurn(end_reason="completed", display_snapshot=snapshot).metadata()
    assert metadata["background_display_turn"]["events"] == []


# LLM: 计时先到、其它回合插入、canonical后到是独立接线交错；相对排序不能代替紧邻保证。
# 函数用途: 验证跨片补齐和重复重放都把计时放到原final正后方，不夹其它回合。
def test_history_reorder_keeps_final_and_timing_adjacent_across_other_turn():
    runtime = TuiRuntime("interleaved-timing")
    request = "bg-main:thread-one:timed"
    timing = {"schema": "conversation_history_display.v1", "request_id": request, "block_id": f"{request}:completion",
              "kind": "system_message", "phase": "completed", "payload": {"source_kind": "turn_timing", "display": {
                  "status": "completed", "completed_at": 1700000000.0, "elapsed_seconds": 1.0}}}
    final = {"schema": "conversation_history_display.v1", "request_id": "history:thread-one:final",
             "block_id": "history:thread-one:final:assistant", "kind": "assistant_completed", "phase": "completed",
             "covered_background_request_id": request, "payload": {"text": "后台回复"}}
    runtime.publish_recovered_history((), display_events=(timing,))
    runtime.write_console("其它回合说明")
    for _ in range(2):
        runtime.publish_recovered_history((), display_events=(final,))
    ids = [block.block_id for block in runtime.store.snapshot().stable_blocks]
    assert ids.index(timing["block_id"]) == ids.index(final["block_id"]) + 1


# LLM: 运行真实后台runtime与canonical存储，仅模型由现有脚本化backend提供；不发送外网通知。
# 函数用途: 证明背景成功回合不靠手造事件即可落盘并回放计时。
def test_background_runtime_commits_and_replays_original_completion(tmp_path):
    from agent_py_agent.agent.conversation import BackgroundMainAgentRuntime
    from agent_py_agent.agent.conversation.history_display import (
        conversation_history_display_events,
    )
    from agent_py_agent.agent.delivery import DeliveryService, build_default_channel_registry
    from agent_py_agent.tests.test_background_owner_delivery_commit import _agent

    agent, _ = _agent(tmp_path, backend_text="后台计时原回复")
    store = agent.conversation_store
    thread = store.threads.get_or_create({"canonical_user_id": "owner-test", "channel": "local",
        "channel_conversation_id": "timing-background", "channel_user_id": "owner-test"})
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": "task-timing", "goal": "后台计时回归", "status": "active"})
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=DeliveryService(build_default_channel_registry(agent.config)))
    report = runtime.run_once({"thread_id": thread.thread_id, "task_id": "task-timing", "reason": "scheduled_progress_report",
                               "route_channel": "local", "route_target": "timing-background"})
    assert report.persisted if hasattr(report, "persisted") else report.message_id
    rows = store.messages.recent(thread.thread_id, limit=0)
    final = next(row for row in rows if row.metadata.get("assistant_part_id") == "final")
    frozen = final.metadata["background_display_turn"]
    assert frozen["events"][-1]["block_id"] == f"{frozen['request_id']}:completion"
    events = conversation_history_display_events(rows)
    ui = TuiRuntime("real-background-history")
    ui.publish_recovered_history((), display_events=events)
    frame = render_tui_snapshot(ui.store.snapshot(), TuiRenderContext(width=180))
    text = [fragments_text(line) for line in frame.transcript_lines]
    assert sum("✻ Worked for" in line and "完成于" in line for line in text) == 1
    assert "完成于" not in fragments_text(frame.footer)


# LLM: 只替换单调时钟；真实 runtime、去重与渲染入口共同证明尾标位置和样式。
# 函数用途: 验证最终正文后只出现一次灰色计时，原底栏不被覆盖。
def test_completion_is_dim_in_transcript_not_footer(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    runtime = TuiRuntime("wp5-timing")
    runtime.enqueue_prompt("turn-one", "任务", queued=False)
    adapter = runtime.begin_turn("turn-one")
    clock[0] = 116.0
    adapter.finalize(TuiTurnSummary(response_text="最终回复"))
    adapter.finalize(TuiTurnSummary(response_text="最终回复"))
    snapshot = runtime.store.snapshot()
    frame = render_tui_snapshot(snapshot, TuiRenderContext(width=180))
    transcript = [fragments_text(line) for line in frame.transcript_lines]
    timing = [text for text in transcript if "✻" in text]
    assert len(timing) == 1
    assert "1m 46s" in timing[0] and "完成于" in timing[0]
    assert transcript.index(timing[0]) > next(i for i, text in enumerate(transcript) if "最终回复" in text)
    assert "F6 原生模式" in fragments_text(frame.footer)
    assert "✻" not in fragments_text(frame.footer)
    assert any(style == "class:tui-muted" and "✻" in text for line in frame.transcript_lines for style, text, *_ in line)


# LLM: 仅在测试临时目录构造记录和超限日志；不登记或启动真实受管进程。
# 函数用途: 准备传统目录与真实目录格式的观察样本。
def _session(home: Path, *, session_id="bg-example", status="running", thread_id="thread-one", native=False):
    home.mkdir(parents=True, exist_ok=True)
    root = process_session_store_root(home, home) if native else home / "workspace/runtime/processes/sessions"
    root.mkdir(parents=True, exist_ok=True)
    output = root / f"{session_id}.log"
    output.write_text("old\n" + "x" * 70000 + "\nline-one\nline-two\n", encoding="utf-8")
    payload = {"session_id": session_id, "command": "python worker.py", "status": status, "pid": os.getpid(),
               "cwd": str(home), "thread_id": thread_id, "started_at": time.time() - 106,
               "exit_code": 0 if status == "exited" else None, "output_path": str(output),
               "completion_notice_id": "notified" if status == "exited" else "",
               "completion_target": {"thread_id": thread_id, "task_id": "task-one", "run_id": "run-one"}}
    (root / f"{session_id}.json").write_text(json.dumps(payload), encoding="utf-8")
    return root, output


# LLM: 字节、inode 和 mtime 同时快照；观察前后比较可捕捉写回和新建锁文件。
# 函数用途: 记录临时数据树的文件事实。
def _files(root):
    return {str(path.relative_to(root)): (path.stat().st_ino, path.stat().st_mtime_ns, path.read_bytes())
            for path in root.rglob("*") if path.is_file()}


# LLM: 覆盖两个目录来源并检查线程筛选、活动数、尾部上限和唤醒文件不变。
# 函数用途: 证明列表与详情入口没有磁盘副作用。
@pytest.mark.parametrize("native", [False, True])
def test_list_and_inspect_are_read_only(tmp_path, native):
    home = tmp_path / "owner"
    root, _ = _session(home, native=native)
    _session(home, session_id="bg-finished", status="exited", native=native)
    wake = home / "pending_process_events.json"
    wake.write_text('{"pending":["wake-one"]}', encoding="utf-8")
    before = _files(tmp_path)
    result = bg.list_background_processes(home, thread_id="thread-one")
    assert result["total"] == 2 and result["active_count"] == 1
    assert bg.list_background_processes(home, thread_id="other")["items"] == []
    assert len(bg.list_background_processes(home, active_only=True)["items"]) == 1
    detail = bg.inspect_background_process(home, "bg-example", tail_lines=2)
    assert detail["log_tail"] == ["line-one", "line-two"]
    assert detail["pid"] == os.getpid() and detail["status"] == "running"
    assert detail["task_id"] == "task-one"
    assert _files(tmp_path) == before
    assert not (root / ".process-sessions.lock").exists()


# LLM: 恶意身份必须在读取前被拒绝，不能通过路径解析越出会话目录。
# 函数用途: 回归进程身份的路径穿越防护。
@pytest.mark.parametrize("session_id", ["../bad", "bg-../bad", "/tmp/bad", "bg-a/b", "bg-a\\b"])
def test_inspect_rejects_traversal(tmp_path, session_id):
    with pytest.raises(ValueError):
        bg.inspect_background_process(tmp_path, session_id)


# LLM: 缺失目录也是只读边界，列表空结果不能附带 mkdir。
# 函数用途: 验证观察不会创建私人空间。
def test_missing_home_is_not_created(tmp_path):
    home = tmp_path / "missing"
    assert bg.list_background_processes(home)["items"] == []
    assert not home.exists()


# LLM: 导航只用真实展示状态机；进程页面不能暴露代理控制身份或可输入权限。
# 函数用途: 核对后台入口优先、只读检查和返回路径。
def test_navigation_background_entry_precedes_goal_and_children():
    root = TuiRuntime("wp5-nav")
    nav = TuiAgentNavigationState(root)
    update = getattr(nav, "update_background_watch_rows", lambda rows: False)
    assert update([{"session_id": "bg-example", "command": "worker", "pid": 123,
                    "status": "running", "alive": True, "elapsed_seconds": 106, "log_tail": ["latest"]}]) is True
    nav.update_rows("", [{"run_id": "child-one", "parent_run_id": "", "status": "RUNNING"}])
    nav.update_goal_rows([{"goal_id": "goal-one", "status": "active"}])
    nav.move_selection(-1)
    assert nav.snapshot().selected_run_id == "bg:watch"
    assert nav.enter_selected()
    assert nav.snapshot().can_send_input is False
    assert nav.snapshot().active_run_id == ""
    assert nav.snapshot().background_watch_active is True
    watch = render_tui_snapshot(nav.active_runtime().store.snapshot(), TuiRenderContext(width=180))
    assert any("latest" in fragments_text(line) for line in watch.transcript_lines)
    assert nav.back() and nav.active_runtime() is root
    nav.move_selection(1)
    assert nav.snapshot().selected_run_id == "goal:goal-one"


# LLM: 样式、底栏顺序与缓存键必须同时反映后台计数，不能只核对文案。
# 函数用途: 验证后台徽标可见且计数变化触发新帧。
def test_background_badge_is_colored_after_f6_and_part_of_frame_key():
    from dataclasses import replace

    from agent_py_agent.cli.chat_parts.tui_block_renderer import tui_render_context_key

    runtime = TuiRuntime("badge")
    context = TuiRenderContext(width=180)
    # 字段尚未接通时在断言上红灯，而不是构造参数异常。
    assert "background_process_count" in context.__dataclass_fields__
    active = replace(context, background_process_count=2)
    frame = render_tui_snapshot(runtime.store.snapshot(), active)
    text = fragments_text(frame.footer)
    assert text.index("F6 原生模式") < text.index("⚡ 后台驻留 2 项")
    assert any(style == "class:tui-bg-watch" for style, *_ in frame.footer)
    assert tui_render_context_key(runtime.store.snapshot(), context) != tui_render_context_key(runtime.store.snapshot(), active)


# LLM: 多次观察更新只能替换活动块，不应向根对话或观察页永久历史追加日志。
# 函数用途: 核对日志选择与可移除观察块的实时更新。
def test_watch_page_refreshes_same_removable_block():
    root = TuiRuntime("watch-live")
    nav = TuiAgentNavigationState(root)
    rows = [{"session_id": "bg-one", "status": "running", "alive": True, "command": "one", "log_tail": ["first"]},
            {"session_id": "bg-two", "status": "running", "alive": True, "command": "two", "log_tail": ["second"]}]
    assert getattr(nav, "update_background_watch_rows", lambda _: False)(rows)
    nav.move_selection(1)
    nav.enter_selected()
    nav.move_selection(1)
    assert nav.snapshot().selected_run_id == "bg:bg-two"
    assert nav.update_background_watch_rows([{**rows[0]}, {**rows[1], "log_tail": ["new-second"]}])
    frame = render_tui_snapshot(nav.active_runtime().store.snapshot(), TuiRenderContext(width=180))
    text = "\n".join(fragments_text(line) for line in frame.transcript_lines)
    assert "new-second" in text and "first" not in text
    assert len(nav.active_runtime().store.snapshot().active_blocks) == 1
    assert not nav.active_runtime().store.snapshot().stable_blocks
    assert not root.store.snapshot().stable_blocks


# LLM: Wave9新会话不再读取owner全量；夹具显式绑定canonical线程，原只读文件与按需日志断言保持。
# 函数用途: 验证已绑定会话的轮询将只读进程投影接入导航与日志。
def test_polling_connects_owner_records_and_only_reads_logs_when_watching(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.cli.chat_parts.tui_threading import _consume_background_notices

    home = tmp_path / "owner"
    _session(home, thread_id="thread-one")
    root = TuiRuntime("poll")
    nav = TuiAgentNavigationState(root)
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=home), thread_id="thread-one")
    before = _files(tmp_path)
    assert _consume_background_notices(agent, "session", root, [None], set(), [0], nav)
    assert nav.snapshot().background_process_count == 1
    nav.move_selection(1)
    nav.enter_selected()
    assert _consume_background_notices(agent, "session", root, [None], set(), [0], nav)
    frame = render_tui_snapshot(nav.active_runtime().store.snapshot(), TuiRenderContext(width=180))
    assert any("line-two" in fragments_text(line) for line in frame.transcript_lines)
    assert _files(tmp_path) == before


# LLM: Esc 直接走原按键函数并记录中断派发反例，不能把退出观察误作取消前台。
# 函数用途: 验证后台观察的立即返回边界。
def test_escape_from_watch_never_interrupts_foreground(monkeypatch):
    import threading
    from types import SimpleNamespace

    from prompt_toolkit.widgets import TextArea

    from agent_py_agent.cli.chat_parts import tui_keybindings as keys

    root = TuiRuntime("watch-escape")
    nav = TuiAgentNavigationState(root)
    nav.update_background_watch_rows([{"session_id": "bg-example", "alive": True}])
    nav.move_selection(1)
    nav.enter_selected()
    interrupted = []
    monkeypatch.setattr(keys, "_dispatch_active_interrupt", lambda *args: interrupted.append(True))
    params = SimpleNamespace(input_area=TextArea(), agent_navigation=nav, tui_runtime=root,
                             state_lock=threading.RLock(), is_running_ref=[True], pending_jobs_ref=[0],
                             exit_armed_at_ref=[0.0], eof_armed_at_ref=[0.0],
                             escape_armed_at_ref=[0.0], escape_armed_text_ref=[""])
    keys._handle_escape_keybinding(SimpleNamespace(app=SimpleNamespace(invalidate=lambda: None)), params)
    assert interrupted == []
    assert nav.active_runtime() is root


# LLM: 损坏观察记录必须显式报错但仍获取原通知，不能拖停独立的同步链路。
# 函数用途: 回归坏记录与原通知同步的隔离。
def test_bad_process_record_does_not_block_original_notice_fetch(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.cli.chat_parts.tui_threading import _consume_background_notices

    home = tmp_path / "owner"
    root, _ = _session(home)
    (root / "bg-broken.json").write_text("broken-json", encoding="utf-8")
    fetched = []

    # LLM: 记录真实同步接缝是否被调用，不模拟观察器的异常处理。
    # 函数用途: 提供已确认线程的成功响应，确保告警来自坏进程记录而不是缺失范围的旧误报。
    def fetch(*args, **kwargs):
        fetched.append(True)
        return {"ok": True, "thread_id": "thread-one", "notices": [], "cursor": 0, "transcript_events": [], "event_cursor": 0, "event_stream_id": ""}

    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=home), request_background_notices=fetch)
    runtime = TuiRuntime("read-failure")
    nav = TuiAgentNavigationState(runtime)
    try:
        result = _consume_background_notices(agent, "session", runtime, [None], set(), [0], nav)
    except OSError:
        result = False
    assert result and fetched == [True]
    assert "观察刷新失败" in runtime.notice()


# LLM: 不安全日志仍应拒读，但选中项详情失败不能阻止原通知响应进入 TUI。
# 函数用途: 回归日志拒读与正常通知链路互不干扰。
def test_bad_log_path_does_not_block_original_notice_fetch(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.cli.chat_parts.tui_threading import _consume_background_notices

    home = tmp_path / "owner"
    root, _ = _session(home)
    record_path = root / "bg-example.json"
    payload = json.loads(record_path.read_text())
    payload["output_path"] = str(tmp_path / "outside-secret")
    record_path.write_text(json.dumps(payload), encoding="utf-8")
    runtime = TuiRuntime("bad-log")
    nav = TuiAgentNavigationState(runtime)
    nav.update_background_watch_rows([{"session_id": "bg-example", "alive": True}])
    nav.move_selection(1)
    nav.enter_selected()
    calls = []
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=home), request_background_notices=lambda *args, **kwargs: calls.append(True) or
        {"ok": True, "notices": [], "cursor": 0, "transcript_events": [], "event_cursor": 0, "event_stream_id": ""})
    try:
        result = _consume_background_notices(agent, "session", runtime, [None], set(), [0], nav)
    except ValueError:
        result = False
    assert result and calls == [True]


# LLM: 完整活动数是独立后端事实，不能从有界展示行再次统计。
# 函数用途: 验证大列表截断不缩小徽标计数。
def test_background_count_is_not_limited_to_visible_rows():
    import inspect

    nav = TuiAgentNavigationState(TuiRuntime("count-all"))
    assert "active_count" in inspect.signature(nav.update_background_watch_rows).parameters
    nav.update_background_watch_rows([{"session_id": "bg-example", "alive": True}], active_count=140)
    assert nav.snapshot().background_process_count == 140


# LLM: 缺失终止时间的终态记录不能用当前时间补成持续增长的计时。
# 函数用途: 验证未知耗时保持未知。
def test_terminal_missing_end_time_is_unknown_and_not_growing(tmp_path):
    home = tmp_path / "owner"
    _session(home, status="exited")
    first = bg.inspect_background_process(home, "bg-example")
    assert first["elapsed_seconds"] is None


# LLM: 复用真实 v4 夹具并禁止可写事务；伪 PID 不代表真实进程生命周期验证。
# 函数用途: 验证规范记录与待唤醒状态可只读观察。
def test_actual_v4_records_are_observed_without_recovery_or_wake_consumption(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling.process_session_store import ProcessSessionStore
    from agent_py_agent.tests import test_process_completion_events as fixtures

    # 夹具中的 PID 为伪终态实例；本测试只验真实 v4 文件读取，不探测伪实例出生时间。
    monkeypatch.setattr(fixtures, "capture_process_birth_token", lambda pid: "fixture-launcher")
    agent, _, authority = fixtures._fixture(tmp_path, managed=True)
    home = agent.home_paths.owner_home_dir
    before = _files(tmp_path)

    # LLM: 观察入口一旦误入 Store 事务就明确失败，不允许恢复或落盘。
    # 函数用途: 捕获观察器的可写查询回归。
    def forbidden(*args, **kwargs):
        pytest.fail("观察不得进入可写的 Store 事务")

    monkeypatch.setattr(ProcessSessionStore, "transaction", forbidden)
    result = bg.list_background_processes(home)
    assert result["active_count"] == 1 and result["total"] == 1
    detail = bg.inspect_background_process(home, "bg-test-notice")
    assert detail["status"] == "exited" and detail["wake_pending"] is True
    assert detail["elapsed_seconds"] == 1.0
    assert _files(tmp_path) == before
    assert authority.root.exists()


# LLM: 运行真实 Application 与键盘管道，读取渲染屏幕和输入框；不启动真实模型或后台进程。
# 函数用途: 验证后台页面按键、实际显示、拒绝输入及返回后的草稿回读。
def test_prompt_toolkit_keys_open_readonly_watch_and_return_with_ctrl_g(tmp_path):
    import asyncio

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from agent_py_agent.cli.chat_parts.tui_ui_setup import make_tui_app
    from agent_py_agent.tests.test_tui_decision_menu import wait_app
    from agent_py_agent.tests.test_tui_prompt_toolkit_pipe import _app_params

    # LLM: 保持 Application 生命周期完整，最终等待退出，避免把已发送按键当作操作成功。
    # 函数用途: 执行观察页进入、切换、返回与输入验证。
    async def scenario():
        runtime = TuiRuntime("watch-keys")
        runtime.publish_session(version="test", model="fixture", workspace=str(tmp_path))
        nav = TuiAgentNavigationState(runtime)
        nav.update_background_watch_rows([
            {"session_id": "bg-one", "command": "first", "alive": True, "log_tail": ["first output"]},
            {"session_id": "bg-two", "command": "second", "alive": True, "log_tail": ["second output"]},
        ])
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            params = _app_params(tmp_path, runtime, agent_navigation=nav)
            app = make_tui_app(params)
            task = asyncio.create_task(app.run_async())

            # LLM: 只读最近真实渲染的字符网格，不能用缺少正文控件的标签收集器代替。
            # 函数用途: 取得已绘制屏幕供观察页可见性断言。
            def rendered_screen():
                screen = app.renderer._last_screen
                return "\n".join("".join(row[x].char for x in sorted(row)) for _, row in sorted(screen.data_buffer.items())) if screen else ""

            try:
                await wait_app(app, lambda: app.is_running and app.render_counter > 0, "观察页测试启动")
                pipe.send_bytes(b"\x1b[A")
                await wait_app(app, lambda: nav.snapshot().selected_run_id == "bg:watch", "上键选中后台徽标")
                pipe.send_bytes(b"\r")
                await wait_app(app, lambda: nav.snapshot().background_watch_active, "进入观察页")
                await wait_app(app, lambda: "后台驻留监控" in rendered_screen(), "后台观察页正文实际显示")
                assert app.current_buffer.read_only()
                pipe.send_text("must-not-send")
                pipe.send_bytes(b"\x1b[B")
                await wait_app(app, lambda: nav.snapshot().selected_run_id == "bg:bg-two", "观察页下键切换日志")
                assert app.current_buffer.text == "" and params.jobs.empty()
                assert not nav.selected_goal()
                pipe.send_bytes(b"\x07")
                await wait_app(app, lambda: not nav.snapshot().background_watch_active, "CtrlG返回主对话")
                assert not app.current_buffer.read_only()
                pipe.send_text("returned draft")
                await wait_app(app, lambda: app.current_buffer.text == "returned draft", "回读主对话草稿")
                assert params.jobs.empty()
            finally:
                app.exit(result=0)
                assert await task == 0

    asyncio.run(scenario())


# LLM: 覆盖越界、软链接与多链接拒读；宿主禁止建硬链接时明确仅验证合成 nlink 反例。
# 函数用途: 核对日志入口的文件身份防护且不改来源内容。
@pytest.mark.parametrize("kind", ["outside", "symlink", "hardlink"])
def test_inspection_rejects_unsafe_log_files(tmp_path, monkeypatch, kind):
    from agent_py_agent.agent.common.nofollow_fs import NoFollowPathError

    home = tmp_path / "owner"
    root, output = _session(home)
    secret = tmp_path / "secret"
    secret.write_text("never disclose", encoding="utf-8")
    record_path = root / "bg-example.json"
    payload = json.loads(record_path.read_text())
    if kind == "outside":
        payload["output_path"] = str(secret)
    else:
        replacement = root / "unsafe.log"
        if kind == "symlink":
            replacement.symlink_to(secret)
        else:
            try:
                os.link(secret, replacement)
            except PermissionError:
                # 本机明确拒绝创建硬链接；保留 nlink=2 的同入口反例，不把 EPERM 当产品拒读证据。
                replacement.write_text("never disclose", encoding="utf-8")
                original_stat = os.stat

                # LLM: 仅对指定日志合成链接数，其余 stat 沿原实现；不冒充真实硬链接验证。
                # 函数用途: 在宿主禁止建链时保留同入口的多链接拒读反例。
                def linked_stat(path, *args, **kwargs):
                    observed = original_stat(path, *args, **kwargs)
                    if str(path) == "unsafe.log" or str(path) == str(replacement):
                        fields = list(observed)
                        fields[3] = 2
                        return os.stat_result(fields)
                    return observed

                monkeypatch.setattr(os, "stat", linked_stat)
                monkeypatch.setattr(os, "supports_dir_fd", {*os.supports_dir_fd, linked_stat})
                monkeypatch.setattr(os, "supports_follow_symlinks", {*os.supports_follow_symlinks, linked_stat})
        payload["output_path"] = str(replacement)
    record_path.write_text(json.dumps(payload), encoding="utf-8")
    before = output.read_bytes()
    with pytest.raises((OSError, ValueError)) as failure:
        bg.inspect_background_process(home, "bg-example")
    if kind == "hardlink":
        assert isinstance(failure.value, NoFollowPathError) and "not a regular file" in str(failure.value)
    assert output.read_bytes() == before and secret.read_text() == "never disclose"


# LLM: 输入明确的宿主展示事件并重放两次；此测试不证明上游计时生产或持久化已接好。
# 函数用途: 核对历史与后台计时展示重放去重且不会重启时钟。
@pytest.mark.parametrize("background", [False, True])
def test_host_timing_display_events_replay_without_new_clock(background):
    runtime = TuiRuntime("timing-replay")
    request = "bg-main:task-one:attempt-one" if background else "history:thread-one:message-one"
    row = {"schema": "background_transcript_event.v1" if background else "conversation_history_display.v1",
           "seq": 1, "task_id": "task-one", "request_id": request, "block_id": f"{request}:completion",
           "kind": "system_message", "phase": "completed",
           "payload": {"text": "原程序摘要", "source_kind": "turn_timing", "display": {
               "logical_id": "request-one", "status": "completed", "completed_at": 1700000000.0,
               "elapsed_seconds": 106.0, "source": "gateway"}}}
    for _ in range(2):
        if background:
            runtime.publish_background_transcript_events([row])
        else:
            runtime.publish_recovered_history((), display_events=(row,))
    frame = render_tui_snapshot(runtime.store.snapshot(), TuiRenderContext(width=180))
    timings = [fragments_text(line) for line in frame.transcript_lines if "✻" in fragments_text(line)]
    assert len(timings) == 1 and "1m 46s" in timings[0] and "2023" in timings[0]
    assert not runtime.completions.clocks


# LLM: 只隔离通知获取并记录首轮计划等待值，不等待真实一秒或开启驻留线程。
# 函数用途: 验证后台活动时轮询仍采用活动节奏。
def test_background_processes_keep_notice_loop_at_active_cadence(monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.cli.chat_parts import tui_threading

    # LLM: 单轮退出夹具仅捕获等待参数，不参与生产状态或生命周期。
    # 类用途: 让轮询节奏断言可确定地收口。
    class Stop:
        # LLM: 首次检查保持可运行，终止由 wait 的确定返回值负责。
        # 函数用途: 允许测试进入一次轮询。
        def is_set(self):
            return False

        # LLM: 不真实睡眠，记录待验证的延迟后要求退出。
        # 函数用途: 捕获活动轮询间隔。
        def wait(self, delay):
            waits.append(delay)
            return True

    waits = []
    monkeypatch.setattr(tui_threading, "_consume_background_notices", lambda *args: True)
    runtime = TuiRuntime("bg-cadence")
    nav = TuiAgentNavigationState(runtime)
    nav.update_background_watch_rows([{"session_id": "bg-running", "alive": True}])
    tui_threading._background_notice_loop(Stop(), SimpleNamespace(), "session", runtime, [None], nav, [False])
    assert waits == [tui_threading.TUI_BACKGROUND_NOTICE_INTERVAL_SECONDS]


# LLM: 恢复原生空串未绑定身份；同owner线程过滤、全owner展示及只读断言保持不变。
# 函数用途: 验证列表严格按会话过滤、Owner全局入口保留未绑定记录及只读文件边界。
@pytest.mark.parametrize("thread_id,expected", [
    (None, {"bg-one", "bg-two", "bg-global"}),
    ("", {"bg-one", "bg-two", "bg-global"}),
    ("thread-one", {"bg-one"}),
    ("thread-two", {"bg-two"}),
])
def test_thread_scoped_background_list_keeps_global_records(tmp_path, thread_id, expected):
    home = tmp_path / "owner"
    for sid, thread in [("bg-one", "thread-one"), ("bg-two", "thread-two"), ("bg-global", "")]:
        _session(home, session_id=sid, thread_id=thread)
    before = _files(tmp_path)
    result = bg.list_background_processes(home, thread_id=thread_id)
    assert {item["session_id"] for item in result["items"]} == expected
    assert result["total"] == len(expected) and result["active_count"] == len(expected)
    assert _files(tmp_path) == before


# LLM: 详情的线程拒绝必须早于日志打开；显式指定线程时跨线程和未绑定记录均拒绝，未指定线程继续支持全局只读。
# 函数用途: 防止同用户其它会话通过直接session_id读取非本会话日志。
def test_thread_scoped_inspection_rejects_foreign_record_before_log_read(tmp_path, monkeypatch):
    import inspect

    assert "thread_id" in inspect.signature(bg.inspect_background_process).parameters
    home = tmp_path / "owner"
    _session(home, session_id="bg-one", thread_id="thread-one")
    _session(home, session_id="bg-global", thread_id="")
    before = _files(tmp_path)
    original = bg._log_tail
    reads = []

    # LLM: 仍读取真实安全日志路径，只记录被打开的session；拒绝项不得触达该接缝。
    # 函数用途: 证明跨线程请求没有先读取日志再过滤返回值。
    def log_tail(home, root, sid, payload, count):
        reads.append(sid)
        return original(home, root, sid, payload, count)

    monkeypatch.setattr(bg, "_log_tail", log_tail)
    with pytest.raises(KeyError) as rejected:
        bg.inspect_background_process(home, "bg-one", thread_id="thread-two")
    assert rejected.value.args == ("bg-one",) and reads == []
    with pytest.raises(KeyError) as rejected_global:
        bg.inspect_background_process(home, "bg-global", thread_id="thread-two")
    assert rejected_global.value.args == ("bg-global",) and reads == []
    assert bg.inspect_background_process(home, "bg-one", thread_id="thread-one")["session_id"] == "bg-one"
    assert bg.inspect_background_process(home, "bg-global")["session_id"] == "bg-global"
    assert bg.inspect_background_process(home, "bg-one")["session_id"] == "bg-one"
    assert _files(tmp_path) == before


# LLM: 覆盖原reader认可的每个线程字段位置，防止summary漏字段后将明确归属误当全局记录。
# 函数用途: 验证列表和详情共用相同的线程身份解释。
@pytest.mark.parametrize("scope", [None, "access_scope", "execution_scope", "completion_target", "metadata", "scope"])
@pytest.mark.parametrize("key", ["thread_id", "conversation_id"])
def test_thread_scope_covers_all_record_identity_locations(tmp_path, scope, key):
    import inspect

    assert "thread_id" in inspect.signature(bg.inspect_background_process).parameters
    home = tmp_path / "owner"
    root, _ = _session(home, thread_id="")
    path = root / "bg-example.json"
    payload = json.loads(path.read_text())
    if scope is None:
        payload[key] = "thread-other"
    else:
        payload.setdefault(scope, {})[key] = "thread-other"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert bg.list_background_processes(home, thread_id="thread-one")["items"] == []
    own = bg.list_background_processes(home, thread_id="thread-other")["items"]
    assert len(own) == 1 and own[0]["thread_id"] == "thread-other"
    with pytest.raises(KeyError):
        bg.inspect_background_process(home, "bg-example", thread_id="thread-one")


# LLM: 真实runtime解析执行DTO，真实通知消费先发布本轮线程；替身只模拟Gateway运输响应，不读网络。
# 函数用途: 验证首次轮询与打开详情都按本次会话过滤，而不是晚一轮才生效。
def test_gateway_first_poll_scopes_background_list_and_detail(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts.execution_target import GatewayExecutionTarget
    from agent_py_agent.cli.chat_parts.tui_threading import _consume_background_notices

    home = tmp_path / "owner"
    for sid, thread in [("bg-one", "thread-one"), ("bg-two", "thread-two"), ("bg-global", "")]:
        _session(home, session_id=sid, thread_id=thread)
    target = GatewayExecutionTarget("thread-one", "request", "turn", "claim").to_dict()
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=home), request_background_notices=lambda *args, **kwargs:
        {"ok": True, "notices": [], "cursor": 0, "transcript_events": [], "event_cursor": 0,
         "event_stream_id": "", "execution_target": target})
    runtime = TuiRuntime("thread-scope")
    nav = TuiAgentNavigationState(runtime)
    updates, details = [], []
    update, detail = nav.update_background_watch_rows, bg.inspect_background_process

    # LLM: 保留真实导航更新，并记录实际投影行集合而不只检查kwargs。
    # 函数用途: 捕捉首次轮询展示的任务集合。
    def updated(rows, **kwargs):
        updates.append({row["session_id"] for row in rows})
        return update(rows, **kwargs)

    # LLM: 详情仍走真实reader与日志入口；只额外记录透传的线程字段。
    # 函数用途: 核对打开观察页之后详情同样受当前线程约束。
    def inspected(*args, **kwargs):
        details.append(kwargs.get("thread_id"))
        return detail(*args, **kwargs)

    monkeypatch.setattr(nav, "update_background_watch_rows", updated)
    monkeypatch.setattr(bg, "inspect_background_process", inspected)
    before = _files(tmp_path)
    assert _consume_background_notices(agent, "display-session", runtime, [None], set(), [0], nav)
    assert updates[-1] == {"bg-one"}
    assert nav.snapshot().background_process_count == 1
    nav.move_selection(1)
    assert nav.enter_selected() and nav.snapshot().background_watch_active
    assert _consume_background_notices(agent, "display-session", runtime, [None], set(), [0], nav)
    assert details == ["thread-one"] and updates[-1] == {"bg-one"}
    frame = render_tui_snapshot(nav.active_runtime().store.snapshot(), TuiRenderContext(width=180))
    assert "本用户全部会话" not in "\n".join(fragments_text(line) for line in frame.transcript_lines)
    assert _files(tmp_path) == before


# LLM: 本地session显示名不等于canonical线程；真实线程存储只读解析，通知业务投影独立隔离。
# 函数用途: 验证本地首次刷新使用原解析器返回的线程身份。
def test_local_background_watch_resolves_canonical_thread(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.channels import LOCAL_AGENT_USER_ID, LOCAL_CHAT_CHANNEL
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.cli.chat_parts import tui_threading

    home = tmp_path / "owner"
    store = ConversationStore(home / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": LOCAL_AGENT_USER_ID,
        "channel": LOCAL_CHAT_CHANNEL, "channel_conversation_id": "display-session", "channel_user_id": LOCAL_AGENT_USER_ID})
    resolved, error = store.threads.resolve_report(channel=LOCAL_CHAT_CHANNEL,
        channel_conversation_id="display-session", channel_user_id=LOCAL_AGENT_USER_ID)
    assert resolved is not None and resolved.thread_id == thread.thread_id and not error
    _session(home, session_id="bg-one", thread_id=thread.thread_id)
    _session(home, session_id="bg-other", thread_id="display-session")
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=home), conversation_store=store)
    runtime = TuiRuntime("local-canonical")
    nav = TuiAgentNavigationState(runtime)
    monkeypatch.setattr(tui_threading, "_consume_local_background_snapshot", lambda *args: True)
    assert tui_threading._consume_background_notices(agent, "display-session", runtime, [None], set(), [0], nav)
    assert nav.snapshot().background_process_count == 1


# LLM: 同一导航上的线程变化先清旧帧；新投影报错时仍不能保留上一线程的日志或计数。
# 函数用途: 回归切线程遇读错的跨会话展示残留。
def test_background_thread_switch_clears_stale_logs_before_failed_projection(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts.execution_target import GatewayExecutionTarget
    from agent_py_agent.cli.chat_parts.tui_threading import _sync_background_watch

    home = tmp_path / "owner"
    _session(home, session_id="bg-one", thread_id="thread-one")
    runtime = TuiRuntime("switch")
    runtime.publish_execution_target(GatewayExecutionTarget("thread-one", "request", "turn", "claim").to_dict())
    nav = TuiAgentNavigationState(runtime)
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=home))
    assert _sync_background_watch(agent, nav)
    nav.move_selection(1)
    assert nav.enter_selected()
    assert _sync_background_watch(agent, nav)
    runtime.publish_execution_target(GatewayExecutionTarget("thread-two", "request", "turn-two", "claim-two").to_dict())

    # LLM: 只在换线程之后制造真实刷新入口异常，检查之前的展示状态是否已被清除。
    # 函数用途: 模拟新会话记录暂时不可读。
    def failed_projection(*args, **kwargs):
        raise OSError("projection unavailable")

    monkeypatch.setattr(bg, "list_background_processes", failed_projection)
    assert _sync_background_watch(agent, nav)
    assert nav.snapshot().background_process_count == 0
    frame = render_tui_snapshot(nav.active_runtime().store.snapshot(), TuiRenderContext(width=180))
    text = "\n".join(fragments_text(line) for line in frame.transcript_lines)
    assert "bg-one" not in text and "line-two" not in text


# LLM: execution_target只代表活动claim；空闲/失败的真实运输上下文不等于可跨线程观察的旧无上下文入口。
# 函数用途: 验证空闲首轮、活动转空闲和切会话首轮失败都暂停未知范围，不读取全owner记录。
@pytest.mark.parametrize("phase", ["idle_first", "idle_after_active", "failed_first", "failed_after_switch"])
def test_gateway_unknown_thread_scope_never_expands_to_owner_records(tmp_path, monkeypatch, phase):
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts.execution_target import GatewayExecutionTarget
    from agent_py_agent.cli.chat_parts.tui_threading import _consume_background_notices

    home = tmp_path / "owner"
    _session(home, session_id="bg-one", thread_id="thread-one")
    _session(home, session_id="bg-two", thread_id="thread-two")
    response = {"ok": True, "notices": [], "cursor": 0, "transcript_events": [], "event_cursor": 0,
                "event_stream_id": "", "execution_target": None}
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=home),
        request_background_notices=lambda *args, **kwargs: dict(response))
    runtime = TuiRuntime("unknown-thread")
    nav = TuiAgentNavigationState(runtime)
    if phase in {"idle_after_active", "failed_after_switch"}:
        response["execution_target"] = GatewayExecutionTarget("thread-one", "request", "turn", "claim").to_dict()
        assert _consume_background_notices(agent, "display-one", runtime, [None], set(), [0], nav)
        assert nav.snapshot().background_process_count == 1
        nav.move_selection(1)
        assert nav.enter_selected()
    response["execution_target"] = None
    response["ok"] = not phase.startswith("failed")
    reads = []
    original = bg.list_background_processes

    # LLM: 记录真实列表是否被触达，不用伪造空列表掩盖范围扩大。
    # 函数用途: 证明未知上下文不会读到任一线程记录。
    def listed(*args, **kwargs):
        reads.append(kwargs.get("thread_id"))
        return original(*args, **kwargs)

    monkeypatch.setattr(bg, "list_background_processes", listed)
    result = _consume_background_notices(agent, "display-two" if phase == "failed_after_switch" else "display-one",
        runtime, [None], set(), [0], nav)
    assert result is response["ok"]
    assert reads == []
    assert nav.snapshot().background_process_count == 0
    frame = render_tui_snapshot(nav.active_runtime().store.snapshot(), TuiRenderContext(width=180))
    text = "\n".join(fragments_text(line) for line in frame.transcript_lines)
    assert "bg-one" not in text and "bg-two" not in text


# LLM: 只读取真实导航当前帧，断言旧日志已消失不能以调用清空函数代替。
# 函数用途: 获取观察页实际渲染文字。
def _watch_frame_text(nav):
    frame = render_tui_snapshot(nav.active_runtime().store.snapshot(), TuiRenderContext(width=180))
    return "\n".join(fragments_text(line) for line in frame.transcript_lines)


# LLM: 通过真实观察同步和导航打开已绑定会话日志；不替换范围解析或渲染状态机。
# 函数用途: 准备可检测跨会话残留的已打开观察页。
def _open_local_watch(agent):
    from agent_py_agent.cli.chat_parts.tui_threading import _sync_background_watch

    runtime = TuiRuntime("local-scope-failure")
    nav = TuiAgentNavigationState(runtime)
    assert _sync_background_watch(agent, nav, session_id="display-one", runtime=runtime)
    assert nav.snapshot().background_process_count == 1
    nav.move_selection(1)
    assert nav.enter_selected()
    assert _sync_background_watch(agent, nav, session_id="display-one", runtime=runtime)
    assert "line-two" in _watch_frame_text(nav)
    return runtime, nav


# LLM: 使用真实Store的绑定索引和路径校验抛错，观察必须清旧帧且保留原通知异常给外层退避。
# 函数用途: 回归切到非法本地绑定时通知消费未触达观察同步的遗漏路径。
def test_local_invalid_store_binding_clears_watch_even_when_notice_consumption_raises(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.channels import LOCAL_AGENT_USER_ID, LOCAL_CHAT_CHANNEL
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.conversation.store_threads import _binding_key
    from agent_py_agent.cli.chat_parts.tui_threading import _consume_background_notices

    home = tmp_path / "owner"
    store = ConversationStore(home / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": LOCAL_AGENT_USER_ID,
        "channel": LOCAL_CHAT_CHANNEL, "channel_conversation_id": "display-one", "channel_user_id": LOCAL_AGENT_USER_ID})
    _session(home, session_id="bg-one", thread_id=thread.thread_id)
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=home), conversation_store=store)
    runtime, nav = _open_local_watch(agent)
    path = store.storage.bindings_path
    bindings = json.loads(path.read_text())
    bindings[_binding_key(LOCAL_CHAT_CHANNEL, "display-two", LOCAL_AGENT_USER_ID)] = "invalid/thread"
    path.write_text(json.dumps(bindings), encoding="utf-8")
    with pytest.raises(ValueError):
        store.threads.resolve_report(channel=LOCAL_CHAT_CHANNEL,
            channel_conversation_id="display-two", channel_user_id=LOCAL_AGENT_USER_ID)
    before = _files(tmp_path)
    invalidated = []

    # LLM: 未确认范围不得读取列表；显式失败可防止用全owner空结果掩盖漏接线。
    # 函数用途: 阻止错误路径读取任一线程的观察记录。
    def forbidden_list(*args, **kwargs):
        pytest.fail("未知会话不得读取进程列表")

    monkeypatch.setattr(bg, "list_background_processes", forbidden_list)
    with pytest.raises(ValueError):
        _consume_background_notices(agent, "display-two", runtime,
            [SimpleNamespace(invalidate=lambda: invalidated.append(True))], set(), [0], nav)
    assert nav.snapshot().background_process_count == 0
    assert "bg-one" not in _watch_frame_text(nav) and "line-two" not in _watch_frame_text(nav)
    assert invalidated and nav._background_watch_scope is None
    assert _files(tmp_path) == before


# LLM: 两个入口分别覆盖通知消费和同步自身的解析抛错，异常类型不应决定是否错误保留旧范围。
# 函数用途: 验证OSError与ValueError解析失败均清空旧行和旧日志。
@pytest.mark.parametrize("entry", ["consume", "sync"])
@pytest.mark.parametrize("error_type", [OSError, ValueError])
def test_local_scope_resolution_exception_clears_previous_watch(tmp_path, monkeypatch, entry, error_type):
    from types import SimpleNamespace

    from agent_py_agent.cli.chat_parts import tui_threading

    home = tmp_path / "owner"
    _session(home, session_id="bg-one", thread_id="thread-one")
    broken = [False]

    # LLM: 初始返回明确会话，随后仅在范围解析接缝抛异常，不改进程reader或导航。
    # 函数用途: 构造一次可信展示后无法确认新范围的确定反例。
    def resolve(**kwargs):
        if broken[0]:
            raise error_type("conversation binding unavailable")
        return SimpleNamespace(thread_id="thread-one"), None

    store = SimpleNamespace(storage=SimpleNamespace(root=home / "conversations"), threads=SimpleNamespace(resolve_report=resolve))
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=home), conversation_store=store)
    runtime, nav = _open_local_watch(agent)
    broken[0] = True
    before = _files(tmp_path)
    if entry == "consume":
        with pytest.raises(error_type):
            tui_threading._consume_background_notices(agent, "display-two", runtime, [None], set(), [0], nav)
    else:
        assert tui_threading._sync_background_watch(agent, nav, session_id="display-two", runtime=runtime)
    assert nav.snapshot().background_process_count == 0
    assert "bg-one" not in _watch_frame_text(nav) and "line-two" not in _watch_frame_text(nav)
    assert nav._background_watch_scope is None
    assert "会话范围未确认" in nav.active_runtime().notice()
    assert _files(tmp_path) == before


# LLM: 范围确认后的普通投影错误不同于解析失败，保留同范围旧帧且只提示，不改变任务或文件。
# 函数用途: 防止修复范围失信时误删同会话有效展示的兼容回归。
@pytest.mark.parametrize("error_type", [OSError, ValueError])
def test_confirmed_local_scope_projection_failure_keeps_same_thread_frame(tmp_path, monkeypatch, error_type):
    from types import SimpleNamespace

    from agent_py_agent.cli.chat_parts.tui_threading import _sync_background_watch

    home = tmp_path / "owner"
    _session(home, session_id="bg-one", thread_id="thread-one")
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=home), thread_id="thread-one")
    runtime, nav = _open_local_watch(agent)
    before = _files(tmp_path)

    # LLM: 明确会话解析保持成功，仅在随后列表读取制造异常。
    # 函数用途: 隔离已确认同范围的普通读取失败。
    def failed_projection(*args, **kwargs):
        raise error_type("process projection unavailable")

    monkeypatch.setattr(bg, "list_background_processes", failed_projection)
    assert _sync_background_watch(agent, nav, session_id="display-one", runtime=runtime)
    assert nav.snapshot().background_process_count == 1 and "line-two" in _watch_frame_text(nav)
    assert "仅保留同会话上次结果" in nav.active_runtime().notice()
    assert nav._background_watch_scope == (str(home), "thread-one")
    assert _files(tmp_path) == before


# LLM: 空会话测试不允许触达全owner reader；真实线程存储与导航负责空态和旧提示清理。
# 函数用途: 锁定刚开窗口和/new后的静默空会话边界。
@pytest.mark.parametrize("session_id", ["new-window", "after-new"])
def test_c4_local_empty_session_is_quiet_and_never_reads_owner_records(tmp_path, monkeypatch, session_id):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.cli.chat_parts.tui_threading import _consume_background_notices

    home = tmp_path / "owner"
    _session(home, thread_id="foreign-thread")
    store = ConversationStore(home / "conversations")
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=home), conversation_store=store)
    runtime = TuiRuntime(session_id)
    nav = TuiAgentNavigationState(runtime)
    nav.update_background_watch_rows([{"session_id": "bg-stale", "alive": True}])
    nav._background_watch_scope = (str(home), "old-thread")
    runtime.set_notice("旧范围错误", notice_kind="background_watch_error")
    monkeypatch.setattr(bg, "list_background_processes", lambda *a, **kw: pytest.fail("空会话不得读owner记录"))
    before = _files(tmp_path)
    assert _consume_background_notices(agent, session_id, runtime, [None], set(), [0], nav)
    assert nav.snapshot().background_process_count == 0
    assert nav._background_watch_scope is None and runtime.notice() == ""
    assert _files(tmp_path) == before


# LLM: load_error以非None为准，不能因thread为空或错误字符串为空而吞掉损坏索引。
# 函数用途: 验证正常空态修复不会隐藏真正的解析失败。
@pytest.mark.parametrize("load_error", ["corrupt", ""])
@pytest.mark.parametrize("thread_id", [None, "thread-one"])
def test_c4_local_load_error_still_clears_and_warns(tmp_path, monkeypatch, load_error, thread_id):
    from types import SimpleNamespace

    from agent_py_agent.cli.chat_parts.tui_threading import _sync_background_watch

    thread = SimpleNamespace(thread_id=thread_id) if thread_id else None
    store = SimpleNamespace(threads=SimpleNamespace(resolve_report=lambda **kw: (thread, load_error)))
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=tmp_path), conversation_store=store)
    runtime = TuiRuntime("corrupt-scope")
    nav = TuiAgentNavigationState(runtime)
    nav.update_background_watch_rows([{"session_id": "bg-stale", "alive": True}])
    monkeypatch.setattr(bg, "list_background_processes", lambda *a, **kw: pytest.fail("损坏范围不得读进程"))
    assert _sync_background_watch(agent, nav, session_id="display", runtime=runtime)
    assert nav.snapshot().background_process_count == 0 and nav._background_watch_scope is None
    assert "会话范围未确认" in runtime.notice()


# LLM: Gateway旧响应、显式无线程和初次运输失败均不能在未打开观察的主页面刷假告警。
# 函数用途: 验证主页面空态静默，并且不会借空thread_id扩大到全owner。
@pytest.mark.parametrize("extra", [{}, {"thread_id": ""}, {"ok": False}])
def test_c4_gateway_empty_main_poll_is_quiet(tmp_path, monkeypatch, extra):
    from types import SimpleNamespace

    from agent_py_agent.cli.chat_parts.tui_threading import _consume_background_notices

    response = {"ok": True, "notices": [], "cursor": 0, "transcript_events": [],
                "event_cursor": 0, "event_stream_id": "", "execution_target": None, **extra}
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=tmp_path),
                            request_background_notices=lambda *a, **kw: response)
    runtime = TuiRuntime("gateway-empty")
    nav = TuiAgentNavigationState(runtime)
    runtime.set_notice("旧范围错误", notice_kind="background_watch_error")
    monkeypatch.setattr(bg, "list_background_processes", lambda *a, **kw: pytest.fail("无线程不得读owner记录"))
    assert _consume_background_notices(agent, "display", runtime, [None], set(), [0], nav) is response["ok"]
    assert nav.snapshot().background_process_count == 0
    assert nav._background_watch_scope is None and runtime.notice() == ""


# LLM: 无活动claim的合法thread投影仍必须精确过滤；切会话后旧绑定不得重新生效。
# 函数用途: 验证Gateway空闲线程保持可观察，而新会话清除前会话缓存与提示。
def test_c4_gateway_idle_thread_projection_and_session_reset(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.cli.chat_parts.tui_threading import _consume_background_notices

    home = tmp_path / "owner"
    _session(home, session_id="bg-one", thread_id="thread-one")
    _session(home, session_id="bg-two", thread_id="thread-two")
    response = {"ok": True, "notices": [], "cursor": 0, "transcript_events": [],
                "event_cursor": 0, "event_stream_id": "", "execution_target": None, "thread_id": "thread-one"}
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=home),
                            request_background_notices=lambda *a, **kw: dict(response))
    runtime = TuiRuntime("gateway-idle")
    nav = TuiAgentNavigationState(runtime)
    before = _files(tmp_path)
    for _ in range(2):
        assert _consume_background_notices(agent, "display-one", runtime, [None], set(), [0], nav)
        assert nav.snapshot().background_process_count == 1
        assert nav._background_watch_scope == (str(home), "thread-one")
        assert runtime.notice() == ""
    response.pop("thread_id")
    monkeypatch.setattr(bg, "list_background_processes", lambda *a, **kw: pytest.fail("切会话不得复用旧thread"))
    assert _consume_background_notices(agent, "display-two", runtime, [None], set(), [0], nav)
    assert nav.snapshot().background_process_count == 0
    assert nav._background_watch_scope is None and runtime.notice() == ""
    assert _files(tmp_path) == before


# LLM: 线程字段损坏或与活动claim冲突属于解析失败，不能以正常空态静默吞掉。
# 函数用途: 验证Gateway范围冲突保持失败关闭且提示用户。
@pytest.mark.parametrize("thread_id", [None, 42, "other-thread", ""])
def test_c4_gateway_invalid_thread_projection_warns_without_read(tmp_path, monkeypatch, thread_id):
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts.execution_target import GatewayExecutionTarget
    from agent_py_agent.cli.chat_parts.tui_threading import _consume_background_notices

    response = {"ok": True, "notices": [], "cursor": 0, "transcript_events": [],
                "event_cursor": 0, "event_stream_id": "", "thread_id": thread_id,
                "execution_target": GatewayExecutionTarget("thread-one", "request", "turn", "claim").to_dict()}
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=tmp_path),
                            request_background_notices=lambda *a, **kw: response)
    runtime = TuiRuntime("gateway-conflict")
    nav = TuiAgentNavigationState(runtime)
    monkeypatch.setattr(bg, "list_background_processes", lambda *a, **kw: pytest.fail("冲突范围不得读取进程"))
    assert not _consume_background_notices(agent, "display", runtime, [None], set(), [0], nav)
    assert nav.snapshot().background_process_count == 0 and nav._background_watch_scope is None
    assert "会话范围未确认" in runtime.notice()


# LLM: 已加载与冷owner使用同一真实存储和精确绑定，不启动HTTP、模型或写回恢复状态。
# 函数用途: 验证/notices三类响应都显式携带线程身份，包括无活动任务的空闲线程。
@pytest.mark.parametrize("cold", [False, True])
@pytest.mark.parametrize("created", [False, True])
def test_c4_gateway_notice_response_has_explicit_thread_id(tmp_path, monkeypatch, cold, created):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.gateway_parts import http_handlers
    from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home

    owner = OwnerIdentity.provider_user("tui-test", "c4-user")
    home = resolve_owner_home(tmp_path, owner).home_dir
    store = ConversationStore(home / "workspace/runtime/workspaces/workspace-c4/conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "c4-user", "channel": "tui-test",
        "channel_conversation_id": "display", "channel_user_id": "c4-user"}) if created else None
    agent = SimpleNamespace(home_paths=SimpleNamespace(root=tmp_path, owner_home_dir=home), conversation_store=store)
    monkeypatch.setattr(http_handlers, "resolve_loaded_gateway_scope_agent", lambda *a: None if cold else agent)
    before = _files(tmp_path)
    payload = http_handlers.read_gateway_client_notices(agent,
        scope=GatewayControlScope("c4-user", "tui-test", "display", resolved_owner=owner), after=0)
    assert payload["ok"] is True and payload["execution_target"] is None
    assert "thread_id" in payload
    assert payload["thread_id"] == (thread.thread_id if thread else "")
    assert _files(tmp_path) == before


# LLM: 损坏绑定不能由Gateway标成ok=True空快照，否则客户端无法区分正常无线程与解析失败。
# 函数用途: 验证热/冷线程索引损坏仍通过传输状态显式报告失败。
@pytest.mark.parametrize("cold", [False, True])
def test_c4_gateway_corrupt_binding_is_not_a_successful_empty_snapshot(tmp_path, monkeypatch, cold):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.gateway_parts import http_handlers
    from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home

    owner = OwnerIdentity.provider_user("tui-test", "c4-user")
    home = resolve_owner_home(tmp_path, owner).home_dir
    store = ConversationStore(home / "workspace/runtime/workspaces/workspace-c4/conversations")
    store.storage.bindings_path.write_text("corrupt-index", encoding="utf-8")
    agent = SimpleNamespace(home_paths=SimpleNamespace(root=tmp_path, owner_home_dir=home), conversation_store=store)
    monkeypatch.setattr(http_handlers, "resolve_loaded_gateway_scope_agent", lambda *a: None if cold else agent)
    before = _files(tmp_path)
    payload = http_handlers.read_gateway_client_notices(agent,
        scope=GatewayControlScope("c4-user", "tui-test", "display", resolved_owner=owner), after=0)
    assert payload["ok"] is False and payload["thread_id"] == ""
    assert payload.get("error") and _files(tmp_path) == before


# LLM: 首次失败会清掉导航范围，但最近已确认的Gateway线程仍是本会话事实，不能下一轮静默吞错。
# 函数用途: 验证已绑定主页面连续运输失败仍保持范围失信提示，而不读取任何进程。
def test_c4_gateway_bound_scope_failures_keep_warning(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.cli.chat_parts.tui_threading import _consume_background_notices

    _session(tmp_path, thread_id="thread-one")
    payload = {"ok": True, "thread_id": "thread-one", "notices": [], "cursor": 0,
               "transcript_events": [], "event_cursor": 0, "event_stream_id": "", "execution_target": None}
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=tmp_path),
                            request_background_notices=lambda *a, **kw: dict(payload))
    runtime = TuiRuntime("bound-failure")
    nav = TuiAgentNavigationState(runtime)
    assert _consume_background_notices(agent, "display", runtime, [None], set(), [0], nav)
    payload["ok"] = False
    monkeypatch.setattr(bg, "list_background_processes", lambda *a, **kw: pytest.fail("失败快照不得读取进程"))
    for _ in range(2):
        assert not _consume_background_notices(agent, "display", runtime, [None], set(), [0], nav)
        assert nav.snapshot().background_process_count == 0
        assert "会话范围未确认" in runtime.notice()


# LLM: 通知阶段的一次性解析异常不能被finally第二次成功解析成正常空会话；保留原异常与明确提示。
# 函数用途: 验证新会话的瞬时OSError/ValueError仍被归为真实失败。
@pytest.mark.parametrize("error_type", [OSError, ValueError])
def test_c4_transient_local_resolution_error_still_warns(tmp_path, error_type):
    from types import SimpleNamespace

    from agent_py_agent.cli.chat_parts.tui_threading import _consume_background_notices

    calls = []

    def resolve(**kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise error_type("transient index error")
        return None, None

    store = SimpleNamespace(storage=SimpleNamespace(root=tmp_path), threads=SimpleNamespace(resolve_report=resolve))
    agent = SimpleNamespace(conversation_store=store)
    runtime = TuiRuntime("transient-local")
    nav = TuiAgentNavigationState(runtime)
    with pytest.raises(error_type):
        _consume_background_notices(agent, "display", runtime, [None], set(), [0], nav)
    assert nav.snapshot().background_process_count == 0
    assert "会话范围未确认" in runtime.notice()


# LLM: 当前可解析到线程不代表曾建立观察范围；普通通知快照首轮失败仍交由原通知去抖处理。
# 函数用途: 验证未观察、未绑定时不会因本地存在历史线程就刷范围黄字。
def test_c4_first_local_snapshot_failure_with_thread_is_quiet(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.cli.chat_parts.tui_threading import _sync_background_watch

    store = SimpleNamespace(threads=SimpleNamespace(resolve_report=lambda **kw: (SimpleNamespace(thread_id="thread-one"), None)))
    agent = SimpleNamespace(conversation_store=store)
    runtime = TuiRuntime("first-local-failure")
    nav = TuiAgentNavigationState(runtime)
    monkeypatch.setattr(bg, "list_background_processes", lambda *a, **kw: pytest.fail("失败快照不得读进程"))
    _sync_background_watch(agent, nav, session_id="display", runtime=runtime, snapshot_ok=False)
    assert nav.snapshot().background_process_count == 0 and runtime.notice() == ""


# LLM: 切session后第一次fetch失败时，旧控制DTO不能成为新session的观察身份；本测试不改变控制目标。
# 函数用途: 验证空绑定不借用旧execution_target，即便后续同步已阻止跨会话列表读取。
def test_c4_gateway_failed_session_switch_does_not_resolve_old_target(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts.execution_target import GatewayExecutionTarget
    from agent_py_agent.cli.chat_parts.tui_threading import (
        _background_watch_thread_id,
        _consume_background_notices,
    )

    payload = {"ok": True, "notices": [], "cursor": 0, "transcript_events": [], "event_cursor": 0,
               "event_stream_id": "", "execution_target": GatewayExecutionTarget("thread-one", "request", "turn", "claim").to_dict()}
    agent = SimpleNamespace(request_background_notices=lambda *a, **kw: dict(payload))
    runtime = TuiRuntime("session-switch-failure")
    nav = TuiAgentNavigationState(runtime)
    assert _consume_background_notices(agent, "display-one", runtime, [None], set(), [0], nav)
    payload["ok"] = False
    assert not _consume_background_notices(agent, "display-two", runtime, [None], set(), [0], nav)
    assert _background_watch_thread_id(agent, nav, session_id="display-two", runtime=runtime) is None


# LLM: 缓存是进程内优化；每例清空两级缓存，旧实现无缓存时仍能先运行行为反例而非导入报错。
# 函数用途: 隔离后台观察缓存，避免测试顺序或时钟替身影响其它用例。
@pytest.fixture(autouse=True)
def _b1_reset_view_cache():
    clear = getattr(bg, "_clear_background_process_view_cache", lambda: None)
    clear()
    yield
    clear()


# LLM: 只在临时目录构造轻量记录；文件与目录回拨十秒以真实通过共用粗粒度时间窗。
# 函数用途: 准备可确定命中缓存的多个后台记录，不启动进程或写真实日志。
def _b1_records(home, count=3):
    root = home / "workspace/runtime/processes/sessions"
    root.mkdir(parents=True)
    old = time.time() - 10
    for index in range(count):
        path = root / f"bg-cache-{index}.json"
        payload = {"session_id": path.stem, "status": "exited", "thread_id": "thread-one",
                   "metadata": {"marker": "original"}, "started_at": time.time() - 100, "finished_at": time.time() - 1}
        path.write_text(json.dumps(payload))
        os.utime(path, (old, old))
    os.utime(root, (old, old))
    return root


# LLM: 仍走原nofollow读取；仅计数真实打开解析入口，不能用假payload制造缓存收益。
# 函数用途: 捕获后台元数据的实际读取次数。
def _b1_count_reads(monkeypatch):
    original, reads = bg.read_bytes_beneath, []

    # LLM: 参数和异常原样透传，不降低文件类型、身份或字节上限检查。
    # 函数用途: 记录一次真实后台元数据读取。
    def read(root, parts, **kwargs):
        reads.append((str(root), parts))
        return original(root, parts, **kwargs)

    monkeypatch.setattr(bg, "read_bytes_beneath", read)
    return reads


# LLM: 不以耗时阈值验优化；同代可信目录的第二次观察必须零次重复文件读取。
# 函数用途: 验证目录缓存命中且调用者不能污染缓存快照。
def test_b1_trusted_directory_reuses_readonly_snapshots(tmp_path, monkeypatch):
    home = tmp_path / "owner"
    _b1_records(home)
    before = _files(home)
    reads = _b1_count_reads(monkeypatch)
    first = bg.list_background_processes(home, thread_id="thread-one")
    assert first["total"] == 3 and len(reads) == 3
    first["items"][0]["status"] = "caller-mutated"
    second = bg.list_background_processes(home, thread_id="thread-one")
    assert len(reads) == 3
    assert {item["status"] for item in second["items"]} == {"exited"}
    assert _files(home) == before


# LLM: 原子replace改变目录代次；未变文件必须命中自己的安全指纹缓存。
# 函数用途: 验证目录一项更新只重读这一项。
def test_b1_atomic_replacement_only_reparses_changed_file(tmp_path, monkeypatch):
    home = tmp_path / "owner"
    root = _b1_records(home)
    reads = _b1_count_reads(monkeypatch)
    bg.list_background_processes(home)
    path = root / "bg-cache-1.json"
    payload = json.loads(path.read_text())
    payload["command"] = "updated"
    replacement = root / "replacement.tmp"
    replacement.write_text(json.dumps(payload))
    replacement.replace(path)
    result = bg.list_background_processes(home)
    assert len(reads) == 4
    assert next(item for item in result["items"] if item["session_id"] == path.stem)["command"] == "updated"


# LLM: TTL按最初复核时刻而非命中时刻计算；60秒兜底必须穿透单文件缓存。
# 函数用途: 验证目录期限强制完整重读并发现不改目录mtime的原位写。
def test_b1_directory_ttl_forces_full_file_revalidation(tmp_path, monkeypatch):
    home = tmp_path / "owner"
    root = _b1_records(home)
    clock = [10.0]
    monkeypatch.setattr(bg.time, "monotonic", lambda: clock[0])
    reads = _b1_count_reads(monkeypatch)
    bg.list_background_processes(home)
    clock[0] = 69.9
    bg.list_background_processes(home)
    assert len(reads) == 3
    path = root / "bg-cache-0.json"
    payload = json.loads(path.read_text())
    payload["command"] = "in-place-update"
    path.write_text(json.dumps(payload))
    clock[0] = 70.0
    result = bg.list_background_processes(home)
    assert len(reads) == 6
    assert result["items"][0]["command"] == "in-place-update"


# LLM: 读取前的不可信判断不能由读取完成后跨过时间窗洗白；目录和文件两层均须守门。
# 函数用途: 验证粗粒度时间窗内不复用或发表缓存。
@pytest.mark.parametrize("becomes_trusted", [False, True])
def test_b1_recent_stat_cannot_seed_or_hit_cache(tmp_path, monkeypatch, becomes_trusted):
    home = tmp_path / "owner"
    _b1_records(home, count=1)
    trusted = [False]
    original, reads = bg.read_bytes_beneath, []
    monkeypatch.setattr(bg, "cache_entry_trustworthy", lambda mtime: trusted[0], raising=False)

    # LLM: 真实读取完成后才改变时间窗判定，专门构造延迟发布反例。
    # 函数用途: 捕获时间窗变化前后的真实文件读取。
    def read(root, parts, **kwargs):
        reads.append(parts)
        value = original(root, parts, **kwargs)
        trusted[0] = becomes_trusted
        return value

    monkeypatch.setattr(bg, "read_bytes_beneath", read)
    bg.list_background_processes(home)
    bg.list_background_processes(home)
    assert len(reads) == 2


# LLM: 匹配会话的非终态探活和耗时每次现算，终态及其它会话不探活；缓存只存静态payload。
# 函数用途: 验证热缓存不会冻结动态状态，也不会扩大探活范围。
@pytest.mark.parametrize("status", ["running", "unknown"])
def test_b1_cache_keeps_liveness_and_elapsed_dynamic_after_thread_filter(tmp_path, monkeypatch, status):
    home = tmp_path / "owner"
    root = _b1_records(home)
    for index, thread in [(0, "thread-one"), (1, "other-thread")]:
        path = root / f"bg-cache-{index}.json"
        payload = json.loads(path.read_text())
        payload.update(status=status, thread_id=thread, started_at=100.0, finished_at=0.0)
        path.write_text(json.dumps(payload))
    for path in root.glob("bg-*.json"):
        os.utime(path, (100, 100))
    os.utime(root, (100, 100))
    clock, live, probes = [200.0], [True], []
    monkeypatch.setattr(bg.time, "time", lambda: clock[0])
    monkeypatch.setattr(bg, "_managed_instance_still_live", lambda sid, *args: probes.append(sid) or live[0])
    reads = _b1_count_reads(monkeypatch)
    first = bg.list_background_processes(home, thread_id="thread-one")
    assert first["items"][0]["elapsed_seconds"] == 100.0
    clock[0], live[0] = 205.0, False
    second = bg.list_background_processes(home, thread_id="thread-one")
    assert second["items"][0]["elapsed_seconds"] == 105.0 and not second["items"][0]["alive"]
    assert probes == ["bg-cache-0", "bg-cache-0"]
    assert len(reads) == 3


# LLM: 原日志reader仍每次读取文件尾部，append不会更新目录mtime也不能冻结所选日志。
# 函数用途: 验证热缓存旁的详情日志实时且保持64KiB上限。
def test_b1_cached_directory_does_not_freeze_selected_log_tail(tmp_path):
    home = tmp_path / "owner"
    root, output = _session(home, status="exited")
    old = time.time() - 10
    os.utime(root / "bg-example.json", (old, old))
    os.utime(root, (old, old))
    bg.list_background_processes(home)
    before = root.stat().st_mtime_ns
    assert bg.inspect_background_process(home, "bg-example", tail_lines=1)["log_tail"] == ["line-two"]
    with output.open("a") as stream:
        stream.write("new-tail\n")
    assert root.stat().st_mtime_ns == before
    detail = bg.inspect_background_process(home, "bg-example", tail_lines=1)
    assert detail["log_tail"] == ["new-tail"]


# LLM: 坏schema仍是合法JSON；Wave9空线程也隔离严格确认的其他线程，原六类位置与无关诊断不能丢失。
# 函数用途: 验证明确其它线程的坏记录在指定及空线程观察中均保留独立诊断。
@pytest.mark.parametrize("location", [None, "access_scope", "execution_scope", "completion_target", "metadata", "scope"])
@pytest.mark.parametrize("key", ["thread_id", "conversation_id"])
def test_b1_foreign_corrupt_record_is_isolated_at_every_scope_location(tmp_path, location, key):
    home = tmp_path / "owner"
    root = _b1_records(home, count=1)
    payload = {"session_id": "bg-corrupt", "schema": "invalid-schema"}
    scope = payload if location is None else payload.setdefault(location, {})
    scope[key] = "other-thread"
    (root / "bg-corrupt.json").write_text(json.dumps(payload))
    scoped = bg.list_background_processes(home, thread_id="thread-one")
    assert scoped["projection_ok"] is True and scoped["errors"] == []
    assert scoped.get("unrelated_errors") == [{"session_id": "bg-corrupt", "error": "ValueError"}]
    assert scoped.get("unscoped_error_count") == 1
    empty_thread = bg.list_background_processes(home)
    assert empty_thread["projection_ok"] is True and empty_thread["errors"] == []
    assert empty_thread["unrelated_errors"] == scoped["unrelated_errors"]
    assert empty_thread["unscoped_error_count"] == 1


# LLM: 只有明确其它owner才隔离；当前线程或线程声明相互冲突的坏记录不得被历史年龄洗白。
# 函数用途: 验证归属冲突失败关闭及其它owner坏文件的隔离。
@pytest.mark.parametrize("kind", ["current", "conflicting", "foreign_owner", "foreign_nested_owner"])
def test_b1_corrupt_scope_classification_preserves_current_thread_errors(tmp_path, kind):
    home = tmp_path / "owner"
    root = _b1_records(home, count=1)
    path = root / "bg-corrupt.json"
    payload = {"session_id": path.stem, "schema": "invalid-schema", "thread_id": "thread-one"}
    if kind == "conflicting":
        payload["scope"] = {"thread_id": "other-thread"}
    if kind.startswith("foreign"):
        target = payload if kind == "foreign_owner" else payload.setdefault("metadata", {})
        target["owner_home"] = str(tmp_path / "other-owner")
    path.write_text(json.dumps(payload))
    old = time.time() - 120
    os.utime(path, (old, old))
    result = bg.list_background_processes(home, thread_id="thread-one")
    assert result["projection_ok"] is False
    assert result["errors"] == [{"session_id": "bg-corrupt", "error": "ValueError"}]
    assert result["unrelated_errors"] == []


# LLM: 无法确认归属的坏文件不能凭年龄隔离，非字典JSON同样保守。
# 函数用途: 验证跨越60秒与长期陈旧后，无归属错误仍令观察失败。
@pytest.mark.parametrize("age", [0.0, 59.9, 60.0, 60.1, 120.0])
@pytest.mark.parametrize("raw", ["broken-json", "[]", '{"session_id":"bg-corrupt","schema":"invalid"}'])
def test_b1_unscoped_corruption_never_isolated_by_age(tmp_path, monkeypatch, age, raw):
    home = tmp_path / "owner"
    root = _b1_records(home, count=1)
    clock = float(int(time.time()))
    monkeypatch.setattr(bg.time, "time", lambda: clock)
    path = root / "bg-corrupt.json"
    path.write_text(raw)
    os.utime(path, (clock - age, clock - age))
    result = bg.list_background_processes(home, thread_id="thread-one")
    assert result["projection_ok"] is False
    assert result["errors"] and result["unrelated_errors"] == []
    assert bg.list_background_processes(home)["projection_ok"] is False


# LLM: 热缓存中的坏文件变老不产生新的归属证据，始终按未知归属错误报告。
# 函数用途: 验证缓存与时间推进不能把无归属错误洗白。
def test_b1_cached_unscoped_error_stays_current_after_aging(tmp_path, monkeypatch):
    home = tmp_path / "owner"
    root = _b1_records(home, count=1)
    path = root / "bg-corrupt.json"
    path.write_text("broken-json")
    clock = [time.time()]
    os.utime(path, (clock[0] - 59, clock[0] - 59))
    os.utime(root, (clock[0] - 10, clock[0] - 10))
    monkeypatch.setattr(bg.time, "time", lambda: clock[0])
    reads = _b1_count_reads(monkeypatch)
    assert not bg.list_background_processes(home, thread_id="thread-one")["projection_ok"]
    clock[0] += 2
    assert not bg.list_background_processes(home, thread_id="thread-one")["projection_ok"]
    assert len(reads) == 2


# LLM: 真实TUI先报告未知归属坏文件，只有出现明确其它线程证据才恢复；不得清除其它kind的用户提示。
# 函数用途: 验证观察恢复会主动清掉专用告警，而非等提示自然超时。
def test_b1_tui_notice_recovers_after_unrelated_orphan_isolated(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.cli.chat_parts.tui_threading import _sync_background_watch

    home = tmp_path / "owner"
    root = _b1_records(home, count=1)
    broken = root / "bg-orphan.json"
    broken.write_text("broken-json")
    runtime = TuiRuntime("b1-notice-recovery")
    navigation = TuiAgentNavigationState(runtime)
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=home), thread_id="thread-one")
    assert _sync_background_watch(agent, navigation, runtime=runtime)
    assert "观察刷新失败" in runtime.notice()
    old = time.time() - 120
    os.utime(broken, (old, old))
    assert _sync_background_watch(agent, navigation, runtime=runtime)
    assert "观察刷新失败" in runtime.notice()
    broken.write_text(json.dumps({"session_id": broken.stem, "schema": "invalid", "thread_id": "other-thread"}))
    assert _sync_background_watch(agent, navigation, runtime=runtime)
    assert runtime.notice() == "" and navigation.snapshot().background_process_count == 0
    runtime.set_notice("其它功能的提示", notice_kind="other_feature")
    _sync_background_watch(agent, navigation, runtime=runtime)
    assert runtime.notice() == "其它功能的提示"


# LLM: 根目录读取失败不可当成空目录或无关历史错误，缓存也不得创建缺失目录。
# 函数用途: 验证不可读根目录保持真实失败诊断。
def test_b1_unreadable_directory_is_current_projection_error(tmp_path, monkeypatch):
    home = tmp_path / "owner"
    root = _b1_records(home, count=1)
    original = os.scandir

    # LLM: 只在本测试目标目录拒绝列名，不伪造权限结论或影响其它临时目录。
    # 函数用途: 注入确定的目录读取失败。
    def scandir(path):
        if str(path) == str(root):
            raise PermissionError("fixture directory denied")
        return original(path)

    monkeypatch.setattr(os, "scandir", scandir)
    result = bg.list_background_processes(home, thread_id="thread-one")
    assert result["projection_ok"] is False
    assert any(item["error"] == "PermissionError" for item in result["errors"])


# LLM: 小容量仅缩短驱逐测试，不更改生产默认值；两级条目按LRU有界，清空入口必须同时失效。
# 函数用途: 验证缓存容量、驱逐与显式清空。
def test_b1_cache_limits_and_clear_are_effective(tmp_path, monkeypatch):
    assert getattr(bg, "_PROCESS_VIEW_DIR_CACHE_MAX_ENTRIES", None) == 64
    assert getattr(bg, "_FILE_CACHE_MAX_ENTRIES", None) == 2048
    monkeypatch.setattr(bg, "_PROCESS_VIEW_DIR_CACHE_MAX_ENTRIES", 2)
    monkeypatch.setattr(bg, "_FILE_CACHE_MAX_ENTRIES", 2)
    homes = [tmp_path / f"owner-{index}" for index in range(3)]
    for home in homes:
        _b1_records(home, count=1)
    reads = _b1_count_reads(monkeypatch)
    for home in homes:
        bg.list_background_processes(home)
    bg.list_background_processes(homes[-1])
    assert len(reads) == 3
    assert len(bg._DIRECTORY_CACHE) == len(bg._FILE_CACHE) == 2
    bg.list_background_processes(homes[0])
    assert len(reads) == 4
    bg._clear_background_process_view_cache()
    assert not bg._DIRECTORY_CACHE and not bg._FILE_CACHE
    bg.list_background_processes(homes[0])
    assert len(reads) == 5


# LLM: 首次缺目录是正常空态；读取开始后目录消失是本轮观察失败，不能退回“目录本来不存在”。
# 函数用途: 验证读取期间根目录消失不会被吞成成功空列表。
def test_b1_disappearing_directory_after_read_is_not_successful_empty(tmp_path, monkeypatch):
    home = tmp_path / "owner"
    root = _b1_records(home, count=1)
    original = getattr(bg, "_directory_stat_key", None)
    assert callable(original)
    visits = []

    # LLM: 只在已见存在的同一目录第二次核验时注入真实类型的缺失错误。
    # 函数用途: 重现初次stat成功、读后stat失败的确定交错。
    def changed(path):
        if path == root:
            visits.append(path)
            if len(visits) > 1:
                raise FileNotFoundError("fixture directory vanished")
        return original(path)

    monkeypatch.setattr(bg, "_directory_stat_key", changed)
    result = bg.list_background_processes(home, thread_id="thread-one")
    assert result["projection_ok"] is False
    assert result["errors"] and result["errors"][0]["error"] == "FileNotFoundError"


# LLM: 原始v4文件沿真实schema校验且禁止可写Store入口；第二次稳定观察不能重复解析或验证。
# 函数用途: 验证原生记录的缓存收益与严格只读边界。
def test_b1_native_record_reuses_schema_validation_without_store_load(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling.process_session_store import ProcessSessionStore
    from agent_py_agent.tests import test_process_completion_events as fixtures

    monkeypatch.setattr(fixtures, "capture_process_birth_token", lambda pid: "fixture-launcher")
    agent, _, authority = fixtures._fixture(tmp_path, managed=True)
    home, old = agent.home_paths.owner_home_dir, time.time() - 10
    for path in authority.root.glob("bg-*.json"):
        os.utime(path, (old, old))
    os.utime(authority.root, (old, old))
    original, validations = bg.validate_process_record, []

    # LLM: 验证器返回值与异常均走原函数，不以伪schema通过替代真实校验。
    # 函数用途: 计数实际v4记录校验次数。
    def validated(payload):
        validations.append(payload["session_id"])
        return original(payload)

    # LLM: 观察层误入恢复写入口时必须令测试失败。
    # 函数用途: 拦截Store加载或事务副作用。
    def forbidden(*args, **kwargs):
        pytest.fail("background view must not load or transact writable Store")

    monkeypatch.setattr(bg, "validate_process_record", validated)
    monkeypatch.setattr(ProcessSessionStore, "load", forbidden)
    monkeypatch.setattr(ProcessSessionStore, "transaction", forbidden)
    before = _files(tmp_path), _files(authority.root)
    assert bg.list_background_processes(home)["total"] == 1
    assert bg.list_background_processes(home)["total"] == 1
    assert validations == ["bg-test-notice"]
    assert (_files(tmp_path), _files(authority.root)) == before


# LLM: 概要消费者可能改动传入字典；深拷贝必须同时隔离文件缓存和目录缓存内部嵌套对象。
# 函数用途: 验证动态投影修改不能污染后续缓存命中。
def test_b1_summary_cannot_mutate_cached_nested_payload(tmp_path, monkeypatch):
    home = tmp_path / "owner"
    _b1_records(home, count=1)
    original = bg._summary

    # LLM: 原概要正常生成后才修改本次私有payload，下一次必须重新拿到未污染快照。
    # 函数用途: 模拟消费者持有并修改嵌套元数据。
    def summary(root, payload):
        assert payload["metadata"]["marker"] == "original"
        result = original(root, payload)
        payload["metadata"]["marker"] = "consumer-mutated"
        return result

    monkeypatch.setattr(bg, "_summary", summary)
    assert bg.list_background_processes(home)["total"] == 1
    assert bg.list_background_processes(home)["total"] == 1


# LLM: 多观察线程只读同代快照；返回字典由各调用者独占，不把共享缓存暴露给并发消费者。
# 函数用途: 验证并发命中不丢记录、不串改调用者数据。
def test_b1_hot_directory_cache_is_threadsafe(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    home = tmp_path / "owner"
    _b1_records(home)
    assert bg.list_background_processes(home)["total"] == 3
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(bg.list_background_processes, [home] * 24))
    assert all(result["projection_ok"] and result["total"] == 3 for result in results)
    results[0]["items"][0]["status"] = "mutated"
    assert results[1]["items"][0]["status"] == "exited"


# LLM: 初始旧mtime不能赋给随后刚被改坏的新文件；读后看到的新代次年龄要重新决定是否属于历史孤儿。
# 函数用途: 验证stat与读取之间的新坏JSON不会被旧文件年龄错误隔离。
def test_b1_new_corruption_during_read_cannot_inherit_old_mtime(tmp_path, monkeypatch):
    home = tmp_path / "owner"
    root = _b1_records(home, count=1)
    path = root / "bg-cache-0.json"
    old = time.time() - 120
    os.utime(path, (old, old))
    original = bg.read_bytes_beneath

    # LLM: 仅在真实reader即将打开指定记录时改写为新坏JSON，不伪造stat或错误类型。
    # 函数用途: 构造旧代次已stat、新代次被读取的确定交错。
    def read(anchor, parts, **kwargs):
        if anchor == root:
            path.write_text("newly-broken-json")
        return original(anchor, parts, **kwargs)

    monkeypatch.setattr(bg, "read_bytes_beneath", read)
    result = bg.list_background_processes(home, thread_id="thread-one")
    assert result["projection_ok"] is False
    assert result["errors"] == [{"session_id": "bg-cache-0", "error": "JSONDecodeError"}]
    assert not result["unrelated_errors"]


# LLM: 目录换代允许复用单文件，但不能给旧文件的复核期限重新起算60秒。
# 函数用途: 验证无关目录变动不会延后完整重读期限。
def test_b1_new_directory_generation_cannot_renew_old_file_ttl(tmp_path, monkeypatch):
    home = tmp_path / "owner"
    root = _b1_records(home)
    clock = [10.0]
    monkeypatch.setattr(bg.time, "monotonic", lambda: clock[0])
    reads = _b1_count_reads(monkeypatch)
    bg.list_background_processes(home)
    clock[0] = 63.0
    (root / "unrelated.tmp").write_text("directory changed")
    old = time.time() - 5
    os.utime(root, (old, old))
    bg.list_background_processes(home)
    assert len(reads) == 3
    clock[0] = 70.0
    bg.list_background_processes(home)
    assert len(reads) == 6


# LLM: 成功展示按来源优先级去重，但坏文件诊断不能被同名有效行或其它坏行吞掉。
# 函数用途: 验证跨根同名记录保留全部错误，并仍只展示一项有效记录。
@pytest.mark.parametrize("first_good,second_good", [(True, False), (False, True), (False, False), (True, True)])
def test_b1_duplicate_session_names_do_not_suppress_corruption(tmp_path, first_good, second_good):
    home = tmp_path / "owner"
    first = _b1_records(home, count=1)
    second = home / ".background_jobs/process_sessions"
    second.mkdir(parents=True)
    raw = (first / "bg-cache-0.json").read_text()
    for root, good in [(first, first_good), (second, second_good)]:
        (root / "bg-cache-0.json").write_text(raw if good else "broken-json")
    result = bg.list_background_processes(home)
    assert len(result["errors"]) == 2 - int(first_good) - int(second_good)
    assert result["total"] == int(first_good or second_good)


# LLM: 解码超深和坏schema内的深对象都只影响单文件；不允许deepcopy缓存把本可报告的错误变成全列表异常。
# 函数用途: 验证深JSON坏记录的只读隔离，同时保留其它有效任务。
@pytest.mark.parametrize("kind", ["decoder", "bad_schema", "valid_legacy"])
def test_b1_deep_json_does_not_abort_other_records(tmp_path, kind):
    import sys

    home = tmp_path / "owner"
    root = _b1_records(home, count=1)
    path = root / "bg-deep.json"
    depth = min(20000, sys.getrecursionlimit() * (2 if kind == "decoder" else 3) // (1 if kind == "decoder" else 4))
    nested = "[" * depth + "0" + "]" * depth
    if kind == "decoder":
        raw = nested
    else:
        extra = ',"schema":"invalid"' if kind == "bad_schema" else ''
        raw = '{"session_id":"bg-deep","status":"exited","thread_id":"thread-one"' + extra + ',"metadata":{"unused":' + nested + '}}'
    path.write_text(raw)
    old = time.time() - 10
    os.utime(path, (old, old))
    os.utime(root, (old, old))
    before = path.read_bytes()
    try:
        result = bg.list_background_processes(home, thread_id="thread-one")
    except RecursionError:
        result = None
    assert isinstance(result, dict), "深JSON不能逃出单文件隔离边界"
    assert any(row["session_id"] == "bg-cache-0" for row in result["items"])
    assert result["projection_ok"] is (kind == "valid_legacy")
    assert path.read_bytes() == before


# LLM: 外owner声明不得压过当前线程或自相冲突的归属，坏记录不能猜为无关。
# 函数用途: 锁定当前线程与外owner声明冲突时的失败关闭。
def test_b1_foreign_owner_corruption_with_current_thread_fails_closed(tmp_path):
    home = tmp_path / "owner"
    root = _b1_records(home, count=1)
    payload = {"session_id": "bg-corrupt", "schema": "invalid", "thread_id": "thread-one",
               "scope": {"thread_id": "other-thread", "owner_home": str(tmp_path / "other-owner")}}
    (root / "bg-corrupt.json").write_text(json.dumps(payload))
    result = bg.list_background_processes(home, thread_id="thread-one")
    assert result["projection_ok"] is False
    assert result["errors"] == [{"session_id": "bg-corrupt", "error": "ValueError"}]
    assert result["unrelated_errors"] == []


@pytest.mark.parametrize("location", [None, "metadata"])
@pytest.mark.parametrize("field", ["thread_id", "conversation_id", "owner_home", "task_id", "request_id"])
@pytest.mark.parametrize("value", [None, True, 23, [], {}, "", " \t"])
def test_b1r2_invalid_attribution_never_isolated(tmp_path, location, field, value):
    home = tmp_path / "owner"
    root = _b1_records(home, count=1)
    payload = {"session_id": "bg-corrupt", "schema": "invalid", "thread_id": "other-thread"}
    scope = payload if location is None else payload.setdefault(location, {})
    scope[field] = value
    path = root / "bg-corrupt.json"
    path.write_text(json.dumps(payload))
    old = time.time() - 120
    os.utime(path, (old, old))
    result = bg.list_background_processes(home, thread_id="thread-one")
    assert result["projection_ok"] is False
    assert result["errors"] == [{"session_id": "bg-corrupt", "error": "ValueError"}]
    assert not result["unrelated_errors"]


@pytest.mark.parametrize("field", ["thread_id", "conversation_id", "owner_home", "task_id", "request_id"])
def test_b1r2_conflicting_foreign_declarations_fail_closed(tmp_path, field):
    home = tmp_path / "owner"
    root = _b1_records(home, count=1)
    payload = {"session_id": "bg-corrupt", "schema": "invalid", "thread_id": "other-thread"}
    first, second = (str(tmp_path / "foreign-one"), str(tmp_path / "foreign-two")) if field == "owner_home" else ("foreign-one", "foreign-two")
    payload[field] = first
    payload["scope"] = {field: second}
    (root / "bg-corrupt.json").write_text(json.dumps(payload))
    result = bg.list_background_processes(home, thread_id="thread-one")
    assert result["projection_ok"] is False and result["errors"]
    assert not result["unrelated_errors"]


@pytest.mark.parametrize("location", [None, "completion_target"])
def test_b1r2_consistent_foreign_owner_without_current_thread_isolated(tmp_path, location):
    home = tmp_path / "owner"
    root = _b1_records(home, count=1)
    payload = {"session_id": "bg-corrupt", "schema": "invalid"}
    scope = payload if location is None else payload.setdefault(location, {})
    scope["owner_home"] = str(tmp_path / "other-owner")
    (root / "bg-corrupt.json").write_text(json.dumps(payload))
    result = bg.list_background_processes(home, thread_id="thread-one")
    assert result["projection_ok"] is True and not result["errors"]
    assert result["unrelated_errors"] == [{"session_id": "bg-corrupt", "error": "ValueError"}]


def test_b1r2_error_snapshot_preserves_ids_without_copying_body():
    scope = {"thread_id": "other", "task_id": "task-fixture", "request_id": "request-fixture"}
    payload = {**scope, "metadata": {**scope, "body": {"ignored": [0] * 1000}}, "body": "not attribution"}
    assert bg._error_scope_payload(payload) == {**scope, "metadata": scope}


def test_b1r2_directory_generations_replace_one_canonical_slot(tmp_path, monkeypatch):
    home = tmp_path / "owner"
    root = _b1_records(home)
    reads = _b1_count_reads(monkeypatch)
    assert bg.list_background_processes(home)["total"] == 3
    for generation in range(5):
        (root / "unrelated.tmp").write_text(str(generation))
        old = time.time() - 10 - generation
        os.utime(root, (old, old))
        assert bg.list_background_processes(home)["total"] == 3
    assert len(reads) == 3
    assert list(bg._DIRECTORY_CACHE) == [(str(home.resolve()), str(root.resolve()))]


# LLM: 同时保留4096正例和4097超限反例，只跟随常量单位命名更新，不改变目录上限或断言。
# 函数用途: 核对目录超限必须明确报错，不能静默截断成完整列表。
@pytest.mark.parametrize("count", [4096, 4097])
def test_b1r2_directory_record_limit_is_explicit_not_truncation(tmp_path, monkeypatch, count):
    home = tmp_path / "owner"
    root = _b1_records(home, count=count)
    before = _files(home)
    reads = _b1_count_reads(monkeypatch)
    result = bg.list_background_processes(home, thread_id="thread-one")
    if count == 4096:
        assert result["projection_ok"] is True and result["total"] == 4096
        assert not result["errors"] and len(reads) == 4096
        assert len(bg._DIRECTORY_CACHE) == 1
        assert bg.list_background_processes(home, thread_id="thread-one")["total"] == 4096
        assert len(reads) == 4096
    else:
        assert result["projection_ok"] is False
        assert any(error.get("error_code") == "DIRECTORY_RECORD_LIMIT_EXCEEDED" for error in result["errors"])
        assert result["total"] == 0 and not result["unrelated_errors"]
        assert not reads and not bg._DIRECTORY_CACHE
    assert getattr(bg, "_MAX_DIRECTORY_RECORDS_COUNT", None) == 4096
    assert len(bg._FILE_CACHE) <= 2048
    assert _files(home) == before and root.exists()


@pytest.mark.parametrize("change", ["state", "corruption"])
def test_b1r2_hot_directory_rechecks_in_place_file_stat(tmp_path, monkeypatch, change):
    home = tmp_path / "owner"
    root = _b1_records(home)
    reads = _b1_count_reads(monkeypatch)
    assert bg.list_background_processes(home)["total"] == 3
    path = root / "bg-cache-1.json"
    directory_mtime, inode = root.stat().st_mtime_ns, path.stat().st_ino
    if change == "state":
        payload = json.loads(path.read_text())
        payload.update(status="failed", command="updated-in-place-without-directory-change")
        path.write_text(json.dumps(payload))
    else:
        path.write_text("new-invalid-json")
    old = time.time() - 10
    os.utime(path, (old, old))
    assert path.stat().st_ino == inode and root.stat().st_mtime_ns == directory_mtime
    result = bg.list_background_processes(home, thread_id="thread-one")
    assert len(reads) == 4
    if change == "state":
        row = next(item for item in result["items"] if item["session_id"] == path.stem)
        assert row["status"] == "failed" and row["command"].startswith("updated-in-place")
        assert result["projection_ok"] is True
    else:
        assert result["projection_ok"] is False and result["total"] == 2
        assert result["errors"] == [{"session_id": path.stem, "error": "JSONDecodeError"}]
    assert bg.list_background_processes(home, thread_id="thread-one") == result
    assert len(reads) == 4


def test_b1r2_empty_directory_hits_do_not_renew_full_scan_deadline(tmp_path, monkeypatch):
    home = tmp_path / "owner"
    root = _b1_records(home, count=0)
    clock, scans = [10.0], []
    original = os.scandir

    def scan(path):
        if Path(path) == root:
            scans.append(path)
        return original(path)

    monkeypatch.setattr(bg.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(os, "scandir", scan)
    assert bg.list_background_processes(home)["total"] == 0
    clock[0] = 69.9
    assert bg.list_background_processes(home)["total"] == 0
    assert len(scans) == 1
    clock[0] = 70.0
    assert bg.list_background_processes(home)["total"] == 0
    assert len(scans) == 2
    clock[0] = 129.9
    assert bg.list_background_processes(home)["total"] == 0
    assert len(scans) == 2


def test_b1r2_refreshing_all_files_does_not_renew_directory_deadline(tmp_path, monkeypatch):
    home = tmp_path / "owner"
    root = _b1_records(home, count=1)
    clock, scans = [10.0], []
    original = os.scandir

    def scan(path):
        if Path(path) == root:
            scans.append(path)
        return original(path)

    monkeypatch.setattr(bg.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(os, "scandir", scan)
    reads = _b1_count_reads(monkeypatch)
    bg.list_background_processes(home)
    directory_mtime = root.stat().st_mtime_ns
    path = root / "bg-cache-0.json"
    payload = json.loads(path.read_text())
    payload["command"] = "changed-after-initial-scan"
    path.write_text(json.dumps(payload))
    old = time.time() - 10
    os.utime(path, (old, old))
    assert root.stat().st_mtime_ns == directory_mtime
    clock[0] = 50.0
    bg.list_background_processes(home)
    assert len(reads) == 2 and len(scans) == 1
    clock[0] = 70.0
    bg.list_background_processes(home)
    assert len(scans) == 2 and len(reads) == 3


# LLM: Wave9允许单次换代后重新扫描；预算内恢复必须包含新记录，缩小样本超限仍失败关闭。
# 函数用途: 核对扫描期间目录增长恢复不漏行，超限不被重试洗白。
@pytest.mark.parametrize("initial_count", [1, 2])
def test_b1r2_directory_growth_during_scan_cannot_claim_complete(tmp_path, monkeypatch, initial_count):
    home = tmp_path / "owner"
    root = _b1_records(home, count=initial_count)
    monkeypatch.setattr(bg, "_MAX_DIRECTORY_RECORDS_COUNT", 2)
    original, inserted = bg._directory_record_names, []

    def names(path):
        result = original(path)
        if path == root and not inserted:
            inserted.append(True)
            (root / "bg-added.json").write_text(json.dumps({"session_id": "bg-added", "status": "exited", "thread_id": "thread-one"}))
        return result

    monkeypatch.setattr(bg, "_directory_record_names", names)
    result = bg.list_background_processes(home, thread_id="thread-one")
    if initial_count == 1:
        assert result["projection_ok"] is True and result["total"] == 2
        assert {row["session_id"] for row in result["items"]} == {"bg-cache-0", "bg-added"}
        assert not result["errors"] and not result["unrelated_errors"]
        return
    assert result["projection_ok"] is False and result["total"] == 0
    assert result["errors"][0].get("error_code") == "DIRECTORY_RECORD_LIMIT_EXCEEDED"
    assert not result["unrelated_errors"] and not bg._DIRECTORY_CACHE


# LLM: 三类正常记录先由真实schema验证器确认合法，再仅污染待测归属；不使用invalid schema或替代reader。
# 函数用途: 构造无schema、合法v1/v4后台记录，不创建或启动真实进程。
def _b1r3_payload(home, version):
    from agent_py_agent.agent.tooling.process_session_records import (
        LEGACY_PROCESS_SESSION_SCHEMA,
        PROCESS_SESSION_SCHEMA,
    )

    payload = {"session_id": "bg-corrupt", "status": "exited", "thread_id": "other-thread"}
    if version == "legacy":
        payload.update(schema=LEGACY_PROCESS_SESSION_SCHEMA, pid=99999999, pid_birth_token="fixture", started_at=1.0,
                       access_scope={"owner_id": "fixture", "conversation_id": "other-thread", "owner_home": str(home)})
    elif version == "managed":
        payload.update(schema=PROCESS_SESSION_SCHEMA, status="not_started", revision=0,
                       activation_scope=None, retain_until_consumed=False,
                       access_scope={"owner_id": "fixture", "conversation_id": "other-thread", "owner_home": str(home)},
                       execution_scope={"owner_home": str(home), "thread_id": "other-thread", "root_task_id": "task-fixture",
                                        "run_id": "run-fixture", "attempt_id": "attempt-fixture"},
                       completion_target={}, launcher_pid=99999999, launcher_birth_token="fixture",
                       pid=0, pid_birth_token="", child_pid=0, child_pid_birth_token="",
                       reserved_at=1.0, started_at=0.0, finished_at=2.0, exit_code=None,
                       stop_requested=False, handoff_confirmed=False, child_launch_started=False,
                       command="fixture", cwd="", output_file="", host_state_file="", completion_notice_id="")
    if version != "schemaless":
        bg.validate_process_record(payload)
    return payload


@pytest.mark.parametrize("version", ["schemaless", "legacy", "managed"])
@pytest.mark.parametrize("location", [None, "metadata"])
@pytest.mark.parametrize("field", ["thread_id", "conversation_id", "owner_home", "task_id", "request_id"])
@pytest.mark.parametrize("value", [23, False, [], {}, " \t"])
def test_b1r3_normal_parse_rejects_invalid_attribution(tmp_path, version, location, field, value):
    home = tmp_path / "owner"
    root = _b1_records(home, count=1)
    payload = _b1r3_payload(home, version)
    scope = payload if location is None else payload.setdefault(location, {})
    scope[field] = value
    path = root / "bg-corrupt.json"
    path.write_text(json.dumps(payload))
    before = _files(home)
    result = bg.list_background_processes(home, thread_id="thread-one")
    assert result["projection_ok"] is False
    assert result["errors"] == [{"session_id": path.stem, "error": "ValueError"}]
    assert not result["unrelated_errors"]
    assert result["total"] == 1 and _files(home) == before
    with pytest.raises(ValueError, match="^invalid_session_attribution$"):
        bg._validate_session_payload(payload, path.stem, home)


@pytest.mark.parametrize("version", ["schemaless", "legacy", "managed"])
@pytest.mark.parametrize("location", ["access_scope", "execution_scope", "completion_target", "metadata", "scope"])
@pytest.mark.parametrize("value", [False, 0, [], ""])
def test_b1r3_normal_parse_rejects_present_nonmapping_scope(tmp_path, version, location, value):
    home = tmp_path / "owner"
    root = _b1_records(home, count=1)
    payload = _b1r3_payload(home, version)
    payload[location] = value
    (root / "bg-corrupt.json").write_text(json.dumps(payload))
    result = bg.list_background_processes(home, thread_id="thread-one")
    assert result["projection_ok"] is False and result["errors"]
    assert not result["unrelated_errors"]
    with pytest.raises(ValueError, match="^invalid_session_scope$"):
        bg._validate_session_payload(payload, "bg-corrupt", home)


@pytest.mark.parametrize("version", ["schemaless", "legacy", "managed"])
@pytest.mark.parametrize("field", ["thread_id", "conversation_id", "owner_home", "task_id", "request_id"])
@pytest.mark.parametrize("difference", ["distinct", "whitespace"])
def test_b1r3_normal_parse_rejects_conflicting_attribution(tmp_path, version, field, difference):
    home = tmp_path / "owner"
    root = _b1_records(home, count=1)
    payload = _b1r3_payload(home, version)
    first = str(tmp_path / "foreign") if field == "owner_home" else "other-thread"
    payload[field] = first
    payload["metadata"] = {field: first + ("-different" if difference == "distinct" else " ")}
    (root / "bg-corrupt.json").write_text(json.dumps(payload))
    result = bg.list_background_processes(home, thread_id="thread-one")
    assert result["projection_ok"] is False and result["errors"]
    assert not result["unrelated_errors"]
    with pytest.raises(ValueError, match="^conflicting_session_attribution$"):
        bg._validate_session_payload(payload, "bg-corrupt", home)


@pytest.mark.parametrize("version", ["schemaless", "legacy", "managed"])
def test_b1r3_valid_normal_records_remain_valid_and_filtered(tmp_path, version):
    home = tmp_path / "owner"
    root = _b1_records(home, count=1)
    payload = _b1r3_payload(home, version)
    (root / "bg-corrupt.json").write_text(json.dumps(payload))
    result = bg.list_background_processes(home, thread_id="thread-one")
    assert result["projection_ok"] is True and result["total"] == 1
    assert not result["errors"] and not result["unrelated_errors"]
    assert bg.list_background_processes(home)["total"] == 2


def test_b1r3_schemaless_none_attribution_and_scopes_remain_optional(tmp_path):
    payload = {"session_id": "bg-optional", "status": "exited",
               **dict.fromkeys((*bg._ATTRIBUTION_FIELDS, *bg._SCOPE_FIELDS))}
    assert bg._validate_session_payload(payload, "bg-optional", tmp_path) == payload
    assert bg._record_thread_id(payload) == ""


@pytest.mark.parametrize("value", [23, True, [], {}, "", " \t", None])
def test_b1r3_thread_reader_never_coerces_invalid_values(value):
    assert bg._record_thread_id({"thread_id": value, "metadata": {"conversation_id": value}}) == ""


# LLM: 按MCP生产路径序列化真实scope dataclass，v3不带v4保留字段；原生验证先于观察，激活ID沿64位hex合同。
# 函数用途: 构造合法共享MCP元数据，不启动MCP或伪造插件安装/激活授权。
def _b1r4_shared_payload(home, version):
    from dataclasses import asdict

    from agent_py_agent.agent.tooling.process_scope import (
        ProcessAccessScope,
        ProcessActivationScope,
        ProcessExecutionScope,
    )
    from agent_py_agent.agent.tooling.process_session_records import (
        ACTIVATION_PROCESS_SESSION_SCHEMA,
    )

    payload = _b1r3_payload(home, "managed")
    payload.pop("thread_id")
    payload.update(access_scope=asdict(ProcessAccessScope("fixture", "", str(home))),
                   execution_scope=asdict(ProcessExecutionScope(owner_home=str(home))),
                   activation_scope=asdict(ProcessActivationScope("fixture", str(home), "mcp-demo", "a" * 64)))
    if version == "v3":
        payload["schema"] = ACTIVATION_PROCESS_SESSION_SCHEMA
        payload.pop("retain_until_consumed")
    bg.validate_process_record(payload)
    return payload


@pytest.mark.parametrize("version", ["v3", "v4"])
def test_b1r4_native_shared_mcp_does_not_poison_any_thread(tmp_path, version):
    home = tmp_path / "owner"
    root, _ = _session(home, session_id="bg-one", thread_id="thread-one")
    _session(home, session_id="bg-two", thread_id="thread-two")
    payload = _b1r4_shared_payload(home, version)
    (root / "bg-corrupt.json").write_text(json.dumps(payload))
    old = time.time() - 10
    for path in root.glob("bg-*.json"):
        os.utime(path, (old, old))
    os.utime(root, (old, old))
    before = _files(home)
    for thread, expected in [("thread-one", "bg-one"), ("thread-two", "bg-two")]:
        result = bg.list_background_processes(home, thread_id=thread)
        assert result["projection_ok"] is True and result["errors"] == []
        assert result["active_count"] == 1
        assert not result["unrelated_errors"]
        assert {row["session_id"] for row in result["items"]} == {expected}
    assert bg.list_background_processes(home)["total"] == 3
    assert bg._record_thread_id(payload) == "" and _files(home) == before


@pytest.mark.parametrize("version", ["schemaless", "legacy", "managed"])
@pytest.mark.parametrize("location", [None, "metadata"])
@pytest.mark.parametrize("field", ["owner_id", "thread_id", "conversation_id", "owner_home", "task_id", "request_id"])
def test_b1r4_normal_empty_strings_are_unbound(tmp_path, version, location, field):
    home = tmp_path / "owner"
    root = _b1_records(home, count=1)
    payload = _b1r3_payload(home, version)
    scope = payload if location is None else payload.setdefault(location, {})
    scope[field] = ""
    (root / "bg-corrupt.json").write_text(json.dumps(payload))
    result = bg.list_background_processes(home, thread_id="thread-one")
    assert result["projection_ok"] is True and not result["errors"] and not result["unrelated_errors"]
    assert bg.list_background_processes(home)["total"] == 2


@pytest.mark.parametrize("version", ["schemaless", "legacy", "managed"])
@pytest.mark.parametrize("location", [None, "access_scope", "execution_scope", "completion_target", "metadata", "scope", "activation_scope"])
@pytest.mark.parametrize("value", [23, True, [], {}, " \t\n"])
def test_b1r4_normal_owner_id_type_is_strict(tmp_path, version, location, value):
    home = tmp_path / "owner"
    root = _b1_records(home, count=1)
    payload = _b1r3_payload(home, version)
    if location is None:
        payload["owner_id"] = value
    else:
        if payload.get(location) is None:
            payload[location] = {}
        payload[location]["owner_id"] = value
    (root / "bg-corrupt.json").write_text(json.dumps(payload))
    result = bg.list_background_processes(home, thread_id="thread-one")
    assert result["projection_ok"] is False and result["errors"] and not result["unrelated_errors"]
    with pytest.raises(ValueError, match="^invalid_session_attribution$"):
        bg._validate_session_payload(payload, "bg-corrupt", home)


@pytest.mark.parametrize("version", ["schemaless", "legacy", "managed"])
def test_b1r4_top_level_owner_id_conflict_fails_closed(tmp_path, version):
    home = tmp_path / "owner"
    root = _b1_records(home, count=1)
    payload = _b1r3_payload(home, version)
    payload["owner_id"] = "owner-A"
    payload.setdefault("access_scope", {})["owner_id"] = "owner-B"
    (root / "bg-corrupt.json").write_text(json.dumps(payload))
    result = bg.list_background_processes(home, thread_id="thread-one")
    assert result["projection_ok"] is False and result["errors"] and not result["unrelated_errors"]
    with pytest.raises(ValueError, match="^conflicting_session_attribution$"):
        bg._validate_session_payload(payload, "bg-corrupt", home)


@pytest.mark.parametrize("version", ["schemaless", "legacy", "managed"])
@pytest.mark.parametrize("value", [False, [], "bad-scope"])
def test_b1r4_activation_scope_requires_mapping_or_none(tmp_path, version, value):
    home = tmp_path / "owner"
    root = _b1_records(home, count=1)
    payload = _b1r3_payload(home, version)
    payload["activation_scope"] = value
    (root / "bg-corrupt.json").write_text(json.dumps(payload))
    result = bg.list_background_processes(home, thread_id="thread-one")
    assert result["projection_ok"] is False and result["errors"] and not result["unrelated_errors"]
    with pytest.raises(ValueError, match="^invalid_session_scope$"):
        bg._validate_session_payload(payload, "bg-corrupt", home)


@pytest.mark.parametrize("field", ["owner_id", "owner_home"])
@pytest.mark.parametrize("version", ["schemaless", "legacy", "managed"])
def test_b1r4_activation_owner_conflict_is_not_ignored(tmp_path, field, version):
    home = tmp_path / "owner"
    root = _b1_records(home, count=1)
    payload = _b1r4_shared_payload(home, "v4") if version == "managed" else _b1r3_payload(home, version)
    payload.update(owner_id="fixture", owner_home=str(home))
    payload["activation_scope"] = {"owner_id": "fixture", "owner_home": str(home), "plugin_id": "mcp-demo", "activation_id": "a" * 64}
    payload["activation_scope"][field] = "other-owner" if field == "owner_id" else str(tmp_path / "other-owner")
    (root / "bg-corrupt.json").write_text(json.dumps(payload))
    result = bg.list_background_processes(home, thread_id="thread-one")
    assert result["projection_ok"] is False and result["errors"] and not result["unrelated_errors"]
    with pytest.raises(ValueError, match="^conflicting_session_attribution$"):
        bg._validate_session_payload(payload, "bg-corrupt", home)


@pytest.mark.parametrize("location", ["access_scope", "activation_scope"])
@pytest.mark.parametrize("value", [None, "", " \t", 23, True, [], {}, "owner-B"])
def test_b1r4_corrupt_owner_id_never_excused_as_foreign(tmp_path, location, value):
    home = tmp_path / "owner"
    root = _b1_records(home, count=1)
    payload = {"session_id": "bg-corrupt", "schema": "invalid", "thread_id": "other-thread", "owner_id": "owner-A",
               location: {"owner_id": value}}
    (root / "bg-corrupt.json").write_text(json.dumps(payload))
    result = bg.list_background_processes(home, thread_id="thread-one")
    assert result["projection_ok"] is False and result["errors"] and not result["unrelated_errors"]


@pytest.mark.parametrize("field", ["owner_id", "conversation_id"])
def test_b1r4_v1_schema_still_rejects_required_empty_binding(tmp_path, field):
    home = tmp_path / "owner"
    root = _b1_records(home, count=1)
    payload = _b1r3_payload(home, "legacy")
    payload["access_scope"][field] = ""
    with pytest.raises(ValueError):
        bg.validate_process_record(payload)
    (root / "bg-corrupt.json").write_text(json.dumps(payload))
    result = bg.list_background_processes(home, thread_id="thread-one")
    assert result["projection_ok"] is False and result["errors"] and not result["unrelated_errors"]


# LLM: v2不带激活和保留字段，v3不带保留字段；先通过真实原生验证器，再由调用测试单点污染。
# 函数用途: 复用已有记录构造合法v2/v3/v4普通任务元数据，不启动进程。
def _b1r5_payload(home, version):
    payload = _b1r3_payload(home, "managed")
    payload["schema"] = f"managed_process_session.{version}"
    if version != "v4":
        payload.pop("retain_until_consumed")
    if version == "v2":
        payload.pop("activation_scope")
    bg.validate_process_record(payload)
    return payload


# LLM: 成对空home复现原生schema仍接受的绕过；独立scope也必须由投影门报指定错误，冷热错误不可豁免。
# 函数用途: 锁住v2/v3/v4必需owner_home非空，保留当前记录展示与全程不写盘。
@pytest.mark.parametrize("version", ["v2", "v3", "v4"])
@pytest.mark.parametrize("location", ["paired", "access_scope", "execution_scope", "activation_scope"])
def test_b1r5_v2_v3_v4_reject_empty_scope_owner_home(tmp_path, version, location):
    home = tmp_path / "owner"
    root = _b1_records(home, count=1)
    payload = (_b1r4_shared_payload(home, version) if location == "activation_scope" and version != "v2"
               else _b1r5_payload(home, version))
    if location == "paired":
        payload["access_scope"]["owner_home"] = payload["execution_scope"]["owner_home"] = ""
        bg.validate_process_record(payload)
    elif location == "activation_scope":
        if version == "v2":
            payload[location] = {"owner_id": "fixture", "plugin_id": "mcp-demo", "activation_id": "a" * 64}
        payload[location]["owner_home"] = ""
    else:
        payload[location]["owner_home"] = ""
    path = root / "bg-corrupt.json"
    path.write_text(json.dumps(payload))
    old = time.time() - 10
    os.utime(path, (old, old))
    os.utime(root, (old, old))
    before = _files(home)
    for _ in range(2):
        result = bg.list_background_processes(home, thread_id="thread-one")
        assert result["projection_ok"] is False and result["errors"] and not result["unrelated_errors"]
        assert result["total"] == 1 and _files(home) == before
    with pytest.raises(ValueError, match="^invalid_session_attribution$"):
        bg._validate_session_payload(payload, "bg-corrupt", home)


# LLM: 激活owner_id不能借可选未绑定空串规则过关；v2夹带激活也先报归属错误，不伪称合法v2共享资源。
# 函数用途: 验证激活必需owner_id空串在原生schema转换之前被拒绝。
@pytest.mark.parametrize("version", ["v2", "v3", "v4"])
def test_b1r5_activation_rejects_empty_owner_id(tmp_path, version):
    home = tmp_path / "owner"
    root = _b1_records(home, count=1)
    payload = _b1r4_shared_payload(home, version) if version != "v2" else _b1r5_payload(home, version)
    if version == "v2":
        payload["activation_scope"] = {"owner_home": str(home), "plugin_id": "mcp-demo", "activation_id": "a" * 64}
    payload["activation_scope"]["owner_id"] = ""
    (root / "bg-corrupt.json").write_text(json.dumps(payload))
    result = bg.list_background_processes(home, thread_id="thread-one")
    assert result["projection_ok"] is False and result["errors"] and not result["unrelated_errors"]
    with pytest.raises(ValueError, match="^invalid_session_attribution$"):
        bg._validate_session_payload(payload, "bg-corrupt", home)
