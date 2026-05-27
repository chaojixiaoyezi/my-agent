from __future__ import annotations

"""LLM: verifies explicit root/coordinator seed creation for hierarchy E2E.

给人看的解释：
测试 `spawn-subagents --role coordinator` 不再创建普通 worker，
而是创建能继续派发下一层的 root/coordinator。
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent_py_agent.agent.agent_core.spawn_role_seed import (
    SpawnExplicitRoleRequest,
    spawn_explicit_role_runs,
)
from agent_py_agent.agent.agent_core.subagent_params import SpawnSubagentsParams
from agent_py_agent.agent.subagents.manager import SubAgentManager
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
    assert "quality_contract" in output


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


# LLM: test_explicit_root_spawn_keeps_product_path_and_write_root covers parent authority coverage.
# 函数用途: 显式 root/coordinator seed 既保留产物目录上下文，也保留覆盖下级的写入权限用于检查/接管/救援。
def test_explicit_root_spawn_keeps_product_path_and_write_root(tmp_path: Path):
    deliverables = tmp_path / "deliverables" / "product"
    agent = MagicMock()
    agent.subagents = SubAgentManager(tmp_path / "subagents", workspace_root=tmp_path)
    request = SpawnExplicitRoleRequest(
        agent=agent,
        options=SpawnSubagentsParams(
            goal=f"Coordinate workers for {deliverables}.",
            count=1,
            role="coordinator",
            agent_name="root-coordinator",
            extra_write_roots=[str(deliverables)],
        ),
        count=1,
        allowed_tools=None,
        workflow_mode="off",
    )

    root = spawn_explicit_role_runs(request)[0]

    assert str(deliverables) in root.goal
    assert root.allowed_write_roots == [root.task_dir, str(deliverables)]
