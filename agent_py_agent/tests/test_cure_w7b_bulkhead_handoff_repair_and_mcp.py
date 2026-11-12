# LLM: W7B 专项回归单测：
#   1. Stage A 工具启动前崩溃降级（INTERNAL_ERROR, not_started/unknown, retryable=False, 防盲目重试说明）；
#   2. Stage B 工具后置记账崩溃（record_one 抛异常保持真实 ToolResult ok=True 不变，补齐 tool_context 最小配对，发 runtime_notice）；
#   3. HandoffBoardTool 单代理前台拦截（SINGLE_AGENT_HANDOFF_NOT_NEEDED, use_todo_tool）、多代理放行与 list_cases 紧凑 4 字段；
#   4. ToolLoopRepairCounters 跨轮交替累计上限（cumulative_turn_repairs == 10 确定性收敛停止）；
#   5. MCPStdioClient 首次冷启动零修改与断线重连警告注入，及 terminate_process_tree 瞬态子进程退出快照重试。
"""Unit and regression tests for Workstream W7B bulkhead, handoff, repair ceiling, and MCP."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.agent_core.orchestration.tools.handoff_board_tool import (
    HandoffBoardTool,
    ManageHandoffBoardTool,
    build_manage_handoff_board_model_spec,
)
from agent_py_agent.agent.agent_core.tool_loop.response_decision import (
    _MAX_CUMULATIVE_TURN_REPAIR_COUNT,
    ToolLoopRepairCounters,
    ToolLoopResponseDecisionRequest,
    _inc_protocol,
    _inc_truncated_output,
    _inc_truncated_write,
    _native_truncated_write_decision,
    _protocol_violation_decision,
    _truncated_output_resume_decision,
)
from agent_py_agent.agent.agent_core.tool_loop.round_execution import (
    _ensure_minimal_tool_context_pairing,
    _record_bulkhead_fallback,
    _record_execution,
    _synthetic_execution,
)
from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.collaboration.handoff_board import HandoffBoardService
from agent_py_agent.agent.collaboration.store import CollaborationStore
from agent_py_agent.agent.gateway_parts.stream_writer import BufferedChunkStreamWriter
from agent_py_agent.agent.tooling.executor import ToolExecution
from agent_py_agent.agent.tooling.mcp_client import MCPServerConfig, MCPStdioClient
from agent_py_agent.agent.tooling.models import ToolInvocationContext
from agent_py_agent.agent.tooling.process_registry import (
    ProcessTerminationReceipt,
    terminate_process_tree,
)
from agent_py_agent.agent.tooling.runtime_contracts import (
    ToolCall,
    ToolFailureFacts,
    ToolResult,
)


def _make_tool_call(call_id: str, tool_name: str, arguments: dict) -> ToolCall:
    return ToolCall(call_id, tool_name, arguments, "native", "sha256:test", "run", "turn", "attempt")


# ---------------------------------------------------------------------------
# 1. Stage A: Pre-result tool crash bulkhead
# ---------------------------------------------------------------------------
def test_stage_a_pre_result_bulkhead_fallback() -> None:
    recorded_params = []
    emitted_events = []

    call = _make_tool_call(call_id="call_a_1", tool_name="bash", arguments={"cmd": "echo test"})
    request = SimpleNamespace(
        calls=[call],
        params=SimpleNamespace(tool_context=[]),
        tool_rounds=1,
        actor="model",
        decision_ref=None,
        record_one=lambda p: recorded_params.append(p),
    )
    exc = RuntimeError("handler crashed before yielding result")
    started_at = time.monotonic() - 0.05

    with patch(
        "agent_py_agent.agent.agent_core.tool_loop.round_execution._emit_tool_progress",
        side_effect=lambda evt: emitted_events.append(evt),
    ):
        wrote, transition = _record_bulkhead_fallback(
            request,
            idx=1,
            call=call,
            started_at=started_at,
            exc=exc,
            started_emitted=True,
            effect_outcome="not_started",
        )

    assert wrote is False
    assert transition is None
    assert len(recorded_params) == 1
    fallback = recorded_params[0].result

    assert fallback.ok is False
    assert fallback.effect_outcome == "not_started"
    assert fallback.metadata.get("retryable") is False
    assert fallback.metadata.get("reported_error_code") == "INTERNAL_ERROR"

    payload = json.loads(fallback.output)
    assert payload["ok"] is False
    assert payload["error_code"] == "INTERNAL_ERROR"
    assert payload["effect_outcome"] == "not_started"
    assert payload["retryable"] is False
    assert "RuntimeError: handler crashed before yielding result" in payload["cause"]
    assert "请勿使用完全相同的参数原样盲目重试" in payload["error"]


# ---------------------------------------------------------------------------
# 2. Stage B: Post-result bookkeeping failure preservation & notice
# ---------------------------------------------------------------------------
def test_stage_b_post_result_bookkeeping_failure_preserves_real_result(tmp_path: Path) -> None:
    chunk_file = tmp_path / "stream.chunk"
    writer = BufferedChunkStreamWriter(chunk_path=chunk_file, rich_transcript=True)

    tool_context = []
    call = _make_tool_call(call_id="c_b_1", tool_name="write_file", arguments={"path": "out.txt", "content": "data"})
    request = SimpleNamespace(
        agent=SimpleNamespace(config=SimpleNamespace(im_stream_heartbeat_seconds=120.0)),
        params=SimpleNamespace(
            tool_context=tool_context,
            effective_on_chunk=writer,
            host_actions=[],
        ),
        tool_rounds=2,
        calls=[call],
        actor="model",
        decision_ref=None,
        record_one=MagicMock(side_effect=RuntimeError("database lock on tool_runtime_ledger")),
    )
    real_result = ToolResult.succeeded(call, "File successfully written.")
    execution = _synthetic_execution(call, real_result, "SUCCESS")

    with patch.object(writer, "write_runtime_notice", wraps=writer.write_runtime_notice) as mock_notice:
        wrote, transition = _record_execution(
            request,
            idx=1,
            started_at=time.monotonic() - 0.02,
            execution=execution,
        )

    assert wrote is False
    assert execution.result.ok is True
    assert execution.result.output == "File successfully written."

    assert mock_notice.called
    kwargs = mock_notice.call_args.kwargs
    assert kwargs["error_code"] == "POST_TOOL_BOOKKEEPING_WARNING"
    assert kwargs["level"] == "warning"
    assert "⚠ 后台归档/自学习记录写入失败（RuntimeError）" in kwargs["text"]

    assert len(tool_context) == 1
    assert "File successfully written." in tool_context[0]


# ---------------------------------------------------------------------------
# 3. HandoffBoardTool single-agent rejection, subagent pass, compact list_cases
# ---------------------------------------------------------------------------
def test_handoff_board_single_agent_rejection_and_list_cases(tmp_path: Path) -> None:
    collab_dir = tmp_path / "collab"
    store = CollaborationStore(collab_dir)
    service = HandoffBoardService(store, workspace_root=tmp_path)

    # 3.1 Spec description check
    spec = build_manage_handoff_board_model_spec()
    assert "单代理独立执行时严禁调用本工具" in spec.description

    # 3.2 Foreground single agent with zero subagents
    subagents_mock = MagicMock()
    subagents_mock.list_runs_for_root_report.return_value = SimpleNamespace(runs=[])
    subagents_mock.list_current_runs_report.return_value = SimpleNamespace(runs=[])
    subagents_mock.list_runs.return_value = []

    single_agent = SimpleNamespace(
        subagents=subagents_mock,
        collaboration_store=store,
        workspace_root=tmp_path,
        _current_run_params=SimpleNamespace(run_id="root-run-001"),
        _current_thread=SimpleNamespace(thread_id="thread-main", thread_kind="conversation"),
    )
    tool = ManageHandoffBoardTool(agent=single_agent, service=service)
    ctx = ToolInvocationContext(runtime_snapshot=MagicMock())

    reject_res = tool.execute_scoped(
        {"action": "open_case", "title": "Single agent attempt"},
        ctx,
    )
    assert reject_res.ok is False
    assert reject_res.error_code == "SINGLE_AGENT_HANDOFF_NOT_NEEDED"
    assert reject_res.effect_outcome == "not_started"
    assert reject_res.result_envelope["retryable"] is False
    assert reject_res.result_envelope["next_action"] == "use_todo_tool"

    # 3.3 Multi-agent context allows open_case
    subagents_mock.list_runs_for_root_report.return_value = SimpleNamespace(runs=[MagicMock()])
    allow_res = tool.execute_scoped(
        {"action": "open_case", "title": "Multi agent valid case"},
        ctx,
    )
    assert allow_res.ok is True
    case_id = allow_res.result_envelope["case"]["case_id"]

    # 3.4 list_cases slim 4-field output
    list_res = tool.execute_scoped({"action": "list_cases"}, ctx)
    assert list_res.ok is True
    cases = list_res.result_envelope.get("cases") or []
    assert len(cases) >= 1
    for c in cases:
        assert set(c.keys()) == {"case_id", "status", "title", "updated_at"}


# ---------------------------------------------------------------------------
# 4. Cumulative turn repair ceiling stops at 10
# ---------------------------------------------------------------------------
def test_cumulative_turn_repair_ceiling_stops_at_10() -> None:
    counters = ToolLoopRepairCounters()
    assert counters.cumulative_turn_repairs == 0

    # Alternating repairs increment cumulative_turn_repairs
    counters = _inc_protocol(counters)
    assert counters.protocol_repairs == 1
    assert counters.cumulative_turn_repairs == 1

    counters = _inc_truncated_write(counters)
    assert counters.truncated_write_repairs == 1
    assert counters.cumulative_turn_repairs == 2

    # Clean round resets consecutive counters but preserves cumulative_turn_repairs
    counters = replace(
        counters,
        protocol_repairs=0,
        truncated_write_repairs=0,
        truncated_output_repairs=0,
    )
    assert counters.protocol_repairs == 0
    assert counters.cumulative_turn_repairs == 2

    # Advance cumulative_turn_repairs to 10
    counters = replace(counters, cumulative_turn_repairs=_MAX_CUMULATIVE_TURN_REPAIR_COUNT)

    req = ToolLoopResponseDecisionRequest(
        agent=MagicMock(),
        params=SimpleNamespace(tool_context=[], max_protocol_repairs=5),
        response=ModelResponse(text="[invalid_json]", backend="tool_loop"),
        counters=counters,
        turn_id="turn_rep_test",
    )

    violation = SimpleNamespace(to_dict=lambda: {"code": "TOOL_CALL_INVALID_SYNTAX"}, code="TOOL_CALL_INVALID_SYNTAX")
    decision = _protocol_violation_decision(req, (violation,))
    assert decision.action == "break"
    assert decision.response.runtime_reason == "cumulative_turn_repairs_exceeded"

    write_call = _make_tool_call("w1", "write_file", {"path": "a.txt"})
    write_resp = ModelResponse(
        text="",
        backend="tool_loop",
        truncated=True,
        runtime_status="unfinished",
        runtime_reason="MODEL_RESPONSE_TRUNCATED",
        runtime_source="model_provider",
    )
    write_req = ToolLoopResponseDecisionRequest(
        agent=MagicMock(),
        params=SimpleNamespace(tool_context=[], native_tool_use=True),
        response=write_resp,
        counters=counters,
        turn_id="turn_rep_test",
    )
    with patch("agent_py_agent.agent.agent_core.tool_loop.response_decision._native_tool_use_active", return_value=True):
        write_decision = _native_truncated_write_decision(write_req, [write_call])
        assert write_decision is not None
        assert write_decision.action == "break"
        assert write_decision.response.runtime_reason == "cumulative_turn_repairs_exceeded"


# ---------------------------------------------------------------------------
# 5. MCPStdioClient cold start vs reconnect notice & process_registry retry
# ---------------------------------------------------------------------------
def test_mcp_client_cold_start_vs_reconnected_warning() -> None:
    cfg = MCPServerConfig(name="test_server", command="echo", args=["test"])
    client = MCPStdioClient(cfg)

    mock_transport = MagicMock()
    mock_transport.is_running.return_value = True
    mock_transport.binding.process.poll.return_value = None
    mock_transport.request.return_value = {
        "content": [{"type": "text", "text": "normal cold result"}],
        "isError": False,
    }

    # 5.1 Cold start: current_transport is None -> reconnected is False
    with patch.object(client, "reconnect", return_value=mock_transport):
        normal_res = client.call_tool("hello", {})
        assert normal_res["content"] == "normal cold result"
        assert "reconnected" not in normal_res
        assert "reconnect_warning" not in normal_res

    # 5.2 Mid-turn reconnect when transport is dead -> reconnected is True
    client._transport = mock_transport
    mock_transport.is_running.return_value = False
    new_mock_transport = MagicMock()
    new_mock_transport.is_running.return_value = True
    new_mock_transport.binding.process.poll.return_value = None
    new_mock_transport.request.return_value = {
        "content": [{"type": "text", "text": "reconnected result"}],
        "isError": False,
    }

    with patch.object(client, "reconnect", return_value=new_mock_transport):
        reconn_res = client.call_tool("hello", {})
        assert reconn_res["reconnected"] is True
        assert "MCP 服务连接中断后已自动重连" in reconn_res["reconnect_warning"]
        assert "reconnected result" in reconn_res["content"]
        assert "[提示：MCP 连接已自动重连，此前进程内会话态已重置]" in reconn_res["content"]


def test_terminate_process_tree_snapshot_retry() -> None:
    calls = []

    def mock_fresh_snapshot(pid):
        calls.append(len(calls))
        if len(calls) == 1:
            # First snapshot: incomplete due to transient child race
            return {1000: "host:1000 1", 1001: "host:1001 1"}, False
        # Second snapshot retry: complete
        return {1000: "host:1000 1"}, True

    with patch("agent_py_agent.agent.tooling.process_registry._IS_WINDOWS", False), \
         patch("agent_py_agent.agent.tooling.process_registry._fresh_process_tree_snapshot", side_effect=mock_fresh_snapshot), \
         patch("agent_py_agent.agent.tooling.process_registry._birth_token_matches", return_value=True), \
         patch("agent_py_agent.agent.tooling.process_registry._same_process", return_value=True), \
         patch("agent_py_agent.agent.tooling.process_registry._birth_tokens_match", return_value=True), \
         patch("agent_py_agent.agent.tooling.process_registry._signal_process_snapshot", return_value=set()), \
         patch("agent_py_agent.agent.tooling.process_registry._wait_process_snapshot_gone", return_value=True):

        receipt = terminate_process_tree(1000, None, expected_birth_token="host:1000 1")
        assert receipt.confirmed is True
        assert len(calls) == 2  # Proves fresh-snapshot retry loop executed
