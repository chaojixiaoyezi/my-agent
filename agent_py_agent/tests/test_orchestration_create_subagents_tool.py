"""Split orchestration tool execution tests for code-size guard clarity."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


# LLM: 参数测试用显式内存任务表；创建与读取返回同一记录，不让 MagicMock 动态属性假装 canonical 状态。
# 函数用途: 构造不运行模型的批量创建替身，实际启动和并发控制由管理器集成测试覆盖。
def _mock_create_items_agent(task_count: int = 3):
    mock_agent = MagicMock()
    mock_agent.config.enable_subagents = True
    mock_agent.config.max_subagents = 10
    mock_agent.subagents.workspace_root = Path("/tmp/project")
    mock_agent.subagents.workspace_roots = [Path("/tmp/project")]
    mock_agent.subagents.workspace = Path("/tmp/project/.my-agent/subagents")
    tasks = [_mock_created_task(index) for index in range(task_count)]
    mock_agent._created_tasks = tasks
    mock_agent.subagents.create_run.side_effect = tasks
    def load(run_id):
        task = next((item for item in tasks if item.id == run_id), None)
        if task is None:
            raise FileNotFoundError(run_id)
        return task

    mock_agent.subagents.load.side_effect = load
    return mock_agent


def _mock_created_task(index: int):
    task = MagicMock()
    task.id = f"run_{index}"
    task.goal = ""
    task.status = "PLANNING"
    task.verification_status = "UNVERIFIED"
    task.task_dir = f"/tmp/run_{index}"
    return task


def test_parent_runtime_snapshot_is_host_bound_for_every_create_attribute_build():
    """The scoped host snapshot overrides input lookalikes without changing public params."""
    from agent_py_agent.agent.agent_core.orchestration.create_policy import (
        create_task_attributes,
    )
    from agent_py_agent.agent.subagents.capability_scope import (
        bind_creation_tool_authority,
    )

    snapshot = SimpleNamespace(
        run_id="root-turn-1",
        available_tool_names=frozenset({"read_file", "mcp__computer_use__list_windows"}),
        snapshot_hash="sha256:test-parent-snapshot",
        owner_type="main_agent",
    )
    expected = {
        "schema_version": "direct_parent_tool_authority.v1",
        "parent_run_id": "root-turn-1",
        "available_tool_names": ["mcp__computer_use__list_windows", "read_file"],
        "snapshot_hash": "sha256:test-parent-snapshot",
        "owner_type": "main_agent",
    }
    raw = {
        "attributes": {
            "direct_parent_tool_authority": {"available_tool_names": ["spoofed_tool"]},
            "marker": "a",
        }
    }
    with bind_creation_tool_authority(snapshot):
        first = create_task_attributes(raw)
        second = create_task_attributes({})

    assert first["direct_parent_tool_authority"] == expected
    assert first["marker"] == "a"
    assert second["direct_parent_tool_authority"] == expected
    assert "direct_parent_tool_authority" not in create_task_attributes(raw)


def test_create_subagents_scoped_handler_binds_and_resets_parent_authority(monkeypatch):
    """Tool Gateway scope is visible only while the create handler is executing."""
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
    from agent_py_agent.agent.subagents.capability_scope import (
        current_creation_tool_authority,
    )
    from agent_py_agent.agent.tooling.models import (
        ToolHandlerOutcome,
        ToolInvocationContext,
    )

    snapshot = SimpleNamespace(
        run_id="root-turn-scoped",
        available_tool_names=frozenset({"read_file"}),
        snapshot_hash="sha256:scoped",
        owner_type="main_agent",
    )
    tool = CreateSubagentsTool(SimpleNamespace())
    observed: dict[str, object] = {}

    def fake_execute(_params):
        observed.update(current_creation_tool_authority())
        return ToolHandlerOutcome("create_subagents", True, "ok")

    monkeypatch.setattr(tool, "execute", fake_execute)
    result = tool.execute_scoped({}, ToolInvocationContext(runtime_snapshot=snapshot))

    assert result.ok
    assert observed["parent_run_id"] == "root-turn-scoped"
    assert current_creation_tool_authority() == {}


def test_unrelated_open_runs_do_not_consume_current_root_session_slots():
    """其它 TUI 的可恢复旧任务不能永久堵住当前根任务的 会话运行时 式会话槽。"""
    from agent_py_agent.agent.agent_core.orchestration_tools import (
        _available_creation_slots,
    )

    unrelated = [
        SimpleNamespace(id=f"old-{index}", status="PENDING")
        for index in range(12)
    ]
    agent = SimpleNamespace(
        config=SimpleNamespace(
            max_subagents=6,
            subagent_hierarchy_max_children_per_tool_call=4,
            task_max_subagents=0,
        ),
        owner_policy=SimpleNamespace(max_subagents=50, max_active_agents=1000),
        _current_run_params=SimpleNamespace(
            task_attributes={"conversation_task_id": "current-root"}
        ),
        subagents=SimpleNamespace(list_runs=lambda: unrelated),
        subagent_run_ids_for_request=lambda _task_id: [],
    )

    slots, details = _available_creation_slots(agent)

    assert slots == 4
    assert details["session_active"] == 0
    assert details["owner_active"] == 12


def test_default_root_session_exposes_eight_slots_without_second_batch_cap():
    """默认只由八个会话槽位收口，不再先用隐藏的四个单次槽位拒绝整批。"""
    from agent_py_agent.agent.agent_core.orchestration_tools import (
        _available_creation_slots,
    )
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleNamespace(
        config=AgentConfig(),
        owner_policy=SimpleNamespace(max_subagents=50, max_active_agents=1000),
        _current_run_params=SimpleNamespace(
            task_attributes={"conversation_task_id": "current-root"}
        ),
        subagents=SimpleNamespace(list_runs=lambda: []),
        subagent_run_ids_for_request=lambda _task_id: [],
    )

    slots, details = _available_creation_slots(agent)

    assert slots == 8
    assert details["session_cap"] == 8
    assert details["per_call_cap"] == 0


def test_cancelled_batch_stops_before_materializing_and_publishing_remaining_children(
    tmp_path,
    monkeypatch,
):
    """在途批量派工收到统一工具取消令牌后，不再落盘或启动剩余 child。"""
    from agent_py_agent.agent.agent_core import orchestration_tools
    from agent_py_agent.agent.common.cancellation import (
        CancellationToken,
        ToolCancelled,
        bind_cancellation_token,
    )
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    token = CancellationToken()
    real_resolve = orchestration_tools.resolve_create_run
    resolved = 0

    def resolve_then_cancel(manager, params):
        nonlocal resolved
        result = real_resolve(manager, params)
        resolved += 1
        if resolved == 1:
            token.cancel("interrupted")
        return result

    publish = MagicMock()
    monkeypatch.setattr(orchestration_tools, "resolve_create_run", resolve_then_cancel)
    monkeypatch.setattr(orchestration_tools, "publish_created_subagents", publish)

    with bind_cancellation_token(token), pytest.raises(ToolCancelled):
        orchestration_tools.execute_create_subagents_service(
            agent,
            {
                "items": [
                    {"goal": "调研单体架构", "agent_name": "mono-researcher"},
                    {"goal": "调研微服务架构", "agent_name": "micro-researcher"},
                    {"goal": "调研 Actor 架构", "agent_name": "actor-researcher"},
                ]
            },
        )

    assert resolved == 1
    assert len(agent.subagents.list_runs()) == 1
    publish.assert_not_called()


class TestCreateSubagentsToolExecute:
    """测试 CreateSubagentsTool.execute() 方法。"""

    def test_user_cancel_is_reported_as_known_interrupted_create(
        self,
        tmp_path,
        monkeypatch,
    ):
        """派工安全点取消不能被 operation coordinator 降成未知副作用。"""
        from agent_py_agent.agent.agent_core import orchestration_tools
        from agent_py_agent.agent.common.cancellation import ToolCancelled
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)

        def cancelled_create(_agent, _params):
            raise ToolCancelled("interrupted")

        monkeypatch.setattr(
            orchestration_tools,
            "execute_create_subagents_service",
            cancelled_create,
        )

        result = orchestration_tools.CreateSubagentsTool(agent).execute(
            {"goal": "创建子代理"}
        )
        payload = json.loads(result.output)

        assert result.ok is False
        assert result.error_code == "CANCELLED"
        assert result.effect_outcome == "failed"
        assert payload["status"] == "cancelled"
        assert "结构化谱系收口" in payload["message"]

    def test_disabled_by_config(self):
        """配置禁用时返回错误。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = False

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({"goal": "测试任务"})

        assert result.ok is False
        assert "禁用" in result.output

    def test_missing_goal_returns_error(self):
        """缺少必填 goal 参数时返回错误。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({})

        assert result.ok is False
        assert "goal" in result.output.lower()

    def test_empty_goal_returns_error(self):
        """空 goal 返回错误。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({"goal": "   "})

        assert result.ok is False

    def test_count_clone_mode_is_rejected(self):
        """模型工具不得把同一个可写任务按 count 克隆。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 2

        mock_task = MagicMock()
        mock_task.id = "run_1"
        mock_task.goal = ""
        mock_task.status = "PENDING"
        mock_task.verification_status = "PENDING"
        mock_task.task_dir = "/tmp"
        mock_agent.subagents.create_run.return_value = mock_task

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({"goal": "测试", "count": 10})

        assert result.ok is False
        assert result.reported_error_code == "TOOL_INVALID_ARGUMENTS"
        assert "不接受 count" in result.output
        assert mock_agent.subagents.create_run.call_count == 0

    def test_single_goal_creates_one_child(self):
        """单 goal 只创建一个 child；多个 child 必须显式列出 items。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        mock_agent.config.subagent_spawn_default_count = 4
        mock_task = MagicMock()
        mock_task.id = "run_1"
        mock_task.goal = ""
        mock_task.status = "PENDING"
        mock_task.verification_status = "PENDING"
        mock_task.task_dir = "/tmp"
        mock_agent.subagents.create_run.return_value = mock_task

        result = CreateSubagentsTool(mock_agent).execute({"goal": "测试"})

        assert result.ok is True
        assert mock_agent.subagents.create_run.call_count == 1

    def test_audit_leaf_enters_exact_source_binding_scope_before_creation(self):
        from agent_py_agent.agent.agent_core.orchestration_tools import (
            CreateSubagentsTool,
        )
        from agent_py_agent.agent.common.audit_activation import (
            AUDIT_ATTR,
            AUDIT_SOURCE_BINDING_PENDING_ATTR,
            AUDIT_SOURCE_BINDING_TOOLS,
        )
        from agent_py_agent.agent.conversation.authority import (
            CONVERSATION_REQUEST_ID_ATTR,
        )

        audit_id = "audit-create-first-turn-scope"
        mock_agent = _mock_create_items_agent(task_count=1)
        mock_agent.config.lease_stale_without_heartbeat_seconds = 300
        mock_agent._current_run_params = SimpleNamespace(
            source="request",
            task_id=audit_id,
            request_id=audit_id,
            task_attributes={
                AUDIT_ATTR: True,
                CONVERSATION_REQUEST_ID_ATTR: audit_id,
                "conversation_task_id": audit_id,
            },
        )
        mock_agent.subagent_run_ids_for_request.return_value = []

        result = CreateSubagentsTool(mock_agent).execute(
            {
                "goal": "持续研判一个来源",
                "role": "worker",
                "allowed_tools": ["watch_stream", "run_command", "write_file"],
                "allowed_skills": ["coding"],
                "context_packs": [
                    {"kind": "parent_recent_read", "path": "private.md"}
                ],
                "extra_write_roots": ["/tmp/broad-write"],
                "output_refs": ["output/report.md"],
            }
        )

        assert result.ok is True
        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert params.allowed_tools == list(AUDIT_SOURCE_BINDING_TOOLS)
        assert params.allowed_skills == []
        assert params.context_packs == []
        assert params.extra_write_roots == []
        assert params.acceptance_checks == []
        assert params.attributes[AUDIT_SOURCE_BINDING_PENDING_ATTR] is True
        assert params.attributes[CONVERSATION_REQUEST_ID_ATTR] == audit_id
        assert "output_refs" not in params.attributes

    def test_per_call_and_current_task_capacity_are_enforced(self):
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 50
        mock_agent.config.subagent_hierarchy_max_children_per_tool_call = 2
        mock_agent.config.task_max_subagents = 2
        mock_agent._current_run_params = SimpleNamespace(
            task_attributes={"conversation_task_id": "task-root"}
        )
        active = MagicMock(id="run-active", status="RUNNING")
        mock_agent.subagents.list_runs.return_value = [active]
        mock_agent.subagent_run_ids_for_request.return_value = ["run-active"]

        result = CreateSubagentsTool(mock_agent).execute({
            "goal": "并行核对",
            "items": [{"goal": "核对接口"}, {"goal": "核对文档"}],
        })

        assert result.ok is False
        assert result.reported_error_code == "SUBAGENT_CAPACITY_EXCEEDED"
        assert result.effect_outcome == "not_started"
        payload = json.loads(result.output)
        assert payload["available"] == 1
        assert payload["limits"]["task_active"] == 1
        assert mock_agent.subagents.create_run.call_count == 0

    def test_capacity_state_failure_rejects_whole_batch(self):
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 50
        mock_agent.subagents.list_runs.side_effect = OSError("registry offline")

        result = CreateSubagentsTool(mock_agent).execute({"goal": "核对接口"})

        assert result.ok is False
        assert result.reported_error_code == "SUBAGENT_CAPACITY_UNAVAILABLE"
        assert result.effect_outcome == "not_started"
        assert mock_agent.subagents.create_run.call_count == 0

    def test_empty_items_with_top_level_goal_falls_through_to_single_goal(self):
        """空 items + 顶层 goal 落单 goal 模式，而不是报错(B1 真机回归)。

        真机 B1:模型把单个子代理规格放顶层(goal),却反射性带了 items:[]。旧逻辑
        直接 TOOL_INVALID_ARGUMENTS,导致 14 次派工全失败、一个子代理都没建出来,
        主代理只能退回独自写。修复后空 items 应被当作"没传 items",用顶层 goal 建一个。
        """
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        mock_task = MagicMock()
        mock_task.id = "run_1"
        mock_task.goal = ""
        mock_task.status = "PENDING"
        mock_task.verification_status = "PENDING"
        mock_task.task_dir = "/tmp"
        mock_agent.subagents.create_run.return_value = mock_task

        result = CreateSubagentsTool(mock_agent).execute({"items": [], "goal": "实现用户认证模块"})

        assert result.ok is True
        assert mock_agent.subagents.create_run.call_count == 1

    def test_items_mode_uses_agent_config_default_when_max_subagents_missing(self):
        """轻量配置对象缺少 max_subagents 时，items 模式使用 AgentConfig 默认值。"""
        from types import SimpleNamespace

        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
        from agent_py_agent.agent.settings import AgentConfig

        mock_agent = MagicMock()
        mock_agent.config = SimpleNamespace(enable_subagents=True)
        mock_task = MagicMock()
        mock_task.id = "run_1"
        mock_task.goal = ""
        mock_task.status = "PENDING"
        mock_task.verification_status = "PENDING"
        mock_task.task_dir = "/tmp"
        mock_agent.subagents.create_run.return_value = mock_task

        result = CreateSubagentsTool(mock_agent).execute({
            "goal": "测试",
            "items": [{"goal": f"任务 {index}"} for index in range(60)],
        })

        assert result.ok is False
        assert result.reported_error_code == "SUBAGENT_CAPACITY_EXCEEDED"
        assert mock_agent.subagents.create_run.call_count == 0
        assert str(AgentConfig().max_subagents) in result.output

    def test_create_subagents_auto_starts_created_runs_without_waiting_for_completion(self, monkeypatch):
        """create_subagents 默认创建并后台启动，父代理不等子代理全部结束。"""
        import agent_py_agent.agent.agent_core.orchestration.background.dispatch as background_dispatch
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _mock_create_items_agent(task_count=2)
        mock_agent.tools.specs.return_value = []
        launched: dict[str, object] = {}

        def fake_background_start(agent, run_ids, *, expected_attempt_ids=None):
            launched["agent"] = agent
            launched["run_ids"] = list(run_ids)
            return {
                "status": "started",
                "dispatch_mode": "background",
                "run_ids": list(run_ids),
                "agent_tree": {"schema_version": "agent_tree_status.v1"},
            }

        monkeypatch.setattr(background_dispatch, "_start_background_dispatch", fake_background_start)

        result = CreateSubagentsTool(mock_agent).execute({
            "goal": "分别整理两份资料",
            "items": [{"goal": "整理资料 A"}, {"goal": "整理资料 B"}],
        })
        payload = json.loads(result.output)

        assert result.ok is True
        mock_agent.dispatch_subagents.assert_not_called()
        assert launched["run_ids"] == ["run_0", "run_1"]
        assert payload["auto_start"]["status"] == "started"
        assert payload["auto_start"]["dispatch_mode"] == "background"
        assert "agent_tree" not in payload["auto_start"]
        assert payload["next_action"]["action"] == "continue_independent_work"

    def test_default_capacity_accepts_one_atomic_batch_of_eight(self, monkeypatch):
        """默认八槽必须允许一个 items 调用完整创建八名 child，不静默拆批或截断。"""
        import agent_py_agent.agent.agent_core.orchestration.background.dispatch as background_dispatch
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _mock_create_items_agent(task_count=8)
        mock_agent.config.max_subagents = 8
        mock_agent.config.subagent_hierarchy_max_children_per_tool_call = 0
        mock_agent.config.task_max_subagents = 0
        mock_agent.tools.specs.return_value = []
        monkeypatch.setattr(
            background_dispatch,
            "_start_background_dispatch",
            lambda _agent, run_ids: {
                "status": "started",
                "dispatch_mode": "background",
                "run_ids": list(run_ids),
            },
        )

        result = CreateSubagentsTool(mock_agent).execute({
            "goal": "分别调研八个项目",
            "items": [{"goal": f"调研项目 {index}"} for index in range(8)],
        })
        payload = json.loads(result.output)

        assert result.ok is True
        assert payload["created"] == 8
        assert payload["created_run_ids"] == [f"run_{index}" for index in range(8)]
        assert mock_agent.subagents.create_run.call_count == 8

    def test_auto_start_process_command_runs_direct_dispatch_for_created_run_ids(self):
        """真实后台进程只跑精确 run_id 的一轮 dispatch，不再抢父进程 watch lock。"""
        from agent_py_agent.agent.agent_core.orchestration.background.dispatch import (
            _background_dispatch_command,
            _BackgroundDispatchRequest,
        )

        mock_agent = _mock_create_items_agent(task_count=2)
        mock_agent.config.config_path = "/tmp/my-agent-config.yaml"
        mock_agent.root = Path("/tmp/actual-task-workspace")
        request = _BackgroundDispatchRequest(
            agent=mock_agent,
            run_ids=["run_a", "run_b"],
            launch_id="launch-1",
            router=object(),
            cfg=object(),
            params=SimpleNamespace(expected_attempt_ids={"run_a": "attempt-a", "run_b": "attempt-b"}),
        )

        command = _background_dispatch_command(mock_agent, request)

        assert command[1:4] == ["-u", "-m", "agent_py_agent"]
        assert command[command.index("--config") + 1] == "/tmp/my-agent-config.yaml"
        assert "subagents-dispatch" in command
        assert command[command.index("--workspace-root") + 1] == str(Path("/tmp/actual-task-workspace").resolve())
        assert "--watch" not in command
        assert "--advance" not in command
        assert "--interval" not in command
        assert "--max-cycles" not in command
        assert "-u" in command
        assert "--background-launch-id" in command
        assert command[command.index("--background-launch-id") + 1] == "launch-1"
        assert command.count("--run-id") == 2
        assert command[command.index("--run-id") + 1] == "run_a"
        assert command[command.index("--run-id", command.index("--run-id") + 1) + 1] == "run_b"

    def test_dispatch_cli_accepts_run_id_scope(self):
        """subagents-dispatch CLI 入口要把 --run-id 传成 include_run_ids。"""
        from argparse import Namespace

        from agent_py_agent.cli._dispatch import _dispatch_params, _subagents_dispatch_options

        args = Namespace(
            apply=True,
            start_runners=True,
            planner=False,
            max_runners=2,
            limit=20,
            reviewer="test",
            note="",
            instruction="",
            max_cards=0,
            no_probe=False,
            take_over_by="",
            locked_file=[],
            interval=None,
            max_cycles=0,
            advance=False,
            force_lock=False,
            watch=False,
            run_id=["run_a", "run_b,run_c"],
            background_launch_id="launch-1",
            expected_attempt=[["run_a", "attempt-a"], ["run_b", "attempt-b"], ["run_c", "attempt-c"]],
        )

        options = _subagents_dispatch_options(args)
        params = _dispatch_params(options)

        assert options.background_launch_id == "launch-1"
        assert params.include_run_ids == ["run_a", "run_b", "run_c"]


class TestCreateSubagentsToolStartControls:
    """测试 create_subagents 的启动和接管控制。"""

    def test_internal_defer_never_tells_model_to_dispatch(self):
        """底层依赖延迟只等待宿主恢复，模型结果里不能再出现手动推进工具。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _mock_create_items_agent(task_count=2)
        mock_agent.tools.specs.return_value = []

        result = CreateSubagentsTool(mock_agent).execute({
            "goal": "先登记两个后续任务",
            "items": [{"goal": "后续任务 A"}, {"goal": "后续任务 B"}],
            "defer_start": True,
        })
        payload = json.loads(result.output)

        assert result.ok is True
        mock_agent.dispatch_subagents.assert_not_called()
        assert payload["auto_start"]["status"] == "deferred"
        assert payload["next_action"]["action"] == "await_dependency_event"

    def test_item_defer_start_only_holds_that_child(self, monkeypatch):
        """单个 item.defer_start=true 只挂起该 child，不拖住同批生产 worker。"""
        from types import SimpleNamespace

        import agent_py_agent.agent.agent_core.orchestration.background.dispatch as background_dispatch

        captured: dict[str, object] = {}

        def fake_start(agent, run_ids, *, expected_attempt_ids=None):
            del agent
            captured["run_ids"] = list(run_ids)
            return {"status": "started", "run_ids": list(run_ids)}

        monkeypatch.setattr(background_dispatch, "_start_background_dispatch", fake_start)
        tasks = [
            SimpleNamespace(id="developer", status="PLANNING", verification_status="UNVERIFIED", attributes={}),
            SimpleNamespace(id="tester", status="PLANNING", verification_status="UNVERIFIED", attributes={"defer_start": True}),
        ]

        payload = background_dispatch.auto_start_tasks(SimpleNamespace(dispatch_subagents=lambda: None), tasks, {})

        assert captured["run_ids"] == ["developer"]
        assert payload["run_ids"] == ["developer"]
        assert payload["deferred_run_ids"] == ["tester"]

    def test_create_subagents_records_explicit_replacement(self, tmp_path):
        """新 child 显式替换旧 run 时，旧 run 进入 TAKEN_OVER，不再被当作活跃任务。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
        from agent_py_agent.agent.subagents.manager import SubAgentManager

        manager = SubAgentManager(tmp_path, workspace_root=tmp_path)
        source = manager.create_run(goal="旧开发代理", thought="卡住了", plan=["写页面"], role="worker")
        agent = SimpleNamespace(
            config=SimpleNamespace(enable_subagents=True, max_subagents=10, access_mode="workspace-write"),
            subagents=manager,
            tools=SimpleNamespace(specs=lambda: []),
        )

        result = CreateSubagentsTool(agent).execute({
            "goal": "接管旧开发代理继续完成页面",
            "role": "worker",
            "replacement_for_run_ids": [source.id],
            "defer_start": True,
        })
        payload = json.loads(result.output)
        replacement_id = payload["created_run_ids"][0]
        reloaded = manager.load(source.id)

        assert result.ok is True
        assert reloaded.status == "TAKEN_OVER"
        assert reloaded.takeover_by == replacement_id
        assert payload["replacement_records"] == [
            {"source_run_id": source.id, "replacement_run_id": replacement_id, "status": "recorded"}
        ]

    def test_replacement_preflight_rejects_other_parent_without_creating(self, tmp_path):
        """不能跨父级接管；预检失败前不产生 replacement 记录。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
        from agent_py_agent.agent.subagents.manager import SubAgentManager

        manager = SubAgentManager(tmp_path, workspace_root=tmp_path)
        source = manager.create_run(goal="别的任务", thought="执行", plan=["做"], role="worker")
        source.parent_id = "other-parent"
        manager.save(source)
        agent = SimpleNamespace(
            config=SimpleNamespace(enable_subagents=True, max_subagents=10, access_mode="workspace-write"),
            subagents=manager,
            tools=SimpleNamespace(specs=lambda: []),
        )

        result = CreateSubagentsTool(agent).execute(
            {
                "goal": "错误跨树接管",
                "replacement_for_run_ids": [source.id],
                "defer_start": True,
            }
        )
        payload = json.loads(result.output)

        assert result.ok is False
        assert payload["error_code"] == "SUBAGENT_REPLACEMENT_INVALID"
        assert len(manager.list_runs()) == 1

    def test_replacement_record_failure_cancels_new_child_before_start(self, tmp_path, monkeypatch):
        """接管边落账失败时，新 child 必须终态取消且不能发布启动。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
        from agent_py_agent.agent.subagents.manager import SubAgentManager

        manager = SubAgentManager(tmp_path, workspace_root=tmp_path)
        source = manager.create_run(goal="旧任务", thought="执行", plan=["做"], role="worker")

        def fail_record(*_args, **_kwargs):
            raise OSError("state store unavailable")

        monkeypatch.setattr(manager, "record_takeover", fail_record)
        agent = SimpleNamespace(
            config=SimpleNamespace(enable_subagents=True, max_subagents=10, access_mode="workspace-write"),
            subagents=manager,
            tools=SimpleNamespace(specs=lambda: []),
        )

        result = CreateSubagentsTool(agent).execute(
            {
                "goal": "接管旧任务",
                "replacement_for_run_ids": [source.id],
            }
        )
        payload = json.loads(result.output)
        replacement = next(task for task in manager.list_runs() if task.id != source.id)

        assert result.ok is False
        assert payload["error_code"] == "SUBAGENT_REPLACEMENT_RECORD_FAILED"
        assert replacement.status == "CANCELLED"
        assert replacement.attributes["creation_abort"]["code"] == payload["error_code"]

class TestCreateSubagentsToolConfigDefaults:
    """测试 create_subagents 对轻量配置对象的默认值兜底。"""

    def test_missing_access_mode_uses_agent_config_default(self):
        """轻量配置对象缺少 access_mode 时，子代理权限仍回退统一配置默认值。"""
        from types import SimpleNamespace

        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
        from agent_py_agent.agent.settings import AgentConfig

        mock_agent = MagicMock()
        mock_agent.config = SimpleNamespace(
            enable_subagents=True,
            max_subagents=10,
                    )

        mock_task = MagicMock()
        mock_task.id = "run_default"
        mock_task.goal = ""
        mock_task.status = "PENDING"
        mock_task.verification_status = "PENDING"
        mock_task.task_dir = "/tmp"
        mock_agent.subagents.create_run.return_value = mock_task

        result = CreateSubagentsTool(mock_agent).execute({"goal": "测试"})
        call_kwargs = mock_agent.subagents.create_run.call_args[1]

        assert result.ok is True
        assert call_kwargs["params"].parent_access_mode == AgentConfig().access_mode


class TestCreateSubagentsToolTemplatePolicy:
    """测试 create_subagents 的角色模板和工具推断策略。"""

    def test_default_tools_use_role_template_policy(self):
        """默认给子代理基础内置工具，避免少填 allowed_tools 变成残废代理。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        mock_agent.config.access_mode = "full-access"
        mock_agent.config.subagent_memory_retention_policy = "delete_after_days"
        mock_agent.config.subagent_memory_delete_after_days = 7
        mock_agent.config.subagent_destroy_summary_required = False

        mock_task = MagicMock()
        mock_task.id = "run_default"
        mock_task.goal = ""
        mock_task.status = "PENDING"
        mock_task.verification_status = "PENDING"
        mock_task.task_dir = "/tmp"
        mock_agent.subagents.create_run.return_value = mock_task

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({"goal": "测试"})

        call_kwargs = mock_agent.subagents.create_run.call_args[1]
        assert "read_file" in call_kwargs["params"].allowed_tools
        assert "write_file" in call_kwargs["params"].allowed_tools
        assert "terminal_session" in call_kwargs["params"].allowed_tools
        assert "apply_patch" in call_kwargs["params"].allowed_tools
        assert "run_command" in call_kwargs["params"].allowed_tools
        assert "create_subagents" not in call_kwargs["params"].allowed_tools
        assert "send_guidance" not in call_kwargs["params"].allowed_tools
        assert "cancel_subagents" not in call_kwargs["params"].allowed_tools
        assert "resolve_capability_requests" not in call_kwargs["params"].allowed_tools
        assert call_kwargs["params"].parent_access_mode == "full-access"
        assert call_kwargs["params"].memory_retention_policy == "delete_after_days"
        assert call_kwargs["params"].memory_delete_after_days == 7
        assert call_kwargs["params"].destroy_summary_required is False
        assert call_kwargs["params"].role == "worker"
        assert result.ok is True

    def test_explicit_tool_preset_read_only_uses_read_tools_only(self):
        """显式 read_only 是结构化工具边界，不保留写入工具。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10

        mock_task = MagicMock()
        mock_task.id = "run_default"
        mock_task.goal = ""
        mock_task.status = "PENDING"
        mock_task.verification_status = "PENDING"
        mock_task.task_dir = "/tmp"
        mock_agent.subagents.create_run.return_value = mock_task

        tool = CreateSubagentsTool(mock_agent)
        tool.execute({"goal": "测试", "tool_preset": "read_only"})

        call_kwargs = mock_agent.subagents.create_run.call_args[1]
        assert "read_file" in call_kwargs["params"].allowed_tools
        assert "search_text" in call_kwargs["params"].allowed_tools
        assert "capability_request" in call_kwargs["params"].allowed_tools
        assert "write_file" not in call_kwargs["params"].allowed_tools
        assert "apply_patch" not in call_kwargs["params"].allowed_tools
        assert "run_command" not in call_kwargs["params"].allowed_tools

    def test_tool_preset_none_does_not_create_toolless_subagent(self):
        """模型传 tool_preset=none 时回退自动策略，不创建空工具子代理。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10

        mock_task = MagicMock()
        mock_task.id = "run_default"
        mock_task.goal = ""
        mock_task.status = "PENDING"
        mock_task.verification_status = "PENDING"
        mock_task.task_dir = "/tmp"
        mock_agent.subagents.create_run.return_value = mock_task

        tool = CreateSubagentsTool(mock_agent)
        tool.execute({"goal": "测试", "tool_preset": "none"})

        call_kwargs = mock_agent.subagents.create_run.call_args[1]
        assert "read_file" in call_kwargs["params"].allowed_tools
        assert "run_command" in call_kwargs["params"].allowed_tools

    def test_unknown_tool_preset_does_not_override_role_template(self):
        """模型误把 role 写到 tool_preset 时，应回退给 role template 自动决定工具。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10

        mock_task = MagicMock()
        mock_task.id = "coordinator_001"
        mock_task.goal = ""
        mock_task.status = "PLANNING"
        mock_task.verification_status = "UNVERIFIED"
        mock_task.task_dir = "/tmp/coordinator_001"
        mock_agent.subagents.create_run.return_value = mock_task

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({
            "goal": "Seed a root coordinator and let the role template choose tools.",
            "role": "coordinator",
            "tool_preset": "coordinator",
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert "read_file" in params.allowed_tools
        assert "run_command" in params.allowed_tools
        assert params.role == "coordinator"

    def test_unknown_tool_preset_keeps_baseline_when_explicit_tools_are_partial(self):
        """未知 tool_preset 不创建新分支；显式工具列表仍补基础工具。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        mock_task = MagicMock()
        mock_task.id = "frontend_001"
        mock_task.goal = ""
        mock_task.status = "PLANNING"
        mock_task.verification_status = "UNVERIFIED"
        mock_task.task_dir = "/tmp/frontend_001"
        mock_agent.subagents.create_run.return_value = mock_task

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({
            "goal": "修复 /tmp/project/artifacts/index2.html 页面。",
            "tool_preset": "frontend-dev",
            "allowed_tools": ["write_file", "read_file", "run_command"],
            "extra_write_roots": ["/tmp/project/artifacts"],
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert "apply_patch" in params.allowed_tools
        assert "apply_patch" in params.allowed_tools
        assert "read_artifact" in params.allowed_tools

    def test_vague_deliverable_worker_uses_output_files_without_extra_write_root(self):
        """output_files 是目标事实；即使没有额外写根，也不应阻止普通子代理开工。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        mock_task = MagicMock()
        mock_task.id = "writer_001"
        mock_task.goal = ""
        mock_task.status = "PLANNING"
        mock_task.verification_status = "UNVERIFIED"
        mock_task.task_dir = "/tmp/writer_001"
        mock_agent.subagents.create_run.return_value = mock_task

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({
            "goal": "写一个极简单文件。",
            "role": "writer",
            "output_files": ["index.html"],
        })

        assert result.ok is True
        mock_agent.subagents.create_run.assert_called_once()
        created_params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert created_params.extra_write_roots == []

    def test_no_comment_constraint_can_be_preserved_in_child_goal(self):
        """用户要求不要注释时，子任务保留“不要写注释”不应被误判为要求写注释。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        mock_agent._current_user_prompt = "只输出完整 HTML，不要注释。"
        mock_agent.subagents.workspace_root = Path("/tmp/project")
        mock_agent.subagents.workspace_roots = [Path("/tmp/project")]
        mock_task = MagicMock()
        mock_task.id = "frontend_001"
        mock_task.goal = ""
        mock_task.status = "PLANNING"
        mock_task.verification_status = "UNVERIFIED"
        mock_task.task_dir = "/tmp/frontend_001"
        mock_agent.subagents.create_run.return_value = mock_task

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({
            "goal": "请在 /tmp/project/deliverables/index1.html 输出完整 HTML，不要写注释。",
            "role": "worker",
            "extra_write_roots": ["/tmp/project/deliverables"],
        })

        assert result.ok is True
        mock_agent.subagents.create_run.assert_called_once()
