"""审计 #2/#19(成本接到 run 层,medium/稳定)真测:真实 LLM 成本累计到全局台账 owner/run 维度。

此前真实成本只按 model 进 /metrics 计数;本条把它接到 generate_model_response 层(那里有
owner=config.my_agent_owner_id、run_id=params.run_id、model=backend.model_name),按"租户/run"累计到
进程级全局 CostLedger,回答"某租户/run 花了多少钱"。无需穿透 worker 线程里的深层 seam。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model.llm_metrics import record_run_cost
from agent_py_agent.agent.agent_core.tool_model_generation import _record_run_cost
from agent_py_agent.agent.llm_scale.cost_ledger import (
    global_cost_ledger,
    reset_global_cost_ledger_for_test,
)


def _resp(inp: int, out: int) -> SimpleNamespace:
    return SimpleNamespace(usage={"input_tokens": inp, "output_tokens": out})


def test_record_run_cost_accumulates_owner_and_run() -> None:
    reset_global_cost_ledger_for_test()
    record_run_cost("acme", "run-1", "claude-sonnet-4-6", _resp(1_000_000, 0))  # sonnet 3/Mtok in = $3
    ledger = global_cost_ledger()
    assert ledger.tenant_cost("acme") == pytest.approx(3.0)
    assert ledger.run_cost("run-1") == pytest.approx(3.0)


def test_record_run_cost_sums_across_calls() -> None:
    reset_global_cost_ledger_for_test()
    record_run_cost("acme", "run-1", "claude-sonnet-4-6", _resp(1_000_000, 0))  # +3
    record_run_cost("acme", "run-2", "claude-sonnet-4-6", _resp(1_000_000, 0))  # +3(同租户不同 run)
    ledger = global_cost_ledger()
    assert ledger.tenant_cost("acme") == pytest.approx(6.0)  # 租户累计跨 run
    assert ledger.run_cost("run-1") == pytest.approx(3.0) and ledger.run_cost("run-2") == pytest.approx(3.0)


def test_record_run_cost_noop_without_model_or_response() -> None:
    reset_global_cost_ledger_for_test()
    record_run_cost("acme", "run-1", "", _resp(100, 0))  # 无 model
    record_run_cost("acme", "run-1", "claude-opus-4-8", None)  # 无响应
    assert global_cost_ledger().total_cost() == 0.0


def test_generate_layer_wiring_extracts_context_and_records() -> None:
    reset_global_cost_ledger_for_test()
    request = SimpleNamespace(
        agent=SimpleNamespace(
            config=SimpleNamespace(my_agent_owner_id="acme-corp"),
            backend=SimpleNamespace(model_name="claude-opus-4-8"),  # opus 15/Mtok in
        ),
        params=SimpleNamespace(run_id="run-9"),
    )
    _record_run_cost(request, _resp(1_000_000, 0))  # = $15
    ledger = global_cost_ledger()
    assert ledger.tenant_cost("acme-corp") == pytest.approx(15.0)  # 按 owner 当租户累计
    assert ledger.run_cost("run-9") == pytest.approx(15.0)


def test_generate_layer_wiring_never_raises_on_bad_request() -> None:
    reset_global_cost_ledger_for_test()
    _record_run_cost(SimpleNamespace(agent=SimpleNamespace(), params=SimpleNamespace()), _resp(100, 0))  # 缺字段不抛
