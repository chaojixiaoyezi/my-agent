"""LLM: runner input dependency tests protect dispatch from blocking valid project reads.

函数/模块用途: 覆盖子代理读取项目根文件时，输入依赖检查必须看项目工作区而不是只看私有运行目录。
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.runner_input_dependencies import (
    missing_input_dependencies,
    params_input_refs,
    params_output_refs,
)


# LLM: Project-root read refs should not block a worker whose task_dir lives under .my_agent/subagents.
# 函数用途: 真实 E2E 中小傻妞读取 README.md 时，只要项目根存在该文件，dispatch 不应把它误判成缺上游产物。
def test_missing_input_dependencies_accepts_project_root_file_for_my_agent_workspace(tmp_path):
    workspace = tmp_path / "fixture_project"
    run_dir = workspace / ".my_agent" / "subagents" / "subagent-worker"
    run_dir.mkdir(parents=True)
    (workspace / "README.md").write_text("project readme", encoding="utf-8")
    task = SimpleNamespace(
        goal="读取 README.md，汇总关键信息，写入 task_dir/scenario_outputs/report.md",
        task_dir=str(run_dir),
        allowed_write_roots=[str(run_dir)],
        context_manifest={"required_read_paths": ["README.md"]},
    )

    assert missing_input_dependencies(task) == []


# LLM: Missing refs still block when neither run-local nor derived project roots contain them.
# 函数用途: 防止放宽项目根后把真正缺失的输入文件误放行。
def test_missing_input_dependencies_still_reports_absent_project_file(tmp_path):
    workspace = tmp_path / "fixture_project"
    run_dir = workspace / ".my_agent" / "subagents" / "subagent-worker"
    run_dir.mkdir(parents=True)
    task = SimpleNamespace(
        goal="读取 AGENTS.md，汇总关键信息",
        task_dir=str(run_dir),
        allowed_write_roots=[str(run_dir)],
        context_manifest={"required_read_paths": ["AGENTS.md"]},
    )

    assert missing_input_dependencies(task) == ["AGENTS.md"]


# LLM: Structured required refs stay hard even when natural prose says optional.
# 函数用途: required_read_paths 是硬合同；“如有则读”这类自然语言不能让代码层把缺文件放行。
def test_missing_input_dependencies_keeps_structured_refs_required(tmp_path):
    workspace = tmp_path / "fixture_project"
    run_dir = workspace / ".my_agent" / "subagents" / "subagent-worker"
    run_dir.mkdir(parents=True)
    task = SimpleNamespace(
        goal="读取 AGENTS.md（如有）或 SOUL.md（如有），汇总关键信息",
        task_dir=str(run_dir),
        allowed_write_roots=[str(run_dir)],
        context_manifest={"required_read_paths": ["AGENTS.md", "SOUL.md"]},
    )

    assert missing_input_dependencies(task) == ["AGENTS.md", "SOUL.md"]


# LLM: Optional subdirectory wording must not make the main file optional.
# 函数用途: “README.md（若有子目录则继续读）”缺 README.md 时仍应阻塞，避免误放行缺核心输入。
def test_missing_input_dependencies_keeps_main_file_required_when_subdirs_optional(tmp_path):
    workspace = tmp_path / "fixture_project"
    run_dir = workspace / ".my_agent" / "subagents" / "subagent-worker"
    run_dir.mkdir(parents=True)
    task = SimpleNamespace(
        goal="读取 README.md（若有子目录则继续读），汇总关键信息",
        task_dir=str(run_dir),
        allowed_write_roots=[str(run_dir)],
        context_manifest={"required_read_paths": ["README.md"]},
    )

    assert missing_input_dependencies(task) == ["README.md"]


# LLM: Output refs must come from tool parameters, not natural-language goal text.
# 函数用途: 复现家具页 E2E：普通描述里的路径不参与依赖判断，output_files 参数才是机器事实。
def test_output_path_ref_is_parameter_fact_not_goal_fact(tmp_path):
    workspace = tmp_path / "fixture_project"
    run_dir = workspace / ".my_agent" / "subagents" / "subagent-worker"
    run_dir.mkdir(parents=True)
    task = SimpleNamespace(
        goal="技术：单文件 HTML，不依赖外部图片/字体/脚本，用 CSS 完成视觉效果。",
        task_dir=str(run_dir),
        allowed_write_roots=[str(workspace)],
        context_manifest={},
        attributes={"output_files": ["lab_outputs/site-output/index.html"]},
    )

    assert params_input_refs({"goal": "required_read_paths: should-not-count.md"}) == []
    assert params_output_refs({"output_files": ["lab_outputs/site-output/index.html"]}) == [
        "lab_outputs/site-output/index.html"
    ]
    assert missing_input_dependencies(task) == []
