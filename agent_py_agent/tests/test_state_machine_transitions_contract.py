from __future__ import annotations


def test_transition_contract_allows_running_to_verifying():
    from agent_py_agent.agent.contracts.state_machine_transitions import transition_contract

    contract = transition_contract("RUNNING", "VERIFYING")

    assert contract.allowed is True
    assert contract.reason == "allowed_transition"
    assert contract.required_condition == "artifact_written_and_pending_acceptance"


def test_transition_contract_rejects_pending_to_done():
    from agent_py_agent.agent.contracts.state_machine_transitions import transition_contract

    contract = transition_contract("PENDING", "DONE")

    assert contract.allowed is False
    assert contract.reason == "disallowed_transition"
    assert contract.required_condition == "final_artifact_ready"


def test_transition_contract_rejects_running_directly_to_done():
    from agent_py_agent.agent.contracts.state_machine_transitions import transition_contract

    contract = transition_contract("RUNNING", "DONE")

    assert contract.allowed is False
    assert contract.reason == "disallowed_transition"
    assert contract.required_condition == "final_artifact_ready"


def test_first_invalid_transition_reports_earliest_invalid_hop():
    from agent_py_agent.agent.contracts.state_machine_transitions import first_invalid_transition

    contract = first_invalid_transition(["PLANNING", "RUNNING", "DONE", "RUNNING"])

    assert contract is not None
    assert contract.from_status == "RUNNING"
    assert contract.to_status == "DONE"
    assert contract.reason == "disallowed_transition"
