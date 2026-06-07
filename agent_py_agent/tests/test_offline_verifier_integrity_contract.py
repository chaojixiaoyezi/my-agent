from __future__ import annotations


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


def test_verifier_ignores_legacy_success_alias_for_tool_trace() -> None:
    from agent_py_agent.agent.contracts.offline_verifier_integrity_contract import (
        validate_verifier_integrity,
    )

    result = validate_verifier_integrity(
        {
            "evidence_claims": [{"source_tool": "query_logs", "evidence_ref": "EV-1"}],
            "tool_trace": [{"tool": "query_logs", "operation_id": "op-1", "success": True}],
            "evidence_store_refs": ["EV-1"],
        }
    )

    assert result.ok is False
    assert result.error_codes == ("EVIDENCE_TOOL_TRACE_MISSING",)


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
