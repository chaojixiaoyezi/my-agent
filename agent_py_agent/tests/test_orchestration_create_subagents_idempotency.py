"""LLM: create_subagents must be idempotent for same-parent named children.

函数/模块用途: 覆盖真实 E2E 中 root 重复创建同一批小傻妞的问题；工具层应复用已有 run 并返回调度合同。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock


# LLM: _workspace_agent uses the real manager so dedupe sees persisted sibling runs.
# 函数用途: 构造真实 SubAgentManager 驱动的 create_subagents 工具测试环境。
def _workspace_agent(tmp_path: Path):
    from agent_py_agent.agent.subagents.manager import SubAgentManager

    agent = MagicMock()
    agent.config.enable_subagents = True
    agent.config.max_subagents = 10
    agent.config.subagent_workflow_mode = "off"
    agent.subagents = SubAgentManager(tmp_path / ".my-agent" / "subagents", workspace_root=tmp_path)
    return agent


# LLM: Repeated natural-language batch creation should reuse named planning children instead of growing the tree.
# 函数用途: 同一父级下同名小傻妞已存在时，第二次 create_subagents 返回 reused_run_ids 和 dispatch_run_ids，不再创建新 run。
def test_items_mode_reuses_existing_named_children_and_returns_dispatch_contract(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _workspace_agent(tmp_path)
    tool = CreateSubagentsTool(agent)
    first = json.loads(tool.execute({"items": _pipeline_items("weekly_star_data.md")}).output)
    second = json.loads(tool.execute({"items": _pipeline_items("weekly_data.md")}).output)

    assert first["created_run_ids"] == first["ids"]
    assert second["created_run_ids"] == []
    assert second["reused_run_ids"] == first["ids"]
    assert second["dispatch_run_ids"] == first["ids"]
    assert second["next_action"]["params"]["run_ids"] == first["ids"]
    assert len(agent.subagents.list_runs()) == 3


# LLM: Done reused children should remain visible but not be dispatched again.
# 函数用途: 如果复用到的 run 已 DONE/VERIFIED，调度合同不能要求父级再次 dispatch 它。
def test_reused_done_children_are_excluded_from_dispatch_contract(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _workspace_agent(tmp_path)
    tool = CreateSubagentsTool(agent)
    first = json.loads(tool.execute({"items": _pipeline_items("weekly_star_data.md")}).output)
    done = agent.subagents.load(first["ids"][0])
    done.status = "DONE"
    done.verification_status = "VERIFIED"
    agent.subagents.save(done)
    second = json.loads(tool.execute({"items": _pipeline_items("weekly_data.md")}).output)

    assert second["reused_run_ids"] == first["ids"]
    assert second["dispatch_run_ids"] == first["ids"][1:]
    assert second["next_action"]["params"]["run_ids"] == first["ids"][1:]


# LLM: count-based fanout is an explicit request for multiple sibling runs, not an idempotent retry.
# 函数用途: 保证 count=2 创建两个相似子代理，不被同名复用压成一个 run。
def test_count_fanout_creates_requested_number_of_sibling_runs(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _workspace_agent(tmp_path)
    payload = json.loads(CreateSubagentsTool(agent).execute({
        "goal": "隔离场景测试：实现 fixture 功能并产出证据",
        "count": 2,
        "role": "worker",
        "agent_name": "小傻妞-隔离测试",
    }).output)

    assert payload["created_run_ids"] == payload["ids"]
    assert len(payload["ids"]) == 2
    assert len(agent.subagents.list_runs()) == 2


# LLM: _pipeline_items mirrors the real Task18 short duplicate batch with stable child names.
# 函数用途: 返回三段流水线 item，output_name 用来模拟第二次创建时目标文本略有变化。
def _pipeline_items(output_name: str) -> list[dict[str, object]]:
    return [
        {
            "agent_name": "小傻妞-数据收集",
            "goal": f"收集 GitHub star 数据，写到 data/subagents/subagent_data_collection/{output_name}",
            "role": "worker",
        },
        {
            "agent_name": "小傻妞-内容编写",
            "goal": "基于数据收集结果写中文解释，输出 project_explanations.md",
            "role": "worker",
        },
        {
            "agent_name": "小傻妞-生成报告",
            "goal": "整合前两步生成 xlsx 和 final_report.md",
            "role": "worker",
        },
    ]
