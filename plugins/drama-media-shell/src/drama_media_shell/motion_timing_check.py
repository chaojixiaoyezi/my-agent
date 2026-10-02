# LLM: 固定迁移 drama-skills VID-04 时序算术；SDK 入口先安全读取 JSONL，本模块保持纯计算且不碰文件系统。
# 模块用途: 核对显式运动分段的溢出、短缺、重叠和时长投影，不生成媒体。
"""Check explicit motion segment timing against accepted shot duration (`VID-04`).

`VID-04` is arithmetic, and it is violated in two directions that fail
differently:

* **Overflow** — segments extend past the accepted duration. Whatever falls
  outside is truncated, taking the closing action, the verbatim dialogue tail
  and the end pose with it.
* **Shortfall** — segments stop short. The unallocated remainder does not
  render as a held frame; the execution end fills it with motion, expression or
  camera movement that has no upstream source at all.

Both are the same rule, so both are reported under the same ID with distinct
diagnostic codes. Relative timing plans are out of scope by contract: only a
plan that declares itself `explicit` makes a checkable arithmetic claim.

The script reads accepted creator files and writes nothing.
"""

from __future__ import annotations

import math
import re
from typing import Any

SCHEMA_VERSION = "1.0.0"
# A .jsonl file opens with a header record declaring the upstream snapshots its
# references name. The header is a declaration, not one of the file's records.
SOURCES_RECORD_TYPE = "sources"
# Seconds are authored by hand, so compare with a tolerance rather than by
# equality: 0.1 + 0.2 != 0.3 in binary floating point, and a rule that fires on
# that would be noise. A millisecond is far below anything a shot can express.
TOLERANCE_SECONDS = 1e-6
# An interval reads `0.0-1.5`. Accept the en dash and the CJK wave dash too:
# creators type these on Chinese input methods and the distinction carries no
# meaning here.
INTERVAL_RE = re.compile(
    r"^\s*(?P<start>\d+(?:\.\d+)?)\s*[-–—~～]\s*(?P<end>\d+(?:\.\d+)?)\s*(?:s|秒)?\s*$"
)


# LLM: 该入口迁移自固定上游校验器；保持原字段、错误与数值语义，只消费调用方已安全读取的数据。
# 类用途: 表示运动或镜头输入无法执行检查。
class CheckError(ValueError):
    """The inputs cannot be checked at all, as opposed to failing a check."""


# LLM: 该入口迁移自固定上游校验器；保持原字段、错误与数值语义，只消费调用方已安全读取的数据。
# 函数用途: 把有限 JSON 数值转换为浮点数。
def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


# LLM: 该入口迁移自固定上游校验器；保持原字段、错误与数值语义，只消费调用方已安全读取的数据。
# 函数用途: 构造带 VID-04 规则身份的检查发现。
def _finding(code: str, motion_id: str, message: str, **extra: Any) -> dict[str, Any]:
    return {
        "code": code,
        "rule": "VID-04",
        "severity": "error",
        "motion_id": motion_id,
        "message": message,
        **extra,
    }


# LLM: 该入口迁移自固定上游校验器；保持原字段、错误与数值语义，只消费调用方已安全读取的数据。
# 函数用途: 解析显式秒数区间。
def _parse_interval(value: Any) -> tuple[float, float] | None:
    """Return an explicit ``(start, end)`` window, or None when unparseable."""

    if isinstance(value, dict):
        start = _finite_number(value.get("start_seconds"))
        end = _finite_number(value.get("end_seconds"))
        if start is not None and end is not None:
            return start, end
        return None
    if isinstance(value, str):
        match = INTERVAL_RE.match(value)
        if match:
            return float(match.group("start")), float(match.group("end"))
    return None


# LLM: 该入口迁移自固定上游校验器；保持原字段、错误与数值语义，只消费调用方已安全读取的数据。
# 函数用途: 计算多个时间窗去重后的覆盖长度。
def _union_length(windows: list[tuple[float, float]]) -> float:
    """Total time occupied by ``windows``, counting overlap once."""

    total = 0.0
    cursor: float | None = None
    span_end = 0.0
    for start, end in sorted(windows):
        if cursor is None:
            cursor, span_end = start, end
            continue
        if start > span_end:
            total += span_end - cursor
            cursor, span_end = start, end
            continue
        span_end = max(span_end, end)
    if cursor is not None:
        total += span_end - cursor
    return total


# LLM: 该入口迁移自固定上游校验器；保持原字段、错误与数值语义，只消费调用方已安全读取的数据。
# 函数用途: 核对时长投影并取得权威镜头时长。
def _accepted_duration(
    spec: dict[str, Any], shots_by_id: dict[str, dict[str, Any]]
) -> tuple[float | None, str | None]:
    """Resolve the accepted shot duration and any conflict with the projection.

    ``boundary_refs.duration.value_seconds`` is a read-only projection of the
    shot record. When both are present and disagree, the projection is stale and
    the arithmetic below would be checked against a number the storyboard never
    accepted, so that is reported instead of silently preferring one.
    """

    boundary = spec.get("boundary_refs")
    duration_ref = boundary.get("duration") if isinstance(boundary, dict) else None
    projected: float | None = None
    record_id: str | None = None
    if isinstance(duration_ref, dict):
        projected = _finite_number(duration_ref.get("value_seconds"))
        if isinstance(duration_ref.get("record_id"), str):
            record_id = duration_ref["record_id"]

    shot_ref = spec.get("shot_ref")
    if record_id is None and isinstance(shot_ref, dict):
        if isinstance(shot_ref.get("record_id"), str):
            record_id = shot_ref["record_id"]

    authoritative: float | None = None
    if record_id is not None:
        shot = shots_by_id.get(record_id)
        if isinstance(shot, dict):
            authoritative = _finite_number(shot.get("duration_seconds"))
        elif projected is not None:
            # A reference naming no shot used to fall through to
            # `resolved = projected`, so the arithmetic was then checked against
            # the spec's own self-declared number and the staleness guard never
            # ran -- one mistyped character switched off VID-04. Only reported
            # when a projection exists: with neither a shot nor a projection
            # there is nothing to be wrong about, and `unmeasured` stays the
            # honest answer.
            return None, (
                f"duration reference {record_id} resolves to no shot, so the "
                f"projected {projected}s is checked against nothing"
            )

    if projected is not None and authoritative is not None:
        if abs(projected - authoritative) > TOLERANCE_SECONDS:
            return None, (
                f"projected duration {projected}s does not match accepted "
                f"{record_id} duration {authoritative}s"
            )
        return authoritative, None
    resolved = authoritative if authoritative is not None else projected
    return resolved, None


# LLM: 相对模式只做分类并记录结构矛盾，不进入秒数运算；状态容器用于把入口嵌套限制在两层。
# 函数用途: 记录相对时序计划或其显式片段矛盾。
def _record_relative(mode: object, segments: list[dict[str, Any]], motion_id: str,
                     state: dict[str, list]) -> None:
    if segments:
        state["findings"].append(
            _finding(
                "VID_TIMING_MODE_INCONSISTENT",
                motion_id,
                f"timing_plan mode is {mode!r} but {len(segments)} segment(s) declare explicit timing",
            )
        )
        return
    state["relative"].append(motion_id)


# LLM: 显式片段解析独立于整批循环，保持坏值原样用于结构化发现并避免三层循环分支。
# 函数用途: 把显式 timing 值拆成有效时间窗和无法解析的原值。
def _parse_windows(segments: list[dict[str, Any]]) -> tuple[list[tuple[float, float]], list[Any]]:
    windows: list[tuple[float, float]] = []
    unparseable: list[Any] = []
    for timing in segments:
        interval = _parse_interval(timing.get("value"))
        target = unparseable if interval is None else windows
        target.append(timing.get("value") if interval is None else interval)
    return windows, unparseable


# LLM: 该入口迁移自固定上游校验器；保持原字段、错误与数值语义，只消费调用方已安全读取的数据。
# 函数用途: 核对整批显式运动时序并返回发现摘要。
def check(
    specs: list[dict[str, Any]], shots: list[dict[str, Any]]
) -> dict[str, Any]:
    shots_by_id = {
        shot["shot_id"]: shot
        for shot in shots
        if isinstance(shot.get("shot_id"), str)
    }
    findings: list[dict[str, Any]] = []
    checked = 0
    # Reported, not a finding: a relative plan makes no arithmetic claim, so it
    # is out of scope rather than passing. Keeping the count visible stops a
    # file of entirely relative plans from reading as a clean explicit check.
    relative: list[str] = []
    unmeasured: list[str] = []
    mode_state = {"findings": findings, "relative": relative}

    for index, spec in enumerate(specs):
        motion_id = spec.get("motion_id")
        if not isinstance(motion_id, str) or not motion_id:
            raise CheckError(f"motion spec {index} has no motion_id")

        plan = spec.get("timing_plan")
        mode = plan.get("mode") if isinstance(plan, dict) else None
        segments = [
            entry.get("timing")
            for entry in spec.get("ordered_subject_motion") or []
            if isinstance(entry, dict)
        ]
        explicit_segments = [
            timing
            for timing in segments
            if isinstance(timing, dict) and timing.get("mode") == "explicit"
        ]
        # Whether the projection matches the shot is a fact about the data, not
        # about the timing mode, so it is checked before the mode gate below.
        # Gated behind `explicit` it never ran on a file of relative plans --
        # which is every spec in the recorded run -- and a projection authored
        # wrong from the start was carried downstream unchallenged.
        duration, conflict = _accepted_duration(spec, shots_by_id)
        if conflict is not None:
            findings.append(_finding("VID_DURATION_PROJECTION_STALE", motion_id, conflict))
            continue

        if mode != "explicit":
            # A relative plan makes no arithmetic claim, so it is out of scope
            # by contract rather than passing. Explicit segments underneath one
            # are a contradiction the creator has to resolve — silently doing
            # the arithmetic anyway would judge a plan the docs promise not to.
            _record_relative(mode, explicit_segments, motion_id, mode_state)
            continue

        if duration is None:
            # Cannot be judged either way, and staying silent here is the
            # failure mode this script exists to remove.
            unmeasured.append(motion_id)
            continue

        windows, unparseable = _parse_windows(explicit_segments)
        if unparseable:
            findings.append(
                _finding(
                    "VID_EXPLICIT_TIMING_UNPARSEABLE",
                    motion_id,
                    "explicit segment timing is not a readable seconds interval",
                    values=unparseable,
                )
            )
            continue
        if not windows:
            findings.append(
                _finding(
                    "VID_EXPLICIT_TIMING_UNPARSEABLE",
                    motion_id,
                    "timing_plan declares explicit mode but no segment carries an interval",
                )
            )
            continue

        inverted = [
            f"{start}-{end}"
            for start, end in windows
            if start < 0 or end < 0 or end < start
        ]
        if inverted:
            findings.append(
                _finding(
                    "VID_EXPLICIT_TIMING_UNPARSEABLE",
                    motion_id,
                    "explicit segment ends before it starts",
                    values=inverted,
                )
            )
            continue

        checked += 1
        ordered = sorted(windows)
        declared_overlap = bool(
            isinstance(plan, dict) and plan.get("declares_overlap") is True
        )
        overlaps = [
            f"{ordered[position - 1][0]}-{ordered[position - 1][1]} / {start}-{end}"
            for position, (start, end) in enumerate(ordered)
            if position and start < ordered[position - 1][1] - TOLERANCE_SECONDS
        ]
        if overlaps and not declared_overlap:
            findings.append(
                _finding(
                    "VID_EXPLICIT_TIMING_UNDECLARED_OVERLAP",
                    motion_id,
                    "segments overlap without an explicit overlap declaration",
                    values=overlaps,
                )
            )
            continue

        # The two failure directions are measured from different quantities on
        # purpose. Comparing one total against the duration hides the plan that
        # commits both at once: 0.0-2.0 plus 3.0-5.0 covers exactly 4.0s of a
        # 4.0s shot, so the totals match — while a segment runs a second past
        # the end (truncated) and the 2-3s window sits unallocated (filled with
        # unsourced motion). Overflow is therefore read off the endpoint, and
        # shortfall off the union clipped to the shot.
        covered = _union_length(ordered)
        # max(), not ordered[-1][1]: sorting (start, end) tuples orders by
        # start, so the last element is the latest-starting segment, whose end
        # can sit well inside an earlier segment that encloses it. A sustained
        # motion spanning the shot with beats inside it — exactly what
        # motion-recipe.md tells creators to mark `declares_overlap` — would
        # otherwise have its overrun read off one of the inner beats.
        last_end = max((end for _, end in ordered), default=0.0)
        inside = _union_length(
            [
                (max(start, 0.0), min(end, duration))
                for start, end in ordered
                if min(end, duration) > max(start, 0.0)
            ]
        )

        if last_end - duration > TOLERANCE_SECONDS:
            findings.append(
                _finding(
                    "VID_EXPLICIT_TIMING_OVERFLOW",
                    motion_id,
                    f"explicit timing runs to {last_end}s on an accepted {duration}s shot; "
                    "everything past the end is truncated",
                    accepted_duration_seconds=duration,
                    endpoint_seconds=round(last_end, 6),
                    overflow_seconds=round(last_end - duration, 6),
                )
            )
        unallocated = duration - inside
        if unallocated > TOLERANCE_SECONDS:
            findings.append(
                _finding(
                    "VID_EXPLICIT_TIMING_SHORTFALL",
                    motion_id,
                    f"explicit timing leaves {round(unallocated, 6)}s of an accepted "
                    f"{duration}s shot unallocated; the remainder will be filled with "
                    "unsourced motion",
                    accepted_duration_seconds=duration,
                    allocated_seconds=round(inside, 6),
                    unallocated_seconds=round(unallocated, 6),
                )
            )

        declared_total = (
            plan.get("declared_total_or_endpoint_seconds") if isinstance(plan, dict) else None
        )
        # The field is named `declared_total_or_endpoint_seconds`, so both
        # readings are legitimate and they differ whenever overlap is declared.
        # Matching either is a pass; insisting on one would make a correct plan
        # fail under the other spelling.
        declared_number = _finite_number(declared_total)
        if declared_total is not None and declared_number is None:
            findings.append(
                _finding(
                    "VID_DECLARED_TOTAL_MISMATCH",
                    motion_id,
                    "timing_plan declared total must be a finite non-negative number",
                )
            )
        elif declared_number is not None and declared_number < 0:
            findings.append(
                _finding(
                    "VID_DECLARED_TOTAL_MISMATCH",
                    motion_id,
                    "timing_plan declared total must be a finite non-negative number",
                    declared_seconds=declared_number,
                )
            )
        elif declared_number is not None and not any(
            abs(declared_number - candidate) <= TOLERANCE_SECONDS
            for candidate in (covered, last_end)
        ):
            findings.append(
                _finding(
                    "VID_DECLARED_TOTAL_MISMATCH",
                    motion_id,
                    f"timing_plan declares {declared_total}s, which is neither the "
                    f"{round(covered, 6)}s its segments occupy nor their "
                    f"{round(last_end, 6)}s endpoint",
                    declared_seconds=declared_number,
                    covered_seconds=round(covered, 6),
                    endpoint_seconds=round(last_end, 6),
                )
            )

    return {
        "schema_version": SCHEMA_VERSION,
        "motion_specs": len(specs),
        "explicit_checked": checked,
        "relative_plans": relative,
        "unmeasured_specs": unmeasured,
        "findings": findings,
        "status": "pass" if not findings else "fail",
    }
