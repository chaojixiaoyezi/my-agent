"""外部审计确认缺陷的故障注入；不替代真实 TUI 验收。"""
from __future__ import annotations

import codecs
import io
import json
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.tooling import mcp_client as mcp
from agent_py_agent.agent.tooling._filesystem_read import ReadFileTool
from agent_py_agent.agent.tooling.artifact import ArtifactReadBudget, ArtifactReadBudgetRequest


@pytest.mark.parametrize("encoding,bom", [("utf-16-be", codecs.BOM_UTF16_BE),
                                         ("utf-32-le", codecs.BOM_UTF32_LE), ("utf-8", codecs.BOM_UTF8)])
def test_all_read_modes_share_encoding_and_no_gaps(tmp_path, encoding, bom):
    source = "第一行\n" + "甲乙丙丁戊己庚辛壬癸" * 30
    path = tmp_path / "notes.txt"
    path.write_bytes(bom + source.replace("\n", "\r\n").encode(encoding))
    tool = ReadFileTool(tmp_path, max_chars=25)
    assert tool.execute({"path": "notes.txt", "start_line": 1, "end_line": 1}).ok
    actual, offset = [], 0
    while offset < len(source):
        result = tool.execute({"path": "notes.txt", "offset": offset, "max_chars": 25})
        assert result.ok, result.output
        window = result.result_envelope["read_window"]
        actual.append(result.output.split("\n", 1)[1][:window["chars"]])
        assert window["next_offset"] == offset + window["chars"]
        offset = window["next_offset"]
    assert "".join(actual) == source


def test_late_long_line_cursor_excludes_prior_lines_and_line_label(tmp_path, monkeypatch):
    path = tmp_path / "long.txt"
    path.write_text("head\nsecond\n0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    tool = ReadFileTool(tmp_path, max_chars=10)
    monkeypatch.setattr(type(path), "read_bytes", lambda self: pytest.fail("must stream"))
    result = tool.execute({"path": "long.txt", "start_line": 3})
    window = result.result_envelope["read_window"]
    assert window["offset"] == len("head\nsecond\n")
    assert window["chars"] == 7
    assert window["complete"] is False
    next_page = tool.execute({"path": "long.txt", "offset": window["next_offset"], "max_chars": 10})
    assert "789ABCDEFG" in next_page.output


def test_sequential_pages_record_resume_cookie(tmp_path):
    from agent_py_agent.agent.common.text_file_window import text_file_index
    path = tmp_path / "large.txt"
    path.write_text("内容" * 100000)
    index = text_file_index(SimpleNamespace(), path)
    assert index.read(150000, 100) == "内容" * 50
    assert 150100 in index.cursors
    assert index.read(150100, 100) == "内容" * 50
    assert len(index.checkpoints) <= 1024


def test_create_only_preserves_concurrent_creator(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling import _filesystem_write as writes
    target = tmp_path / "new.txt"
    original_link = writes.os.link
    def competing_link(source, destination):
        target.write_text("other writer")
        return original_link(source, destination)
    monkeypatch.setattr(writes.os, "link", competing_link)
    with pytest.raises(FileExistsError):
        writes._atomic_write_bytes(target, b"our writer", create_only=True)
    assert target.read_text() == "other writer"
    assert list(tmp_path.iterdir()) == [target]


def test_artifact_budget_reserves_pending_and_releases_failure():
    budget = ArtifactReadBudget(window_seconds=60, max_chars=10)
    first, error = budget.reserve(ArtifactReadBudgetRequest("run", 6))
    assert first is not None and not error
    second, error = budget.reserve(ArtifactReadBudgetRequest("run", 6))
    assert second is None and error
    budget.settle(first, 0)
    third, error = budget.reserve(ArtifactReadBudgetRequest("run", 10))
    assert third is not None and not error
    budget.settle(third, 3)
    fourth, error = budget.reserve(ArtifactReadBudgetRequest("run", 7))
    assert fourth is not None and not error


def test_mcp_queue_wait_counts_towards_timeout():
    client = mcp.MCPStdioClient(mcp.MCPServerConfig("queue", "unused"))
    client._lock.acquire()
    before = time.monotonic()
    try:
        with pytest.raises(mcp.MCPError) as error:
            client._request("tools/list", {}, timeout=0.06)
        assert error.value.code == "MCP_TIMEOUT"
        assert time.monotonic() - before < 0.5
        assert not client._pending_request_ids
    finally:
        client._lock.release()


def test_mcp_server_request_is_not_a_client_response(monkeypatch):
    client = mcp.MCPStdioClient(mcp.MCPServerConfig("request", "unused"))
    sent = []
    monkeypatch.setattr(client, "_send", lambda message, **kwargs: sent.append(message))
    client._pending_request_ids.add(1)
    client._dispatch_message({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert sent == [{"jsonrpc": "2.0", "id": 1, "result": {}}]
    assert client._responses == {}


def test_mcp_rejects_unsupported_protocol(monkeypatch):
    client = mcp.MCPStdioClient(mcp.MCPServerConfig("version", "unused"))
    monkeypatch.setattr(client, "_request", lambda *a, **kw: {
        "protocolVersion": "unsupported", "capabilities": {}, "serverInfo": {}})
    with pytest.raises(mcp.MCPError) as error:
        client._handshake()
    assert error.value.code == "MCP_PROTOCOL_ERROR"


@pytest.mark.parametrize("definition", [{"name": "tool", "inputSchema": []},
                                       {"name": 1, "inputSchema": {}}, {"name": "tool"}])
def test_mcp_catalog_does_not_replace_malformed_schema_with_empty(definition):
    with pytest.raises(mcp.MCPError) as error:
        mcp._discover_tools(lambda *args, **kwargs: {"tools": [definition]}, timeout=1)
    assert error.value.code == "MCP_PROTOCOL_ERROR"


def test_mcp_list_change_during_refresh_is_not_lost(monkeypatch):
    client = mcp.MCPStdioClient(mcp.MCPServerConfig("changed", "unused"))
    def changed(*args, **kwargs):
        client._dispatch_message({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        return {"tools": []}
    monkeypatch.setattr(client, "_request", changed)
    assert client.list_tools() == []
    assert client.tools_changed is True


def test_mcp_timeout_sends_cancellation_to_real_server(tmp_path):
    from agent_py_agent.tests.test_mcp_client import _HANG_ON_CALL_SERVER, _config
    observed = tmp_path / "cancel.json"
    server = _HANG_ON_CALL_SERVER
    # textwrap.dedent 后服务循环缩进为四格。
    server = server.replace('    if method == "initialize":',
        '    if method == "notifications/cancelled":\n'
        f'        with open({str(observed.with_suffix(".tmp"))!r}, "w") as output:\n'
        '            output.write(json.dumps(req["params"]))\n'
        f'        __import__("os").replace({str(observed.with_suffix(".tmp"))!r}, {str(observed)!r})\n'
        '    elif method == "initialize":')
    client = mcp.MCPStdioClient(_config(server, timeout=0.1))
    try:
        client.start()
        with pytest.raises(mcp.MCPError) as error:
            client.call_tool("slow", {})
        assert error.value.code == "MCP_TIMEOUT"
        until = time.monotonic() + 1
        while not observed.exists() and time.monotonic() < until:
            threading.Event().wait(0.01)
        assert json.loads(observed.read_text())["requestId"] == 2
        assert client._pending_request_ids == set()
    finally:
        client.stop()


def test_mcp_write_backpressure_has_deadline():
    server = '''import json,sys,time
request=json.loads(sys.stdin.readline())
print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":{"protocolVersion":"2024-11-05","capabilities":{},"serverInfo":{}}}),flush=True)
time.sleep(30)
'''
    client = mcp.MCPStdioClient(mcp.MCPServerConfig("backpressure", sys.executable,
        args=["-u", "-c", server], timeout=0.1, max_line_chars=1024 * 1024))
    try:
        client.start()
        before = time.monotonic()
        with pytest.raises(mcp.MCPError) as error:
            client.call_tool("unused", {"text": "x" * 500000})
        assert error.value.code == "MCP_TIMEOUT"
        assert time.monotonic() - before < 1
        assert client._proc.poll() is not None
    finally:
        client.stop()


def test_mcp_stderr_without_newlines_is_bounded():
    tail = mcp._StderrTail("noise")
    tail.drain(io.StringIO("x" * 500000))
    assert len(tail._lines) == 20
    assert sum(map(len, tail._lines)) <= 20 * 4096


def test_creation_storage_error_is_not_invalid_arguments(monkeypatch):
    from agent_py_agent.agent.agent_core import orchestration_tools as orchestration
    def fail(*args):
        raise OSError("storage unavailable")
    monkeypatch.setattr(orchestration, "_execute_create_subagents", fail)
    result = orchestration.execute_create_subagents_service(SimpleNamespace(), {"goal": "valid"})
    assert result.error_code == "TOOL_EXECUTION_FAILED"
    assert result.effect_outcome == "unknown"
    assert result.result_envelope["error_category"] == "storage"


def test_parent_wait_load_failure_is_unavailable():
    from agent_py_agent.agent.subagents.direct_parent_lifecycle import reconcile_all_parent_waits
    def fail():
        raise OSError("storage unavailable")
    summary = reconcile_all_parent_waits(SimpleNamespace(list_runs=fail))
    assert summary["ok"] is False
    assert summary["checked"] is None
    assert summary["state"] == "unavailable"


def test_idle_status_uses_owner_model_choice_without_changing_active_request(monkeypatch):
    from agent_py_agent.agent.gateway_parts.control_service import _status_model_name
    from agent_py_agent.agent.settings import model_profiles

    agent = SimpleNamespace(config=SimpleNamespace(model_name="gateway-placeholder"), home_paths=object())
    monkeypatch.setattr(model_profiles, "selected_model_config", lambda agent: SimpleNamespace(model_name="chosen-model"))
    assert _status_model_name(agent, idle=True) == "chosen-model"
    assert _status_model_name(agent, idle=False) == "gateway-placeholder"
    assert agent.config.model_name == "gateway-placeholder"


def test_schedule_list_honors_limit(monkeypatch):
    from agent_py_agent.agent.scheduler import tool as schedules
    monkeypatch.setattr(schedules, "_job_projection", lambda job, **kwargs: job)
    repository = SimpleNamespace(list_jobs=lambda: ([{"job_id": str(i)} for i in range(6)], []),
                                 active_runs_by_job=lambda: ({}, []))
    tool = schedules.ScheduleTool.__new__(schedules.ScheduleTool)
    result = tool._execute(repository, "list", {"limit": 2})
    payload = json.loads(result.output)
    assert len(payload["jobs"]) == 2
    assert payload["total"] == 6 and payload["has_more"] is True


def test_search_provider_fallback_happens_after_domain_filter():
    from agent_py_agent.agent.tooling.web_search import _search_with_providers
    providers = [SimpleNamespace(name="irrelevant", search=lambda *args: [{"url": "https://bad.test"}]),
                 SimpleNamespace(name="relevant", search=lambda *args: [{"url": "https://wanted.test/page"}])]
    result = _search_with_providers(providers, "topic", 2, allowed_domains=["wanted.test"])
    assert result.provider_name == "relevant"


@pytest.mark.parametrize("command,background", [
    ("printf '%s' '&'", False), ('printf "%s" "&"', False),
    (r"printf '%s' \&", False), ("echo hi # & ignored", False),
    ("echo hi && echo bye", False), ("echo hi 2>&1", False),
    ("echo hi &> output", False), ("echo hi |& cat", False),
    ("sleep 3 &", True), ("sleep 3&echo hi", True),
    ("echo '&'; sleep 3 &", True), ("cat <<'EOF'\nx & y\nEOF", False),
])
def test_shell_background_lexer_keeps_quote_and_escape(command, background):
    from agent_py_agent.agent.tooling.shell_syntax import contains_unmanaged_background_operator
    assert contains_unmanaged_background_operator(command) is background


def test_shell_capture_drains_both_unbroken_streams_with_bounded_memory(monkeypatch):
    import subprocess

    from agent_py_agent.agent.tooling import shell
    from agent_py_agent.agent.tooling.process_output_capture import ProcessOutputCapture
    monkeypatch.setattr(shell, "ProcessOutputCapture", lambda proc: ProcessOutputCapture(proc, max_bytes=4096))
    proc = subprocess.Popen([sys.executable, "-c", "import os; os.write(1,b'x'*2000000); os.write(2,b'y'*2000000)"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    result = shell._communicate_process(proc, command="large-output", timeout=5)
    assert result.returncode == 0
    assert len(result.stdout) == len(result.stderr) == 4096
    assert result.capture["bytes_seen"] == {"stdout": 2000000, "stderr": 2000000}
    assert result.capture["complete"] is False
    assert result.capture["truncated"] is True


def test_memory_embedder_init_failure_is_visible_without_secret(tmp_path, monkeypatch, caplog):
    from agent_py_agent.agent.core import _build_memory_embedder
    from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
    from agent_py_agent.agent.retrieval import embedding
    from agent_py_agent.agent.settings.config import AgentConfig
    def broken(**kwargs):
        raise RuntimeError("secret-not-for-logs")
    monkeypatch.setattr(embedding, "OpenAICompatibleEmbedder", broken)
    status = {}
    assert _build_memory_embedder(AgentConfig(memory_semantic_recall=True, memory_embedding_model="test"), diagnostics=status) is None
    assert status["error_code"] == "MEMORY_EMBEDDING_INIT_FAILED"
    memory = JsonlMemory(tmp_path / "memory.jsonl", semantic_status=status)
    memory.add("user", "保留正式事实", kind="fact")
    assert memory.runtime_snapshot()["health"] == "degraded"
    assert len(memory.all()) == 1
    assert "secret-not-for-logs" not in caplog.text


def test_semantic_runtime_failure_is_visible_and_recovery_clears_own_error(tmp_path):
    from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
    state = {"broken": True}
    def embed(texts):
        if state["broken"]:
            raise OSError("offline")
        return [[1.0, 0.0] for _ in texts]
    memory = JsonlMemory(tmp_path / "memory.jsonl", embedder=SimpleNamespace(embed=embed))
    memory._semantic_records("hello", 1)
    assert memory.runtime_snapshot()["semantic_recall"]["errors"] == {"search": "OSError"}
    state["broken"] = False
    memory._semantic_records("hello", 1)
    assert memory.runtime_snapshot()["semantic_recall"]["state"] == "configured"


def test_history_around_stays_inside_anchor_thread_and_honors_filters(tmp_path):
    from agent_py_agent.agent.capability.session_search_tool import SessionSearchTool
    from agent_py_agent.agent.local_storage.store import LocalStore
    store = LocalStore(tmp_path / "history.db")
    for n, thread in enumerate(["a", "b", "a", "b", "a"]):
        store.upsert_record(source_type="gateway_request", source_id=str(n), title=str(n), content=str(n),
                            metadata={"conversation_runtime": {"thread_id": thread}})
    rows = store.list_recent(limit=10)
    anchor = next(row for row in rows if row.source_id == "2")
    tool = SessionSearchTool(SimpleNamespace(local_store=store))
    result = json.loads(tool.execute({"around_id": anchor.id, "window": 10}).output)
    assert result["context_kind"] == "same_thread_records"
    assert result["thread_id"] == "a"
    assert {row["title"] for row in result["messages"]} == {"0", "2", "4"}
    assert store.records_around(anchor.id, source_type="memory") == ([], 0, 0)


def test_edit_preview_uses_changed_location_not_preexisting_new_text():
    from agent_py_agent.agent.tooling._filesystem_edit import _replaced_context_preview
    before = "new\n" + "unchanged\n" * 25 + "old\nend\n"
    after = before.replace("old", "new")
    assert "end" in _replaced_context_preview(before, after, "new", 1)
    assert "end" in _replaced_context_preview(before, before.replace("old\n", ""), "", 1)


def test_native_compact_search_is_logarithmic_and_keeps_smallest_prefix():
    from agent_py_agent.agent.agent_core.tool_ir_compact import compact_native_ir_to_token_budget
    from agent_py_agent.agent.backends.tool_ir import ToolResult
    from agent_py_agent.tests._tool_runtime_harness import (
        canonical_history_call,
        canonical_history_result,
    )
    history = [canonical_history_result(canonical_history_call("read_file", {}, call_id=f"call-{n}"), "x") for n in range(128)]
    params = SimpleNamespace(tool_ir_history=history)
    calls = []
    def estimate():
        calls.append(1)
        return len([x for x in params.tool_ir_history if isinstance(x, ToolResult)]) * 100
    assert compact_native_ir_to_token_budget(params, max_tokens=4000, token_estimator=estimate) == 88
    assert len(calls) <= 10
    assert len(params.tool_ir_history) == 40


def test_pty_windows_discovery_reports_conpty_unavailable(monkeypatch):
    from agent_py_agent.agent.tooling import pty_sessions
    monkeypatch.setattr(pty_sessions, "os", SimpleNamespace(name="nt"))
    availability = pty_sessions.TerminalSessionTool(SimpleNamespace()).availability()
    assert availability.available is False
    assert availability.error_code == "PTY_UNAVAILABLE"


def test_web_cache_eviction_and_expiry_release_bodies(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling import web_fetch_tools
    from agent_py_agent.agent.tooling.web import WebFetchTool
    from agent_py_agent.agent.tooling.web_fetch_runtime import RawResponseParts
    tool = WebFetchTool(max_chars=1000, timeout=5, artifact_root=tmp_path)
    now = [1000.0]
    monkeypatch.setattr(web_fetch_tools.time, "time", lambda: now[0])
    for i in range(80):
        tool._cache_put(str(i), "text", RawResponseParts(200, {}, b"hello", str(i)))
    assert len(tool._cache) == 64
    assert tool._cache_get("0", "text") is None
    now[0] += tool.cache_ttl_seconds + 1
    assert tool._cache_get("79", "text") is None
    assert not tool._cache


def test_web_refresh_bypasses_only_local_cache(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling.web import WebFetchTool
    from agent_py_agent.agent.tooling.web_fetch_runtime import RawResponseParts
    tool = WebFetchTool(max_chars=1000, timeout=5, artifact_root=tmp_path)
    calls = []
    def fetch(request):
        calls.append(request)
        return RawResponseParts(200, {"Content-Type": "text/plain"}, b"hello", request.url)
    monkeypatch.setattr(tool, "_fetch_raw_request", fetch)
    for refresh in (False, False, True):
        result = tool.execute({"url": "https://example.test", "refresh": refresh})
        assert result.ok
    assert len(calls) == 2


def test_web_extract_forwards_parameters_and_stops_at_total_bytes(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling import web_fetch_tools
    from agent_py_agent.agent.tooling.web import WebFetchTool
    from agent_py_agent.agent.tooling.web_fetch_runtime import RawResponseParts
    tool = WebFetchTool(max_chars=1000, timeout=5, artifact_root=tmp_path)
    calls = []
    def fetch(request, **kwargs):
        calls.append(request)
        return RawResponseParts(200, {}, b"x" * request.max_bytes, request.url)
    monkeypatch.setattr(web_fetch_tools, "fetch_raw_response", fetch)
    monkeypatch.setattr(web_fetch_tools, "page_payload_from_response", lambda root, url, response, limit: {"url": url})
    result = tool.execute({"urls": [f"https://example.test/{i}" for i in range(16)], "method": "POST",
                           "headers": {"X-Test": "visible-test"}, "body": "hello"})
    assert result.ok
    assert all(call.method == "POST" and call.headers["X-Test"] == "visible-test" and call.data == b"hello" for call in calls)
    assert sum(call.max_bytes for call in calls) == 8 * 1024 * 1024
    assert result.result_envelope["complete"] is False
    assert result.result_envelope["remaining_urls"]


def test_web_total_deadline_stops_continuous_slow_body():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from agent_py_agent.agent.tooling.web_fetch_runtime import (
        FetchRawRequest,
        PinResult,
        fetch_raw_response,
    )
    stop = threading.Event()
    class Drip(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "100000")
            self.end_headers()
            try:
                while not stop.wait(0.01):
                    self.wfile.write(b"x")
                    self.wfile.flush()
            except OSError:
                pass
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Drip)
    worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    worker.start()
    started = time.monotonic()
    try:
        result = fetch_raw_response(FetchRawRequest("web_fetch", f"http://localhost:{server.server_port}/", "GET", {}, None, 0.15),
                                    format_http_error=lambda *args: None, resolve_pin=lambda url: PinResult("127.0.0.1", None))
        assert result.error_code == "TOOL_TIMEOUT"
        assert time.monotonic() - started < 0.8
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        worker.join(1)


@pytest.mark.parametrize("mode", ["content", "line_numbers", "files_with_matches", "count"])
def test_rg_python_common_contract_parity(tmp_path, monkeypatch, mode):
    import shutil

    from agent_py_agent.agent.tooling import _filesystem_search as search
    if not shutil.which("rg"):
        pytest.skip("rg is needed for the comparison")
    (tmp_path / "one.txt").write_text("中文 needle\nNEEDLE\n")
    (tmp_path / "two.txt").write_text("无关\nneedle again\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "ignored.txt").write_text("needle")
    tool = search.SearchTextTool(tmp_path, max_matches=20)
    params = {"query": "needle", "literal": True, "ignore_case": True, "output_mode": mode, "limit": 20}
    fast = tool.execute(params)
    monkeypatch.setattr(search, "_start_rg_process", lambda *args: None)
    fallback = tool.execute(params)
    assert fast.ok and fallback.ok
    assert fast.output == fallback.output


def test_pty_pending_start_reserves_last_slot(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling import pty_sessions as pty
    if pty.os.name == "nt":
        pytest.skip("POSIX PTY admission")
    registry = pty.PtySessionRegistry()
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(pty, "_MAX_SESSIONS", 1)
    def spawn(*args):
        entered.set()
        release.wait(1)
    monkeypatch.setattr(registry, "_spawn", spawn)
    worker = threading.Thread(target=lambda: registry.start("dummy", tmp_path))
    worker.start()
    assert entered.wait(1)
    try:
        with pytest.raises(OSError, match="PTY_SESSION_LIMIT"):
            registry.start("dummy", tmp_path)
    finally:
        release.set()
        worker.join(1)
    assert not registry._pending_starts


def test_pty_write_backpressure_and_history_are_bounded(monkeypatch):
    import os
    import pty as os_pty
    import tty

    from agent_py_agent.agent.tooling import pty_sessions as pty
    registry = pty.PtySessionRegistry()
    master, slave = os_pty.openpty()
    tty.setraw(slave)
    os.set_blocking(master, False)
    session = pty.PtySession("current", "dummy", SimpleNamespace(poll=lambda: None), master, 0, 0, None)
    registry._sessions["current"] = session
    monkeypatch.setattr(pty, "_WRITE_TIMEOUT_SECONDS", 0.08)
    started = time.monotonic()
    try:
        with pytest.raises(pty.PtyWriteError) as caught:
            registry.write("current", b"x" * 1000000)
        assert caught.value.code == "TOOL_TIMEOUT"
        assert 0 < caught.value.written < 1000000
        assert time.monotonic() - started < 0.5
    finally:
        os.close(master)
        os.close(slave)
    for n in range(100):
        registry._sessions[str(n)] = SimpleNamespace(closed=True, process=SimpleNamespace(poll=lambda: 0))
    registry._prune_finished()
    assert len(registry._sessions) == 33


@pytest.mark.parametrize("kind", ["write", "edit", "patch"])
def test_observed_file_version_blocks_stale_model_edit(tmp_path, kind):
    from agent_py_agent.agent.tooling._filesystem_edit import EditFileTool
    from agent_py_agent.agent.tooling._filesystem_patch import ApplyPatchTool
    from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool
    path = tmp_path / "source.txt"
    path.write_text("alpha\nbeta\n")
    read = ReadFileTool(tmp_path, max_chars=100).execute({"path": "source.txt"})
    version = read.result_envelope["file_version"]
    path.write_text("alpha\nbeta\nhuman-added\n")
    tools_and_args = {
        "write": (WriteFileTool(tmp_path), {"path": "source.txt", "content": "ALPHA\nbeta\n", "expected_version": version}),
        "edit": (EditFileTool(tmp_path), {"path": "source.txt", "old_string": "alpha", "new_string": "ALPHA", "expected_version": version}),
        "patch": (ApplyPatchTool(tmp_path), {"patch": "*** Begin Patch\n*** Update File: source.txt\n-alpha\n+ALPHA\n*** End Patch", "expected_versions": {"source.txt": version}}),
    }
    tool, args = tools_and_args[kind]
    result = tool.execute(args)
    assert not result.ok and result.error_code == "STALE_VERSION"
    assert result.effect_outcome == "not_started"
    assert path.read_text() == "alpha\nbeta\nhuman-added\n"
    current = ReadFileTool(tmp_path, max_chars=100).execute({"path": "source.txt"}).result_envelope["file_version"]
    if kind == "patch":
        args["expected_versions"]["source.txt"] = current
    else:
        args["expected_version"] = current
    assert tool.execute(args).ok
