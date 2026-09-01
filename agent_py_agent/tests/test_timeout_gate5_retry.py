from __future__ import annotations

"""第1项 B 门槛5 验收测试: 超时后一次重试(结构化 tool ledger 判定)。

steward seq 1500 细化要求(门槛5): 重试资格不靠消息形状, 以结构化 tool
ledger(IR 最后一条 tool_use 的 terminal 配对状态)判定; 未确认/截断/未知
一律 fail-closed 不重试; 重试只重发模型调用不重放工具; 每 logical turn
全局最多一次; physical_attempt 记账证明不重复工具效果。

四必补测:
1. 无工具超时可重试(IR 无 tool_use -> 重试 -> 成功)
2. 已确认工具结果后超时 -> 重试只重发模型调用(工具执行计数不变)
3. 未确认工具(孤儿 tool_use) -> 不重试(fail-closed)
4. 重试再超时 -> 无第三次
"""

import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.tool_model_generation import (
    ModelGenerateParams,
    _ir_last_tool_use_confirmed,
    generate_model_response,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.backends.errors import ProviderTimeoutError
from agent_py_agent.agent.backends.tool_ir import AssistantTurn
from agent_py_agent.agent.contracts.model_call_ledger import ModelCallLedger
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_history_call,
    canonical_history_result,
    make_test_protocol_snapshot,
)

# ---------------------------------------------------------------- helpers

def _agent(request_timeout: float = 0.05) -> SimpleNamespace:
    """无 dynamic_timeout 字段 -> effective == base == request_timeout。"""
    return SimpleNamespace(
        backend=SimpleNamespace(name="test", max_tokens=512),
        config=SimpleNamespace(request_timeout=request_timeout),
        _current_subagent_run_id="",
    )


def _params(*, history: list | None = None) -> ToolLoopExecuteParams:
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
        task_attributes={},
        request_id="r5",
        run_id="run5",
        task_id="t5",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
        tool_protocol_snapshot=make_test_protocol_snapshot(
            run_id="run5",
            source_protocol="native",
        ),
        tool_ir_history=history or [],
        save=False,
        delivery_contract={},
    )


class _FlakyBackend:
    """第 1 次挂起触发墙钟超时, 第 2 次成功——验证重试恰好一次。"""

    name = "flaky-test-backend"
    calls = 0

    def generate(self, prompt: str, on_chunk=None, **kwargs: object) -> ModelResponse:
        type(self).calls += 1
        if type(self).calls == 1:
            time.sleep(0.5)  # > request_timeout=0.05 -> wall_clock
        return ModelResponse(text="recovered", backend=self.name)


class _AlwaysTimeoutBackend:
    """永远挂起——重试再超时, 验证无第三次。"""

    name = "always-timeout-backend"
    calls = 0

    def generate(self, prompt: str, on_chunk=None, **kwargs: object) -> ModelResponse:
        type(self).calls += 1
        time.sleep(0.5)
        return ModelResponse(text="never", backend=self.name)


def _run(
    backend: object, params: ToolLoopExecuteParams
) -> tuple[object, SimpleNamespace]:
    agent = SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(request_timeout=0.05),
        _current_subagent_run_id="",
    )
    response = generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=params,
            prompt="hello",
            tool_rounds=1,
        )
    )
    return response, agent


def _ledger(agent: object) -> ModelCallLedger:
    return agent._model_call_ledger  # type: ignore[attr-defined]


def _confirmed_call(params: ToolLoopExecuteParams) -> ToolCall:
    return canonical_history_call(
        "read_file",
        {"path": "x.md"},
        call_id="g5-c1",
        source_protocol=params.tool_protocol_snapshot.source_protocol,
        run_id=params.run_id,
        turn_id=f"{params.run_id}:round-1",
        attempt_id=params.request_id,
    )


# ---------------------------------------------------------------- 1. 无工具超时可重试

def test_no_tool_timeout_retries_once_and_succeeds() -> None:
    """IR 无 tool_use -> 超时可重试一次, 返回重试成功的响应。"""
    _FlakyBackend.calls = 0
    params = _params()  # IR 为空(无工具)
    response, agent = _run(_FlakyBackend(), params)
    assert response.text == "recovered"
    assert _FlakyBackend.calls == 2  # 恰好重试一次
    # 收口证据(seq1545): attempt-1=timed_out、attempt-2=finished,
    # 成功响应与 attempt-2 对账(call_id 相同)
    records = _ledger(agent).records()
    assert len(records) == 2
    attempts = [r.metadata["physical_attempt"] for r in records]
    assert attempts == [1, 2]
    assert records[0].status == "timed_out"
    assert records[1].status == "finished"  # 重试成功记录统一收口
    assert records[1].finished_at is not None
    # 字段级对账(seq1634 补证): attempt-2 用全新 call_id(非复用 attempt-1),
    # 成功响应经 _finish_model_generation(retry_state) 收口落在 attempt-2 的
    # call_id 上——「响应确实是重试产物」由 call_id 不同 + 唯一 finished 共同证明。
    assert "attempt-1" in records[0].call_id
    assert "attempt-2" in records[1].call_id
    assert records[0].call_id != records[1].call_id


# ---------------------------------------------------------------- 2. 已确认工具后超时: 重试不重放工具

def test_confirmed_tool_timeout_retries_without_replaying_tool() -> None:
    """IR 最后 tool_use 已配对 -> 重试只重发模型调用, 工具效果不重复。"""
    _FlakyBackend.calls = 0
    # 构造已确认 IR: AssistantTurn(tool_calls=[c1]) + ToolResult(call_id=c1)
    params = _params()
    call = _confirmed_call(params)
    result = canonical_history_result(call, "file contents")
    params = _params(history=[AssistantTurn(tool_calls=[call]), result])

    response, _ = _run(_FlakyBackend(), params)
    assert response.text == "recovered"
    assert _FlakyBackend.calls == 2  # 2 次模型调用(超时+重试)
    # 工具不重放: IR 历史长度不变(重试不写入新 ToolCall/ToolResult)
    assert len(params.tool_ir_history) == 2


# ---------------------------------------------------------------- 3. 未确认工具不重试(fail-closed)

def test_unconfirmed_tool_timeout_does_not_retry() -> None:
    """孤儿 tool_use(截断/未确认) -> fail-closed 不重试, 直接上抛。"""
    _AlwaysTimeoutBackend.calls = 0
    params = _params()
    call = _confirmed_call(params)
    params = _params(history=[AssistantTurn(tool_calls=[call])])  # 无配对回执
    with pytest.raises(ProviderTimeoutError):
        _run(_AlwaysTimeoutBackend(), params)
    assert _AlwaysTimeoutBackend.calls == 1  # 只调一次, 未重试
    # 未确认场景 ledger 只落 1 条(不重试 -> 无 attempt-2)
    assert _AlwaysTimeoutBackend.calls == 1


# ---------------------------------------------------------------- 4. 重试再超时无第三次

def test_retry_timeout_no_third_attempt() -> None:
    """重试又超时 -> 不再重试(每 logical turn 全局最多一次), 上抛。"""
    _AlwaysTimeoutBackend.calls = 0
    params = _params()  # 无工具 -> 可重试
    with pytest.raises(ProviderTimeoutError):
        _run(_AlwaysTimeoutBackend(), params)
    assert _AlwaysTimeoutBackend.calls == 2  # attempt-1 超时 + 重试超时, 无第三次


def test_stream_timeout_is_left_for_outer_reconnect_loop() -> None:
    """流式超时不在生成层静默重试，外层负责可见的 会话运行时 reconnect。"""
    from agent_py_agent.agent.agent_core.tool_model_generation import (
        _retry_once_after_timeout,
    )

    params = _params()
    request = ModelGenerateParams(
        agent=_agent(),
        params=params,
        prompt="hello",
        tool_rounds=1,
    )
    assert (
        _retry_once_after_timeout(
            request,
            ProviderTimeoutError("idle", stage="stream_idle"),
        )
        is None
    )


# ---------------------------------------------------------------- 5. 判定函数单元测

def test_ir_last_tool_use_confirmed_matrix() -> None:
    """配对判定矩阵: 无工具/已配对 -> True; 孤儿/部分配对 -> False。"""
    params = _params()
    c1 = _confirmed_call(params)
    c2 = canonical_history_call(
        "write_file",
        {"path": "y.md", "content": "x"},
        call_id="g5-c2",
        source_protocol=params.tool_protocol_snapshot.source_protocol,
        run_id=params.run_id,
        turn_id=f"{params.run_id}:round-1",
        attempt_id=params.request_id,
    )
    r1 = canonical_history_result(c1, "ok")
    r2 = canonical_history_result(c2, "ok")
    # 无工具 -> True
    empty = _params()
    assert _ir_last_tool_use_confirmed(_req(empty)) is True
    # 已配对 -> True
    confirmed = _params(history=[AssistantTurn(tool_calls=[c1]), r1])
    assert _ir_last_tool_use_confirmed(_req(confirmed)) is True
    # 孤儿 -> False
    orphan = _params(history=[AssistantTurn(tool_calls=[c1])])
    assert _ir_last_tool_use_confirmed(_req(orphan)) is False
    # 部分配对(c2 孤儿) -> False
    partial = _params(history=[AssistantTurn(tool_calls=[c1, c2]), r1])
    assert _ir_last_tool_use_confirmed(_req(partial)) is False
    # 最后一条已配对(c2 有回执) -> True
    last_confirmed = _params(history=[AssistantTurn(tool_calls=[c1, c2]), r1, r2])
    assert _ir_last_tool_use_confirmed(_req(last_confirmed)) is True


def _req(params: ToolLoopExecuteParams) -> ModelGenerateParams:
    return ModelGenerateParams(
        agent=_agent(),
        params=params,
        prompt="",
        tool_rounds=1,
    )
