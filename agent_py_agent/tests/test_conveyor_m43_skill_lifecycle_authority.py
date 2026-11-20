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


def test_equal_create_body_under_new_key_is_a_no_write_success(r3_native):
    server, agent = r3_native
    name = "same-body"
    params = {"skill_id": name, "body_markdown": _skill_markdown(name, "existing")}
    key = "m43-original-create-key"
    status, created = _http_business(server, "skills.create", params, idempotency_key=key)
    assert status == 200, created
    path = agent.home_paths.owner_home_dir / "skills" / name / "SKILL.md"
    before = path.stat()
    status, reply = _http_business(server, "skills.create", params, idempotency_key="m43-independent-key")
    assert status == 200, reply
    assert reply["data"]["skill"]["version_ref"] == created["data"]["skill"]["version_ref"]
    after = path.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert _reservation(agent, name) is None
    changed = {**params, "body_markdown": _skill_markdown(name, "changed")}
    status, reply = _http_business(server, "skills.create", changed, idempotency_key="m43-changed-body-key")
    assert status == 409, reply["error"]
    assert reply["error"]["code"] == "STATE_CONFLICT"
    assert _reservation(agent, name) is None and path.read_text() == params["body_markdown"]
    status, replay = _http_business(server, "skills.create", params, idempotency_key=key)
    assert status == 200 and replay["data"] == created["data"]


def _skill(server, name):
    status, reply = _http_business(server, "skills.get", {"skill_id": name})
    assert status == 200, reply
    return reply["data"]["skill"]


def _reservation(agent, name):
    state = V13Store.read_only(agent.home_paths.owner_home_dir)._read("skill_operations")
    return state.get("metadata", {}).get("skill-mutation:" + name)


# 在准备行已持久化、原生写入之前改动同一个技能，模拟所有者的并发编辑。
def _edit_before_native_write(monkeypatch, agent, name, staged_name, body):
    staged = getattr(business, staged_name)
    path = agent.home_paths.owner_home_dir / "skills" / name / "SKILL.md"

    def edit_then_stage(*args, **kwargs):
        assert _reservation(agent, name) is not None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return staged(*args, **kwargs)

    monkeypatch.setattr(business, staged_name, edit_then_stage)
    return path


@pytest.mark.parametrize("action", ["skills.create", "skills.update", "skills.activate", "skills.retire"])
def test_known_pre_write_cas_refusal_settles_the_original_key(r3_native, monkeypatch, action):
    server, agent = r3_native
    name = "cas-" + action.split(".")[1]
    params = {"skill_id": name}
    if action == "skills.create":
        params["body_markdown"] = _skill_markdown(name, "first")
    else:
        status, created = _http_business(server, "skills.create", {
            "skill_id": name, "body_markdown": _skill_markdown(name, "first"),
        }, idempotency_key="m43-cas-seed-0001")
        assert status == 200, created
        if action == "skills.update":
            params.update(body_markdown=_skill_markdown(name, "second"),
                          expected_version_ref=created["data"]["skill"]["version_ref"])
        if action == "skills.activate":
            status, retired = _http_business(server, "skills.retire", params, idempotency_key="m43-cas-retire-0001")
            assert status == 200 and retired["data"]["skill"]["enabled"] is False, retired
    owner_edit = _skill_markdown(name, "owner edit")
    staged_name = "_write_skill_staged" if action in {"skills.create", "skills.update"} else "_toggle_staged"
    key = "m43-cas-refused-0001"
    with monkeypatch.context() as patch:
        path = _edit_before_native_write(patch, agent, name, staged_name, owner_edit)
        status, refused = _http_business(server, action, params, idempotency_key=key)
    assert status == 409 and refused["error"]["code"] == "REVISION_CONFLICT", refused
    assert refused["effect_outcome"] == "not_started" and "skill_mutation_cas_refused" in str(refused["error"])
    assert path.read_text() == owner_edit and _reservation(agent, name) is None

    # 原键是终态：重放返回同一结论且不再写入；同键换参数仍是幂等冲突。
    status, replay = _http_business(server, action, params, idempotency_key=key)
    assert status == 409 and replay["effect_outcome"] == "not_started", replay
    assert replay["error"]["code"] == "REVISION_CONFLICT" and "skill_mutation_cas_refused" in str(replay["error"])
    status, changed = _http_business(server, action, {**params, "skill_id": "cas-other"}, idempotency_key=key)
    assert status == 409 and changed["error"]["code"] == "IDEMPOTENCY_CONFLICT", changed
    assert path.read_text() == owner_edit

    current = _skill(server, name)
    if action == "skills.update":
        status, stale = _http_business(server, action, params, idempotency_key="m43-cas-stale-0001")
        assert status == 409 and stale["error"]["code"] == "REVISION_CONFLICT", stale
        assert stale["effect_outcome"] == "not_started" and path.read_text() == owner_edit
    status, fresh = _http_business(server, "skills.update", {
        "skill_id": name, "body_markdown": _skill_markdown(name, "fresh"), "expected_version_ref": current["version_ref"],
    }, idempotency_key="m43-cas-fresh-update-0001")
    assert status == 200 and fresh["data"]["skill"]["description"] == "fresh", fresh
    status, retired = _http_business(server, "skills.retire", {"skill_id": name}, idempotency_key="m43-cas-fresh-retire-0001")
    assert status == 200 and retired["data"]["skill"]["enabled"] is False, retired


def test_settlement_committed_before_materialization_replays_after_recovery(r3_native, monkeypatch):
    import errno
    import json

    from agent_py_agent.agent.business_spaces import v13_store as store_module

    server, agent = r3_native
    name = "cas-journal"
    status, created = _http_business(server, "skills.create", {
        "skill_id": name, "body_markdown": _skill_markdown(name, "first"),
    }, idempotency_key="m43-journal-seed-0001")
    assert status == 200, created
    params = {"skill_id": name, "body_markdown": _skill_markdown(name, "second"),
              "expected_version_ref": created["data"]["skill"]["version_ref"]}
    write = store_module.write_bytes_atomic_beneath
    fault = {"journal": False, "raised": False}

    def stop_after_commit_point(root, parts, raw, *args, **kwargs):
        if fault["journal"] and not fault["raised"]:
            fault["raised"] = True
            raise OSError(errno.EIO, "simulated I/O error after the journal commit")
        if parts[-1] == ".transaction.json" and raw and not fault["raised"]:
            facts = json.loads(raw).get("states", {}).get("skill_operations", {}).get("metadata_facts", {})
            fault["journal"] = any(item.startswith("skill-mutation-settled:") for item in facts)
        return write(root, parts, raw, *args, **kwargs)

    key = "m43-journal-refused-0001"
    with monkeypatch.context() as patch:
        path = _edit_before_native_write(patch, agent, name, "_write_skill_staged", _skill_markdown(name, "owner edit"))
        patch.setattr(store_module, "write_bytes_atomic_beneath", stop_after_commit_point)
        status, refused = _http_business(server, "skills.update", params, idempotency_key=key)
    assert fault == {"journal": True, "raised": True}
    assert status == 409 and refused["effect_outcome"] == "not_started", refused
    # 终态只在未物化日志里：新的存储句柄仍读到已释放的占位和原键终态。
    assert _reservation(agent, name) is None
    status, replay = _http_business(server, "skills.update", params, idempotency_key=key)
    assert status == 409 and "skill_mutation_cas_refused" in str(replay["error"]), replay
    status, fresh = _http_business(server, "skills.update", {
        **params, "expected_version_ref": _skill(server, name)["version_ref"],
    }, idempotency_key="m43-journal-fresh-0001")
    assert status == 200 and fresh["data"]["skill"]["description"] == "second", fresh
    status, replay = _http_business(server, "skills.update", params, idempotency_key=key)
    assert status == 409 and replay["effect_outcome"] == "not_started", replay
    assert path.read_text() == _skill_markdown(name, "second")


def test_uncertain_orphan_needs_current_version_authorization(r3_native, monkeypatch):
    server, agent = r3_native
    name = "orphan-skill"
    status, created = _http_business(server, "skills.create", {
        "skill_id": name, "body_markdown": _skill_markdown(name, "first"),
    }, idempotency_key="m43-orphan-seed-0001")
    assert status == 200, created
    original_version = created["data"]["skill"]["version_ref"]
    params = {"skill_id": name, "body_markdown": _skill_markdown(name, "second"), "expected_version_ref": original_version}
    old_key = "m43-orphan-update-0001"

    def stop_before_write(*args, **kwargs):
        raise OSError("simulated interruption after preparation")

    with monkeypatch.context() as patch:
        patch.setattr(business, "_write_skill_staged", stop_before_write)
        status, reply = _http_business(server, "skills.update", params, idempotency_key=old_key)
        assert status == 503, reply
    path = agent.home_paths.owner_home_dir / "skills" / name / "SKILL.md"
    third = _skill_markdown(name, "external")
    path.write_text(third, encoding="utf-8")
    orphan = _reservation(agent, name)

    # 第三种内容下旧键的历史效果未知：同键重放不清除占位，没有当前版本授权的新请求也不能越过它。
    status, reply = _http_business(server, "skills.update", params, idempotency_key=old_key)
    assert status == 409 and reply["effect_outcome"] == "unknown", reply
    status, reply = _http_business(server, "skills.retire", {"skill_id": name}, idempotency_key="m43-orphan-retire-0001")
    assert status == 503 and reply["error"]["code"] == "OUTCOME_UNKNOWN", reply
    status, reply = _http_business(server, "skills.update", params, idempotency_key="m43-orphan-stale-0001")
    assert status == 503 and reply["error"]["code"] == "OUTCOME_UNKNOWN", reply
    assert _reservation(agent, name) == orphan and path.read_text() == third

    # 其它技能不受这个占位影响。
    status, other = _http_business(server, "skills.create", {
        "skill_id": "independent-skill", "body_markdown": _skill_markdown("independent-skill", "first"),
    }, idempotency_key="m43-orphan-other-0001")
    assert status == 200, other

    current = _skill(server, name)["version_ref"]
    status, reconciled = _http_business(server, "skills.update", {
        **params, "body_markdown": _skill_markdown(name, "reconciled"), "expected_version_ref": current,
    }, idempotency_key="m43-orphan-current-0001")
    assert status == 200 and reconciled["data"]["skill"]["description"] == "reconciled", reconciled
    assert _reservation(agent, name) is None
    status, reply = _http_business(server, "skills.update", params, idempotency_key=old_key)
    assert status == 409 and reply["effect_outcome"] == "unknown" and "attribution" in str(reply["error"]), reply
    assert path.read_text() == _skill_markdown(name, "reconciled")
    status, retired = _http_business(server, "skills.retire", {"skill_id": name}, idempotency_key="m43-orphan-retire-0002")
    assert status == 200 and retired["data"]["skill"]["enabled"] is False, retired


def test_recoverable_prepared_entry_still_requires_the_original_key(r3_native, monkeypatch):
    server, agent = r3_native
    name = "recoverable-skill"
    status, created = _http_business(server, "skills.create", {
        "skill_id": name, "body_markdown": _skill_markdown(name, "first"),
    }, idempotency_key="m43-recover-seed-0001")
    assert status == 200, created
    params = {"skill_id": name, "body_markdown": _skill_markdown(name, "second"),
              "expected_version_ref": created["data"]["skill"]["version_ref"]}
    key = "m43-recover-update-0001"
    original = V13Store.transaction

    def lose_receipt(self, collection, mutate, **kwargs):
        if collection == "skill_operations" and kwargs["idempotency_key"] == key:
            raise OSError("simulated receipt interruption")
        return original(self, collection, mutate, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(V13Store, "transaction", lose_receipt)
        patch.setattr(business, "_has_durable_skill_receipt", lambda *args: None)
        status, reply = _http_business(server, "skills.update", params, idempotency_key=key)
        assert status == 503, reply
    # 原生写已经落盘、回执未提交：当前内容是计划像，旧键仍可恢复，所以带当前版本的新请求也不能结清它。
    status, reply = _http_business(server, "skills.update", {
        **params, "body_markdown": _skill_markdown(name, "third"), "expected_version_ref": _skill(server, name)["version_ref"],
    }, idempotency_key="m43-recover-fresh-0001")
    assert status == 503 and reply["error"]["code"] == "OUTCOME_UNKNOWN", reply
    status, reply = _http_business(server, "skills.update", params, idempotency_key=key)
    assert status == 200 and reply["data"]["skill"]["description"] == "second", reply
    assert _reservation(agent, name) is None
