"""LLM: tests for model generation boundary behavior inside the tool loop.

给人看的解释：
这里测试的不是某个具体模型厂商，而是 my-agent 调模型的公共边界：
如果后端请求卡住，工具循环必须按 request_timeout 退出，方便父级后续恢复任务。
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.tool_model_generation import (
    ModelGenerateParams,
    _effective_model_request_timeout_seconds,
    generate_model_response,
)
from agent_py_agent.agent.backend import ModelResponse
from agent_py_agent.agent.backends.errors import ProviderTimeoutError


# LLM: _BlockingBackend simulates a provider call that ignores socket-level timeouts.
# 类用途: 测试专用模型后端；generate 会短暂卡住，用来复现真实 E2E 中 root runner 长时间 RUNNING 的情况。
class _BlockingBackend:
    name = "blocking-test-backend"

    # LLM: __init__ exposes an event so tests can verify the backend was actually entered.
    # 函数用途: 初始化测试事件；没有外部 I/O，只用于确认 generate 已被调用。
    def __init__(self) -> None:
        self.entered = threading.Event()

    # LLM: generate blocks longer than the configured request timeout and then returns late.
    # 函数用途: 模拟模型服务持续不返回的情况；正常逻辑应该在返回前就触发 ProviderTimeoutError。
    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.entered.set()
        time.sleep(0.08)
        return ModelResponse(text="late response", backend=self.name)


# LLM: _StreamingLongWriteBackend simulates a model trying to emit a whole site in one tool call.
# 类用途: 测试专用模型后端；它持续流出未闭合 write_file content，公共边界应提前打断而不是等超时。
class _StreamingLongWriteBackend:
    name = "streaming-long-write-test-backend"

    # LLM: __init__ tracks how far the fake stream advanced before the boundary interrupted it.
    # 函数用途: 初始化测试计数器；用于证明系统没有等完整超长工具调用输出完。
    def __init__(self) -> None:
        self.chunks_emitted = 0

    # LLM: generate emits an invalid oversized write stream that used to end as provider_timeout.
    # 函数用途: 模拟模型把完整网页塞进一次 write_file content 且迟迟不闭合工具调用。
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


# LLM: _StreamingTokenBackend emits one chunk so tests can verify first-token ledger facts.
# 类用途: 测试专用模型后端；通过 on_chunk 模拟真实流式首 token 到达。
class _StreamingTokenBackend:
    name = "streaming-token-test-backend"
    model_name = "test-model"
    max_tokens = 64

    # LLM: generate emits a normal chunk and returns a final response.
    # 函数用途: 模拟一次成功模型调用，供账本测试读取 first_token 和 finished 事件。
    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        if on_chunk is not None:
            on_chunk("hello")
        return ModelResponse(text="hello world", backend=self.name)


# LLM: _TimeoutAwareBackend records the backend request_timeout visible during generate.
# 类用途: 测试动态 timeout 是否真正传入 HTTP backend 层，而不是只停在外层 guard。
class _TimeoutAwareBackend:
    name = "timeout-aware-test-backend"
    model_name = "test-model"
    max_tokens = 1200

    # LLM: __init__ starts with an unrealistically low backend timeout to expose missing overrides.
    # 函数用途: 初始化 request_timeout 和观测字段，验证 generate 期间能看到动态预算。
    def __init__(self) -> None:
        self.request_timeout = 1
        self.seen_timeout = 0

    # LLM: generate captures request_timeout and returns immediately.
    # 函数用途: 不发网络请求，只记录调用期间后端超时字段是否被提升。
    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.seen_timeout = self.request_timeout
        if on_chunk is not None:
            on_chunk("ok")
        return ModelResponse(text="ok", backend=self.name)


# LLM: _tool_loop_params returns the smallest valid tool-loop bundle for generation tests.
# 函数用途: 构造 generate_model_response 所需参数包，避免每个测试重复填一长串字段。
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


# LLM: model generation must have a wall-clock guard above individual backend implementations.
# 函数用途: 确认 backend.generate 自己卡住时，公共模型调用边界会按 request_timeout 抛出可恢复超时。
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


# LLM: large write_file payloads fail before the tool layer can help unless the stream boundary intervenes.
# 函数用途: 复现购物站点类失败：模型把大 HTML 塞进未闭合 write_file，系统应转入分块恢复而不是等待 request_timeout。
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
    assert "write_file.content inline content streaming exceeded" in response.text
    assert "site/index.html" in response.text


# LLM: model generation should leave structured timing facts for timeout tuning and recovery.
# 函数用途: 验证正常流式模型调用会写入 started/first_token/finished 账本，不依赖响应自然语言。
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


# LLM: dynamic model timeout extends only agents that expose dynamic timeout config.
# 函数用途: 验证旧 fake agent 仍只用 request_timeout，而真实配置可按首 token 估算抬高预算。
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


# LLM: Wall timeout must reserve output generation time, not only prefill/first-token time.
# 函数用途: 验证动态模型请求总超时会把 max_tokens 对应的输出时间纳入预算，避免长回复被 240s 墙过早杀掉。
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


# LLM: Dynamic timeout must reach the backend stream deadline, not only the outer guard thread.
# 函数用途: 验证模型调用边界会把结构化动态总超时写进后端请求层，防止流式 SSE 仍按旧 240s 截断。
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
