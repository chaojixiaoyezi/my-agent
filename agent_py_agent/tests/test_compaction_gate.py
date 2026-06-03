
from __future__ import annotations

from agent_py_agent.agent.contracts.gates.compaction_gate import (
    CompactionGateFacts,
    compaction_gate_snapshot,
    evaluate_compaction_gate,
)


def _full_pre_state() -> dict:
    return {
        "task_id": "task-1",
        "run_id": "run-1",
        "contract": {"id": "c1", "status": "active"},
        "approval_refs": {"approval-1": "approved"},
        "artifact_refs": {"out": "/tmp/out.txt"},
        "recovery_packet": {"packet_id": "rp-1"},
        "pending_actions": ["tool_call", "file_write"],
        "failed_actions": [],
    }


class TestPreCompactChecks:
    def test_allows_complete_state(self):
        facts = CompactionGateFacts(
            task_id="task-1",
            run_id="run-1",
            pre_compact_state=_full_pre_state(),
            phase="pre_compact",
        )
        decision = evaluate_compaction_gate(facts)
        assert decision.allowed is True

    def test_blocks_missing_contract(self):
        state = _full_pre_state()
        del state["contract"]
        facts = CompactionGateFacts(pre_compact_state=state, phase="pre_compact")
        decision = evaluate_compaction_gate(facts)
        assert decision.allowed is False
        assert any("MISSING_CONTRACT" in f.code for f in decision.findings)

    def test_blocks_missing_approval_refs(self):
        state = _full_pre_state()
        del state["approval_refs"]
        facts = CompactionGateFacts(pre_compact_state=state, phase="pre_compact")
        decision = evaluate_compaction_gate(facts)
        assert decision.allowed is False
        assert any("MISSING_APPROVAL_REFS" in f.code for f in decision.findings)

    def test_allows_empty_artifact_refs_before_artifacts_exist(self):
        state = _full_pre_state()
        state["artifact_refs"] = {}
        facts = CompactionGateFacts(pre_compact_state=state, phase="pre_compact")
        decision = evaluate_compaction_gate(facts)
        assert decision.allowed is True

    def test_none_field_is_missing(self):
        state = _full_pre_state()
        state["recovery_packet"] = None
        facts = CompactionGateFacts(pre_compact_state=state, phase="pre_compact")
        decision = evaluate_compaction_gate(facts)
        assert decision.allowed is False
        assert any("MISSING_RECOVERY_PACKET" in f.code for f in decision.findings)


class TestPostCompactChecks:
    def test_allows_preserved_state(self):
        pre = _full_pre_state()
        post = _full_pre_state()
        facts = CompactionGateFacts(
            pre_compact_state=pre,
            post_compact_state=post,
            phase="post_compact",
        )
        decision = evaluate_compaction_gate(facts)
        assert decision.allowed is True

    def test_blocks_lost_contract(self):
        pre = _full_pre_state()
        post = _full_pre_state()
        del post["contract"]
        facts = CompactionGateFacts(
            pre_compact_state=pre,
            post_compact_state=post,
            phase="post_compact",
        )
        decision = evaluate_compaction_gate(facts)
        assert decision.allowed is False
        assert any("LOST_CONTRACT" in f.code for f in decision.findings)

    def test_blocks_lost_recovery_packet(self):
        pre = _full_pre_state()
        post = _full_pre_state()
        del post["recovery_packet"]
        facts = CompactionGateFacts(
            pre_compact_state=pre,
            post_compact_state=post,
            phase="post_compact",
        )
        decision = evaluate_compaction_gate(facts)
        assert decision.allowed is False
        assert any("LOST_RECOVERY_PACKET" in f.code for f in decision.findings)

    def test_ok_when_pending_actions_unchanged(self):
        pre = _full_pre_state()
        post = _full_pre_state()
        post["pending_actions"] = ["new_action"]
        facts = CompactionGateFacts(
            pre_compact_state=pre,
            post_compact_state=post,
            phase="post_compact",
        )
        decision = evaluate_compaction_gate(facts)
        assert decision.allowed is True


class TestCompactionGateSnapshot:
    def test_snapshot_preserves_fields(self):
        snap = compaction_gate_snapshot({
            "task_id": "task-1",
            "run_id": "run-1",
            "contract": {"id": "c1"},
            "approval_refs": ["a1", "a2"],
            "artifact_refs": {"out": "/tmp/o"},
            "recovery_packet": {"pid": "rp-1"},
            "pending_actions": ["write", "send"],
            "failed_actions": [],
        })
        assert snap["task_id"] == "task-1"
        assert snap["run_id"] == "run-1"
        assert snap["contract"] == 1
        assert snap["approval_refs"] == 2
        assert snap["recovery_packet"] == 1
        assert snap["pending_actions"] == 2
        assert snap["failed_actions"] == 0

    def test_snapshot_handles_none(self):
        snap = compaction_gate_snapshot()
        assert snap["task_id"] == ""
        assert snap["contract"] is None
        assert snap["approval_refs"] is None
