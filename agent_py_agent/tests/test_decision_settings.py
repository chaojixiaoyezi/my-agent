"""原 owner/thread 决策设置的离线原子性、继承、迁移与权限回归。"""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from agent_py_agent.agent.capability.config import CapabilityConfig, load_capability_config
from agent_py_agent.agent.conversation.models import ConversationThread
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.settings.config import AgentConfig, load_config
from agent_py_agent.agent.settings.decision_settings import (
    execute_decision_settings_operation as execute,
)
from agent_py_agent.agent.settings.decision_settings_schema import (
    DecisionSettingsConflict,
    empty_decision_settings,
)
from agent_py_agent.agent.settings.memory import normalize_memory_settings
from agent_py_agent.agent.settings.model_profiles import (
    execute_model_profile_operation,
    model_profiles_path,
    read_model_profiles,
)
from agent_py_agent.agent.settings.model_provider_schema import ModelProfileError
from agent_py_agent.agent.settings.shared_model_catalog import (
    resolve_shared_model,
    set_shared_profile,
)
from agent_py_agent.tests.test_decision_model_profiles import decision
from agent_py_agent.tests.test_model_profiles import Host, add
from agent_py_agent.tests.test_shared_model_catalog import admin_host


# LLM: 只使用临时目录和原 ConversationStore，不构造 Agent 或后端，不发送模型请求。
# 函数用途: 为设置服务提供已认证宿主所需的最小上下文。
def host_at(path, owner="alice"):
    host = Host(path / "config", owner=owner)
    host.capability_config = CapabilityConfig()
    host.conversation_store = ConversationStore(path / "conversations")
    return host


# LLM: 通过真实共用服务获取当前 CAS 代次；测试竞争另行固定旧版本，不伪造写入接口。
# 函数用途: 便捷提交一次字段修改并返回原服务读回。
def patch(host, changes, *, thread_id="", scope="owner"):
    revision = execute(host, "read", {"scope": scope}, thread_id=thread_id)["revision"]
    return execute(host, "patch", {"scope": scope, "changes": changes, "expected_revision": revision}, thread_id=thread_id)


def test_defaults_no_model_no_agent_no_credentials_and_no_owner_file(tmp_path):
    host = host_at(tmp_path)
    result = execute(host, "read", {})
    assert result["ok"] and result["revision"] == {"owner": 0, "thread": 0}
    assert result["effective"]["enabled"] is False
    assert result["effective"]["timeout_seconds"] == 2
    assert result["effective"]["stage_timeout_seconds"] == 4
    assert result["effective"]["points"]["curator"]["timeout_seconds"] == 4
    assert all(point["mode"] == "off" for point in result["effective"]["points"].values())
    assert not model_profiles_path(host.home_paths).exists()
    assert "api_key" not in json.dumps(result)


def test_owner_partial_patch_reset_and_stage_upper_bound(tmp_path):
    host = host_at(tmp_path)
    result = patch(host, {"timeout_seconds": 5, "enabled": True, "points.recall.mode": "observe"})
    assert result["effective"]["points"]["recall"]["max_request_seconds"] == 4
    assert result["effective"]["points"]["recall"]["limiting_field"] == "stage_timeout_seconds"
    result = patch(host, {"enabled": False})
    assert result["effective"]["points"]["recall"]["mode"] == "observe"
    assert result["effective"]["points"]["recall"]["effective_mode"] == "off"
    result = patch(host, {"enabled": True, "points.recall.timeout_seconds": 1})
    result = execute(host, "reset", {"fields": ["points.recall.timeout_seconds"], "expected_revision": result["revision"]})
    assert result["effective"]["points"]["recall"]["timeout_seconds"] == 5
    assert result["sources"]["points.recall.timeout_seconds"] == "inherit:timeout_seconds:owner"
    assert result["effective_from"] == "next_request" and result["stage_budget_policy"] == "preserve_started_stage"
    assert result["overrides"]["owner"] == {"enabled": True, "timeout_seconds": 5, "points.recall.mode": "observe"}
    assert result["before"]["overrides"]["owner"]["points.recall.timeout_seconds"] == 1


def test_thread_override_reset_owner_cas_and_original_state_preserved(tmp_path):
    host = host_at(tmp_path)
    thread = host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})
    host.conversation_store.threads.update_atomic(thread.thread_id, lambda t: replace(t, compact_generation=9, summary="keep", metadata={"other": True}))
    result = patch(host, {"timeout_seconds": 3}, thread_id=thread.thread_id)
    old_revision = result["revision"]
    patch(host, {"background_timeout_seconds": 6})
    with pytest.raises(DecisionSettingsConflict):
        execute(host, "patch", {"scope": "thread", "expected_revision": old_revision, "changes": {"enabled": True}}, thread_id=thread.thread_id)
    result = patch(host, {"timeout_seconds": 7}, thread_id=thread.thread_id, scope="thread")
    assert result["sources"]["timeout_seconds"] == "thread"
    assert result["effective"]["timeout_seconds"] == 7
    result = execute(host, "reset", {"scope": "thread", "fields": ["timeout_seconds"], "expected_revision": result["revision"]}, thread_id=thread.thread_id)
    assert result["effective"]["timeout_seconds"] == 3
    stored = host.conversation_store.threads.load(thread.thread_id)
    assert (stored.compact_generation, stored.summary, stored.metadata) == (9, "keep", {"other": True})
    assert stored.decision_settings["overrides"] == {}


@pytest.mark.parametrize("scope", ["owner", "thread"])
def test_same_revision_has_exactly_one_concurrent_winner(tmp_path, scope):
    host = host_at(tmp_path)
    thread = host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})
    revision = execute(host, "read", {}, thread_id=thread.thread_id)["revision"]
    barrier = threading.Barrier(2)

    def update(seconds):
        barrier.wait()
        try:
            return execute(host, "patch", {"scope": scope, "expected_revision": revision, "changes": {"timeout_seconds": seconds}}, thread_id=thread.thread_id)
        except DecisionSettingsConflict:
            return "conflict"

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(update, (3, 5)))
    assert sum(item == "conflict" for item in results) == 1
    assert execute(host, "read", {}, thread_id=thread.thread_id)["revision"][scope] == 1


@pytest.mark.parametrize("value", [True, False, 0, -1, float("nan"), float("inf"), -float("inf"), "2", None, 10**1000])
def test_invalid_seconds_rejected_without_replacing_original(tmp_path, value):
    host = host_at(tmp_path)
    patch(host, {"enabled": True})
    path = model_profiles_path(host.home_paths)
    original = path.read_bytes()
    with pytest.raises(ModelProfileError):
        patch(host, {"timeout_seconds": value})
    assert path.read_bytes() == original


@pytest.mark.parametrize("changes", [{"enabled": 1}, {"points.other.mode": "apply"}, {"points.recall.mode": "auto"}, {"points.recall.profile_id": "../../secret"}, {"points.recall.timeout_seconds": None}])
def test_unknown_points_bad_modes_and_reference_paths_rejected(tmp_path, changes):
    with pytest.raises(ModelProfileError):
        patch(host_at(tmp_path), changes)


def test_disabled_profile_can_bind_without_key_and_broken_ref_can_be_disabled(tmp_path):
    host = host_at(tmp_path)
    key, _ = decision(host)
    data = read_model_profiles(model_profiles_path(host.home_paths))
    provider_id = data["profiles"][key]["provider_id"]
    execute_model_profile_operation(host, "save_provider", {"provider_id": provider_id, "editing": True, "clear_key": True, "provider": {**data["providers"][provider_id], "api_key": "", "enabled": False}})
    result = patch(host, {"profile_id": key, "points.recall.mode": "apply"})
    assert result["effective"]["points"]["recall"]["connection"]["configured"] is False
    execute_model_profile_operation(host, "delete_model", {"profile_id": key})
    result = patch(host, {"enabled": False})
    assert result["effective"]["profile_id"] == key
    assert result["effective"]["points"]["recall"]["connection"]["reason"] == "unavailable"
    assert "secret" not in json.dumps(result)


def test_generation_profile_cannot_bind_as_decision(tmp_path):
    host = host_at(tmp_path)
    key, _ = add(host)
    with pytest.raises(ModelProfileError, match="用途"):
        patch(host, {"profile_id": key})


def test_shared_missing_credentials_is_configurable_but_revocation_still_blocks_binding(tmp_path):
    host = host_at(tmp_path)
    admin = admin_host(tmp_path / "config")
    key, _ = decision(admin)
    set_shared_profile(admin, key, True)
    data = read_model_profiles(model_profiles_path(admin.home_paths))
    provider_id = data["profiles"][key]["provider_id"]
    execute_model_profile_operation(admin, "save_provider", {"provider_id": provider_id, "editing": True, "clear_key": True, "provider": {"enabled": False}})
    result = patch(host, {"profile_id": "shared:" + key})
    assert result["effective"]["points"]["recall"]["connection"]["configured"] is False
    with pytest.raises(ModelProfileError):
        resolve_shared_model(host.home_paths, "shared:" + key, capability="decision")
    assert "only-private-secret" not in model_profiles_path(host.home_paths).read_text()
    set_shared_profile(admin, key, False)
    result = patch(host, {"enabled": False})
    assert result["effective"]["points"]["recall"]["connection"]["reason"] == "unavailable"
    with pytest.raises(ModelProfileError):
        patch(host, {"profile_id": "shared:" + key})


def test_owner_lock_stays_held_through_thread_cas_and_save(tmp_path, monkeypatch):
    host = host_at(tmp_path)
    thread = host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})
    revision = execute(host, "read", {}, thread_id=thread.thread_id)["revision"]
    entered, release, owner_finished = threading.Event(), threading.Event(), threading.Event()
    original = host.conversation_store.threads.update_atomic

    def hold_thread_update(thread_id, update):
        entered.set()
        assert release.wait(2)
        return original(thread_id, update)

    def owner_update():
        try:
            return execute(host, "patch", {"expected_revision": revision, "changes": {"timeout_seconds": 8}}, thread_id=thread.thread_id)
        finally:
            owner_finished.set()

    monkeypatch.setattr(host.conversation_store.threads, "update_atomic", hold_thread_update)
    with ThreadPoolExecutor(2) as pool:
        child = pool.submit(execute, host, "patch", {"scope": "thread", "expected_revision": revision, "changes": {"timeout_seconds": 3}}, thread_id=thread.thread_id)
        assert entered.wait(2)
        owner = pool.submit(owner_update)
        try:
            assert not owner_finished.wait(0.05)
        finally:
            release.set()
        assert child.result()["revision"] == {"owner": 0, "thread": 1}
        with pytest.raises(DecisionSettingsConflict):
            owner.result()


def test_owner_and_thread_isolation_and_payload_cannot_supply_identity(tmp_path):
    host = host_at(tmp_path)
    patch(host, {"timeout_seconds": 8})
    other = host_at(tmp_path, "bob")
    assert execute(other, "read", {})["effective"]["timeout_seconds"] == 2
    thread = host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})
    with pytest.raises(ModelProfileError, match="其他用户"):
        execute(other, "read", {}, thread_id=thread.thread_id)
    with pytest.raises(ModelProfileError):
        execute(host, "read", {"owner_id": "bob"})


def test_v3_owner_migration_preserves_existing_fields_until_explicit_write(tmp_path):
    host = host_at(tmp_path)
    key, _ = add(host)
    path = model_profiles_path(host.home_paths)
    legacy = json.loads(path.read_text())
    legacy["schema"] = "owner_model_profiles.v3"
    legacy.pop("decision_settings")
    legacy["extension"] = {"retained": True}
    path.write_text(json.dumps(legacy))
    before = path.read_bytes()
    execute(host, "read", {})
    assert path.read_bytes() == before
    patch(host, {"enabled": False})
    migrated = json.loads(path.read_text())
    assert migrated["schema"] == "owner_model_profiles.v4"
    assert migrated["extension"] == legacy["extension"]
    assert migrated["providers"] == legacy["providers"] and migrated["profiles"][key] == legacy["profiles"][key]


def test_v9_thread_migration_preserves_unknown_extensions_on_atomic_write(tmp_path):
    host = host_at(tmp_path)
    thread = host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})
    path = host.conversation_store.threads.storage.thread_path(thread.thread_id)
    legacy = json.loads(path.read_text())
    legacy.update(schema_version="conversation_thread.v9", compact_generation=7, future_extension={"keep": 1})
    legacy.pop("decision_settings")
    path.write_text(json.dumps(legacy))
    before = path.read_bytes()
    assert execute(host, "read", {}, thread_id=thread.thread_id)["revision"]["thread"] == 0
    assert path.read_bytes() == before
    patch(host, {"enabled": False}, thread_id=thread.thread_id, scope="thread")
    migrated = json.loads(path.read_text())
    assert migrated["schema_version"] == "conversation_thread.v10"
    assert migrated["future_extension"] == {"keep": 1} and migrated["compact_generation"] == 7


@pytest.mark.parametrize("data", [
    {"schema_version": "conversation_thread.v99"},
    {"schema_version": "conversation_thread.v10"},
    {"schema_version": "conversation_thread.v10", "decision_settings": {}},
    {"schema_version": "conversation_thread.v10", "decision_settings": {**empty_decision_settings(), "revision": True}},
])
def test_unknown_thread_schema_and_invalid_current_envelope_rejected(data):
    with pytest.raises(ModelProfileError):
        ConversationThread.from_dict({"thread_id": "t", "canonical_user_id": "alice", **data})


def test_defaults_follow_original_config_domains_and_fractional_yaml(tmp_path):
    host = host_at(tmp_path)
    host.config = AgentConfig(decision_timeout_seconds=2.5, memory_decision_recall_mode="observe")
    host.capability_config = CapabilityConfig(decision_subagent_model_mode="apply", decision_subagent_model_timeout_seconds=0.75)
    result = execute(host, "read", {})
    assert result["effective"]["points"]["recall"]["mode"] == "observe"
    assert result["effective"]["points"]["subagent_model"]["timeout_seconds"] == 0.75
    assert result["sources"]["points.subagent_model.mode"] == "capability_config.decision_subagent_model_mode"
    path = tmp_path / "agent.yaml"
    path.write_text("decision_timeout_seconds: 0.75\nmemory_decision_recall_timeout_seconds: null\n")
    config = load_config(path)
    assert config.decision_timeout_seconds == 0.75 and config.memory_decision_recall_timeout_seconds is None
    capability = tmp_path / "capability.yaml"
    capability.write_text("decision_skill_tool_timeout_seconds: 0.25\n")
    assert load_capability_config(capability).decision_skill_tool_timeout_seconds == 0.25
    memory, _ = normalize_memory_settings({"memory_decision_curator_timeout_seconds": 0.1})
    assert memory.memory_decision_curator_timeout_seconds == 0.1


@pytest.mark.parametrize("value", ["true", "false", "0", "-1", "NaN", "Infinity", "1e400"])
def test_invalid_decision_time_in_yaml_is_not_silently_replaced(tmp_path, value):
    path = tmp_path / "agent.yaml"
    path.write_text(f"decision_timeout_seconds: {value}\n")
    with pytest.raises(ModelProfileError):
        load_config(path)
