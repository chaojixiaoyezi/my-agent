"""决策设置作用范围、历史覆盖清理及原运行入口的离线验证。"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.config import load_capability_config
from agent_py_agent.agent.conversation import decision_model_call as calls
from agent_py_agent.agent.conversation import decision_policy as policy
from agent_py_agent.agent.conversation import decision_service as service
from agent_py_agent.agent.settings.decision_settings import (
    execute_decision_settings_operation as execute,
)
from agent_py_agent.agent.settings.decision_settings_schema import (
    GENERAL_FIELDS,
    POINT_FIELDS,
    POINT_RUNTIME_SCOPES,
    DecisionSettingsConflict,
    decision_field_scopes,
    decision_point_fields,
)
from agent_py_agent.agent.settings.model_profiles import model_profiles_path
from agent_py_agent.agent.settings.model_provider_schema import ModelProfileError
from agent_py_agent.tests.test_decision_model_profiles import decision
from agent_py_agent.tests.test_decision_protocol import questions
from agent_py_agent.tests.test_decision_service import successful
from agent_py_agent.tests.test_decision_settings import host_at, patch


@pytest.fixture
def configured(tmp_path):
    host = host_at(tmp_path)
    key, _ = decision(host, model_name="owner-decision")
    thread = host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})
    patch(host, {"enabled": True, "profile_id": key, "points.curator.mode": "apply",
                 "points.recall.mode": "apply", "background_timeout_seconds": 8, "stage_timeout_seconds": 1})
    return host, thread, key


# LLM: 只注入此前 schema 合法而当前不再允许新增的覆盖，模拟旧版本持久记录；不绕过实际 read/reset 服务。
# 函数用途: 在原线程存储保留历史后台字段及其他会话状态，供兼容清理测试使用。
def legacy_thread(host, thread_id, overrides):
    return host.conversation_store.threads.update_atomic(thread_id, lambda current: replace(
        current, summary="保留原摘要", compact_generation=7,
        decision_settings={**current.decision_settings, "revision": current.decision_settings["revision"] + 1,
                           "overrides": {**current.decision_settings["overrides"], **overrides}},
    ))


def test_metadata_is_one_complete_field_registry_and_returns_independent_views(configured):
    host, thread, _key = configured
    view = execute(host, "read", {"scope": "thread"}, thread_id=thread.thread_id)
    fields = view["field_scopes"]
    expected = set(GENERAL_FIELDS) | {f"points.{point}.{field}" for point in POINT_RUNTIME_SCOPES for field in decision_point_fields(point)}
    assert set(fields) == expected
    assert fields["background_timeout_seconds"] == ["owner"]
    assert all(fields[f"points.curator.{field}"] == ["owner"] for field in POINT_FIELDS)
    assert fields["enabled"] == fields["points.recall.mode"] == ["owner", "thread"]
    assert {point: row["runtime_scope"] for point, row in view["effective"]["points"].items()} == dict(POINT_RUNTIME_SCOPES)
    fields["points.curator.mode"].append("thread")
    assert decision_field_scopes()["points.curator.mode"] == ["owner"]


def test_subagent_candidate_ids_are_structured_owner_and_thread_settings(configured):
    host, thread, key = configured
    field = "points.subagent_model.candidate_profile_ids"
    owner = patch(host, {field: [key]})
    assert owner["effective"]["points"]["subagent_model"]["candidate_profile_ids"] == [key]
    scoped = patch(host, {field: ["shared:" + key]}, scope="thread", thread_id=thread.thread_id)
    assert scoped["effective"]["points"]["subagent_model"]["candidate_profile_ids"] == ["shared:" + key]
    assert scoped["sources"][field] == "thread"
    for invalid in ("all", [key, key], [""], ["not-a-profile"]):
        with pytest.raises(ModelProfileError):
            patch(host, {field: invalid}, scope="thread", thread_id=thread.thread_id)
    assert execute(host, "read", {"scope": "thread"}, thread_id=thread.thread_id)["effective"]["points"]["subagent_model"]["candidate_profile_ids"] == ["shared:" + key]


def test_capability_yaml_candidate_ids_validate_as_model_refs(configured, tmp_path):
    _host, _thread, key = configured
    path = tmp_path / "capability.yaml"
    path.write_text(f'decision_subagent_model_candidate_profile_ids: ["{key}"]\n', encoding="utf-8")
    assert load_capability_config(path).decision_subagent_model_candidate_profile_ids == [key]
    path.write_text('decision_subagent_model_candidate_profile_ids: ["not-a-profile"]\n', encoding="utf-8")
    with pytest.raises(ModelProfileError):
        load_capability_config(path)


@pytest.mark.parametrize("key,value", [
    ("background_timeout_seconds", 5), ("points.curator.mode", "off"),
    ("points.curator.timeout_seconds", 0.5), ("points.curator.profile_id", ""),
])
def test_new_thread_background_patch_rejects_entire_transaction(configured, monkeypatch, key, value):
    host, thread, _key = configured
    view = execute(host, "read", {"scope": "thread"}, thread_id=thread.thread_id)
    thread_path = host.conversation_store.threads.storage.thread_path(thread.thread_id)
    owner_path = model_profiles_path(host.home_paths)
    originals = thread_path.read_bytes(), owner_path.read_bytes()
    notifications = []
    monkeypatch.setattr(policy, "notify_decision_settings_changed", lambda *_: notifications.append(True))
    with pytest.raises(ModelProfileError, match="作用范围"):
        execute(host, "patch", {"scope": "thread", "expected_revision": view["revision"],
            "changes": {"timeout_seconds": 17, key: value}}, thread_id=thread.thread_id)
    assert (thread_path.read_bytes(), owner_path.read_bytes()) == originals
    assert not notifications
    assert execute(host, "read", {}, thread_id=thread.thread_id)["revision"] == view["revision"]


@pytest.mark.parametrize("owner_enabled,thread_enabled", [(True, False), (False, True)])
def test_curator_uses_owner_switch_and_profile_while_front_points_inherit_thread(configured, owner_enabled, thread_enabled):
    host, thread, owner_key = configured
    thread_key, _ = decision(host, model_name="thread-decision")
    patch(host, {"enabled": owner_enabled})
    view = patch(host, {"enabled": thread_enabled, "profile_id": thread_key}, scope="thread", thread_id=thread.thread_id)
    curator = view["effective"]["points"]["curator"]
    recall = view["effective"]["points"]["recall"]
    assert (curator["enabled"], curator["profile_id"], curator["enabled_source"]) == (owner_enabled, owner_key, "owner")
    assert curator["effective_mode"] == ("apply" if owner_enabled else "off")
    assert (recall["enabled"], recall["profile_id"], recall["enabled_source"]) == (thread_enabled, thread_key, "thread")
    assert recall["effective_mode"] == ("apply" if thread_enabled else "off")
    assert view["sources"]["points.curator.profile_id"] == "inherit:profile_id:owner"
    assert view["sources"]["points.recall.profile_id"] == "inherit:profile_id:thread"


@pytest.mark.parametrize("point_seconds,expected,limiting_field", [
    (None, 8, "points.curator.timeout_seconds"),
    (1.5, 1.5, "points.curator.timeout_seconds"),
    (12, 8, "background_timeout_seconds"),
])
def test_curator_upper_bound_uses_background_budget_and_ignores_old_thread_values(configured, point_seconds, expected, limiting_field):
    host, thread, _key = configured
    if point_seconds is not None:
        patch(host, {"points.curator.timeout_seconds": point_seconds})
    patch(host, {"timeout_seconds": 0.1, "stage_timeout_seconds": 0.05}, scope="thread", thread_id=thread.thread_id)
    legacy_thread(host, thread.thread_id, {"background_timeout_seconds": 99, "points.curator.timeout_seconds": 100})
    view = execute(host, "read", {}, thread_id=thread.thread_id)
    curator = view["effective"]["points"]["curator"]
    assert curator["timeout_seconds"] == (8 if point_seconds is None else point_seconds)
    assert (curator["max_request_seconds"], curator["limiting_field"]) == (expected, limiting_field)
    assert view["effective"]["background_timeout_seconds"] == 8
    assert view["sources"]["background_timeout_seconds"] == "owner"
    assert view["sources"]["points.curator.timeout_seconds"] == ("inherit:background_timeout_seconds:owner" if point_seconds is None else "owner")
    assert view["effective"]["points"]["recall"]["max_request_seconds"] == 0.05


def test_legacy_thread_background_overrides_are_readonly_until_explicit_reset(configured):
    host, thread, key = configured
    forbidden = {"background_timeout_seconds": 99, "points.curator.mode": "off",
                 "points.curator.timeout_seconds": 100, "points.curator.profile_id": ""}
    legacy_thread(host, thread.thread_id, {**forbidden, "timeout_seconds": 17})
    path = host.conversation_store.threads.storage.thread_path(thread.thread_id)
    original = path.read_bytes()
    before = execute(host, "read", {"scope": "thread"}, thread_id=thread.thread_id)
    assert path.read_bytes() == original
    assert before["overrides"]["thread"] == {**forbidden, "timeout_seconds": 17}
    assert before["effective"]["points"]["curator"]["profile_id"] == key
    result = execute(host, "reset", {"scope": "thread", "expected_revision": before["revision"],
                     "fields": list(forbidden)}, thread_id=thread.thread_id)
    assert result["overrides"]["thread"] == {"timeout_seconds": 17}
    assert result["effective"]["points"]["curator"] == before["effective"]["points"]["curator"]
    saved = host.conversation_store.threads.load(thread.thread_id)
    assert (saved.summary, saved.compact_generation) == ("保留原摘要", 7)
    assert result["revision"]["thread"] == before["revision"]["thread"] + 1
    with pytest.raises(DecisionSettingsConflict):
        execute(host, "reset", {"scope": "thread", "expected_revision": before["revision"],
                "fields": list(forbidden)}, thread_id=thread.thread_id)


def test_legacy_cleanup_still_compares_owner_revision(configured):
    host, thread, _key = configured
    legacy_thread(host, thread.thread_id, {"points.curator.mode": "off"})
    before = execute(host, "read", {"scope": "thread"}, thread_id=thread.thread_id)
    patch(host, {"points.curator.mode": "observe"})
    with pytest.raises(DecisionSettingsConflict):
        execute(host, "reset", {"scope": "thread", "expected_revision": before["revision"],
                "fields": ["points.curator.mode"]}, thread_id=thread.thread_id)
    current = execute(host, "read", {"scope": "thread"}, thread_id=thread.thread_id)
    result = execute(host, "reset", {"scope": "thread", "expected_revision": current["revision"],
                     "fields": ["points.curator.mode"]}, thread_id=thread.thread_id)
    assert not result["overrides"]["thread"]
    assert result["effective"]["points"]["curator"]["mode"] == "observe"


def test_point_runtime_scope_filters_stage_and_rejects_wrong_host_without_network(configured, monkeypatch):
    host, thread, _key = configured
    patch(host, {f"points.{point}.mode": "apply" for point in POINT_RUNTIME_SCOPES})
    front = SimpleNamespace(run_id="front", task_attributes={"conversation_thread_id": thread.thread_id})
    background = SimpleNamespace(run_id="background", task_attributes={})
    front_stage = service.begin_decision_stage(host, front, operation_id="front")
    background_stage = service.begin_decision_stage(host, background, operation_id="background", scope="owner_background")
    assert set(front_stage.enabled_points) == {point for point, scope in POINT_RUNTIME_SCOPES.items() if scope == "thread"}
    assert set(background_stage.enabled_points) == {point for point, scope in POINT_RUNTIME_SCOPES.items() if scope == "owner_background"}
    monkeypatch.setattr(calls, "invoke_decision_model_call", lambda *_a, **_kw: pytest.fail("范围错误不得发送请求"))
    for point, runtime_scope in POINT_RUNTIME_SCOPES.items():
        params, stage = (front, front_stage) if runtime_scope == "owner_background" else (background, background_stage)
        outcome = service.decide(host, params, stage, point=point, state={}, questions=questions(), candidates_revision="scope-test")
        assert (outcome.status, outcome.reason, outcome.may_apply) == ("error", "invalid_input", False)
    assert not hasattr(host, "_model_call_ledger")


def test_curator_original_service_uses_same_background_cap_as_projection(configured, monkeypatch):
    host, _thread, _key = configured
    patch(host, {"points.curator.timeout_seconds": 12})
    params = SimpleNamespace(run_id="background", task_attributes={})
    monkeypatch.setattr(service, "time", SimpleNamespace(monotonic=lambda: 100.0))
    captured = []

    def invoke(*args, **kwargs):
        captured.append(kwargs["deadline"])
        return successful(*args, **kwargs)

    monkeypatch.setattr(calls, "invoke_decision_model_call", invoke)
    view = execute(host, "read", {})
    stage = service.begin_decision_stage(host, params, operation_id="background", scope="owner_background")
    outcome = service.decide(host, params, stage, point="curator", state={}, questions=questions(), candidates_revision="scope-test")
    assert outcome.status == "success" and outcome.may_apply
    assert captured == [100.0 + view["effective"]["points"]["curator"]["max_request_seconds"]] == [108.0]
