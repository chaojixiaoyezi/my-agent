"""orchestration_tools.py 单元测试。

测试编排工具注册、权限控制、执行边界等功能。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# LLM: test_create_subagents_tool_spec_uses_template_index_not_full_prompt protects startup token budget.
# 函数用途: create_subagents 工具说明只暴露角色模板索引，不把完整角色系统提示词放进主代理常驻工具说明。
def test_create_subagents_tool_spec_uses_template_index_not_full_prompt():
    from agent_py_agent.agent.agent_core.orchestration_tool_specs import build_create_subagents_spec

    spec = build_create_subagents_spec()
    role_detail = spec.parameter_details["role"]

    assert "模板位置" in role_detail
    assert "worker" in role_detail
    assert "你是执行子代理" not in role_detail
    assert "不同工作切片不要用 count" in spec.parameter_details["count"]


class TestTaskProgressTool:
    """测试通用任务进度账本。"""

    def test_updates_and_reads_current_agent_progress(self, tmp_path):
        """模型可以用一个工具记录长期任务小块进度，后续读取不会丢。"""
        from agent_py_agent.agent.agent_core.task_progress_tool import TaskProgressTool
        from agent_py_agent.agent.config import AgentConfig
        from agent_py_agent.agent.core import SimpleAgent

        agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
        agent._main_agent_run_id = "run-main"
        tool = TaskProgressTool(agent)

        update = tool.execute(
            {
                "action": "update",
                "summary": "已读完项目 A，准备读项目 B。",
                "next_action": "继续阅读项目 B 的核心模块。",
                "items": [
                    {"id": "project-a", "title": "阅读项目 A", "status": "done", "evidence": ["A/README.md"]},
                    {"id": "project-b", "title": "阅读项目 B", "status": "in_progress"},
                ],
            }
        )
        read = tool.execute({"action": "read"})
        payload = json.loads(read.output)

        assert update.ok is True
        assert read.ok is True
        assert payload["run_id"] == "run-main"
        assert payload["summary"] == "已读完项目 A，准备读项目 B。"
        assert payload["next_action"] == "继续阅读项目 B 的核心模块。"
        assert payload["counts"]["done"] == 1
        assert payload["counts"]["in_progress"] == 1
        assert payload["items"][0]["evidence"] == ["A/README.md"]


class TestTaskProgressRegistryTool:
    """测试 task_progress 经过真实工具注册表时的行为。"""

    def test_registry_gate_allows_task_progress_updates(self, tmp_path):
        """task_progress 的工具声明必须完整，否则真实工具入口会在执行前拦掉。"""
        from agent_py_agent.agent.config import AgentConfig
        from agent_py_agent.agent.core import SimpleAgent

        agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
        agent._main_agent_run_id = "run-main"

        result = agent.tools.execute_call(
            {
                "tool": "task_progress",
                "action": "update",
                "summary": "已完成第一块。",
                "items": [{"id": "block-1", "title": "第一块", "status": "done"}],
            }
        )

        assert result.ok is True
        assert (tmp_path / "memory_archive" / "task_progress" / "run-main" / "progress.json").exists()

    def test_registry_accepts_tool_name_wrapped_payload(self, tmp_path):
        """模型常把参数包放进同名字段，注册表应统一拆包后再执行。"""
        from agent_py_agent.agent.config import AgentConfig
        from agent_py_agent.agent.core import SimpleAgent

        agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
        agent._main_agent_run_id = "run-main"

        result = agent.tools.execute_call(
            {
                "tool": "task_progress",
                "task_progress": {
                    "action": "update",
                    "summary": "已读目录。",
                    "items": [{"id": "list", "status": "done"}],
                },
            }
        )

        assert result.ok is True
        payload = json.loads(result.output)
        assert payload["summary"] == "已读目录。"

    def test_registry_scope_drives_task_progress_run_id(self, tmp_path):
        """真实工具循环注入的 run_scope 应决定进度账本归属，不能落到 main。"""
        from agent_py_agent.agent.action_protocol import RunScope, ToolCallEnvelope
        from agent_py_agent.agent.config import AgentConfig
        from agent_py_agent.agent.core import SimpleAgent

        agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
        envelope = ToolCallEnvelope(
            call_id="call-1",
            source="test",
            tool="task_progress",
            args={
                "action": "update",
                "summary": "读完第一批项目。",
                "items": [{"id": "batch-1", "title": "第一批", "status": "done"}],
            },
            scope=RunScope(run_id="run-scoped", task_id="task-scoped", request_id="request-scoped"),
        )

        result = agent.tools.execute_call(envelope.to_dict())

        assert result.ok is True
        assert (tmp_path / "memory_archive" / "task_progress" / "run-scoped" / "progress.json").exists()
        assert not (tmp_path / "memory_archive" / "task_progress" / "main" / "progress.json").exists()

class TestInspectAgentTreeTool:
    """测试只读代理树查看工具。"""

    def test_returns_main_and_descendant_tree_without_dispatching(self, tmp_path):
        """查看状态不能触发 dispatch，也不能清掉 pending_work。"""
        import json

        from agent_py_agent.agent.agent_core.orchestration_tools import InspectAgentTreeTool
        from agent_py_agent.agent.subagents.manager import SubAgentManager

        manager = SubAgentManager(tmp_path)
        root = manager.create_run(goal="root", thought="", plan=["split"], role="coordinator")
        child = manager.create_run(
            goal="child",
            thought="",
            plan=["write"],
            parent_id=root.id,
            root_id=root.id,
            depth=1,
            role="worker",
        )
        child.current_tool = "write_file"
        child.last_progress_summary = "写出阶段报告"
        child.artifact_refs = ["artifact:report.md"]
        child.blockers = ["等待收口"]
        manager.save(child)
        root.child_ids = [child.id]
        manager.save(root)

        mock_agent = MagicMock()
        mock_agent.subagents = manager
        mock_agent._has_pending_work = True
        mock_agent.dispatch_subagents = MagicMock()

        result = InspectAgentTreeTool(mock_agent).execute({"root_id": root.id})
        payload = json.loads(result.output)

        assert result.ok is True
        assert payload["effect"] == "read_only"
        assert payload["main"]["agent_kind"] == "main_agent"
        assert payload["nodes"][1]["task_id"] == child.id
        assert payload["nodes"][1]["parent_run_id"] == root.id
        assert payload["nodes"][1]["current_tool"] == "write_file"
        assert payload["nodes"][1]["last_progress_summary"] == "写出阶段报告"
        assert payload["nodes"][1]["artifact_refs"] == ["artifact:report.md"]
        assert payload["nodes"][1]["blockers"] == ["等待收口"]
        assert mock_agent.dispatch_subagents.call_count == 0
        assert mock_agent._has_pending_work is True

    def test_tree_includes_child_progress_ledger_summary(self, tmp_path):
        """父代理查看 tree 时，应能看到子代理自己的进度摘要。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import InspectAgentTreeTool
        from agent_py_agent.agent.subagents.manager import SubAgentManager
        from agent_py_agent.agent.task_progress import write_task_progress

        manager = SubAgentManager(tmp_path)
        child = manager.create_run(goal="child", thought="", plan=["compare"], role="worker")
        write_task_progress(
            tmp_path,
            child.id,
            {
                "summary": "已检查 3 个来源，剩 2 个。",
                "next_action": "继续检查剩余来源。",
                "items": [
                    {"id": "source-1", "title": "来源 1", "status": "done"},
                    {"id": "source-2", "title": "来源 2", "status": "in_progress"},
                ],
            },
        )

        mock_agent = MagicMock()
        mock_agent.subagents = manager
        mock_agent.root = tmp_path
        mock_agent._main_agent_run_id = "main"

        payload = json.loads(InspectAgentTreeTool(mock_agent).execute({"root_id": child.id}).output)
        progress = payload["nodes"][0]["progress_layer"]["task_progress"]

        assert progress["summary"] == "已检查 3 个来源，剩 2 个。"
        assert progress["counts"]["done"] == 1
        assert progress["counts"]["in_progress"] == 1
        assert progress["next_action"] == "继续检查剩余来源。"

    def test_tree_includes_child_coverage_summary(self, tmp_path):
        """父代理查看 tree 时，应能看到子代理覆盖了哪些对象。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import InspectAgentTreeTool
        from agent_py_agent.agent.subagents.manager import SubAgentManager
        from agent_py_agent.agent.task_progress import write_task_progress

        manager = SubAgentManager(tmp_path)
        child = manager.create_run(goal="child", thought="", plan=["compare"], role="worker")
        write_task_progress(
            tmp_path,
            child.id,
            {
                "summary": "正在对比多个来源。",
                "coverage": {
                    "dimensions": ["查询", "写证据"],
                    "targets": [
                        {"id": "source-a", "checks": {"查询": "done", "写证据": "done"}},
                        {"id": "source-b", "checks": {"查询": "done", "写证据": "pending"}},
                    ],
                },
            },
        )

        mock_agent = MagicMock()
        mock_agent.subagents = manager
        mock_agent.root = tmp_path
        mock_agent._main_agent_run_id = "main"

        payload = json.loads(InspectAgentTreeTool(mock_agent).execute({"root_id": child.id}).output)
        coverage = payload["nodes"][0]["progress_layer"]["task_progress"]["coverage"]

        assert coverage["counts"]["targets_total"] == 2
        assert coverage["active_targets"][0]["id"] == "source-b"
        assert coverage["active_targets"][0]["checks"]["写证据"] == "pending"


class TestRaiseEventTool:
    """测试子孙代理事件冒泡。"""

    # LLM: Descendant event tools should infer tree lineage from task_id, not require the model to copy ids.
    # 函数用途: 孙代理只传自己的 task_id 时，事件账本仍能记录 source/parent/root，供主代理快速定位子树。
    def test_infers_descendant_lineage_from_task_id(self, tmp_path):
        from agent_py_agent.agent.agent_core.orchestration_tools import RaiseEventTool
        from agent_py_agent.agent.config import AgentConfig
        from agent_py_agent.agent.core import SimpleAgent

        agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
        thread = agent.conversation_store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1"})
        root = agent.subagents.create_run(goal="root", thought="", plan=["root"])
        child = agent.subagents.create_run(
            goal="child",
            thought="",
            plan=["child"],
            parent_id=root.id,
            root_id=root.id,
            depth=1,
        )
        grandchild = agent.subagents.create_run(
            goal="grandchild",
            thought="",
            plan=["grandchild"],
            parent_id=child.id,
            root_id=root.id,
            depth=2,
            attributes={"conversation_thread_id": thread.thread_id},
        )

        result = RaiseEventTool(agent).execute(
            {
                "task_id": grandchild.id,
                "event_type": "coordination_needed",
                "summary": "需要上级协调其它代理补充证据。",
            }
        )
        observation = agent.conversation_store.recent_observations(thread.thread_id)[-1]

        assert result.ok is True
        assert observation.source_agent_id == grandchild.id
        assert observation.parent_agent_id == child.id
        assert observation.root_task_id == root.id


class TestDispatchSubagentsTool:
    """测试 dispatch_subagents 的模型参数入口。"""

    def test_top_level_run_ids_runs_and_executes(self, tmp_path):
        """顶层显式目标应按 run_ids 推进真实 runner。"""
        from agent_py_agent.agent.agent_core.orchestration_dispatch_tool import (
            DispatchSubagentsTool,
        )
        from agent_py_agent.agent.config import AgentConfig
        from agent_py_agent.agent.core import SimpleAgent

        agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
        task = agent.subagents.create_run(goal="child", thought="", plan=["do"], role="worker")
        report = SimpleNamespace(dry_run=False, summary={"ok": True}, records=[])
        agent.dispatch_subagents = MagicMock(return_value=report)

        result = DispatchSubagentsTool(agent).execute({"run_ids": [task.id]})
        params = agent.dispatch_subagents.call_args.kwargs["params"]

        assert result.ok is True
        assert params.include_run_ids == [task.id]
        assert params.apply is True
        assert params.execute_runners is True
        assert params.max_runners == 1

    def test_model_facing_summary_renames_record_dry_run_count(self, tmp_path):
        """工具调用不是 dry-run 时，不把单条记录计数渲染成顶层 dry_run 语义。"""
        from agent_py_agent.agent.agent_core.orchestration_dispatch_tool import (
            DispatchSubagentsTool,
        )
        from agent_py_agent.agent.config import AgentConfig
        from agent_py_agent.agent.core import SimpleAgent

        agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
        report = SimpleNamespace(
            dry_run=False,
            summary={"total": 1, "ok": 1, "dry_run": 1, "applied": 0},
            records=[],
        )
        agent.dispatch_subagents = MagicMock(return_value=report)

        result = DispatchSubagentsTool(agent).execute({"run_ids": ["missing-run"]})
        payload = json.loads(result.output)

        assert payload["dry_run"] is False
        assert "dry_run" not in payload["summary"]
        assert payload["summary"]["record_dry_run_count"] == 1

    def test_model_facing_records_do_not_reuse_top_level_dry_run_name(self, tmp_path):
        """records 里的逐条预览状态不能继续叫 dry_run，避免模型误读整轮没执行。"""
        from agent_py_agent.agent.agent_core.orchestration_dispatch_tool import (
            DispatchSubagentsTool,
        )
        from agent_py_agent.agent.config import AgentConfig
        from agent_py_agent.agent.core import SimpleAgent

        agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
        record = SimpleNamespace(
            step="runner",
            action="execute",
            run_id="child-1",
            ok=True,
            dry_run=True,
            applied=False,
            message="preview only",
            before_status="PENDING",
            after_status="PENDING",
            evidence_paths=[],
        )
        agent.dispatch_subagents = MagicMock(return_value=SimpleNamespace(
            dry_run=False,
            summary={"total": 1},
            records=[record],
        ))

        payload = json.loads(DispatchSubagentsTool(agent).execute({"dry_run": False}).output)

        assert payload["dry_run"] is False
        assert "dry_run" not in payload["records"][0]
        assert payload["records"][0]["record_dry_run"] is True


class TestScheduleChildSubagentsTool:
    """测试当前 runner 创建下一层子节点的安全边界。"""

    def test_rejects_without_current_runner_context(self):
        """没有当前 runner id 时，不能绕过主节点直接挂 child。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import ScheduleChildSubagentsTool

        mock_agent = MagicMock()
        mock_agent._current_subagent_run_id = ""
        tool = ScheduleChildSubagentsTool(mock_agent)

        result = tool.execute({"children": [{"goal": "leaf"}], "dry_run": False})

        assert not result.ok
        assert "顶层派工请使用 create_subagents" in result.output

    def test_runner_context_max_depth_can_mean_one_more_layer(self, tmp_path):
        """模型在 depth=1 传 max_depth=1 时，按“再开一层”兼容处理。"""
        import json

        from agent_py_agent.agent.agent_core.orchestration_tools import ScheduleChildSubagentsTool
        from agent_py_agent.agent.config import AgentConfig
        from agent_py_agent.agent.core import SimpleAgent

        agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
        root = agent.subagents.create_run(goal="root", thought="root", plan=["root"])
        child = agent.subagents.create_run(
            goal="child", thought="child", plan=["child"], parent_id=root.id, root_id=root.id, depth=1,
        )
        agent._current_subagent_run_id = child.id
        tool = ScheduleChildSubagentsTool(agent)

        result = tool.execute(
            {
                "dry_run": False,
                "max_depth": 1,
                "children": [{"goal": "leaf", "role": "leaf", "agent_name": "leaf"}],
            }
        )
        payload = json.loads(result.output)
        leaf = agent.subagents.load(payload["created_run_ids"][0])

        assert result.ok
        assert leaf.parent_id == child.id
        assert leaf.depth == 2

    def test_runner_context_schedule_defaults_to_apply_direct_child(self, tmp_path):
        """runner 内部省略 dry_run 时，应真实创建当前节点的直接 child。"""
        import json

        from agent_py_agent.agent.agent_core.orchestration_tools import ScheduleChildSubagentsTool
        from agent_py_agent.agent.config import AgentConfig
        from agent_py_agent.agent.core import SimpleAgent

        agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
        root = agent.subagents.create_run(goal="root", thought="root", plan=["root"])
        agent._current_subagent_run_id = root.id
        tool = ScheduleChildSubagentsTool(agent)

        result = tool.execute({"children": [{"goal": "leaf", "role": "leaf_worker", "agent_name": "leaf"}]})
        payload = json.loads(result.output)

        assert result.ok
        assert payload["dry_run"] is False
        assert len(payload["created_run_ids"]) == 1
        assert agent.subagents.load(root.id).child_ids == payload["created_run_ids"]

    def test_runner_context_schedule_accepts_flat_params(self, tmp_path):
        """schedule_child_subagents 只接受顶层 children/dry_run 参数。"""
        import json

        from agent_py_agent.agent.agent_core.orchestration_tools import ScheduleChildSubagentsTool
        from agent_py_agent.agent.config import AgentConfig
        from agent_py_agent.agent.core import SimpleAgent

        agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
        root = agent.subagents.create_run(goal="root", thought="root", plan=["root"])
        agent._current_subagent_run_id = root.id
        tool = ScheduleChildSubagentsTool(agent)

        result = tool.execute({
            "tool": "schedule_child_subagents",
            "dry_run": False,
            "children": [{"goal": "grandchild coordinator", "role": "coordinator", "agent_name": "页面组"}],
        })
        payload = json.loads(result.output)
        child = agent.subagents.load(payload["created_run_ids"][0])

        assert result.ok
        assert payload["dry_run"] is False
        assert child.parent_id == root.id
        assert child.agent_name == "小傻妞-页面组"
