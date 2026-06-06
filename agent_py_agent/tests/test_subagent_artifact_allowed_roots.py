"""LLM: artifact manifest regressions for authorized product output roots.

函数/模块用途: 确认 leaf 写到 `allowed_write_roots` 的业务产物能被 manifest 安全登记。
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.result_artifact_evidence import normalize_artifact_ref


def test_subagent_persistence_resolves_allowed_product_artifacts(tmp_path) -> None:
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables"

    task = manager.create_run(
        goal="外部产物目录引用",
        thought="leaf 写业务产物，manifest 只登记元数据。",
        plan=["写 artifact", "保存 manifest"],
        extra_write_roots=[str(deliverables)],
    )
    artifact_path = deliverables / "build" / "index.html"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text("<h1>ok</h1>\n", encoding="utf-8")
    task.artifact_refs = [str(artifact_path)]
    manager.save(task)

    loaded = manager.load(task.id)
    record = _read_jsonl(loaded.agent_run_artifact_manifest_jsonl)[0]

    assert record["ref"] == str(artifact_path)
    assert record["path"] == str(artifact_path)
    assert record["exists"] is True
    assert record["resolution_status"] == "resolved"
    assert record["size_bytes"] == artifact_path.stat().st_size


def test_subagent_artifact_ref_uses_declared_workspace_root(tmp_path) -> None:
    manager = SubAgentManager(tmp_path / ".my-agent" / "subagents", workspace_root=tmp_path)
    task = manager.create_run(goal="登记项目根相对产物", thought="", plan=["write"])
    artifact_path = tmp_path / "outputs" / "report.md"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text("ok\n", encoding="utf-8")

    assert normalize_artifact_ref(task, "outputs/report.md") == str(artifact_path)


def test_subagent_artifact_ref_does_not_infer_workspace_from_stale_subagent_path(tmp_path) -> None:
    current = tmp_path / "current"
    old = tmp_path / "old"
    manager = SubAgentManager(current / ".my-agent" / "subagents", workspace_root=current)
    task = manager.create_run(goal="不要从旧路径反推根目录", thought="", plan=["write"])
    task.task_dir = str(old / ".my-agent" / "subagents" / task.id)
    stale_artifact = old / "outputs" / "report.md"
    stale_artifact.parent.mkdir(parents=True)
    stale_artifact.write_text("old\n", encoding="utf-8")

    assert normalize_artifact_ref(task, "outputs/report.md") == "outputs/report.md"


def test_subagent_workspace_root_attribute_comes_from_current_manager(tmp_path) -> None:
    current = tmp_path / "current"
    old = tmp_path / "old"
    manager = SubAgentManager(current / ".my-agent" / "subagents", workspace_root=current)

    task = manager.create_run(
        goal="结构化 workspace root 不能被旧参数覆盖",
        thought="",
        plan=["write"],
        attributes={"workspace_root": str(old), "workspace_roots": [str(old)]},
    )

    assert task.attributes["workspace_root"] == str(current.resolve())
    assert task.attributes["workspace_roots"] == [str(current.resolve())]


def _read_jsonl(path: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
