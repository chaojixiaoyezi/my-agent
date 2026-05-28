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
            "小傻妞-市场-1",
            "小傻妞-竞争-2",
            "小傻妞-策略-3",
        ]
        assert payload["created"] == 3
        assert payload["auto_start"]["status"] == "started"
        assert payload["auto_start"]["run_ids"] == ["run_1", "run_2", "run_3"]
        assert payload["next_action"]["tool"] == "subagent_board"

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

    def test_items_cap_uses_agent_config_default_when_config_field_missing(self):
        """轻量配置对象缺少 max_subagents 时，也使用 AgentConfig 默认值。"""
        from types import SimpleNamespace

        from agent_py_agent.agent.agent_core.orchestration_create_items import CreateSubagentItem
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
        from agent_py_agent.agent.settings import AgentConfig

        mock_agent = MagicMock()
        mock_agent.config = SimpleNamespace(enable_subagents=True, subagent_workflow_mode="off")
        tool = CreateSubagentsTool(mock_agent)
        items = [CreateSubagentItem(goal=f"任务 {index}", params={}) for index in range(60)]

        assert len(tool._cap_items(items)) == AgentConfig().max_subagents

    def test_items_validate_before_creating_any_run(self, monkeypatch):
        """某个 item 失败时不应留下半创建的子代理记录。"""
        from agent_py_agent.agent.agent_core import orchestration_tools
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        def fake_target_error(request):
            return "second item invalid" if request.params.get("extra_write_roots") == ["/bad"] else ""

        monkeypatch.setattr(orchestration_tools, "external_write_target_error", fake_target_error)
        mock_agent = _agent()
        result = CreateSubagentsTool(mock_agent).execute({
            "items": [{"goal": "市场"}, {"goal": "竞争", "extra_write_roots": ["/bad"]}],
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

    def test_items_preserve_refs_first_context_manifest(self):
        """items[] 应把资料路径作为 refs 传给子代理，而不是要求 root 先读正文。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _agent()
        mock_agent.subagents.create_run.side_effect = _create_run_sequence()

        result = CreateSubagentsTool(mock_agent).execute({
            "items": [
                {
                    "goal": "分析市场资料",
                    "required_read_paths": ["data/market.md", "rubric.md"],
                    "context_manifest": {"required_read_paths": ["README.md"]},
                    "context_packs": [{"kind": "brief", "summary": "评分标准", "path": "rubric.md"}],
                }
            ],
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert params.context_manifest["required_read_paths"] == [
            "README.md",
            "data/market.md",
            "rubric.md",
        ]
        assert params.context_packs == [{"kind": "brief", "summary": "评分标准", "path": "rubric.md"}]

    def test_single_goal_preserves_source_refs_and_context_pack_refs(self):
        """单任务模式也应保留 source_refs/context_pack_refs，方便小傻妞按路径读取资料。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _agent()
        mock_task = MagicMock()
        mock_task.id = "run_1"
        mock_task.goal = "研究资料"
        mock_task.status = "PLANNING"
        mock_task.verification_status = "UNVERIFIED"
        mock_task.task_dir = "/tmp/run_1"
        mock_agent.subagents.create_run.return_value = mock_task

        result = CreateSubagentsTool(mock_agent).execute({
            "goal": "研究资料并写摘要",
            "source_refs": ["docs/a.md"],
            "context_pack_refs": ["packs/brief.json"],
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert params.context_manifest["required_read_paths"] == ["docs/a.md"]
        assert params.context_manifest["task_pack_refs"] == ["packs/brief.json"]
        assert params.context_packs == [{"kind": "context_ref", "path": "packs/brief.json"}]

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

    def test_create_subagents_keeps_grandchild_like_names_as_display_text(self):
        """名字里的小小傻妞/grandchild 只是展示文本，不应变成 create 阶段硬拒。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _agent()
        mock_agent.subagents.create_run.side_effect = _create_run_sequence()
        result = CreateSubagentsTool(mock_agent).execute({
            "items": [{
                "goal": "分析印尼市场",
                "role": "grandchild_worker",
                "agent_name": "小小傻妞-印尼市场",
            }],
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert params.agent_name == "小小傻妞-印尼市场"


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
        assert "apply_patch" in params.allowed_tools
        assert "apply_patch" in params.allowed_tools
