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

    approval = classify_error("dangerous command blocked: approval required before execution")
    assert approval.code == "APPROVAL_REQUIRED"
    assert approval.retryable is True

    no_progress = error_contract("NO_PROGRESS")
    assert no_progress.category == "orchestration"
    assert no_progress.recommended_action == "change_strategy_or_stop"


# LLM: Raw text fallback must not let generic words like schema/path outrank concrete failure signals.
# 函数用途: 验证错误分类优先读更明确的结构化失败语义，避免宽匹配带偏恢复策略。
def test_error_taxonomy_does_not_overclassify_generic_schema_or_path_words() -> None:
    from agent_py_agent.agent.contracts.error_taxonomy import classify_error

    timeout = classify_error("tool timed out while reading JSON schema documentation")
    assert timeout.code == "TOOL_TIMEOUT"

    generic_schema = classify_error("report mentions schema and path as documentation headings")
    assert generic_schema.code == "UNKNOWN_ERROR"


# LLM: Error classification should be rule-ranked and multilingual without broad text authority.
# 函数用途: 验证错误分类按明确失败信号排序，并支持中文运行错误，不把普通正文当机器事实。
def test_error_taxonomy_uses_ranked_specific_matches_and_chinese_signals() -> None:
    from agent_py_agent.agent.contracts.error_taxonomy import (
        classify_error,
        tool_failure_taxonomy,
    )

    assert classify_error("artifact schema doc says missing rows, but request timed out").code == "TOOL_TIMEOUT"
    assert classify_error("执行失败：权限不足，无法写入目标目录").code == "WRITE_FORBIDDEN"
    assert classify_error("工具不存在: magic_search").code == "TOOL_UNAVAILABLE"
    assert classify_error("请求超时，请稍后重试").code == "TOOL_TIMEOUT"
    assert classify_error("HTTP 429 rate limit exceeded").code == "RATE_LIMITED"
    assert classify_error("quota exceeded for provider").code == "QUOTA_EXCEEDED"
    assert classify_error("service unavailable during maintenance window").code == "MAINTENANCE"

    taxonomy = tool_failure_taxonomy()
    assert taxonomy == sorted(taxonomy)
    assert {"RATE_LIMITED", "QUOTA_EXCEEDED", "MAINTENANCE"}.issubset(taxonomy)


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
    assert can_dispatch(RunStateFacts(status="FAILED"), force=True) is True
    assert can_dispatch(RunStateFacts(status="DONE", verification_status="VERIFIED"), force=True) is False
    assert can_closeout(RunStateFacts(status="DONE", verification_status="VERIFIED")) is True

    blocked = recovery_decision(RunStateFacts(status="BLOCKED", failure_type="TOOL_UNAVAILABLE"))
    assert blocked.action == "repair_or_request_capability"
    assert blocked.allow_new_run is False

    approval = recovery_decision(RunStateFacts(status="FAILED", failure_type="APPROVAL_REQUIRED"))
    assert approval.action == "request_approval_or_stop"
    assert approval.allow_new_run is False

    no_progress = recovery_decision(RunStateFacts(status="BLOCKED", failure_type="NO_PROGRESS"))
    assert no_progress.action == "change_strategy_or_stop"
    assert no_progress.allow_new_run is False

    failed = recovery_decision(RunStateFacts(status="FAILED", attempts=3, max_attempts=3))
    assert failed.action == "takeover_or_stop"
    assert failed.allow_new_run is True


# LLM: State-machine recovery should not silently hide unhandled states or infinite blocked repairs.
# 函数用途: 验证状态机兜底会写 warning，BLOCKED 也受 attempts 限制，证据/阶段 JSON 错误有明确返工动作。
def test_run_state_machine_warns_and_uses_structured_recovery_actions(caplog) -> None:
    from agent_py_agent.agent.contracts.state_machine import (
        RunStateFacts,
        can_repair,
        recovery_decision,
    )

    staged = recovery_decision(RunStateFacts(status="BLOCKED", failure_type="STAGED_JSON_INVALID"))
    assert staged.action == "repair_structured_checkpoint_json"
    assert staged.reason == "blocked_staged_json_invalid"

    evidence = recovery_decision(RunStateFacts(status="BLOCKED", failure_type="EVIDENCE_CLAIM_UNSOURCED"))
    assert evidence.action == "repair_evidence_refs"
    assert evidence.reason == "blocked_evidence_claim_unsourced"

    assert can_repair(RunStateFacts(status="BLOCKED", attempts=1, max_attempts=2)) is True
    assert can_repair(RunStateFacts(status="BLOCKED", attempts=2, max_attempts=2)) is False
    exhausted = recovery_decision(RunStateFacts(status="BLOCKED", attempts=2, max_attempts=2))
    assert exhausted.action == "takeover_or_stop"
    assert exhausted.allow_new_run is True

    with caplog.at_level("WARNING"):
        unknown = recovery_decision(RunStateFacts(status="PAUSED", failure_type="UNKNOWN_ERROR"))
    assert unknown.action == "manual_review"
    assert "unhandled recovery state" in caplog.text


# LLM: State machine snapshots should normalize legacy task objects into one contract shape.
# 函数用途: 验证调度层可以从任意 task-like 对象得到统一状态、错误类型和 closeout 结果。
def test_run_state_snapshot_from_task_like_object() -> None:
    from types import SimpleNamespace

    from agent_py_agent.agent.contracts.state_machine import run_state_snapshot_from_task

    snapshot = run_state_snapshot_from_task(
        SimpleNamespace(
            id="run-1",
            status="timeout",
            verification_status="needs_closeout",
            runner_last_error="tool timed out after 240 seconds",
            runner_attempts="2",
        )
    )

    assert snapshot["run_id"] == "run-1"
    assert snapshot["schema_version"] == "state_machine.v1"
    assert snapshot["status"] == "TIMEOUT"
    assert snapshot["verification_status"] == "NEEDS_CLOSEOUT"
    assert snapshot["failure_type"] == "TOOL_TIMEOUT"
    assert snapshot["can_dispatch"] is False
    assert snapshot["can_closeout"] is False
    assert snapshot["recovery_decision"]["action"] == "repair"


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


# LLM: Acceptance contracts should combine artifacts, tests, and run state into one final gate.
# 函数用途: 验证任务完成判断能用结构化产物报告、真实测试记录和状态机结果统一判定。
def test_acceptance_contract_evaluates_artifacts_tests_and_state(tmp_path) -> None:
    from agent_py_agent.agent.contracts.acceptance_contract import (
        AcceptanceContract,
        AcceptanceInput,
        evaluate_acceptance_contract,
    )
    from agent_py_agent.agent.contracts.artifact_acceptance import (
        ArtifactAcceptanceRequest,
        validate_artifact,
    )
    from agent_py_agent.agent.contracts.state_machine import RunStateFacts
    from agent_py_agent.agent.subagents.execution_records import TestExecutionRecord

    artifact = tmp_path / "report.json"
    artifact.write_text('{"ok": true}', encoding="utf-8")
    report = validate_artifact(ArtifactAcceptanceRequest(path=artifact, workspace_root=tmp_path))
    test_record = TestExecutionRecord(
        test_name="json-valid",
        executed=True,
        validation_method="artifact_acceptance",
        validation_result={"ok": True},
    )

    result = evaluate_acceptance_contract(
        AcceptanceInput(
            contract=AcceptanceContract(items=["report exists"], required_artifact_kinds=["json"]),
            artifact_reports=[report],
            test_records=[test_record],
            run_state=RunStateFacts(status="DONE", verification_status="VERIFIED"),
        )
    )

    assert result.ok is True
    assert result.status == "accepted"
    assert {item["code"] for item in result.findings} >= {
        "ACCEPTANCE_STATE_OK",
        "ACCEPTANCE_ARTIFACTS_OK",
        "ACCEPTANCE_TESTS_OK",
    }


# LLM: Acceptance constraints are machine criteria metadata, not dead fields.
# 函数用途: 验证 constraints 会进入验收 finding，后续人工/返工链路能看到约束事实。
def test_acceptance_contract_records_constraints_as_criteria(tmp_path) -> None:
    from agent_py_agent.agent.contracts.acceptance_contract import (
        AcceptanceContract,
        AcceptanceInput,
        evaluate_acceptance_contract,
    )
    from agent_py_agent.agent.contracts.state_machine import RunStateFacts

    result = evaluate_acceptance_contract(
        AcceptanceInput(
            contract=AcceptanceContract(constraints=["不得写出工作区外"]),
            artifact_reports=[],
            test_records=[],
            run_state=RunStateFacts(status="DONE", verification_status="VERIFIED"),
        )
    )

    criteria = next(item for item in result.findings if item["code"] == "ACCEPTANCE_CRITERIA_RECORDED")
    assert criteria["ok"] is True
    assert criteria["constraints"] == ["不得写出工作区外"]
