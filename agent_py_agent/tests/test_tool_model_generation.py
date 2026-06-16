"""LLM: tests for model generation boundary behavior inside the tool loop.

给人看的解释：
这里测试的不是某个具体模型厂商，而是 my-agent 调模型的公共边界：
如果后端请求卡住，工具循环必须按 request_timeout 退出，方便父级后续恢复任务。
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import tool_model_generation as tool_model_generation_module
from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.tool_model_generation import (
    ModelGenerateParams,
    _effective_model_request_timeout_seconds,
    generate_model_response,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.backends.errors import ProviderContextWindowError, ProviderTimeoutError
from agent_py_agent.agent.tooling.content_transport_policy import RECOVERY_WRITE_CHUNK_CHARS


class _BlockingBackend:
    name = "blocking-test-backend"

    def __init__(self) -> None:
        self.entered = threading.Event()

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.entered.set()
        time.sleep(0.08)
        return ModelResponse(text="late response", backend=self.name)


class _StreamingLongWriteBackend:
    name = "streaming-long-write-test-backend"

    def __init__(self) -> None:
        self.chunks_emitted = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        parts = [
            '[TOOL_CALL]\n'
            '{"tool":"write_file","filesystem":{"path":"site/index.html","content":"',
            "A" * 128,
            "B" * 128,
            "C" * 128,
        ]
        text = ""
        for part in parts:
            self.chunks_emitted += 1
            text += part
            if on_chunk is not None:
                on_chunk(part)
        return ModelResponse(text=text, backend=self.name)


class _StreamingRecoveryLongWriteBackend:
    name = "streaming-recovery-long-write-test-backend"

    def __init__(self) -> None:
        self.chunks_emitted = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        parts = [
            '[TOOL_CALL]\n'
            '{"tool":"write_file","path":"reports/final.md","content":"',
            "A" * (RECOVERY_WRITE_CHUNK_CHARS + 1),
            "B" * 5000,
        ]
        text = ""
        for part in parts:
            self.chunks_emitted += 1
            text += part
            if on_chunk is not None:
                on_chunk(part)
        return ModelResponse(text=text, backend=self.name)


class _StreamingRepeatedToolBackend:
    name = "streaming-repeated-tool-test-backend"

    def __init__(self) -> None:
        self.chunks_emitted = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        parts = [
            '[TOOL_CALL]\n{"tool":"read_file","path":"final.html"}\n[/TOOL_CALL]',
            "\n[TOOL_CALL]\n{\"tool\":\"write_file\",\"action\":\"append\"",
            ',"session_id":"same","chunk_index":3,"content":"duplicate"}\n[/TOOL_CALL]',
        ]
        text = ""
        for part in parts:
            self.chunks_emitted += 1
            text += part
            if on_chunk is not None:
                on_chunk(part)
        return ModelResponse(text=text, backend=self.name)


class _StreamingCompleteThenStallBackend:
    name = "streaming-complete-then-stall-test-backend"

    def __init__(self) -> None:
        self.chunks_emitted = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        tool_text = '[TOOL_CALL]\n{"tool":"read_file","path":"README.md"}\n[/TOOL_CALL]'
        self.chunks_emitted += 1
        if on_chunk is not None:
            on_chunk(tool_text)
        time.sleep(0.2)
        return ModelResponse(text=f"{tool_text}\nlate prose", backend=self.name)


class _StreamingLongFileWriteSessionBackend:
    name = "streaming-long-file-write-session-test-backend"

    def __init__(self) -> None:
        self.chunks_emitted = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        parts = [
            '[TOOL_CALL]\n'
            '{"tool":"write_file","action":"append",'
            '"session_id":"homepage-v1","chunk_index":2,"content":"',
            "hello\\n",
            "world" * 30_000,
        ]
        text = ""
        for part in parts:
            self.chunks_emitted += 1
            text += part
            if on_chunk is not None:
                on_chunk(part)
        return ModelResponse(text=text, backend=self.name)


class _StreamingTokenBackend:
    name = "streaming-token-test-backend"
    model_name = "test-model"
    max_tokens = 64

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        if on_chunk is not None:
            on_chunk("hello")
        return ModelResponse(text="hello world", backend=self.name)


class _PlainRuntimeContextTextBackend:
    name = "plain-runtime-context-text-test-backend"

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        raise RuntimeError("ordinary exception text mentions context length but is not a provider code")


class _ProviderContextWindowBackend:
    name = "provider-context-window-test-backend"

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        raise ProviderContextWindowError("HTTP 400: prompt too long")


class _StreamingLiteralProtocolMarkerContentBackend:
    name = "streaming-literal-protocol-marker-content-test-backend"

    def __init__(self) -> None:
        self.chunks_emitted = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        payload = {
            "tool": "write_file",
            "path": "outputs/report.md",
            "content": (
                "# 报告\n\n"
                "正文会原样提到 `[TOOL_CALL]` 和 `[/TOOL_CALL]`，"
                "它们只是文档内容，不是新的工具调用。"
            ),
        }
        text = "[TOOL_CALL]\n" + json.dumps(payload, ensure_ascii=False) + "\n[/TOOL_CALL]"
        parts = [
            text[:80],
            text[80:140],
            text[140:],
        ]
        for part in parts:
            self.chunks_emitted += 1
            if on_chunk is not None:
                on_chunk(part)
        return ModelResponse(text=text, backend=self.name)


class _NonStreamingUnclosedLongWriteBackend:
    name = "non-streaming-unclosed-long-write-test-backend"

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        del prompt, on_chunk
        text = (
            '[TOOL_CALL]\n'
            '{"tool":"write_file","path":"outputs/report.md","content":"'
            + ("A" * 400)
        )
        return ModelResponse(text=text, backend=self.name)


class _TimeoutAwareBackend:
    name = "timeout-aware-test-backend"
    model_name = "test-model"
    max_tokens = 1200

    def __init__(self) -> None:
        self.request_timeout = 1
        self.seen_timeout = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.seen_timeout = self.request_timeout
        if on_chunk is not None:
            on_chunk("ok")
        return ModelResponse(text="ok", backend=self.name)


def _tool_loop_params() -> ToolLoopExecuteParams:
    return ToolLoopExecuteParams(
        user_prompt="",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        granted_capabilities=None,
        write_boundary=None,
        task_attributes=None,
        request_id="",
        run_id="",
        task_id="",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
    )


class _DigestTurnTimeoutBackend:
    name = "digest-turn-timeout-test-backend"
    # digest 轮判定要从 backend 读 context_window_tokens 算 trigger/ceiling，缺它则 window=0、
    # preflight 直接返回 None 而不进 digest 分支——测不到 digest 路径。这里显式给上。
    context_window_tokens = 1000

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        del prompt, on_chunk
        raise ProviderTimeoutError("模型接口请求超时: request_timeout=1s")


class _DigestTurnGenericErrorBackend:
    name = "digest-turn-generic-error-test-backend"
    context_window_tokens = 1000

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        del prompt, on_chunk
        raise RuntimeError("某个非上下文窗口的一般异常")


class _DigestTurnOkBackend:
    name = "digest-turn-ok-test-backend"
    context_window_tokens = 1000

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        del prompt, on_chunk
        return ModelResponse(text="digest summary ok", backend=self.name)


def _digest_turn_agent(backend) -> SimpleNamespace:
    from agent_py_agent.agent.settings import AgentConfig

    return SimpleNamespace(
        config=AgentConfig(auto_save_memory=True, request_timeout=0),
        backend=backend,
        _current_subagent_run_id="",
    )


def _digest_turn_params() -> ToolLoopExecuteParams:
    params = _tool_loop_params()
    # digest 轮的前置：有可摘要的工具结果历史 + pending 标记。preflight 在 prompt 落进
    # [trigger, 90% ceiling) 时放行一轮 digest（返回 None 并置 inflight），不直接 compact。
    params.tool_context.append("[tool-record round=1 index=1]\nread_file 历史")
    params.live_archive_state["pending_tool_context_digest"] = True
    return params


def _assert_digest_turn_admitted(agent, params, *, tool_rounds: int) -> None:
    from agent_py_agent.agent.agent_core.model.context_pressure import preflight_context_pressure_response

    request = ModelGenerateParams(agent=agent, params=params, prompt="系统上下文" * 160, tool_rounds=tool_rounds)
    assert preflight_context_pressure_response(request) is None
    assert params.live_archive_state.get("tool_context_digest_inflight") is True


def test_digest_turn_provider_timeout_clears_digest_marks():
    # H3：digest 轮抛 ProviderTimeoutError 时，try/finally 必须清掉 digest_inflight/pending，
    # 否则 _has_pending_tool_context_digest 永远为真，preflight 永远走 digest 分支、再不发
    # context_overflow，compact 永久卡死。
    backend = _DigestTurnTimeoutBackend()
    agent = _digest_turn_agent(backend)
    params = _digest_turn_params()
    _assert_digest_turn_admitted(agent, params, tool_rounds=3)

    with pytest.raises(ProviderTimeoutError):
        generate_model_response(
            ModelGenerateParams(agent=agent, params=params, prompt="系统上下文" * 160, tool_rounds=3)
        )

    assert params.live_archive_state.get("tool_context_digest_inflight") in (None, False)
    assert params.live_archive_state.get("pending_tool_context_digest") in (None, False)


def test_digest_turn_failure_unblocks_next_preflight_compact():
    # H3 端到端口径：digest 轮失败后，下一次 preflight 不再被卡在 digest 分支，能正常发
    # context_overflow（落回 compact/resume），证明永久堵死被解除。
    backend = _DigestTurnGenericErrorBackend()
    agent = _digest_turn_agent(backend)
    params = _digest_turn_params()
    _assert_digest_turn_admitted(agent, params, tool_rounds=3)

    with pytest.raises(RuntimeError, match="一般异常"):
        generate_model_response(
            ModelGenerateParams(agent=agent, params=params, prompt="系统上下文" * 160, tool_rounds=3)
        )

    from agent_py_agent.agent.agent_core.model.context_pressure import preflight_context_pressure_response

    next_request = ModelGenerateParams(agent=agent, params=params, prompt="系统上下文" * 160, tool_rounds=4)
    response = preflight_context_pressure_response(next_request)

    assert response is not None
    assert response.runtime_status == "context_overflow"
    assert response.runtime_source == "preflight"


def test_digest_turn_success_still_consumes_marks():
    # 不退化：digest 轮成功完成时仍要消费标记（finally 与成功路径共同保证，且幂等）。
    backend = _DigestTurnOkBackend()
    agent = _digest_turn_agent(backend)
    params = _digest_turn_params()
    _assert_digest_turn_admitted(agent, params, tool_rounds=3)

    response = generate_model_response(
        ModelGenerateParams(agent=agent, params=params, prompt="系统上下文" * 160, tool_rounds=3)
    )

    assert response.text == "digest summary ok"
    assert params.live_archive_state.get("tool_context_digest_inflight") in (None, False)
    assert params.live_archive_state.get("pending_tool_context_digest") in (None, False)


def test_model_generate_enforces_request_timeout_when_backend_blocks():
    backend = _BlockingBackend()
    agent = SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(request_timeout=0.01),
        _current_subagent_run_id="",
    )

    started = time.monotonic()
    with pytest.raises(ProviderTimeoutError, match=r"request_timeout=0.01s"):
        generate_model_response(
            ModelGenerateParams(
                agent=agent,
                params=_tool_loop_params(),
                prompt="hello",
                tool_rounds=0,
            )
        )

    assert backend.entered.is_set()
    assert time.monotonic() - started < 0.06


def test_model_generate_aborts_streaming_write_file_content_over_inline_limit():
    backend = _StreamingLongWriteBackend()
    agent = SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(request_timeout=10, tool_write_inline_max_chars=200),
        _current_subagent_run_id="",
    )

    response = generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=_tool_loop_params(),
            prompt="build a shopping site",
            tool_rounds=0,
        )
    )

    assert 1 < backend.chunks_emitted < 5
    assert response.backend == backend.name
    assert '"tool": "__parse_error__"' in response.text
    assert "inline content streaming exceeded" in response.text
    assert "site/index.html" in response.text


def test_model_generate_does_not_use_recovery_chunk_as_stream_abort_limit():
    backend = _StreamingRecoveryLongWriteBackend()
    agent = SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(request_timeout=10, tool_write_inline_max_chars=50_000),
        _current_subagent_run_id="",
    )
    params = _tool_loop_params()
    params.archive_tool_calls.append(
        {
            "tool": "__parse_error__",
            "error_code": "TOOL_CALL_UNCLOSED",
            "parameters": {
                "tool": "__parse_error__",
                "error_code": "TOOL_CALL_UNCLOSED",
                "source_tool": "write_file",
                "path": "reports/final.md",
                "content_field_present": True,
                "write_recovery": {"strategy": "restart_same_file_with_append_chunks"},
            },
        }
    )

    response = generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=params,
            prompt="continue writing report",
            tool_rounds=3,
        )
    )

    assert backend.chunks_emitted == 3
    assert '"tool": "__parse_error__"' not in response.text
    assert "reports/final.md" in response.text
    assert len(response.text) > RECOVERY_WRITE_CHUNK_CHARS


def test_model_generate_uses_configured_stream_limit_during_long_write_recovery():
    backend = _StreamingRecoveryLongWriteBackend()
    agent = SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(request_timeout=10, tool_write_inline_max_chars=4_000),
        _current_subagent_run_id="",
    )
    params = _tool_loop_params()
    params.archive_tool_calls.append(
        {
            "tool": "__parse_error__",
            "error_code": "TOOL_CALL_UNCLOSED",
            "parameters": {
                "tool": "__parse_error__",
                "error_code": "TOOL_CALL_UNCLOSED",
                "source_tool": "write_file",
                "path": "reports/final.md",
                "content_field_present": True,
                "write_recovery": {"strategy": "restart_same_file_with_append_chunks"},
            },
        }
    )

    response = generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=params,
            prompt="continue writing report",
            tool_rounds=3,
        )
    )

    payload = json.loads(response.text.split("\n", 2)[1])
    assert 1 < backend.chunks_emitted < 4
    assert payload["tool"] == "__parse_error__"
    assert payload["error_code"] == "TOOL_INLINE_CONTENT_STREAM_ABORTED"
    assert payload["path"] == "reports/final.md"
    assert payload["streaming_content_limit"] == 4_000


def test_model_generate_does_not_compact_from_plain_exception_text():
    backend = _PlainRuntimeContextTextBackend()
    agent = SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(request_timeout=10),
        _current_subagent_run_id="",
    )

    with pytest.raises(RuntimeError, match="context length"):
        generate_model_response(
            ModelGenerateParams(
                agent=agent,
                params=_tool_loop_params(),
                prompt="hello",
                tool_rounds=0,
            )
        )


def test_model_generate_compacts_from_typed_provider_context_window_error():
    backend = _ProviderContextWindowBackend()
    agent = SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(request_timeout=10),
        _current_subagent_run_id="",
    )

    response = generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=_tool_loop_params(),
            prompt="hello",
            tool_rounds=0,
        )
    )

    assert response.runtime_status == "context_overflow"
    assert response.runtime_source == "provider_error"


def test_model_generate_salvages_streaming_write_file_append_prefix():
    backend = _StreamingLongFileWriteSessionBackend()
    agent = SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(request_timeout=10, tool_write_inline_max_chars=120),
        _current_subagent_run_id="",
    )

    response = generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=_tool_loop_params(),
            prompt="build furniture homepage",
            tool_rounds=1,
        )
    )

    assert 1 < backend.chunks_emitted < 4
    assert '"tool": "__parse_error__"' in response.text
    assert "homepage-v1" in response.text
    assert "inline content streaming exceeded" in response.text


def test_model_generate_recovers_unclosed_long_write_after_full_response():
    backend = _NonStreamingUnclosedLongWriteBackend()
    agent = SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(request_timeout=0, tool_write_inline_max_chars=120),
        _current_subagent_run_id="",
    )

    response = generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=_tool_loop_params(),
            prompt="write long report",
            tool_rounds=1,
        )
    )

    payload = json.loads(response.text.split("\n", 2)[1])
    assert payload["tool"] == "__parse_error__"
    assert payload["error_code"] == "TOOL_INLINE_CONTENT_STREAM_ABORTED"
    assert payload["source_tool"] == "write_file"
    assert payload["path"] == "outputs/report.md"
    assert payload["previous_write_committed"] is False
    assert payload["write_recovery"]["first_tool_call"]["mode"] == "overwrite"
    assert payload["write_recovery"]["next_tool_call"]["mode"] == "append"


def test_model_generate_keeps_all_complete_streaming_tool_blocks():
    backend = _StreamingRepeatedToolBackend()
    agent = SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(request_timeout=10, tool_write_inline_max_chars=12_000),
        _current_subagent_run_id="",
    )

    response = generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=_tool_loop_params(),
            prompt="read final output",
            tool_rounds=3,
        )
    )

    assert backend.chunks_emitted == 3
    assert response.text == (
        '[TOOL_CALL]\n{"tool":"read_file","path":"final.html"}\n[/TOOL_CALL]\n'
        '[TOOL_CALL]\n{"tool":"write_file","action":"append","session_id":"same","chunk_index":3,"content":"duplicate"}\n[/TOOL_CALL]'
    )


def test_model_generate_returns_complete_tool_block_before_stream_finishes(monkeypatch):
    monkeypatch.setattr(tool_model_generation_module, "_TOOL_STREAM_COMPLETE_DRAIN_SECONDS", 0.01)
    monkeypatch.setattr(tool_model_generation_module, "_TOOL_STREAM_POLL_SECONDS", 0.005)
    backend = _StreamingCompleteThenStallBackend()
    agent = SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(request_timeout=10, tool_write_inline_max_chars=12_000),
        _current_subagent_run_id="",
    )

    started = time.monotonic()
    response = generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=_tool_loop_params(),
            prompt="read source",
            tool_rounds=1,
        )
    )

    assert time.monotonic() - started < 0.1
    assert backend.chunks_emitted == 1
    assert response.text == '[TOOL_CALL]\n{"tool":"read_file","path":"README.md"}\n[/TOOL_CALL]'


def test_model_generate_records_model_call_ledger_for_streaming_response():
    backend = _StreamingTokenBackend()
    agent = SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(
            request_timeout=10,
            dynamic_timeout_min=1,
            dynamic_timeout_max=20,
            dynamic_timeout_safety_margin=1.2,
        ),
        _current_subagent_run_id="",
    )

    response = generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=_tool_loop_params(),
            prompt="hello",
            tool_rounds=2,
        )
    )

    records = agent._model_call_ledger.records()
    assert response.text == "hello world"
    assert len(records) == 1
    assert records[0].status == "finished"
    assert records[0].events == ("started", "first_token", "finished")
    assert records[0].backend == backend.name
    assert records[0].model == "test-model"
    assert records[0].metadata["tool_rounds"] == 2
    assert "first_token_timeout_estimate" in records[0].metadata


def test_model_generate_keeps_literal_protocol_markers_inside_write_content():
    backend = _StreamingLiteralProtocolMarkerContentBackend()
    agent = SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(request_timeout=10, tool_write_inline_max_chars=12_000),
        _current_subagent_run_id="",
    )

    response = generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=_tool_loop_params(),
            prompt="write report",
            tool_rounds=1,
        )
    )

    assert backend.chunks_emitted >= 2
    assert '"tool": "__parse_error__"' not in response.text
    assert "`[TOOL_CALL]`" in response.text
    assert "`[/TOOL_CALL]`" in response.text


def test_effective_model_timeout_uses_dynamic_config_only_when_present():
    legacy_agent = SimpleNamespace(config=SimpleNamespace(request_timeout=1), backend=SimpleNamespace())
    dynamic_agent = SimpleNamespace(
        config=SimpleNamespace(
            request_timeout=1,
            dynamic_timeout_min=1,
            dynamic_timeout_max=60,
            dynamic_timeout_safety_margin=2,
        ),
        backend=SimpleNamespace(),
    )

    assert _effective_model_request_timeout_seconds(legacy_agent, 30) == 1
    assert _effective_model_request_timeout_seconds(dynamic_agent, 30) == 30


def test_effective_model_timeout_includes_output_generation_budget():
    dynamic_agent = SimpleNamespace(
        config=SimpleNamespace(
            request_timeout=1,
            dynamic_timeout_min=1,
            dynamic_timeout_max=120,
            dynamic_timeout_safety_margin=2,
        ),
        backend=SimpleNamespace(max_tokens=1200),
    )

    assert _effective_model_request_timeout_seconds(dynamic_agent, 30) == 70


def test_model_generate_applies_dynamic_timeout_to_backend_request():
    backend = _TimeoutAwareBackend()
    agent = SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(
            request_timeout=1,
            dynamic_timeout_min=1,
            dynamic_timeout_max=120,
            dynamic_timeout_safety_margin=2,
        ),
        _current_subagent_run_id="",
    )

    response = generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=_tool_loop_params(),
            prompt="hello",
            tool_rounds=0,
        )
    )

    assert response.text == "ok"
    assert backend.seen_timeout > 40
    assert backend.request_timeout == 1
