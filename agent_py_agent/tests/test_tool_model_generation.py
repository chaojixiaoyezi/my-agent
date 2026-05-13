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
