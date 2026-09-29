"""唤醒毒丸判定的纯函数合同：结果分类、同因连续段、总上限、两类退避与结案判定。

判定只看结构化事实（admission 码、异常类型与 error_code、报告字段），不读文案；未知码默认计数。
正向对照与反向断言成对出现；最后两例用纯函数重放 204f4ddf9 与 7b83c8730 的失败形态。
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends.errors import (
    ModelNotConfiguredError,
    ProviderConfigurationError,
    ProviderConnectionError,
    ProviderQuotaExhaustedError,
    ProviderRequestRejectedError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderTransientError,
    ProviderUsageLimitError,
)
from agent_py_agent.agent.capability.skill_snapshot import SkillSnapshotError
from agent_py_agent.agent.conversation.background_execution import BackgroundCompactSliceYield
from agent_py_agent.agent.conversation.wake_poison import (
    WAKE_ATTEMPT_ABANDONED,
    WAKE_ATTEMPT_NEUTRAL,
    WAKE_ATTEMPT_SUCCESS,
    WAKE_EXPECTED_WAIT_ADMISSIONS,
    WAKE_POISON_SAME_CAUSE_LIMIT,
    WAKE_POISON_TOTAL_LIMIT,
    WAKE_REASON_CHANNEL_UNAVAILABLE,
    WAKE_REDELIVERY_GIVE_UP_SECONDS,
    WAKE_UNCOUNTED_STALL_SECONDS,
    WAKE_VERDICT_BATCH_FAILURE,
    WAKE_VERDICT_FAILURE,
    WAKE_VERDICT_NEUTRAL,
    WAKE_VERDICT_REDELIVERY_FAILURE,
    WakeAttemptVerdict,
    WakePoisonState,
    mark_stall_alerted,
    needs_isolation,
    next_poison_state,
    poison_backoff_seconds,
    quarantine_decision,
    redelivery_backoff_seconds,
    stall_alert,
    verdict_for_admission,
    verdict_for_batch,
    verdict_for_error,
    verdict_for_missing_report,
    verdict_for_report,
)
from agent_py_agent.agent.runtime_db.operations import (
    RuntimeExecutionBusyError,
    RuntimeRecoveryRequiredError,
)
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.agent.settings.model_provider_schema import ModelProfileError


def _failure(code: str) -> WakeAttemptVerdict:
    return WakeAttemptVerdict(WAKE_VERDICT_FAILURE, code)


def _feed(verdicts, *, start: float = 1000.0, step: float = 10.0) -> WakePoisonState:
    state = WakePoisonState()
    for index, verdict in enumerate(verdicts):
        state = next_poison_state(state, verdict, now=start + index * step)
    return state


def _sqlite_error(code: int | None) -> sqlite3.OperationalError:
    error = sqlite3.OperationalError("database is locked")
    if code is not None:
        error.sqlite_errorcode = code
    return error


# 合同清单写死在测试里：被测集合少一项或多一项都要让测试变红。
EXPECTED_WAITS = ("turn_interrupted", "terminal_task_link", "authority_recovery_required",
                  "wake_source_not_pending", "wake_source_changed")


class TestAdmission:
    @pytest.mark.parametrize("admission", EXPECTED_WAITS)
    def test_expected_waits_are_not_counted(self, admission):
        assert verdict_for_admission(admission) == WakeAttemptVerdict(WAKE_VERDICT_NEUTRAL, f"admission:{admission}")

    def test_expected_wait_set_is_exactly_the_contract(self):
        assert frozenset(EXPECTED_WAITS) == WAKE_EXPECTED_WAIT_ADMISSIONS

    @pytest.mark.parametrize("admission", ["host_delivery_consumed", "wake_source_unreadable", "future_code_v9"])
    def test_any_other_admission_counts_without_registration(self, admission):
        assert verdict_for_admission(admission) == _failure(f"admission:{admission}")

    def test_empty_admission_is_a_caller_bug(self):
        with pytest.raises(ValueError):
            verdict_for_admission("  ")


class TestError:
    @pytest.mark.parametrize("error", [
        ProviderTransientError("overloaded"),
        ProviderUsageLimitError("rate limited"),
        ProviderTimeoutError("slow", stage="first_event"),
        ProviderQuotaExhaustedError("quota"),
        ModelNotConfiguredError(),
        ModelProfileError("profile missing"),
        RuntimeExecutionBusyError("busy"),
        RuntimeRecoveryRequiredError("unknown run"),
        InterruptedError(),
        BlockingIOError(),
        TimeoutError(),
        BackgroundCompactSliceYield(),
        _sqlite_error(5),
        _sqlite_error(6),
        _sqlite_error(517),
        KeyboardInterrupt(),
    ], ids=lambda error: type(error).__name__)
    def test_transient_types_are_not_counted(self, error):
        verdict = verdict_for_error(error)
        assert verdict.kind == WAKE_VERDICT_NEUTRAL and verdict.reason_code.startswith("error:")

    @pytest.mark.parametrize(("error", "reason"), [
        (ProviderRequestRejectedError("expired key", status_code=401), "error:PROVIDER_REQUEST_REJECTED:http_401"),
        (ProviderRequestRejectedError("forbidden", status_code=403), "error:PROVIDER_REQUEST_REJECTED:http_403"),
        (ProviderRequestRejectedError("proxy auth", status_code=407), "error:PROVIDER_REQUEST_REJECTED:http_407"),
        (ProviderConnectionError("dns"), "error:PROVIDER_CONNECTION_FAILED"),
        (ProviderConfigurationError("bad endpoint"), "error:PROVIDER_CONFIGURATION_INVALID"),
    ], ids=["401", "403", "407", "connection", "configuration"])
    def test_environment_faults_are_not_counted(self, error, reason):
        assert verdict_for_error(error) == WakeAttemptVerdict(WAKE_VERDICT_NEUTRAL, reason)

    @pytest.mark.parametrize("status", [400, 413, 422])
    def test_request_level_rejections_still_count_with_their_status(self, status):
        error = ProviderRequestRejectedError("bad request", status_code=status)
        assert verdict_for_error(error) == _failure(f"error:PROVIDER_REQUEST_REJECTED:http_{status}")

    def test_base_exceptions_are_not_counted(self):
        assert verdict_for_error(KeyboardInterrupt()) == WakeAttemptVerdict(
            WAKE_VERDICT_NEUTRAL, "error:base_exception:KeyboardInterrupt")

    @pytest.mark.parametrize(("error", "code"), [
        (SkillSnapshotError("SKILL_TASK_BINDING_INVALID"), "error:SKILL_TASK_BINDING_INVALID"),
        (ProviderResponseError("empty", error_code="MODEL_EMPTY_RESPONSE"), "error:MODEL_EMPTY_RESPONSE"),
        (ProviderRequestRejectedError("400"), "error:PROVIDER_REQUEST_REJECTED"),
        (DataCorruptionError("bad ledger"), "error:data_corruption:DataCorruptionError"),
        (RuntimeError("boom"), "error:programmer_bug:RuntimeError"),
        (KeyError("x"), "error:programmer_bug:KeyError"),
        (_sqlite_error(None), "error:programmer_bug:OperationalError"),
        (_sqlite_error(1), "error:programmer_bug:OperationalError"),
    ], ids=lambda value: value if isinstance(value, str) else type(value).__name__)
    def test_other_errors_count_with_a_structured_code(self, error, code):
        assert verdict_for_error(error) == _failure(code)

    def test_message_text_never_changes_the_cause(self):
        first = verdict_for_error(ValueError("SKILL_TASK_BINDING_INVALID"))
        second = verdict_for_error(ValueError("something else entirely"))
        assert first == second and "SKILL_TASK_BINDING_INVALID" not in first.reason_code
        assert verdict_for_error(SkillSnapshotError("SKILL_TASK_BINDING_INVALID", "detail a")) == verdict_for_error(
            SkillSnapshotError("SKILL_TASK_BINDING_INVALID", "detail b"))


class TestReport:
    def test_handled_report_is_success(self):
        assert verdict_for_report(SimpleNamespace(wake_handled=True), delivery_frozen=False) == WAKE_ATTEMPT_SUCCESS

    def test_missing_report_and_unsettled_rerun_count(self):
        assert verdict_for_missing_report() == _failure("run:no_report")
        unsettled = SimpleNamespace(wake_handled=False)
        assert verdict_for_report(unsettled, delivery_frozen=False) == _failure("run:delivery_not_committed")

    def test_unsettled_with_frozen_delivery_is_a_redelivery_failure(self):
        verdict = verdict_for_report(SimpleNamespace(wake_handled=False), delivery_frozen=True)
        assert verdict == WakeAttemptVerdict(WAKE_VERDICT_REDELIVERY_FAILURE, WAKE_REASON_CHANNEL_UNAVAILABLE)


    def test_none_is_not_a_report(self):
        with pytest.raises(ValueError):
            verdict_for_report(None, delivery_frozen=False)


class TestErrorCodeShape:
    @pytest.mark.parametrize("code", ["   ", "lower_case", "HAS SPACE", "X detail=1", ""])
    def test_malformed_error_codes_fall_back_to_category_and_class(self, code):
        error = RuntimeError("boom")
        error.error_code = code
        assert verdict_for_error(error) == _failure("error:programmer_bug:RuntimeError")

    def test_surrounding_whitespace_is_trimmed_from_a_real_code(self):
        error = RuntimeError("boom")
        error.error_code = "  SKILL_TASK_BINDING_INVALID "
        assert verdict_for_error(error) == _failure("error:SKILL_TASK_BINDING_INVALID")


class TestStreak:
    def test_same_cause_quarantines_exactly_at_the_limit(self):
        almost = _feed([_failure("error:X")] * (WAKE_POISON_SAME_CAUSE_LIMIT - 1))
        assert quarantine_decision(almost) is None
        decision = quarantine_decision(next_poison_state(almost, _failure("error:X"), now=5000.0))
        assert decision is not None and decision.to_dict() == {
            "reason_code": "error:X", "same_cause_count": WAKE_POISON_SAME_CAUSE_LIMIT,
            "total_count": WAKE_POISON_SAME_CAUSE_LIMIT, "redelivery_failures": 0, "mixed_causes": False}

    def test_changed_cause_restarts_the_streak_but_total_keeps_counting(self):
        state = _feed([_failure("error:X")] * 4 + [_failure("error:Y")])
        assert (state.reason_code, state.same_cause_count, state.total_count) == ("error:Y", 1, 5)
        assert quarantine_decision(state) is None

    def test_neutral_results_neither_count_nor_break_the_streak(self):
        pattern = [_failure("error:X"), WAKE_ATTEMPT_NEUTRAL] * (WAKE_POISON_SAME_CAUSE_LIMIT - 1) + [_failure("error:X")]
        state = _feed(pattern)
        assert (state.same_cause_count, state.total_count) == (WAKE_POISON_SAME_CAUSE_LIMIT, WAKE_POISON_SAME_CAUSE_LIMIT)
        assert quarantine_decision(state) is not None

    def test_alternating_causes_hit_the_total_limit_as_mixed(self):
        alternating = [_failure("error:A"), _failure("error:B")] * (WAKE_POISON_TOTAL_LIMIT // 2)
        assert quarantine_decision(_feed(alternating[:-1])) is None
        decision = quarantine_decision(_feed(alternating))
        assert decision is not None and decision.mixed_causes is True
        assert (decision.reason_code, decision.total_count) == ("error:B", WAKE_POISON_TOTAL_LIMIT)

    def test_success_clears_everything(self):
        state = _feed([_failure("error:X")] * 4 + [WAKE_ATTEMPT_SUCCESS])
        assert state == WakePoisonState() and quarantine_decision(state) is None

    def test_unknown_verdict_kind_is_rejected(self):
        with pytest.raises(ValueError):
            next_poison_state(WakePoisonState(), WakeAttemptVerdict("mystery", "x"), now=1.0)

    def test_failure_timestamps_keep_the_first_and_move_the_last(self):
        state = next_poison_state(WakePoisonState(), _failure("error:X"), now=100.0)
        assert (state.first_failed_at, state.last_failed_at) == (100.0, 100.0)
        state = next_poison_state(state, _failure("error:Y"), now=250.0)
        assert (state.first_failed_at, state.last_failed_at) == (100.0, 250.0)
        state = next_poison_state(state, WAKE_ATTEMPT_NEUTRAL, now=400.0)
        assert (state.first_failed_at, state.last_failed_at, state.next_attempt_at) == (100.0, 250.0, 310.0)

    def test_abandoned_attempts_count_as_their_own_cause(self):
        state = _feed([WAKE_ATTEMPT_ABANDONED] * WAKE_POISON_SAME_CAUSE_LIMIT)
        assert quarantine_decision(state).reason_code == "attempt:abandoned"


class TestBatchIsolation:
    def test_only_single_member_failures_count(self):
        failure = _failure("error:X")
        assert verdict_for_batch(failure, 1) == failure
        assert verdict_for_batch(failure, 3) == WakeAttemptVerdict(WAKE_VERDICT_BATCH_FAILURE, "error:X")
        for other in (WAKE_ATTEMPT_NEUTRAL, WAKE_ATTEMPT_SUCCESS):
            assert verdict_for_batch(other, 3) == other

    @pytest.mark.parametrize("size", [0, -1, True, 2.0])
    def test_batch_size_must_be_a_positive_integer(self, size):
        with pytest.raises(ValueError):
            verdict_for_batch(_failure("error:X"), size)

    def test_batch_failures_isolate_the_next_attempt_but_never_quarantine(self):
        batch = WakeAttemptVerdict(WAKE_VERDICT_BATCH_FAILURE, "error:X")
        state = next_poison_state(WakePoisonState(), batch, now=100.0)
        assert (state.batch_failures, state.next_attempt_at, needs_isolation(state)) == (1, 130.0, True)
        assert (state.same_cause_count, state.total_count) == (0, 0)
        assert quarantine_decision(_feed([batch] * 50)) is None
        assert needs_isolation(WakePoisonState()) is False

    def test_isolated_failures_count_and_keep_isolating_until_success(self):
        state = _feed([WakeAttemptVerdict(WAKE_VERDICT_BATCH_FAILURE, "error:X")]
                      + [_failure("error:X")] * (WAKE_POISON_SAME_CAUSE_LIMIT - 1))
        assert needs_isolation(state) and quarantine_decision(state) is None
        assert quarantine_decision(next_poison_state(state, _failure("error:X"), now=9999.0)) is not None
        assert needs_isolation(next_poison_state(state, WAKE_ATTEMPT_SUCCESS, now=9999.0)) is False


class TestUncountedStall:
    WAIT = WakeAttemptVerdict(WAKE_VERDICT_NEUTRAL, "error:PROVIDER_CONNECTION_FAILED")

    def test_uncounted_run_tracks_count_start_and_latest_reason(self):
        state = next_poison_state(WakePoisonState(), self.WAIT, now=100.0)
        state = next_poison_state(state, WakeAttemptVerdict(WAKE_VERDICT_NEUTRAL, "admission:turn_interrupted"), now=200.0)
        assert (state.uncounted_count, state.uncounted_since, state.uncounted_reason_code) == (
            2, 100.0, "admission:turn_interrupted")
        assert (state.same_cause_count, state.total_count, state.next_attempt_at) == (0, 0, 0.0)

    def test_alert_fires_at_the_window_and_then_every_window(self):
        state = next_poison_state(WakePoisonState(), self.WAIT, now=0.0)
        assert stall_alert(state, now=WAKE_UNCOUNTED_STALL_SECONDS - 1) is None
        alert = stall_alert(state, now=WAKE_UNCOUNTED_STALL_SECONDS)
        assert alert is not None and alert.to_dict() == {
            "reason_code": "error:PROVIDER_CONNECTION_FAILED", "uncounted_count": 1, "uncounted_since": 0.0,
            "alert_number": 1}
        state = mark_stall_alerted(state, now=WAKE_UNCOUNTED_STALL_SECONDS)
        assert stall_alert(state, now=2 * WAKE_UNCOUNTED_STALL_SECONDS - 1) is None
        second = stall_alert(state, now=2 * WAKE_UNCOUNTED_STALL_SECONDS)
        assert second is not None and second.alert_number == 2

    def test_a_counted_failure_ends_the_run_and_its_alerts_but_other_kinds_do_not(self):
        state = mark_stall_alerted(next_poison_state(WakePoisonState(), self.WAIT, now=0.0), now=5.0)
        for kind in (WAKE_VERDICT_BATCH_FAILURE, WAKE_VERDICT_REDELIVERY_FAILURE):
            kept = next_poison_state(state, WakeAttemptVerdict(kind, "x"), now=10.0)
            assert (kept.uncounted_count, kept.stall_alerts) == (1, 1)
        ended = next_poison_state(state, _failure("error:X"), now=10.0)
        assert (ended.uncounted_count, ended.uncounted_since, ended.uncounted_reason_code,
                ended.stall_alerts, ended.last_stall_alert_at) == (0, 0.0, "", 0, 0.0)

    def test_the_alert_window_is_the_redelivery_window_of_24_hours(self):
        assert WAKE_UNCOUNTED_STALL_SECONDS == WAKE_REDELIVERY_GIVE_UP_SECONDS == 24 * 3600

    def test_uncounted_results_never_quarantine(self):
        assert quarantine_decision(_feed([self.WAIT] * 500)) is None


class TestBackoff:
    def test_counted_failures_back_off_30_60_120_240_then_cap(self):
        assert [poison_backoff_seconds(n) for n in range(0, 8)] == [0.0, 30.0, 60.0, 120.0, 240.0, 300.0, 300.0, 300.0]
        state = next_poison_state(WakePoisonState(), _failure("error:X"), now=100.0)
        assert state.next_attempt_at == 130.0
        assert next_poison_state(state, _failure("error:X"), now=200.0).next_attempt_at == 260.0

    def test_backoff_grows_with_total_failures_even_when_causes_alternate(self):
        state = next_poison_state(WakePoisonState(), _failure("error:A"), now=100.0)
        state = next_poison_state(state, _failure("error:B"), now=200.0)
        assert (state.same_cause_count, state.next_attempt_at) == (1, 260.0)

    def test_redelivery_next_attempt_advances_with_each_failure(self):
        redelivery = WakeAttemptVerdict(WAKE_VERDICT_REDELIVERY_FAILURE, WAKE_REASON_CHANNEL_UNAVAILABLE)
        first = next_poison_state(WakePoisonState(), redelivery, now=1000.0)
        assert (first.next_attempt_at, first.first_redelivery_failed_at, first.last_redelivery_failed_at) == (
            1030.0, 1000.0, 1000.0)
        second = next_poison_state(first, redelivery, now=1100.0)
        assert (second.next_attempt_at, second.first_redelivery_failed_at, second.last_redelivery_failed_at) == (
            1160.0, 1000.0, 1100.0)

    def test_redelivery_uses_its_own_backoff_not_the_failure_one(self):
        redelivery = WakeAttemptVerdict(WAKE_VERDICT_REDELIVERY_FAILURE, WAKE_REASON_CHANNEL_UNAVAILABLE)
        state = _feed([redelivery] * 6, start=0.0, step=10.0)
        assert state.next_attempt_at == 50.0 + 900.0

    def test_redelivery_backs_off_to_a_fifteen_minute_cap(self):
        assert [redelivery_backoff_seconds(n) for n in (1, 2, 5, 6, 7, 60)] == [30.0, 60.0, 480.0, 900.0, 900.0, 900.0]


class TestRedelivery:
    def test_redelivery_failures_do_not_touch_the_failure_counts(self):
        redelivery = WakeAttemptVerdict(WAKE_VERDICT_REDELIVERY_FAILURE, WAKE_REASON_CHANNEL_UNAVAILABLE)
        state = _feed([redelivery] * 50)
        assert (state.same_cause_count, state.total_count, state.redelivery_failures) == (0, 0, 50)
        assert quarantine_decision(state) is None

    def test_redelivery_quarantines_once_the_window_reaches_24_hours(self):
        redelivery = WakeAttemptVerdict(WAKE_VERDICT_REDELIVERY_FAILURE, WAKE_REASON_CHANNEL_UNAVAILABLE)
        first = next_poison_state(WakePoisonState(), redelivery, now=0.0)
        just_before = next_poison_state(first, redelivery, now=WAKE_REDELIVERY_GIVE_UP_SECONDS - 1)
        assert quarantine_decision(just_before) is None
        at_limit = next_poison_state(just_before, redelivery, now=WAKE_REDELIVERY_GIVE_UP_SECONDS)
        decision = quarantine_decision(at_limit)
        assert decision is not None and decision.reason_code == "delivery:channel_unavailable"
        assert (decision.redelivery_failures, decision.mixed_causes) == (3, False)


class TestStateShape:
    def test_round_trip(self):
        state = _feed([_failure("error:X"), _failure("error:X")])
        assert WakePoisonState.from_dict(state.to_dict()) == state

    # 类型用例都建立在一份其它字段完全自洽的状态上，确保拦下它的是类型检查而不是一致性检查。
    CONSISTENT = {"reason_code": "error:X", "same_cause_count": 1, "total_count": 2,
                  "first_failed_at": 1.0, "last_failed_at": 2.0}

    def test_the_consistent_base_is_accepted(self):
        assert WakePoisonState.from_dict({**WakePoisonState().to_dict(), **self.CONSISTENT}).total_count == 2

    @pytest.mark.parametrize("patch", [
        {"same_cause_count": True}, {"total_count": 1.5}, {"reason_code": 3}, {"next_attempt_at": "soon"},
        {"first_failed_at": -2.0}, {"last_failed_at": True}, {"redelivery_failures": -1},
    ])
    def test_bad_fields_are_rejected(self, patch):
        with pytest.raises(ValueError):
            WakePoisonState.from_dict({**WakePoisonState().to_dict(), **self.CONSISTENT, **patch})

    @pytest.mark.parametrize("patch", [
        {"last_failed_at": float("nan")}, {"next_attempt_at": float("inf")}, {"surprise": 1},
        {"same_cause_count": 3, "total_count": 2, "reason_code": "error:X", "first_failed_at": 1.0, "last_failed_at": 2.0},
        {"same_cause_count": 1, "total_count": 1, "reason_code": "", "first_failed_at": 1.0, "last_failed_at": 1.0},
        {"reason_code": "error:X"},
        {"total_count": 1, "same_cause_count": 1, "reason_code": "error:X", "first_failed_at": 5.0, "last_failed_at": 4.0},
        {"first_failed_at": 3.0},
        {"redelivery_failures": 1, "first_redelivery_failed_at": 9.0, "last_redelivery_failed_at": 8.0},
        {"last_redelivery_failed_at": 2.0},
        {"uncounted_reason_code": "error:X"},
        {"stall_alerts": 1},
        {"uncounted_count": 1, "uncounted_since": 5.0, "last_stall_alert_at": 6.0},
        {"uncounted_count": 1, "uncounted_since": 5.0, "stall_alerts": 1, "last_stall_alert_at": 4.0},
    ], ids=["nan", "inf", "unknown-key", "same-over-total", "count-without-code", "code-without-count",
            "first-after-last", "time-without-failure", "redelivery-first-after-last", "redelivery-time-without-failure",
            "uncounted-reason-without-run", "alerts-without-run", "alert-time-without-alert", "alert-before-run"])
    def test_non_finite_unknown_or_contradictory_states_are_rejected(self, patch):
        with pytest.raises(ValueError):
            WakePoisonState.from_dict({**WakePoisonState().to_dict(), **patch})

    def test_non_object_is_rejected(self):
        with pytest.raises(ValueError):
            WakePoisonState.from_dict(["not", "a", "dict"])


class TestReplaysOfTodaysBadBuilds:
    def test_7b83c8730_programmer_bug_stops_after_five_claims(self):
        error = SkillSnapshotError("SKILL_TASK_BINDING_INVALID")
        state, now, claims = WakePoisonState(), 0.0, 0
        while quarantine_decision(state) is None and claims < 100:
            now = max(now, state.next_attempt_at)
            state = next_poison_state(state, verdict_for_error(error), now=now)
            claims += 1
        assert claims == WAKE_POISON_SAME_CAUSE_LIMIT and now == 30 + 60 + 120 + 240
        assert quarantine_decision(state).reason_code == "error:SKILL_TASK_BINDING_INVALID"

    def test_204f4ddf9_nonexecuted_claim_stops_after_five_claims(self):
        state = _feed([verdict_for_admission("host_delivery_consumed")] * WAKE_POISON_SAME_CAUSE_LIMIT)
        assert quarantine_decision(state).reason_code == "admission:host_delivery_consumed"
