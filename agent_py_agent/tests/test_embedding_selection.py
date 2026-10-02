"""语义记忆“向量模型”选择（S1/S2，用户 10-02 拍板）。

锁定：管理员菜单（TUI /model → 选择模型 → 向量模型、IM /model vector）经参数中心边界授权写入两项全局配置，回执说明重启后生效；
my-agent 自配（manage_models set_embedding）只在“本机管理员 + 本人目录里的嵌入档案 + 端点主机与默认对话模型相同”时直接写，
主机不同不写、返回 needs_user_choice；共享档案、普通 owner 一律拒绝；关闭能还原；tool_vector_search_enabled 始终不变。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.capability.model_profile_tool import (
    ManageModelsTool,
    build_manage_models_model_spec,
)
from agent_py_agent.agent.contracts.error_taxonomy import error_contract
from agent_py_agent.agent.contracts.recovery import RecoveryAction
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.gateway_parts import http_handlers, model_profile_service
from agent_py_agent.agent.settings import embedding_selection
from agent_py_agent.agent.settings.config import load_config
from agent_py_agent.agent.settings.model_profiles import (
    execute_model_profile_operation,
    model_profiles_path,
)
from agent_py_agent.agent.settings.model_provider_schema import ModelProfileError
from agent_py_agent.agent.settings.parameter_changes import parameter_history
from agent_py_agent.agent.settings.thread_model_selection import execute_local_model_operation
from agent_py_agent.cli.chat_parts import tui_model_menu
from agent_py_agent.cli.chat_parts.control_runtime import _command_text

CHAT_BASE = "https://api.minimax.test/anthropic"
SECRET = "fake-embedding-credential-5678"


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    home = SimpleNamespace(root=tmp_path, config_dir=tmp_path / "config", owner_provider="local",
                           owner_kind="main", owner_id="local/main")  # 与生产 owner_resolver 的路径式编号一致
    path = tmp_path / "desktop.yaml"
    path.write_text('agent_name: "embedding-selection-test"\n', encoding="utf-8")
    config = load_config(path)
    config.api_base = CHAT_BASE  # 管理员的默认对话模型就是部署配置
    return SimpleNamespace(home_paths=home, config=config)


def _add(host, api_base="https://api.minimax.test/v1", capability="embedding"):
    profile_id = str(uuid4())
    profile = {"model_name": "embo-01", "model_backend": "openai_compatible", "api_base": api_base,
               "api_key": SECRET, "model_context_window_tokens": 4096, "capability": capability}
    execute_model_profile_operation(host, "add", {"profile_id": profile_id, "profile": profile})
    return profile_id


def _member(host):
    host.home_paths.owner_provider, host.home_paths.owner_kind = "feishu", "user"
    host.home_paths.owner_id = "providers/feishu/users/member"
    return host


def _saved(host):
    config = load_config(host.config.config_path)
    return config.embedding_model_profile, config.memory_semantic_recall, config.tool_vector_search_enabled


def _file(host):
    return Path(host.config.config_path).read_text(encoding="utf-8")


def _ledger(host):
    return [(row["key"], row["actor"]) for row in parameter_history(user_path=host.config.config_path, limit=0)]


def _run(tool, **params):
    outcome = tool.execute(params)
    return outcome, json.loads(outcome.output)


def test_same_host_sets_directly_and_closing_restores(host):
    before = _saved(host)
    profile_id = _add(host)
    result = embedding_selection.model_set_embedding(host, profile_id)
    assert result["ok"] and result["restart_required"] and len(result["change_ids"]) == 2
    assert "重启" in result["message"] and SECRET not in json.dumps(result, ensure_ascii=False)
    assert _saved(host) == (profile_id, True, before[2])
    closed = embedding_selection.model_disable_embedding(host)
    assert closed["ok"] and closed["semantic_recall"] is False and "重启" in closed["message"]
    assert _saved(host) == before == ("", False, before[2])
    assert "tool_vector_search_enabled" not in _file(host)
    # 选中先写档案再开召回，关闭先关召回再清档案：任何一步停下都不会把记忆发出去。
    assert _ledger(host)[::-1] == [("embedding_model_profile", "model"), ("memory_semantic_recall", "model"),
                                   ("memory_semantic_recall", "model"), ("embedding_model_profile", "model")]


@pytest.mark.parametrize("api_base,same", [
    ("https://api.minimax.test/v1", True),
    ("https://API.MiniMax.test:443/v1", True),
    ("https://api.minimax.test:8443/v1", False),
    ("http://api.minimax.test/v1", False),
    ("https://emb.minimax.test/v1", False),
    ("https://api.openai.test/v1", False),
])
def test_host_rule_compares_scheme_host_and_port(host, api_base, same):
    profile_id = _add(host, api_base)
    original = _file(host)
    result = embedding_selection.model_set_embedding(host, profile_id)
    assert result["ok"] is same
    if not same:
        assert result["status"] == "needs_user_choice" and result["error_code"] == "EMBEDDING_HOST_DIFFERS"
        assert "/model" in result["message"] and _file(host) == original and _ledger(host) == []


def test_endpoint_host_drops_credentials_and_rejects_unparsable():
    assert embedding_selection._endpoint_host("https://user:pw@api.minimax.test/v1") == "https://api.minimax.test:443"
    assert embedding_selection._endpoint_host("http://Local.Test:8080/x") == "http://local.test:8080"
    for value in ("", "not a url", "https://h.test:99999/v1", "ftp://h.test/x"):
        assert embedding_selection._endpoint_host(value) == ""


def test_selected_chat_profile_is_the_comparison_host(host):
    chat_id = _add(host, "https://api.openai.test/v1", capability="agentic")
    execute_model_profile_operation(host, "set_default", {"profile_id": chat_id})
    assert embedding_selection.model_set_embedding(host, _add(host))["error_code"] == "EMBEDDING_HOST_DIFFERS"
    assert embedding_selection.model_set_embedding(host, _add(host, "https://api.openai.test/embed"))["ok"]


def test_unresolvable_default_chat_model_asks_the_user_instead_of_writing(host):
    chat_id = _add(host, "https://api.minimax.test/anthropic", capability="agentic")
    execute_model_profile_operation(host, "set_default", {"profile_id": chat_id})
    path = model_profiles_path(host.home_paths)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["profiles"][chat_id]["enabled"] = False
    path.write_text(json.dumps(data), encoding="utf-8")
    original = _file(host)
    result = embedding_selection.model_set_embedding(host, _add(host))
    assert result["status"] == "needs_user_choice" and _file(host) == original


def _reference(host, kind):
    if kind == "shared":  # 真实共享出去的嵌入档案：共享引用一律不认，只认本人目录里的原记录
        profile_id = _add(host)
        execute_model_profile_operation(host, "set_shared", {"profile_id": profile_id, "enabled": True})
        return f"shared:{profile_id}"
    return str(uuid4()) if kind == "foreign" else _add(host, capability="agentic")


@pytest.mark.parametrize("reference", ["shared", "foreign", "chat_capability"])
def test_shared_foreign_or_non_embedding_profiles_are_refused_without_writing(host, reference):
    profile_id = _reference(host, reference)
    original = _file(host)
    with pytest.raises(ModelProfileError) as caught:
        embedding_selection.model_set_embedding(host, profile_id)
    assert caught.value.reason == ("capability_mismatch" if reference == "chat_capability" else "profile_not_found")
    with pytest.raises(ModelProfileError):
        embedding_selection.apply_embedding_choice(host, profile_id, actor="chat")
    assert _file(host) == original and _ledger(host) == []


def test_ordinary_owner_is_refused_on_every_entry(host):
    profile_id = _add(_member(host))
    original = _file(host)
    for result in (embedding_selection.model_set_embedding(host, profile_id),
                   embedding_selection.model_disable_embedding(host),
                   embedding_selection.apply_embedding_choice(host, profile_id, actor="chat")):
        assert result["ok"] is False and result["error_code"] == "PARAMETER_BOUNDARY" and "管理员" in result["message"]
    assert embedding_selection.embedding_choices(host)["can_change"] is False
    assert _file(host) == original and _ledger(host) == []


@pytest.mark.parametrize("operation", ["select", "off"])
def test_second_step_failure_stops_on_the_side_that_sends_nothing(host, monkeypatch, operation):
    profile_id = _add(host)
    if operation == "off":
        embedding_selection.apply_embedding_choice(host, profile_id, actor="chat")
    real = embedding_selection.set_parameter
    calls = []

    def flaky(key, value, **kwargs):
        calls.append(key)
        return real(key, value, **kwargs) if len(calls) == 1 else {"ok": False, "code": "PARAMETER_NOT_EFFECTIVE",
                                                                      "error": "写入后没有生效"}

    monkeypatch.setattr(embedding_selection, "set_parameter", flaky)
    result = embedding_selection.apply_embedding_choice(host, profile_id if operation == "select" else "", actor="chat")
    assert result["ok"] is False and result["error_code"] == "PARAMETER_NOT_EFFECTIVE" and len(result["change_ids"]) == 1
    assert _saved(host)[1] is False  # 召回始终是关的


def test_choices_list_only_own_embedding_profiles_and_restart_state(host):
    profile_id = _add(host)
    _add(host, capability="agentic")
    listing = embedding_selection.embedding_choices(host)
    assert [row["id"] for row in listing["choices"]] == [profile_id] and listing["can_change"] is True
    assert listing["restart_pending"] is False and SECRET not in json.dumps(listing, ensure_ascii=False)
    embedding_selection.apply_embedding_choice(host, profile_id, actor="chat")
    pending = embedding_selection.embedding_choices(host)
    assert pending["restart_pending"] is True and pending["saved"] == {"profile_id": profile_id, "semantic_recall": True}
    assert "重启 Gateway 后生效" in pending["message"]


def test_manage_models_actions_map_outcomes(host):
    tool = ManageModelsTool(host)
    enum = build_manage_models_model_spec().input_schema["properties"]["action"]["enum"]
    assert {"set_embedding", "disable_embedding"} <= set(enum)
    outcome, body = _run(tool, action="set_embedding", profile_id=_add(host))
    assert outcome.ok and body["restart_required"] and SECRET not in outcome.output
    outcome, body = _run(tool, action="set_embedding", profile_id=_add(host, "https://api.openai.test/v1"))
    assert not outcome.ok and outcome.error_code == "EMBEDDING_HOST_DIFFERS" and outcome.effect_outcome == "not_started"
    assert body["status"] == "needs_user_choice"
    outcome, _ = _run(tool, action="set_embedding", profile_id=f"shared:{uuid4()}")
    assert outcome.error_code == "TOOL_PERMISSION_DENIED" and outcome.effect_outcome == "not_started"
    outcome, _ = _run(tool, action="set_embedding")
    assert outcome.error_code == "TOOL_INVALID_ARGUMENTS"
    outcome, body = _run(tool, action="disable_embedding")
    assert outcome.ok and body["semantic_recall"] is False
    outcome, _ = _run(ManageModelsTool(_member(host)), action="disable_embedding")
    assert outcome.error_code == "TOOL_PERMISSION_DENIED" and outcome.effect_outcome == "not_started"
    assert error_contract("EMBEDDING_HOST_DIFFERS").recommended_action == RecoveryAction.REQUEST_USER_INPUT.value


def test_manage_models_list_shows_semantic_memory_state_and_next_step_to_admins_only(host):
    profile_id = _add(host)
    _, body = _run(ManageModelsTool(host), action="list")
    view = body["semantic_memory"]
    assert view["choices"] == [{"id": profile_id, "model_name": "embo-01"}] and view["restart_pending"] is False
    assert view["running"] == {"profile_id": "", "semantic_recall": False} and "set_embedding" in view["hint"]
    embedding_selection.model_set_embedding(host, profile_id)
    _, body = _run(ManageModelsTool(host), action="list")
    assert body["semantic_memory"]["restart_pending"] is True and "disable_embedding" in body["semantic_memory"]["hint"]
    _, body = _run(ManageModelsTool(_member(host)), action="list")
    assert "semantic_memory" not in body


def test_manage_models_partial_write_keeps_unknown_effect(host, monkeypatch):
    monkeypatch.setattr(embedding_selection, "apply_embedding_choice", lambda *_a, **_k: {
        "ok": False, "error_code": "PARAMETER_NOT_EFFECTIVE", "change_ids": ["c1"], "message": "第二步没有生效"})
    outcome, _ = _run(ManageModelsTool(host), action="set_embedding", profile_id=_add(host))
    assert outcome.error_code == "TOOL_EXECUTION_FAILED" and outcome.reported_error_code == "PARAMETER_NOT_EFFECTIVE"
    assert outcome.effect_outcome == ""  # 可能已写一步：副作用未知，交给对账


def _menu(monkeypatch, listing, picked):
    calls, shown = [], []

    async def request(app, agent, session_id, operation, payload=None):
        calls.append((operation, payload))
        return listing if operation == "embedding_list" else {"ok": True, "message": "已保存，重启 Gateway 后生效"}

    async def dialog(app, title, body, actions, *, focus=None, completion=None):
        shown.append([value for value, _label in focus.values])
        return picked

    monkeypatch.setattr(tui_model_menu, "_request", request)
    monkeypatch.setattr(tui_model_menu, "_dialog", dialog)
    message = asyncio.run(tui_model_menu._vector_menu(None, None, "sess"))
    return message, calls, shown


@pytest.mark.parametrize("picked,expected", [
    ("p1", ("embedding_select", {"profile_id": "p1"})), ("", ("embedding_off", {})), (None, None),
])
def test_tui_vector_menu_saves_choice_or_off_and_back_writes_nothing(monkeypatch, picked, expected):
    listing = {"ok": True, "can_change": True, "message": "当前运行：关闭（只按关键词）。",
               "choices": [{"id": "p1", "model_name": "embo-01", "provider_name": "MiniMax"}]}
    message, calls, shown = _menu(monkeypatch, listing, picked)
    assert shown == [["p1", ""]]
    assert calls[1:] == ([expected] if expected else [])
    assert ("重启" in message) is bool(expected)


def test_tui_vector_menu_tells_members_it_is_admin_only(monkeypatch):
    listing = {"ok": True, "can_change": False, "message": "当前运行：关闭（只按关键词）。", "choices": []}
    message, calls, shown = _menu(monkeypatch, listing, "p1")
    assert "管理员" in message and shown == [] and [op for op, _ in calls] == ["embedding_list"]


def test_tui_select_menu_offers_vector_entry(monkeypatch):
    offered = []

    async def choose(app, title, rows):
        offered.extend(value for value, _label in rows)
        return "vector"

    async def vector(app, agent, session_id):
        return "vector-menu"

    monkeypatch.setattr(tui_model_menu, "_choose_action", choose)
    monkeypatch.setattr(tui_model_menu, "_vector_menu", vector)
    assert asyncio.run(tui_model_menu._select_menu(None, None, "sess", None)) == "vector-menu"
    assert offered == ["chat", "decision", "vector"]


def test_local_tui_path_reaches_the_same_entry_without_a_session_store(host):
    profile_id = _add(host)
    listing = execute_local_model_operation(host, "sess", "embedding_list", {})
    assert [row["id"] for row in listing["choices"]] == [profile_id]
    assert execute_local_model_operation(host, "sess", "embedding_select", {"profile_id": profile_id})["ok"]
    assert _saved(host)[:2] == (profile_id, True) and _ledger(host)[0][1] == "chat"


def test_gateway_menu_request_routes_to_embedding_entry(host, monkeypatch):
    profile_id = _add(host)
    handler = SimpleNamespace(_read_json=lambda: {"operation": "embedding_select", "conversation_id": "sess",
                                                  "profile_id": profile_id}, reply=None)
    handler._send_json = lambda status, payload: setattr(handler, "reply", (status, payload))
    monkeypatch.setattr(http_handlers, "require_trusted_source", lambda _handler: False)
    monkeypatch.setattr(http_handlers, "_request_channel", lambda _handler: ("main", "local"))
    monkeypatch.setattr(http_handlers, "_gateway_control_scope", lambda *_a, **_k: object())
    monkeypatch.setattr(model_profile_service, "_scoped_model_host",
                        lambda _agent, _scope: (host, SimpleNamespace(thread_id="t1")))
    model_profile_service.handle_client_models(handler, SimpleNamespace(agent=object()))
    assert handler.reply[0] == 200 and handler.reply[1]["ok"] and _saved(host)[:2] == (profile_id, True)


@pytest.mark.parametrize("text,value,valid", [
    ("/model vector", "", True), ("/model vector 2", "2", True), ("/model vector off", "off", True),
    ("/model vector a b", "a b", False),
])
def test_im_vector_command_parses_and_rebuilds(text, value, valid):
    command = parse_conversation_control(text, reject_unknown_slash=True)
    assert (command.kind, command.operation, command.value, command.valid) == ("model", "vector", value, valid)
    if valid:
        assert _command_text(command) == text
    assert parse_conversation_control("/model default 1", reject_unknown_slash=True).operation == "set_default"


def _im(host, monkeypatch, text):
    monkeypatch.setattr(model_profile_service, "_scoped_model_host",
                        lambda _agent, _scope: (host, SimpleNamespace(thread_id="t1")))
    command = parse_conversation_control(text, reject_unknown_slash=True)
    return model_profile_service.execute_model_text_control(host, command, None)


def test_im_vector_view_select_and_off(host, monkeypatch):
    profile_id = _add(host)
    view = _im(host, monkeypatch, "/model vector")
    assert view.ok and "1. embo-01" in view.message and profile_id in view.message and SECRET not in view.message
    assert not _im(host, monkeypatch, "/model vector 9").ok
    chosen = _im(host, monkeypatch, "/model vector 1")
    assert chosen.ok and "重启" in chosen.message and _saved(host)[:2] == (profile_id, True)
    assert _im(host, monkeypatch, "/model vector off").ok and _saved(host)[:2] == ("", False)


def test_im_vector_member_is_refused(host, monkeypatch):
    profile_id = _add(_member(host))
    result = _im(host, monkeypatch, f"/model vector {profile_id}")
    assert not result.ok and "管理员" in result.message and _ledger(host) == []
    view = _im(host, monkeypatch, "/model vector")
    assert view.ok and "只能查看" in view.message and "用法" not in view.message
