# recovery packet 损坏、compact/resume 返工、重复幂等、等价绕过、scope creep 等场景。

from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.contracts.gates.artifact_gate import evaluate_artifact_report_gate
from agent_py_agent.agent.contracts.gates.command_policy import evaluate_command_policy
from agent_py_agent.agent.contracts.gates.compaction_gate import (
    CompactionGateFacts,
    compaction_gate_snapshot,
    evaluate_compaction_gate,
)
from agent_py_agent.agent.contracts.gates.network_safety import (
    NetworkSafetyFacts,
    evaluate_network_safety_gate,
)
from agent_py_agent.agent.contracts.gates.tool_guardrail import (
    ToolGuardrailConfig,
    ToolGuardrailFacts,
    evaluate_tool_guardrail_gate,
)


# ============================================================
# Scenario 1: 模型假完成
# ============================================================
class TestScenario1ModelFakeCompletion:
    """模型报告 ok=True 但包含 hard findings（文件实际缺失等）。"""

    def test_artifact_gate_blocks_when_findings_present(self):
        report = {
            "artifact_ref": "/tmp/out.json",
            "artifact_kind": "output",
            "ok": True,
            "findings": [
                {"code": "ARTIFACT_MISSING", "severity": "hard", "message": "file does not exist",
                 "location": "/tmp/out.json", "value": ""},
            ],
        }
        decision = evaluate_artifact_report_gate(report)
        assert not decision.allowed
        assert any("ARTIFACT_MISSING" in f.code for f in decision.findings)

    def test_artifact_gate_blocks_when_not_ok(self):
        report = {
            "artifact_ref": "/tmp/out.json",
            "artifact_kind": "output",
            "ok": False,
        }
        decision = evaluate_artifact_report_gate(report)
        assert not decision.allowed

    def test_artifact_gate_allows_clean_report(self):
        report = {
            "artifact_ref": "/tmp/out.json",
            "artifact_kind": "output",
            "ok": True,
        }
        decision = evaluate_artifact_report_gate(report)
        assert decision.allowed


# ============================================================
# Scenario 2: 产物缺失
# ============================================================
class TestScenario2ArtifactMissing:
    def test_missing_artifact_ref_blocked(self):
        report = {"artifact_kind": "output", "ok": True}
        decision = evaluate_artifact_report_gate(report)
        assert not decision.allowed
        assert any("ARTIFACT_REF_MISSING" in f.code for f in decision.findings)

    def test_missing_kind_blocked(self):
        report = {"artifact_ref": "/tmp/out.json", "ok": True}
        decision = evaluate_artifact_report_gate(report)
        assert not decision.allowed
        assert any("ARTIFACT_KIND_MISSING" in f.code for f in decision.findings)


# ============================================================
# Scenario 3: 空产物
# ============================================================
class TestScenario3EmptyArtifact:
    def test_empty_finding_code_blocked(self):
        report = {
            "artifact_ref": "/tmp/out.json",
            "artifact_kind": "output",
            "ok": True,
            "findings": [
                {"code": "ARTIFACT_EMPTY", "severity": "hard", "message": "file is empty",
                 "location": "/tmp/out.json", "value": "0 bytes"},
            ],
        }
        decision = evaluate_artifact_report_gate(report)
        assert not decision.allowed
        assert any("ARTIFACT_EMPTY" in f.code for f in decision.findings)

    def test_no_findings_allowed(self):
        report = {
            "artifact_ref": "/tmp/ok.txt",
            "artifact_kind": "output",
            "ok": True,
        }
        decision = evaluate_artifact_report_gate(report)
        assert decision.allowed


# ============================================================
# Scenario 4: checkpoint 坏 JSON
# ============================================================
class TestScenario4BadCheckpointJSON:
    def test_command_policy_rejects_null_byte(self):
        decision = evaluate_command_policy("ls\0-cat")
        assert not decision.allowed
        assert "COMMAND_ARGV_INVALID" in decision.finding_codes

    def test_command_policy_detects_dangerous_command(self):
        decision = evaluate_command_policy("sudo rm -rf /")
        assert not decision.allowed
        assert len(decision.findings) > 0

    def test_command_policy_empty_command(self):
        decision = evaluate_command_policy("")
        assert not decision.allowed
        assert "COMMAND_EMPTY" in decision.finding_codes


# ============================================================
# Scenario 5: 工具失败后模型说成功
# ============================================================
class TestScenario5ToolFailureModelClaimsSuccess:
    def test_guardrail_tracks_failures(self):
        config = ToolGuardrailConfig(repeat_fail_threshold=2)
        records = (
            {"tool_name": "terminal", "args_hash": "h1", "failed": True, "failure_class": "code:failed"},
            {"tool_name": "terminal", "args_hash": "h1", "failed": True, "failure_class": "code:failed"},
        )
        decision = evaluate_tool_guardrail_gate(
            ToolGuardrailFacts("terminal", "h1"),
            config=config,
            records=records,
        )
        assert decision.allowed
        assert any("REPEAT_FAILURE_HINT" in f.code for f in decision.findings)

    def test_guardrail_blocks_same_action_despite_optimism(self):
        config = ToolGuardrailConfig(repeat_fail_threshold=1)
        records = tuple(
            {"tool_name": "terminal", "args_hash": "h1", "failed": True, "failure_class": "code:failed"}
            for _ in range(3)
        )
        decision = evaluate_tool_guardrail_gate(
            ToolGuardrailFacts("terminal", "h1"),
            config=config,
            records=records,
        )
        assert not decision.allowed
        assert decision.recommended_action == "change_strategy"
        assert any("REPEAT_FAILURE_BLOCKED" in f.code for f in decision.findings)


# ============================================================
# Scenario 6: recovery packet 损坏
# ============================================================
class TestScenario6RecoveryPacketCorrupted:
    def test_pre_compact_detects_missing_recovery(self):
        state = {
            "task_id": "t1", "run_id": "r1",
            "contract": {"id": "c1"},
            "approval_refs": {"a1": "ok"},
            "artifact_refs": {"f1": "/tmp/f"},
            "pending_actions": ["write"],
            "failed_actions": [],
        }
        facts = CompactionGateFacts(pre_compact_state=state, phase="pre_compact")
        decision = evaluate_compaction_gate(facts)
        assert not decision.allowed
        assert any("MISSING_RECOVERY_PACKET" in f.code for f in decision.findings)

    def test_post_compact_detects_lost_recovery(self):
        pre = compaction_gate_snapshot({
            "task_id": "t1", "run_id": "r1",
            "contract": {"id": "c1"},
            "approval_refs": ["a1"], "artifact_refs": {"f1": "/f"},
            "recovery_packet": {"pid": "rp1"},
            "pending_actions": ["do_x"], "failed_actions": [],
        })
        post = dict(pre)
        del post["recovery_packet"]
        facts = CompactionGateFacts(
            pre_compact_state=pre, post_compact_state=post, phase="post_compact",
        )
        decision = evaluate_compaction_gate(facts)
        assert not decision.allowed
        assert any("LOST_RECOVERY_PACKET" in f.code for f in decision.findings)


# ============================================================
# Scenario 7: compact/resume 后继续返工
# ============================================================
class TestScenario7CompactResumeRework:
    def test_compact_preserved_allows_continue(self):
        pre = compaction_gate_snapshot({
            "task_id": "t1", "run_id": "r1",
            "contract": {"id": "c1"},
            "approval_refs": ["a1"], "artifact_refs": {"f1": "/f"},
            "recovery_packet": {"pid": "rp1"},
            "pending_actions": ["write"], "failed_actions": [],
        })
        post = dict(pre)
        post["pending_actions"] = ["verify"]
        facts = CompactionGateFacts(
            pre_compact_state=pre, post_compact_state=post, phase="post_compact",
        )
        decision = evaluate_compaction_gate(facts)
        assert decision.allowed

    def test_compact_lost_contract_blocks(self):
        pre = compaction_gate_snapshot({"contract": {"id": "c1"}})
        post = dict(pre)
        del post["contract"]
        facts = CompactionGateFacts(
            pre_compact_state=pre, post_compact_state=post, phase="post_compact",
        )
        decision = evaluate_compaction_gate(facts)
        assert not decision.allowed
        assert any("LOST_CONTRACT" in f.code for f in decision.findings)


# ============================================================
# Scenario 9: 等价动作绕过
# ============================================================
class TestScenario9EquivalentActionBypass:
    def test_rm_alias_normalized(self):
        from agent_py_agent.agent.contracts.gates.command_policy import command_name
        assert command_name("/bin/rm") == "rm"
        assert command_name("/usr/bin/rm") == "rm"
        assert command_name("rm") == "rm"

    def test_mkfs_prefix_detected(self):
        from agent_py_agent.agent.contracts.gates.command_policy import _is_dangerous_executable
        assert _is_dangerous_executable("mkfs.ext4")
        assert _is_dangerous_executable("mkfs.fat")
        assert _is_dangerous_executable("mkfs")

    def test_workspace_recursive_cleanup_allowed(self):
        decision = evaluate_command_policy("rm -rf /tmp/x")
        assert decision.allowed

    def test_root_recursive_delete_detected(self):
        decision = evaluate_command_policy("rm -r -f /")
        assert not decision.allowed
        assert any(f.evidence.get("pattern") == "RM_PROTECTED_TARGET" for f in decision.findings)

    def test_chmod_world_writable_allowed_by_command_policy(self):
        decision = evaluate_command_policy("chmod 777 file.txt")
        assert decision.allowed

    def test_chmod_0777_allowed_by_command_policy(self):
        decision = evaluate_command_policy("chmod 0777 file.txt")
        assert decision.allowed


# ============================================================
# Scenario 10: scope creep
# ============================================================
class TestScenario10ScopeCreep:
    def test_dangerous_path_blocked(self, tmp_path):
        from agent_py_agent.agent.contracts.gates.path_url_command import (
            PathUrlCommandFacts,
            evaluate_path_url_command_gate,
        )
        danger = tmp_path / "danger"
        danger.mkdir()
        payload = {"path": str(danger / "passwd")}
        facts = PathUrlCommandFacts(
            payload=payload,
            workspace_root=tmp_path,
            workspace_roots=[tmp_path],
            path_dangerous_roots=[str(danger)],
        )
        decision = evaluate_path_url_command_gate(facts)
        assert not decision.allowed
        assert any("PATH_DANGEROUS_ROOT_BLOCKED" in f.code for f in decision.findings)

    def test_private_network_blocked(self):
        facts = NetworkSafetyFacts(
            url="http://127.0.0.1:8080/api",
            resolver=lambda host: ["127.0.0.1"] if host == "127.0.0.1" else [],
        )
        decision = evaluate_network_safety_gate(facts)
        assert not decision.allowed
        assert any("PRIVATE_HOST" in f.code for f in decision.findings)

    def test_localhost_explicit_allow(self):
        facts = NetworkSafetyFacts(
            url="http://localhost:3000/api",
            resolver=lambda host: ["127.0.0.1"] if "localhost" in host else [],
            allowed_private_hosts=("localhost",),
        )
        decision = evaluate_network_safety_gate(facts)
        assert decision.allowed

    def test_file_url_blocked(self):
        from agent_py_agent.agent.contracts.gates.path_url_command import (
            PathUrlCommandFacts,
            evaluate_path_url_command_gate,
        )
        payload = {"url": "file:///etc/passwd"}
        facts = PathUrlCommandFacts(
            payload=payload,
            workspace_root=Path("/tmp"),
        )
        decision = evaluate_path_url_command_gate(facts)
        assert not decision.allowed
        assert any("FILE_URL_BLOCKED" in f.code for f in decision.findings)
