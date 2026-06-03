from __future__ import annotations


def test_effective_contract_merge_preserves_framework_safety_precedence() -> None:
    from agent_py_agent.agent.contracts.effective_contract_snapshot import (
        build_effective_contract_snapshot,
    )

    snapshot = build_effective_contract_snapshot(
        run_id="run-1",
        layers=(
            {
                "layer": "framework",
                "dangerous_actions": {"block_ip": {"approval_required": True, "forbidden_targets": ["10.0.0.1"]}},
            },
            {"layer": "task_type", "dangerous_actions": {"block_ip": {"approval_required": False}}},
            {"layer": "case", "artifacts": {"required": [{"path": "output.md"}]}},
        ),
    )

    block_ip = snapshot.effective_contract["dangerous_actions"]["block_ip"]
    assert snapshot.ok is True
    assert block_ip["approval_required"] is True
    assert block_ip["forbidden_targets"] == ["10.0.0.1"]
    assert snapshot.contract_hash.startswith("sha256:")


def test_effective_contract_snapshot_detects_replay_hash_mismatch() -> None:
    from agent_py_agent.agent.contracts.effective_contract_snapshot import (
        build_effective_contract_snapshot,
        validate_replay_effective_contract,
    )

    snapshot = build_effective_contract_snapshot(
        run_id="run-1",
        layers=({"layer": "case", "artifacts": {"required": [{"path": "output.md"}]}},),
    )
    result = validate_replay_effective_contract(
        {"run_id": "run-1", "contract_hash": "sha256:old"},
        snapshot,
    )

    assert result.ok is False
    assert result.error_codes == ("EFFECTIVE_CONTRACT_HASH_MISMATCH",)


def test_effective_contract_snapshot_requires_ref_and_hash() -> None:
    from agent_py_agent.agent.contracts.effective_contract_snapshot import (
        validate_run_contract_snapshot,
    )

    result = validate_run_contract_snapshot({"run_id": "run-1", "effective_contract_ref": ""})

    assert result.ok is False
    assert result.error_codes == ("EFFECTIVE_CONTRACT_REF_MISSING", "EFFECTIVE_CONTRACT_HASH_MISSING")
