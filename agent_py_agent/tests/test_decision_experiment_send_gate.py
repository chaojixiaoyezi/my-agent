"""实验发送门：宿主授权、经验输入上界、原账预留与传输层单次许可；只连本地 HTTP 服务并统计 TCP accept，不调用真实供应商。"""
import hashlib
import json
import threading
from dataclasses import replace
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model.call_runtime import model_call_ledger
from agent_py_agent.agent.backends.gateway_helpers import gateway_request_body
from agent_py_agent.agent.backends.provider_send_gate import (
    ProviderSendAttempt,
    ProviderSendRefused,
)
from agent_py_agent.agent.backends.typesafe_decision import TypesafeDecisionBackend
from agent_py_agent.agent.backends.typesafe_decision_wire import jev_empirical_input_bound
from agent_py_agent.agent.capability import decision_recommendation
from agent_py_agent.agent.capability.decision_recommendation import (
    CapabilityPresentation,
    recommend_capabilities,
)
from agent_py_agent.agent.concurrency.interrupt import InterruptHandle
from agent_py_agent.agent.contracts.model_call_budget import (
    InputTokenBound,
    ModelCallBudgetError,
    SendPermitBinding,
)
from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallFinishParams,
    ModelCallLedger,
    ModelCallProviderAttemptParams,
    ModelCallStartedParams,
)
from agent_py_agent.agent.conversation import (
    decision_experiment,
    decision_model_call,
    decision_policy,
)
from agent_py_agent.agent.conversation.decision_policy import decision_owner_ref
from agent_py_agent.agent.runtime_db.host_commands import HostCommandIdentity
from agent_py_agent.agent.settings.decision_experiment import authorize_decision_experiment
from agent_py_agent.agent.settings.decision_experiment_schema import (
    EMPIRICAL_INPUT_BOUND_POLICY,
    EXPERIMENT_SCHEMA_V1,
)
from agent_py_agent.agent.settings.decision_settings import (
    execute_decision_settings_operation as execute,
)
from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation
from agent_py_agent.tests import test_decision_capability_http as capability_module
from agent_py_agent.tests.test_decision_capability_consumer import model_input
from agent_py_agent.tests.test_decision_capability_consumer import surface as surface  # noqa: F401
from agent_py_agent.tests.test_decision_capability_http import (
    capability_http as capability_http,  # noqa: F401
)
from agent_py_agent.tests.test_decision_capability_http import configure
from agent_py_agent.tests.test_decision_settings import patch
from agent_py_agent.tests.test_tool_presentation_projection import (
    prepared as tool_surface,  # noqa: F401
)

_PREFIX = "skill_tool_decision:experiment:"


# LLM: 复用原 capability_http/surface 夹具；只替换本地服务的 JSON 记录与用量字段，并在服务端 accept 处计数，不替换任何生产边界。
# 原 60 个中文 Skill 的完整请求约 119KB/62 题，经验上界约 76.7k 超出标定，默认取前 10 个 Skill 留在标定范围内。
# 函数用途: 建立普通模式关闭、实验能力开启的本地决策环境，记录收到的原始字节与 TCP 连接次数。
@pytest.fixture
def lab(surface, capability_http, monkeypatch):  # noqa: F811
    port = int(capability_http.url.rsplit(":", 1)[1])
    accepted, raw, original_answer = [], [], capability_module.native_answer
    full = surface.catalog.snapshot
    lab = SimpleNamespace(surface=surface, http=capability_http, raw=raw, monkeypatch=monkeypatch,
                          usage=lambda _raw: 12345, accepts=lambda: accepted.count(port), full_skills=full,
                          thread_id=surface.params.task_attributes["agent_thread_id"])
    subset = replace(full, entries=full.entries[:10], fingerprint="experiment-subset")
    surface.host.current_skill_snapshot = lambda: subset
    surface.host.skill_snapshot_for_run_scope = lambda _root: subset

    def verify(server, _request, _address):
        accepted.append(server.server_address[1])
        return True

    def loads(data):
        raw.append(bytes(data))
        return json.loads(data)

    monkeypatch.setattr(ThreadingHTTPServer, "verify_request", verify)
    monkeypatch.setattr(capability_module, "json", SimpleNamespace(loads=loads, dumps=json.dumps))
    monkeypatch.setattr(capability_module, "native_answer",
                        lambda body: {**original_answer(body), "usage": {"input_tokens": lab.usage(raw[-1])}})
    lab.baseline = configure(surface, capability_http, timeout=1.5)
    surface.params.attempt_id = "capability-attempt"
    patch(surface.host, {"points.skill_tool.mode": "off", "experiment_enabled": True})
    return lab


# LLM: 测试侧的宿主来源夹具，等价于 Gateway /experiment 已冻结的参数；产品入口另见 test_decision_experiment_command。
# 函数用途: 以当前设置版本为本轮授权一次只观察实验并返回授权信封。
def _grant(lab, **limits):
    host, params = lab.surface.host, lab.surface.params
    values = {"duration_seconds": 60, "max_http_requests": 1, "max_input_tokens": 60_000, **limits}
    return authorize_decision_experiment(
        host, params, source=HostCommandIdentity("alice", "alice", "chat", lab.thread_id, params.request_id),
        expected_revision=execute(host, "read", {"scope": "thread"}, thread_id=lab.thread_id)["revision"],
        points=["skill_tool"], input_bound_policy=EMPIRICAL_INPUT_BOUND_POLICY, **values)["experiment_authorization"]


def _recommend(lab):
    surface = lab.surface
    return recommend_capabilities(surface.host, surface.params, surface.snapshot, surface.contract)


def _snapshot(lab, authorization):
    return model_call_ledger(lab.surface.host).input_budget_snapshot(authorization["authorization_id"])


def _bound_for(raw):
    return jev_empirical_input_bound(raw, json.loads(raw), point="skill_tool")


# LLM: 原结果必须保持基础快照与未采用选择；比较实际渲染的 prompt/schema，而不是只比较对象身份。
# 函数用途: 断言实验结果没有改变模型可见输入。
def _assert_original(lab, result):
    surface = lab.surface
    assert result.tool_snapshot is surface.snapshot and result.selected_skill_ids is None
    assert model_input(surface, result)[:2] == model_input(surface, CapabilityPresentation(surface.snapshot))[:2]


def _revoke(lab, authorization):
    host = lab.surface.host
    revision = execute(host, "read", {"scope": "thread"}, thread_id=lab.thread_id)["revision"]
    execute(host, "experiment_revoke", {"scope": "thread", "expected_revision": revision,
                                        "authorization_id": authorization["authorization_id"]}, thread_id=lab.thread_id)


# LLM: 用原账本 API 在同一授权下完成一次已结算的发送，模拟此前已用掉 HTTP 次数；不连服务器。
# 函数用途: 预先消耗授权唯一的一次 HTTP 预算。
def _spend_http(lab, authorization):
    ledger, binding = model_call_ledger(lab.surface.host), authorization["binding"]
    started = ModelCallStartedParams("prior-call", "fixture", "jev-test", 1, request_id=binding["request_id"],
        run_id=binding["run_id"], metadata={"purpose": "decision", **{key: binding[key] for key in (
            "owner_ref", "thread_id", "task_id", "attempt_id")}})
    send = SendPermitBinding(endpoint="https://prior.test/v1/systemone", body_sha256="0" * 64, model="jev-test",
                             connection_revision="prior")
    bound = InputTokenBound(tokens=100, method="fixture.v1", body_bytes=1, questions=1, state_bytes=1)
    _record, token = ledger.reserve_input_budget(started, budget_id=authorization["authorization_id"], input_bound=bound,
                                                 send_binding=send)
    with token:
        ledger.consume_send_permit("prior-call", budget_id=authorization["authorization_id"], binding=send)
        ledger.provider_attempt(ModelCallProviderAttemptParams("prior-call", "http-1", "started"))
        ledger.finished(ModelCallFinishParams("prior-call", input_tokens=50, provider_usage_reported=True,
                                              provider_usage_fields=("input_tokens",)))
        ledger.settle_input_budget("prior-call")


def _rewrite_v1(lab, authorization):
    v1 = {key: value for key, value in authorization.items() if key != "input_bound_policy"} | {"schema": EXPERIMENT_SCHEMA_V1}
    lab.surface.host.conversation_store.threads.update_atomic(lab.thread_id, lambda latest: replace(
        latest, decision_settings={**latest.decision_settings, "experiment_authorization": v1}))


def _expire(lab, authorization):
    lab.monkeypatch.setattr(decision_experiment, "time", SimpleNamespace(time=lambda: authorization["expires_at"]))


# LLM: 原 surface 的 60 个中文 Skill 按最终 wire 字节计算的经验上界超过 57,600，是真实的越界样本。
# 函数用途: 恢复完整 Skill 目录，让实验请求超出经验上界标定范围。
def _full_catalog(lab):
    host = lab.surface.host
    host.current_skill_snapshot = lambda: lab.full_skills
    host.skill_snapshot_for_run_scope = lambda _root: lab.full_skills


_REFUSALS = {
    "no_grant": (lambda lab: None, "experiment_authorization_missing"),
    "capability_off": (lambda lab: (_grant(lab), patch(lab.surface.host, {"experiment_enabled": False})), "experiment_disabled"),
    "revoked": (lambda lab: _revoke(lab, _grant(lab)), "experiment_revoked"),
    "expired": (lambda lab: _expire(lab, _grant(lab)), "experiment_expired"),
    "ledger_generation": (lambda lab: (_grant(lab), setattr(lab.surface.host, "_model_call_ledger", ModelCallLedger())),
                          "experiment_ledger_changed"),
    "http_used_up": (lambda lab: _spend_http(lab, _grant(lab)), "http_budget_exhausted"),
    "bound_above_remaining": (lambda lab: _grant(lab, max_input_tokens=1_000), "input_budget_exhausted"),
    "out_of_calibration": (lambda lab: (_grant(lab), _full_catalog(lab)), "input_bound_out_of_calibration"),
    "state_out_of_calibration": (lambda lab: (_grant(lab), setattr(lab.surface.params, "user_prompt", "长" * 800)),
                                 "input_bound_out_of_calibration"),
    "v1_envelope": (lambda lab: _rewrite_v1(lab, _grant(lab)), "input_bound_policy_missing"),
}


@pytest.mark.parametrize("case", sorted(_REFUSALS))
def test_missing_authorization_or_budget_never_opens_a_connection(lab, case):
    setup, code = _REFUSALS[case]
    setup(lab)
    before = [record.call_id for record in model_call_ledger(lab.surface.host).records()]
    result = _recommend(lab)
    assert result.finding == _PREFIX + code
    assert lab.accepts() == 0 and not lab.http.requests and not lab.raw
    assert [record.call_id for record in model_call_ledger(lab.surface.host).records()] == before
    _assert_original(lab, result)


def _revoke_current(lab, _backend, _prepared):
    host = lab.surface.host
    _revoke(lab, execute(host, "read", {"scope": "thread"}, thread_id=lab.thread_id)["experiment_authorization"])


def _rotate_key(lab, _backend, _prepared):
    host = lab.surface.host
    profile_id = execute(host, "read", {})["effective"]["profile_id"]
    execute_model_profile_operation(host, "save_provider", {"provider_id": "provider-" + profile_id,
                                                            "provider": {"api_key": "rotated-secret"}, "editing": True})


def _throwing_observer(*_args):
    raise RuntimeError("遥测故障")


# 攻击均发生在原账预留之后、传输层硬门之前；硬门必须在任何 DNS/连接/遥测之前拒绝。
_ATTACKS = {
    "revoke": (_revoke_current, "experiment_revoked"),
    "key_change": (_rotate_key, "connection_changed"),
    "tampered_payload": (lambda lab, _b, prepared: prepared.payload["state"].update(query="被篡改的正文"), "send_body_mismatch"),
    "tampered_endpoint": (lambda lab, backend, _p: setattr(backend, "api_base", "http://127.0.0.1:9"), "send_endpoint_mismatch"),
    "forged_stage_identity": (lambda lab, _b, _p: setattr(lab.surface.params, "attempt_id", "forged-attempt"),
                              "experiment_identity_changed"),
}


@pytest.mark.parametrize("attack", sorted(_ATTACKS))
def test_bypass_after_reservation_is_refused_before_any_connection(lab, attack):
    authorization, original_send = _grant(lab), TypesafeDecisionBackend.send
    action, code = _ATTACKS[attack]

    def send(backend, prepared, *, permit):
        action(lab, backend, prepared)
        return original_send(backend, prepared, permit=permit)

    lab.monkeypatch.setattr(TypesafeDecisionBackend, "send", send)
    lab.monkeypatch.setattr(decision_model_call, "record_model_provider_attempt", _throwing_observer)
    result = _recommend(lab)
    assert result.finding == _PREFIX + "send_refused:" + code
    assert result.observation["adopted"] is False and result.observation["retain_reason"] == "send_refused:" + code
    assert lab.accepts() == 0 and not lab.http.requests and not lab.raw
    record = model_call_ledger(lab.surface.host).records()[-1]
    assert (record.status, record.error_type, record.error_code) == ("failed", "ProviderSendRefused", "DECISION_SEND_REFUSED")
    assert record.provider_attempt_count == 0 and record.metadata["send_permit"]["state"] == "issued"
    assert record.metadata["input_budget_outcome"] == "refused_before_send"
    snapshot = _snapshot(lab, authorization)
    assert snapshot["status"] == "send_refused" and snapshot["reserved_http_requests"] == 1
    assert snapshot["charged_input_tokens"] == record.metadata["input_bound"]["tokens"]
    owner = decision_owner_ref(lab.surface.host)
    assert all(key[0] != owner for key in decision_policy._FAILURES), "发送拒绝不能进入连接退避"
    _assert_original(lab, result)


def test_default_off_never_builds_an_experiment_stage_or_connects(lab):
    patch(lab.surface.host, {"experiment_enabled": False})
    calls, original = [], decision_recommendation.begin_decision_stage
    lab.monkeypatch.setattr(decision_recommendation, "begin_decision_stage",
                            lambda *a, **k: calls.append(k.get("experiment", False)) or original(*a, **k))
    result = _recommend(lab)
    assert calls == [False] and result.finding == "" and result.observation is None
    assert lab.accepts() == 0 and not model_call_ledger(lab.surface.host).records()


def test_routing_change_after_reservation_cancels_before_any_connection(lab):
    authorization, original_send = _grant(lab), TypesafeDecisionBackend.send

    def send(backend, prepared, *, permit):
        patch(lab.surface.host, {"points.skill_tool.context_policy": "metadata"})
        return original_send(backend, prepared, permit=permit)

    lab.monkeypatch.setattr(TypesafeDecisionBackend, "send", send)
    result = _recommend(lab)
    # 设置撤销句柄可能先唤醒调用方，也可能由发送门先拒绝；两条路径都必须零连接且不退还占用。
    assert result.finding in {_PREFIX + "settings_changed", _PREFIX + "send_refused:settings_changed"}
    _join_bounded_workers()
    assert lab.accepts() == 0 and not lab.http.requests
    assert model_call_ledger(lab.surface.host).records()[-1].metadata["send_permit"]["state"] == "issued"
    snapshot = _snapshot(lab, authorization)
    assert snapshot["status"] == "send_refused" and snapshot["reserved_http_requests"] == 1


def test_authorized_send_is_single_bound_request_and_settles_provider_input(lab):
    authorization, outcomes, original_decide = _grant(lab), [], decision_recommendation.decide
    lab.monkeypatch.setattr(decision_recommendation, "decide", lambda *a, **k: outcomes.append(original_decide(*a, **k)) or outcomes[-1])
    result = _recommend(lab)
    assert result.finding == _PREFIX + "success"
    assert [(item.mode, item.status, item.may_apply) for item in outcomes] == [("observe", "success", False)]
    observation = result.observation
    assert (observation["mode"], observation["status"], observation["adopted"], observation["retain_reason"]) == (
        "observe", "success", False, "success")
    assert lab.accepts() == 1 and len(lab.http.requests) == 1 and lab.http.requests[0][0] == "/v1/systemone"
    [record] = model_call_ledger(lab.surface.host).records()
    assert hashlib.sha256(lab.raw[0]).hexdigest() == record.metadata["send_permit"]["body_sha256"]
    bound = _bound_for(lab.raw[0])
    assert record.metadata["input_bound"] == bound.to_record() and bound.kind == "empirical"
    assert record.input_tokens not in {bound.tokens, 12345} and record.provider_attempt_count == 1
    assert record.metadata["purpose"] == "decision" and record.metadata["decision_operation"] == "experiment_observe"
    snapshot = _snapshot(lab, authorization)
    assert (snapshot["reserved_http_requests"], snapshot["charged_input_tokens"], snapshot["provider_input_tokens"]) == (1, 12345, 12345)
    assert snapshot["input_bound_kind"] == "empirical" and snapshot["status"] == "active"
    assert snapshot["input_bound_ratio"] == round(12345 / bound.tokens, 6)
    _assert_original(lab, result)
    again = _recommend(lab)
    assert again.finding == _PREFIX + "http_budget_exhausted"
    assert lab.accepts() == 1 and len(model_call_ledger(lab.surface.host).records()) == 1
    _assert_original(lab, again)


def test_provider_input_above_empirical_bound_closes_budget(lab):
    authorization = _grant(lab, max_http_requests=2)
    lab.usage = lambda raw: _bound_for(raw).tokens + 1
    _recommend(lab)
    bound = _bound_for(lab.raw[0]).tokens
    snapshot = _snapshot(lab, authorization)
    assert snapshot["status"] == "input_bound_violated" and snapshot["unknown_usage_calls"] == 0
    assert snapshot["charged_input_tokens"] == snapshot["provider_input_tokens"] == bound + 1
    assert snapshot["input_bound_warning"] == "input_bound_ratio_high"
    again = _recommend(lab)
    assert again.finding == _PREFIX + "experiment_budget_input_bound_violated"
    assert lab.accepts() == 1


def _join_bounded_workers():
    for thread in threading.enumerate():
        if thread.name.startswith("bounded-call:"):
            thread.join(3)


def test_hanging_server_times_out_keeps_whole_bound_and_late_answer_cannot_reopen(lab):
    authorization = _grant(lab, max_http_requests=2)
    lab.http.block = True
    try:
        result = _recommend(lab)
        [record] = model_call_ledger(lab.surface.host).records()
        snapshot = _snapshot(lab, authorization)
        assert record.status == "timed_out" and record.metadata["send_permit"]["state"] == "consumed"
        assert snapshot["status"] == "usage_unknown" and snapshot["unknown_usage_calls"] == 1
        assert snapshot["charged_input_tokens"] == record.metadata["input_bound"]["tokens"]
        assert snapshot["reserved_http_requests"] == 1 and snapshot["provider_input_tokens"] == 0
        _assert_original(lab, result)
    finally:
        lab.http.release.set()
    assert lab.http.finished.wait(3)
    _join_bounded_workers()
    assert _snapshot(lab, authorization) == snapshot
    assert model_call_ledger(lab.surface.host).records()[0].status == "timed_out"
    assert _recommend(lab).finding == _PREFIX + "experiment_budget_usage_unknown"
    assert lab.accepts() == 1


def test_throwing_observer_cannot_open_the_gate_and_missing_attempt_keeps_whole_bound(lab):
    authorization = _grant(lab)
    lab.monkeypatch.setattr(decision_model_call, "record_model_provider_attempt", _throwing_observer)
    result = _recommend(lab)
    [record] = model_call_ledger(lab.surface.host).records()
    assert lab.accepts() == 1 and record.provider_attempt_count == 0
    assert record.metadata["send_permit"]["state"] == "consumed"
    snapshot = _snapshot(lab, authorization)
    assert snapshot["status"] == "usage_unknown" and snapshot["charged_input_tokens"] == record.metadata["input_bound"]["tokens"]
    _assert_original(lab, result)


_STATIC = SendPermitBinding(endpoint="https://decision.test/v1/systemone", body_sha256="a" * 64, model="jev-test",
                            connection_revision="connection")


@pytest.mark.parametrize("changes,code", [({"method": "GET"}, "send_method_mismatch"), ({"url": "https://x.test/"}, "send_endpoint_mismatch"),
                                          ({"body_sha256": "b" * 64}, "send_body_mismatch"), ({"model": "other"}, "send_model_mismatch"),
                                          ({"attempt": 1}, "send_retry_forbidden")])
def test_permit_static_binding_refuses_before_settings_or_ledger(changes, code):
    ledger = SimpleNamespace(consume_send_permit=lambda *_a, **_k: pytest.fail("静态绑定不符不能进入账本"))
    experiment = decision_model_call.DecisionExperimentCall("authorization", "thread", "skill_tool", "policy", "connection")
    permit = decision_model_call.issue_send_permit(object(), SimpleNamespace(), ledger=ledger, call_id="call",
                                                   experiment=experiment, binding=_STATIC, deadline=10**9, handle=None)
    attempt = ProviderSendAttempt(**{"method": "POST", "url": _STATIC.endpoint, "body_sha256": _STATIC.body_sha256,
                                     "model": _STATIC.model, "attempt": 0, **changes})
    with pytest.raises(ProviderSendRefused) as caught:
        permit.admit(attempt)
    assert caught.value.code == code


@pytest.mark.parametrize("handle_cancelled,deadline,code", [(True, 10**9, "settings_changed"), (False, 0.0, "deadline_exhausted")])
def test_permit_checks_cancel_handle_and_deadline_before_rereading_settings(handle_cancelled, deadline, code):
    ledger = SimpleNamespace(consume_send_permit=lambda *_a, **_k: pytest.fail("运行态拒绝不能进入账本"))
    handle = InterruptHandle()
    if handle_cancelled:
        handle.cancel()
    experiment = decision_model_call.DecisionExperimentCall("authorization", "thread", "skill_tool", "policy", "connection")
    # agent 故意不可读设置：若运行态检查被跳过，会得到 experiment_state_unavailable 而不是固定原因。
    permit = decision_model_call.issue_send_permit(object(), SimpleNamespace(), ledger=ledger, call_id="call",
                                                   experiment=experiment, binding=_STATIC, deadline=deadline, handle=handle)
    with pytest.raises(ProviderSendRefused) as caught:
        permit.admit(ProviderSendAttempt("POST", _STATIC.endpoint, _STATIC.body_sha256, _STATIC.model, 0))
    assert caught.value.code == code


@pytest.mark.parametrize("body_bytes,questions,expected", [(65_063, 27, 40_468), (64_921, 27, 40_397), (173, 1, 1_367)])
def test_empirical_bound_covers_measured_samples_with_margin(body_bytes, questions, expected):
    billed = {27: 17_383, 1: 296}[questions]
    payload = {"state": {"q": "x"}, "questions": {f"q{index}": {} for index in range(questions)}}
    bound = jev_empirical_input_bound(b"x" * body_bytes, payload, point="skill_tool")
    assert bound.tokens == expected and bound.tokens >= 2.3 * billed
    assert (bound.kind, bound.method, bound.body_bytes, bound.questions) == ("empirical", "jev_wire_bytes.v1", body_bytes, questions)
    assert bound.state_bytes == len(gateway_request_body(payload["state"]))


def test_empirical_bound_is_monotonic_in_bytes_and_questions():
    previous = 0
    for body_bytes in range(0, 20_001, 997):
        tokens = jev_empirical_input_bound(b"x" * body_bytes, {"state": {}, "questions": {"a": {}}}, point="skill_tool").tokens
        assert tokens >= previous
        previous = tokens
    counts = [jev_empirical_input_bound(b"x" * 1000, {"state": {}, "questions": {str(i): {} for i in range(q)}},
                                        point="skill_tool").tokens for q in range(1, 65)]
    assert counts == sorted(counts) and len(set(counts)) == 64


@pytest.mark.parametrize("body,questions,state,point", [
    (b"x" * 10, 65, "", "skill_tool"), (b"x" * 10, 1, "s" * 4095, "skill_tool"),
    (b"x" * 120_000, 1, "", "skill_tool"), (b"x" * 10, 1, "", "planning"), (b"x" * 10, 0, "", "skill_tool"),
    ("text", 1, "", "skill_tool")])
def test_empirical_bound_refuses_outside_calibration(body, questions, state, point):
    payload = {"state": state, "questions": {str(index): {} for index in range(questions)}}
    with pytest.raises(ModelCallBudgetError, match="input_bound_out_of_calibration"):
        jev_empirical_input_bound(body, payload, point=point)


def test_empirical_bound_accepts_exact_calibration_edges():
    payload = {"state": "s" * 4094, "questions": {str(index): {} for index in range(64)}}
    assert len(gateway_request_body(payload["state"])) == 4096
    bound = jev_empirical_input_bound(b"x" * (2 * (57_600 - 1024 - 256 * 64)), payload, point="skill_tool")
    assert bound.tokens == 57_600 and bound.questions == 64 and bound.state_bytes == 4096
