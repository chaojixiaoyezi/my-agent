from dataclasses import replace

import pytest

from agent_py_agent.agent.business_spaces import v13_w11_skills_learning as business
from agent_py_agent.agent.business_spaces.v13_store import V13Store
from agent_py_agent.agent.capability.skill_learning_maintenance import control_learning
from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStoreError
from agent_py_agent.tests.test_nw37_r3_v13_compensation_and_archive import (
    _http_business,
    _skill_markdown,
)
from agent_py_agent.tests.test_nw37_r3_v13_compensation_and_archive import r3_native as r3_native
from agent_py_agent.tests.test_sm_w2_usage_strikes import (
    _publish,
    _record_real_use,
    _store_with_runtime,
)


@pytest.mark.parametrize("state", ["cooled", "archived"])
def test_stale_receipt_is_lifetime_only(tmp_path, state):
    _, _, store = _store_with_runtime(tmp_path)
    _publish(store, "target")
    before = store.load_registry().skills["target"]
    with store.state_lock():
        registry = store.load_registry()
        if state == "archived":
            store._transition_archive_locked(registry, "target")
            registry = store.load_registry()
        current = registry.skills["target"]
        current = replace(current, maintenance={**current.maintenance, "state": state, "activation_id": "new"})
        store.save_registry(replace(registry, skills={**registry.skills, "target": current}))

    _record_real_use(store, before, task_id="stale")
    assert store.load_registry().skills["target"] == current
    assert '"task_id":"stale"' in (store.directory / "usage_ledger.jsonl").read_text().replace(" ", "")


def _at_capacity(tmp_path):
    _, _, store = _store_with_runtime(tmp_path)
    _publish(store, "target")
    with store.state_lock():
        store._transition_archive_locked(store.load_registry(), "target")
    _publish(store, "victim")
    return store


def test_modified_restore_target_leaves_victim_live(tmp_path):
    store = _at_capacity(tmp_path)
    path = store.archived_skill_path("target")
    path.write_text(path.read_text() + "\n改动\n", encoding="utf-8")
    with pytest.raises(SkillLearningStoreError) as failure:
        control_learning(store, "target", operation="restore", max_skills=1, protection_complete=True)
    assert failure.value.code == "SKILL_LEARNING_USER_MODIFIED"
    assert store.load_registry().skills["victim"].maintenance["state"] != "archived"


def test_update_resumes_after_native_write(r3_native, monkeypatch):
    server, _ = r3_native
    name = "journal-skill"
    path = {"skill_id": name, "body_markdown": _skill_markdown(name, "first")}
    status, created = _http_business(server, "skills.create", path, idempotency_key="m43-create-key-0001")
    assert status == 200, created
    params = {**path, "body_markdown": _skill_markdown(name, "second"),
              "expected_version_ref": created["data"]["skill"]["version_ref"]}
    original = V13Store.transaction

    def lose_receipt(self, collection, mutate, **kwargs):
        if collection == "skill_operations" and kwargs["idempotency_key"] == "m43-update-key-0001":
            raise OSError("simulated receipt interruption")
        return original(self, collection, mutate, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(V13Store, "transaction", lose_receipt)
        patch.setattr(business, "_has_durable_skill_receipt", lambda *args: None)
        status, reply = _http_business(server, "skills.update", params, idempotency_key="m43-update-key-0001")
        assert status == 503, reply
    status, reply = _http_business(server, "skills.update", params, idempotency_key="m43-update-key-0001")
    assert status == 200, reply.get("error")
    assert reply["data"]["skill"]["description"] == "second"


def test_exact_receipt_at_capacity_exchanges_one_slot(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.capability import skill_learning_protection
    from agent_py_agent.agent.capability.skill_usage_ledger import record_skill_usage_event
    from agent_py_agent.agent.settings.config import AgentConfig

    store = _at_capacity(tmp_path)
    target = store.load_registry().skills["target"]
    agent = SimpleNamespace(skill_learning_store=store, config=AgentConfig(self_learning_max_skills=1))
    monkeypatch.setattr(skill_learning_protection, "learning_protection_for_agent", lambda _: {
        "protected_ids": (), "protection_complete": True,
    })

    record_skill_usage_event(
        agent, event="manual_get", skill_id="owner:target", source="learned",
        task_id="exact", body_chars=240, content_sha256=target.sha256,
        incarnation_id=target.maintenance["incarnation_id"], activation_id=target.maintenance["activation_id"],
    )
    registry = store.load_registry()
    restored, victim = registry.skills["target"], registry.skills["victim"]
    assert restored.maintenance["state"] == "trial"
    assert victim.maintenance["state"] == "archived"
    assert victim.last_operation_id == restored.last_operation_id
    assert registry.pending_operation is None


def test_interrupted_exchange_recovers_both_records(tmp_path, monkeypatch):
    store = _at_capacity(tmp_path)
    move = store._move_dir_nofollow

    def interrupted(src_root, src_parts, dst_root, dst_parts):
        if src_parts == ("archived", "target"):
            raise OSError("simulated directory move interruption")
        return move(src_root, src_parts, dst_root, dst_parts)

    with monkeypatch.context() as patch:
        patch.setattr(store, "_move_dir_nofollow", interrupted)
        with pytest.raises(OSError, match="simulated directory move interruption"):
            control_learning(store, "target", operation="restore", max_skills=1, protection_complete=True)
    pending = store.load_registry()
    assert pending.pending_operation["kind"] == "displace_restore"
    assert pending.skills["victim"].maintenance["state"] == "trial"
    assert store.recover_lifecycle()["completed"] is True
    final = store.load_registry()
    assert final.pending_operation is None
    assert final.skills["target"].maintenance["state"] == "trial"
    assert final.skills["victim"].maintenance["state"] == "archived"
    assert store.recover_lifecycle()["status"] == "idle"


@pytest.mark.parametrize("action", ["skills.create", "skills.retire"])
def test_prepared_create_and_toggle_resume_without_second_native_write(r3_native, monkeypatch, action):
    server, _ = r3_native
    name = "resume-skill"
    params = {"skill_id": name}
    if action == "skills.retire":
        status, result = _http_business(server, "skills.create", {
            **params, "body_markdown": _skill_markdown(name, "first"),
        }, idempotency_key="m43-setup-key-0001")
        assert status == 200, result
    else:
        params["body_markdown"] = _skill_markdown(name, "first")

    original = V13Store.transaction
    evidence = []

    def lose_receipt(self, collection, mutate, **kwargs):
        if collection == "skill_operations" and kwargs["idempotency_key"] == "m43-interrupted-key-0001":
            metadata = self._read(collection).get("metadata", {})
            evidence.append(metadata.get("skill-mutation:" + name))
            raise OSError("simulated receipt interruption")
        return original(self, collection, mutate, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(V13Store, "transaction", lose_receipt)
        patch.setattr(business, "_has_durable_skill_receipt", lambda *args: None)
        status, reply = _http_business(server, action, params, idempotency_key="m43-interrupted-key-0001")
        assert status == 503, reply
    assert len(evidence) == 1 and evidence[0]["state"] == "prepared"
    assert evidence[0]["original_sha"] != evidence[0]["planned_sha"]

    def unexpected_write(*args, **kwargs):
        pytest.fail("prepared replay attempted a second native write")

    monkeypatch.setattr(business, "_write_skill_staged" if action == "skills.create" else "_toggle_staged", unexpected_write)
    status, reply = _http_business(server, action, params, idempotency_key="m43-interrupted-key-0001")
    assert status == 200, reply
    assert reply["data"]["skill"]["enabled"] is (action == "skills.create")


def test_expired_restore_refusal_is_on_usage_receipt(tmp_path):
    import json
    from types import SimpleNamespace

    from agent_py_agent.agent.capability.skill_usage_ledger import record_skill_usage_event
    from agent_py_agent.agent.settings.config import AgentConfig

    _, _, store = _store_with_runtime(tmp_path)
    _publish(store, "target")
    with store.state_lock():
        _, target = store._transition_archive_locked(store.load_registry(), "target", now=1.0)
    agent = SimpleNamespace(skill_learning_store=store, config=AgentConfig(self_learning_evict_seconds=1))
    record_skill_usage_event(
        agent, event="manual_get", skill_id="owner:target", source="learned",
        task_id="expired", body_chars=240, content_sha256=target.sha256,
        incarnation_id=target.maintenance["incarnation_id"], activation_id=target.maintenance["activation_id"],
    )
    assert store.load_registry().skills["target"] == target
    rows = [json.loads(line) for line in (store.directory / "usage_ledger.jsonl").read_text().splitlines()]
    assert rows[-1]["restore_refusal"] == "SKILL_LEARNING_ARCHIVE_EXPIRED"


@pytest.mark.parametrize("unrecognized_change", [False, True])
def test_prepared_update_rechecks_original_and_planned_body(r3_native, monkeypatch, unrecognized_change):
    server, agent = r3_native
    name = "resume-update"
    original_body = _skill_markdown(name, "first")
    status, created = _http_business(server, "skills.create", {
        "skill_id": name, "body_markdown": original_body,
    }, idempotency_key="m43-seed-update-0001")
    assert status == 200, created
    params = {"skill_id": name, "body_markdown": _skill_markdown(name, "second"),
              "expected_version_ref": created["data"]["skill"]["version_ref"]}
    key = "m43-resume-update-0001"

    def stop_before_write(*args, **kwargs):
        raise OSError("simulated interruption after preparation")

    with monkeypatch.context() as patch:
        patch.setattr(business, "_write_skill_staged", stop_before_write)
        status, reply = _http_business(server, "skills.update", params, idempotency_key=key)
        assert status == 503, reply
    path = agent.home_paths.owner_home_dir / "skills" / name / "SKILL.md"
    assert path.read_text() == original_body
    if unrecognized_change:
        third_body = _skill_markdown(name, "external")
        path.write_text(third_body, encoding="utf-8")
    status, reply = _http_business(server, "skills.update", params, idempotency_key=key)
    if unrecognized_change:
        assert reply["error"]["code"] == "REVISION_CONFLICT"
        assert reply["effect_outcome"] == "unknown"
        assert "attribution" in str(reply["error"])
        assert path.read_text() == third_body
    else:
        assert status == 200, reply
        assert reply["data"]["skill"]["description"] == "second"


def test_equal_create_body_under_new_key_has_no_mutation_receipt(r3_native):
    server, _ = r3_native
    name = "same-body"
    params = {"skill_id": name, "body_markdown": _skill_markdown(name, "existing")}
    key = "m43-original-create-key"
    status, created = _http_business(server, "skills.create", params, idempotency_key=key)
    assert status == 200, created
    status, reply = _http_business(server, "skills.create", params, idempotency_key="m43-independent-key")
    assert status == 409 and reply["error"]["code"] == "STATE_CONFLICT"
    status, replay = _http_business(server, "skills.create", params, idempotency_key=key)
    assert status == 200 and replay["data"] == created["data"]
