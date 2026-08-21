"""Gateway 流式 chunk 文件测试。"""

import json
import threading
import time
from pathlib import Path

from agent_py_agent.agent.contracts.tool_approval import (
    ToolApprovalDecision,
    build_tool_approval_request,
)
from agent_py_agent.agent.gateway_parts.paths import (
    GatewayPaths,
    gateway_chunk_path,
    gateway_chunk_path_candidates,
)
from agent_py_agent.agent.gateway_parts.permission_bridge import (
    write_gateway_permission_decision,
)
from agent_py_agent.agent.gateway_parts.request_execution import (
    BufferedChunkStreamWriter,
    close_chunk_stream,
)
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall


def _make_paths(tmp_path: Path) -> GatewayPaths:
    processing = tmp_path / "requests" / "processing"
    processing.mkdir(parents=True, exist_ok=True)
    return GatewayPaths(
        root=tmp_path,
        pid=tmp_path / "gateway.pid",
        adapter_pid=tmp_path / "adapter.pid",
        state=tmp_path / "gateway_state.json",
        heartbeat=tmp_path / "gateway_heartbeat.json",
        stop_request=tmp_path / "gateway_stop.request",
        log=tmp_path / "gateway.log",
        inbox=tmp_path / "requests" / "pending",
        processing=processing,
        done=tmp_path / "requests" / "done",
        failed=tmp_path / "requests" / "failed",
        responses=tmp_path / "responses",
        history=tmp_path / "gateway_requests.jsonl",
    )


def _permission_request(request_id: str):
    call = ToolCall(
        call_id="gateway-permission-call",
        tool_name="run_command",
        arguments={"command": "printf fixture"},
        source_protocol="native",
        schema_hash="sha256:gateway-permission",
        run_id="gateway-permission-run",
        turn_id="gateway-permission-turn",
        attempt_id="gateway-permission-attempt",
    )
    return build_tool_approval_request(
        call,
        request_id=request_id,
        round_number=1,
        call_index=1,
        description="run_command(printf fixture)",
    )


def test_gateway_chunk_path_is_in_processing(tmp_path):
    paths = _make_paths(tmp_path)
    chunk_path = gateway_chunk_path(paths, "req-123")
    assert chunk_path == paths.processing / "req-123.chunks.jsonl"


def test_gateway_chunk_path_candidates_include_archives(tmp_path):
    paths = _make_paths(tmp_path)
    chunk_path = gateway_chunk_path(paths, "req-123")

    assert gateway_chunk_path_candidates(chunk_path) == (
        paths.processing / "req-123.chunks.jsonl",
        paths.done / "req-123.chunks.jsonl",
        paths.failed / "req-123.chunks.jsonl",
    )


def test_chunk_file_write_and_read(tmp_path):
    paths = _make_paths(tmp_path)
    chunk_path = gateway_chunk_path(paths, "test-req")

    # Simulate daemon writing chunks
    with open(chunk_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": time.time(), "text": "你好"}, ensure_ascii=False) + "\n")
        f.write(json.dumps({"t": time.time(), "text": "世界"}, ensure_ascii=False) + "\n")

    # Simulate CLI reading chunks
    lines = chunk_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    obj0 = json.loads(lines[0])
    obj1 = json.loads(lines[1])
    assert obj0["text"] == "你好"
    assert obj1["text"] == "世界"


def test_chunk_file_cleanup(tmp_path):
    paths = _make_paths(tmp_path)
    chunk_path = gateway_chunk_path(paths, "test-req")

    # Write some chunks
    with open(chunk_path, "a", encoding="utf-8") as f:
        f.write('{"t": 1, "text": "x"}\n')

    assert chunk_path.exists()
    chunk_path.unlink(missing_ok=True)
    assert not chunk_path.exists()


def test_close_chunk_stream_keeps_file_for_late_pollers(tmp_path):
    paths = _make_paths(tmp_path)
    chunk_path = gateway_chunk_path(paths, "test-req")
    chunk_path.write_text('{"t": 1, "text": "x"}\n', encoding="utf-8")

    close_chunk_stream(chunk_path)

    assert chunk_path.exists()


def test_buffered_chunk_stream_writer_coalesces_small_deltas(tmp_path):
    paths = _make_paths(tmp_path)
    chunk_path = gateway_chunk_path(paths, "test-req")
    writer = BufferedChunkStreamWriter(chunk_path, flush_interval_seconds=999, flush_chars=10)

    writer.write("你")
    writer.write("好")
    assert not chunk_path.exists()
    writer.write("，世界很大")
    writer.close()

    lines = chunk_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["text"] == "你好，世界很大"


def test_buffered_chunk_stream_writer_flushes_tail_on_close(tmp_path):
    paths = _make_paths(tmp_path)
    chunk_path = gateway_chunk_path(paths, "test-req")
    writer = BufferedChunkStreamWriter(chunk_path, flush_interval_seconds=999, flush_chars=999)

    writer.write("最后一点")
    writer.close()

    lines = chunk_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["text"] == "最后一点"


def test_compact_boundary_is_typed_and_does_not_copy_summary(tmp_path):
    paths = _make_paths(tmp_path)
    chunk_path = gateway_chunk_path(paths, "compact-boundary")
    writer = BufferedChunkStreamWriter(chunk_path)

    writer.write_compact_boundary(3)

    row = json.loads(chunk_path.read_text(encoding="utf-8").strip())
    assert row["kind"] == "conversation_compacted"
    assert row["compact_generation"] == 3
    assert set(row) == {"t", "kind", "compact_generation"}


def test_context_usage_is_rich_only_and_whitelists_numeric_projection(tmp_path):
    paths = _make_paths(tmp_path)
    disabled_path = gateway_chunk_path(paths, "context-disabled")
    disabled = BufferedChunkStreamWriter(disabled_path, rich_transcript=False)
    usage = {
        "schema": "model_visible_context_usage.v1",
        "estimated": True,
        "context_window_tokens": 128_000,
        "compact_trigger_tokens": 115_200,
        "current_tokens": 31_400,
        "prompt_tokens": 8_000,
        "messages_tokens": 6_000,
        "runtime_guidance_tokens": 400,
        "tool_schema_tokens": 17_000,
        "protocol": "native",
        "prompt": "must not escape",
        "tools": [{"secret": "must not escape"}],
    }

    assert disabled.write_context_usage(usage) is False
    assert not disabled_path.exists()

    enabled_path = gateway_chunk_path(paths, "context-enabled")
    enabled = BufferedChunkStreamWriter(enabled_path, rich_transcript=True)
    assert enabled.write_context_usage(usage) is True
    row = json.loads(enabled_path.read_text(encoding="utf-8").strip())

    assert row["kind"] == "context_usage_updated"
    assert row["context_usage"] == {
        key: usage[key]
        for key in (
            "schema",
            "estimated",
            "context_window_tokens",
            "compact_trigger_tokens",
            "current_tokens",
            "prompt_tokens",
            "messages_tokens",
            "runtime_guidance_tokens",
            "tool_schema_tokens",
            "protocol",
        )
    }
    assert "prompt" not in row["context_usage"]
    assert "tools" not in row["context_usage"]


def test_context_window_compaction_is_typed_rich_only_and_content_free(tmp_path):
    paths = _make_paths(tmp_path)
    disabled_path = gateway_chunk_path(paths, "window-disabled")
    disabled = BufferedChunkStreamWriter(disabled_path, rich_transcript=False)
    value = {
        "schema": "model_visible_context_compaction.v1",
        "generation": 2,
        "before_tokens": 118_400,
        "after_tokens": 31_200,
        "trigger_tokens": 115_200,
        "dropped_pairs": 84,
        "preserved_pairs": 12,
        "summary": "must not escape",
    }

    assert disabled.write_context_compaction(value) is False
    assert not disabled_path.exists()

    enabled_path = gateway_chunk_path(paths, "window-enabled")
    enabled = BufferedChunkStreamWriter(enabled_path, rich_transcript=True)
    assert enabled.write_context_compaction(value) is True
    row = json.loads(enabled_path.read_text(encoding="utf-8").strip())

    assert row == {
        "t": row["t"],
        "kind": "context_window_compacted",
        "context_compaction": {
            key: value[key]
            for key in (
                "schema",
                "generation",
                "before_tokens",
                "after_tokens",
                "trigger_tokens",
                "dropped_pairs",
                "preserved_pairs",
            )
        },
    }
    assert "summary" not in row["context_compaction"]


def test_conversation_compact_progress_is_rich_only_and_content_free(tmp_path):
    paths = _make_paths(tmp_path)
    value = {
        "schema": "conversation_compaction_progress.v1",
        "phase": "progress",
        "stage": "summarizing",
        "percent": 35,
        "generation": 4,
        "before_tokens": 118_400,
        "after_tokens": 0,
        "trigger_tokens": 115_200,
        "source_messages": 80,
        "summary": "must not escape",
        "prompt": "must not escape",
    }
    disabled_path = gateway_chunk_path(paths, "compact-progress-disabled")
    disabled = BufferedChunkStreamWriter(disabled_path, rich_transcript=False)
    assert disabled.write_conversation_compact_progress(value) is False
    assert not disabled_path.exists()

    enabled_path = gateway_chunk_path(paths, "compact-progress-enabled")
    enabled = BufferedChunkStreamWriter(enabled_path, rich_transcript=True)
    assert enabled.write_conversation_compact_progress(value) is True
    row = json.loads(enabled_path.read_text(encoding="utf-8").strip())

    assert row["kind"] == "conversation_compaction_progress"
    assert row["compact_progress"] == {
        key: value[key]
        for key in (
            "schema",
            "phase",
            "stage",
            "percent",
            "generation",
            "before_tokens",
            "after_tokens",
            "trigger_tokens",
            "source_messages",
        )
    }
    assert "summary" not in row["compact_progress"]
    assert "prompt" not in row["compact_progress"]


def test_noninteractive_gateway_permission_fails_closed_without_waiting(tmp_path):
    paths = _make_paths(tmp_path)
    chunk_path = gateway_chunk_path(paths, "permission-disabled")
    writer = BufferedChunkStreamWriter(chunk_path, interactive_approvals=False)
    request = _permission_request("permission-disabled")

    decision = writer.request_permission(request.to_dict())

    assert decision["permission_id"] == request.permission_id
    assert decision["decision"] == "unavailable"
    assert not chunk_path.exists()


def test_interactive_gateway_permission_waits_for_exact_decision_file(tmp_path):
    paths = _make_paths(tmp_path)
    chunk_path = gateway_chunk_path(paths, "permission-enabled")
    writer = BufferedChunkStreamWriter(chunk_path, interactive_approvals=True)
    request = _permission_request("permission-enabled")
    result: dict[str, object] = {}

    def wait_for_decision() -> None:
        result.update(writer.request_permission(request.to_dict()))

    thread = threading.Thread(target=wait_for_decision)
    thread.start()
    deadline = time.monotonic() + 1.0
    rows: list[dict[str, object]] = []
    while time.monotonic() < deadline:
        if chunk_path.exists():
            rows = [
                json.loads(line)
                for line in chunk_path.read_text(encoding="utf-8").splitlines()
            ]
            if any(row.get("kind") == "permission_requested" for row in rows):
                break
        time.sleep(0.01)

    write_gateway_permission_decision(
        chunk_path,
        request,
        ToolApprovalDecision(
            request.permission_id,
            "approved",
            "only inspect the target",
        ),
    )
    thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert result["decision"] == "approved"
    assert result["feedback"] == "only inspect the target"
    rows = [
        json.loads(line)
        for line in chunk_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [row["kind"] for row in rows] == [
        "permission_requested",
        "permission_resolved",
    ]
