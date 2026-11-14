"""质量确立只认版本绑定的真实使用，目录缺失与读取失败分别收敛。"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability import skill_learning_maintenance as maintenance
from agent_py_agent.agent.capability.skill_learning_host_feedback import (
    observe_command_verification,
)
from agent_py_agent.tests.test_sm_w2_usage_strikes import (
    _publish,
    _record_real_use,
    _store_with_runtime,
)


def _source(record, *, authority, receipt_id, run_id=""):
    return maintenance.LearningObservationSource(
        authority=authority,
        actor_id="reviewer" if authority == "human" else "",
        receipt_id=receipt_id,
        run_id=run_id,
        incarnation_id=record.maintenance["incarnation_id"],
        activation_id=record.maintenance["activation_id"],
        strike_epoch=record.maintenance["strike_epoch"],
    )


def _adopt(store, record, task_id, run_id):
    maintenance.observe_learning(
        store, record.name, sha256=record.sha256, task_id=task_id,
        outcome="adopted", source=_source(record, authority="model", receipt_id="", run_id=run_id),
    )


def _two_verified_uses(home, repo, store, authority):
    for number in (1, 2):
        record = store.load_registry().skills["alpha"]
        task_id, run_id = f"use-{number}", f"run-{number}"
        _record_real_use(store, record, task_id=task_id)
        _adopt(store, record, task_id, run_id)
        if authority == "host":
            params = SimpleNamespace(
                run_id=run_id, attempt_id="attempt", task_id=task_id,
                request_id=f"request-{number}", task_attributes={},
            )
            agent = SimpleNamespace(
                config=SimpleNamespace(enable_self_learning=True), home_paths=home,
                runtime_db=repo, _current_run_params=params,
            )
            assert observe_command_verification(
                agent, params, {"id": number, "status": "passed", "exit_code": 0},
            ) == []
        else:
            maintenance.observe_learning(
                store, record.name, sha256=record.sha256, task_id=task_id,
                outcome="effective", source=_source(
                    record, authority="human", receipt_id=f"review-{number}", run_id=run_id,
                ),
            )
    return store.load_registry().skills["alpha"]


def _three_strikes(store):
    result = None
    for number in (1, 2, 3):
        record = store.load_registry().skills["alpha"]
        task_id = f"failure-{number}"
        _record_real_use(store, record, task_id=task_id)
        result = maintenance.observe_learning(
            store, record.name, sha256=record.sha256, task_id=task_id,
            outcome="ineffective", source=_source(
                record, authority="host", receipt_id=f"negative-{number}",
            ),
        )
        if number < 3:
            current = store.load_registry().skills["alpha"]
            assert current.maintenance["hurt_count"] == number
            assert current.maintenance["quality"] == "trial"
            assert current.maintenance["uses_since_hurt"] == 0
    return result


@pytest.mark.parametrize("authority", ["host", "human"])
def test_two_real_uses_and_positive_observations_remain_trial(tmp_path, authority):
    home, repo, store = _store_with_runtime(tmp_path)
    _publish(store, "alpha")
    record = _two_verified_uses(home, repo, store, authority)
    data = record.maintenance
    assert data["quality"] == "trial"
    assert data["state"] == "trial"
    assert data["uses_since_hurt"] == data["helped_count"] == 2
    assert "established_at" not in data
    assert data["last_used_at"] > 0
    facts = [row for row in data["observations"] if row["authority"] == authority]
    assert len(facts) == 2
    assert {row["task_id"] for row in facts} == {"use-1", "use-2"}
    assert all(row["outcome"] == "effective" and row["sha256"] == record.sha256 for row in facts)


@pytest.mark.parametrize("authority", ["host", "human"])
def test_update_before_third_use_has_no_previous_stable(tmp_path, authority):
    home, repo, store = _store_with_runtime(tmp_path)
    _publish(store, "alpha")
    v1 = _two_verified_uses(home, repo, store, authority)
    _publish(store, "alpha", suffix="v2", update_from=v1)
    v2 = store.load_registry().skills["alpha"]
    assert v2.sha256 != v1.sha256
    assert v2.maintenance["quality"] == "trial"
    assert v2.maintenance.get("previous_stable") is None


@pytest.mark.parametrize("authority", ["host", "human"])
def test_third_strike_retires_instead_of_restoring_unproven_v1(tmp_path, authority):
    home, repo, store = _store_with_runtime(tmp_path)
    _publish(store, "alpha")
    v1 = _two_verified_uses(home, repo, store, authority)
    _publish(store, "alpha", suffix="v2", update_from=v1)
    result = _three_strikes(store)
    assert result["action"] == "retired"
    assert result["state"] == "retired" and result["hurt_count"] == 3
    registry = store.load_registry()
    assert "alpha" not in registry.skills
    assert "alpha" in registry.blocked_names
    assert v1.maintenance["task_family_fingerprint"] in registry.blocked_task_families
    assert not (store.learned_root / "alpha" / "SKILL.md").exists()


@pytest.mark.parametrize("authority", ["host", "human"])
def test_third_real_use_establishes_and_supplies_proven_rollback(tmp_path, authority):
    home, repo, store = _store_with_runtime(tmp_path)
    _publish(store, "alpha")
    v1 = _two_verified_uses(home, repo, store, authority)
    _record_real_use(store, v1, task_id="use-3")
    established = store.load_registry().skills["alpha"]
    assert established.maintenance["quality"] == "established"
    assert established.maintenance["quality_basis"] == "uses_threshold"
    assert established.maintenance["uses_since_hurt"] == 3
    assert established.maintenance["established_at"] > 0
    v1_body = store.skill_path("alpha").read_bytes()
    _publish(store, "alpha", suffix="v2", update_from=established)
    v2 = store.load_registry().skills["alpha"]
    assert v2.maintenance["quality"] == "trial"
    assert v2.maintenance["previous_stable"] == {
        "version": established.version, "revision": established.revision,
        "sha256": established.sha256, "established_at": established.maintenance["established_at"],
    }
    assert _three_strikes(store)["action"] == "rolled_back"
    registry = store.load_registry()
    rolled = registry.skills["alpha"]
    assert rolled.sha256 == established.sha256
    assert rolled.maintenance["quality"] == "established"
    assert rolled.maintenance["quality_basis"] == "rollback_established"
    assert rolled.maintenance["activation_id"] != v2.maintenance["activation_id"]
    assert "alpha" not in registry.blocked_names
    assert store.skill_path("alpha").read_bytes() == v1_body


@pytest.mark.parametrize("hurt_count", [1, 2])
def test_positive_observations_do_not_shorten_four_or_five_use_epoch(tmp_path, hurt_count):
    _, _, store = _store_with_runtime(tmp_path)
    _publish(store, "alpha")
    initial = store.load_registry().skills["alpha"]
    for number in (1, 2):
        _record_real_use(store, initial, task_id=f"initial-{number}")
    for number in range(1, hurt_count + 1):
        record = store.load_registry().skills["alpha"]
        task_id = f"negative-{number}"
        _adopt(store, record, task_id, f"negative-run-{number}")
        maintenance.observe_learning(
            store, record.name, sha256=record.sha256, task_id=task_id,
            outcome="ineffective", source=_source(record, authority="host", receipt_id=task_id),
        )
        data = store.load_registry().skills["alpha"].maintenance
        assert data["hurt_count"] == data["strike_epoch"] == number
        assert data["uses_since_hurt"] == 0
    required = 3 + hurt_count
    for number in range(1, required):
        record = store.load_registry().skills["alpha"]
        task_id = f"fresh-{number}"
        _record_real_use(store, record, task_id=task_id)
        _adopt(store, record, task_id, f"fresh-run-{number}")
        maintenance.observe_learning(
            store, record.name, sha256=record.sha256, task_id=task_id,
            outcome="effective", source=_source(record, authority="human", receipt_id=task_id),
        )
        data = store.load_registry().skills["alpha"].maintenance
        assert data["quality"] == "trial"
        assert data["uses_since_hurt"] == number
        assert data["hurt_count"] == hurt_count
    _record_real_use(store, store.load_registry().skills["alpha"], task_id="final-use")
    data = store.load_registry().skills["alpha"].maintenance
    assert data["quality"] == "established" and data["quality_basis"] == "uses_threshold"
    assert data["uses_since_hurt"] == required and data["hurt_count"] == hurt_count


@pytest.mark.parametrize("archived", [False, True])
@pytest.mark.parametrize("empty", [False, True], ids=["absent", "empty"])
def test_missing_managed_directory_is_user_modified(tmp_path, archived, empty):
    _, _, store = _store_with_runtime(tmp_path)
    _publish(store, "alpha")
    if archived:
        with store.state_lock():
            store._transition_archive_locked(store.load_registry(), "alpha", now=100.0)
    record = store.load_registry().skills["alpha"]
    path = store.directory / "archived" / "alpha" / "SKILL.md" if archived else store.skill_path("alpha")
    path.unlink()
    if not empty:
        path.parent.rmdir()
    assert maintenance._verify_single_ownership(store, record) == "user_modified"


@pytest.mark.parametrize("empty", [False, True], ids=["absent", "empty"])
def test_maintenance_releases_deleted_directory_slot_and_preserves_history(tmp_path, empty):
    _, _, store = _store_with_runtime(tmp_path)
    _publish(store, "alpha")
    record = store.load_registry().skills["alpha"]
    version_body = store.read_version("alpha", record.revision)
    path = store.skill_path("alpha")
    path.unlink()
    if not empty:
        path.parent.rmdir()
    report = maintenance.maintain_learning(
        store, mode="autonomous", protection_complete=True, batch_count=100, batch_ms=5000,
    )
    assert report["backlog"] is False
    registry = store.load_registry()
    handed = registry.skills["alpha"]
    assert handed.maintenance["ownership"] == "user_modified"
    assert handed.sha256 == record.sha256 and handed.revision == record.revision
    assert store.read_version("alpha", record.revision) == version_body
    assert store.managed_records(registry) == {}
    assert registry.live_count(managed_names=store.managed_records(registry)) == 0
    assert not path.exists()


def test_directory_read_error_remains_unknown(tmp_path, monkeypatch):
    _, _, store = _store_with_runtime(tmp_path)
    _publish(store, "alpha")
    record = store.load_registry().skills["alpha"]

    def unavailable(*args):
        raise OSError("directory unavailable")

    monkeypatch.setattr(maintenance, "list_names_beneath", unavailable)
    assert maintenance._has_extra_dir_entries(store.skills_root, ("learned", "alpha")) == (False, True)
    assert maintenance._verify_single_ownership(store, record) == "unknown"


def test_directory_link_error_remains_unknown(tmp_path):
    _, _, store = _store_with_runtime(tmp_path)
    _publish(store, "alpha")
    record = store.load_registry().skills["alpha"]
    path = store.skill_path("alpha")
    path.unlink()
    path.parent.rmdir()
    destination = tmp_path / "linked-directory"
    destination.mkdir()
    path.parent.symlink_to(destination, target_is_directory=True)
    assert maintenance._has_extra_dir_entries(store.skills_root, ("learned", "alpha")) == (False, True)
    assert maintenance._verify_single_ownership(store, record) == "unknown"


@pytest.mark.parametrize("ownership", ["user_modified", "owner_layout"])
def test_ownership_handoff_is_terminal_without_directory_probe(tmp_path, monkeypatch, ownership):
    _, _, store = _store_with_runtime(tmp_path)
    _publish(store, "alpha")
    original = store.load_registry().skills["alpha"]
    record = replace(original, maintenance={**original.maintenance, "ownership": ownership})

    def unexpected(*args):
        raise AssertionError("terminal ownership must not probe the directory")

    monkeypatch.setattr(maintenance, "_has_extra_dir_entries", unexpected)
    assert maintenance._verify_single_ownership(store, record) == ownership
