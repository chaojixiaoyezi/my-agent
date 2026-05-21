from __future__ import annotations

from pathlib import Path


# LLM: LLM activation readiness should compose the seven pre-live-model gates.
# 函数用途: 验证真 LLM 上场前总闸门串起 1-7 步，且报告只暴露 refs 和机器字段。
def test_llm_activation_readiness_runs_all_seven_gates(tmp_path: Path) -> None:
    from agent_py_agent.agent.contracts.llm_activation_readiness import (
        LLMActivationReadinessRequest,
        run_llm_activation_readiness,
    )
    from agent_py_agent.agent.contracts.pre_real_task_validation import (
        PreRealTaskValidationRequest,
        run_pre_real_task_validation,
    )

    pre_real = run_pre_real_task_validation(
        PreRealTaskValidationRequest(workspace=tmp_path / "pre-real")
    )
    report = run_llm_activation_readiness(
        LLMActivationReadinessRequest(
            workspace=tmp_path / "llm-ready",
            pre_real_report=pre_real,
        )
    )

    assert report.ok is True
    assert report.summary == {"failed": 0, "passed": 7, "total": 7}
    assert [phase.phase_id for phase in report.phases] == [
        "model_adapter_entry_contract",
        "prompt_context_assembly_contract",
        "tool_side_effect_gate",
        "small_llm_canary_design",
        "llm_trace_replay_capture",
        "model_timeout_input_speed_budget",
        "pre_llm_total_gate",
    ]
    assert (tmp_path / "llm-ready" / report.report_ref).is_file()
    assert "写一个小文件" not in str(report.to_dict())


# LLM: The final gate must refuse live LLM activation when the prerequisite report is absent.
# 函数用途: 验证 1-6 预真实任务报告缺失时，总闸门失败，而不是默认放行。
def test_llm_activation_readiness_rejects_missing_pre_real_report(tmp_path: Path) -> None:
    from agent_py_agent.agent.contracts.llm_activation_readiness import (
        LLMActivationReadinessRequest,
        run_llm_activation_readiness,
    )

    report = run_llm_activation_readiness(
        LLMActivationReadinessRequest(workspace=tmp_path / "missing-pre-real")
    )

    assert report.ok is False
    assert report.summary == {"failed": 1, "passed": 6, "total": 7}
    assert report.phases[-1].phase_id == "pre_llm_total_gate"
    assert report.phases[-1].issues == ["PRE_REAL_VALIDATION_MISSING"]


# LLM: Canary cases must be structured, refs-first, bounded, and replayable before live LLM use.
# 函数用途: 验证小型 LLM canary 设计不内联 prompt 文本，并满足小真实验收闸门。
def test_llm_activation_canary_design_is_structured_and_side_effect_free(tmp_path: Path) -> None:
    from agent_py_agent.agent.contracts.llm_activation_readiness import (
        build_small_llm_canary_gate,
    )
    from agent_py_agent.agent.contracts.small_real_acceptance_gate import (
        validate_small_real_acceptance_gate,
    )

    gate = build_small_llm_canary_gate(tmp_path / "canary")
    validation = validate_small_real_acceptance_gate(gate)

    assert validation.ok is True
    assert len(gate["cases"]) == 4
    for case in gate["cases"]:
        assert "prompt" not in case
        assert case["prompt_ref"].startswith("prompt://")
        assert case["real_execution_allowed"] is False
        assert set(case["allowed_effects"]) <= {"read_only", "dry_run"}


# LLM: Timeout budget readiness must use structured 5K/10K probe facts, not prose timing notes.
# 函数用途: 验证输入阶段预算来自模型调用账本 probe，并写出可审计的 timeout evidence ref。
def test_llm_activation_timeout_budget_uses_probe_ledger(tmp_path: Path) -> None:
    from agent_py_agent.agent.contracts.llm_activation_readiness import (
        build_model_timeout_budget,
    )

    budget = build_model_timeout_budget(tmp_path / "timeout")

    assert budget["estimate"]["source"] == "probe_5k_10k"
    assert budget["estimate"]["timeout_seconds"] > 0
    assert budget["estimate"]["cache_suspected"] is False
    assert budget["ledger_ref"].endswith("model_call_ledger.json")
    assert (tmp_path / "timeout" / "model_call_ledger.json").is_file()
