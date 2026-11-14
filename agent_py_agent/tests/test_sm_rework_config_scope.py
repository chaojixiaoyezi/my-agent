"""验证全局配置仅在主会话开放，受限运行仍保留决策设置分流。"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent_py_agent.agent import runtime_context
from agent_py_agent.agent.settings.config import load_config
from agent_py_agent.agent.settings.live_switches import refresh_live_switches, register_live_config
from agent_py_agent.agent.settings.parameter_changes import ledger_path, parameter_history
from agent_py_agent.agent.subagents.role_templates import active_model_subagent_tools
from agent_py_agent.agent.tooling import user_config_tool as module
from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call

_DECISION_ACTIONS = {
    "decision_read", "decision_patch", "decision_reset",
    "decision_models", "decision_probe",
}
_RESTRICTED_CONTEXTS = (
    "runtime", "legacy", "task_local", "control_plane", "isolated", "agent_run_id", "combined",
)


@pytest.fixture
def local_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[SimpleNamespace, Path]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    monkeypatch.delenv("MY_AGENT_CONFIG", raising=False)
    config_dir = home / "config"
    config_dir.mkdir()
    path = config_dir / "desktop.yaml"
    path.write_text(
        "enable_self_learning: true\nself_learning_max_skills: 50\nmemory_lesson_archive_days: 15\n",
        encoding="utf-8",
    )
    config = load_config(path)
    register_live_config(config)
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_provider="local", owner_kind="main", owner_id="main"),
        config=config,
        _current_run_params=SimpleNamespace(
            run_id="main-run", task_id="main-task", context_scope="default", task_attributes={},
        ),
    )
    return agent, path


@contextmanager
def _restricted_context(agent: SimpleNamespace, kind: str) -> Iterator[None]:
    """分别设置各类运行标记并恢复原状态，避免组合标记遮住单独分支。"""
    main_params = agent._current_run_params
    scope = kind if kind in {"task_local", "control_plane", "isolated"} else "default"
    attrs = {"agent_run_id": "child-run"} if kind in {"agent_run_id", "combined"} else {}
    agent._current_run_params = SimpleNamespace(
        run_id="child-run", task_id="child-task",
        context_scope="task_local" if kind == "combined" else scope, task_attributes=attrs,
    )
    previous = None
    try:
        if kind in {"runtime", "combined"}:
            previous = runtime_context.set_current_subagent_context(
                agent, run_id="child-run", attempt_id="child-attempt", task_attributes=attrs,
            )
        elif kind == "legacy":
            agent._current_subagent_run_id = "child-run"
        yield
    finally:
        if previous is not None:
            runtime_context.restore_current_subagent_context(agent, previous)
        if kind == "legacy":
            del agent._current_subagent_run_id
        agent._current_run_params = main_params


def test_main_session_keeps_full_schema_persistence_history_and_live_refresh(local_agent, tmp_path):
    agent, path = local_agent
    tool = module.UserConfigTool(agent)
    full_schema = module.UserConfigTool.model_spec.input_schema
    assert module._is_main_owner(agent) is True
    assert tool.model_spec.input_schema == full_schema
    assert {"set", "reset", "revert"} <= set(full_schema["properties"]["action"]["enum"])

    changes = (
        ("self_learning_max_skills", "51", 51),
        ("enable_self_learning", "false", False),
        ("memory_lesson_archive_days", "30", 30),
    )
    for count, (key, value, expected) in enumerate(changes, start=1):
        execution = execute_canonical_test_call(
            tmp_path / key, tools={"user_config": tool}, tool_name="user_config",
            arguments={"action": "set", "key": key, "value": value},
            allowed_tools=["user_config"], run_id="main-run", call_id="main-" + key,
            trusted_run_context={"run_scope": {"agent_kind": "main", "run_id": "main-run"}},
        )
        assert execution.decision.status == "allow", execution.decision
        assert execution.result.ok, execution.result
        assert execution.result.handler_executed is True
        assert getattr(load_config(path), key) == expected
        history = parameter_history(user_path=path, limit=0)
        assert len(history) == count
        assert {item["key"] for item in history} == {item[0] for item in changes[:count]}
        assert key in refresh_live_switches(agent.config)
        assert getattr(agent.config, key) == expected

    assert ledger_path(path).read_bytes()
    assert module.UserConfigTool(agent).model_spec.input_schema == full_schema


@pytest.mark.parametrize("context_kind", _RESTRICTED_CONTEXTS)
def test_restricted_context_has_decision_only_schema(local_agent, context_kind):
    agent, _path = local_agent
    shared_home = agent.home_paths
    full_schema = module.UserConfigTool.model_spec.input_schema
    with _restricted_context(agent, context_kind):
        assert agent.home_paths is shared_home
        assert module._is_main_owner(agent) is False
        schema = module.UserConfigTool(agent).model_spec.input_schema
        assert set(schema["properties"]["action"]["enum"]) == _DECISION_ACTIONS
        assert schema["required"] == ["action"]
        assert not {"key", "value", "include_constants", "include_full_config"} & schema["properties"].keys()
    assert runtime_context.current_subagent_run_id(agent) == ""
    assert module._is_main_owner(agent) is True
    assert module.UserConfigTool(agent).model_spec.input_schema == full_schema


@pytest.mark.parametrize("context_kind", _RESTRICTED_CONTEXTS)
@pytest.mark.parametrize("entrypoint", ("canonical", "direct"))
def test_restricted_global_changes_leave_config_and_ledger_bytes_unchanged(
    local_agent, tmp_path, context_kind, entrypoint,
):
    agent, path = local_agent
    # 沿用主会话构造的完整声明，确认执行阶段自身复核，而不只依赖声明裁剪。
    tool = module.UserConfigTool(agent)
    initial = tool.execute({"action": "set", "key": "self_learning_max_skills", "value": "51"})
    assert initial.ok, initial.output
    history = parameter_history(user_path=path, limit=0)
    assert len(history) == 1
    before_config = path.read_bytes()
    before_ledger = ledger_path(path).read_bytes()
    operations = (
        {"action": "set", "key": "enable_self_learning", "value": "false"},
        {"action": "reset", "key": "self_learning_max_skills"},
        {"action": "revert", "change_id": str(history[0]["id"])},
    )
    allowed_tools = active_model_subagent_tools(["user_config"])
    assert allowed_tools == ["user_config"]
    with _restricted_context(agent, context_kind):
        for operation in operations:
            if entrypoint == "canonical":
                execution = execute_canonical_test_call(
                    tmp_path / operation["action"], tools={"user_config": tool}, tool_name="user_config",
                    arguments=operation, allowed_tools=allowed_tools, run_id="child-run",
                    attempt_id="child-attempt", call_id="child-" + operation["action"],
                    trusted_run_context={"run_scope": {"agent_kind": "subagent", "run_id": "child-run"}},
                )
                assert execution.decision.status == "allow", execution.decision
                assert execution.result.handler_executed is True
                result = execution.result
            else:
                result = tool.execute(operation)
            assert not result.ok, result
            assert result.error_code == "TOOL_PERMISSION_DENIED"
            assert result.effect_outcome == "not_started"
            assert path.read_bytes() == before_config
            assert ledger_path(path).read_bytes() == before_ledger
            assert parameter_history(user_path=path, limit=0) == history

        refresh_live_switches(agent.config)
        assert agent.config.enable_self_learning is True
        assert agent.config.self_learning_max_skills == 51

    # 同一工具返回主会话后继续可写，证明限制没有残留到后续主会话。
    main_result = execute_canonical_test_call(
        tmp_path / "main", tools={"user_config": tool}, tool_name="user_config",
        arguments={"action": "set", "key": "enable_self_learning", "value": "false"},
        allowed_tools=["user_config"], run_id="main-run", call_id="main-resumed",
        trusted_run_context={"run_scope": {"agent_kind": "main", "run_id": "main-run"}},
    )
    assert main_result.result.ok, main_result.result
    refresh_live_switches(agent.config)
    assert agent.config.enable_self_learning is False
    assert len(parameter_history(user_path=path, limit=0)) == 2


@pytest.mark.parametrize("action", sorted(_DECISION_ACTIONS))
def test_child_keeps_all_decision_routes(local_agent, tmp_path, action):
    agent, path = local_agent
    before_config = path.read_bytes()
    assert not ledger_path(path).exists()
    target = "_decision_model_operation" if action in {"decision_models", "decision_probe"} else "_decision"
    operation = action if target == "_decision_model_operation" else action.removeprefix("decision_")
    params = {"action": action}
    outcome = ToolHandlerOutcome("user_config", True, "decision route")

    with _restricted_context(agent, "combined"):
        tool = module.UserConfigTool(agent)
        # 仅替换分流终点，避免目录与连接操作；构造和工具执行仍走真实入口。
        with patch.object(module, target, return_value=outcome) as route:
            assert tool.execute(params) is outcome
            route.assert_called_once_with(agent, operation, params)
            execution = execute_canonical_test_call(
                tmp_path / action, tools={"user_config": tool}, tool_name="user_config",
                arguments=params, allowed_tools=active_model_subagent_tools(["user_config"]),
                run_id="child-run", attempt_id="child-attempt", call_id=action,
                trusted_run_context={"run_scope": {"agent_kind": "subagent", "run_id": "child-run"}},
            )
            assert execution.decision.status == "allow", execution.decision
            assert execution.result.ok, execution.result
            assert execution.result.handler_executed is True
            assert route.call_count == 2
            route.assert_called_with(agent, operation, params)
    assert path.read_bytes() == before_config
    assert not ledger_path(path).exists()


@pytest.mark.parametrize("agent", (
    None,
    SimpleNamespace(home_paths=SimpleNamespace(owner_provider="local", owner_kind="user", owner_id="alice")),
))
def test_missing_or_non_admin_context_has_no_global_set(agent):
    assert module._is_main_owner(agent) is False
    schema = module.UserConfigTool(agent).model_spec.input_schema
    assert set(schema["properties"]["action"]["enum"]) == _DECISION_ACTIONS
    result = module.UserConfigTool(agent).execute({"action": "set", "key": "enable_self_learning", "value": "false"})
    assert not result.ok
    assert result.error_code == "TOOL_PERMISSION_DENIED"
    assert result.effect_outcome == "not_started"
