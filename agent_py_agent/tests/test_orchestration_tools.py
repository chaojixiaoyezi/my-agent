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


def _register_authority_chain(agent: object, run_id: str, *, task_id: str = "") -> str:
    """seq 253 闭合：MANAGED authority 门要求 run 登记真实权威链（生产同一
    登记入口 record_run_creation），返回 current attempt_id。"""

    repo = getattr(getattr(agent, "subagents", None), "runtime_db", None)
    if repo is None:
        return "test-attempt"
    record = repo.record_run_creation(
        owner_id="owner-a",
        goal=f"orchestration gate {run_id}",
        conversation_task_id=task_id or f"task-{run_id}",
        thread_id=f"thread-{run_id}",
        run_id=run_id,
        role="assistant",
    )
    return str(record["attempt_id"])


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
    assert "职责短标题" in spec.parameter_descriptions["description"]
    item_schema = spec.input_schema["properties"]["items"]["items"]
    assert item_schema["required"] == ["goal"]
    assert item_schema["properties"]["description"]["maxLength"] == 240
    assert item_schema["properties"]["output_files"]["type"] == "array"
    assert "交付身份与冲突范围" in spec.parameter_descriptions["output_files"]
    assert "角色就变为协调者" in spec.description
    assert "不要重做已经委派的任务" in spec.description
    assert "replacement child" in spec.description
    assert "goal 里写‘先 A 后 B’不会形成执行顺序" in spec.description
    assert "等 A 的生命周期完成事件自动唤醒后" in spec.description


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

    params = create_run_params(
        agent,
        {"role": "worker", "description": "调研项目 A"},
        "阅读项目 A 并写报告",
        ["read_file"],
    )
    task = agent.subagents.create_run(params=params)

    assert task.description == "调研项目 A"
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


def test_model_surface_has_no_manual_subagent_dispatch_tools(tmp_path):
    """创建会自动开跑；模型只保留创建、消息和打断入口。"""
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")),
        tmp_path,
    )

    names = set(agent.tools.tools)
    assert {
        "create_subagents",
        "send_guidance",
        "cancel_subagents",
        "resolve_capability_requests",
    } <= names
    assert "inspect_agent_tree" not in names
    assert "dispatch_subagents" not in names
    assert "schedule_child_subagents" not in names


def test_descendant_create_uses_same_public_tool_and_hierarchy_service():
    """孙代理继续调用 create_subagents，不需要知道内部 schedule 名称。"""
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
    from agent_py_agent.agent.tooling.models import ToolHandlerOutcome

    agent = SimpleNamespace(
        _current_subagent_run_id="parent-child",
        config=SimpleNamespace(enable_subagents=True),
    )
    with patch(
        "agent_py_agent.agent.agent_core.orchestration_tools.execute_child_creation",
        return_value=ToolHandlerOutcome("create_subagents", True, "ok"),
    ) as nested:
        result = CreateSubagentsTool(agent).execute(
            {
                "goal": "继续拆分",
                "items": [{"goal": "写模块 A"}, {"goal": "写模块 B"}],
            }
        )

    assert result.tool == "create_subagents"
    assert result.ok is True
    nested.assert_called_once()
    call_params = nested.call_args.args[1]
    assert [item["goal"] for item in call_params["children"]] == ["写模块 A", "写模块 B"]
    assert nested.call_args.kwargs["tool_name"] == "create_subagents"


def test_descendant_create_enforces_same_per_call_limit(tmp_path):
    """递归创建不能用旧的不限量路径绕过每次最多 child 数。"""
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            subagent_hierarchy_max_children_per_tool_call=2,
        ),
        tmp_path,
    )
    parent = agent.subagents.create_run(goal="父任务", thought="拆分", plan=["执行"])
    agent._current_subagent_run_id = parent.id

    result = CreateSubagentsTool(agent).execute(
        {
            "goal": "并行处理三个目标",
            "items": [
                {"goal": "目标一"},
                {"goal": "目标二"},
                {"goal": "目标三"},
            ],
        }
    )

    assert result.ok is False
    assert "单次最多创建 2 个" in result.output
    assert agent.subagents.load(parent.id).child_ids == []


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
        attempt_id = _register_authority_chain(agent, "run-main")

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
            attempt_id=attempt_id,
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
            attempt_id=_register_authority_chain(agent, "run-scoped", task_id="task-scoped"),
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
            attempt_id=_register_authority_chain(agent, "run-main"),
        )
        payload = json.loads(result.output)

        assert result.ok is True
        assert payload["soft_feedback"]["blocking"] is False
        assert payload["soft_feedback"]["missing_evidence_item_ids"] == ["project-a"]
        assert "补证据" in payload["soft_feedback"]["message"]
