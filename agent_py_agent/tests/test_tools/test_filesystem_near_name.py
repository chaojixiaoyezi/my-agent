from __future__ import annotations

from pathlib import Path


# LLM: Near-name suggestions only run on an already-failed path resolution, so these cases all go
# through the real ToolHandlerOutcome surface where the model actually sees them.
# 说明: 覆盖「命中 / 不命中 / 超上限不扫 / 越权不出现」四类行为，并断言结构化字段。
def _near_name_tool(tmp_path: Path, subdir: str = "docs"):
    """造一个只有该子目录的读取工具，模拟真实工作区。"""
    from agent_py_agent.agent.tooling._filesystem_read import ReadFileTool

    root = tmp_path / "workspace"
    (root / subdir).mkdir(parents=True)
    return root, root / subdir, ReadFileTool(root, max_chars=2000)


def test_missing_path_suggests_near_name_in_same_directory(tmp_path: Path):
    """LLM: A one-character typo must surface the real sibling name.

    新手说明:
    用户把 final_report.md 写成 finl_report.md 时，回执里应该出现正确的文件名，
    但系统不能自动改路径、也不能真的去读它。
    """
    root, docs, tool = _near_name_tool(tmp_path)
    (docs / "final_report.md").write_text("real", encoding="utf-8")
    (docs / "unrelated.md").write_text("x", encoding="utf-8")

    result = tool.execute({"path": "docs/finl_report.md"})

    assert not result.ok
    assert result.error_code == "PATH_NOT_FOUND"
    candidates = result.result_envelope["candidate_paths"]
    assert str(docs / "final_report.md") in candidates
    assert "final_report.md" in result.output
    # 只提示，不自动改写：请求路径原样保留。
    assert result.result_envelope["requested_path"] == "docs/finl_report.md"


def test_missing_path_without_near_name_returns_no_suggestion(tmp_path: Path):
    """LLM: A genuinely different name must not be guessed at.

    新手说明:
    目录里有个 summary.md，但用户要的是 2026-budget.yaml —— 两者毫不相干，
    这时候给建议只会把人带偏，所以必须给空。
    """
    root, docs, tool = _near_name_tool(tmp_path)
    (docs / "summary.md").write_text("x", encoding="utf-8")

    result = tool.execute({"path": "docs/2026-budget.yaml"})

    assert not result.ok
    assert result.error_code == "PATH_NOT_FOUND"
    candidates = result.result_envelope["candidate_paths"]
    assert str(docs / "summary.md") not in candidates
    assert all("summary" not in path for path in candidates)


def test_missing_path_in_huge_directory_skips_near_name_scan(tmp_path: Path):
    """LLM: Above the entry cap the directory is not scanned at all.

    新手说明:
    目录里被塞了几百个生成文件时，宁可不给建议，也不能让一次读错路径变慢。
    这里放进超过上限的条目，断言那个"真正相近"的名字**不会**出现。
    """
    from agent_py_agent.agent.tooling import filesystem_path_recovery as recovery

    root, docs, tool = _near_name_tool(tmp_path)
    (docs / "final_report.md").write_text("real", encoding="utf-8")
    for index in range(recovery._NEAR_NAME_MAX_DIRECTORY_ENTRIES + 5):
        (docs / f"generated-{index:05d}.txt").write_text("x", encoding="utf-8")

    assert len(list(docs.iterdir())) > recovery._NEAR_NAME_MAX_DIRECTORY_ENTRIES
    # 直接调近名实现，排除跨目录候选的干扰。
    near = recovery.suggest_near_name_paths(
        "finl_report.md",
        docs,
        recovery.NearNameScope(workspace_roots=[root]),
    )
    assert near == []


def test_near_name_never_leaks_outside_granted_roots(tmp_path: Path):
    """LLM: A directory outside the granted roots must not leak its filenames.

    新手说明:
    不给权限的目录里有哪些文件，不能靠"相近文件名建议"被看出来 —— 必须返回空。
    """
    from agent_py_agent.agent.tooling import filesystem_path_recovery as recovery

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "final_report.md").write_text("secret", encoding="utf-8")
    granted = tmp_path / "granted"
    granted.mkdir()

    near = recovery.suggest_near_name_paths(
        "finl_report.md",
        outside,
        recovery.NearNameScope(workspace_roots=[granted]),
    )
    assert near == []


def test_near_name_returns_at_most_two_suggestions(tmp_path: Path):
    """LLM: The cap keeps a wrong path from producing a wall of guesses.

    新手说明:
    同目录里好几个都"长得像"时，只给最像的两个，避免用户在一堆候选里挑花眼。
    """
    from agent_py_agent.agent.tooling import filesystem_path_recovery as recovery

    root = tmp_path / "workspace"
    docs = root / "docs"
    docs.mkdir(parents=True)
    for name in ("alpha_one.md", "alpha_two.md", "alpha_three.md", "alpha_four.md"):
        (docs / name).write_text("x", encoding="utf-8")

    near = recovery.suggest_near_name_paths(
        "alpha_xne.md",
        docs,
        recovery.NearNameScope(workspace_roots=[root]),
    )
    assert len(near) <= recovery._NEAR_NAME_MAX_SUGGESTIONS
