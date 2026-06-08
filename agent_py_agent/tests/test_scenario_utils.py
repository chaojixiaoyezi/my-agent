from __future__ import annotations

"""scenario-test helper regressions."""

import tempfile
from pathlib import Path

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.cli.scenario_utils import (
    build_scenario_prompt,
    build_scenario_runner_instruction,
    collect_scenario_report_files,
)
from agent_py_agent.cli.scenario_workspace import ScenarioConfigRequest, write_scenario_config


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


def test_collect_scenario_report_files_reads_output_json_artifacts():
    """场景测试最终核对应信任子代理 output.json 登记的真实产物。"""

    with tempfile.TemporaryDirectory() as td:
        fixture_root = Path(td)
        agent = SimpleAgent(
            AgentConfig(subagent_workspace=".my_agent/subagents"),
            fixture_root,
        )
        task = agent.subagents.create_run(
            goal="写场景报告",
            thought="报告可以落在 agent run workspace 内。",
            plan=["read", "write"],
            allowed_tools=["read_file", "write_file"],
        )
        report_dir = Path(task.agent_run_workspace_dir) / "scenario_outputs"
        report_dir.mkdir(parents=True)
        report_file = report_dir / f"{task.id}.md"
        report_file.write_text("ok\n", encoding="utf-8")
        Path(task.output_json).write_text(
            '{"artifacts":[{"path":"' + str(report_file) + '","kind":"report"}]}',
            encoding="utf-8",
        )

        reports = collect_scenario_report_files(agent, fixture_root, expected_count=1)

        assert reports == [report_file]


def test_runner_instruction_mentions_write_boundary_target():
    """runner 提示词要明确 task_dir/allowed_write_roots，避免模型先写错旧路径。"""

    instruction = build_scenario_runner_instruction()

    assert "allowed_write_roots" in instruction
    assert "task_dir/scenario_outputs/<run_id>.md" in instruction


def test_scenario_main_prompt_stays_plain_user_language():
    """真实主代理场景 prompt 不应把内部工具/协议名直接塞给模型。"""

    prompt = build_scenario_prompt(2)

    assert "2 个帮手" in prompt
    for internal in ("create_subagents", "dispatch_subagents", "start_runners", "SUBAGENT_RESULT"):
        assert internal not in prompt


def test_write_scenario_config_persists_runner_stress_overrides(tmp_path):
    source = tmp_path / "agent_config.yaml"
    target = tmp_path / "scenario_agent_config.yaml"
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    source.write_text("model_backend: echo\n", encoding="utf-8")

    write_scenario_config(
        ScenarioConfigRequest(
            source_config=source,
            target_config=target,
            fixture_root=fixture,
            request_timeout=180,
            max_subagents=10,
            runner_concurrency="5",
            runner_start_rate="10",
            model_request_timeout=240,
        )
    )

    text = target.read_text(encoding="utf-8")
    assert 'runner_concurrency: "5"' in text
    assert 'runner_start_rate: "10"' in text
    assert "request_timeout: 240" in text
