"""Exercise learned-skill orphan recovery through the business HTTP contract."""

import pytest

from agent_py_agent.agent.business_spaces import v13_w11_skills_learning as business
from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
from agent_py_agent.tests.test_conveyor_m43_skill_lifecycle_authority import _reservation, _skill
from agent_py_agent.tests.test_nw37_r3_v13_compensation_and_archive import (
    _http_business,
    _skill_markdown,
)
from agent_py_agent.tests.test_nw37_r3_v13_compensation_and_archive import r3_native as r3_native
from agent_py_agent.tests.test_sm_w2_usage_strikes import _publish


@pytest.mark.parametrize("fresh_action", ["skills.activate", "skills.retire"])
def test_learned_toggle_orphan_reconciles_with_exact_current_version(r3_native, monkeypatch, fresh_action):
    server, agent = r3_native
    name = "learned-orphan"
    store = SkillLearningStore.for_home(agent.home_paths)
    _publish(store, name)
    v1 = store.load_registry().skills[name]
    original_version = _skill(server, name)["version_ref"]
    params = {"skill_id": name}
    old_key = "learned-retire-old-key"

    def stop_after_prepare(*args, **kwargs):
        raise OSError("interrupted after lifecycle preparation")

    with monkeypatch.context() as fault:
        fault.setattr(business, "_toggle_staged", stop_after_prepare)
        status, reply = _http_business(server, "skills.retire", params, idempotency_key=old_key)
        assert status == 503, reply
    orphan = _reservation(agent, name)
    assert orphan is not None

    _publish(store, name, suffix="v2", update_from=v1)
    current = _skill(server, name)
    assert current["version_ref"] != original_version and current["enabled"]
    native = store.skill_path(name)
    before = native.read_bytes()
    sidecar = native.with_name("skill_meta.json")
    sidecar_before = sidecar.read_bytes() if sidecar.exists() else None
    for suffix, ref in (("missing", {}), ("stale", {"expected_version_ref": original_version})):
        status, reply = _http_business(server, fresh_action, {**params, **ref},
                                       idempotency_key=f"learned-toggle-{suffix}")
        assert status == 503 and reply["error"]["code"] == "OUTCOME_UNKNOWN", reply
        assert _reservation(agent, name) == orphan
        assert native.read_bytes() == before

    status, reply = _http_business(server, fresh_action, {
        **params, "expected_version_ref": current["version_ref"],
    }, idempotency_key="learned-toggle-current")
    assert status == 200, reply
    assert reply["data"]["skill"]["enabled"] is (fresh_action == "skills.activate")
    assert _reservation(agent, name) is None
    if fresh_action == "skills.activate":
        assert (sidecar.read_bytes() if sidecar.exists() else None) == sidecar_before
    status, reply = _http_business(server, fresh_action, {
        **params, "expected_version_ref": original_version,
    }, idempotency_key="learned-toggle-stale-without-orphan")
    assert status == 409 and reply["error"]["code"] == "REVISION_CONFLICT", reply
    assert _reservation(agent, name) is None
    status, reply = _http_business(server, "skills.retire", params, idempotency_key=old_key)
    assert status == 409 and reply["effect_outcome"] == "unknown", reply
    status, reply = _http_business(server, "skills.update", {
        **params, "expected_version_ref": _skill(server, name)["version_ref"],
        "body_markdown": _skill_markdown(name, "unsanctioned body update"),
    }, idempotency_key="learned-body-update")
    assert status == 409, reply
    assert native.read_bytes() == before


@pytest.mark.parametrize("action", ["skills.activate", "skills.retire"])
def test_guarded_toggle_checks_native_image_after_preparation(r3_native, monkeypatch, action):
    server, agent = r3_native
    name = "learned-native-cas"
    store = SkillLearningStore.for_home(agent.home_paths)
    _publish(store, name)
    original = store.load_registry().skills[name]
    params = {"skill_id": name, "expected_version_ref": _skill(server, name)["version_ref"]}
    key = "learned-toggle-native-cas"
    stage = business._toggle_staged

    def publish_after_prepare(*args, **kwargs):
        assert _reservation(agent, name) is not None
        _publish(store, name, suffix="v2", update_from=original)
        return stage(*args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(business, "_toggle_staged", publish_after_prepare)
        status, refused = _http_business(server, action, params, idempotency_key=key)
    assert status == 409 and refused["effect_outcome"] == "not_started", refused
    assert _reservation(agent, name) is None
    current = _skill(server, name)
    assert current["version_ref"] != params["expected_version_ref"] and current["enabled"]
    status, replay = _http_business(server, action, params, idempotency_key=key)
    assert status == 409 and replay["effect_outcome"] == "not_started", replay
    assert refused["error"]["code"] == replay["error"]["code"] == "REVISION_CONFLICT"
    details = [{entry["key"]: entry["value"] for entry in result["error"]["details"]}
               for result in (refused, replay)]
    assert details[0] == details[1]
    assert details[0]["reason"] == "skill_mutation_cas_refused"
