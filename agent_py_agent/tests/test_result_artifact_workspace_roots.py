"""LLM: artifact refs under the product workspace must validate consistently.

函数/模块用途: 覆盖真实 E2E 中子代理把产物写到任务工作区公共产物路径，
但 artifact integrity 只看 run-local 目录而误判缺失的回归场景。
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.subagents.models import SubAgentParsedOutput
from agent_py_agent.agent.subagents.result_artifact_evidence import normalize_artifact_ref
from agent_py_agent.agent.subagents.result_artifact_integrity import missing_local_artifact_refs


# LLM: Product workspace relative refs should resolve from the workspace root, not only the run dir.
# 函数用途: 子代理声明 `data/subagents/<alias>/file` 时，只要任务工作区里真实存在，就不能误报 missing_artifact_refs。
def test_workspace_product_artifact_ref_is_not_missing_from_run_local_task(tmp_path) -> None:
    workspace = tmp_path / "task_18_short_github_star_growth_xlsx"
    run_dir = workspace / "data" / "subagents" / "subagent-actual-run"
    run_dir.mkdir(parents=True)
    artifact_ref = "data/subagents/subagent_data_collection/star_data.md"
    artifact = workspace / artifact_ref
    artifact.parent.mkdir(parents=True)
    artifact.write_text("github star data", encoding="utf-8")
    task = SimpleNamespace(
        task_dir=str(run_dir),
        output_dir=str(run_dir / "output"),
        reports_dir=str(run_dir / "reports"),
        agent_run_workspace_dir="",
        task_workspace_artifacts_dir="",
        agent_run_artifacts_dir="",
        task_workspace_shared_dir="",
        task_workspace_dir="",
        data_dir="",
        scratch_dir="",
        allowed_write_roots=[str(run_dir)],
        child_ids=[],
    )
    parsed = SubAgentParsedOutput(
        found=True,
        ok=True,
        parse_error="",
        status="DONE",
        evidence_packets=[{
            "claim": "star data collected",
            "checked_scope": "task workspace product path",
            "artifact_refs": [artifact_ref],
            "confidence": 0.9,
        }],
    )

    assert normalize_artifact_ref(task, artifact_ref) == str(artifact)
    assert missing_local_artifact_refs(task, parsed, []) == []
