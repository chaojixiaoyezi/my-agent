"""manage_models 工具：代理代替用户增删改切模型目录；回执与归档参数不含密钥，删服务商走危险门，子代理不可用。"""

from __future__ import annotations

import json
from types import SimpleNamespace

from agent_py_agent.agent.capability.model_profile_tool import ManageModelsTool
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.memory_archive.tool_output_externalizer import _safe_parameters
from agent_py_agent.agent.settings.model_profiles import model_profiles_path, read_model_profiles
from agent_py_agent.agent.settings.thread_model_selection import default_model_profile_id
from agent_py_agent.agent.tooling.models import tool_effect_for_runtime_policy
from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call
from agent_py_agent.tests.test_model_profiles import Host

SECRET = "sk-only-private-secret-1234"
PROFILE = {
    "model_name": "MiniMax-M2.7", "model_backend": "anthropic_compatible",
    "api_base": "https://example.test/anthropic", "api_key": SECRET, "model_context_window_tokens": 200000,
}


def _host(tmp_path, owner="alice"):
    host = Host(tmp_path / "config", owner)
    host.conversation_store = ConversationStore(tmp_path / owner, model_default=lambda: default_model_profile_id(host))
    return host


def _run(tool, **params):
    outcome = tool.execute(params)
    return outcome, json.loads(outcome.output)


def _thread(host, conversation_id):
    return host.conversation_store.threads.get_or_create({
        "canonical_user_id": "alice", "owner_id": "alice", "owner_home": "", "channel": "tui",
        "channel_conversation_id": conversation_id, "channel_user_id": "alice", "title": "会话",
    })


def test_add_set_default_and_list_never_echo_secret(tmp_path):
    host = _host(tmp_path)
    tool = ManageModelsTool(host)
    outcome, body = _run(tool, action="add", profile=PROFILE)
    assert outcome.ok and body["ok"] is True and body["action"] == "add"
    assert SECRET not in outcome.output
    profile_id = body["profile_id"]
    row = next(item for item in body["profiles"] if item["id"] == profile_id)
    assert row["has_key"] is True and row["model_name"] == "MiniMax-M2.7" and "api_key" not in row
    outcome, body = _run(tool, action="set_default", profile_id=profile_id)
    assert outcome.ok and body["default_selected"] == profile_id and "hint" in body
    assert read_model_profiles(model_profiles_path(host.home_paths))["selected"] == profile_id
    outcome, body = _run(tool, action="list")
    assert outcome.ok and SECRET not in outcome.output
    assert body["selection_scope"] == "owner_default" and "thread_id" not in body and "can_share" not in body


def test_provider_and_model_lifecycle_with_editing_and_deletes(tmp_path):
    host = _host(tmp_path)
    tool = ManageModelsTool(host)
    outcome, body = _run(tool, action="save_provider", provider_id="minimax",
                         provider={"display_name": "MiniMax", "api_base": "https://example.test/v1", "api_key": SECRET})
    assert outcome.ok and body["provider_id"] == "minimax" and SECRET not in outcome.output
    outcome, body = _run(tool, action="save_model", provider_id="minimax",
                         profile={"model_name": "M2.7", "model_backend": "openai_compatible", "model_context_window_tokens": 128000})
    assert outcome.ok
    profile_id = body["profile_id"]
    # 未声明 editing 的同编号保存被拒，且落盘前就停下。
    outcome, _ = _run(tool, action="save_provider", provider_id="minimax",
                      provider={"display_name": "改名", "api_base": "https://example.test/v1"})
    assert not outcome.ok and outcome.error_code == "MODEL_PROFILE_INVALID" and outcome.effect_outcome == "not_started"
    # 编辑时不带密钥表示保留原密钥，模型不需要再看到它。
    outcome, _ = _run(tool, action="save_provider", provider_id="minimax", editing=True,
                      provider={"display_name": "MiniMax 主号", "api_base": "https://example.test/v1"})
    assert outcome.ok
    provider = read_model_profiles(model_profiles_path(host.home_paths))["providers"]["minimax"]
    assert provider["api_key"] == SECRET and provider["display_name"] == "MiniMax 主号"
    outcome, _ = _run(tool, action="delete_provider", provider_id="minimax")
    assert not outcome.ok and outcome.error_code == "MODEL_PROFILE_INVALID", "先删模型再删服务商"
    outcome, _ = _run(tool, action="delete_model", profile_id=profile_id)
    assert outcome.ok
    outcome, body = _run(tool, action="delete_provider", provider_id="minimax")
    assert outcome.ok and body["providers"] == []


def test_select_switches_only_the_current_conversation(tmp_path):
    host = _host(tmp_path)
    tool = ManageModelsTool(host)
    _, body = _run(tool, action="add", profile=PROFILE)
    profile_id = body["profile_id"]
    current, other = _thread(host, "tui-1"), _thread(host, "tui-2")
    host._current_run_params = SimpleNamespace(task_attributes={"conversation_thread_id": current.thread_id})
    outcome, body = _run(tool, action="select", profile_id=profile_id)
    assert outcome.ok and body["selected"] == profile_id and body["selection_scope"] == "thread"
    assert body["default_selected"] == "default" and "thread_id" not in body
    assert host.conversation_store.threads.load(current.thread_id).model_profile_id == profile_id
    assert host.conversation_store.threads.load(other.thread_id).model_profile_id != profile_id


def test_select_without_conversation_points_to_set_default(tmp_path):
    outcome, body = _run(ManageModelsTool(_host(tmp_path)), action="select", profile_id="default")
    assert not outcome.ok and outcome.error_code == "MODEL_PROFILE_NO_THREAD"
    assert outcome.effect_outcome == "not_started" and "set_default" in body["error"]


def test_invalid_arguments_are_rejected_before_any_write(tmp_path):
    host = _host(tmp_path)
    tool = ManageModelsTool(host)
    for params in (
        {"action": "nuke"}, {"action": "probe"}, {"action": "save_provider", "provider_id": "x"},
        {"action": "add"}, {"action": "save_model", "editing": True, "profile": {"model_name": "m"}},
    ):
        outcome = tool.execute(params)
        assert not outcome.ok and outcome.error_code == "TOOL_INVALID_ARGUMENTS", params
        assert outcome.effect_outcome == "not_started"
    assert not model_profiles_path(host.home_paths).exists()


def test_effect_levels_follow_the_structured_action():
    policy = ManageModelsTool.runtime_policy
    assert tool_effect_for_runtime_policy(policy, {"action": "list"}) == "read_only"
    assert tool_effect_for_runtime_policy(policy, {"action": "probe"}) == "read_only"
    assert tool_effect_for_runtime_policy(policy, {"action": "discover"}) == "read_only"
    assert tool_effect_for_runtime_policy(policy, {"action": "add"}) == "mutating"
    assert tool_effect_for_runtime_policy(policy, {"action": "select"}) == "mutating"
    assert tool_effect_for_runtime_policy(policy, {"action": "delete_model"}) == "mutating"
    assert tool_effect_for_runtime_policy(policy, {"action": "delete_provider"}) == "dangerous"


def test_delete_provider_needs_approval_while_saving_is_autonomous(tmp_path):
    host = _host(tmp_path)
    tool = ManageModelsTool(host)
    saved = execute_canonical_test_call(
        tmp_path, tools={"manage_models": tool}, tool_name="manage_models",
        arguments={"action": "save_provider", "provider_id": "p1",
                   "provider": {"display_name": "P", "api_base": "https://example.test/v1", "api_key": SECRET}},
    )
    assert saved.result.ok is True and saved.result.handler_executed is True
    gated = execute_canonical_test_call(
        tmp_path, tools={"manage_models": tool}, tool_name="manage_models",
        arguments={"action": "delete_provider", "provider_id": "p1"},
    )
    assert gated.result.status == "approval_required" and gated.result.handler_executed is False
    assert "p1" in read_model_profiles(model_profiles_path(host.home_paths))["providers"]


def test_tool_is_hidden_inside_subagent_runs(tmp_path):
    host = _host(tmp_path)
    host._current_subagent_run_id = "run-child-1"
    availability = ManageModelsTool(host).availability()
    assert availability.available is False and availability.error_code == "TOOL_UNAVAILABLE"
    host._current_subagent_run_id = ""
    assert ManageModelsTool(host).availability().available is True


def test_archived_parameters_redact_nested_secrets():
    safe = _safe_parameters({"action": "add", "profile": dict(PROFILE), "provider": {"api_key": SECRET, "display_name": "P"}})
    text = json.dumps(safe, ensure_ascii=False)
    assert SECRET not in text and safe["profile"]["model_name"] == "MiniMax-M2.7" and safe["provider"]["display_name"] == "P"


def test_probe_failure_is_a_provider_fact_without_secret(tmp_path, monkeypatch):
    host = _host(tmp_path)
    tool = ManageModelsTool(host)
    _, body = _run(tool, action="add", profile=PROFILE)

    def boom(*_args, **_kwargs):
        raise RuntimeError("connection refused " + SECRET)

    monkeypatch.setattr("agent_py_agent.agent.settings.model_provider_network.get_backend", boom)
    outcome, result = _run(tool, action="probe", profile_id=body["profile_id"])
    assert not outcome.ok and outcome.error_code == "MODEL_PROBE_FAILED"
    assert result["ok"] is False and result["action"] == "probe" and SECRET not in outcome.output
