from __future__ import annotations


# LLM: transition_contract must keep legal lifecycle hops explicit and testable.
# 函数用途: 验证 RUNNING -> VERIFYING 这种合法迁移会返回允许和稳定条件名。
def test_transition_contract_allows_running_to_verifying():
    from agent_py_agent.agent.contracts.state_machine_transitions import transition_contract

    contract = transition_contract("RUNNING", "VERIFYING")

    assert contract.allowed is True
    assert contract.reason == "allowed_transition"
    assert contract.required_condition == "artifact_written_and_pending_acceptance"


# LLM: disallowed lifecycle hops should be machine-visible for replay and control-plane checks.
# 函数用途: 验证 PENDING 不能直接跳到 DONE，避免调度层把未执行任务误算完成。
def test_transition_contract_rejects_pending_to_done():
    from agent_py_agent.agent.contracts.state_machine_transitions import transition_contract

    contract = transition_contract("PENDING", "DONE")

    assert contract.allowed is False
    assert contract.reason == "disallowed_transition"
    assert contract.required_condition == "final_artifact_ready"


# LLM: first_invalid_transition must pinpoint the earliest broken hop in a replay sequence.
# 函数用途: 验证序列中出现 RUNNING -> DONE -> RUNNING 这种非法回跳时，会返回第一处冲突。
def test_first_invalid_transition_reports_earliest_invalid_hop():
    from agent_py_agent.agent.contracts.state_machine_transitions import first_invalid_transition

    contract = first_invalid_transition(["PLANNING", "RUNNING", "DONE", "RUNNING"])

    assert contract is not None
    assert contract.from_status == "DONE"
    assert contract.to_status == "RUNNING"
    assert contract.reason == "disallowed_transition"
