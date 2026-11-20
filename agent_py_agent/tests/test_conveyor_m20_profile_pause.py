from __future__ import annotations

import pickle
import time
from copy import copy
from dataclasses import FrozenInstanceError, asdict, fields, make_dataclass, replace
from pathlib import Path
from weakref import ref

import pytest

from agent_py_agent.agent.common.fast_dataclass import fast_replace
from agent_py_agent.agent.conversation import wake_poison
from agent_py_agent.agent.conversation.wake_attempt_tracking import (
    InflightWakeAttempt,
    WakeAttemptTracker,
    wake_attempt_waiting,
)
from agent_py_agent.agent.gateway_parts import request_binding, request_worker
from agent_py_agent.agent.settings.model_profiles import (
    execute_model_profile_operation,
    model_profiles_path,
    read_model_profiles,
)
from agent_py_agent.agent.settings.model_provider_schema import ModelProfileError
from agent_py_agent.agent.settings.thread_model_selection import thread_model_profile_id
from agent_py_agent.cli import gateway_loops
from agent_py_agent.cli.gateway_lane_retry import BackgroundLaneRetry
from agent_py_agent.tests.test_session_task_real_chain import _select_scripted_model
from agent_py_agent.tests.test_wake_attempt_wiring import (
    _attempts,
    _chain_with_message_wake,
    _spy_claims,
)


@pytest.mark.parametrize("reason", ["profile_disabled", "provider_disabled"])
def test_six_disabled_profile_errors_keep_original_wake_pending(tmp_path, monkeypatch, reason):
    chain, (wake,) = _chain_with_message_wake(tmp_path, monkeypatch)
    attempts = _attempts(chain)
    for index in range(6):
        if chain.scheduler.store.wakes.pending_one(wake.wake_signal_id) is None:
            break
        tracker = WakeAttemptTracker(chain.scheduler, (wake,), 1000.0 + index * 86_400)
        assert tracker.begin(f"claim-{index}") == ""
        try:
            raise ModelProfileError("disabled test profile", reason=reason)
        except ModelProfileError as error:
            tracker.note_error(error, quota=False)
        tracker.finish(None)

    rows, errors = attempts.quarantined()
    assert errors == []
    assert rows == []
    assert chain.scheduler.store.wakes.pending_one(wake.wake_signal_id) == wake
    state, error = attempts.state_report(wake.wake_signal_id)
    assert error is None
    assert state.same_cause_count == state.total_count == 0
    assert state.environment_pause_reason == "environment_pause:model_profile_disabled"
    assert len(state.environment_pause_fingerprint) == 64
    assert wake_attempt_waiting(chain.scheduler, wake, now=1_000_000.0)


@pytest.mark.parametrize("change", ["profile", "provider", "explicit_selection"])
def test_disabled_profile_resumes_the_same_wake_once(tmp_path, monkeypatch, change):
    chain, (wake,) = _chain_with_message_wake(tmp_path, monkeypatch)
    agent, scheduler = chain.agent, chain.scheduler
    profile_id = scheduler.store.threads.require(wake.thread_id).model_profile_id
    assert profile_id
    claims = _spy_claims(chain, monkeypatch)

    def set_enabled(enabled):
        kind = "provider" if change == "provider" else "profile"
        profiles = read_model_profiles(model_profiles_path(agent.home_paths))["profiles"]
        item_id = profiles[profile_id]["provider_id"] if kind == "provider" else profile_id
        execute_model_profile_operation(agent, f"save_{kind}", {
            f"{kind}_id": item_id, "editing": True, kind: {"enabled": enabled},
        })

    # 让真实调度器和尝试账处理档案解析异常，原模型传输沿 fixture 的脚本线路。
    set_enabled(False)
    supervisor = object.__new__(gateway_loops._BackgroundMainSupervisor)
    supervisor._lane_retry = BackgroundLaneRetry()
    submitted = supervisor._safe_thread_tick(scheduler, "base", wake.thread_id)
    assert submitted == []
    state, error = _attempts(chain).state_report(wake.wake_signal_id)
    assert error is None and state.environment_pause_reason == wake_poison.WAKE_REASON_MODEL_PROFILE_DISABLED
    attempted_claims = len(claims)
    for offset in (0, 86_401):
        assert wake.thread_id not in scheduler.ready_thread_ids(now=time.time() + offset)
        assert not supervisor._thread_lane_can_retry(scheduler, "base", wake.thread_id)
    assert len(claims) == attempted_claims

    restarted_scheduler = gateway_loops._build_background_scheduler(agent, scheduler.runtime.channels)
    assert wake.thread_id not in restarted_scheduler.ready_thread_ids(now=time.time())
    if change == "explicit_selection":
        _select_scripted_model(agent, agent.config)
        selected = read_model_profiles(model_profiles_path(agent.home_paths))["selected"]
        assert selected != profile_id
        thread_model_profile_id(agent, wake.thread_id, select=selected)
    else:
        set_enabled(True)
    assert wake.thread_id in restarted_scheduler.ready_thread_ids(now=time.time())
    assert wake.thread_id in scheduler.ready_thread_ids(now=time.time())
    assert supervisor._thread_lane_can_retry(scheduler, "base", wake.thread_id)
    supervisor._safe_thread_tick(scheduler, "base", wake.thread_id)
    assert len(claims) == attempted_claims + 1
    assert scheduler.store.wakes.pending_one(wake.wake_signal_id) is None
    assert not _attempts(chain).has_ledger(wake.wake_signal_id)
    assert wake.thread_id not in scheduler.ready_thread_ids(now=time.time())


@pytest.mark.parametrize("reason", ["profile_not_found", "catalog_invalid", "credential_missing", "capability_mismatch"])
def test_other_profile_errors_keep_the_existing_failure_verdict(reason):
    verdict = wake_poison.verdict_for_error(ModelProfileError("test profile error", reason=reason))
    assert verdict.kind == wake_poison.WAKE_VERDICT_FAILURE


def test_profile_pause_preserves_existing_failure_history():
    earlier = wake_poison.WakeAttemptVerdict(wake_poison.WAKE_VERDICT_FAILURE, "error:prior")
    state = wake_poison.next_poison_state(wake_poison.WakePoisonState(), earlier, now=1_000.0)
    assert "environment_pause_reason" not in state.to_dict()
    verdict = wake_poison.verdict_for_error(ModelProfileError("disabled", reason="profile_disabled"))
    paused = wake_poison.next_poison_state(state, verdict, now=2_000.0)
    assert wake_poison.WakePoisonState.from_dict(paused.to_dict()) == paused
    assert paused.total_count == paused.same_cause_count == 1
    assert not paused.uncounted_count and wake_poison.quarantine_decision(paused) is None
    retried = wake_poison.next_poison_state(paused, earlier, now=90_000.0)
    assert retried.total_count == retried.same_cause_count == 2
    assert "environment_pause_reason" not in retried.to_dict()


_REQUEST_PATH = Path("gateway/processing/request.json")
_CONSTRUCTOR_CASES = [
    (InflightWakeAttempt, (None, "wake", "claim")),
    (wake_poison.WakeAttemptVerdict, ("neutral", "attempt:test")),
    (wake_poison.WakePoisonState, ()),
    (wake_poison.QuarantineDecision, ("attempt:test", 0, 0, 0, False)),
    (wake_poison.WakeStallAlert, ("attempt:test", 1, 2.0, 1)),
    (wake_poison.WakeAttemptFacts, ()),
    (request_binding.GatewayActiveTurnTransition, (_REQUEST_PATH, "request", "attempt")),
    (request_worker._ClaimedGatewayRequestContext, (None, _REQUEST_PATH, {}, "request", "worker")),
    (request_worker._PendingGatewayRequest, (_REQUEST_PATH, {})),
    (request_worker.AdmissionLimits, (3, 6)),
    (request_worker._WorkerRouteCheck, (None, None, _REQUEST_PATH, {})),
]


@pytest.mark.parametrize(("cls", "args"), _CONSTRUCTOR_CASES, ids=[cls.__name__ for cls, _ in _CONSTRUCTOR_CASES])
def test_fast_frozen_matches_plain_dataclass(cls, args):
    reference = make_dataclass(cls.__name__, [(item.name, item.type, copy(item)) for item in fields(cls)], frozen=True)
    dto, plain = cls(*args), reference(*args)
    assert asdict(dto) == asdict(plain)
    assert repr(dto) == repr(plain)
    assert asdict(fast_replace(dto)) == asdict(replace(plain))
    assert pickle.loads(pickle.dumps(dto)) == dto
    with pytest.raises(FrozenInstanceError):
        setattr(dto, fields(cls)[0].name, None)
    if cls.__module__ == request_worker.__name__:
        assert ref(dto)() is dto


def g1_case_inflight_wake_attempt():
    return lambda: InflightWakeAttempt(None, "wake", "claim")


def g1_case_wake_attempt_verdict():
    return lambda: wake_poison.WakeAttemptVerdict("neutral", "attempt:test")


def g1_case_wake_poison_state():
    return wake_poison.WakePoisonState


def g1_case_quarantine_decision():
    return lambda: wake_poison.QuarantineDecision("attempt:test", 0, 0, 0, False)


def g1_case_wake_stall_alert():
    return lambda: wake_poison.WakeStallAlert("attempt:test", 1, 2.0, 1)


def g1_case_wake_attempt_facts():
    return wake_poison.WakeAttemptFacts


def g1_case_gateway_active_turn_transition():
    return lambda: request_binding.GatewayActiveTurnTransition(_REQUEST_PATH, "request", "attempt")


def g1_case_claimed_gateway_request_context():
    return lambda: request_worker._ClaimedGatewayRequestContext(None, _REQUEST_PATH, {}, "request", "worker")


def g1_case_pending_gateway_request():
    return lambda: request_worker._PendingGatewayRequest(_REQUEST_PATH, {})


def g1_case_admission_limits():
    return lambda: request_worker.AdmissionLimits(3, 6)


def g1_case_worker_route_check():
    return lambda: request_worker._WorkerRouteCheck(None, None, _REQUEST_PATH, {})
