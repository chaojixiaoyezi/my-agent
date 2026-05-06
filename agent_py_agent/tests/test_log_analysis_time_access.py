from __future__ import annotations

"""Tests for log_analysis.analytics.detectors.field_access module."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from agent_py_agent.agent.log_analysis.analytics.detectors.field_access import (
    SUSPICIOUS_CHILD_PROCESSES,
    WEB_PARENT_PROCESSES,
    _basename,
    _canonical_time,
    _clamp_float,
    _event_dict,
    _event_time,
    _field,
    _parse_time,
    _path_value,
    _present,
    _sort_time,
    _text,
    _time_bucket,
    _to_float,
    _to_int,
    _truthy,
    _window_for_events,
    _within_after,
    _within_before,
)

# ---------------------------------------------------------------------------
# _event_dict tests
# ---------------------------------------------------------------------------

class TestParseTime:
    def test_iso_string_with_z(self) -> None:
        result = _parse_time("2024-01-15T10:30:00Z")
        assert result is not None
        assert result.year == 2024
        assert result.month == 1
        assert result.tzinfo is not None

    def test_iso_string_with_offset(self) -> None:
        result = _parse_time("2024-01-15T10:30:00+00:00")
        assert result is not None
        assert result.year == 2024

    def test_naive_datetime_gets_utc(self) -> None:
        dt = datetime(2024, 1, 15, 10, 30)
        result = _parse_time(dt)
        assert result is not None
        assert result.tzinfo == timezone.utc

    def test_aware_datetime_preserved(self) -> None:
        dt = datetime(2024, 1, 15, 10, 30, tzinfo=timezone(timedelta(hours=5)))
        result = _parse_time(dt)
        assert result is not None
        assert result.utcoffset() == timedelta(hours=5)

    def test_epoch_int(self) -> None:
        result = _parse_time(1705312200)
        assert result is not None
        assert result.year == 2024

    def test_epoch_float(self) -> None:
        result = _parse_time(1705312200.0)
        assert result is not None

    def test_none_returns_none(self) -> None:
        assert _parse_time(None) is None

    def test_bool_returns_none(self) -> None:
        assert _parse_time(True) is None
        assert _parse_time(False) is None

    def test_invalid_string_returns_none(self) -> None:
        assert _parse_time("not-a-date") is None

    def test_empty_string_returns_none(self) -> None:
        assert _parse_time("") is None

class TestCanonicalTime:
    def test_utc_datetime(self) -> None:
        dt = datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        assert _canonical_time(dt) == "2024-01-15T10:30:00Z"

    def test_non_utc_converted(self) -> None:
        dt = datetime(2024, 1, 15, 15, 30, 0, tzinfo=timezone(timedelta(hours=5)))
        result = _canonical_time(dt)
        assert result.endswith("Z")
        assert "10:30:00Z" in result

    def test_none_returns_empty(self) -> None:
        assert _canonical_time(None) == ""

class TestEventTime:
    def test_event_time_field(self) -> None:
        ev = {"event_time": "2024-01-15T10:30:00Z"}
        result = _event_time(ev)
        assert result is not None
        assert result.year == 2024

    def test_timestamp_field(self) -> None:
        ev = {"@timestamp": "2024-01-15T10:30:00Z"}
        result = _event_time(ev)
        assert result is not None

    def test_created_at_field(self) -> None:
        ev = {"created_at": "2024-01-15T10:30:00Z"}
        result = _event_time(ev)
        assert result is not None

    def test_no_time_field_returns_none(self) -> None:
        assert _event_time({"foo": "bar"}) is None

class TestSortTime:
    def test_timed_event_sorts_first(self) -> None:
        ev = {"event_time": "2024-01-15T10:30:00Z"}
        key = _sort_time(ev)
        assert key[0] == 0

    def test_un_timed_event_sorts_last(self) -> None:
        ev = {"foo": "bar"}
        key = _sort_time(ev)
        assert key[0] == 1
        assert key[1] == ""

class TestWithinAfter:
    def test_candidate_within_window(self) -> None:
        start = {"event_time": "2024-01-15T10:00:00Z"}
        cand = {"event_time": "2024-01-15T10:05:00Z"}
        assert _within_after(start, cand, 10) is True

    def test_candidate_outside_window(self) -> None:
        start = {"event_time": "2024-01-15T10:00:00Z"}
        cand = {"event_time": "2024-01-15T10:20:00Z"}
        assert _within_after(start, cand, 10) is False

    def test_candidate_before_start(self) -> None:
        start = {"event_time": "2024-01-15T10:00:00Z"}
        cand = {"event_time": "2024-01-15T09:50:00Z"}
        assert _within_after(start, cand, 10) is False

    def test_missing_time_returns_false(self) -> None:
        start = {"event_time": "2024-01-15T10:00:00Z"}
        assert _within_after(start, {"foo": "bar"}, 10) is False

    def test_both_missing_returns_false(self) -> None:
        assert _within_after({"a": 1}, {"b": 2}, 10) is False

    def test_exact_boundary(self) -> None:
        start = {"event_time": "2024-01-15T10:00:00Z"}
        cand = {"event_time": "2024-01-15T10:10:00Z"}
        assert _within_after(start, cand, 10) is True

class TestWithinBefore:
    def test_candidate_within_window(self) -> None:
        end = {"event_time": "2024-01-15T10:10:00Z"}
        cand = {"event_time": "2024-01-15T10:05:00Z"}
        assert _within_before(cand, end, 10) is True

    def test_candidate_outside_window(self) -> None:
        end = {"event_time": "2024-01-15T10:30:00Z"}
        cand = {"event_time": "2024-01-15T10:00:00Z"}
        assert _within_before(cand, end, 10) is False

    def test_missing_time_returns_false(self) -> None:
        end = {"event_time": "2024-01-15T10:10:00Z"}
        assert _within_before({"foo": "bar"}, end, 10) is False

class TestWindowForEvents:
    def test_empty_events(self) -> None:
        start, end = _window_for_events([])
        assert isinstance(start, str)
        assert isinstance(end, str)
        assert start == end

    def test_single_event(self) -> None:
        ev = {"event_time": "2024-01-15T10:30:00Z"}
        start, end = _window_for_events([ev])
        assert start == end
        assert "2024-01-15" in start

    def test_multiple_events_sorted(self) -> None:
        ev1 = {"event_time": "2024-01-15T12:00:00Z"}
        ev2 = {"event_time": "2024-01-15T08:00:00Z"}
        ev3 = {"event_time": "2024-01-15T10:00:00Z"}
        start, end = _window_for_events([ev1, ev2, ev3])
        assert "08:00:00" in start
        assert "12:00:00" in end

    def test_events_without_time_ignored(self) -> None:
        ev1 = {"event_time": "2024-01-15T10:00:00Z"}
        ev2 = {"foo": "bar"}
        start, end = _window_for_events([ev1, ev2])
        assert start == end

class TestTimeBucket:
    def test_none_returns_unknown(self) -> None:
        assert _time_bucket(None, 5) == "unknown-time"

    def test_bucketing_5min(self) -> None:
        dt = datetime(2024, 1, 15, 10, 7, 30, tzinfo=timezone.utc)
        result = _time_bucket(dt, 5)
        assert "10:05:00" in result

    def test_bucketing_15min(self) -> None:
        dt = datetime(2024, 1, 15, 10, 22, 0, tzinfo=timezone.utc)
        result = _time_bucket(dt, 15)
        assert "10:15:00" in result

    def test_bucketing_60min(self) -> None:
        dt = datetime(2024, 1, 15, 10, 45, 0, tzinfo=timezone.utc)
        result = _time_bucket(dt, 60)
        assert "10:00:00" in result

    def test_zero_minutes_uses_one(self) -> None:
        dt = datetime(2024, 1, 15, 10, 5, 0, tzinfo=timezone.utc)
        result = _time_bucket(dt, 0)
        # With max(minutes, 1) = 1, minute 5 // 1 * 1 = 5
        assert "10:05:00" in result

class TestConstants:
    def test_web_parent_processes_not_empty(self) -> None:
        assert len(WEB_PARENT_PROCESSES) > 0

    def test_suspicious_child_processes_not_empty(self) -> None:
        assert len(SUSPICIOUS_CHILD_PROCESSES) > 0

    def test_web_parents_are_lowercase(self) -> None:
        for proc in WEB_PARENT_PROCESSES:
            assert proc == proc.lower()

    def test_suspicious_children_are_lowercase(self) -> None:
        for proc in SUSPICIOUS_CHILD_PROCESSES:
            assert proc == proc.lower()

    def test_known_web_parents_present(self) -> None:
        assert "nginx" in WEB_PARENT_PROCESSES
        assert "apache" in WEB_PARENT_PROCESSES

    def test_known_suspicious_children_present(self) -> None:
        assert "bash" in SUSPICIOUS_CHILD_PROCESSES
        assert "powershell.exe" in SUSPICIOUS_CHILD_PROCESSES
