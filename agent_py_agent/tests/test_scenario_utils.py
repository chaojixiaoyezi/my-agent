from __future__ import annotations

"""scenario-test helper regressions."""

import tempfile
from pathlib import Path

from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.cli.scenario_utils import (
    build_scenario_runner_instruction,
    collect_scenario_report_files,
)


def test_collect_scenario_report_files_reads_task_dir_outputs():
    """场景测试最终核对要读取子代理真实允许写入的 task_dir 产物。"""

    with tempfile.TemporaryDirectory() as td:
        fixture_root = Path(td)
        agent = SimpleAgent(
            AgentConfig(subagent_workspace=".my_agent/subagents"),
            fixture_root,
        )
        task = agent.subagents.create_run(
            goal="写场景报告",
            thought="报告必须落在 task_dir 内。",
            plan=["read", "write"],
            allowed_tools=["read_file", "write_file"],
        )
        report_dir = Path(task.task_dir) / "scenario_outputs"
        report_dir.mkdir(parents=True)
        report_file = report_dir / f"{task.id}.md"
        report_file.write_text("ok\n", encoding="utf-8")

        reports = collect_scenario_report_files(agent, fixture_root, expected_count=1)

        assert reports == [report_file]


def test_runner_instruction_mentions_write_boundary_target():
    """runner 提示词要明确 task_dir/allowed_write_roots，避免模型先写错旧路径。"""

    instruction = build_scenario_runner_instruction()

    assert "allowed_write_roots" in instruction
    assert "task_dir/scenario_outputs/<run_id>.md" in instruction
