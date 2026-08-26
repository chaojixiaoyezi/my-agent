"""LLM: tests for model generation boundary behavior inside the tool loop.

给人看的解释：
这里测试的不是某个具体模型厂商，而是 my-agent 调模型的公共边界：
如果后端请求卡住，工具循环必须按 request_timeout 退出，方便父级后续恢复任务。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.tool_model_generation import (
    ModelGenerateParams,
    _effective_model_request_timeout_seconds,
    _publish_transport_retry,
    generate_model_response,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.backends.errors import ProviderContextWindowError, ProviderTimeoutError
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.tooling.content_transport_policy import RECOVERY_WRITE_CHUNK_CHARS
from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot


def test_transport_retry_projection_uses_structured_attempt_event() -> None:
    projected: list[dict[str, object]] = []

    class Sink:
        def write_provider_retry(self, **payload: object) -> bool:
            projected.append(dict(payload))
            return True

    _publish_transport_retry(
        Sink(),
        {
            "status": "failed",
            "retry_scheduled": True,
            "retry_attempt": 2,
            "retry_total": 3,
            "retry_wait_seconds": 5.0,
            "error_type": "URLError",
        },
    )
    _publish_transport_retry(
        Sink(),
        {"status": "failed", "retry_scheduled": False, "error_type": "URLError"},
    )

    assert projected == [
        {
            "scope": "transport",
            "attempt": 2,
            "total": 3,
            "delay_seconds": 5.0,
            "error_type": "URLError",
        }
    ]


class _BlockingBackend:
    name = "blocking-test-backend"

    def __init__(self) -> None:
        self.entered = threading.Event()

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.entered.set()
        time.sleep(0.08)
        return ModelResponse(text="late response", backend=self.name)


class _SubmissionInspectingBackend:
    name = "submission-inspecting-backend"

    def __init__(self, store: ConversationStore, dedupe_key: str) -> None:
        self.store = store
        self.dedupe_key = dedupe_key
        self.observed_status = ""

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        del prompt, on_chunk
        receipt = self.store.guidance_once_receipt(self.dedupe_key)
        self.observed_status = str(receipt.status if receipt is not None else "")
        assert receipt is not None and receipt.submission_id
        return ModelResponse(text="accepted", backend=self.name)


class _InterruptibleBlockingBackend:
    name = "interruptible-blocking-test-backend"

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.closed = threading.Event()

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        from agent_py_agent.agent.concurrency.interrupt import (
            is_interrupted,
            register_interrupt_callback,
        )

        del prompt, on_chunk
        with register_interrupt_callback(self.closed.set):
            self.entered.set()
            self.closed.wait(timeout=5)
            if is_interrupted():
                raise InterruptedError("模型传输已关闭")
        return ModelResponse(text="late response", backend=self.name)


class _StreamingLongWriteBackend:
    name = "streaming-long-write-test-backend"

    def __init__(self) -> None:
        self.chunks_emitted = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        parts = [
            '[TOOL_CALL]\n{"tool":"write_file","filesystem":{"path":"site/index.html","content":"',
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
            '[TOOL_CALL]\n{"tool":"write_file","path":"reports/final.md","content":"',
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
            '\n[TOOL_CALL]\n{"tool":"write_file","action":"append"',
            ',"session_id":"same","chunk_index":3,"content":"duplicate"}\n[/TOOL_CALL]',
        ]
        text = ""
        for part in parts:
            self.chunks_emitted += 1
            text += part
            if on_chunk is not None:
                on_chunk(part)
        return ModelResponse(text=text, backend=self.name)


class _StreamingToolThenProseBackend:
    name = "streaming-tool-then-prose-test-backend"

    def __init__(self) -> None:
        self.chunks_emitted = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        del prompt
        parts = [
            '[TOOL_CALL]\n{"tool":"read_file","path":"README.md"}\n[/TOOL_CALL]',
            "\nlate prose",
        ]
        for part in parts:
            self.chunks_emitted += 1
            if on_chunk is not None:
                on_chunk(part)
        return ModelResponse(text="".join(parts), backend=self.name)


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
        raise RuntimeError(
            "ordinary exception text mentions context length but is not a provider code"
        )


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
        text = '[TOOL_CALL]\n{"tool":"write_file","path":"outputs/report.md","content":"' + (
            "A" * 400
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


class _IdleTimeoutOwnedBackend(_TimeoutAwareBackend):
    stream_enabled = True
    stream_timeout_is_idle = True

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.seen_timeout = self.request_timeout
        time.sleep(0.03)
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
        write_boundary=None,
        task_attributes=None,
        request_id="",
        run_id="",
        task_id="",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
        tool_protocol_snapshot=make_test_protocol_snapshot(source_protocol="text"),
    )


def test_provider_boundary_commits_guidance_batch_before_backend_io(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    dedupe_key = "thread/provider-boundary"
    entry = store.append_guidance_once(
        {
            "target_type": "request",
            "target_id": "request-provider-boundary",
            "message": "模型触网前必须整批提交",
            "metadata": {
                "dedupe_key": dedupe_key,
                "expected_turn_id": "request-provider-boundary",
            },
        },
        dedupe_key=dedupe_key,
    )
    assert store.claim_guidance_once_for_turn(
        entry,
        expected_turn_id="request-provider-boundary",
        attempt_id="attempt-provider-boundary",
    )
    backend = _SubmissionInspectingBackend(store, dedupe_key)
    agent = SimpleNamespace(
        backend=backend,
        conversation_store=store,
        config=SimpleNamespace(request_timeout=10),
        _current_subagent_run_id="",
    )
    params = replace(
        _tool_loop_params(),
        request_id="request-provider-boundary",
        attempt_id="attempt-provider-boundary",
        live_archive_state={
            "_guidance_ack_ids": {entry.guidance_id},
            "_guidance_ack_entries": {entry.guidance_id: entry},
        },
    )

    response = generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=params,
            prompt="continue",
            tool_rounds=0,
        )
    )

    assert response.text == "accepted"
    assert backend.observed_status == "submitted"
    receipt = store.guidance_once_receipt(dedupe_key)
    assert receipt is not None and receipt.status == "submitted"
    assert params.live_archive_state["_guidance_submission_id"] == receipt.submission_id


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


def test_stream_transport_idle_timeout_is_not_reapplied_as_total_wall_timeout():
    backend = _IdleTimeoutOwnedBackend()
    agent = SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(request_timeout=0.01),
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


def test_model_generate_relays_named_task_interrupt_to_timeout_guard_thread():
    from agent_py_agent.agent.concurrency.interrupt import (
        interrupt_by_name,
        register_interruptible,
    )

    backend = _InterruptibleBlockingBackend()
    agent = SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(request_timeout=600),
        _current_subagent_run_id="",
    )
    outcome: dict[str, object] = {}

    def run_model_turn() -> None:
        try:
            with register_interruptible("real-model-turn-stop"):
                generate_model_response(
                    ModelGenerateParams(
                        agent=agent,
                        params=_tool_loop_params(),
                        prompt="hello",
                        tool_rounds=0,
                    )
                )
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=run_model_turn)
    thread.start()
    assert backend.entered.wait(timeout=2)

    started = time.monotonic()
    assert interrupt_by_name("real-model-turn-stop") is True
    thread.join(timeout=2)

    assert backend.closed.is_set()
    assert not thread.is_alive()
    assert isinstance(outcome.get("error"), InterruptedError)
    assert time.monotonic() - started < 2


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
    assert response.text == ""
    violation = response.tool_protocol_violations[0]
    assert violation["code"] == "TOOL_INLINE_CONTENT_STREAM_ABORTED"
    assert "inline content streaming exceeded" in violation["detail"]
    assert "site/index.html" not in str(response.tool_protocol_violations)


def test_model_generate_allows_unclosed_write_below_configured_stream_limit():
    backend = _StreamingRecoveryLongWriteBackend()
    agent = SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(request_timeout=10, tool_write_inline_max_chars=50_000),
        _current_subagent_run_id="",
    )
    params = _tool_loop_params()

    response = generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=params,
            prompt="continue writing report",
            tool_rounds=3,
        )
    )

    assert backend.chunks_emitted == 3
    assert response.tool_protocol_violations == []
    assert "reports/final.md" in response.text
    assert len(response.text) > RECOVERY_WRITE_CHUNK_CHARS


def test_model_generate_uses_configured_stream_limit_for_unclosed_write():
    backend = _StreamingRecoveryLongWriteBackend()
    agent = SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(request_timeout=10, tool_write_inline_max_chars=4_000),
        _current_subagent_run_id="",
    )
    params = _tool_loop_params()

    response = generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=params,
            prompt="continue writing report",
            tool_rounds=3,
        )
    )

    violation = response.tool_protocol_violations[0]
    evidence = json.loads(violation["evidence_preview"])
    assert 1 < backend.chunks_emitted < 4
    assert violation["code"] == "TOOL_INLINE_CONTENT_STREAM_ABORTED"
    assert evidence["streaming_content_limit"] == 4_000
    assert "reports/final.md" not in str(response.tool_protocol_violations)


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


def test_model_generate_rejects_unclosed_long_write_after_full_response():
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

    assert response.text == ""
    violation = response.tool_protocol_violations[0]
    evidence = json.loads(violation["evidence_preview"])
    assert violation["code"] == "TOOL_INLINE_CONTENT_STREAM_ABORTED"
    assert evidence["source_tool"] == "write_file"
    assert evidence["previous_write_committed"] is False
    assert "outputs/report.md" not in str(response.tool_protocol_violations)


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


def test_model_generate_does_not_strip_prose_to_promote_text_tool_block():
    backend = _StreamingToolThenProseBackend()
    agent = SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(request_timeout=10, tool_write_inline_max_chars=12_000),
        _current_subagent_run_id="",
    )

    response = generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=_tool_loop_params(),
            prompt="read source",
            tool_rounds=1,
        )
    )

    assert backend.chunks_emitted == 2
    assert response.text.endswith("\nlate prose")


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
    assert response.tool_protocol_violations == []
    assert "`[TOOL_CALL]`" in response.text
    assert "`[/TOOL_CALL]`" in response.text


def test_effective_model_timeout_uses_dynamic_config_only_when_present():
    legacy_agent = SimpleNamespace(
        config=SimpleNamespace(request_timeout=1), backend=SimpleNamespace()
    )
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


class _KwargRecordingBackend:
    name = "kwarg-recording-backend"

    def __init__(self) -> None:
        self.seen: list[dict] = []

    def generate(self, prompt: str, on_chunk=None, **kwargs) -> ModelResponse:
        self.seen.append({"prompt": prompt, **kwargs})
        return ModelResponse(text="ok", backend=self.name)


def _do_generate_with_tool_choice(backend, tool_choice):
    from agent_py_agent.agent.agent_core.tool_model_generation import _do_backend_generate

    state = SimpleNamespace(
        tools=[{"name": "remember", "description": "remember a fact"}],
        tool_choice=tool_choice,
        messages=[{"role": "user", "content": "hi"}],
        on_chunk=None,
    )
    return _do_backend_generate(backend, "prompt", state)


def test_forced_tool_choice_turn_disables_thinking_for_provider_compat():
    """LLM: 强制 tool_choice(specific/required/none)必须同时关思考——部分兼容端点(如
    工具运行时 zen)在思考模式下拒绝强制工具选择,回哑 400;与 generate_structured 同形态。"""
    from agent_py_agent.agent.tooling.runtime_contracts import ToolChoice

    for choice in (
        ToolChoice.specific("remember", "open_required_action_unique_tool"),
        ToolChoice.required("open_required_action"),
        ToolChoice.none("open_required_action_has_no_provider_tool"),
    ):
        backend = _KwargRecordingBackend()
        _do_generate_with_tool_choice(backend, choice)
        assert backend.seen[-1]["thinking_disabled"] is True
        assert backend.seen[-1]["tool_choice"] is choice


def test_auto_tool_choice_turn_keeps_original_request_shape():
    """auto 轮保持原请求形态,不传 thinking_disabled(对不识别该字段的端点零影响)。"""
    from agent_py_agent.agent.tooling.runtime_contracts import ToolChoice

    backend = _KwargRecordingBackend()
    _do_generate_with_tool_choice(backend, ToolChoice.auto("ordinary_tool_turn"))
    assert "thinking_disabled" not in backend.seen[-1]


def test_isolated_presentation_turn_does_not_stream_thinking_delta():
    from agent_py_agent.agent.agent_core.tool_model_generation import _do_backend_generate
    from agent_py_agent.agent.tooling.runtime_contracts import ToolChoice

    class Sink:
        def write_thinking_delta(self, _text: str) -> None:
            raise AssertionError("presentation reasoning must stay private")

    backend = _KwargRecordingBackend()
    state = SimpleNamespace(
        tools=[],
        tool_choice=ToolChoice.auto("presentation"),
        messages=None,
        on_chunk=None,
        params=SimpleNamespace(
            context_scope="isolated",
            effective_on_chunk=Sink(),
        ),
    )

    _do_backend_generate(backend, "prompt", state)

    assert "on_thinking_delta" not in backend.seen[-1]


def test_native_tool_turn_passes_tool_input_progress_only_to_capable_backend():
    from agent_py_agent.agent.agent_core.tool_model_generation import _do_backend_generate
    from agent_py_agent.agent.tooling.runtime_contracts import ToolChoice

    class Sink:
        def write_tool_input_progress(self, _value: object) -> None:
            return None

    class CapableBackend(_KwargRecordingBackend):
        supports_tool_input_progress = True

    state = SimpleNamespace(
        tools=[{"name": "write_file", "description": "write"}],
        tool_choice=ToolChoice.auto("native"),
        messages=[],
        on_chunk=None,
        params=SimpleNamespace(
            context_scope="shared",
            effective_on_chunk=Sink(),
        ),
    )
    capable = CapableBackend()
    _do_backend_generate(capable, "prompt", state)
    assert callable(capable.seen[-1]["on_tool_input_progress"])

    ordinary = _KwargRecordingBackend()
    _do_backend_generate(ordinary, "prompt", state)
    assert "on_tool_input_progress" not in ordinary.seen[-1]
