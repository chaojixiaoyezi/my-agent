"""请求计时只解释等待区间；原审计、取消收口和异常身份不能被测量改变。"""
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import request_execution as execution
from agent_py_agent.agent.gateway_parts.io import write_json_file
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.local_storage import diagnostics


class Clock:
    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now

    def time(self):
        return 1000.0 + self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.mark.parametrize("corrupt", [False, True])
def test_prepare_separates_file_context_and_audit_without_changing_request(tmp_path, monkeypatch, corrupt):
    clock = Clock()
    monkeypatch.setattr(execution, "time", clock)
    monkeypatch.setattr(diagnostics, "time", clock)
    path = tmp_path / "request.json"
    if corrupt:
        path.write_text("{", encoding="utf-8")
    else:
        write_json_file(path, {"id": "request", "kind": "ask", "prompt": "private body"})
    original_bytes = path.read_bytes()
    original_read = execution.read_json_file_report
    seen = []

    def read(*args, **kwargs):
        clock.advance(0.125)
        return original_read(*args, **kwargs)

    def paths(_agent):
        clock.advance(0.25)
        return gateway_paths_from_root(tmp_path / "gateway")

    def audit(_agent, context):
        seen.append(context)
        with diagnostics.local_store_stage("gateway_audit_render_ms"):
            clock.advance(1.5)
        clock.advance(3.5)

    monkeypatch.setattr(execution, "read_json_file_report", read)
    monkeypatch.setattr(execution, "gateway_paths", paths)
    monkeypatch.setattr(execution, "audit_request_processing", audit)
    context = execution._prepare_gateway_request_context(SimpleNamespace(), path)
    assert seen == [context] and path.read_bytes() == original_bytes
    assert bool(context.get("skip_execution")) is corrupt
    assert context["stage_timings"] == {
        "request_json_read_ms": 125.0, "request_context_build_ms": 250.0,
        "request_audit_ms": 5000.0, "gateway_audit_render_ms": 1500.0,
    }
    assert all(type(value) is float for value in context["stage_timings"].values())


@pytest.mark.parametrize("fail_body", [False, True])
def test_handler_separates_cleanup_and_preserves_cancel_end_boundary(tmp_path, monkeypatch, fail_body):
    import threading

    clock = Clock()
    monkeypatch.setattr(execution, "time", clock)
    path = tmp_path / "processing" / "request.json"
    request = {"id": "request", "kind": "ask", "status": "processing"}
    write_json_file(path, request)
    response = {"id": "request", "kind": "ask", "ok": False, "status": "processing"}
    context = {"request": request, "request_id": "request", "kind": "ask", "request_path": path,
               "response_path": tmp_path / "response.json", "started_at": clock.time(),
               "response": response, "stage_timings": {"request_json_read_ms": 7.0}}
    stop = threading.Event()
    order = []

    class Lease:
        def join(self, timeout):
            assert stop.is_set() and timeout == 2
            order.append("lease")
            clock.advance(2)

    class Writer:
        observed_tool_rounds = 0

        def __init__(self, *_args, **_kwargs):
            pass

        def close(self):
            order.append("chunk")
            clock.advance(3)

    def body(actual, _writer):
        assert actual["request"] is request
        clock.advance(4)
        write_json_file(path, {**request, "cancel_requested": True, "cancel_requested_at": clock.time()})
        if fail_body:
            raise InterruptedError("test cancellation")
        response.update(ok=True, status="done")

    def settle(*_args, **_kwargs):
        order.append("guidance")
        clock.advance(5)

    def release(_context):
        order.append("release")
        clock.advance(6)

    def complete(_agent, *, params):
        assert params.response is response and response["status"] == "interrupted"
        assert response["ended_at"] == 1120.0
        order.append("audit")
        clock.advance(7)

    monkeypatch.setattr(execution, "_prepare_gateway_request_context", lambda *_: context)
    monkeypatch.setattr(execution, "_start_gateway_request_lease", lambda *_: (stop, Lease()))
    monkeypatch.setattr(execution, "open_chunk_stream", lambda path: (path, None))
    monkeypatch.setattr(execution, "BufferedChunkStreamWriter", Writer)
    monkeypatch.setattr(execution, "_gateway_request_interactive_approvals", lambda *_: False)
    monkeypatch.setattr(execution, "turn_started", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(execution, "turn_ended", lambda *_args: order.append("ended"))
    monkeypatch.setattr(execution, "_execute_gateway_request_body", body)
    monkeypatch.setattr(execution, "_settle_pending_gateway_guidance", settle)
    monkeypatch.setattr(execution, "_release_unclaimed_turn_guidance", release)
    monkeypatch.setattr(execution, "audit_request_completed", complete)
    result = execution._handle_gateway_request(SimpleNamespace(config=SimpleNamespace()), path)
    assert result is response and result["status"] == "interrupted" and result["ok"]
    assert result["error_code"] == "INTERRUPTED"
    assert order == ["lease", "chunk", "guidance", "release", "audit", "ended"]
    stages = result["stages_ms"]
    assert stages["request_json_read_ms"] == 7.0
    assert stages["execution_body_ms"] == 4000.0
    assert stages["lease_stop_ms"] == 2000.0 and stages["chunk_close_ms"] == 3000.0
    assert stages["guidance_close_ms"] == 5000.0 and stages["guidance_release_ms"] == 6000.0
    assert stages["execution_ms"] == 14000.0 and stages["total_ms"] == 14000.0
    assert stages["finalize_ms"] == 6000.0 and stages["completed_audit_ms"] == 7000.0
    assert result["ended_at"] == 1120.0 and result["duration_seconds"] == 20.0
    assert clock.time() == 1127.0


@pytest.mark.parametrize("entry_fails", [False, True])
def test_broken_clock_does_not_mask_body_exception(monkeypatch, entry_fails):
    calls = 0

    def clock():
        nonlocal calls
        calls += 1
        if entry_fails or calls > 1:
            raise ValueError("diagnostic clock unavailable")
        return 1.0

    monkeypatch.setattr(execution, "time", SimpleNamespace(monotonic=clock))
    original = InterruptedError("original cancellation")
    stages = {}
    with pytest.raises(InterruptedError) as caught:
        with execution._measure_gateway_stage(stages, "body_ms"):
            raise original
    assert caught.value is original and stages == {}
