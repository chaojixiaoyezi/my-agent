from __future__ import annotations

"""LLM: verifies explicit root/coordinator seed creation for hierarchy E2E.

给人看的解释：
测试 `spawn-subagents --role coordinator` 不再创建普通 worker，
而是创建能继续派发下一层的 root/coordinator。
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent_py_agent.agent.subagents.models import SubAgentTask


# LLM: test_cmd_spawn_serializes_dataclass_task keeps legacy spawn JSON output stable.
# 函数用途: 验证 spawn-subagents 输出可序列化的任务 JSON，并保留 quality contract 字段。
def test_cmd_spawn_serializes_dataclass_task(tmp_path: Path, capsys):
    from agent_py_agent.cli.subagents import cmd_spawn

    args = MagicMock()
    args.config = str(tmp_path / "config.yaml")
    args.goal = "测试任务"
    args.count = 1
    args.role = "worker"
    args.agent_name = ""

    mock_agent = MagicMock()
    mock_agent.spawn_subagents.return_value = [
        SubAgentTask(id="run_001", goal="测试任务", thought="思考", plan=["执行"])
    ]

    with patch("agent_py_agent.cli._board.make_agent", return_value=mock_agent):
        result = cmd_spawn(args)

    output = json.loads(capsys.readouterr().out)
    assert result == 0
    assert output["id"] == "run_001"
    assert output["quality_contract"]["final_judge"] == "parent_final_gate"


# LLM: test_cmd_spawn_passes_explicit_role_bundle verifies CLI args reach the spawn bundle.
# 函数用途: 确认命令行 role/agent-name 不丢失，后续 SimpleAgent 能创建真正 coordinator seed。
def test_cmd_spawn_passes_explicit_role_bundle(tmp_path: Path):
    from agent_py_agent.cli.subagents import cmd_spawn

    args = MagicMock()
    args.config = str(tmp_path / "config.yaml")
    args.goal = "主节点任务"
    args.count = 1
    args.role = "coordinator"
    args.agent_name = "root-coordinator"

    mock_agent = MagicMock()
    mock_agent.spawn_subagents.return_value = [
        SubAgentTask(id="root_001", goal="主节点任务", thought="协调", plan=["创建子代理"])
    ]

    with patch("agent_py_agent.cli._board.make_agent", return_value=mock_agent):
        result = cmd_spawn(args)

    params = mock_agent.spawn_subagents.call_args.kwargs["params"]
    assert result == 0
    assert params.role == "coordinator"
    assert params.agent_name == "root-coordinator"
