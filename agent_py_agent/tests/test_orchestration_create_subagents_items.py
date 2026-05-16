"""Focused tests for create_subagents items/tasks batch mode."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock


# LLM: _agent keeps items-mode tests small and below code-size guard thresholds.
# 函数用途: 创建只包含 create_subagents 所需字段的 mock agent，避免每个测试重复样板。
def _agent(max_subagents: int = 10) -> MagicMock:
    mock_agent = MagicMock()
    mock_agent.config.enable_subagents = True
    mock_agent.config.max_subagents = max_subagents
    mock_agent.config.subagent_workflow_mode = "off"
    mock_agent.subagents.workspace = Path("/tmp/subs")
    return mock_agent


# LLM: _create_run_sequence mirrors SubAgentManager ids while keeping assertions deterministic.
# 函数用途: 返回按调用次数递增的 create_run side effect，便于检查 next_action.run_ids。
def _create_run_sequence():
    created_count = 0

    def create_run(*, params):
        nonlocal created_count
        created_count += 1
        task = MagicMock()
        task.id = f"run_{created_count}"
        task.goal = params.goal
        task.status = "PLANNING"
        task.verification_status = "UNVERIFIED"
        task.task_dir = f"/tmp/{task.id}"
        return task

    return create_run


class TestCreateSubagentsItemsMode:
    """测试 create_subagents 的 items/tasks 结构化批量入口。"""

    def test_items_create_distinct_goals_without_top_level_goal(self):
        """items[] 批量模式应创建不同目标，不能复制同一个 goal。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _agent()
        mock_agent.subagents.create_run.side_effect = _create_run_sequence()

        result = CreateSubagentsTool(mock_agent).execute({
            "items": [
                {"goal": "研究越南市场环境", "role": "worker", "agent_name": "小傻妞-市场"},
                {"goal": "研究竞争格局", "role": "worker", "agent_name": "小傻妞-竞争"},
                {"goal": "制定进入策略", "role": "coordinator", "agent_name": "小傻妞-策略"},
            ],
            "acceptance_checks": ["必须有证据", "必须标注未确认信息"],
        })

        created_params = [
            call.kwargs["params"] for call in mock_agent.subagents.create_run.call_args_list
        ]
        payload = json.loads(result.output)

        assert result.ok is True
        assert [params.goal for params in created_params] == [
            "研究越南市场环境",
            "研究竞争格局",
            "制定进入策略",
        ]
        assert [params.agent_name for params in created_params] == [
            "小傻妞-市场",
            "小傻妞-竞争",
            "小傻妞-策略",
        ]
        assert payload["created"] == 3
        assert payload["next_action"]["tool"] == "dispatch_subagents"
        assert payload["next_action"]["params"]["run_ids"] == ["run_1", "run_2", "run_3"]
        assert payload["next_action"]["params"]["execute_runners"] is True

    def test_items_are_capped_by_max_subagents(self):
        """items[] 也应遵守 max_subagents，避免模型一次性撒太多任务。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _agent(max_subagents=2)
        mock_agent.subagents.create_run.side_effect = _create_run_sequence()

        result = CreateSubagentsTool(mock_agent).execute({
            "items": [
                {"goal": "市场"},
                {"goal": "竞争"},
                {"goal": "策略"},
            ],
        })

        assert result.ok is True
        assert mock_agent.subagents.create_run.call_count == 2

    def test_items_validate_before_creating_any_run(self, monkeypatch):
        """某个 item 失败时不应留下半创建的子代理记录。"""
        from agent_py_agent.agent.agent_core import orchestration_tools
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        def fake_target_error(agent, goal, allowed_tools):
            return "second item invalid" if "竞争" in goal else ""

        monkeypatch.setattr(orchestration_tools, "external_write_target_error", fake_target_error)
        mock_agent = _agent()
        result = CreateSubagentsTool(mock_agent).execute({
            "items": [{"goal": "市场"}, {"goal": "竞争"}],
        })

        assert result.ok is False
        assert "second item invalid" in result.output
        assert mock_agent.subagents.create_run.call_count == 0

    def test_items_do_not_inherit_global_plan_but_keep_nested_tasks(self):
        """items[] 不应把顶层全局计划误当成每个 child 自己的计划。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _agent()
        mock_agent.subagents.create_run.side_effect = _create_run_sequence()

        result = CreateSubagentsTool(mock_agent).execute({
            "plan": "主代理最后输出 final_report.md",
            "items": [
                {
                    "goal": "研究市场环境",
                    "tasks": [{"goal": "分析越南市场", "agent_name": "小小傻妞-越南"}],
                }
            ],
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert not any("final_report.md" in item for item in params.plan)
        assert any("分析越南市场" in item for item in params.plan)

    def test_items_and_tasks_cannot_be_mixed(self):
        """create_subagents 不能同时传 items 和 tasks，避免模型把两套批量协议混成一坨。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _agent()
        result = CreateSubagentsTool(mock_agent).execute({
            "items": [{"goal": "研究市场", "role": "worker"}],
            "tasks": [{"goal": "整合市场", "role": "coordinator"}],
            "count": 3,
        })

        assert result.ok is False
        assert "不要同时传 items 和 tasks" in result.output
        mock_agent.subagents.create_run.assert_not_called()

    def test_create_subagents_rejects_direct_grandchild_items(self):
        """root 入口只能创建直接小傻妞；小小傻妞应由上级用 schedule_child_subagents 创建。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _agent()
        result = CreateSubagentsTool(mock_agent).execute({
            "items": [{
                "goal": "分析印尼市场",
                "role": "grandchild_worker",
                "agent_name": "小小傻妞-印尼市场",
            }],
        })

        assert result.ok is False
        assert "create_subagents 只能创建直接小傻妞" in result.output
        assert "schedule_child_subagents" in result.output
        mock_agent.subagents.create_run.assert_not_called()


class TestCreateSubagentsToolGrantProtocol:
    """测试 create_subagents 对模型少填工具和协议漂移的兜底。"""

    def test_partial_explicit_allowed_tools_keep_baseline_write_tools(self):
        """模型只填 read_file/list_files 时，系统仍补齐基础读写工具，避免子代理变哑巴。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _agent()
        mock_task = MagicMock()
        mock_task.id = "worker_001"
        mock_task.goal = ""
        mock_task.status = "PLANNING"
        mock_task.verification_status = "UNVERIFIED"
        mock_task.task_dir = "/tmp/worker_001"
        mock_agent.subagents.create_run.return_value = mock_task

        result = CreateSubagentsTool(mock_agent).execute({
            "goal": "研究资料并写 output.json",
            "role": "worker",
            "allowed_tools": ["read_file", "list_files"],
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert "read_file" in params.allowed_tools
        assert "write_file" in params.allowed_tools
        assert "append_file" in params.allowed_tools
        assert "replace_in_file" in params.allowed_tools
