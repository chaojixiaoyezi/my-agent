"""LLM: Focused runner-context acceptance dispatch tests.

函数/模块用途: 验证父 runner 内部 dispatch 能在真实测试通过后收口直接 child，同时不扩大发散的大型 dispatch 测试文件。
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.subagent import EvidencePacket, VerificationEvidence


# LLM: _setup_review_task creates a child waiting for parent acceptance with refs-bearing evidence.
# 函数用途: 构造等待验收的子任务、output.json 和 evidence packet，供 runner-context dispatch 测试使用。
def _setup_review_task(agent, task, *, patch_status="applied", patch_summary="已应用"):
    task.status = "AWAITING_ACCEPTANCE"
    task.verification_status = "NEEDS_ACCEPTANCE"
    task.channel_status = "OK"
    task.evidence.append(
        VerificationEvidence(kind="note", summary="有验收证据", ok=True, created_at=time.time())
    )
    task.evidence_packets.append(EvidencePacket(
        id="evpkt-dispatch",
        claim="调度任务已有验收证据",
        checked_scope="dispatch review fixture",
        evidence_refs=[task.acceptance_file],
        artifact_refs=[task.output_json],
        confidence=0.9,
        created_at=time.time(),
    ))
    agent.subagents.save(task)
    Path(task.output_json).write_text(
        json.dumps({
            "run_id": task.id,
            "tests": [{"name": "smoke", "validation_method": "file_check", "file_path": "README.md", "ok": True}],
            "patches": [{"path": "agent_py_agent/agent/demo.py", "status": patch_status, "summary": patch_summary}],
            "blockers": [],
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return task


# LLM: _write_output_patches keeps the fixture in the no-pending-patch acceptance path.
# 函数用途: 覆盖 output.json 里的 patches 列表，让测试聚焦 runner-context follow-up apply。
def _write_output_patches(task, patches: list[dict[str, object]]) -> None:
    output_path = Path(task.output_json)
    output = json.loads(output_path.read_text(encoding="utf-8"))
    output["patches"] = patches
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")


# LLM: test_runner_context_dispatch_applies_passed_child_acceptance_followup covers the active parent runner path.
# 函数用途: 确认 runner 内 dispatch 默认跑 tests，并在 follow-up 可验收时把 direct child 置为 DONE/VERIFIED。
def test_runner_context_dispatch_applies_passed_child_acceptance_followup():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        (root / "README.md").write_text("runner-context acceptance apply\n", encoding="utf-8")
        parent = agent.subagents.create_run(
            goal="parent runner", thought="dispatch child", plan=["dispatch"], role="coordinator",
        )
        child = _setup_review_task(
            agent,
            agent.subagents.create_run(
                goal="child awaiting acceptance",
                thought="tests pass, parent should apply follow-up",
                plan=["test", "accept"],
                parent_id=parent.id,
                root_id=parent.id,
                depth=1,
            ),
            patch_status="none",
        )
        _write_output_patches(child, [])
        agent._current_subagent_run_id = parent.id

        try:
            result = DispatchSubagentsTool(agent).execute({})
        finally:
            agent._current_subagent_run_id = ""

        payload = json.loads(result.output)
        loaded = agent.subagents.load(child.id)
        record = next(item for item in payload["records"] if item["step"] == "acceptance")
        assert result.ok is True
        assert record["run_id"] == child.id
        assert record["applied"] is True
        assert record["after_status"] == "DONE"
        assert loaded.status == "DONE"
        assert loaded.verification_status == "VERIFIED"
