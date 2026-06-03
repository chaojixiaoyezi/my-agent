"""Regression tests for model-visible subagent refs."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock


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
                            "final_report": "/tmp/project/data/subagents/tasks/run_0/agents/run_0/final_report.md",
                        },
                    }
                ],
            },
        }

    monkeypatch.setattr(background_dispatch, "_start_background_dispatch", fake_background_start)

    result = CreateSubagentsTool(mock_agent).execute({"goal": "整理资料", "count": 1})

    assert result.ok is True
    assert "/data/subagents/" not in result.output
    assert "[internal_legacy_subagent_path_hidden]" not in result.output


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


def test_sanitizer_removes_legacy_path_embedded_in_model_visible_text():
    """旧 data/subagents 路径夹在自然语言里时，也不能继续作为模型可见事实。"""
    from agent_py_agent.agent.model_visible_ref_sanitizer import sanitize_model_visible_refs
    from agent_py_agent.agent.model_visible_refs import current_model_text

    text = "请读取 /tmp/project/data/subagents/subagent-old/data_collection.md 后继续。"

    assert current_model_text(text) == "请读取 data_collection.md 后继续。"
    sanitized = sanitize_model_visible_refs({"goal": text, "summary": text})
    assert "/data/subagents/" not in sanitized["goal"]
    assert sanitized["goal"] == "请读取 data_collection.md 后继续。"
