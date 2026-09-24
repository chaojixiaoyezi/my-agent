from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent import plugin_host_api as api
from agent_py_agent.agent.plugin_manifest import (
    PLUGIN_PACKAGE_SCHEMA_V4,
    PluginManifest,
    PluginPackageError,
)


class _Ref:
    def __init__(self, activation_id="act-1"):
        self.scope = SimpleNamespace(activation_id=activation_id)
        self.current = activation_id
        self.owner_value = SimpleNamespace(identity="owner-a", plugins_dir=None)

    def require(self):
        if self.current is None:
            raise ValueError("revoked")
        return SimpleNamespace(activation=SimpleNamespace(activation_id=self.current))

    def owner(self):
        return self.owner_value


@pytest.fixture(autouse=True)
def _reset():
    api.set_host_api_base(None)
    yield
    api.set_host_api_base(None)


def test_tokens_only_exist_while_gateway_serves_and_die_with_activation():
    ref = _Ref()
    assert api.issue_host_api_env(ref, "harness") == {}
    api.set_host_api_base("http://127.0.0.1:8420")
    env = api.issue_host_api_env(ref, "harness")
    assert env[api.HOST_API_URL_ENV] == "http://127.0.0.1:8420/plugin-host/query"
    token = env[api.HOST_API_TOKEN_ENV]
    assert api.verify_host_api_token(token).plugin_id == "harness"
    assert api.verify_host_api_token("forged") is None
    ref.current = "act-2"  # 换代
    assert api.verify_host_api_token(token) is None
    ref.current = "act-1"
    assert api.verify_host_api_token(token) is None  # 失效后不复活
    env = api.issue_host_api_env(ref, "harness")
    ref.current = None  # 停用/卸载
    assert api.verify_host_api_token(env[api.HOST_API_TOKEN_ENV]) is None
    env = api.issue_host_api_env(_Ref(), "harness")
    api.set_host_api_base(None)  # Gateway 停止
    assert api.verify_host_api_token(env[api.HOST_API_TOKEN_ENV]) is None


def test_query_whitelists_topics_and_projects_public_thread_fields(monkeypatch):
    thread = SimpleNamespace(thread_id="t1", title="  很长的标题 " * 20, status="active", updated_at=5.0,
                             compact_generation=2, summary="不应出现", owner_home="/secret")
    store = SimpleNamespace(threads=SimpleNamespace(list_report=lambda limit: ([thread], []), load=lambda tid: thread))
    owner_agent = SimpleNamespace(conversation_store=store)
    monkeypatch.setattr("agent_py_agent.agent.gateway_parts.request_worker._resolve_loaded_request_agent_for_owner",
                        lambda base, identity: owner_agent)
    monkeypatch.setattr(api, "_plugins", lambda owner: [{"plugin_id": "p", "version": "1", "enabled": True, "summary": "s"}])
    monkeypatch.setattr("agent_py_agent.agent.conversation.agent_activity.conversation_agent_activity",
                        lambda agent, store, tid: SimpleNamespace(to_dict=lambda: {"compact_count": 1}))
    grant = api._Grant(_Ref(), "harness")
    result = api.query_host(object(), grant, {"topics": ["threads", "plugins", "activity"], "thread_id": "t1"})
    row = result["threads"][0]
    assert set(row) == {"thread_id", "title", "status", "updated_at", "compact_generation"} and len(row["title"]) <= 60
    assert "不应出现" not in str(result) and "/secret" not in str(result)
    assert result["plugins"][0]["plugin_id"] == "p" and result["activity"]["context"]["compact_count"] == 1
    for bad in ({"topics": ["messages"]}, {"topics": []}, {"topics": ["activity"]}):
        with pytest.raises(ValueError):
            api.query_host(object(), grant, bad)


def test_manifest_v4_host_api_round_trip():
    payload = {
        "schema_version": PLUGIN_PACKAGE_SCHEMA_V4, "plugin_id": "harness-console", "version": "0.1.0",
        "summary": "控制台", "entry_module": "harness_console", "entry_wheel": "wheels/a.whl",
        "wheels": [{"path": "wheels/a.whl", "sha256": "0" * 64}], "actions": [], "default_action": "",
        "settings_schema": {"type": "object", "properties": {}},
        "tools": [{"name": "open", "description": "打开", "input_schema": {"type": "object", "properties": {}},
                   "requested_effect": "mutating"}],
        "panels": [], "skills": [], "host_api": ["read"],
    }
    manifest = PluginManifest.from_payload(payload)
    assert manifest.host_api == ("read",) and manifest.to_payload()["schema_version"] == PLUGIN_PACKAGE_SCHEMA_V4
    assert PluginManifest.from_payload(manifest.to_payload()) == manifest
    for bad in (["write"], [], ["read", "read"]):
        with pytest.raises(PluginPackageError):
            PluginManifest.from_payload({**payload, "host_api": bad})
