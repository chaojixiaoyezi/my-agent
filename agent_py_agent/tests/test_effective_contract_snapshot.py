from __future__ import annotations


# LLM: Effective contract snapshots should merge layers while preserving high-level safety rules.
# 函数用途: 验证低层任务合同不能覆盖框架级高危审批要求。
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


# LLM: Replay should use the saved effective contract hash rather than current mutable policy.
# 函数用途: 验证 replay 合同快照必须和 runlog 记录的 contract_hash 一致。
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


# LLM: Every run should carry a persisted effective contract ref and hash.
# 函数用途: 验证缺少 effective_contract_ref 或 contract_hash 会被运行预检拦住。
def test_effective_contract_snapshot_requires_ref_and_hash() -> None:
    from agent_py_agent.agent.contracts.effective_contract_snapshot import (
        validate_run_contract_snapshot,
    )

    result = validate_run_contract_snapshot({"run_id": "run-1", "effective_contract_ref": ""})

    assert result.ok is False
    assert result.error_codes == ("EFFECTIVE_CONTRACT_REF_MISSING", "EFFECTIVE_CONTRACT_HASH_MISSING")
