from __future__ import annotations

"""LLM: regression tests for workspace boundary and shared merge safety.

给人看的解释：
这里专门覆盖架构评审指出的 P0 风险：artifact 不能越界读取，
shared workspace 不能被最后一个子代理覆盖。
"""

import json
from pathlib import Path

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.models import EvidencePacket, Finding


def test_subagent_artifact_manifest_blocks_outside_workspace_refs(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    outside_path = tmp_path.parent / f"{tmp_path.name}-outside-artifact.txt"
    outside_path.write_text("outside artifact body\n", encoding="utf-8")

    task = manager.create_run(
        goal="拦截越界 artifact",
        thought="绝对路径不能让 manifest 到 workspace 外读取内容。",
        plan=["保存 manifest", "确认 blocked"],
    )
    task.artifact_refs = [str(outside_path)]
    manager.save(task)

    loaded = manager.load(task.id)
    record = _read_jsonl(loaded.task_artifact_manifest_jsonl)[0]

    assert record["ref"] == str(outside_path)
    assert record["resolution_status"] == "blocked_outside_workspace"
    assert record["exists"] is False
    assert record["size_bytes"] == 0
    assert record["sha256"] == ""
    assert "outside artifact body" not in json.dumps(record, ensure_ascii=False)


def test_subagent_shared_workspace_merges_sibling_facts(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    first = _task_with_shared_fact(manager, "finding-shared-1", "evpkt-shared-1", "第一个 sibling 的发现")
    manager.save(first)
    second = _task_with_shared_fact(manager, "finding-shared-2", "evpkt-shared-2", "第二个 sibling 补充了验收风险")
    second.root_id = first.root_id
    manager.save(second)

    first.findings[0].claim = "第一个 sibling 的发现已更新"
    manager.save(first)

    loaded = manager.load(second.id)
    findings = _read_jsonl(loaded.task_workspace_shared_findings_jsonl)
    evidence_index = _read_jsonl(loaded.task_workspace_shared_evidence_index_jsonl)
    blackboard = Path(loaded.task_workspace_shared_blackboard).read_text(encoding="utf-8")

    assert [item["id"] for item in findings] == ["finding-shared-1", "finding-shared-2"]
    assert [item["id"] for item in evidence_index] == ["evpkt-shared-1", "evpkt-shared-2"]
    assert findings[0]["claim"] == "第一个 sibling 的发现已更新"
    assert "第二个 sibling 补充了验收风险" in blackboard
    assert "第一个 sibling 的发现已更新" in blackboard


def _task_with_shared_fact(manager: SubAgentManager, finding_id: str, evidence_id: str, claim: str):
    task = manager.create_run(
        goal="共享子代理任务局部事实",
        thought="shared 目录只放结构化 facts，不写主 memory。",
        plan=["写 evidence packet", "写 finding", "同步 shared"],
    )
    task.status = "RUNNING"
    task.current_step = "同步 shared workspace"
    task.latest_summary = claim
    task.evidence_packets = [
        EvidencePacket(
            id=evidence_id,
            claim=claim,
            checked_scope="shared workspace",
            evidence_refs=["reports/status_report.json"],
            artifact_refs=["output.json"],
            confidence=0.82,
        ),
    ]
    task.findings = [
        Finding(
            id=finding_id,
            claim=claim,
            status="OPEN",
            severity="P2",
            evidence_packet_ids=[evidence_id],
            evidence_refs=["reports/status_report.json"],
            confidence=0.74,
        ),
    ]
    return task


def _read_jsonl(path: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
