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
    assert result["effective"]["timeout_seconds"] == 3
    assert result["effective"]["stage_timeout_seconds"] == 5
    assert result["effective"]["points"]["curator"]["timeout_seconds"] == 4
    assert all(point["mode"] == "off" for point in result["effective"]["points"].values())
    assert not model_profiles_path(host.home_paths).exists()
    assert "api_key" not in json.dumps(result)


def test_owner_partial_patch_reset_and_stage_upper_bound(tmp_path):
    host = host_at(tmp_path)
    # 阶段默认 5 秒（2026-10-02 由 4 提到 5），单次期限设得更长时由阶段封顶。
    result = patch(host, {"timeout_seconds": 6, "enabled": True, "points.recall.mode": "observe"})
    assert result["effective"]["points"]["recall"]["max_request_seconds"] == 5
    assert result["effective"]["points"]["recall"]["limiting_field"] == "stage_timeout_seconds"
    result = patch(host, {"enabled": False})
    assert result["effective"]["points"]["recall"]["mode"] == "observe"
    assert result["effective"]["points"]["recall"]["effective_mode"] == "off"
    result = patch(host, {"enabled": True, "points.recall.timeout_seconds": 1})
    result = execute(host, "reset", {"fields": ["points.recall.timeout_seconds"], "expected_revision": result["revision"]})
    assert result["effective"]["points"]["recall"]["timeout_seconds"] == 6
    assert result["sources"]["points.recall.timeout_seconds"] == "inherit:timeout_seconds:owner"
    assert result["effective_from"] == "next_request" and result["stage_budget_policy"] == "preserve_started_stage"
    assert result["overrides"]["owner"] == {"enabled": True, "timeout_seconds": 6, "points.recall.mode": "observe"}
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


def test_restore_owner_sets_and_unsets_in_one_revision_without_intermediate_state(tmp_path):
    host = host_at(tmp_path)
    original = patch(host, {"enabled": True, "timeout_seconds": 3})
    changed = patch(host, {"enabled": False, "points.recall.mode": "observe"})
    result = execute(host, "restore", {"expected_revision": changed["revision"],
        "set": {"enabled": original["overrides"]["owner"]["enabled"]},
        "unset": ["points.recall.mode"]})
    assert result["revision"] == {"owner": changed["revision"]["owner"] + 1, "thread": 0}
    assert result["before"]["overrides"]["owner"] == changed["overrides"]["owner"]
    assert result["overrides"]["owner"] == original["overrides"]["owner"]
    assert read_model_profiles(model_profiles_path(host.home_paths))["decision_settings"]["revision"] == result["revision"]["owner"]


def test_restore_owner_stale_revision_never_overwrites_later_user_edit(tmp_path):
    host = host_at(tmp_path)
    original = patch(host, {"enabled": True})
    patch(host, {"enabled": False})
    path = model_profiles_path(host.home_paths)
    before = path.read_bytes()
    with pytest.raises(DecisionSettingsConflict):
        execute(host, "restore", {"expected_revision": original["revision"],
            "set": {"enabled": True}, "unset": []})
    assert path.read_bytes() == before


def test_restore_preserves_explicit_empty_value_distinct_from_inheritance(tmp_path):
    host = host_at(tmp_path)
    original = patch(host, {"profile_id": ""})
    changed = patch(host, {"enabled": True, "profile_id": ""})
    restored = execute(host, "restore", {"expected_revision": changed["revision"],
        "set": {"profile_id": original["overrides"]["owner"]["profile_id"]},
        "unset": ["enabled"]})
    assert restored["overrides"]["owner"] == {"profile_id": ""}
    assert "profile_id" in restored["overrides"]["owner"]
    assert "enabled" not in restored["overrides"]["owner"]


def test_restore_thread_preserves_other_state_and_later_user_edit_wins(tmp_path):
    host = host_at(tmp_path)
    thread = host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})
    host.conversation_store.threads.update_atomic(thread.thread_id,
        lambda row: replace(row, compact_generation=11, summary="保留历史", metadata={"other": True}))
    original = patch(host, {"timeout_seconds": 3}, thread_id=thread.thread_id, scope="thread")
    changed = patch(host, {"timeout_seconds": 5, "points.recall.mode": "observe"},
        thread_id=thread.thread_id, scope="thread")
    # 用户在实验之后先改了别的字段，旧恢复必须因完整 owner/thread CAS 失败，不能覆盖其操作。
    latest = patch(host, {"enabled": True}, thread_id=thread.thread_id, scope="thread")
    before = host.conversation_store.threads.storage.thread_path(thread.thread_id).read_bytes()
    with pytest.raises(DecisionSettingsConflict):
        execute(host, "restore", {"scope": "thread", "expected_revision": changed["revision"],
            "set": {"timeout_seconds": 3}, "unset": ["points.recall.mode"]}, thread_id=thread.thread_id)
    assert host.conversation_store.threads.storage.thread_path(thread.thread_id).read_bytes() == before
    restored = execute(host, "restore", {"scope": "thread", "expected_revision": latest["revision"],
        "set": {"timeout_seconds": original["overrides"]["thread"]["timeout_seconds"]},
        "unset": ["points.recall.mode"]}, thread_id=thread.thread_id)
    stored = host.conversation_store.threads.load(thread.thread_id)
    assert restored["revision"] == {"owner": 0, "thread": latest["revision"]["thread"] + 1}
    assert restored["overrides"]["thread"] == {"timeout_seconds": 3, "enabled": True}
    assert (stored.compact_generation, stored.summary, stored.metadata) == (11, "保留历史", {"other": True})


@pytest.mark.parametrize("set_values,unset", [
    ({}, []), ({"enabled": True}, ["enabled"]), ({}, ["enabled", "enabled"]),
    ({"points.other.mode": "apply"}, []), ({}, ["points.other.mode"]),
    ({"enabled": 1}, []), ({"background_timeout_seconds": 3}, []),
])
def test_restore_invalid_or_wrong_scope_leaves_thread_bytes_unchanged(tmp_path, set_values, unset):
    host = host_at(tmp_path)
    thread = host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})
    path = host.conversation_store.threads.storage.thread_path(thread.thread_id)
    before = path.read_bytes()
    revision = execute(host, "read", {}, thread_id=thread.thread_id)["revision"]
    with pytest.raises(ModelProfileError):
        execute(host, "restore", {"scope": "thread", "expected_revision": revision,
            "set": set_values, "unset": unset}, thread_id=thread.thread_id)
    assert path.read_bytes() == before


def test_restore_does_not_revive_deleted_decision_profile(tmp_path):
    host = host_at(tmp_path)
    key, _ = decision(host)
    result = patch(host, {"enabled": True})
    execute_model_profile_operation(host, "delete_model", {"profile_id": key})
    path = model_profiles_path(host.home_paths)
    before = path.read_bytes()
    with pytest.raises(ModelProfileError):
        execute(host, "restore", {"expected_revision": result["revision"],
            "set": {"profile_id": key}, "unset": []})
    assert path.read_bytes() == before


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
    assert execute(other, "read", {})["effective"]["timeout_seconds"] == 3
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
    legacy.pop("catalog_generation", None)
    legacy["extension"] = {"retained": True}
    path.write_text(json.dumps(legacy))
    before = path.read_bytes()
    execute(host, "read", {})
    assert path.read_bytes() == before
    patch(host, {"enabled": False})
    migrated = json.loads(path.read_text())
    assert migrated["schema"] == "owner_model_profiles.v5"
    assert isinstance(migrated["catalog_generation"], str) and migrated["catalog_generation"]
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
    host.config = AgentConfig(decision_timeout_seconds=2.5, memory_decision_recall_mode="observe",
                              memory_decision_pre_recall_mode="apply")
    host.capability_config = CapabilityConfig(decision_subagent_model_mode="apply")
    result = execute(host, "read", {})
    assert result["effective"]["points"]["recall"]["mode"] == "observe"
    assert result["effective"]["points"]["pre_recall"]["mode"] == "apply"
    # 点位期限与模型引用不再有配置字段：没有覆盖时继承通用值，来源写明继承；选模型两点另有 5 秒下限。
    assert result["effective"]["points"]["recall"]["timeout_seconds"] == 2.5
    assert result["sources"]["points.recall.timeout_seconds"].startswith("inherit:timeout_seconds:")
    assert result["effective"]["points"]["subagent_model"]["timeout_seconds"] == 5.0
    assert result["sources"]["points.subagent_model.timeout_seconds"] == "point_default:5"
    assert result["sources"]["points.subagent_model.mode"] == "capability_config.decision_subagent_model_mode"
    path = tmp_path / "agent.yaml"
    path.write_text("decision_timeout_seconds: 0.75\nmemory_decision_recall_timeout_seconds: 0.5\n"
                    "decision_planning_profile_id: someone\n")
    config = load_config(path)
    assert config.decision_timeout_seconds == 0.75
    assert not hasattr(config, "memory_decision_recall_timeout_seconds")
    assert {"memory_decision_recall_timeout_seconds", "decision_planning_profile_id"} <= {
        warning.split("'")[1] for warning in config.config_warnings if "unknown config key" in warning}
    capability = tmp_path / "capability.yaml"
    capability.write_text("decision_skill_tool_mode: observe\n")
    assert load_capability_config(capability).decision_skill_tool_mode == "observe"
    # 2026-09-27 起与主配置一致：已删除的点位期限写在能力配置里只告警并忽略，不再拒绝加载。
    capability.write_text("decision_skill_tool_timeout_seconds: 0.25\n")
    stale = load_capability_config(capability)
    assert any("decision_skill_tool_timeout_seconds" in w for w in stale.config_warnings)
    assert stale.decision_skill_tool_mode == "off"
    memory, _ = normalize_memory_settings({"memory_decision_curator_mode": "observe"})
    assert memory.memory_decision_curator_mode == "observe"
    # 按点位单独设期限只走用户长期设置覆盖层。
    view = patch(host, {"points.planning.timeout_seconds": 1.5})
    assert view["effective"]["points"]["planning"]["timeout_seconds"] == 1.5
    assert view["sources"]["points.planning.timeout_seconds"] == "owner"


@pytest.mark.parametrize("value", ["true", "false", "0", "-1", "NaN", "Infinity", "1e400"])
def test_invalid_decision_time_in_yaml_is_not_silently_replaced(tmp_path, value):
    path = tmp_path / "agent.yaml"
    path.write_text(f"decision_timeout_seconds: {value}\n")
    with pytest.raises(ModelProfileError):
        load_config(path)


def test_foreground_default_is_three_seconds_and_a_user_point_override_is_kept(tmp_path):
    from agent_py_agent.agent.settings.user_config_capability import packaged_config_path

    # 2026-10-01：前台单次期限默认 2→3 秒；随包 YAML 与 dataclass 必须是同一个值，点位不覆盖时继承它。
    assert AgentConfig().decision_timeout_seconds == 3.0
    assert load_config(packaged_config_path()).decision_timeout_seconds == 3.0
    host = host_at(tmp_path)
    # 2026-10-02（已定做法 10）：选模型点位不覆盖时默认不低于 5 秒，阶段默认 5 秒，所以实际也能等满 5 秒。
    point = execute(host, "read", {})["effective"]["points"]["model_selection"]
    assert (point["timeout_seconds"], point["max_request_seconds"]) == (5.0, 5.0)
    assert point["limiting_field"] == "points.model_selection.timeout_seconds"
    # 用户给选模型单独设的 5 秒（生产现值）是覆盖层的值，改默认不影响它；其它前台点位跟着新默认走。
    view = patch(host, {"points.model_selection.timeout_seconds": 5.0, "stage_timeout_seconds": 5.0})
    assert view["effective"]["points"]["model_selection"]["max_request_seconds"] == 5.0
    assert view["effective"]["points"]["planning"]["max_request_seconds"] == 3.0


def test_selection_points_default_to_at_least_five_seconds_and_overrides_still_win(tmp_path):
    from agent_py_agent.agent.settings.decision_settings_defaults import (
        POINT_TIMEOUT_FLOOR_SECONDS,
        SELECTION_POINT_TIMEOUT_SECONDS,
    )
    from agent_py_agent.agent.settings.user_config_capability import packaged_config_path
    from agent_py_agent.cli.chat_parts.tui_decision_menu import _source

    assert SELECTION_POINT_TIMEOUT_SECONDS == 5.0 and set(POINT_TIMEOUT_FLOOR_SECONDS) == {"model_selection", "subagent_model"}
    assert AgentConfig().decision_stage_timeout_seconds == load_config(packaged_config_path()).decision_stage_timeout_seconds == 5.0
    host = host_at(tmp_path)
    view = execute(host, "read", {})
    for name in ("model_selection", "subagent_model"):
        point = view["effective"]["points"][name]
        assert (point["timeout_seconds"], point["max_request_seconds"]) == (5.0, 5.0), name
        assert view["sources"][f"points.{name}.timeout_seconds"] == "point_default:5"
    assert view["effective"]["points"]["planning"]["timeout_seconds"] == 3.0, "其它前台点位仍继承通用 3 秒"
    assert view["sources"]["points.planning.timeout_seconds"].startswith("inherit:timeout_seconds:")
    assert _source("point_default:5") == "本点位默认（不低于 5 秒）"

    overridden = patch(host, {"points.model_selection.timeout_seconds": 3.0})
    assert overridden["effective"]["points"]["model_selection"]["timeout_seconds"] == 3.0, "点位覆盖优先，可以低于下限"
    assert overridden["sources"]["points.model_selection.timeout_seconds"] == "owner"
    raised = patch(host, {"timeout_seconds": 8.0, "stage_timeout_seconds": 9.0})
    assert raised["effective"]["points"]["subagent_model"]["timeout_seconds"] == 8.0, "通用期限更长时跟着通用走"
    assert raised["sources"]["points.subagent_model.timeout_seconds"].startswith("inherit:timeout_seconds:")
    store = host.conversation_store
    thread = store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})
    session = patch(host, {"points.subagent_model.timeout_seconds": 2.0}, thread_id=thread.thread_id, scope="thread")
    assert session["effective"]["points"]["subagent_model"]["timeout_seconds"] == 2.0, "本会话覆盖同样优先"
