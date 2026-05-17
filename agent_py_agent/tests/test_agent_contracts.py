from __future__ import annotations


# LLM: Core contracts should be machine-readable and reusable across main/subagent flows.
# 函数用途: 验证错误分类、状态机、幂等键和真实 E2E 矩阵先有统一合同，不再靠零散 guard。
def test_error_taxonomy_classifies_failures_and_recommends_recovery() -> None:
    from agent_py_agent.agent.contracts.error_taxonomy import classify_error, error_contract

    path_error = classify_error("write_file failed: path outside workspace /tmp/other")
    assert path_error.code == "PATH_OUTSIDE_WORKSPACE"
    assert path_error.retryable is False
    assert "修正路径" in path_error.recovery_hint

    upstream = classify_error("anthropic compatible provider timeout after 240 seconds")
    assert upstream.code == "MODEL_UPSTREAM_FAILED"
    assert upstream.retryable is True

    contract = error_contract("ARTIFACT_MISSING")
    assert contract.category == "artifact"
    assert contract.recommended_action == "read_or_rebuild_artifact_ref"


# LLM: State machine decisions must be facts, not prompt-specific guard prose.
# 函数用途: 验证统一状态机能判断是否可调度、是否可收口、失败后应修复还是接管。
def test_run_state_machine_dispatch_closeout_and_recovery_decisions() -> None:
    from agent_py_agent.agent.contracts.state_machine import (
        RunStateFacts,
        can_closeout,
        can_dispatch,
        recovery_decision,
    )

    assert can_dispatch(RunStateFacts(status="PLANNING")) is True
    assert can_dispatch(RunStateFacts(status="RUNNING")) is False
    assert can_dispatch(RunStateFacts(status="DONE", verification_status="VERIFIED")) is False
    assert can_closeout(RunStateFacts(status="DONE", verification_status="VERIFIED")) is True
    assert can_closeout(RunStateFacts(status="DONE", verification_status="NEEDS_ACCEPTANCE")) is False

    blocked = recovery_decision(RunStateFacts(status="BLOCKED", failure_type="TOOL_UNAVAILABLE"))
    assert blocked.action == "repair_or_request_capability"
    assert blocked.allow_new_run is False

    failed = recovery_decision(RunStateFacts(status="FAILED", attempts=3, max_attempts=3))
    assert failed.action == "takeover_or_stop"
    assert failed.allow_new_run is True


# LLM: Idempotency keys make duplicate model calls safe without hardcoding workflow guards.
# 函数用途: 验证幂等键对 dict/list 顺序稳定，并能生成 create/dispatch/compact 的通用操作键。
def test_idempotency_contract_stable_keys_and_operation_shapes() -> None:
    from agent_py_agent.agent.contracts.idempotency import idempotency_key, operation_id

    left = idempotency_key(
        "create_subagents",
        {
            "parent_run_id": "root",
            "role": "worker",
            "goal": "写 index.html",
            "write_roots": ["/tmp/a", "/tmp/b"],
            "metadata": {"b": 2, "a": 1},
        },
    )
    right = idempotency_key(
        "create_subagents",
        {
            "metadata": {"a": 1, "b": 2},
            "write_roots": ["/tmp/a", "/tmp/b"],
            "goal": "写 index.html",
            "role": "worker",
            "parent_run_id": "root",
        },
    )

    assert left == right
    assert left.startswith("idem:create_subagents:")
    assert operation_id("compact_apply", {"plan_id": "plan-1", "scope": {"task_id": "t1"}}).startswith(
        "op:compact_apply:"
    )


# LLM: Real E2E matrix must be a data contract so CI/manual runners can execute the same scenarios.
# 函数用途: 验证真实端到端测试矩阵覆盖主代理、compact、artifact、失败修复和子代理复用内核。
def test_real_e2e_matrix_contains_required_scenarios() -> None:
    from agent_py_agent.agent.contracts.e2e_matrix import REAL_E2E_MATRIX, required_matrix_ids

    ids = {item.case_id for item in REAL_E2E_MATRIX}
    assert required_matrix_ids().issubset(ids)
    assert "windows_chinese_path_write" in ids
    assert "compact_resume_continue" in ids
    assert "subagent_reuses_main_kernel" in ids
    assert all(item.execution_mode in {"deterministic", "real_model"} for item in REAL_E2E_MATRIX)
    assert all(item.acceptance for item in REAL_E2E_MATRIX)
