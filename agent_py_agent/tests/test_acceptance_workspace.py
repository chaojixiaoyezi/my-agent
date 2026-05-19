from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.subagents.acceptance_workspace import acceptance_workspace_root_for_task


# LLM: parent acceptance must follow absolute product refs even when the manager root is the subagent runtime dir.
# 函数用途: 复现验收修复卡的 artifact_integrity 缺 file_path，因为验收 cwd 选到了 .my_agent/subagents。
def test_acceptance_workspace_falls_back_to_artifact_dir_when_manager_roots_do_not_contain_refs(tmp_path):
    runtime_root = tmp_path / ".my_agent" / "subagents"
    runtime_root.mkdir(parents=True)
    artifact = tmp_path / "lab_outputs" / "shop-demo" / "index.html"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("<html><body>ok</body></html>", encoding="utf-8")
    manager = SimpleNamespace(workspace_root=runtime_root, workspace=runtime_root, workspace_roots=[])
    task = SimpleNamespace(
        artifact_refs=[str(artifact)],
        allowed_write_roots=[str(artifact.parent)],
        task_dir=str(runtime_root / "subagent-1"),
    )

    root = acceptance_workspace_root_for_task(
        manager,
        task=task,
        output={"artifacts": [{"path": str(artifact)}]},
    )

    assert root == artifact.parent
