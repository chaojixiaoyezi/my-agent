from __future__ import annotations

import os
import threading
import time
from dataclasses import replace

import pytest

from agent_py_agent.agent.adapter.manager import _render_gateway_progress
from agent_py_agent.agent.common.cancellation import CancellationToken
from agent_py_agent.agent.gateway_parts.http_handlers import _read_public_progress_events
from agent_py_agent.agent.gateway_parts.stream_writer import BufferedChunkStreamWriter
from agent_py_agent.agent.tooling.executor import ToolExecutor, ToolExecutorRequest
from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
from agent_py_agent.tests.test_plugin_event_gateway import event_warning_log, gateway
from agent_py_agent.tests.test_plugin_event_runtime import _params
from agent_py_agent.tests.test_tool_runtime_unification import _call, _CountingTool, _snapshot

_FIXTURES = (gateway, event_warning_log)


# LLM: 测试 handler 不产生任何输出，等待唯一执行器的真实心跳；不替被测代码发事件。
# 类用途: 在测试中稳定模拟长时间无输出和异常工具。
class SilentTool(_CountingTool):
    def __init__(self, pulse: threading.Event, *, raises: bool = False):
        super().__init__()
        self.pulse = pulse
        self.raises = raises

    def execute(self, params):
        self.executions += 1
        assert self.pulse.wait(2), "真实 handler 没有收到执行器心跳"
        if self.raises:
            raise RuntimeError("test handler failure")
        return ToolHandlerOutcome(self.model_spec.name, True, "完整最终结果")


@pytest.mark.parametrize("raises", [False, True])
def test_executor_refreshes_silent_handler_and_stops_before_return(tmp_path, raises):
    pulse = threading.Event()
    tool = SilentTool(pulse, raises=raises)
    events = []

    def progress(event):
        events.append(event)
        pulse.set()

    result = ToolExecutor().execute(ToolExecutorRequest(
        call=_call(tool, {"value": "secret-not-public"}), runtime_snapshot=_snapshot(tool),
        workspace_root=tmp_path, operation_store_required=False,
        on_handler_progress=progress, handler_progress_interval_seconds=0.02,
    ))
    assert result.result.ok is not raises
    assert events and all(e["wait_state"] == "handler_result" for e in events)
    assert events[0]["elapsed_seconds"] >= 0.01
    assert "secret-not-public" not in str(events)
    before = len(events)
    time.sleep(0.05)
    assert len(events) == before


def test_no_liveness_for_rejected_or_already_cancelled_call(tmp_path):
    tool = _CountingTool()
    events = []
    request = ToolExecutorRequest(call=_call(tool, {"value": "test"}), runtime_snapshot=_snapshot(tool),
        workspace_root=tmp_path, operation_store_required=False, on_handler_progress=events.append,
        handler_progress_interval_seconds=0.01)
    invalid = replace(request.call, arguments={})
    assert not ToolExecutor().execute(replace(request, call=invalid)).result.ok
    token = CancellationToken()
    token.cancel()
    assert not ToolExecutor().execute(replace(request, cancellation_token=token)).result.ok
    assert not events and tool.executions == 0


def test_cancelled_handler_stops_heartbeat_even_before_handler_returns(tmp_path):
    token = CancellationToken()
    pulse = threading.Event()
    tool = SilentTool(pulse)
    events = []

    def progress(event):
        events.append(event)
        token.cancel()
        time.sleep(0.04)
        pulse.set()

    ToolExecutor().execute(ToolExecutorRequest(call=_call(tool, {"value": "test"}),
        runtime_snapshot=_snapshot(tool), workspace_root=tmp_path, operation_store_required=False,
        on_handler_progress=progress, handler_progress_interval_seconds=0.01, cancellation_token=token))
    assert len(events) == 1


def test_original_gateway_projection_shows_real_elapsed_without_model_or_stdout(tmp_path):
    writer = BufferedChunkStreamWriter(tmp_path / "chunks.jsonl", delivery_channel="feishu")
    event = {"call_id": "call-live", "tool": "run_command", "phase": "running",
             "status": "执行中", "wait_state": "handler_result", "elapsed_seconds": 61.25}
    writer.write_tool_liveness(event)
    assert _read_public_progress_events(writer.chunk_path, 0)[0][0]["level"] == "minimal"
    assert _render_gateway_progress(_read_public_progress_events(writer.chunk_path, 0)[0][0]) == "执行中：run_command"
    writer.set_verbose_level("stream", preview_max_chars=100)
    writer.write_tool_liveness(event)
    public, _ = _read_public_progress_events(writer.chunk_path, 0, channel="feishu")
    assert public[1]["preview_max_chars"] == 100
    text = _render_gateway_progress(public[1])
    assert "61.25" in text and "仍在执行" in text and "等待工具结果" in text
    writer.rich_transcript = True
    writer.write_tool_liveness(event)
    assert len(_read_public_progress_events(writer.chunk_path, 0)[0]) == 2


def test_real_model_tool_seam_passes_liveness_into_registry(gateway, tmp_path):
    from agent_py_agent.agent.agent_core.tool_call_runtime import (
        ToolCallRuntimeRequest,
        execute_traced_tool_call,
    )
    from agent_py_agent.agent.agent_core.tool_loop.round_execution import ToolCallExecuteParams

    pulse = threading.Event()
    tool = SilentTool(pulse)
    agent, _paths, _server, _events = gateway
    params = _params(agent, tool)
    writer = BufferedChunkStreamWriter(tmp_path / "chunks.jsonl", delivery_channel="feishu")
    writer.set_verbose_level("stream")
    original = writer.write_tool_liveness

    def observe(event):
        original(event)
        pulse.set()

    writer.write_tool_liveness = observe
    params = replace(params, effective_on_chunk=writer)
    agent.config.im_stream_heartbeat_seconds = 1
    call = replace(_call(tool, {"value": "no-output"}), run_id=params.run_id, attempt_id=params.attempt_id)
    execution = execute_traced_tool_call(ToolCallRuntimeRequest(agent, ToolCallExecuteParams(params, 1, 1, call), call))
    assert execution.result.ok
    public, _ = _read_public_progress_events(writer.chunk_path, 0)
    assert public[0]["round"] == public[0]["call_index"] == 1
    assert public[0]["elapsed_seconds"] >= 0.9


def test_real_silent_shell_updates_window_and_stops_after_final(tmp_path):
    from agent_py_agent.agent.adapter.delivery import (
        GatewayReplyDeliveryStore,
        GatewayReplyDeliveryWorker,
        PendingGatewayReply,
    )
    from agent_py_agent.agent.adapter.progress_draft import GatewayProgressBatch, send_gateway_draft
    from agent_py_agent.agent.delivery import (
        ChannelAdapterRegistry,
        ChannelCapabilities,
        DeliveryService,
    )
    from agent_py_agent.agent.tooling.shell import ShellTool
    from agent_py_agent.tests.test_im_progress_draft import Adapter

    duration = float(os.environ.get("MY_AGENT_REAL_LONG_TOOL_SECONDS", "2.5"))
    interval = 120.0 if duration >= 125 else 1.0
    writer = BufferedChunkStreamWriter(tmp_path / "chunks.jsonl", delivery_channel="feishu")
    writer.set_verbose_level("off", timezone="Asia/Shanghai")
    provider = Adapter()
    registry = ChannelAdapterRegistry()
    registry.register_adapter("feishu", provider, capabilities=ChannelCapabilities(edit_progress=True))
    service = DeliveryService(registry)
    store = GatewayReplyDeliveryStore(tmp_path / "delivery")
    final = [None]
    finals = []

    def poll(row):
        events, cursor = _read_public_progress_events(writer.chunk_path, row.progress_cursor, channel="feishu")
        return GatewayProgressBatch(tuple(_render_gateway_progress(e) for e in events), cursor, "stream", True, 100,
                                    events[-1].get("updated_at", "") if events else "")

    worker = GatewayReplyDeliveryWorker(store, poll_response=lambda _: final[0],
        deliver_response=lambda _, text: finals.append(text) or True, poll_progress=poll,
        deliver_progress=lambda row, text: send_gateway_draft(service, row, text))
    worker.enqueue(PendingGatewayReply(request_id="gw-real-silent", channel="feishu", user_id="ou_synthetic", message_id="om_synthetic"))

    def progress(event):
        writer.write_tool_liveness(event)
        worker.run_once()

    tool = ShellTool(tmp_path)
    started = time.monotonic()
    result = ToolExecutor().execute(ToolExecutorRequest(call=_call(tool, {"command": f"sleep {duration:g}", "timeout": int(duration) + 16}),
        runtime_snapshot=_snapshot(tool), workspace_root=tmp_path, operation_store_required=False,
        on_handler_progress=progress, handler_progress_interval_seconds=interval))
    assert result.result.ok and result.result.handler_executed, (result.result.error_code, result.result.output)
    assert time.monotonic() - started >= duration
    assert len(provider.created) == 1
    preview = store.pending()[0].progress_draft["content"]
    assert "执行中：run_command" in preview and "更新于 " in preview and len(preview) <= 100
    if duration >= 245 or duration < 125:
        assert provider.edits and all(identity == "om_preview" for identity, _ in provider.edits)
    final[0] = "独立完整最终结果"
    worker.run_once()
    assert finals == [final[0]] and not store.pending()
    count = len(provider.edits)
    time.sleep(0.05)
    worker.run_once()
    assert len(provider.edits) == count
