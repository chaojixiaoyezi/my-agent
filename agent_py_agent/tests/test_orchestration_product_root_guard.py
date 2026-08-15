"""LLM: Tests product-root handling for root/coordinator orchestration seeds.

函数/模块用途: 产物路径是目标事实，不是写入白名单；普通输出目录不应要求模型补 extra_write_roots。
"""

from pathlib import Path
from unittest.mock import MagicMock

from agent_py_agent.agent.agent_core.orchestration.tool_specs import (
    build_create_subagents_model_spec,
)
from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool


def test_explicit_coordinator_product_delivery_uses_output_files_without_extra_write_root():
    mock_agent = MagicMock()
    mock_agent.config.enable_subagents = True
    mock_agent.config.max_subagents = 10
    mock_agent.subagents.workspace_root = "/tmp/project"
    mock_task = MagicMock()
    mock_task.id = "site_001"
    mock_task.task_dir = "/tmp/project/data/subagents/site_001"
    mock_task.goal = "交付示例网站。"
    mock_task.status = "RUNNING"
    mock_task.verification_status = "UNVERIFIED"
    mock_agent.subagents.create_run.return_value = mock_task

    tool = CreateSubagentsTool(mock_agent)
    result = tool.execute(
        {
            "goal": "交付示例网站。",
            "output_files": ["build/index.html", "build/style.css", "build/app.js"],
            "role": "coordinator",
        }
    )

    assert result.ok is True
    params = mock_agent.subagents.create_run.call_args.kwargs["params"]
    assert params.extra_write_roots == [str(Path("/tmp/project/build").resolve(strict=False))]


def test_create_subagents_model_spec_does_not_require_write_root_parameters():
    spec = build_create_subagents_model_spec()
    rendered = "\n".join(
        [
            spec.description,
            str(spec.parameter_descriptions),
            str(spec.examples),
        ]
    )

    assert "allowed_write_roots" not in rendered
    assert "extra_write_roots" not in rendered
    assert "output_files" in rendered
