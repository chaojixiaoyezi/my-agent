# LLM: 真实writer仅向pytest临时chunk写入，思考测试用固定单调时钟，不请求模型或修改服务。
# 模块用途: 验证Gateway事件顺序、thinking首块即时与合批脱敏，保留审批和遥测合同。
"""Gateway 流式 chunk 文件测试。"""

import json
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

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
from agent_py_agent.agent.gateway_parts.request_context import _gateway_compact_progress_callback
from agent_py_agent.agent.gateway_parts.stream_writer import BufferedChunkStreamWriter
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall


def test_compact_start_publishes_selected_model_window_before_progress(tmp_path: Path):
    path = gateway_chunk_path(_make_paths(tmp_path), "cross-model-compact")
    writer = BufferedChunkStreamWriter(path, rich_transcript=True)
    callback = _gateway_compact_progress_callback(writer)
    callback({
        "schema": "conversation_compaction_progress.v1", "phase": "started", "stage": "preparing",
        "percent": 5, "generation": 1, "source_kind": "conversation_transcript",
        "commit_authority": "conversation_thread", "operation_id": "transcript:cross-model",
        "before_tokens": 595_000, "trigger_tokens": 180_000, "context_window_tokens": 200_000,
    })
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["kind"] for row in rows] == ["context_usage_updated", "conversation_compaction_progress"]
    assert rows[0]["context_usage"]["context_window_tokens"] == 200_000
    assert rows[0]["context_usage"]["current_tokens"] == 595_000
    assert rows[0]["context_usage"]["compact_trigger_tokens"] == 180_000
    assert _gateway_compact_progress_callback(lambda text: None) is None


def test_compact_start_updates_canonical_usage_before_tui_poll(tmp_path):
    from agent_py_agent.agent.conversation.agent_activity import conversation_agent_activity
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime

    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "alice"})
    old = {"schema": "model_visible_context_usage.v1", "context_window_tokens": 1_000_000,
           "current_tokens": 164_000, "estimated": True}
    store.model_usage.update_context_usage(thread.thread_id, old, expected_compact_generation=0)
    original_bundle = store.context_bundle(thread.thread_id)
    runtime = TuiRuntime("compact-model-switch")
    runtime.update_background_activity(0, {"compact_count": 0, "context_usage": old})

    def receive_usage(usage):
        # 与真实状态轮询相同的持久读取，必须在收到实时帧前已经看见新容量。
        projection = conversation_agent_activity(SimpleNamespace(), store, thread.thread_id).to_dict()
        runtime.update_background_activity(0, projection)
        assert runtime.store.snapshot().status.context_usage.context_window_tokens == 200_000

    writer = SimpleNamespace(write_context_usage=receive_usage, write_conversation_compact_progress=lambda value: True)
    callback = _gateway_compact_progress_callback(writer, store=store, thread=thread)
    callback({"phase": "started", "before_tokens": 438_000, "trigger_tokens": 180_000, "context_window_tokens": 200_000})
    runtime.update_background_activity(0, conversation_agent_activity(SimpleNamespace(), store, thread.thread_id).to_dict())
    assert runtime.store.snapshot().status.context_usage.current_tokens == 438_000
    assert store.context_bundle(thread.thread_id) == original_bundle
    assert store.threads.load(thread.thread_id).updated_at == thread.updated_at


def test_compact_start_telemetry_failure_does_not_stop_progress():
    events = []

    def fail(*args, **kwargs):
        raise OSError("private path not for display")

    writer = SimpleNamespace(write_context_usage=lambda value: events.append("usage"),
                             write_conversation_compact_progress=lambda value: events.append("progress"))
    callback = _gateway_compact_progress_callback(writer,
        store=SimpleNamespace(model_usage=SimpleNamespace(update_context_usage=fail)),
        thread=SimpleNamespace(thread_id="test-thread", compact_generation=0))
    callback({"phase": "started", "before_tokens": 438_000, "context_window_tokens": 200_000})
    assert events == ["usage", "progress"]


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
    request = build_tool_approval_request(
        call,
        request_id=request_id,
        round_number=1,
        call_index=1,
        description="run_command(printf fixture)",
    )
    return replace(request, requested_at=time.time(), expires_at=time.time() + 300)


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


def test_writer_close_keeps_file_for_late_pollers(tmp_path):
    paths = _make_paths(tmp_path)
    chunk_path = gateway_chunk_path(paths, "test-req")
    chunk_path.write_text('{"t": 1, "text": "x"}\n', encoding="utf-8")

    BufferedChunkStreamWriter(chunk_path).close()

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


def test_turn_resumed_boundary_is_typed_content_free_and_after_buffered_output(tmp_path):
    paths = _make_paths(tmp_path)
    chunk_path = gateway_chunk_path(paths, "turn-resumed")
    writer = BufferedChunkStreamWriter(chunk_path, flush_interval_seconds=999, flush_chars=999)

    writer.write("续跑前还没刷出的进度")
    writer.write_turn_resumed(" gateway_safe_restart ")

    rows = [json.loads(line) for line in chunk_path.read_text(encoding="utf-8").splitlines()]
    assert [row["kind"] for row in rows] == ["runtime_progress", "turn_resumed"]
    assert rows[1]["cause"] == "gateway_safe_restart"
    assert set(rows[1]) == {"t", "kind", "cause"}


# Gateway 放行的 context_usage 数字字段（与 gateway_parts.stream_events 的白名单同一份）。
_CONTEXT_USAGE_PUBLIC_KEYS = ("schema", "estimated", "context_window_tokens", "compact_trigger_tokens", "current_tokens",
                             "prompt_tokens", "messages_tokens", "runtime_guidance_tokens", "tool_schema_tokens", "protocol")


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
    # 2026-10-08 状态行用的两个短标签：快照没给时为空串，仍然只放行字符串、不放行正文
    assert row["context_usage"] == {**{key: usage[key] for key in _CONTEXT_USAGE_PUBLIC_KEYS},
                                    "model_name": "", "reasoning_level": ""}
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
        "operation_id": "transcript:test-operation",
        "source_kind": "conversation_transcript",
        "commit_authority": "conversation_thread",
        "before_tokens": 118_400,
        "after_tokens": 0,
        "trigger_tokens": 115_200,
        "source_messages": 80,
        "error_code": "COMPACT_MODEL_EMPTY_RESPONSE",
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
            "operation_id",
            "source_kind",
            "commit_authority",
            "before_tokens",
            "after_tokens",
            "trigger_tokens",
            "source_messages",
            "error_code",
        )
    }
    assert "summary" not in row["compact_progress"]
    assert "prompt" not in row["compact_progress"]


def test_conversation_compact_superseded_crosses_gateway_as_typed_terminal(tmp_path):
    """未采用候选通过同一无正文 schema 结束动画，不会被 Gateway 丢成悬挂块。"""
    paths = _make_paths(tmp_path)
    chunk_path = gateway_chunk_path(paths, "compact-superseded")
    writer = BufferedChunkStreamWriter(chunk_path, rich_transcript=True)
    value = {
        "schema": "conversation_compaction_progress.v1",
        "phase": "superseded",
        "stage": "candidate_discarded",
        "percent": 0,
        "generation": 4,
        "operation_id": "live-tool:test-operation",
        "source_kind": "active_turn_tool_archive",
        "commit_authority": "conversation_thread",
        "before_tokens": 118_400,
        "after_tokens": 109_000,
        "trigger_tokens": 115_200,
        "source_messages": 80,
    }

    assert writer.write_conversation_compact_progress(value) is True

    row = json.loads(chunk_path.read_text(encoding="utf-8").strip())
    assert row["kind"] == "conversation_compaction_progress"
    assert row["compact_progress"] == value


def test_noninteractive_gateway_permission_fails_closed_without_waiting(tmp_path):
    paths = _make_paths(tmp_path)
    chunk_path = gateway_chunk_path(paths, "permission-disabled")
    writer = BufferedChunkStreamWriter(chunk_path, interactive_approvals=False)
    request = _permission_request("permission-disabled")

    decision = writer.request_permission(request.to_dict())

    assert decision["permission_id"] == request.permission_id
    assert decision["decision"] == "denied"
    assert decision["reason_code"] == "APPROVAL_NONINTERACTIVE"
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


# LLM: 替换此模块的时钟对象而非进程全局time，真实JSONL及两个sink沿原发布函数执行。
# 函数用途: 给thinking阈值测试提供确定时间和独立writer。
@pytest.fixture
def thinking_writer(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import stream_writer

    clock = [100.0]
    monkeypatch.setattr(stream_writer, "time", SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 1.0))
    writer = BufferedChunkStreamWriter(tmp_path / "thinking.jsonl", rich_transcript=True, delivery_channel="feishu")
    return writer, clock


# LLM: 观察真实落盘chunk，不把write调用次数当作已可见事件。
# 函数用途: 读回本writer已经发布的结构化事件。
def _thinking_rows(writer):
    if not writer.chunk_path.exists():
        return []
    return [json.loads(line) for line in writer.chunk_path.read_text(encoding="utf-8").splitlines()]


# LLM: 通过真实JSONL观察空增量不占首块及首个非空增量即时发布。
# 函数用途: 保持TUI首token响应合同。
def test_w21_baseline_single_thinking_delta_is_immediately_visible(thinking_writer):
    writer, _clock = thinking_writer
    writer.write_thinking_delta("")
    assert _thinking_rows(writer) == []
    writer.write_thinking_delta("首块")
    assert [row["text"] for row in _thinking_rows(writer)] == ["首块"]


# LLM: 固定时钟下只让字符阈值触发，close不得重放已排空缓冲。
# 函数用途: 验证后续255字等待、第256字合成一条。
def test_w21_subsequent_thinking_deltas_batch_at_256_characters(thinking_writer):
    writer, _clock = thinking_writer
    writer.write_thinking_delta("首块")
    for _ in range(255):
        writer.write_thinking_delta("字")
    assert [row["text"] for row in _thinking_rows(writer)] == ["首块"]
    writer.write_thinking_delta("末")
    assert [row["text"] for row in _thinking_rows(writer)] == ["首块", "字" * 255 + "末"]
    writer.close()
    assert len(_thinking_rows(writer)) == 2


# LLM: 单调时钟边界可复现，未到0.25秒不发，末尾换行复用原刷新公式。
# 函数用途: 检查时间阈值和换行触发的思考合批。
def test_w21_thinking_interval_flushes_on_next_delta_at_quarter_second(thinking_writer):
    writer, clock = thinking_writer
    writer.write_thinking_delta("首块")
    writer.write_thinking_delta("a")
    clock[0] = 100.249
    writer.write_thinking_delta("b")
    assert len(_thinking_rows(writer)) == 1
    clock[0] = 100.25
    writer.write_thinking_delta("c")
    assert [row["text"] for row in _thinking_rows(writer)] == ["首块", "abc"]
    writer.write_thinking_delta("换行\n")
    assert _thinking_rows(writer)[-1]["text"] == "换行\n"


# LLM: 第一块独立即时，后续raw片段合并后净化，外部通道不应发布完整宿主路径或原标识。
# 函数用途: 验证同一缓冲批次跨块拆开的标识和路径仍被脱敏。
def test_w21_buffered_thinking_redacts_identifiers_and_paths_split_across_chunks(thinking_writer):
    writer, _clock = thinking_writer
    writer.set_identifier_redactions((("om_private_thread", "当前会话"),))
    writer.write_thinking_delta("分析：")
    for text in ("om_pri", "vate_thread 文件 /Us", "ers/fixture/work/report.txt"):
        writer.write_thinking_delta(text)
    assert len(_thinking_rows(writer)) == 1
    writer.close()
    text = _thinking_rows(writer)[-1]["text"]
    assert "om_private_thread" not in text and "/Users/" not in text
    assert "当前会话" in text and "report.txt" in text


# LLM: 任一非thinking边界不得越过待发增量，完整思考和提交还须重置段首额度。
# 函数用途: 检查各收口/事件接线先排空思考且新段首块即时。
@pytest.mark.parametrize("boundary", ["flush", "thinking", "progress", "commit", "event", "close"])
def test_w21_pending_thinking_flushes_before_every_boundary(thinking_writer, boundary):
    writer, _clock = thinking_writer
    writer.write_thinking_delta("首块")
    writer.write_thinking_delta("缓冲尾部")
    assert len(_thinking_rows(writer)) == 1
    if boundary == "thinking":
        writer.write_thinking("完整思考")
    elif boundary == "progress":
        writer.write_progress({"phase": "started", "round": 1}, "工具启动")
    elif boundary == "commit":
        writer.commit_model_response({})
    elif boundary == "event":
        writer._write_event({"kind": "host_notice", "text": "宿主边界"})
    else:
        getattr(writer, boundary)()
    rows = _thinking_rows(writer)
    assert [row["kind"] for row in rows[:2]] == ["thinking_delta", "thinking_delta"]
    assert rows[1]["text"] == "缓冲尾部"
    if boundary in {"thinking", "progress", "event"}:
        assert len(rows) == 3 and rows[-1]["kind"] != "thinking_delta"
    if boundary in {"thinking", "commit"}:
        writer.write_thinking_delta("新段首块")
        assert _thinking_rows(writer)[-1]["text"] == "新段首块"


# LLM: 非rich能力门在入缓冲前生效，不能等close才发布隐藏思考。
# 函数用途: 验证普通客户端仍完全收不到thinking事件。
def test_w21_plain_writer_never_buffers_or_emits_thinking(thinking_writer):
    writer, _clock = thinking_writer
    writer.rich_transcript = False
    for _ in range(300):
        writer.write_thinking_delta("隐藏思考")
    writer.close()
    assert _thinking_rows(writer) == []


# LLM: 非终态flush/事件边界须保留跨块敏感前缀，且完整敏感词后紧跟的新前缀同样不能提前外泄。
# 函数用途: 验证跨块敏感会话ID在中间flush、穿插事件及最终close下均完全脱敏。
def test_w21_cross_chunk_redaction_holds_partial_prefix_across_intermediate_flush(thinking_writer):
    from agent_py_agent.agent.gateway_parts.stream_events import drain_thinking_delta_text

    writer, _clock = thinking_writer
    writer.delivery_channel = "feishu"
    writer.set_identifier_redactions((("om_secret_id", "当前会话"), ("om_secret_id_long", "长会话")))
    flushed = drain_thinking_delta_text(writer, "已见 om_secret_id 与 om_sec", final=False)
    assert "om_secret_id" not in flushed and "当前会话" in flushed
    assert writer._thinking_buffer == ["om_sec"]
    writer._thinking_buffer.clear()
    writer._thinking_buffer_chars = 0
    writer.write_thinking_delta("首块立即发出")
    writer.write_thinking_delta("检查 om_sec")
    writer.flush()
    writer.write_thinking_delta("ret_id 与 om_secret_")
    writer._write_event({"kind": "host_notice", "text": "穿插事件"})
    writer.write_thinking_delta("id_long 完毕")
    writer.close()
    full_thinking = "".join(row["text"] for row in _thinking_rows(writer) if row["kind"] == "thinking_delta")
    assert "om_secret_id" not in full_thinking
    assert "当前会话" in full_thinking and "长会话" in full_thinking


# LLM: 思考摘取后、净化完成前到达的跨线程非thinking事件必须延迟到本批thinking落盘之后，且不得触发二次重复发布。
# 函数用途: 验证思考净化窗口内并发普通事件的发布顺序与单次排空语义。
def test_w21_thinking_flush_drain_orders_concurrent_notice_without_duplication(thinking_writer, monkeypatch):
    import threading

    from agent_py_agent.agent.gateway_parts import stream_writer as sw

    writer, _clock = thinking_writer
    writer.write_thinking_delta("first|")
    writer.write_thinking_delta("pending|")
    entered, release = threading.Event(), threading.Event()
    original_drain = sw.drain_thinking_delta_text

    def paused_drain(w, raw, **kwargs):
        if raw == "pending|":
            entered.set()
            assert release.wait(3)
        return original_drain(w, raw, **kwargs)

    monkeypatch.setattr(sw, "drain_thinking_delta_text", paused_drain)
    worker = threading.Thread(target=writer.flush)
    worker.start()
    try:
        assert entered.wait(3)
        writer._write_event({"kind": "host_notice", "text": "notice"})
    finally:
        release.set()
    worker.join(3)
    writer.close()
    assert [row.get("text") for row in _thinking_rows(writer)] == ["first|", "pending|", "notice"]


# LLM: 在途思考发布期间新到的tail必须在后到notice之前发出、close不得丢失在途新尾，且延迟发布异常需复原未发项与tid。
# 函数用途: 覆盖在途flush期间新尾+close、新尾+notice顺序及延迟发布异常恢复。
def test_w21_inflight_thinking_tail_ordering_and_exception_recovery(thinking_writer, monkeypatch):
    import pytest

    from agent_py_agent.agent.gateway_parts import stream_writer as sw

    writer, _clock = thinking_writer
    writer.write_thinking_delta("first|")
    writer.write_thinking_delta("pending|")
    entered, release = threading.Event(), threading.Event()
    original_write = sw.write_chunk_event

    def paused_write(path, payload, **kwargs):
        if payload.get("kind") == "thinking_delta" and payload.get("text") == "pending|":
            entered.set()
            assert release.wait(3)
        return original_write(path, payload, **kwargs)

    monkeypatch.setattr(sw, "write_chunk_event", paused_write)
    worker = threading.Thread(target=writer.flush)
    worker.start()
    try:
        assert entered.wait(3)
        writer.write_thinking_delta("tail|")
        writer._write_event({"kind": "host_notice", "text": "notice|"})
    finally:
        release.set()
    worker.join(3)
    writer._deferred_after_thinking.extend(
        [({"kind": "host_notice", "text": "boom"}, None), ({"kind": "host_notice", "text": "survivor"}, None)]
    )
    writer._thinking_publishing_tid = threading.get_ident()

    def failing_publish(w, event, **kwargs):
        if event.get("text") == "boom":
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        sw.complete_thinking_flush(writer, failing_publish)
    assert writer._thinking_publishing_tid is None
    assert [item[0]["text"] for item in writer._deferred_after_thinking] == ["survivor"]
    writer.close()
    assert not writer._deferred_after_thinking
    assert [row.get("text") for row in _thinking_rows(writer)] == [
        "first|",
        "pending|",
        "tail|",
        "notice|",
        "survivor",
    ]
    writer._thinking_publishing_tid = 999999999
    assert sw.wait_thinking_flush_idle(writer, lambda *_a, **_kw: None, timeout=0.01) is False
    assert writer.prepare_display_history() == {}
    assert writer._defer_prepare_final is False
    writer.close()
    assert writer._defer_close_sinks is False
    sw.complete_thinking_flush(writer, lambda *_a, **_kw: None)
    assert writer._thinking_publishing_tid == 999999999
    writer._thinking_publishing_tid = None

    order: list[str] = []
    prep_entered, prep_release = threading.Event(), threading.Event()
    writer.transcript_sink = SimpleNamespace(
        prepare_final=lambda: (prep_entered.set(), prep_release.wait(3), order.append("prepare_final"), {})[-1],
        close=lambda: order.append("close"),
    )
    writer._thinking_publishing_tid = threading.get_ident()
    writer._defer_prepare_final = True
    pub_thread = threading.Thread(
        target=lambda: (
            setattr(writer, "_thinking_publishing_tid", threading.get_ident()),
            sw.complete_thinking_flush(writer, lambda *_a, **_kw: None),
        )
    )
    pub_thread.start()
    assert prep_entered.wait(3)
    writer.close()
    assert order == []
    prep_release.set()
    pub_thread.join(3)
    assert order == ["prepare_final", "close"]
