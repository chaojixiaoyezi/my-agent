
from __future__ import annotations

from ...settings.runtime_guard_config import runtime_guard_bool, runtime_guard_int

DEFAULT_REPEAT_FAIL_THRESHOLD = 10
DEFAULT_READONLY_NO_PROGRESS_THRESHOLD = 3
# L2 阶梯:同工具同类失败连续达该值即收口并自动续跑(不再跟随 guardrail 3N DENY)。
DEFAULT_REPEATED_FAILURE_HALT_THRESHOLD = 8
# L4 阶梯:真硬门默认关;开启后同类失败达该值强制收口等用户介入。
DEFAULT_HARD_FAILURE_HALT_THRESHOLD = 15


def repeat_fail_threshold(params: object) -> int:
    attrs = getattr(params, "task_attributes", None)
    if isinstance(attrs, dict):
        parsed = _int_value(attrs.get("repeat_fail_threshold"))
        if parsed >= 0:
            return parsed
    return runtime_guard_int(
        "repeat_fail_threshold",
        DEFAULT_REPEAT_FAIL_THRESHOLD,
        policy=getattr(params, "runtime_guard_policy", None),
    )


def readonly_no_progress_threshold(params: object) -> int:
    attrs = getattr(params, "task_attributes", None)
    if isinstance(attrs, dict):
        parsed = _int_value(attrs.get("readonly_no_progress_threshold"))
        if parsed >= 0:
            return parsed
    return runtime_guard_int(
        "readonly_no_progress_threshold",
        DEFAULT_READONLY_NO_PROGRESS_THRESHOLD,
        policy=getattr(params, "runtime_guard_policy", None),
    )


def terminal_block_enabled(params: object) -> bool:
    attrs = getattr(params, "task_attributes", None)
    if isinstance(attrs, dict) and "terminal_block_enabled" in attrs:
        value = attrs.get("terminal_block_enabled")
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value or "").strip().lower()
        return text in {"1", "true"}
    else:
        return runtime_guard_bool(
            "terminal_block_enabled",
            False,
            policy=getattr(params, "runtime_guard_policy", None),
        )


def repeated_failure_halt_threshold(params: object) -> int:
    """L2 收口阈值:同工具同类失败连续达该值,本轮收口并自动续跑。"""
    attrs = getattr(params, "task_attributes", None)
    if isinstance(attrs, dict):
        parsed = _int_value(attrs.get("repeated_failure_halt_threshold"))
        if parsed >= 0:
            return parsed
    return runtime_guard_int(
        "repeated_failure_halt_threshold",
        DEFAULT_REPEATED_FAILURE_HALT_THRESHOLD,
        policy=getattr(params, "runtime_guard_policy", None),
    )


def hard_failure_halt_enabled(params: object) -> bool:
    """L4 真硬门开关:默认关闭,开启后同类失败达硬阈值即强制收口等用户。"""
    attrs = getattr(params, "task_attributes", None)
    if isinstance(attrs, dict) and "hard_failure_halt_enabled" in attrs:
        value = attrs.get("hard_failure_halt_enabled")
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value or "").strip().lower()
        return text in {"1", "true"}
    return runtime_guard_bool(
        "hard_failure_halt_enabled",
        False,
        policy=getattr(params, "runtime_guard_policy", None),
    )


def hard_failure_halt_threshold(params: object) -> int:
    """L4 硬门阈值:开启后同类失败达该值强制收口。"""
    attrs = getattr(params, "task_attributes", None)
    if isinstance(attrs, dict):
        parsed = _int_value(attrs.get("hard_failure_halt_threshold"))
        if parsed >= 0:
            return parsed
    return runtime_guard_int(
        "hard_failure_halt_threshold",
        DEFAULT_HARD_FAILURE_HALT_THRESHOLD,
        policy=getattr(params, "runtime_guard_policy", None),
    )


def _int_value(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1
