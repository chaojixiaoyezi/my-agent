# LLM: 验证退役决策字段（skill_proposal_review 与实验配置）只做持久读时温和迁移，不原地改输入、不放宽写校验。
# 模块用途: 覆盖 F02 / P5-02 读时迁移、只读落盘边界、写门严格拒绝与全调用方协同。
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from types import MappingProxyType, SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.conversation.decision_selection_cadence import selection_cadence_from_read
from agent_py_agent.agent.conversation.models import ConversationThread
from agent_py_agent.agent.settings.decision_settings import execute_decision_settings_operation
from agent_py_agent.agent.settings.decision_settings_projection import (
    decision_point_mode_from_read,
    decision_settings_projection,
    decision_summary_mode_from_read,
)
from agent_py_agent.agent.settings.decision_settings_schema import (
    DECISION_SETTINGS_SCHEMA,
    decision_field_scopes,
    migrate_retired_decision_settings,
    thread_decision_settings,
    validate_decision_settings,
)
from agent_py_agent.agent.settings.model_profiles import model_profiles_path, read_model_profiles
from agent_py_agent.agent.settings.model_provider_schema import ModelProfileError
from agent_py_agent.agent.tooling.user_config_tool import UserConfigTool
from agent_py_agent.tests.test_decision_settings import host_at


def _legacy_decision_envelope(*, revision: int = 3, extra_overrides: dict | None = None) -> dict:
    overrides = {
        "enabled": True,
        "points.recall.mode": "observe",
        "points.model_selection.cadence": "structure_change",
        "points.skill_proposal_review": {"enabled": True},
        "points.skill_proposal_review.enabled": True,
        "points.skill_proposal_review.mode": "observe",
        "points.skill_proposal_review.model": "legacy-model",
        "points.skill_proposal_review.max_tokens": 512,
        "points.skill_proposal_review.timeout_ms": 2500,
        "experiment_enabled": True,
    }
    if extra_overrides:
        overrides.update(extra_overrides)
    return {
        "schema": DECISION_SETTINGS_SCHEMA,
        "revision": revision,
        "overrides": overrides,
        "experiment_authorization": {
            "schema": "decision_experiment_authorization.v2",
            "status": "active",
            "authorization_id": "auth-legacy-1",
        },
    }


def test_migrate_retired_decision_settings_does_not_mutate_input_and_handles_non_mappings():
    for sentinel in (None, 42, "raw-text", ["list-item"]):
        assert migrate_retired_decision_settings(sentinel) is sentinel

    original = _legacy_decision_envelope(revision=7)
    snapshot = deepcopy(original)
    migrated = migrate_retired_decision_settings(original)

    assert original == snapshot
    assert migrated is not original
    assert migrated["overrides"] is not original["overrides"]
    assert migrated["revision"] == 7
    assert migrated["experiment_authorization"] is None
    assert migrated["overrides"] == {
        "enabled": True,
        "points.recall.mode": "observe",
        "points.model_selection.cadence": "structure_change",
    }
    assert validate_decision_settings(migrated) == migrated


def test_migrate_retired_decision_settings_accepts_mapping_proxy_and_v1_records():
    proxy = MappingProxyType({
        "schema": "decision_settings.v1",
        "revision": 4,
        "overrides": MappingProxyType({
            "enabled": True,
            "experiment_enabled": False,
            "points.skill_proposal_review.mode": "apply",
        }),
    })
    migrated = migrate_retired_decision_settings(proxy)
    assert type(migrated) is dict
    assert type(migrated["overrides"]) is dict
    assert "experiment_authorization" not in migrated
    assert validate_decision_settings(migrated) == {
        "schema": DECISION_SETTINGS_SCHEMA,
        "revision": 4,
        "overrides": {"enabled": True},
        "experiment_authorization": None,
    }


def test_read_model_profiles_migrates_legacy_decision_settings_readonly_and_allows_later_patch(tmp_path):
    host = host_at(tmp_path)
    path = model_profiles_path(host.home_paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "schema": "owner_model_profiles.v6",
        "selected": "default",
        "providers": {},
        "profiles": {},
        "catalog_generation": uuid4().hex,
        "decision_settings": _legacy_decision_envelope(revision=5),
    }
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    before_bytes = path.read_bytes()

    loaded = read_model_profiles(path)
    assert loaded["decision_settings"] == {
        "schema": DECISION_SETTINGS_SCHEMA,
        "revision": 5,
        "overrides": {
            "enabled": True,
            "points.recall.mode": "observe",
            "points.model_selection.cadence": "structure_change",
        },
        "experiment_authorization": None,
    }
    effective = execute_decision_settings_operation(host, "read", {})
    assert effective["revision"] == {"owner": 5, "thread": 0}
    assert effective["effective"]["enabled"] is True
    assert effective["effective"]["points"]["recall"]["mode"] == "observe"
    assert effective["effective"]["points"]["model_selection"]["cadence"] == "structure_change"
    assert effective["experiment_authorization"] is None
    assert path.read_bytes() == before_bytes

    patched = execute_decision_settings_operation(
        host,
        "patch",
        {"expected_revision": effective["revision"], "changes": {"timeout_seconds": 6.0}},
    )
    assert patched["revision"] == {"owner": 6, "thread": 0}
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["decision_settings"]["experiment_authorization"] is None
    assert "experiment_enabled" not in persisted["decision_settings"]["overrides"]
    assert not any(
        key == "points.skill_proposal_review" or key.startswith("points.skill_proposal_review.")
        for key in persisted["decision_settings"]["overrides"]
    )
    assert persisted["decision_settings"]["overrides"]["timeout_seconds"] == 6.0


def test_read_model_profiles_still_rejects_unknown_non_retired_override_keys(tmp_path):
    host = host_at(tmp_path)
    path = model_profiles_path(host.home_paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "schema": "owner_model_profiles.v6",
        "selected": "default",
        "providers": {},
        "profiles": {},
        "catalog_generation": uuid4().hex,
        "decision_settings": _legacy_decision_envelope(
            revision=2,
            extra_overrides={"points.unregistered_point.mode": "observe"},
        ),
    }
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ModelProfileError):
        read_model_profiles(path)


def test_thread_decision_settings_store_load_and_thread_patch_migrate_legacy_keys(tmp_path):
    host = host_at(tmp_path)
    thread = host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})
    thread_path = host.conversation_store.threads.storage.thread_path(thread.thread_id)
    raw_thread = json.loads(thread_path.read_text(encoding="utf-8"))
    raw_thread["decision_settings"] = _legacy_decision_envelope(
        revision=2,
        extra_overrides={"points.planning.mode": "apply"},
    )
    thread_path.write_text(json.dumps(raw_thread, ensure_ascii=False), encoding="utf-8")
    before_bytes = thread_path.read_bytes()

    migrated = thread_decision_settings(raw_thread)
    assert migrated["revision"] == 2
    assert migrated["experiment_authorization"] is None
    assert migrated["overrides"] == {
        "enabled": True,
        "points.recall.mode": "observe",
        "points.model_selection.cadence": "structure_change",
        "points.planning.mode": "apply",
    }

    loaded_thread = ConversationThread.from_dict(raw_thread)
    assert loaded_thread.decision_settings == migrated
    stored_thread = host.conversation_store.threads.load(thread.thread_id)
    assert stored_thread.decision_settings == migrated
    assert thread_path.read_bytes() == before_bytes

    view = execute_decision_settings_operation(host, "read", {"scope": "thread"}, thread_id=thread.thread_id)
    assert view["revision"] == {"owner": 0, "thread": 2}
    assert view["effective"]["points"]["planning"]["effective_mode"] == "apply"
    assert view["experiment_authorization"] is None

    patched = execute_decision_settings_operation(
        host,
        "patch",
        {"scope": "thread", "expected_revision": view["revision"], "changes": {"timeout_seconds": 4.5}},
        thread_id=thread.thread_id,
    )
    assert patched["revision"] == {"owner": 0, "thread": 3}
    assert patched["effective"]["timeout_seconds"] == 4.5
    saved_thread = json.loads(thread_path.read_text(encoding="utf-8"))
    assert saved_thread["decision_settings"]["experiment_authorization"] is None
    assert "experiment_enabled" not in saved_thread["decision_settings"]["overrides"]


def test_update_thread_handles_in_memory_legacy_thread_snapshot(tmp_path, monkeypatch):
    host = host_at(tmp_path)
    thread = host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})
    legacy_thread = replace(thread, decision_settings=_legacy_decision_envelope(revision=3))
    saved_snapshots: list[ConversationThread] = []

    def fake_update_atomic(thread_id, updater):
        updated = updater(legacy_thread)
        saved_snapshots.append(updated)
        return updated

    monkeypatch.setattr(host.conversation_store.threads, "update_atomic", fake_update_atomic)
    result = execute_decision_settings_operation(
        host,
        "patch",
        {"scope": "thread", "expected_revision": {"owner": 0, "thread": 3}, "changes": {"timeout_seconds": 7.0}},
        thread_id=thread.thread_id,
    )
    assert result["revision"] == {"owner": 0, "thread": 4}
    assert saved_snapshots and saved_snapshots[0].decision_settings["experiment_authorization"] is None
    assert "experiment_enabled" not in saved_snapshots[0].decision_settings["overrides"]


def test_projection_point_mode_summary_and_selection_cadence_handle_legacy_inputs(tmp_path):
    host = host_at(tmp_path)
    owner_legacy = _legacy_decision_envelope(revision=1)
    thread_legacy = SimpleNamespace(
        thread_id="thread-legacy-1",
        decision_settings=_legacy_decision_envelope(
            revision=2,
            extra_overrides={"points.planning.mode": "apply", "points.model_selection.cadence": "every_turn"},
        ),
    )
    config = SimpleNamespace()

    projection = decision_settings_projection(
        host,
        {"decision_settings": owner_legacy},
        thread_legacy,
        scope="thread",
    )
    assert projection["revision"] == {"owner": 1, "thread": 2}
    assert projection["experiment_authorization"] is None
    assert projection["effective"]["points"]["planning"]["effective_mode"] == "apply"
    assert projection["effective"]["points"]["recall"]["effective_mode"] == "observe"

    assert decision_point_mode_from_read(config, owner_legacy, thread_legacy, point="planning") == "apply"
    assert decision_summary_mode_from_read(config, owner_legacy, thread_legacy) == "apply"
    assert selection_cadence_from_read(config, owner_legacy, thread_legacy) == "every_turn"
    assert selection_cadence_from_read(config, owner_legacy, None) == "structure_change"


def test_legacy_record_with_all_current_fields_plus_retired_keys_succeeds():
    full_current = {}
    for key in decision_field_scopes():
        if key in {"enabled", "observe_sampling_enabled", "observe_nonblocking_enabled"}:
            full_current[key] = True
        elif key.endswith("timeout_seconds"):
            full_current[key] = 2.0
        elif key.endswith("profile_id"):
            full_current[key] = ""
        elif key.endswith("candidate_profile_ids"):
            full_current[key] = []
        elif key.endswith("optional_categories"):
            full_current[key] = ["plugins"]
        elif key.endswith("context_policy"):
            full_current[key] = "progressive"
        elif key.endswith("cadence"):
            full_current[key] = "every_turn"
        elif key.endswith("fragment_material"):
            full_current[key] = "query_text"
        else:
            full_current[key] = "observe"

    bloated = _legacy_decision_envelope(revision=9, extra_overrides=full_current)
    assert len(bloated["overrides"]) > len(decision_field_scopes())
    with pytest.raises(ValueError):
        validate_decision_settings(bloated)
    validated = validate_decision_settings(migrate_retired_decision_settings(bloated))
    assert validated["overrides"] == full_current


@pytest.mark.parametrize("retired_key,retired_value", [
    ("experiment_enabled", True),
    ("points.skill_proposal_review", "observe"),
    ("points.skill_proposal_review.enabled", True),
    ("points.skill_proposal_review.mode", "observe"),
    ("points.skill_proposal_review.model", "legacy-model"),
    ("points.skill_proposal_review.max_tokens", 512),
    ("points.skill_proposal_review.timeout_ms", 2500),
])
def test_strict_write_validation_still_rejects_retired_override_keys(tmp_path, retired_key, retired_value):
    candidate = {
        "schema": DECISION_SETTINGS_SCHEMA,
        "revision": 1,
        "overrides": {retired_key: retired_value},
        "experiment_authorization": None,
    }
    with pytest.raises(ValueError):
        validate_decision_settings(candidate)

    host = host_at(tmp_path)
    revision = execute_decision_settings_operation(host, "read", {})["revision"]
    with pytest.raises(ValueError):
        execute_decision_settings_operation(
            host,
            "patch",
            {"expected_revision": revision, "changes": {retired_key: retired_value}},
        )
    with pytest.raises(ValueError):
        execute_decision_settings_operation(
            host,
            "reset",
            {"expected_revision": revision, "fields": [retired_key]},
        )

    tool = UserConfigTool(host)
    tool_result = tool.execute({
        "action": "decision_patch",
        "expected_revision": revision,
        "changes": {retired_key: retired_value},
    })
    assert not tool_result.ok


def test_strict_write_validation_still_rejects_non_null_experiment_authorization():
    candidate = {
        "schema": DECISION_SETTINGS_SCHEMA,
        "revision": 1,
        "overrides": {},
        "experiment_authorization": {"schema": "decision_experiment_authorization.v2", "status": "active"},
    }
    with pytest.raises(ValueError):
        validate_decision_settings(candidate)
