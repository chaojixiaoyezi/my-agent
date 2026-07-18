from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent_py_agent.agent.scheduler.schedule import (
    ScheduleValidationError,
    build_schedule,
    compute_next_run,
    next_cron_timestamp,
    parse_cron,
    parse_instant,
)


def _ts(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def test_absolute_and_interval_schedules_are_structured_and_timezone_aware() -> None:
    once = build_schedule(
        kind="at",
        at="2026-07-19T09:30:00",
        timezone_name="Asia/Shanghai",
        now=0,
    )
    assert once == {
        "kind": "at",
        "at": "2026-07-19T01:30:00Z",
        "timezone": "Asia/Shanghai",
    }
    assert compute_next_run(once, after=_ts("2026-07-19T01:00:00Z")) == _ts(
        "2026-07-19T01:30:00Z"
    )
    assert compute_next_run(once, after=_ts("2026-07-19T01:30:00Z")) is None

    every = build_schedule(
        kind="every",
        every_seconds=600,
        anchor_at=1_000,
        timezone_name="UTC",
        now=900,
    )
    assert compute_next_run(every, after=900) == 1_000
    assert compute_next_run(every, after=1_000) == 1_600
    assert compute_next_run(every, after=2_199) == 2_200


def test_cron_uses_vixie_day_semantics_and_full_range_steps_are_wildcards() -> None:
    fields = parse_cron("0 9 1 * mon")
    assert fields.date_matches(datetime(2026, 7, 1).date())
    assert fields.date_matches(datetime(2026, 7, 6).date())
    assert not fields.date_matches(datetime(2026, 7, 7).date())

    all_days = parse_cron("0 9 */1 * */1")
    assert all_days.day_of_month_wildcard is True
    assert all_days.day_of_week_wildcard is True


def test_cron_skips_dst_gap_and_emits_both_fall_back_folds() -> None:
    with pytest.raises(ScheduleValidationError, match="does not exist"):
        parse_instant("2026-03-08T02:30:00", timezone_name="America/Los_Angeles")

    before_fold = _ts("2026-11-01T07:00:00Z")
    first = next_cron_timestamp(
        "30 1 1 11 *",
        timezone_name="America/Los_Angeles",
        after=before_fold,
    )
    second = next_cron_timestamp(
        "30 1 1 11 *",
        timezone_name="America/Los_Angeles",
        after=first,
    )
    assert datetime.fromtimestamp(first, timezone.utc).isoformat() == "2026-11-01T08:30:00+00:00"
    assert datetime.fromtimestamp(second, timezone.utc).isoformat() == "2026-11-01T09:30:00+00:00"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": "every", "every_seconds": 59},
        {"kind": "cron", "cron": "0 0 * *"},
        {"kind": "cron", "cron": "0 0 * * *", "timezone_name": "Mars/Olympus"},
        {"kind": "at", "at": float("inf")},
    ],
)
def test_invalid_schedule_shapes_fail_closed(kwargs: dict[str, object]) -> None:
    with pytest.raises(ScheduleValidationError):
        build_schedule(now=0, **kwargs)
