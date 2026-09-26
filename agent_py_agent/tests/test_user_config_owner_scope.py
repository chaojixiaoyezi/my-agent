"""普通 owner 的 user_config 只暴露本人决策设置，legacy 本机配置仍限主代理。"""

from __future__ import annotations

import json
from types import SimpleNamespace

from agent_py_agent.agent.contracts.tool_manifest_contract import tool_manifest_payload
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.runtime_context import (
    restore_current_subagent_context,
    set_current_subagent_context,
)
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.settings.model_profiles import model_profiles_path
from agent_py_agent.agent.tooling.user_config_tool import UserConfigTool
from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call
from agent_py_agent.tests.test_decision_settings import host_at


# LLM: 使用真实 Agent 注册链和原 owner identity，两个 owner 共用一个私有 home 以测试隔离而不联网。
# 函数用途: 构造本机 main/user 的工具快照及设置存储，用于回归工具展示和执行双门。
def _agent(tmp_path, monkeypatch, *, owner_kind: str, owner_id: str, provider: str = "local") -> SimpleAgent:
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "home"))
    return SimpleAgent(
        AgentConfig(model_backend="echo", enable_tools=True,
            my_agent_owner_provider=provider, my_agent_owner_kind=owner_kind, my_agent_owner_id=owner_id),
        tmp_path / "project",
    )


def test_user_manifest_has_decision_only_user_config_and_no_gateway_status(tmp_path, monkeypatch):
    user = _agent(tmp_path, monkeypatch, owner_kind="user", owner_id="alice")
    manifest = tool_manifest_payload(user.tools.runtime_snapshot(run_id="owner-test"))

    assert manifest["owner_type"] == "user"
    assert "user_config" in manifest["visible_tools"]
    assert "user_config" in manifest["executable_tools"]
    assert "gateway_status" not in manifest["visible_tools"]
    assert "restart_gateway" not in manifest["visible_tools"]
    entry = next(item for item in manifest["tools"] if item["name"] == "user_config")
    schema = entry["input_schema"]
    assert schema["required"] == ["action"]
    assert set(schema["properties"]["action"]["enum"]) == {
        "decision_read", "decision_patch", "decision_reset", "decision_models", "decision_probe", "decision_experiment_revoke",
    }
    assert "key" not in schema["properties"] and "value" not in schema["properties"]
    assert "MY_AGENT_CONFIG" not in entry["description"]


def test_remote_user_gets_decision_only_but_group_gets_no_config_tool(tmp_path, monkeypatch):
    remote = _agent(tmp_path, monkeypatch, owner_kind="user", owner_id="ou-alice", provider="feishu")
    group = _agent(tmp_path, monkeypatch, owner_kind="group", owner_id="group-a", provider="feishu")
    user_manifest = tool_manifest_payload(remote.tools.runtime_snapshot(run_id="remote-user"))
    group_manifest = tool_manifest_payload(group.tools.runtime_snapshot(run_id="remote-group"))

    assert "user_config" in user_manifest["visible_tools"]
    assert "view" not in next(item for item in user_manifest["tools"] if item["name"] == "user_config")["input_schema"]["properties"]["action"]["enum"]
    assert "user_config" not in group_manifest["visible_tools"]
    assert "gateway_status" not in group_manifest["visible_tools"]
    assert "restart_gateway" not in group_manifest["visible_tools"]


def test_user_legacy_actions_rejected_even_when_called_directly(tmp_path, monkeypatch):
    user = _agent(tmp_path, monkeypatch, owner_kind="user", owner_id="alice")
    protected = tmp_path / "global.yaml"
    protected.write_text("memory_compact_auto_trigger_percent: 90\n", encoding="utf-8")
    monkeypatch.setenv("MY_AGENT_CONFIG", str(protected))
    tool = user.tools.tools["user_config"]

    for params in ({}, {"action": "view"}, {"action": "set", "key": "memory_compact_auto_trigger_percent", "value": "20"}):
        result = tool.execute(params)
        assert not result.ok and result.error_code == "TOOL_PERMISSION_DENIED"
        assert result.effect_outcome == "not_started"
        assert str(protected) not in result.output
    assert protected.read_text(encoding="utf-8") == "memory_compact_auto_trigger_percent: 90\n"


def test_missing_owner_identity_cannot_gain_legacy_config_actions():
    tool = UserConfigTool(SimpleNamespace(home_paths=SimpleNamespace()))

    assert "view" not in tool.model_spec.input_schema["properties"]["action"]["enum"]
    result = tool.execute({"action": "view"})
    assert not result.ok and result.error_code == "TOOL_PERMISSION_DENIED"


def test_user_legacy_action_rejected_by_canonical_model_schema(tmp_path, monkeypatch):
    user = _agent(tmp_path, monkeypatch, owner_kind="user", owner_id="alice")
    execution = execute_canonical_test_call(tmp_path, tools={"user_config": user.tools.tools["user_config"]},
        tool_name="user_config", arguments={"action": "view"})

    assert not execution.result.ok
    assert execution.result.error_code == "TOOL_INVALID_ARGUMENTS"


def test_user_decision_cas_and_owner_isolation(tmp_path, monkeypatch):
    alice = _agent(tmp_path, monkeypatch, owner_kind="user", owner_id="alice")
    bob = _agent(tmp_path, monkeypatch, owner_kind="user", owner_id="bob")
    tool = alice.tools.tools["user_config"]
    first = json.loads(tool.execute({"action": "decision_read"}).output)
    assert first["revision"] == {"owner": 0, "thread": 0}

    saved = tool.execute({"action": "decision_patch", "expected_revision": first["revision"],
        "changes": {"enabled": True, "timeout_seconds": 6}})
    assert saved.ok
    assert json.loads(saved.output)["effective"]["timeout_seconds"] == 6
    stale = tool.execute({"action": "decision_patch", "expected_revision": first["revision"],
        "changes": {"enabled": False}})
    assert not stale.ok and stale.error_code == "STALE_VERSION"
    assert json.loads(bob.tools.tools["user_config"].execute({"action": "decision_read"}).output)["effective"]["enabled"] is False
    assert not model_profiles_path(bob.home_paths).exists()
    assert model_profiles_path(alice.home_paths) != model_profiles_path(bob.home_paths)
    for field in ("owner_id", "thread_id", "run_id"):
        result = tool.execute({"action": "decision_read", field: "bob"})
        assert not result.ok and result.error_code == "TOOL_INVALID_ARGUMENTS"


def test_main_agent_keeps_complete_config_tool_and_gateway_status(tmp_path, monkeypatch):
    main = _agent(tmp_path, monkeypatch, owner_kind="main", owner_id="main")
    manifest = tool_manifest_payload(main.tools.runtime_snapshot(run_id="main-test"))

    assert manifest["owner_type"] == "main_agent"
    assert "gateway_status" in manifest["visible_tools"]
    assert "restart_gateway" in manifest["visible_tools"]
    entry = next(item for item in manifest["tools"] if item["name"] == "user_config")
    assert {"view", "set", "decision_read", "decision_patch"}.issubset(
        set(entry["input_schema"]["properties"]["action"]["enum"]))
    assert {"key", "value"}.issubset(entry["input_schema"]["properties"])
    assert main.tools.tools["user_config"].execute({"action": "decision_read"}).ok


def test_gateway_main_turn_uses_trusted_run_thread_for_decision_settings(tmp_path):
    host = host_at(tmp_path)
    thread = host.conversation_store.threads.get_or_create(
        {"canonical_user_id": "alice", "owner_id": "alice", "channel_conversation_id": "current"})
    host._current_run_params = SimpleNamespace(task_attributes={"conversation_thread_id": thread.thread_id})
    tool = UserConfigTool(host)

    read = tool.execute({"action": "decision_read"})
    assert read.ok, read.output
    report = json.loads(read.output)
    assert report["thread_id"] == thread.thread_id
    assert report["scope_resolution"]["source"] == "current_run_params"
    saved = tool.execute({"action": "decision_patch", "scope": "thread",
        "expected_revision": report["revision"], "changes": {"enabled": True}})
    assert saved.ok, saved.output
    assert host.conversation_store.threads.load(thread.thread_id).decision_settings["overrides"] == {"enabled": True}


def test_child_without_own_thread_cannot_fall_back_to_parent_run_thread(tmp_path):
    host = host_at(tmp_path)
    parent = host.conversation_store.threads.get_or_create(
        {"canonical_user_id": "alice", "owner_id": "alice", "channel_conversation_id": "parent"})
    host._current_run_params = SimpleNamespace(task_attributes={"conversation_thread_id": parent.thread_id})
    previous = set_current_subagent_context(host, run_id="child-run", task_attributes={})
    try:
        result = UserConfigTool(host).execute({"action": "decision_read", "scope": "thread"})
        assert not result.ok and result.error_code == "TOOL_PERMISSION_DENIED"
    finally:
        restore_current_subagent_context(host, previous)


def test_decision_read_puts_cas_revision_in_bounded_tool_preview(tmp_path):
    host = host_at(tmp_path)
    tool = UserConfigTool(host)
    output = tool.execute({"action": "decision_read"}).output

    assert len(output) > 4000  # 原投影包含所有接入点；检查真实有界预览，而非碰巧完整展示。
    assert '"revision"' in output[:4000]
    assert '"subagent_model"' in output[:4000]
