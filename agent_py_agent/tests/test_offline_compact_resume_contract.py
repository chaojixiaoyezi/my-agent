from __future__ import annotations


# LLM: Compact bundles must preserve waiting state and referenced tool outputs for resume.
# 函数用途: 验证等待人工状态、等待原因和 tool result refs 都在 compact 结构字段中保留。
def test_compact_resume_preserves_waiting_for_human_and_tool_refs() -> None:
    from agent_py_agent.agent.contracts.offline_compact_resume_contract import (
        validate_compact_resume_bundle,
    )

    result = validate_compact_resume_bundle(
        {
            "compact": {
                "current_status": "WAITING_FOR_USER",
                "waiting_reason": "approval",
                "tool_result_refs": ["memory://tool-results/op-query"],
                "failed_operation_ids": [],
                "executed_side_effect_operation_ids": [],
                "summary": _complete_summary(),
            },
            "resume_events": [],
        }
    )

    assert result.ok is True
    assert result.error_codes == ()


# LLM: Compact bundles must not drop structured waiting state or tool result refs.
# 函数用途: 验证压缩前的等待状态和工具结果引用没有进入 compact，会返回对应错误码。
def test_compact_resume_rejects_missing_waiting_state_or_tool_refs() -> None:
    from agent_py_agent.agent.contracts.offline_compact_resume_contract import (
        validate_compact_resume_bundle,
    )

    result = validate_compact_resume_bundle(
        {
            "pre_compact": {
                "current_status": "WAITING_FOR_TOOL",
                "waiting_reason": "tool_result",
                "tool_result_refs": ["memory://tool-results/op-query"],
            },
            "compact": {
                "current_status": "",
                "waiting_reason": "",
                "tool_result_refs": [],
                "failed_operation_ids": [],
                "executed_side_effect_operation_ids": [],
                "summary": _complete_summary(),
            },
            "resume_events": [],
        }
    )

    assert result.ok is False
    assert result.error_codes == ("COMPACT_STATE_MISSING", "COMPACT_TOOL_REF_MISSING")


# LLM: Compact resume must not forget failed operation ids from pre-compact events.
# 函数用途: 验证压缩前失败的工具 operation_id 没进入 compact，会返回 COMPACT_FAILURE_FORGOTTEN。
def test_compact_resume_cannot_forget_failed_tool_result() -> None:
    from agent_py_agent.agent.contracts.offline_compact_resume_contract import (
        validate_compact_resume_bundle,
    )

    result = validate_compact_resume_bundle(
        {
            "pre_compact_events": [
                {"type": "tool_result", "operation_id": "op-failed", "ok": False, "error_code": "TOOL_TIMEOUT"}
            ],
            "compact": {
                "current_status": "RUNNING",
                "tool_result_refs": ["memory://tool-results/op-failed"],
                "failed_operation_ids": [],
                "executed_side_effect_operation_ids": [],
                "summary": _complete_summary(),
            },
            "resume_events": [],
        }
    )

    assert result.ok is False
    assert result.error_codes == ("COMPACT_FAILURE_FORGOTTEN",)


# LLM: Resume after compact must not replay dangerous side-effect operation ids.
# 函数用途: 验证 compact 已记录的副作用 operation_id 在 resume 中再次 tool_call 会被拦截。
def test_compact_resume_cannot_repeat_dangerous_operation() -> None:
    from agent_py_agent.agent.contracts.offline_compact_resume_contract import (
        validate_compact_resume_bundle,
    )

    result = validate_compact_resume_bundle(
        {
            "compact": {
                "current_status": "RUNNING",
                "tool_result_refs": [],
                "failed_operation_ids": [],
                "executed_side_effect_operation_ids": ["op-block-ip"],
                "summary": _complete_summary(),
            },
            "resume_events": [
                {"type": "tool_call", "operation_id": "op-block-ip", "tool": "block_ip"},
            ],
        }
    )

    assert result.ok is False
    assert result.error_codes == ("DANGEROUS_OPERATION_REPLAYED",)


# LLM: Resume after compact must not repeat a previously stalled progress fingerprint.
# 函数用途: 验证 compact 记录的 no_progress_fingerprints 在 resume 中再次出现会被拦截。
def test_compact_resume_cannot_repeat_no_progress_loop() -> None:
    from agent_py_agent.agent.contracts.offline_compact_resume_contract import (
        validate_compact_resume_bundle,
    )

    result = validate_compact_resume_bundle(
        {
            "compact": {
                "current_status": "RUNNING",
                "tool_result_refs": [],
                "failed_operation_ids": [],
                "executed_side_effect_operation_ids": [],
                "no_progress_fingerprints": ["read:a:unchanged"],
                "summary": _complete_summary(),
            },
            "resume_events": [
                {"type": "tool_call", "tool": "read_file", "progress_fingerprint": "read:a:unchanged"},
            ],
        }
    )

    assert result.ok is False
    assert result.error_codes == ("COMPACT_NO_PROGRESS_LOOP_REPEATED",)


# LLM: Compact summary quality is a structured checklist, not a prose style judgment.
# 函数用途: 验证 summary 缺少状态、完成动作、未完成动作、证据 refs、下一步限制时会失败。
def test_compact_summary_quality_requires_state_done_next_and_refs() -> None:
    from agent_py_agent.agent.contracts.offline_compact_resume_contract import (
        validate_compact_resume_bundle,
    )

    result = validate_compact_resume_bundle(
        {
            "compact": {
                "current_status": "RUNNING",
                "tool_result_refs": [],
                "failed_operation_ids": [],
                "executed_side_effect_operation_ids": [],
                "summary": {"current_status": "RUNNING", "completed_actions": ["read file"]},
            },
            "resume_events": [],
        }
    )

    assert result.ok is False
    assert result.error_codes == ("COMPACT_SUMMARY_INCOMPLETE",)
    assert "pending_actions" in result.findings[0]["missing_fields"]


# LLM: _complete_summary keeps compact tests focused on each behavior instead of repeated fixture data.
# 函数用途: 返回满足 compact 摘要质量合同的最小结构化 summary。
def _complete_summary() -> dict[str, object]:
    return {
        "current_status": "RUNNING",
        "completed_actions": ["read source"],
        "pending_actions": ["write report"],
        "evidence_refs": ["memory://evidence/source"],
        "next_constraints": ["do not replay side effects"],
    }
