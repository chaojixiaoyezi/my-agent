from __future__ import annotations

"""Typed scheduler expressions and timezone-aware next-run calculation.

The persisted contract intentionally mirrors the three durable time shapes used
by 通道运行时 and 长期助手: one absolute instant, a fixed interval with an anchor,
or a five-field cron expression in an IANA timezone.  Parsing is structural;
runtime authority never depends on words in the scheduled prompt.
"""

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_UTC = timezone.utc
_CRON_PARTS = 5
_MAX_CRON_SEARCH_DAYS = 366 * 8
_MIN_INTERVAL_SECONDS = 60
_MAX_INTERVAL_SECONDS = 366 * 24 * 60 * 60
_MONTH_NAMES = {name.lower(): index for index, name in enumerate(calendar.month_abbr) if name}
_DOW_NAMES = {
    "sun": 0,
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6,
}
_FIELD_LIMITS = (
    (0, 59, {}),
    (0, 23, {}),
    (1, 31, {}),
    (1, 12, _MONTH_NAMES),
    (0, 7, _DOW_NAMES),
)


class ScheduleValidationError(ValueError):
    """A typed schedule cannot be parsed or has no future occurrence."""


@dataclass(frozen=True)
class CronFields:
    minutes: tuple[int, ...]
    hours: tuple[int, ...]
    days_of_month: frozenset[int]
    months: frozenset[int]
    days_of_week: frozenset[int]
    day_of_month_wildcard: bool
    day_of_week_wildcard: bool

    def date_matches(self, candidate: date) -> bool:
        if candidate.month not in self.months:
            return False
        dom_matches = candidate.day in self.days_of_month
        cron_dow = (candidate.weekday() + 1) % 7
        dow_matches = cron_dow in self.days_of_week
        if self.day_of_month_wildcard and self.day_of_week_wildcard:
            return True
        if self.day_of_month_wildcard:
            return dow_matches
        if self.day_of_week_wildcard:
            return dom_matches
        # Vixie cron semantics: when both fields are restricted, either match fires.
        return dom_matches or dow_matches


def build_schedule(
    *,
    kind: object,
    at: object = None,
    every_seconds: object = None,
    anchor_at: object = None,
    cron: object = None,
    timezone_name: object = None,
    default_timezone: str = "",
    now: float,
) -> dict[str, object]:
    normalized_kind = str(kind or "").strip().lower()
    zone_name = _canonical_zone_name(str(timezone_name or default_timezone or "").strip())
    if normalized_kind == "at":
        instant = parse_instant(at, timezone_name=zone_name)
        return {
            "kind": "at",
            "at": _utc_iso(instant),
            "timezone": zone_name,
        }
    if normalized_kind == "every":
        interval = _integer(every_seconds, field="every_seconds")
        if not _MIN_INTERVAL_SECONDS <= interval <= _MAX_INTERVAL_SECONDS:
            raise ScheduleValidationError(
                f"every_seconds must be between {_MIN_INTERVAL_SECONDS} and {_MAX_INTERVAL_SECONDS}"
            )
        anchor = (
            float(now)
            if anchor_at in (None, "")
            else parse_instant(
                anchor_at,
                timezone_name=zone_name,
            ).timestamp()
        )
        return {
            "kind": "every",
            "every_seconds": interval,
            "anchor_at": anchor,
            "timezone": zone_name,
        }
    if normalized_kind == "cron":
        expression = " ".join(str(cron or "").split())
        if not expression:
            raise ScheduleValidationError("cron expression is required")
        parse_cron(expression)
        _zone(zone_name)
        return {
            "kind": "cron",
            "expression": expression,
            "timezone": zone_name,
        }
    raise ScheduleValidationError("schedule_kind must be at, every, or cron")


def validate_schedule(schedule: object) -> dict[str, object]:
    if not isinstance(schedule, dict):
        raise ScheduleValidationError("schedule must be an object")
    kind = str(schedule.get("kind") or "").strip().lower()
    if kind == "at":
        instant = parse_instant(
            schedule.get("at"), timezone_name=str(schedule.get("timezone") or "UTC")
        )
        return {
            "kind": "at",
            "at": _utc_iso(instant),
            "timezone": _canonical_zone_name(str(schedule.get("timezone") or "UTC")),
        }
    if kind == "every":
        interval = _integer(schedule.get("every_seconds"), field="every_seconds")
        if not _MIN_INTERVAL_SECONDS <= interval <= _MAX_INTERVAL_SECONDS:
            raise ScheduleValidationError("stored every_seconds is out of range")
        anchor = _finite_float(schedule.get("anchor_at"), field="anchor_at")
        return {
            "kind": "every",
            "every_seconds": interval,
            "anchor_at": anchor,
            "timezone": _canonical_zone_name(str(schedule.get("timezone") or "UTC")),
        }
    if kind == "cron":
        expression = " ".join(str(schedule.get("expression") or "").split())
        parse_cron(expression)
        zone_name = _canonical_zone_name(str(schedule.get("timezone") or "UTC"))
        _zone(zone_name)
        return {"kind": "cron", "expression": expression, "timezone": zone_name}
    raise ScheduleValidationError("stored schedule kind is invalid")


def compute_next_run(schedule: dict[str, object], *, after: float) -> float | None:
    normalized = validate_schedule(schedule)
    kind = str(normalized["kind"])
    if kind == "at":
        instant = parse_instant(normalized["at"], timezone_name=str(normalized["timezone"]))
        timestamp = instant.timestamp()
        return timestamp if timestamp > float(after) else None
    if kind == "every":
        interval = int(normalized["every_seconds"])
        anchor = float(normalized["anchor_at"])
        if after < anchor:
            return anchor
        steps = int((float(after) - anchor) // interval) + 1
        return anchor + steps * interval
    return next_cron_timestamp(
        str(normalized["expression"]),
        timezone_name=str(normalized["timezone"]),
        after=float(after),
    )


def schedule_timestamp(schedule: dict[str, object]) -> float:
    normalized = validate_schedule(schedule)
    if normalized["kind"] != "at":
        raise ScheduleValidationError("schedule is not an absolute instant")
    return parse_instant(
        normalized["at"],
        timezone_name=str(normalized["timezone"]),
    ).timestamp()


def default_misfire_grace_seconds(schedule: dict[str, object]) -> int:
    normalized = validate_schedule(schedule)
    if normalized["kind"] == "at":
        return 300
    if normalized["kind"] == "every":
        return max(60, min(int(normalized["every_seconds"]) // 2, 7200))
    return 3600


def next_cron_timestamp(expression: str, *, timezone_name: str, after: float) -> float:
    fields = parse_cron(expression)
    zone = _zone(timezone_name)
    local_after = datetime.fromtimestamp(float(after), zone)
    candidate_date = local_after.date()
    for _ in range(_MAX_CRON_SEARCH_DAYS):
        if fields.date_matches(candidate_date):
            timestamps = _candidate_timestamps(candidate_date, fields, zone)
            future = [timestamp for timestamp in timestamps if timestamp > after]
            if future:
                return min(future)
        candidate_date = date.fromordinal(candidate_date.toordinal() + 1)
    raise ScheduleValidationError("cron expression has no occurrence in the next eight years")


@lru_cache(maxsize=512)
def parse_cron(expression: str) -> CronFields:
    parts = str(expression or "").strip().split()
    if len(parts) != _CRON_PARTS:
        raise ScheduleValidationError("cron must contain exactly five fields")
    expanded: list[tuple[frozenset[int], bool]] = []
    for index, part in enumerate(parts):
        minimum, maximum, names = _FIELD_LIMITS[index]
        values = _expand_field(part, minimum=minimum, maximum=maximum, names=names)
        if index == 4:
            values = frozenset(0 if value == 7 else value for value in values)
            wildcard = values == frozenset(range(0, 7))
        else:
            wildcard = values == frozenset(range(minimum, maximum + 1))
        expanded.append((values, wildcard))
    return CronFields(
        minutes=tuple(sorted(expanded[0][0])),
        hours=tuple(sorted(expanded[1][0])),
        days_of_month=expanded[2][0],
        months=expanded[3][0],
        days_of_week=expanded[4][0],
        day_of_month_wildcard=expanded[2][1],
        day_of_week_wildcard=expanded[4][1],
    )


def parse_instant(value: object, *, timezone_name: str = "") -> datetime:
    if isinstance(value, bool) or value in (None, ""):
        raise ScheduleValidationError("absolute time is required")
    if isinstance(value, int | float):
        timestamp = _finite_float(value, field="absolute time")
        try:
            return datetime.fromtimestamp(timestamp, _UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise ScheduleValidationError("absolute time is outside the supported range") from exc
    text = str(value).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ScheduleValidationError("absolute time must be ISO-8601 or a Unix timestamp") from exc
    if parsed.tzinfo is None:
        zone = _zone(_canonical_zone_name(timezone_name))
        localized = parsed.replace(tzinfo=zone)
        roundtrip = datetime.fromtimestamp(localized.timestamp(), zone)
        if roundtrip.replace(tzinfo=None) != parsed:
            raise ScheduleValidationError("absolute time does not exist in the selected timezone")
        parsed = localized
    return parsed.astimezone(_UTC)


def iso_from_timestamp(value: object) -> str:
    try:
        timestamp = float(value or 0.0)
    except (TypeError, ValueError):
        return ""
    return _utc_iso(datetime.fromtimestamp(timestamp, _UTC)) if timestamp > 0 else ""


def _expand_field(
    text: str,
    *,
    minimum: int,
    maximum: int,
    names: dict[str, int],
) -> frozenset[int]:
    source = str(text or "").strip().lower()
    if not source or re.search(r"[^a-z0-9*/,-]", source):
        raise ScheduleValidationError(f"invalid cron field: {text}")
    values: set[int] = set()
    for token in source.split(","):
        base, step = _split_step(token)
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            left, right = base.split("-", 1)
            start = _field_value(left, names)
            end = _field_value(right, names)
        else:
            start = _field_value(base, names)
            end = maximum if "/" in token else start
        if not minimum <= start <= maximum or not minimum <= end <= maximum or start > end:
            raise ScheduleValidationError(f"cron field value out of range: {token}")
        values.update(range(start, end + 1, step))
    if not values:
        raise ScheduleValidationError(f"empty cron field: {text}")
    return frozenset(values)


def _split_step(token: str) -> tuple[str, int]:
    if token.count("/") > 1:
        raise ScheduleValidationError(f"invalid cron step: {token}")
    if "/" not in token:
        return token, 1
    base, raw_step = token.split("/", 1)
    step = _integer(raw_step, field="cron step")
    if step <= 0:
        raise ScheduleValidationError("cron step must be positive")
    return base, step


def _field_value(raw: str, names: dict[str, int]) -> int:
    if raw in names:
        return names[raw]
    if not raw.isdigit():
        raise ScheduleValidationError(f"invalid cron value: {raw}")
    return int(raw)


def _candidate_timestamps(candidate_date: date, fields: CronFields, zone: ZoneInfo) -> list[float]:
    timestamps: set[float] = set()
    for hour in fields.hours:
        for minute in fields.minutes:
            for fold in (0, 1):
                local = datetime(
                    candidate_date.year,
                    candidate_date.month,
                    candidate_date.day,
                    hour,
                    minute,
                    tzinfo=zone,
                    fold=fold,
                )
                timestamp = local.timestamp()
                roundtrip = datetime.fromtimestamp(timestamp, zone)
                if (
                    roundtrip.date() == candidate_date
                    and roundtrip.hour == hour
                    and roundtrip.minute == minute
                    and roundtrip.fold == fold
                ):
                    timestamps.add(timestamp)
    return sorted(timestamps)


def _canonical_zone_name(value: str) -> str:
    return str(value or "").strip() or _local_zone_name()


def _local_zone_name() -> str:
    local = datetime.now().astimezone().tzinfo
    key = str(getattr(local, "key", "") or "").strip()
    return key or "UTC"


@lru_cache(maxsize=128)
def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(str(name or "UTC"))
    except ZoneInfoNotFoundError as exc:
        raise ScheduleValidationError(f"unknown IANA timezone: {name}") from exc


def _integer(value: object, *, field: str) -> int:
    if isinstance(value, bool):
        raise ScheduleValidationError(f"{field} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ScheduleValidationError(f"{field} must be an integer") from exc
    if isinstance(value, float) and value != parsed:
        raise ScheduleValidationError(f"{field} must be an integer")
    return parsed


def _finite_float(value: object, *, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ScheduleValidationError(f"{field} must be a number") from exc
    if parsed != parsed or parsed in {float("inf"), float("-inf")}:
        raise ScheduleValidationError(f"{field} must be finite")
    return parsed


def _utc_iso(value: datetime) -> str:
    return value.astimezone(_UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "ScheduleValidationError",
    "build_schedule",
    "compute_next_run",
    "default_misfire_grace_seconds",
    "iso_from_timestamp",
    "next_cron_timestamp",
    "parse_cron",
    "parse_instant",
    "schedule_timestamp",
    "validate_schedule",
]
