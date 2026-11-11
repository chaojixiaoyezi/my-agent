"""W2 正文候选生命周期、跨块脱敏、完整安全输入及工作流订阅合同。"""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.tool_loop.round_execution import (
    ToolProgressEvent,
    _payload_progress_detail,
    _structured_tool_progress,
)
from agent_py_agent.agent.common.log_redaction import redact_sensitive_text
from agent_py_agent.agent.conversation.display_archive import read_display_archive_page
from agent_py_agent.agent.gateway_parts.http_handlers import _read_public_progress_events
from agent_py_agent.agent.gateway_parts.progress_text_delta import TextDeltaBuffer
from agent_py_agent.agent.gateway_parts.stream_writer import BufferedChunkStreamWriter
from agent_py_agent.agent.tooling.runtime_contracts import ToolResult, ToolSuccessFacts
from agent_py_agent.tests.test_committed_model_messages import _payload
from agent_py_agent.tests.test_display_archive import _owner
from agent_py_agent.tests.test_tool_round_execution import _canonical_calls


def _stream(tmp_path, *, level="stream", holdback=8):
    writer = BufferedChunkStreamWriter(tmp_path / "text.chunks.jsonl", text_delta_holdback_chars=holdback)
    writer.set_verbose_level(level)
    return writer


def _events(writer):
    return _read_public_progress_events(writer.chunk_path, 0, include=frozenset({"assistant_text_delta"}))[0]


@pytest.mark.parametrize("key", ["Authorization", "Proxy-Authorization", "Cookie", "Set-Cookie"])
def test_review_unfinished_header_key_never_loses_redaction_context(key):
    buffer = TextDeltaBuffer()
    buffer.begin("header-call")
    first = '{"' + key + '"' + " " * 300
    last = ': "Bearer SyntheticHeaderCredential"}\n' + "正常后续。" * 100
    rows = buffer.append(first, model_call_id="header-call")
    rows += buffer.append(last, model_call_id="header-call")
    rows += buffer.finish("header-call")
    public = "".join(row["text"] for row in rows if row["kind"] == "assistant_text_delta")
    assert "SyntheticHeaderCredential" not in public
    assert public == redact_sensitive_text(first + last)


@pytest.mark.parametrize("opening,closing", [
    ("[RUN_INTERNAL] ", ""), ("<tool_call>\n", "\n</tool_call>"),
    ("[TOOL_CALL] ", "\n[/TOOL_CALL]"),
])
def test_review_stream_preserves_whole_call_protocol_suppression(tmp_path, opening, closing):
    writer = _stream(tmp_path)
    callback = writer.bind_model_call("call-current")
    text = opening + "不能公开的内部调用参数。" * 80 + closing
    for offset in range(0, len(text), 7):
        callback(text[offset:offset + 7])
    _commit(writer, "")
    assert not any(row["kind"] == "assistant_text_delta" for row in _events(writer))


def test_review_close_marks_writer_closed_before_candidate_reset(tmp_path, monkeypatch):
    writer = _stream(tmp_path)
    writer.bind_model_call("call-current")("原候选。" * 30)
    reset = BufferedChunkStreamWriter.reset_model_text
    observed = []

    def observe_gap(self, reason, **kwargs):
        reset(self, reason, **kwargs)
        if reason == "cancel":
            # 固定旧关闭代码中 reset 返回到关闭标记之间的交错观察点，无 sleep 或线程调度假设。
            observed.append(bool(getattr(self, "_liveness_closed", False)))
            self.bind_model_call("late-call")("关闭过程中新来的候选。" * 40)

    monkeypatch.setattr(BufferedChunkStreamWriter, "reset_model_text", observe_gap)
    writer.close()
    assert observed == [True]
    assert _events(writer)[-1]["kind"] == "assistant_text_reset"


@pytest.mark.parametrize("patch", [{"status": []}, {"duration_ms": True}, {"seq": -1}, {"next_step_ids": [1]}])
def test_workflow_invalid_schema_is_ignored(tmp_path, patch):
    writer = _stream(tmp_path)
    writer.write_workflow_step_progress({"workflow_run_id": "run", "workflow_id": "workflow", "step_id": "step",
        "name": "步骤", "status": "running", "duration_ms": 0, "description": "", "next_step_ids": [], "seq": 0,
        **patch})
    assert not writer.chunk_path.exists()


def _commit(writer, text, call_id="call-current", boundary="terminal"):
    writer.commit_model_response(_payload([{"text": text}], call_id=call_id, boundary=boundary, final=text))


@pytest.mark.parametrize("tag", ["memory-context", "read_file", "function"])
@pytest.mark.parametrize("width", [1, 37])
def test_review_unfinished_xml_attributes_never_reach_any_delta(tmp_path, tag, width):
    from agent_py_agent.agent.conversation.channels import project_user_reply

    writer = _stream(tmp_path)
    callback = writer.bind_model_call("call-current")
    first = '公开前缀。\n<' + tag + ' note="' + "SyntheticInternalArgument " * 80
    last = '">内部记忆正文</' + tag + '>\n公开后缀。' + "正常后续。" * 100
    callback(first)
    assert "SyntheticInternalArgument" not in json.dumps(_events(writer))
    for offset in range(0, len(last), width):
        callback(last[offset:offset + width])
        assert "SyntheticInternalArgument" not in json.dumps(_events(writer))
    _commit(writer, first + last)
    rows = _events(writer)
    assert "SyntheticInternalArgument" not in writer.chunk_path.read_text()
    rendered = ""
    for row in rows:
        if row["kind"] == "assistant_text_reset":
            rendered = ""
        elif row["kind"] == "assistant_text_delta":
            rendered += row["text"]
    assert rendered == project_user_reply(first + last).content


@pytest.mark.parametrize("text", ["", "[RUN_INTERNAL] 内部终稿"])
@pytest.mark.parametrize("boundary", ["terminal", "tools"])
def test_review_empty_public_commit_closes_published_candidate(tmp_path, text, boundary):
    writer = _stream(tmp_path)
    callback = writer.bind_model_call("call-current")
    callback("待撤销候选。" * 80)
    assert any(row["kind"] == "assistant_text_delta" for row in _events(writer))
    _commit(writer, text, boundary=boundary)
    rows = _events(writer)
    assert "call-current" in writer._committed_model_calls
    assert writer._text_delta.model_call_id == ""
    if boundary == "terminal":
        assert rows[-2]["kind"] == "assistant_text_reset"
        assert rows[-1] == {"kind": "assistant_text_done", "model_call_id": "call-current", "seq": 0}
    else:
        assert rows[-1]["kind"] == "assistant_text_reset" and rows[-1]["reason"] == "tool_call"
    snapshot = writer.chunk_path.read_bytes()
    _commit(writer, text, boundary=boundary)
    callback("提交后不能复活的迟到正文。" * 40)
    assert writer.chunk_path.read_bytes() == snapshot


@pytest.mark.parametrize("patch", [
    {"schema": "invalid"}, {"boundary": []}, {"messages": {}}, {"source": {}}, {"source": {"call_id": []}},
])
def test_review_invalid_empty_commit_cannot_close_candidate(tmp_path, patch):
    writer = _stream(tmp_path)
    writer.bind_model_call("call-current")("仍有效的候选。" * 80)
    snapshot = writer.chunk_path.read_bytes()
    writer.commit_model_response({**_payload([{"text": ""}], call_id="call-current", final=""), **patch})
    assert writer.chunk_path.read_bytes() == snapshot
    assert writer._text_delta.model_call_id == "call-current" and not writer._committed_model_calls


def test_review_character_callbacks_batch_projection_work(monkeypatch):
    from agent_py_agent.agent.gateway_parts import progress_text_delta

    projected, scanned = [], []
    safe_cut = progress_text_delta._safe_cut

    def counted_cut(raw, limit, identifiers):
        scanned.append(len(raw))
        return safe_cut(raw, limit, identifiers)

    def counted_project(text):
        projected.append(len(text))
        return text

    monkeypatch.setattr(progress_text_delta, "_safe_cut", counted_cut)
    buffer = TextDeltaBuffer(project=counted_project)
    buffer.begin("counted-call")
    text, rows = "字" * 2048, []
    for char in text:
        rows.extend(buffer.append(char, model_call_id="counted-call"))
    rows.extend(buffer.finish("counted-call"))
    assert "".join(row["text"] for row in rows if row["kind"] == "assistant_text_delta") == text
    assert len(scanned) <= len(text) // 64 + 2
    assert sum(scanned) <= 64 * len(text) and sum(projected) <= 64 * len(text)


def test_projection_budget_stops_scanning_and_resets_for_next_call():
    work = []

    def project(text):
        work.append(len(text))
        return text

    buffer = TextDeltaBuffer(holdback_chars=8, project=project, max_projection_chars=4096)
    buffer.begin("budget-call")
    rows = []
    for _ in range(2048):
        rows.extend(buffer.append("字", model_call_id="budget-call"))
    assert rows[-1]["kind"] == "assistant_text_reset" and rows[-1]["reason"] == "superseded"
    used = tuple(work)
    assert buffer.append("不能在预算耗尽后继续投影", model_call_id="budget-call") == []
    assert buffer.finish("budget-call", text="权威终稿。" * 100)[-1]["kind"] == "assistant_text_done"
    assert tuple(work) == used and sum(work) <= 4096
    buffer.begin("next-call")
    assert buffer.append("新候选。" * 20, model_call_id="next-call")
    assert buffer.finish("next-call", text="新候选。" * 20)[-1]["kind"] == "assistant_text_done"


def test_acceptance_same_call_delta_then_terminal(tmp_path):
    writer = _stream(tmp_path)
    callback = writer.bind_model_call("call-current")
    text = "这是模型产生的公开正文。" * 30
    callback(text[:100])
    # 首次发布发生在 commit 之前，不能以终稿人工分字冒充 token 流。
    before = _events(writer)
    assert before[0] == {"kind": "process_settings", "verbose_level": "stream"}
    assert before[1:] and all(row["kind"] == "assistant_text_delta" for row in before[1:])
    callback(text[100:])
    _commit(writer, text)
    rows = _events(writer)
    deltas = [row for row in rows if row["kind"] == "assistant_text_delta"]
    assert "".join(row["text"] for row in deltas) == text
    assert [row["seq"] for row in deltas] == list(range(len(deltas)))
    assert rows[-1] == {"kind": "assistant_text_done", "model_call_id": "call-current", "seq": len(deltas) - 1}
    _commit(writer, text)
    assert _events(writer) == rows


def test_acceptance_retry_and_cancel_correction(tmp_path):
    writer = _stream(tmp_path)
    callback = writer.bind_model_call("call-current")
    callback("重试前正文。" * 20)
    writer.write_provider_retry(scope="transport", attempt=1, total=2, delay_seconds=0, error_type="TimeoutError")
    callback("重试后正文。" * 20)
    rows = _events(writer)
    reset_index = next(index for index, row in enumerate(rows) if row["kind"] == "assistant_text_reset")
    assert rows[reset_index]["reason"] == "retry"
    assert rows[reset_index + 1]["seq"] == 0
    writer.close()
    assert _events(writer)[-1]["reason"] == "cancel"
    snapshot = writer.chunk_path.read_bytes()
    callback("关闭后迟到正文。" * 50)
    assert writer.chunk_path.read_bytes() == snapshot


@pytest.mark.parametrize("secret", ["sk-" + "SyntheticCredential" * 40,
    "https://reader:" + "SyntheticPassword" * 45 + "@example.invalid/report",
    'password="' + "quoted synthetic credential " * 25 + '"',
    "Authorization: Bearer " + "SyntheticBearer" * 50,
    "-----BEGIN PRIVATE KEY-----\n" + "SYNTHETICPRIVATE" * 40 + "\n-----END PRIVATE KEY-----"])
@pytest.mark.parametrize("width", [1, 37, 257])
def test_acceptance_cross_chunk_credentials_never_escape(secret, width):
    buffer = TextDeltaBuffer()
    buffer.begin("call-safe")
    text = "正常开始。 " + secret + "\n" + "正常后续。" * 100
    rows = []
    for offset in range(0, len(text), width):
        rows.extend(buffer.append(text[offset:offset + width], model_call_id="call-safe"))
    rows.extend(buffer.finish("call-safe"))
    public = "".join(row["text"] for row in rows if row["kind"] == "assistant_text_delta")
    assert public == redact_sensitive_text(text)
    assert not any(marker in public for marker in ("SyntheticCredential", "SyntheticPassword", "SyntheticBearer", "SYNTHETICPRIVATE"))
    assert "quoted synthetic credential" not in public


def test_acceptance_old_call_late_write_is_rejected(tmp_path):
    writer = _stream(tmp_path)
    old_callback = writer.bind_model_call("call-old")
    old_callback("旧候选。" * 40)
    callback = writer.bind_model_call("call-current")
    before = writer.chunk_path.read_bytes()
    old_callback("不能混入新调用。" * 40)
    assert writer.chunk_path.read_bytes() == before
    callback("新候选。" * 40)
    _commit(writer, "新候选。" * 40)
    rows = _events(writer)
    assert any(row.get("reason") == "superseded" and row["model_call_id"] == "call-old" for row in rows)
    assert rows[-1]["model_call_id"] == "call-current"


def test_acceptance_cursor_replay_and_default_invisibility(tmp_path):
    writer = _stream(tmp_path)
    callback = writer.bind_model_call("call-current")
    callback("第一段。" * 40)
    first, cursor = _read_public_progress_events(writer.chunk_path, 0, include=frozenset({"assistant_text_delta"}))
    callback("第二段。" * 40)
    _commit(writer, "第一段。" * 40 + "第二段。" * 40)
    rest, end = _read_public_progress_events(writer.chunk_path, cursor, include=frozenset({"assistant_text_delta"}))
    assert end > cursor
    seqs = [row["seq"] for row in first + rest if row["kind"] == "assistant_text_delta"]
    assert len(seqs) == len(set(seqs))
    assert _read_public_progress_events(writer.chunk_path, end, include=frozenset({"assistant_text_delta"})) == ([], end)
    assert _read_public_progress_events(writer.chunk_path, 0)[0] == [{"kind": "process_settings", "verbose_level": "stream"}]


def test_acceptance_long_command_input_and_full_output_archive(tmp_path):
    agent, thread = _owner(tmp_path)
    writer = _stream(tmp_path, level="full")
    command = "printf '" + "字" * 300 + "' --token=" + "sk-SyntheticCredential123456789"
    call = _canonical_calls([{"tool": "run_command", "command": command}])[0]
    request = SimpleNamespace(agent=agent, params=SimpleNamespace(
        task_attributes={"conversation_thread_id": thread.thread_id}), tool_rounds=1)
    started = ToolProgressEvent(request, 1, call, "started", "开始")
    first = _structured_tool_progress(started, "run_command", _payload_progress_detail(call))
    assert len(first["detail"]) <= 100
    assert first["input"]["command"] == redact_sensitive_text(command)
    assert "SyntheticCredential" not in json.dumps(first)
    stdout, stderr = "输出" * 12000, "诊断" * 12000
    result = ToolResult.succeeded(call, stdout + stderr, facts=ToolSuccessFacts(metadata={
        "handler_details": {"display": {"kind": "command", "return_code": 0,
                                        "stdout": stdout, "stderr": stderr, "capture_complete": True}}}))
    last = _structured_tool_progress(replace(started, phase="finished", status="完成", result=result),
                                     "run_command", _payload_progress_detail(call))
    assert last["call_id"] == first["call_id"] == call.call_id
    writer.write_progress(first, "开始")
    writer.write_progress(last, "完成")
    rows = _read_public_progress_events(writer.chunk_path, 0)[0]
    assert rows[0] == {"kind": "process_settings", "verbose_level": "full"}
    tool_rows = rows[1:]
    assert tool_rows and all(row["kind"] == "tool_progress" for row in tool_rows)
    assert stdout not in json.dumps(rows, ensure_ascii=False) and stderr not in json.dumps(rows, ensure_ascii=False)
    reference = rows[-1]["display_archive_ref"]
    restored = []
    for page in range(reference["page_count"]):
        restored.extend(read_display_archive_page(agent.conversation_store, reference, page)["rows"])
    assert "".join(row["text"] for row in restored if row["kind"] == "stdout") == stdout
    assert "".join(row["text"] for row in restored if row["kind"] == "stderr") == stderr
    short = _structured_tool_progress(started, "run_command", "", max_input_chars=16)
    assert short["input_truncated"] is True and len(short["input"]["command"]) == 16
    writer.set_verbose_level("on")
    writer.write_progress(first, "开始")
    assert "input" not in _read_public_progress_events(writer.chunk_path, 0)[0][-1]


@pytest.mark.parametrize("call_id", ["", None, "   "])
def test_empty_tool_call_id_is_omitted(call_id):
    call = SimpleNamespace(call_id=call_id, arguments={})
    event = ToolProgressEvent(SimpleNamespace(tool_rounds=1), 1, call, "started", "开始")
    assert "call_id" not in _structured_tool_progress(event, "read_file", "")


@pytest.mark.parametrize("level", ["off", "on", "full"])
def test_non_stream_level_never_persists_candidate(level, tmp_path):
    writer = _stream(tmp_path, level=level)
    callback = writer.bind_model_call("call-current")
    callback("原始候选。" * 50)
    writer.close()
    assert not writer.chunk_path.exists()


def test_tool_boundary_resets_candidate_and_late_write(tmp_path):
    writer = _stream(tmp_path)
    callback = writer.bind_model_call("call-current")
    callback("工具之前。" * 50)
    _commit(writer, "先执行工具。", boundary="tools")
    assert any(row.get("reason") == "tool_call" for row in _events(writer))
    snapshot = writer.chunk_path.read_bytes()
    callback("旧正文迟到。" * 40)
    assert writer.chunk_path.read_bytes() == snapshot


def test_text_delta_memory_budget_never_leaks_discarded_secret_suffix():
    buffer = TextDeltaBuffer(holdback_chars=8, max_buffer_chars=32)
    buffer.begin("bounded-call")
    assert buffer.append("sk-" + "S" * 60, model_call_id="bounded-call")[0]["reason"] == "superseded"
    assert buffer.append("secret-tail", model_call_id="bounded-call") == []
    assert all("text" not in row for row in buffer.finish("bounded-call"))


def test_workflow_step_is_typed_redacted_monotonic_and_opt_in(tmp_path):
    writer = _stream(tmp_path)
    event = {"workflow_run_id": "workflow-run", "workflow_id": "workflow", "step_id": "step",
             "name": "读取", "status": "running", "duration_ms": 0,
             "description": "token=synthetic-private-workflow", "next_step_ids": ["next"], "seq": 1,
             "private_unknown": "not-public"}
    writer.write_workflow_step_progress(event)
    writer.write_workflow_step_progress(event)
    assert _read_public_progress_events(writer.chunk_path, 0)[0] == [{"kind": "process_settings", "verbose_level": "stream"}]
    rows, cursor = _read_public_progress_events(writer.chunk_path, 0, include=frozenset({"workflow_step_progress"}))
    assert cursor == 2 and len(rows) == 2 and rows[0] == {"kind": "process_settings", "verbose_level": "stream"}
    assert rows[1]["kind"] == "workflow_step_progress" and rows[1]["step_id"] == "step" and "workflow_step" not in rows[1]
    assert "synthetic-private" not in json.dumps(rows) and "private_unknown" not in rows[1]
    writer.write_workflow_step_progress({**event, "seq": 2, "status": "succeeded"})
    assert _read_public_progress_events(writer.chunk_path, cursor, include=frozenset({"workflow_step_progress"}))[0][0]["seq"] == 2

import http.client

from agent_py_agent.agent.gateway_parts import request_execution
from agent_py_agent.agent.gateway_parts.paths import gateway_chunk_path
from agent_py_agent.agent.gateway_parts.request_context import GatewayConversationContext
from agent_py_agent.tests.test_gateway_identity_trust import _TOKEN, _server


def _w2_http(server, path, body=None, *, token=True):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Gateway-Token"] = _TOKEN
    try:
        conn.request("GET" if body is None else "POST", path,
                     None if body is None else json.dumps(body), headers)
        response = conn.getresponse()
        return response.status, json.loads(response.read())
    finally:
        conn.close()

@pytest.mark.parametrize("bad", [None, True, 0, [], {}, "FULL", "no"])
def test_ask_invalid_verbose_level_has_no_pending_file(tmp_path, bad):
    server = _server(tmp_path)
    server.start()
    try:
        status, payload = _w2_http(server, "/ask", {"goal": "验证档位", "verbose_level": bad})
        assert status == 400 and payload["error_code"] == "INVALID_VERBOSE_LEVEL"
        assert payload["error"] == "verbose_level必须是off/on/full/stream之一。"
        assert list(server.paths.inbox.glob("*.json")) == []
    finally:
        server.stop()

@pytest.mark.parametrize("explicit,expected", [("full", "full"), ("stream", "stream"), ("off", "off"), (None, "on")])
def test_ask_verbose_level_full_exposes_tool_progress_without_control(tmp_path, monkeypatch, explicit, expected):
    server = _server(tmp_path)
    server.start()
    try:
        body = {"goal": "读取资料", "conversation_id": "w2-http", "delivery_intent": "queue",
                "metadata": {"message_id": "w2-verbose"}}
        if explicit is not None:
            body["verbose_level"] = explicit
        status, receipt = _w2_http(server, "/ask", body)
        assert status == 202, receipt
        request_id = receipt["request_id"]
        request = json.loads((server.paths.inbox / f"{request_id}.json").read_text())
        assert request.get("verbose_level") == explicit
        writer = BufferedChunkStreamWriter(gateway_chunk_path(server.paths, request_id))
        context = SimpleNamespace(request=request, on_chunk=writer, agent=server.agent)
        conversation = GatewayConversationContext(verbose_level="on")
        # 只截在真实writer档位配置之后；HTTP、持久请求和档位分派均用产品代码。
        class Configured(Exception):
            pass
        def after_configure(*_args):
            writer.write_progress({"tool": "read_file", "phase": "started", "status": "开始"}, "开始")
            raise Configured
        monkeypatch.setattr(request_execution.request_history, "gateway_identifier_redactions", lambda *_args: ())
        monkeypatch.setattr(request_execution, "_configure_gateway_approval_session", after_configure)
        with pytest.raises(Configured):
            request_execution._execute_gateway_conversation_turn(context, "读取资料", conversation)
        writer.close()
        status, progress = _w2_http(server, f"/progress/{request_id}")
        assert status == 200
        tools = [row for row in progress["events"] if row.get("kind") == "tool_progress"]
        assert bool(tools) is (expected != "off")
        if tools:
            assert tools[0]["level"] == expected
        assert conversation.verbose_level == "on"
    finally:
        server.stop()

def test_progress_include_validation_and_kind_cannot_be_overridden(tmp_path):
    server = _server(tmp_path)
    server.start()
    try:
        status, receipt = _w2_http(server, "/ask", {"goal": "验证事件", "verbose_level": "stream"})
        assert status == 202
        rid = receipt["request_id"]
        path = gateway_chunk_path(server.paths, rid)
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [{"kind": "assistant_text_delta", "verbose_level": "stream", "model_call_id": "typed-call", "seq": 1, "text": "正文"},
                {"kind": "assistant_text_done", "verbose_level": "stream", "model_call_id": "typed-call", "seq": 1},
                {"kind": "tool_progress", "verbose_level": "full", "progress": {"tool": "read_file", "phase": "started", "kind": "forged"}}]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        code, payload = _w2_http(server, f"/progress/{rid}?include=unknown")
        assert code == 400 and payload["error_code"] == "INVALID_PROGRESS_INCLUDE"
        code, default = _w2_http(server, f"/progress/{rid}")
        assert code == 200 and len(default["events"]) == 1
        assert default["events"][0]["kind"] == "tool_progress"
        code, opted = _w2_http(server, f"/progress/{rid}?include=assistant_text_delta")
        assert code == 200 and [event["kind"] for event in opted["events"]] == ["assistant_text_delta", "assistant_text_done", "tool_progress"]
        code, replay = _w2_http(server, f"/progress/{rid}?since={opted['next']}&include=assistant_text_delta")
        assert code == 200 and replay["events"] == [] and replay["next"] == opted["next"]
    finally:
        server.stop()


def test_workflow_writer_to_http_opt_in_roundtrip(tmp_path):
    server = _server(tmp_path)
    server.start()
    try:
        code, receipt = _w2_http(server, "/ask", {"goal": "订阅步骤", "verbose_level": "stream"})
        assert code == 202
        request_id = receipt["request_id"]
        writer = BufferedChunkStreamWriter(gateway_chunk_path(server.paths, request_id))
        writer.set_verbose_level("stream")
        event = {"workflow_run_id": "run-wire", "workflow_id": "workflow", "step_id": "read",
                 "name": "读取", "status": "running", "duration_ms": 0, "description": "步骤说明",
                 "next_step_ids": ["analyze"], "seq": 1, "private_unknown": "not-public"}
        writer.write_workflow_step_progress(event)
        writer.write_workflow_step_progress(event)
        code, default = _w2_http(server, f"/progress/{request_id}")
        assert code == 200 and default["events"] == [{"kind": "process_settings", "verbose_level": "stream"}] and default["next"] == 2
        code, opted = _w2_http(server, f"/progress/{request_id}?include=workflow_step_progress")
        assert code == 200
        assert opted["events"] == [{"kind": "process_settings", "verbose_level": "stream"},
                                   {**{key: value for key, value in event.items() if key != "private_unknown"},
                                    "kind": "workflow_step_progress"}]
        code, replay = _w2_http(server, f"/progress/{request_id}?include=workflow_step_progress&since={opted['next']}")
        assert code == 200 and replay["events"] == [] and replay["next"] == opted["next"]
        writer.close()
    finally:
        server.stop()


# LLM: 仅替换供应商传输；真实 Agent 负责调用账本、回调组装和 sealed 提交，测试不手工绑定或提交。
# 函数用途: 经真实 generation → writer → HTTP 验证提交前增量与终稿使用同一物理调用编号。
def test_real_generation_delta_before_commit_matches_sealed_call_over_http(tmp_path):
    from agent_py_agent.agent.agent_core.model.call_runtime import model_call_ledger
    from agent_py_agent.agent.backends.base import ModelResponse
    from agent_py_agent.tests.test_cli_run_conversation import _agent, _StaticBackend

    server = _server(tmp_path / "http")
    agent = _agent(tmp_path / "execution")
    writer = None
    observed = {}
    text = "这是供应商回调实际产生的公开正文。" * 80
    server.start()
    try:
        code, receipt = _w2_http(server, "/ask", {"goal": "验证正文绑定", "verbose_level": "stream"})
        assert code == 202
        request_id = receipt["request_id"]
        writer = BufferedChunkStreamWriter(gateway_chunk_path(server.paths, request_id), rich_transcript=True)
        writer.set_verbose_level("stream")

        class Streaming(_StaticBackend):
            def generate(self, prompt, on_chunk=None, **kwargs):
                self.calls += 1
                on_chunk(text[:500])
                observed["code"], observed["before"] = _w2_http(
                    server, f"/progress/{request_id}?include=assistant_text_delta")
                observed["default_code"], observed["default"] = _w2_http(server, f"/progress/{request_id}")
                observed["call_id"] = model_call_ledger(agent).records()[-1].call_id
                on_chunk(text[500:])
                return ModelResponse(text, self.name)

        agent.backend = Streaming()
        result = agent.run("验证正文绑定", source="gateway", request_id=request_id,
                           on_chunk=writer, allowed_tools=[], save=False)
        assert agent.backend.calls == 1 and result.response == text
        assert observed["code"] == observed["default_code"] == 200
        before = [row for row in observed["before"]["events"] if row["kind"] == "assistant_text_delta"]
        assert before, "真实供应商回调在提交前必须已经产生 delta，不能由终稿补造"
        assert {row["model_call_id"] for row in before} == {observed["call_id"]}
        assert not any(row["kind"] == "assistant_text_done" for row in observed["before"]["events"])
        assert not any(row["kind"].startswith("assistant_text_") for row in observed["default"]["events"])
        code, progress = _w2_http(server, f"/progress/{request_id}?include=assistant_text_delta")
        assert code == 200
        text_rows = [row for row in progress["events"] if row["kind"].startswith("assistant_text_")]
        deltas = [row for row in text_rows if row["kind"] == "assistant_text_delta"]
        assert "".join(row["text"] for row in deltas) == text
        assert [row["seq"] for row in deltas] == list(range(len(deltas)))
        sealed = [json.loads(line) for line in writer.chunk_path.read_text().splitlines()
                  if json.loads(line).get("kind") == "assistant_final"]
        assert sealed and sealed[-1]["model_source"]["call_id"] == observed["call_id"]
        assert text_rows[-1] == {"kind": "assistant_text_done", "model_call_id": observed["call_id"],
                                 "seq": len(deltas) - 1}
        assert _w2_http(server, f"/progress/{request_id}?include=assistant_text_delta&since={progress['next']}")[1]["events"] == []
    finally:
        if writer is not None:
            writer.close()
        server.stop()


# LLM: 复用真实工具轮与物理调用账本，不由测试构造 call_id；旧回调在第二次生成中真实重入。
# 函数用途: 验证多轮 generation 自动重绑身份，旧供应商回调不能污染后一次正文。
def test_real_generation_tool_boundary_rebinds_and_rejects_late_callback(tmp_path):
    from agent_py_agent.agent.agent_core.model.call_runtime import model_call_ledger
    from agent_py_agent.agent.backends.base import ModelResponse
    from agent_py_agent.tests.test_cli_run_conversation import _agent, _StaticBackend

    agent = _agent(tmp_path)
    source = agent.home_paths.owner_home_dir / "stream-source.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("真实工具证据", encoding="utf-8")
    writer = _stream(tmp_path)
    observed, callbacks = [], []
    texts = ["先核对当前资料。" * 80, "已核对并完成交付。" * 80]

    class ReadThenReply(_StaticBackend):
        def generate(self, prompt, on_chunk=None, **kwargs):
            self.calls += 1
            callbacks.append(on_chunk)
            current = model_call_ledger(agent).records()[-1].call_id
            on_chunk(texts[self.calls - 1])
            if self.calls == 2:
                before = _events(writer)
                callbacks[0]("SyntheticLateOldCall 不得进入新正文。" * 80)
                assert _events(writer) == before
            observed.append((current, list(_events(writer))))
            if self.calls == 1:
                return ModelResponse(texts[0], self.name, tool_use_blocks=[
                    {"id": "w2-native-read", "name": "read_file", "input": {"path": str(source)}}])
            return ModelResponse(texts[1], self.name)

    agent.backend = ReadThenReply()
    try:
        result = agent.run("读取资料并完成核对", on_chunk=writer, allowed_tools=["read_file"], save=False)
        assert agent.backend.calls == 2 and result.tool_rounds == 1 and result.response == texts[1]
        ids = [call_id for call_id, _rows in observed]
        assert len(set(ids)) == 2
        for call_id, rows in observed:
            assert any(row["kind"] == "assistant_text_delta" and row["model_call_id"] == call_id for row in rows)
        rows = _events(writer)
        assert any(row.get("reason") == "tool_call" and row["model_call_id"] == ids[0] for row in rows)
        assert rows[-1]["kind"] == "assistant_text_done" and rows[-1]["model_call_id"] == ids[1]
        assert "SyntheticLateOldCall" not in writer.chunk_path.read_text()
        assert ids == [record.call_id for record in model_call_ledger(agent).records()]
        assert _read_public_progress_events(writer.chunk_path, 0)[0]
        assert not any(row["kind"].startswith("assistant_text_")
                       for row in _read_public_progress_events(writer.chunk_path, 0)[0])
    finally:
        writer.close()
