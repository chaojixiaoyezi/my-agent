from __future__ import annotations


# LLM: Verifier integrity should reject keyword-only evidence and forged tool names.
# 函数用途: 验证报告标题/文字声明不能替代真实 ToolTrace 或证据 refs。
def test_verifier_rejects_keyword_cheating_and_forged_tool_evidence() -> None:
    from agent_py_agent.agent.contracts.offline_verifier_integrity_contract import (
        validate_verifier_integrity,
    )

    result = validate_verifier_integrity(
        {
            "report_sections": [{"name": "Evidence", "content_hash": "same_as_heading"}],
            "evidence_claims": [{"source_tool": "query_logs", "evidence_ref": "EV-1"}],
            "tool_trace": [],
            "evidence_store_refs": ["EV-1"],
        }
    )

    assert result.ok is False
    assert result.error_codes == ("VERIFIER_KEYWORD_ONLY_CONTENT", "EVIDENCE_TOOL_TRACE_MISSING")


# LLM: Evidence refs should exist and satisfy freshness budgets.
# 函数用途: 验证不存在或过期的证据不能让验收通过。
def test_verifier_rejects_missing_and_stale_evidence_refs() -> None:
    from agent_py_agent.agent.contracts.offline_verifier_integrity_contract import (
        validate_verifier_integrity,
    )

    result = validate_verifier_integrity(
        {
            "evidence_claims": [
                {"evidence_ref": "EV-999", "source_tool": "query_logs", "age_minutes": 120},
            ],
            "tool_trace": [{"tool": "query_logs", "operation_id": "op-1", "ok": True}],
            "evidence_store_refs": ["EV-1"],
            "evidence_freshness": {"max_age_minutes": 60},
        }
    )

    assert result.ok is False
    assert result.error_codes == ("EVIDENCE_REF_MISSING", "EVIDENCE_STALE")


# LLM: Core verifiers should be bounded and not depend on LLM calls for pass/fail.
# 函数用途: 验证 verifier 超时或声明 requires_llm=true 都会被核心验收合同拒绝。
def test_core_verifier_timeout_and_llm_dependency_are_failures() -> None:
    from agent_py_agent.agent.contracts.offline_verifier_integrity_contract import (
        validate_verifier_integrity,
    )

    result = validate_verifier_integrity(
        {
            "verifier": {"duration_ms": 1500, "timeout_ms": 1000, "requires_llm": True},
            "evidence_claims": [],
            "tool_trace": [],
            "evidence_store_refs": [],
        }
    )

    assert result.ok is False
    assert result.error_codes == ("VERIFIER_TIMEOUT_EXCEEDED", "CORE_VERIFIER_LLM_DEPENDENCY")
