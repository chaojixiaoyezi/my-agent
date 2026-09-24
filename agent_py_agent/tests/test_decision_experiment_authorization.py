"""E1 授权原语：临时原设置/账本和宿主身份；不调用任何真实模型或 Gateway。"""
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.contracts.model_call_ledger import ModelCallLedger
from agent_py_agent.agent.conversation import decision_model_call, decision_service
from agent_py_agent.agent.runtime_context import (
    restore_current_subagent_context,
    set_current_subagent_context,
)
from agent_py_agent.agent.runtime_db.host_commands import HostCommandIdentity
from agent_py_agent.agent.settings.config import AgentConfig, load_config
from agent_py_agent.agent.settings.decision_experiment import authorize_decision_experiment
from agent_py_agent.agent.settings.decision_experiment_schema import (
    EMPIRICAL_INPUT_BOUND_POLICY,
    EXPERIMENT_SCHEMA,
    EXPERIMENT_SCHEMA_V1,
    validate_experiment_authorization,
)
from agent_py_agent.agent.settings.decision_settings import (
    execute_decision_settings_operation as execute,
)
from agent_py_agent.agent.settings.decision_settings_schema import (
    DecisionSettingsAccessError,
    DecisionSettingsConflict,
    validate_decision_settings,
)
from agent_py_agent.agent.settings.model_profiles import model_profiles_path
from agent_py_agent.agent.settings.model_provider_schema import ModelProfileError
from agent_py_agent.agent.tooling.user_config_tool import UserConfigTool
from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call
from agent_py_agent.tests.test_decision_settings import host_at, patch


@pytest.fixture
def running(tmp_path):
    host = host_at(tmp_path)
    thread = host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})
    params = SimpleNamespace(thread_id=thread.thread_id, run_id="run-1", task_id="task-1", attempt_id="attempt-1",
                             request_id="message-1", task_attributes={"agent_thread_id": thread.thread_id, "task_id": "task-1"})
    prior = set_current_subagent_context(host, run_id=params.run_id, attempt_id=params.attempt_id, task_attributes=params.task_attributes)
    try:
        yield host, thread, params
    finally:
        restore_current_subagent_context(host, prior)


def _grant(running, **changes):
    host, thread, params = running
    kwargs = {"source": HostCommandIdentity("alice", "alice", "host_control", thread.thread_id, "authorize-1"),
              "expected_revision": execute(host, "read", {}, thread_id=thread.thread_id)["revision"],
              "points": ["planning"], "duration_seconds": 30, "max_http_requests": 2, "max_input_tokens": 100,
              "input_bound_policy": EMPIRICAL_INPUT_BOUND_POLICY}
    kwargs.update(changes)
    return authorize_decision_experiment(host, params, **kwargs)


def _enabled(running):
    host, thread, _params = running
    return patch(host, {"enabled": True, "experiment_enabled": True, "points.planning.mode": "observe"}, thread_id=thread.thread_id)


def _stage(running):
    host, _thread, params = running
    return decision_service.begin_decision_stage(host, params, operation_id="sample-1", experiment=True)


def test_default_off_skips_preparation_ledger_and_experiment_writes(running, monkeypatch):
    host, thread, params = running
    original = host.conversation_store.threads.storage.thread_path(thread.thread_id).read_bytes()
    monkeypatch.setattr(decision_service, "decision_backend_from_profile", lambda *_: pytest.fail("off 不能准备后端"))
    stage = _stage(running)
    assert stage.error_code == "experiment_disabled" and not stage.enabled_points
    outcome = decision_service.decide(host, params, stage, point="planning", state=object(), questions=object(), candidates_revision="v1")
    assert outcome.status == "off" and outcome.reason == "experiment_disabled"
    assert not hasattr(host, "_model_call_ledger")
    assert not model_profiles_path(host.home_paths).exists()
    assert host.conversation_store.threads.storage.thread_path(thread.thread_id).read_bytes() == original
    with pytest.raises(DecisionSettingsAccessError):
        _grant(running)
    assert not hasattr(host, "_model_call_ledger")


def test_config_flag_yaml_and_dataclass_agree_and_enable_is_not_authorization(running, tmp_path):
    host, _thread, _params = running
    assert AgentConfig().decision_experiment_enabled is False
    path = tmp_path / "config.yaml"
    path.write_text("decision_experiment_enabled: true\n", encoding="utf-8")
    assert load_config(path).decision_experiment_enabled is True
    _enabled(running)
    assert _stage(running).error_code == "experiment_authorization_missing"
    assert not hasattr(host, "_model_call_ledger")


def test_explicit_v1_migration_preserves_revision_and_overrides_without_authority():
    old = {"schema": "decision_settings.v1", "revision": 9, "overrides": {"enabled": True}}
    result = validate_decision_settings(old)
    assert result == {**old, "schema": "decision_settings.v2", "experiment_authorization": None}
    assert old["schema"] == "decision_settings.v1"
    with pytest.raises(ModelProfileError):
        validate_decision_settings({**old, "schema": "decision_settings.v2"})


def test_host_grant_is_single_thread_authority_and_ordinary_mode_point_never_sends(running, monkeypatch):
    host, thread, params = running
    _enabled(running)
    owner_bytes = model_profiles_path(host.home_paths).read_bytes()
    # 夹具只构造宿主 API 的来源；产品入口是 Gateway /experiment，另见 test_decision_experiment_send_gate。
    report = _grant(running)
    authorization = report["experiment_authorization"]
    assert authorization["operations"] == ["observe"] and authorization["status"] == "active"
    assert authorization["schema"] == EXPERIMENT_SCHEMA and authorization["input_bound_policy"] == EMPIRICAL_INPUT_BOUND_POLICY
    assert authorization["binding"]["task_id"] == params.task_id
    assert authorization["binding"]["attempt_id"] == params.attempt_id
    assert authorization["binding"]["ledger_id"] == host._model_call_ledger.ledger_id
    assert authorization["source"]["operation_id"].startswith("host-command:")
    assert authorization["settings_revision"] == report["revision"]
    assert model_profiles_path(host.home_paths).read_bytes() == owner_bytes
    assert execute(host, "read", {})["experiment_authorization"] is None
    assert not host._model_call_ledger.records()
    monkeypatch.setattr(decision_service, "decision_backend_from_profile", lambda *_: pytest.fail("普通模式未关不能构造后端"))
    stage = _stage(running)
    # 准入通过，但 planning 普通模式为 observe：实验与普通建议互斥，不发布可准备点。
    assert stage.error_code == "" and stage.enabled_points == ()
    # 即使调用方伪造已发布的点，服务边界仍复读设置，普通模式未关就不能进入发送链。
    outcome = decision_service.decide(host, params, replace(stage, enabled_points=("planning",)), point="planning",
        state=object(), questions=object(), candidates_revision="v1")
    assert outcome.status == "experiment_unavailable" and outcome.reason == "experiment_point_mode_not_off"
    assert not host._model_call_ledger.records()
    budget = host._model_call_ledger.input_budget_snapshot(authorization["authorization_id"])
    assert budget["reserved_http_requests"] == budget["charged_input_tokens"] == 0


def test_stage_publishes_only_authorized_points_whose_ordinary_mode_is_off(running):
    host, thread, _params = running
    patch(host, {"enabled": True, "experiment_enabled": True, "points.planning.mode": "off"}, thread_id=thread.thread_id)
    _grant(running)
    stage = _stage(running)
    assert stage.error_code == "" and stage.enabled_points == ("planning",) and stage.experiment
    ordinary = decision_service.begin_decision_stage(host, running[2], operation_id="sample-2")
    assert "planning" not in ordinary.enabled_points and ordinary.experiment_available


@pytest.mark.parametrize("policy", ["", "provider:exact.v1", None])
def test_grant_requires_the_accepted_empirical_policy_before_budget(running, policy):
    host, _thread, _params = running
    _enabled(running)
    with pytest.raises(ModelProfileError):
        _grant(running, input_bound_policy=policy)
    assert not hasattr(host, "_model_call_ledger")


def test_v1_or_unknown_policy_envelope_stays_readable_but_can_never_be_admitted(running):
    host, thread, _params = running
    patch(host, {"enabled": True, "experiment_enabled": True, "points.planning.mode": "off"}, thread_id=thread.thread_id)
    current = _grant(running)["experiment_authorization"]
    v1 = {key: value for key, value in current.items() if key != "input_bound_policy"} | {"schema": EXPERIMENT_SCHEMA_V1}
    assert validate_experiment_authorization(v1) == v1
    with pytest.raises(ModelProfileError):
        validate_experiment_authorization({**v1, "input_bound_policy": EMPIRICAL_INPUT_BOUND_POLICY})
    with pytest.raises(ModelProfileError):
        validate_experiment_authorization({**current, "schema": ["v2"]})
    for envelope, reason in ((v1, "input_bound_policy_missing"),
                             ({**current, "input_bound_policy": "empirical:other.v9"}, "input_bound_policy_unsupported")):
        host.conversation_store.threads.update_atomic(thread.thread_id, lambda latest, value=envelope: replace(
            latest, decision_settings={**latest.decision_settings, "experiment_authorization": value}))
        stage = _stage(running)
        assert stage.error_code == reason and stage.enabled_points == ()
    assert not host._model_call_ledger.records()


def test_same_revision_concurrent_host_primitives_have_one_settings_winner(running):
    host, thread, _params = running
    _enabled(running)
    revision = execute(host, "read", {}, thread_id=thread.thread_id)["revision"]
    barrier = threading.Barrier(2)

    def submit(index):
        barrier.wait()
        try:
            return _grant(running, expected_revision=revision,
                source=HostCommandIdentity("alice", "alice", "host_control", thread.thread_id, f"authorize-{index}"))
        except DecisionSettingsConflict:
            return "conflict"

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(submit, range(2)))
    assert results.count("conflict") == 1
    assert execute(host, "read", {}, thread_id=thread.thread_id)["revision"]["thread"] == revision["thread"] + 1
    assert not host._model_call_ledger.records()


def test_canonical_tool_gate_can_reject_revoke_and_allowed_revoke_still_uses_original_cas(running, tmp_path):
    host, thread, params = running
    _enabled(running)
    report = _grant(running)
    arguments = {"action": "decision_experiment_revoke", "scope": "thread", "expected_revision": report["revision"],
                 "authorization_id": report["experiment_authorization"]["authorization_id"]}
    tool = UserConfigTool(host)
    refused = execute_canonical_test_call(tmp_path, tools={"user_config": tool}, tool_name="user_config",
                                          arguments=arguments, allowed_tools=[])
    assert refused.result.status != "succeeded"
    assert execute(host, "read", {}, thread_id=thread.thread_id)["experiment_authorization"]["status"] == "active"
    executed = execute_canonical_test_call(tmp_path, tools={"user_config": tool}, tool_name="user_config",
                                           arguments=arguments, run_id=params.run_id, attempt_id=params.attempt_id)
    assert executed.result.status == "succeeded"
    assert _stage(running).error_code == "experiment_revoked"


@pytest.mark.parametrize("field,value", [("status", []), ("operations", ["apply"]), ("source", {}), ("binding", {}),
                                        ("expires_at", float("inf")), ("authorization_id", "\ud800")])
def test_corrupt_authorization_never_becomes_empty_or_implicitly_granted(running, field, value):
    host, thread, _params = running
    _enabled(running)
    _grant(running)
    settings = host.conversation_store.threads.load(thread.thread_id).decision_settings
    with pytest.raises(ModelProfileError):
        validate_decision_settings({**settings, "experiment_authorization": {**settings["experiment_authorization"], field: value}})


@pytest.mark.parametrize("source", [None, {}, HostCommandIdentity("bob", "bob", "host_control", "other", "auth")])
def test_source_is_host_only_and_cross_owner_denied_before_budget(running, source):
    host, _thread, _params = running
    _enabled(running)
    with pytest.raises(DecisionSettingsAccessError):
        _grant(running, source=source)
    assert not hasattr(host, "_model_call_ledger")


@pytest.mark.parametrize("field,value", [("points", []), ("points", ["curator"]), ("points", ["planning", "planning"]),
    ("duration_seconds", 0), ("duration_seconds", float("inf")), ("max_http_requests", True),
    ("max_http_requests", 0), ("max_input_tokens", -1), ("max_input_tokens", 10**1000)])
def test_invalid_scope_or_unbounded_limits_do_not_create_budget(running, field, value):
    host, _thread, _params = running
    _enabled(running)
    with pytest.raises(ModelProfileError):
        _grant(running, **{field: value})
    assert not hasattr(host, "_model_call_ledger")


def test_model_tool_cannot_grant_or_patch_authority_even_through_executor(running, tmp_path):
    host, thread, _params = running
    _enabled(running)
    tool = UserConfigTool(host)
    actions = tool.model_spec.input_schema["properties"]["action"]["enum"]
    assert "decision_experiment_authorize" not in actions
    for arguments in ({"action": "decision_experiment_authorize"},
                      {"action": "decision_patch", "scope": "thread", "changes": {"experiment_authorization": {"authorized": True}}},
                      {"action": "decision_read", "host_authorization": {"authorized": True}}):
        result = execute_canonical_test_call(tmp_path, tools={"user_config": tool}, tool_name="user_config", arguments=arguments)
        assert result.result.status != "succeeded"
    with pytest.raises(DecisionSettingsAccessError):
        execute(host, "experiment_authorize", {"scope": "thread", "expected_revision": {"owner": 1, "thread": 0}}, thread_id=thread.thread_id)
    assert not hasattr(host, "_model_call_ledger")


def test_read_revoke_uses_original_tool_cas_and_reenable_never_revives(running):
    host, thread, _params = running
    _enabled(running)
    report = _grant(running)
    authorization_id = report["experiment_authorization"]["authorization_id"]
    tool = UserConfigTool(host)
    assert tool.execute({"action": "decision_read", "scope": "thread"}).ok
    revoked = tool.execute({"action": "decision_experiment_revoke", "scope": "thread", "expected_revision": report["revision"],
                            "authorization_id": authorization_id})
    assert revoked.ok
    assert _stage(running).error_code == "experiment_revoked"
    assert host._model_call_ledger.input_budget_snapshot(authorization_id)["status"] == "revoked"
    _enabled(running)
    assert _stage(running).error_code == "experiment_revoked"
    current = execute(host, "read", {}, thread_id=thread.thread_id)
    with pytest.raises(DecisionSettingsConflict):
        _grant(running, expected_revision=current["revision"])
    assert tool.execute({"action": "decision_experiment_revoke", "scope": "thread", "expected_revision": report["revision"],
                         "authorization_id": authorization_id}).error_code == "STALE_VERSION"


def test_other_setting_edit_same_value_invalidates_old_authorization(running):
    host, thread, _params = running
    _enabled(running)
    _grant(running)
    patch(host, {"enabled": True}, thread_id=thread.thread_id)
    assert _stage(running).error_code == "experiment_settings_changed"


def test_old_permission_cannot_open_new_ledger_or_new_execution_slice(running):
    host, _thread, params = running
    _enabled(running)
    _grant(running)
    original = host._model_call_ledger
    host._model_call_ledger = ModelCallLedger()
    assert _stage(running).error_code == "experiment_ledger_changed"
    del host._model_call_ledger
    assert _stage(running).error_code == "experiment_ledger_changed"
    assert not hasattr(host, "_model_call_ledger")
    host._model_call_ledger = original
    params.request_id = "next-message"
    assert _stage(running).error_code == "experiment_identity_changed"


def test_expiry_and_point_scope_are_checked_before_preparation(running, monkeypatch):
    host, _thread, params = running
    _enabled(running)
    report = _grant(running)
    stage = _stage(running)
    outcome = decision_service.decide(host, params, stage, point="recall", state=object(), questions=object(), candidates_revision="v1")
    assert outcome.reason == "experiment_point_forbidden"
    from agent_py_agent.agent.conversation import decision_experiment

    monkeypatch.setattr(decision_experiment.time, "time", lambda: report["experiment_authorization"]["expires_at"])
    assert _stage(running).error_code == "experiment_expired"


def test_direct_call_boundary_refuses_untyped_experiment_before_input_estimate_or_ledger(running, monkeypatch):
    host, _thread, params = running
    monkeypatch.setattr(decision_model_call, "estimate_tokens", lambda *_: pytest.fail("未知实验标签不能估算后放行"))
    from agent_py_agent.agent.contracts.model_call_budget import ModelCallBudgetError
    from agent_py_agent.tests.test_decision_protocol import binding, questions

    request = decision_model_call.DecisionRequest(binding(), {"fact": 1}, questions())
    backend = SimpleNamespace(decide=lambda *_a, **_k: pytest.fail("未知实验标签不能发送"))
    for experiment in ("fake", {"authorization_id": "fake"}):
        with pytest.raises(ModelCallBudgetError, match="experiment_call_invalid"):
            decision_model_call.invoke_decision_model_call(host, params, request, backend, deadline=10, resource_key="test",
                                                           experiment=experiment)
    assert not hasattr(host, "_model_call_ledger")
