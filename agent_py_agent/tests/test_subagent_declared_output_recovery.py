"""T5 实测修复:子代理把 declared output 同名文件写到自己 work 目录(没落共享 output)时,
materialize 回退扫 work 捞回同名文件填进 declared 槽,而非只放 placeholder 占位桩——
主代理读 output 即得真内容,免去"读桩→瞎找 work→重写"的 16 轮绕路。"""

from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.subagents.result_artifact_evidence import (
    materialize_missing_declared_output_artifacts,
)


def _task(workdir: Path):
    return SimpleNamespace(
        attributes={"output_files": ["report.md"]},
        task_workspace_dir=str(workdir),
        agent_run_workspace_dir="",
        task_dir="",
        id="run-1",
        root_id="task-1",
    )


def _parsed():
    return SimpleNamespace(
        status="DONE", summary="结构化兜底摘要", findings=[],
        evidence_packets=[], artifacts=[], next_actions=[],
    )


def test_recovers_same_name_file_from_work(tmp_path):
    """子代理把 declared 同名文件写在 work 子目录 → materialize 复制进 declared 槽,非占位桩。"""
    sub = tmp_path / "agents" / "sub-1"
    sub.mkdir(parents=True)
    (sub / "report.md").write_text("真实报告内容\n详细分析正文...", encoding="utf-8")

    out = materialize_missing_declared_output_artifacts(_task(tmp_path), _parsed(), [])

    assert len(out) == 1
    declared = tmp_path / "report.md"
    assert declared.exists()
    assert "真实报告内容" in declared.read_text(encoding="utf-8")  # 复制了真内容
    assert out[0].get("placeholder") is not True                  # 不是占位桩


def test_placeholder_when_no_same_name_in_work(tmp_path):
    """work 里没有 declared 同名文件 → 保持 placeholder(诚实占位,旧行为不变)。"""
    sub = tmp_path / "agents" / "sub-1"
    sub.mkdir(parents=True)
    (sub / "other.md").write_text("无关文件", encoding="utf-8")

    out = materialize_missing_declared_output_artifacts(_task(tmp_path), _parsed(), [])

    assert len(out) == 1
    assert out[0].get("placeholder") is True  # 找不到同名 → 占位桩


def test_placeholder_when_multiple_same_name_ambiguous(tmp_path):
    """work 里有多个 declared 同名文件(歧义)→ 不瞎猜,保持 placeholder。"""
    for sid in ("sub-1", "sub-2"):
        d = tmp_path / "agents" / sid
        d.mkdir(parents=True)
        (d / "report.md").write_text(f"report from {sid}", encoding="utf-8")

    out = materialize_missing_declared_output_artifacts(_task(tmp_path), _parsed(), [])

    assert len(out) == 1
    assert out[0].get("placeholder") is True  # 多个同名歧义 → 不瞎猜


def test_recovers_when_declared_claimed_but_not_written(tmp_path):
    """A1:子代理声明 artifact path 在 output 但实际没写到那(写在 work)→ 不被 existing_refs
    跳过(声明≠真写到),回退扫 work 捞回真内容(修 T5 schema-version 子代理读扑空瞎找)。"""
    sub = tmp_path / "agents" / "sub-1"
    sub.mkdir(parents=True)
    (sub / "report.md").write_text("真实报告(子代理写在 work)", encoding="utf-8")
    claimed = [{"path": str(tmp_path / "report.md"), "kind": "md"}]  # 声明在 output 但没真写

    out = materialize_missing_declared_output_artifacts(_task(tmp_path), _parsed(), claimed)

    declared = tmp_path / "report.md"
    assert declared.exists()
    assert "真实报告" in declared.read_text(encoding="utf-8")  # 声明但没写 → 仍捞回填入
