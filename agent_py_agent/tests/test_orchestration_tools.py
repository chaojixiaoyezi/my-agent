"""orchestration_tools.py 单元测试。

测试编排工具注册、权限控制、执行边界等功能。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.capability import CapabilityRouter
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call


def test_create_subagents_model_spec_uses_template_index_not_full_prompt():
    from agent_py_agent.agent.agent_core.orchestration.tool_specs import (
        build_create_subagents_model_spec,
    )

    spec = build_create_subagents_model_spec()
    role_detail = spec.input_schema["properties"]["role"]["description"]

    assert "模板位置" not in role_detail
    assert "worker" in role_detail
    assert "你是执行子代理" not in role_detail
    assert "count" not in spec.parameter_descriptions
    assert "count" not in spec.input_schema["properties"]
    assert spec.input_schema["required"] == ["goal"]
    assert all('"goal"' in example for example in spec.examples)


def test_inspect_agent_tree_model_schema_excludes_owner_wide_history():
    from agent_py_agent.agent.agent_core.orchestration.tool_specs import (
        build_inspect_agent_tree_model_spec,
    )

    model_spec = build_inspect_agent_tree_model_spec()
    schema = model_spec.input_schema

    assert schema["properties"]["scope"]["enum"] == [
        "root_tree",
        "own_subtree",
        "subtree",
    ]
    assert "all" not in model_spec.parameter_descriptions["scope"]


def test_create_subagents_inherits_current_task_workspace(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration.create_policy import create_run_params
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo", my_agent_home=str(tmp_path / "home"), subagent_workspace="subs"
        ),
        tmp_path,
    )
    task_root = (
        tmp_path / "home" / "owners" / "local" / "main" / "tasks" / "2026-06-01" / "big-task"
    )
    agent._current_run_task_workspace = str(task_root)

    params = create_run_params(agent, {"role": "worker"}, "阅读项目 A 并写报告", ["read_file"])
    task = agent.subagents.create_run(params=params)

    assert task.attributes["run_workspace"]["task_root"] == str(task_root)
    assert task.task_workspace_dir == str(task_root)
    assert task.agent_run_workspace_dir == str(task_root / "work" / "agents" / task.id)


def test_subagent_tools_are_not_registered_when_subagents_disabled(tmp_path):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleAgent(AgentConfig(model_backend="echo", enable_subagents=False), tmp_path)

    hidden = {
        "create_subagents",
        "dispatch_subagents",
        "inspect_agent_tree",
        "schedule_child_subagents",
        "cancel_subagents",
        "wait",
        "send_guidance",
    }
    assert hidden.isdisjoint(agent.tools.tools)
    assert hidden.isdisjoint({spec.name for spec in agent.tools.specs(include_orchestration=True)})


class TestTaskProgressTool:
    """测试通用任务进度账本。"""

    def test_updates_and_reads_current_agent_progress(self, tmp_path):
        """模型可以用一个工具记录长期任务小块进度，后续读取不会丢。"""
        from agent_py_agent.agent.agent_core.task_progress_tool import TaskProgressTool
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        agent = SimpleAgent(
            AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path
        )
        agent._main_agent_run_id = "run-main"
        tool = TaskProgressTool(agent)

        update = tool.execute(
            {
                "action": "update",
                "summary": "已读完项目 A，准备读项目 B。",
                "next_action": "继续阅读项目 B 的核心模块。",
                "items": [
                    {
                        "id": "project-a",
                        "title": "阅读项目 A",
                        "status": "done",
                        "evidence": ["A/README.md"],
                    },
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


def test_task_identity_keeps_main_resume_and_child_ledgers_separate():
    from agent_py_agent.agent.agent_core.runtime.task_identity import (
        durable_task_id,
        progress_ledger_id,
    )

    agent = SimpleNamespace(
        _main_agent_run_id="main-fallback", _current_request_id="request-fallback"
    )
    resumed = SimpleNamespace(
        run_id="request-after-resume",
        task_id="request-after-resume",
        context_scope="conversation",
        source="gateway",
        task_attributes={"conversation_task_id": "task-original"},
    )
    child = SimpleNamespace(
        run_id="child-run",
        task_id="child-task",
        context_scope="task_local",
        source="subagent_run",
        task_attributes={"conversation_task_id": "task-original"},
    )

    assert durable_task_id(resumed) == "task-original"
    assert progress_ledger_id(agent, resumed) == "task-original"
    assert durable_task_id(child) == "child-task"
    assert progress_ledger_id(agent, child, scoped_id="child-scoped") == "child-scoped"


class TestTaskProgressRegistryTool:
    """测试 task_progress 经过真实工具注册表时的行为。"""

    def test_registry_gate_allows_task_progress_updates(self, tmp_path):
        """task_progress 的工具声明必须完整，否则真实工具入口会在执行前拦掉。"""
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        agent = SimpleAgent(
            AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path
        )
        agent._main_agent_run_id = "run-main"

        result = execute_registry_test_call(
            agent.tools,
            "task_progress",
            {
                "action": "update",
                "summary": "已完成第一块。",
                "items": [{"id": "block-1", "title": "第一块", "status": "done"}],
            },
            run_id="run-main",
            call_id="task-progress-valid",
        )

        assert result.ok is True
        owner_home = Path(agent.home_paths.owner_home_dir)
        assert (
            owner_home / "memory_archive" / "task_progress" / "run-main" / "progress.json"
        ).exists()

    def test_registry_rejects_tool_name_wrapped_payload(self, tmp_path):
        """同名 wrapper 不是当前工具协议，不能被静默忽略或拆包执行。"""
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        agent = SimpleAgent(
            AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path
        )
        agent._main_agent_run_id = "run-main"

        result = execute_registry_test_call(
            agent.tools,
            "task_progress",
            {
                "task_progress": {
                    "action": "update",
                    "summary": "已读目录。",
                    "items": [{"id": "list", "status": "done"}],
                },
            },
            run_id="run-main",
            call_id="task-progress-wrapper-invalid",
        )

        assert result.ok is False
        assert result.error_code == "TOOL_INVALID_ARGUMENTS"
        assert result.failure_stage == "validation"
        assert result.handler_executed is False
        assert result.metadata["action_decision"]["evidence"]["issues"] == [
            {
                "path": "$.task_progress",
                "keyword": "additionalProperties",
                "expected": False,
                "actual_type": "object",
            }
        ]

    def test_registry_scope_drives_task_progress_run_id(self, tmp_path):
        """真实工具循环注入的 run_scope 应决定进度账本归属，不能落到 main。"""
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        agent = SimpleAgent(
            AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path
        )
        result = execute_registry_test_call(
            agent.tools,
            "task_progress",
            {
                "action": "update",
                "summary": "读完第一批项目。",
                "items": [{"id": "batch-1", "title": "第一批", "status": "done"}],
            },
            run_id="run-scoped",
            call_id="call-1",
            write_boundary={"task_id": "task-scoped"},
        )

        assert result.ok is True
        owner_home = Path(agent.home_paths.owner_home_dir)
        assert (
            owner_home / "memory_archive" / "task_progress" / "run-scoped" / "progress.json"
        ).exists()
        assert not (
            owner_home / "memory_archive" / "task_progress" / "main" / "progress.json"
        ).exists()

    def test_task_progress_soft_feedback_names_missing_evidence_items(self, tmp_path):
        """模型一写完成/结果但没证据时，工具应立即给可操作软提醒，不等最终验收。"""
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        agent = SimpleAgent(
            AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path
        )
        agent._main_agent_run_id = "run-main"

        result = execute_registry_test_call(
            agent.tools,
            "task_progress",
            {
                "action": "update",
                "items": [
                    {"id": "project-a", "title": "项目 A", "status": "done", "notes": "完成"}
                ],
            },
            run_id="run-main",
            call_id="task-progress-soft-feedback",
        )
        payload = json.loads(result.output)

        assert result.ok is True
        assert payload["soft_feedback"]["blocking"] is False
        assert payload["soft_feedback"]["missing_evidence_item_ids"] == ["project-a"]
        assert "补证据" in payload["soft_feedback"]["message"]


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

        mock_agent.capability_router = CapabilityRouter()
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

        mock_agent.capability_router = CapabilityRouter()
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

        mock_agent.capability_router = CapabilityRouter()
        mock_agent.subagents = manager
        mock_agent.root = tmp_path
        mock_agent._main_agent_run_id = "main"

        payload = json.loads(InspectAgentTreeTool(mock_agent).execute({"root_id": child.id}).output)
        coverage = payload["nodes"][0]["progress_layer"]["task_progress"]["coverage"]

        assert coverage["counts"]["targets_total"] == 2
        assert coverage["active_targets"][0]["id"] == "source-b"
        assert coverage["active_targets"][0]["checks"]["写证据"] == "pending"

    def test_repeated_tree_inspection_returns_cooldown_snapshot(self, tmp_path):
        """短时间重复看同一棵树时，应返回缓存提示，避免主代理高频轮询。"""
        import json

        from agent_py_agent.agent.agent_core.orchestration_tools import InspectAgentTreeTool
        from agent_py_agent.agent.subagents.manager import SubAgentManager

        manager = SubAgentManager(tmp_path)
        child = manager.create_run(goal="child", thought="", plan=["compare"], role="worker")
        mock_agent = MagicMock()
        mock_agent.capability_router = CapabilityRouter()
        mock_agent.subagents = manager

        tool = InspectAgentTreeTool(mock_agent)
        first = json.loads(tool.execute({"root_id": child.id}).output)
        second = json.loads(tool.execute({"root_id": child.id}).output)

        assert "cooldown_active" not in first
        assert second["cooldown_active"] is True
        assert second["status"] == "POLL_COOLDOWN"
        assert "inspect_agent_tree_recent_duplicate" in second["warnings"]
        assert "不要高频轮询" in second["policy"]["next_step"]
        assert second["policy"]["suggested_tool_call"]["tool"] == "wait"
        assert second["policy"]["suggested_tool_call"]["seconds"] == 120
        assert second["direct_children"]["suggested_tool_call"]["tool"] == "wait"
        assert "tasks" not in second

    def test_repeated_tree_inspection_skips_full_kernel_render_when_state_unchanged(self, tmp_path):
        """cooldown 内 task 状态没变时，不再完整读取和渲染代理树。"""
        import json

        from agent_py_agent.agent.agent_core.orchestration_tools import InspectAgentTreeTool
        from agent_py_agent.agent.subagents.manager import SubAgentManager

        manager = SubAgentManager(tmp_path)
        child = manager.create_run(goal="child", thought="", plan=["compare"], role="worker")
        original_snapshot = manager.kernel_snapshot
        calls = []

        def counted_snapshot(query=None):
            calls.append(query)
            return original_snapshot(query)

        manager.kernel_snapshot = counted_snapshot
        mock_agent = MagicMock()
        mock_agent.capability_router = CapabilityRouter()
        mock_agent.subagents = manager

        tool = InspectAgentTreeTool(mock_agent)
        first = json.loads(tool.execute({"root_id": child.id}).output)
        second = json.loads(tool.execute({"root_id": child.id}).output)

        assert "cooldown_active" not in first
        assert second["cooldown_active"] is True
        assert second["cooldown_source"] == "cached_tree_state_fingerprint"
        assert len(calls) == 1

    def test_repeated_tree_inspection_uses_configured_watch_interval(self, tmp_path):
        """代理树重复查看 cooldown 应跟随 subagent_watch_interval_seconds。"""
        import json
        from types import SimpleNamespace

        from agent_py_agent.agent.agent_core.orchestration_tools import InspectAgentTreeTool
        from agent_py_agent.agent.subagents.manager import SubAgentManager

        manager = SubAgentManager(tmp_path)
        child = manager.create_run(goal="child", thought="", plan=["compare"], role="worker")
        mock_agent = MagicMock()
        mock_agent.capability_router = CapabilityRouter()
        mock_agent.config = SimpleNamespace(subagent_watch_interval_seconds=240)
        mock_agent.subagents = manager

        tool = InspectAgentTreeTool(mock_agent)
        tool.execute({"root_id": child.id})
        second = json.loads(tool.execute({"root_id": child.id}).output)

        assert second["cooldown_seconds"] == 240
        assert second["policy"]["suggested_tool_call"]["seconds"] == 240
        assert second["direct_children"]["suggested_tool_call"]["seconds"] == 240

    def test_tree_inspection_cooldown_does_not_hide_status_changes(self, tmp_path):
        """cooldown 只能压缩没变化的树，不能把已完成子代理继续显示成运行中。"""
        import json

        from agent_py_agent.agent.agent_core.orchestration_tools import InspectAgentTreeTool
        from agent_py_agent.agent.subagents.manager import SubAgentManager

        manager = SubAgentManager(tmp_path)
        child = manager.create_run(goal="child", thought="", plan=["compare"], role="worker")
        child.status = "RUNNING"
        manager.save(child)
        mock_agent = MagicMock()
        mock_agent.capability_router = CapabilityRouter()
        mock_agent.subagents = manager

        tool = InspectAgentTreeTool(mock_agent)
        first = json.loads(tool.execute({"root_id": child.id}).output)
        child.status = "DONE"
        child.progress = 1.0
        child.artifact_refs = ["/tmp/report.md"]
        manager.save(child)
        second = json.loads(tool.execute({"root_id": child.id}).output)

        assert "cooldown_active" not in first
        assert "cooldown_active" not in second
        assert second["nodes"][0]["status"] == "DONE"
        assert second["child_result_index"][0]["primary_artifact_refs"] == ["/tmp/report.md"]

    def test_tree_inspection_suggests_wait_for_pending_children(self, tmp_path):
        """普通查看代理树时，运行中的子代理应引导到 wait，而不是继续轮询。"""
        import json

        from agent_py_agent.agent.agent_core.orchestration_tools import InspectAgentTreeTool
        from agent_py_agent.agent.subagents.manager import SubAgentManager

        manager = SubAgentManager(tmp_path)
        child = manager.create_run(goal="child", thought="", plan=["compare"], role="worker")
        child.status = "RUNNING"
        manager.save(child)
        mock_agent = MagicMock()
        mock_agent.capability_router = CapabilityRouter()
        mock_agent.subagents = manager
        mock_agent._main_agent_run_id = "main"

        payload = json.loads(
            InspectAgentTreeTool(mock_agent).execute({"scope": "root_tree"}).output
        )

        assert payload["coordination_advice"]["suggested_tool_call"]["tool"] == "wait"
        assert payload["policy"]["suggested_tool_call"]["tool"] == "wait"
        assert "调用 wait" in payload["policy"]["next_step"]

    def test_main_agent_registers_wait_tool(self, tmp_path):
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)

        assert "wait" in agent.tools.tools
        result = json.loads(
            agent.tools.tools["wait"]
            .execute({"seconds": 5, "task_id": "root-task-1", "reason": "等子代理完成"})
            .output
        )
        assert result["ok"] is True
        assert result["scheduled"] is True
        assert result["interval_seconds"] == 60
        assert result["reason"] == "等子代理完成"
        policy = agent.conversation_store.get_progress_policy(result["policy_id"])
        assert policy is not None
        assert policy.task_id == "root-task-1"
        assert policy.metadata["kind"] == "subagent_progress_watch"

    def test_wait_tool_uses_configured_default_and_caps_user_seconds(self, tmp_path):
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        agent = SimpleAgent(
            AgentConfig(
                model_backend="echo", subagent_workspace="subs", subagent_watch_interval_seconds=240
            ),
            tmp_path,
        )

        defaulted = json.loads(agent.tools.tools["wait"].execute({"task_id": "task-a"}).output)
        capped = json.loads(
            agent.tools.tools["wait"].execute({"task_id": "task-b", "seconds": 99999}).output
        )

        assert defaulted["interval_seconds"] == 240
        assert capped["interval_seconds"] == 7200

    def test_wait_tool_ignores_interval_seconds_alias(self, tmp_path):
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        agent = SimpleAgent(
            AgentConfig(
                model_backend="echo", subagent_workspace="subs", subagent_watch_interval_seconds=180
            ),
            tmp_path,
        )

        payload = json.loads(
            agent.tools.tools["wait"]
            .execute({"task_id": "task-alias", "interval_seconds": 600})
            .output
        )

        assert payload["ok"] is True
        assert payload["interval_seconds"] == 180

    def test_wait_tool_never_blocks_registers_nonblocking_reminder(self, tmp_path):
        """wait 改为事件驱动:任何模式(含曾经会阻塞的 cli_run)都不再原地 sleep,只登记非阻塞进度
        提醒并让模型结束本回合等事件唤醒——父代理派完子代理后不被卡死、能继续响应。"""
        from types import SimpleNamespace

        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
        agent._current_run_params = SimpleNamespace(source="cli_run", task_id="task-cli-wait")

        payload = json.loads(
            agent.tools.tools["wait"]
            .execute(
                {
                    "seconds": 60,
                    "reason": "等子代理",
                    # Unknown/legacy extras cannot turn internal wait into an
                    # IM delivery route.  User reminders belong to schedule.
                    "route_channel": "feishu",
                    "route_target": "ou_other_user",
                }
            )
            .output
        )

        assert payload["mode"] == "nonblocking_schedule"
        assert payload["next_action"] == "end_turn_and_yield"
        assert payload["scheduled"] is True
        assert payload["delivery_scope"] == "internal_agent_only"
        assert payload["user_notification_created"] is False
        assert "用户提醒" in payload["guidance"]
        assert "slept_seconds" not in payload

        policy = agent.conversation_store.get_progress_policy(payload["policy_id"])
        assert policy is not None
        assert policy.route_channel == "internal"
        assert policy.route_target == ""

        spec = agent.tools.tools["wait"].model_spec
        assert "用户的未来提醒使用 schedule" in spec.description
        assert "提醒" not in spec.keywords

    def test_tree_inspection_cooldown_can_be_disabled(self, tmp_path):
        import json

        from agent_py_agent.agent.agent_core.orchestration_tools import InspectAgentTreeTool
        from agent_py_agent.agent.subagents.manager import SubAgentManager

        manager = SubAgentManager(tmp_path)
        child = manager.create_run(goal="child", thought="", plan=["compare"], role="worker")
        mock_agent = MagicMock()
        mock_agent.capability_router = CapabilityRouter()
        mock_agent.subagents = manager

        tool = InspectAgentTreeTool(mock_agent)
        tool.execute({"root_id": child.id, "cooldown_seconds": 0})
        second = json.loads(tool.execute({"root_id": child.id, "cooldown_seconds": 0}).output)

        assert "cooldown_active" not in second


class TestRaiseEventTool:
    """测试子孙代理事件冒泡。"""

    def test_infers_descendant_lineage_from_task_id(self, tmp_path):
        from agent_py_agent.agent.agent_core.orchestration_tools import RaiseEventTool
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        agent = SimpleAgent(
            AgentConfig(
                model_backend="echo",
                my_agent_home=str(tmp_path / "home"),
                subagent_workspace="subs",
            ),
            tmp_path,
        )
        thread = agent.conversation_store.get_or_create_thread(
            {
                "canonical_user_id": "user-1",
                "channel": "internal",
                "channel_conversation_id": "thread-1",
                "channel_user_id": "user-1",
            }
        )
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
        from agent_py_agent.agent.agent_core.orchestration.dispatch.tool import (
            DispatchSubagentsTool,
        )
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        agent = SimpleAgent(
            AgentConfig(
                model_backend="echo",
                my_agent_home=str(tmp_path / "home"),
                subagent_workspace="subs",
            ),
            tmp_path,
        )
        task = agent.subagents.create_run(goal="child", thought="", plan=["do"], role="worker")
        report = SimpleNamespace(dry_run=False, summary={"ok": True}, records=[])
        agent.dispatch_subagents = MagicMock(return_value=report)

        result = DispatchSubagentsTool(agent).execute({"run_ids": [task.id]})
        params = agent.dispatch_subagents.call_args.kwargs["params"]

        assert result.ok is True
        assert params.include_run_ids == [task.id]
        assert params.apply is True
        assert params.start_runners is True
        assert params.max_runners == 1
        assert params.execution_plan.preview_only is False
        assert params.execution_plan.mutate_state is True
        assert params.execution_plan.start_runners is True
        assert params.execution_plan.max_runners == 1

    def test_model_facing_summary_hides_internal_dry_run_record_count(self, tmp_path):
        """模型只看顶层 dry_run；内部记录预检计数不再暴露。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch.tool import (
            DispatchSubagentsTool,
        )
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        agent = SimpleAgent(
            AgentConfig(
                model_backend="echo",
                my_agent_home=str(tmp_path / "home"),
                subagent_workspace="subs",
            ),
            tmp_path,
        )
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
        assert "record_dry_run_count" not in payload["summary"]

    def test_model_facing_records_hide_internal_preview_flag(self, tmp_path):
        """逐条内部预检位不进入模型结果，避免覆盖顶层执行事实。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch.tool import (
            DispatchSubagentsTool,
        )
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        agent = SimpleAgent(
            AgentConfig(
                model_backend="echo",
                my_agent_home=str(tmp_path / "home"),
                subagent_workspace="subs",
            ),
            tmp_path,
        )
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
        agent.dispatch_subagents = MagicMock(
            return_value=SimpleNamespace(
                dry_run=False,
                summary={"total": 1},
                records=[record],
            )
        )

        payload = json.loads(DispatchSubagentsTool(agent).execute({"dry_run": False}).output)

        assert payload["dry_run"] is False
        assert "dry_run" not in payload["records"][0]
        assert "record_dry_run" not in payload["records"][0]


class TestScheduleChildSubagentsTool:
    """测试当前 runner 创建下一层子节点的安全边界。"""

    def test_rejects_without_current_runner_context(self):
        """没有当前 runner id 时，不能绕过主节点直接挂 child。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import ScheduleChildSubagentsTool

        mock_agent = MagicMock()

        mock_agent.capability_router = CapabilityRouter()
        mock_agent._current_subagent_run_id = ""
        tool = ScheduleChildSubagentsTool(mock_agent)

        result = tool.execute({"children": [{"goal": "leaf"}], "dry_run": False})

        assert not result.ok
        assert "顶层派工请使用 create_subagents" in result.output

    def test_runner_context_max_depth_uses_absolute_depth_limit(self, tmp_path):
        """显式 max_depth 是绝对深度限制，不按“再开一层”兼容处理。"""
        import json

        from agent_py_agent.agent.agent_core.orchestration_tools import ScheduleChildSubagentsTool
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        agent = SimpleAgent(
            AgentConfig(
                model_backend="echo",
                my_agent_home=str(tmp_path / "home"),
                subagent_workspace="subs",
            ),
            tmp_path,
        )
        root = agent.subagents.create_run(goal="root", thought="root", plan=["root"])
        child = agent.subagents.create_run(
            goal="child",
            thought="child",
            plan=["child"],
            parent_id=root.id,
            root_id=root.id,
            depth=1,
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

        assert result.ok
        assert payload["blocked"] is True
        assert payload["reason"] == "max_depth_exceeded:1"
        assert payload["created_run_ids"] == []

    def test_runner_context_schedule_defaults_to_apply_direct_child(self, tmp_path):
        """runner 内部省略 dry_run 时，应真实创建当前节点的直接 child。"""
        import json

        from agent_py_agent.agent.agent_core.orchestration_tools import ScheduleChildSubagentsTool
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        agent = SimpleAgent(
            AgentConfig(
                model_backend="echo",
                my_agent_home=str(tmp_path / "home"),
                subagent_workspace="subs",
            ),
            tmp_path,
        )
        root = agent.subagents.create_run(goal="root", thought="root", plan=["root"])
        agent._current_subagent_run_id = root.id
        tool = ScheduleChildSubagentsTool(agent)

        result = tool.execute(
            {"children": [{"goal": "leaf", "role": "worker", "agent_name": "leaf"}]}
        )
        payload = json.loads(result.output)

        assert result.ok
        assert payload["dry_run"] is False
        assert len(payload["created_run_ids"]) == 1
        assert agent.subagents.load(root.id).child_ids == payload["created_run_ids"]

    def test_runner_context_schedule_accepts_flat_params(self, tmp_path):
        """schedule_child_subagents 只接受顶层 children/dry_run 参数。"""
        import json

        from agent_py_agent.agent.agent_core.orchestration_tools import ScheduleChildSubagentsTool
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        agent = SimpleAgent(
            AgentConfig(
                model_backend="echo",
                my_agent_home=str(tmp_path / "home"),
                subagent_workspace="subs",
            ),
            tmp_path,
        )
        root = agent.subagents.create_run(goal="root", thought="root", plan=["root"])
        agent._current_subagent_run_id = root.id
        tool = ScheduleChildSubagentsTool(agent)

        result = tool.execute(
            {
                "tool": "schedule_child_subagents",
                "dry_run": False,
                "children": [
                    {
                        "goal": "grandchild coordinator",
                        "role": "coordinator",
                        "agent_name": "页面组",
                    }
                ],
            }
        )
        payload = json.loads(result.output)
        child = agent.subagents.load(payload["created_run_ids"][0])

        assert result.ok
        assert payload["dry_run"] is False
        assert child.parent_id == root.id
        assert child.agent_name == "页面组"
