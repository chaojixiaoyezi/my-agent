"""LLM: Tests product-root handling for root/coordinator orchestration seeds.

函数/模块用途: 产物路径是目标事实，不是写入白名单；普通输出目录不应要求模型补 extra_write_roots。
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_py_agent.agent.agent_core.orchestration.tool_specs import (
    build_create_subagents_model_spec,
)
from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool


# 这些用例拿 MagicMock 当 agent；建子代理时会按 agent.home_paths.owner_home_dir 写任务进度，MagicMock 当路径用时是
# 相对路径 "MagicMock/..."。每条测试先切到自己的临时目录，免得写进仓库根（conftest 的仓库树防线会让这种测试报错）。
@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


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
    assert params.extra_write_roots == [
        str(Path("/tmp/project").resolve(strict=False)),
    ]


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
