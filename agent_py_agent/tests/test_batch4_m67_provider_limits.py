import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model import call_runtime
from agent_py_agent.agent.agent_core.model.call_runtime import model_request_token_budget
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.agent_core.tool_model_generation import _generate_with_request_options
from agent_py_agent.agent.backends.base import BackendOptions
from agent_py_agent.agent.backends.errors import ProviderTimeoutError
from agent_py_agent.agent.backends.openai_chat import OpenAICompatibleBackend
from agent_py_agent.agent.backends.responses import OpenAIResponsesBackend


def _state(backend, *, deadline=None):
    boundary = {"effect_policy": "read_only", "deadline_at": deadline, "token_budget": 128} if deadline else {}
    params = RunParams(write_boundary=boundary)
    agent = SimpleNamespace(backend=backend, config=SimpleNamespace(max_tokens=512), _current_run_params=params)
    return SimpleNamespace(
        agent=agent, params=params, tools=None, messages=None, on_chunk=None, first_token_timeout_seconds=3.0,
        token_budget=model_request_token_budget(agent, input_tokens=12),
    )


def test_active_chat_stream_ends_at_the_profile_deadline():
    started = Event()
    payloads = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payloads.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                for _ in range(32):
                    self.wfile.write(b'data: {"choices":[{"delta":{"content":"result "}}]}\n\n')
                    self.wfile.flush()
                    started.set()
                    time.sleep(.05)
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as httpd:
        thread = Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            backend = OpenAICompatibleBackend(BackendOptions(
                f"http://127.0.0.1:{httpd.server_port}/v1", "test", "model", stream_enabled=True, max_tokens=512,
            ))
            with pytest.raises(ProviderTimeoutError) as exc:
                _generate_with_request_options(backend, "Inspect", _state(backend, deadline=time.time() + .8))
            assert exc.value.stage == "wall_clock"
            assert started.is_set()
            assert payloads[0]["max_tokens"] == 128
            assert _generate_with_request_options(backend, "Inspect", _state(backend)).text.strip()
            assert payloads[1]["max_tokens"] == 512
        finally:
            httpd.shutdown()
            thread.join(timeout=3)


def test_subscription_route_refuses_finite_output_ceiling_before_send(monkeypatch):
    backend = OpenAIResponsesBackend(BackendOptions(
        "https://example.test/v1", "", "model", stream_enabled=False, max_tokens=512,
    ))
    backend.auth_ref = {"mode": "chatgpt"}
    sent = []

    def response(_path, payload, _headers, **_options):
        sent.append(payload)
        return {"status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "OK"}]},
        ]}

    monkeypatch.setattr(backend, "request_json", response)
    with pytest.raises(RuntimeError, match="MODEL_CAPABILITY_MISMATCH") as exc:
        _generate_with_request_options(backend, "Inspect", _state(backend, deadline=time.time() + 10))
    assert exc.value.error_code == "MODEL_CAPABILITY_MISMATCH"
    assert exc.value.reason == "workflow_output_limit_unavailable"
    assert exc.value.effect_outcome == "not_started"
    assert exc.value.details["next_action"]
    assert sent == []

    assert _generate_with_request_options(backend, "Inspect", _state(backend)).text == "OK"
    assert len(sent) == 1 and "max_output_tokens" not in sent[0]


def test_retry_uses_remaining_deadline_and_expiry_does_not_send(monkeypatch):
    backend = OpenAICompatibleBackend(BackendOptions(
        "https://example.test/v1", "test", "model", stream_enabled=False, max_tokens=512,
    ))
    sent = []
    now = [100.0]
    monkeypatch.setattr(call_runtime, "time", SimpleNamespace(time=lambda: now[0]))

    def response(_path, payload, _headers, **options):
        sent.append((payload["max_tokens"], options.get("total_deadline_seconds")))
        return {"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]}

    monkeypatch.setattr(backend, "request_json", response)
    state = _state(backend, deadline=110.0)
    assert _generate_with_request_options(backend, "Inspect", state).text == "OK"
    now[0] = 107.0
    assert _generate_with_request_options(backend, "Inspect", state).text == "OK"
    now[0] = 110.0
    with pytest.raises(ProviderTimeoutError) as expired:
        _generate_with_request_options(backend, "Inspect", state)
    assert expired.value.stage == "wall_clock"
    assert sent == [(128, 10.0), (128, 3.0)]
