"""Focused tests for create_subagents output-root binding."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock


# LLM: _mock_workspace_agent keeps output-ref tests small and independent from the large create tool suite.
# 函数用途: 构造拥有真实 SubAgentManager 的最小 agent，用于检查 payload 和持久化 task 是否一致。
def _mock_workspace_agent(tmp_path: Path):
    from agent_py_agent.agent.subagents.manager import SubAgentManager

    mock_agent = MagicMock()
    mock_agent.config.enable_subagents = True
    mock_agent.config.max_subagents = 10
    mock_agent.config.subagent_workflow_mode = "off"
    mock_agent.subagents = SubAgentManager(tmp_path / ".my-agent" / "subagents", workspace_root=tmp_path)
    return mock_agent


# LLM: This regression covers the real E2E bug where a stale guessed run id entered a new child goal.
# 函数用途: 确认 create_subagents 返回给模型的 payload 和 task.json 都使用真实 run_id 作为自写产物目录。
def test_items_mode_payload_rebinds_stale_self_output_run_id(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    stale_id = "subagent-0000000000-stale"
    mock_agent = _mock_workspace_agent(tmp_path)

    result = CreateSubagentsTool(mock_agent).execute({
        "items": [{
            "goal": f"收集项目数据，请把结果写到 data/subagents/{stale_id}/data_collection.md",
            "agent_name": "小傻妞-数据收集",
            "role": "worker",
        }],
    })
    payload = json.loads(result.output)
    run_id = payload["ids"][0]
    loaded = mock_agent.subagents.load(run_id)

    assert result.ok is True
    assert stale_id not in payload["tasks"][0]["goal"]
    assert f"data/subagents/{run_id}/data_collection.md" in payload["tasks"][0]["goal"]
    assert loaded.attributes["output_ref_rebindings"][0]["to"].endswith("/data_collection.md")


# LLM: This keeps the workspace-root default behavior out of the oversized create tool test file.
# 函数用途: 验证已有真实任务工作区时，模糊交付 worker 默认获得 workspace_root 写入根。
def test_vague_deliverable_worker_defaults_to_workspace_root():
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    mock_agent = MagicMock()
    mock_agent.config.enable_subagents = True
    mock_agent.config.max_subagents = 10
    mock_agent.config.subagent_workflow_mode = "off"
    mock_agent.subagents.workspace_root = Path("/tmp/project")
    mock_agent.subagents.workspace_roots = [Path("/tmp/project")]
    mock_agent.subagents.workspace = Path("/tmp/project/.my-agent/subagents")
    mock_task = MagicMock()
    mock_task.id = "writer_001"
    mock_task.goal = ""
    mock_task.status = "PLANNING"
    mock_task.verification_status = "UNVERIFIED"
    mock_task.task_dir = "/tmp/writer_001"
    mock_agent.subagents.create_run.return_value = mock_task

    result = CreateSubagentsTool(mock_agent).execute({
        "goal": "在目标目录生成一个完整文件 index.html，并报告路径。",
        "role": "writer",
    })
    params = mock_agent.subagents.create_run.call_args.kwargs["params"]

    assert result.ok is True
    assert params.extra_write_roots == [str(Path("/tmp/project").resolve(strict=False))]
