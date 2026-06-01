"""Regression tests for model-visible subagent refs."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock


# LLM: _mock_create_agent keeps path-redaction tests out of the larger create_subagents suite.
# 函数用途: 构造最小 create_subagents agent 替身，专测模型可见路径净化。
def _mock_create_agent():
    mock_agent = MagicMock()
    mock_agent.config.enable_subagents = True
    mock_agent.config.max_subagents = 10
    mock_agent.config.subagent_workflow_mode = "off"
    mock_agent.tools.specs.return_value = []
    mock_agent.subagents.workspace_root = Path("/tmp/project")
    mock_agent.subagents.workspace_roots = [Path("/tmp/project")]
    mock_agent.subagents.workspace = Path("/tmp/project/data/subagents")
    task = MagicMock()
    task.id = "run_0"
    task.goal = ""
    task.status = "PLANNING"
    task.verification_status = "UNVERIFIED"
    task.task_dir = "/tmp/project/data/subagents/run_0"
    task.output_json = "/tmp/project/data/subagents/run_0/output.json"
    mock_agent.subagents.create_run.return_value = task
    return mock_agent


def test_create_subagents_payload_hides_legacy_subagent_paths(monkeypatch):
    """create_subagents 返回给模型的 payload 不能暴露旧 data/subagents 路径。"""
    from agent_py_agent.agent.agent_core import orchestration_background_dispatch
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
                            "final_report": "/tmp/project/data/subagents/tasks/run_0/agents/run_0/final_report.md",
                        },
                    }
                ],
            },
        }

    monkeypatch.setattr(orchestration_background_dispatch, "_start_background_dispatch", fake_background_start)

    result = CreateSubagentsTool(mock_agent).execute({"goal": "整理资料", "count": 1})

    assert result.ok is True
    assert "/data/subagents/" not in result.output
    assert "[internal_legacy_subagent_path_hidden]" in result.output


def test_sanitizer_keeps_output_filename_without_legacy_path():
    """output_files 里如果还有旧路径，只保留可执行的目标文件名，不暴露旧目录。"""
    from agent_py_agent.agent.model_visible_ref_sanitizer import sanitize_model_visible_refs

    payload = {
        "output_files": ["/tmp/project/data/subagents/subagent-old/data_collection.md"],
        "task_dir": "/tmp/project/data/subagents/subagent-old",
    }

    sanitized = sanitize_model_visible_refs(payload)

    assert sanitized["output_files"] == ["data_collection.md"]
    assert sanitized["task_dir"] == "[internal_legacy_subagent_path_hidden]"
