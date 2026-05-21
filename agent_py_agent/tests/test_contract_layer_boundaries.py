from __future__ import annotations

from agent_py_agent.agent.contracts.contract_layers import (
    CONTRACT_LAYER_CORE_RUNTIME,
    CONTRACT_LAYER_FORMAT_VALIDATOR,
    CONTRACT_LAYER_LIVE_SCENARIO,
    CONTRACT_LAYER_OFFLINE_TEST,
    classify_contract_module,
)


def test_contract_layer_classification_keeps_runtime_gates_separate_from_scenarios():
    assert classify_contract_module("gates.adapters") == CONTRACT_LAYER_CORE_RUNTIME
    assert classify_contract_module("tool_protocol_v2") == CONTRACT_LAYER_CORE_RUNTIME
    assert classify_contract_module("artifact_xlsx_contract") == CONTRACT_LAYER_FORMAT_VALIDATOR
    assert classify_contract_module("offline_tool_guardrail_contract") == CONTRACT_LAYER_OFFLINE_TEST
    assert classify_contract_module("main_agent_real_task_suite_cases") == CONTRACT_LAYER_LIVE_SCENARIO


def test_contract_layer_classification_does_not_mark_live_suites_as_core():
    scenario_modules = (
        "e2e_matrix_runner",
        "small_real_acceptance_runner",
        "medium_real_acceptance_runner",
        "main_agent_task_execution",
        "long_task_recovery_scenario",
    )

    assert {
        classify_contract_module(name)
        for name in scenario_modules
    } == {CONTRACT_LAYER_LIVE_SCENARIO}
