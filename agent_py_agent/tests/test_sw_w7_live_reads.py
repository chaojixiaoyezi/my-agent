"""SW-W7：正式装配后的同一实例读取 live 开关；HTTP 只访问回环协议夹具。"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends.base import BackendOptions
from agent_py_agent.agent.backends.factory import get_backend
from agent_py_agent.agent.backends.openai_chat import OpenAICompatibleBackend
from agent_py_agent.agent.prompting_parts.thread_persona import thread_persona_scope
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling._filesystem_read import FileSystemAccessOptions
from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool, WriteFileToolOptions
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams
from agent_py_agent.tests.test_thread_persona_freeze import (
    prefix_and_updates,
    request,
)
from agent_py_agent.tests.test_thread_persona_freeze import (
    scene as scene,
)


def _response_body(path):
    if path.endswith("/responses"):
        return {"id": "response-test", "status": "completed", "output": [{
            "id": "message-test", "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": "ok", "annotations": []}],
        }]}
    if path.endswith("/messages"):
        return {"id": "message-test", "type": "message", "role": "assistant",
                "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1}}
    return {"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]}


def _stream_frames(path, body):
    if path.endswith("/responses"):
        return [{"type": "response.completed", "response": body}]
    if path.endswith("/messages"):
        return [
            {"type": "message_start", "message": body | {"content": [], "stop_reason": None}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
            {"type": "message_stop"},
        ]
    return [{"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]


@pytest.fixture
def provider_http():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, payload))
            body = _response_body(self.path)
            if payload.get("stream"):
                frames = _stream_frames(self.path, body)
                raw = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames).encode()
                if self.path.endswith("/chat/completions"):
                    raw += b"data: [DONE]\n\n"
                content_type = "text/event-stream"
            else:
                raw, content_type = json.dumps(body).encode(), "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", calls
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        assert not worker.is_alive()


@pytest.mark.parametrize("name", ["openai_compatible", "openai_responses", "anthropic_compatible"])
@pytest.mark.parametrize("initial", [False, True])
def test_factory_backend_next_request_reads_live_stream_switch(provider_http, name, initial):
    base, calls = provider_http
    config = AgentConfig(api_base=base, api_key="unit-test-placeholder", model_name="fixture-model",
                         stream_enabled=initial, model_reasoning_control="none")
    backend = get_backend(name, config)
    for enabled in (initial, not initial, initial):
        config.stream_enabled = enabled
        response = backend.generate("仅回复 ok")
        assert response.text == "ok"
        assert bool(calls[-1][1].get("stream")) is enabled
        assert backend.stream_timeout_is_idle is enabled
    assert len(calls) == 3


def test_standalone_backend_keeps_explicit_options(provider_http):
    base, calls = provider_http
    options = BackendOptions(api_base=base, api_key="unit-test-placeholder", model_name="fixture-model",
                             stream_enabled=False)
    backend = OpenAICompatibleBackend(options)
    assert backend.generate("ok").text == "ok"
    assert not calls[-1][1].get("stream")
    assert backend.stream_timeout_is_idle is False


@pytest.mark.parametrize("name,mode", [
    ("openai_compatible", "oauth"), ("anthropic_compatible", "oauth"),
    ("openai_responses", "oauth"), ("openai_responses", "chatgpt"),
])
def test_oauth_binding_keeps_live_switch_or_required_protocol_stream(name, mode):
    # 只构造无秘密的假引用；不解析凭据、不发认证或模型请求。
    config = AgentConfig(api_base="https://fixture.invalid/v1", api_key="", model_name="fixture-model")
    config.model_auth_ref = dict(path="unused", provider_id="fixture", generation="one", binding="one", mode=mode)
    backend = get_backend(name, config)
    for enabled in (False, True, False):
        config.stream_enabled = enabled
        expected = enabled or mode == "chatgpt"
        assert backend.stream_enabled is expected
        assert backend.stream_timeout_is_idle is expected
        assert config.stream_enabled is enabled


@pytest.mark.parametrize("operation", ["write_file", "edit_file", "apply_patch"])
@pytest.mark.parametrize("initial", [False, True])
def test_registry_file_tools_next_call_reads_live_diagnostics(tmp_path, operation, initial):
    config = AgentConfig(enable_file_syntax_diagnostics=initial)
    registry = ToolRegistry(ToolRegistryParams(
        workspace_root=tmp_path, max_chars=1000, max_entries=10, max_matches=10,
        web_max_chars=1000, http_timeout=1, catalog_limit=100, retrieval_limit=10,
        vector_search_enabled=False, live_config=config,
    ))
    tool = registry.tools[operation]
    reader = registry.tools["read_file"]
    try:
        for enabled in (initial, not initial, initial):
            config.enable_file_syntax_diagnostics = enabled
            path = tmp_path / "value.json"
            path.write_text("{}\n", encoding="utf-8")
            arguments = {
                "write_file": {"path": "value.json", "content": "{\n"},
                "edit_file": {"path": "value.json", "old_string": "{}", "new_string": "{"},
                "apply_patch": {"patch": "*** Begin Patch\n*** Update File: value.json\n-{}\n+{\n*** End Patch"},
            }[operation]
            result = tool.execute(arguments)
            assert result.ok, result.output
            assert path.read_bytes() == b"{\n"
            assert ("syntax_diagnostics" in result.result_envelope) is enabled
            if enabled:
                assert result.result_envelope["syntax_diagnostics"]["observations"][0]["status"] == "invalid"
            assert reader.enable_file_syntax_diagnostics is enabled
            assert reader.execute({"path": "value.json"}).ok
            assert registry.tools[operation] is tool
    finally:
        registry.close_mcp_clients()


@pytest.mark.parametrize("enabled", [False, True])
def test_standalone_file_tool_keeps_explicit_options(tmp_path, enabled):
    access = FileSystemAccessOptions(enable_file_syntax_diagnostics=enabled)
    tool = WriteFileTool(tmp_path, options=WriteFileToolOptions(access_options=access))
    result = tool.execute({"path": "value.json", "content": "{"})
    assert result.ok
    assert ("syntax_diagnostics" in result.result_envelope) is enabled


def test_legacy_flag_cannot_disable_thread_persona_freeze(scene):
    builder, paths, _ = scene
    builder.config.thread_prompt_prefix_freeze_enabled = False
    first, _ = prefix_and_updates(builder, request())
    paths["agents"].write_text("- 当轮变化\n", encoding="utf-8")
    stable, updates = prefix_and_updates(builder, request())
    assert stable == first
    assert "当轮变化" in updates


def test_legacy_flag_cannot_drop_canonical_thread_scope():
    config = AgentConfig()
    config.thread_prompt_prefix_freeze_enabled = False
    thread = SimpleNamespace(compact_generation=3, model_profile_id="model-one")
    agent = SimpleNamespace(config=config, conversation_store=SimpleNamespace(
        threads=SimpleNamespace(load=lambda _thread: thread)))
    params = SimpleNamespace(task_attributes={"conversation_thread_id": "thread-one"},
                             tool_protocol_snapshot=SimpleNamespace(source_protocol="native"))
    scope = thread_persona_scope(agent, params)
    assert scope is not None
    assert scope.thread_id == "thread-one" and scope.compact_generation == 3
