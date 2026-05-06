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

class TestEventDict:
    def test_plain_dict_passthrough(self) -> None:
        d = {"a": 1, "b": 2}
        assert _event_dict(d) == d

    def test_dict_is_copy(self) -> None:
        d = {"a": 1}
        result = _event_dict(d)
        result["b"] = 2
        assert "b" not in d

    def test_dataclass_conversion(self) -> None:
        @dataclass
        class Ev:
            x: int = 10
            y: str = "hello"

        result = _event_dict(Ev())
        assert result == {"x": 10, "y": "hello"}

    def test_object_with_to_dict(self) -> None:
        class Obj:
            def to_dict(self) -> dict[str, Any]:
                return {"k": "v"}

        assert _event_dict(Obj()) == {"k": "v"}

    def test_object_with_to_dict_returns_non_mapping(self) -> None:
        class Obj:
            def to_dict(self) -> list:
                return [1, 2]

        result = _event_dict(Obj())
        # to_dict returned non-mapping, falls through to vars()
        assert isinstance(result, dict)

    def test_plain_object_via_vars(self) -> None:
        class Obj:
            def __init__(self) -> None:
                self.a = 1
                self.b = 2

        result = _event_dict(Obj())
        assert result["a"] == 1
        assert result["b"] == 2

    def test_scalar_fallback(self) -> None:
        result = _event_dict(42)
        assert result == {"value": 42}


# ---------------------------------------------------------------------------
# _present tests
# ---------------------------------------------------------------------------

class TestPresent:
    def test_none_is_not_present(self) -> None:
        assert _present(None) is False

    def test_empty_string_not_present(self) -> None:
        assert _present("") is False

    def test_empty_list_not_present(self) -> None:
        assert _present([]) is False

    def test_empty_dict_not_present(self) -> None:
        assert _present({}) is False

    def test_nonempty_string_is_present(self) -> None:
        assert _present("x") is True

    def test_zero_is_present(self) -> None:
        assert _present(0) is True

    def test_false_is_present(self) -> None:
        assert _present(False) is True

    def test_nonempty_list_is_present(self) -> None:
        assert _present([1]) is True


# ---------------------------------------------------------------------------
# _text tests
# ---------------------------------------------------------------------------

class TestText:
    def test_none_returns_empty(self) -> None:
        assert _text(None) == ""

    def test_bool_true(self) -> None:
        assert _text(True) == "true"

    def test_bool_false(self) -> None:
        assert _text(False) == "false"

    def test_int(self) -> None:
        assert _text(42) == "42"

    def test_strips_whitespace(self) -> None:
        assert _text("  hello  ") == "hello"

    def test_empty_string(self) -> None:
        assert _text("") == ""


# ---------------------------------------------------------------------------
# _truthy tests
# ---------------------------------------------------------------------------

class TestTruthy:
    def test_bool_true(self) -> None:
        assert _truthy(True) is True

    def test_bool_false(self) -> None:
        assert _truthy(False) is False

    def test_int_zero(self) -> None:
        assert _truthy(0) is False

    def test_int_nonzero(self) -> None:
        assert _truthy(5) is True

    def test_float_zero(self) -> None:
        assert _truthy(0.0) is False

    def test_string_true(self) -> None:
        assert _truthy("true") is True

    def test_string_yes(self) -> None:
        assert _truthy("yes") is True

    def test_string_1(self) -> None:
        assert _truthy("1") is True

    def test_string_new(self) -> None:
        assert _truthy("new") is True

    def test_string_rare(self) -> None:
        assert _truthy("rare") is True

    def test_string_unusual(self) -> None:
        assert _truthy("unusual") is True

    def test_string_no(self) -> None:
        assert _truthy("no") is False

    def test_empty_string(self) -> None:
        assert _truthy("") is False

    def test_none(self) -> None:
        assert _truthy(None) is False


# ---------------------------------------------------------------------------
# _to_int tests
# ---------------------------------------------------------------------------

class TestToInt:
    def test_int(self) -> None:
        assert _to_int(42) == 42

    def test_string_int(self) -> None:
        assert _to_int("80") == 80

    def test_float_truncated(self) -> None:
        assert _to_int(3.7) == 3

    def test_bool_returns_none(self) -> None:
        assert _to_int(True) is None

    def test_none_returns_none(self) -> None:
        assert _to_int(None) is None

    def test_invalid_string_returns_none(self) -> None:
        assert _to_int("abc") is None

    def test_empty_string_returns_none(self) -> None:
        assert _to_int("") is None


# ---------------------------------------------------------------------------
# _to_float tests
# ---------------------------------------------------------------------------

class TestToFloat:
    def test_float(self) -> None:
        assert _to_float(3.14) == pytest.approx(3.14)

    def test_int(self) -> None:
        assert _to_float(42) == pytest.approx(42.0)

    def test_string_float(self) -> None:
        assert _to_float("2.5") == pytest.approx(2.5)

    def test_bool_returns_none(self) -> None:
        assert _to_float(True) is None

    def test_none_returns_none(self) -> None:
        assert _to_float(None) is None

    def test_invalid_string_returns_none(self) -> None:
        assert _to_float("abc") is None


# ---------------------------------------------------------------------------
# _clamp_float tests
# ---------------------------------------------------------------------------

class TestClampFloat:
    def test_zero(self) -> None:
        assert _clamp_float(0) == 0.0

    def test_one(self) -> None:
        assert _clamp_float(1) == 1.0

    def test_above_one_clamped(self) -> None:
        assert _clamp_float(1.5) == 1.0

    def test_below_zero_clamped(self) -> None:
        assert _clamp_float(-0.5) == 0.0

    def test_mid_value(self) -> None:
        assert _clamp_float(0.6) == pytest.approx(0.6)

    def test_none_returns_zero(self) -> None:
        assert _clamp_float(None) == 0.0

    def test_invalid_string_returns_zero(self) -> None:
        assert _clamp_float("abc") == 0.0

    def test_string_number(self) -> None:
        assert _clamp_float("0.8") == pytest.approx(0.8)


# ---------------------------------------------------------------------------
# _path_value tests
# ---------------------------------------------------------------------------

class TestPathValue:
    def test_top_level_key(self) -> None:
        assert _path_value({"foo": 42}, "foo") == 42

    def test_case_insensitive_key(self) -> None:
        assert _path_value({"Foo": 42}, "foo") == 42

    def test_dotted_path(self) -> None:
        d = {"process": {"name": "bash"}}
        assert _path_value(d, "process.name") == "bash"

    def test_dotted_path_case_insensitive(self) -> None:
        d = {"Process": {"Name": "bash"}}
        assert _path_value(d, "process.name") == "bash"

    def test_missing_key_returns_none(self) -> None:
        assert _path_value({"a": 1}, "b") is None

    def test_missing_nested_key_returns_none(self) -> None:
        assert _path_value({"a": 1}, "a.b") is None

    def test_non_mapping_intermediate_returns_none(self) -> None:
        assert _path_value({"a": 42}, "a.b") is None


# ---------------------------------------------------------------------------
# _field tests
# ---------------------------------------------------------------------------

class TestField:
    def test_top_level_match(self) -> None:
        assert _field({"src_ip": "1.2.3.4"}, "src_ip") == "1.2.3.4"

    def test_alternative_names(self) -> None:
        assert _field({"source_ip": "1.2.3.4"}, "src_ip", "source_ip") == "1.2.3.4"

    def test_nested_in_attributes(self) -> None:
        d = {"attributes": {"src_ip": "10.0.0.1"}}
        assert _field(d, "src_ip") == "10.0.0.1"

    def test_nested_in_process(self) -> None:
        d = {"process": {"name": "bash"}}
        assert _field(d, "name") == "bash"

    def test_empty_value_skipped(self) -> None:
        d = {"src_ip": "", "source_ip": "1.2.3.4"}
        assert _field(d, "src_ip", "source_ip") == "1.2.3.4"

    def test_missing_returns_none(self) -> None:
        assert _field({"a": 1}, "missing") is None


# ---------------------------------------------------------------------------
# _basename tests
# ---------------------------------------------------------------------------

class TestBasename:
    def test_simple_name(self) -> None:
        assert _basename("bash") == "bash"

    def test_unix_path(self) -> None:
        assert _basename("/usr/bin/bash") == "bash"

    def test_windows_path(self) -> None:
        assert _basename("C:\\Windows\\cmd.exe") == "cmd.exe"

    def test_mixed_slashes(self) -> None:
        assert _basename("C:\\Windows/system32/cmd.exe") == "cmd.exe"

    def test_uppercase_lowercased(self) -> None:
        assert _basename("CMD.EXE") == "cmd.exe"

    def test_none_returns_empty(self) -> None:
        assert _basename(None) == ""

    def test_trailing_slash(self) -> None:
        assert _basename("/usr/bin/") == ""


# ---------------------------------------------------------------------------
# _parse_time tests
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# _canonical_time tests
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# _event_time tests
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# _sort_time tests
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# _within_after / _within_before tests
# ---------------------------------------------------------------------------





# ---------------------------------------------------------------------------
# _window_for_events tests
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# _time_bucket tests
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Constants tests
# ---------------------------------------------------------------------------
