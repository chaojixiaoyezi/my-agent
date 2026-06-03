from __future__ import annotations


def test_main_agent_core_entrypoints_accepts_complete_structured_contract() -> None:
    from agent_py_agent.agent.contracts.main_agent_core_entrypoints import (
        validate_main_agent_core_entrypoints,
    )

    result = validate_main_agent_core_entrypoints(
        {
            "entrypoints": {
                "state_machine": "agent.contracts.state_machine",
                "tool_executor": "agent.agent_core.tool_call_runtime",
                "closeout_gate": "agent.agent_core.delivery_closeout.gates",
                "runlog": "agent.contracts.run_trace_contract",
                "tooltrace": "agent.contracts.run_trace_contract",
                "approval_gate": "agent.contracts.approval_gate",
                "effective_contract": "agent.contracts.effective_contract_snapshot",
            },
            "invariants": {
                "state_mutation_mode": "event_only",
                "success_requires_verification": True,
                "tool_calls_require_executor": True,
                "machine_facts_source": "structured_fields",
            },
        }
    )

    assert result.ok is True
    assert result.error_codes == ()


def test_main_agent_core_entrypoints_rejects_missing_or_unsafe_invariants() -> None:
    from agent_py_agent.agent.contracts.main_agent_core_entrypoints import (
        validate_main_agent_core_entrypoints,
    )

    result = validate_main_agent_core_entrypoints(
        {
            "entrypoints": {
                "state_machine": "agent.contracts.state_machine",
                "tool_executor": "",
            },
            "invariants": {
                "state_mutation_mode": "direct_status_write",
                "success_requires_verification": False,
                "tool_calls_require_executor": False,
                "machine_facts_source": "natural_language",
            },
        }
    )

    assert result.ok is False
    assert result.error_codes == (
        "MAIN_CORE_ENTRYPOINT_MISSING",
        "MAIN_CORE_SUCCESS_WITHOUT_VERIFICATION",
        "MAIN_CORE_TOOL_EXECUTOR_BYPASS",
        "MAIN_CORE_STATE_MUTATION_NOT_EVENT_ONLY",
        "MAIN_CORE_NATURAL_LANGUAGE_FACT_SOURCE",
    )
