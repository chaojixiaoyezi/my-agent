"""Regression tests for current model-visible subagent refs."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock


def _mock_create_agent():
    mock_agent = MagicMock()
    mock_agent.config.enable_subagents = True
    mock_agent.config.max_subagents = 10
    mock_agent.tools.specs.return_value = []
    mock_agent.subagents.workspace_root = Path("/tmp/project")
    mock_agent.subagents.workspace_roots = [Path("/tmp/project")]
    mock_agent.subagents.workspace = Path("/tmp/project/tasks/current/work/agents")
    task = MagicMock()
    task.id = "run_0"
    task.goal = ""
    task.status = "PLANNING"
    task.verification_status = "UNVERIFIED"
    task.task_dir = "/tmp/project/tasks/current/work/agents/run_0"
    task.output_json = "/tmp/project/tasks/current/work/agents/run_0/output.json"
    mock_agent.subagents.create_run.return_value = task
    return mock_agent


def test_create_subagents_payload_keeps_current_paths_visible(monkeypatch):
    """create_subagents 不再调用旧路径隐藏器。"""
    import agent_py_agent.agent.agent_core.orchestration.background.dispatch as background_dispatch
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    mock_agent = _mock_create_agent()

    def fake_background_start(agent, run_ids):
        return {
            "status": "started",
            "dispatch_mode": "background",
            "run_ids": list(run_ids),
            "agent_tree": {
                "schema_version": "agent_tree_status.v1",
                "nodes": [
                    {
                        "run_id": "run_0",
                        "workspace_refs": {
                            "final_report": "/tmp/project/tasks/current/work/agents/run_0/final_report.md",
                        },
                    }
                ],
            },
        }

    monkeypatch.setattr(background_dispatch, "_start_background_dispatch", fake_background_start)

    result = CreateSubagentsTool(mock_agent).execute({"goal": "整理资料"})

    assert result.ok is True
    assert "[internal_legacy_subagent_path_hidden]" not in result.output


def test_model_visible_ref_sanitizer_module_is_removed():
    """旧路径 sanitizer 不再是主链路的一部分。"""
    import importlib.util

    assert importlib.util.find_spec("agent_py_agent.agent.model_visible_ref_sanitizer") is None


def test_model_visible_text_is_passthrough():
    """模型可见文本不再做旧路径替换。"""
    from agent_py_agent.agent.model_visible_refs import current_model_text

    text = "请读取 /tmp/project/tasks/current/work/agents/subagent/data_collection.md 后继续。"

    assert current_model_text(text) == text
