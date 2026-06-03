
"""Low-level field access, event normalisation, type converters, and time helpers.

本模块提供日志事件分析的底层工具函数——把事件转成 dict、按名字/路径取值、
类型安全转换（int/float/bool/text）、以及时间解析/排序/窗口计算。
由 field_extractors.py 和 classifiers.py 进一步构建在此之上。
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ...models import utc_now_iso

JsonDict = dict[str, Any]
EventLike = Mapping[str, Any] | object



WEB_PARENT_PROCESSES = {
    "apache",
    "apache2",
    "caddy",
    "gunicorn",
    "httpd",
    "iisexpress",
    "java",
    "nginx",
    "node",
    "php-cgi",
    "php-fpm",
    "python",
    "tomcat",
    "uwsgi",
    "w3wp.exe",
}

SUSPICIOUS_CHILD_PROCESSES = {
    "bash",
    "bitsadmin.exe",
    "certutil.exe",
    "cmd.exe",
    "curl",
    "curl.exe",
    "mshta.exe",
    "nc",
    "ncat",
    "netcat",
    "perl",
    "php",
    "powershell.exe",
    "pwsh",
    "python",
    "python.exe",
    "regsvr32.exe",
    "ruby",
    "sh",
    "wget",
    "wget.exe",
    "wmic.exe",
}



def _event_dict(event: EventLike) -> JsonDict:
    if isinstance(event, Mapping):
        return dict(event)
    if is_dataclass(event):
        return asdict(event)
    to_dict = getattr(event, "to_dict", None)
    if callable(to_dict):
        value = to_dict()
        if isinstance(value, Mapping):
            return dict(value)
    try:
        return dict(vars(event))
    except TypeError:
        return {"value": event}



def _field(payload: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        value = _path_value(payload, name)
        if _present(value):
            return value
    return _field_from_nested_bags(payload, names)


def _field_from_nested_bags(payload: Mapping[str, Any], names: Sequence[str]) -> Any:
    for bag_name in ("attributes", "raw_fields", "event", "security", "network", "http", "process", "file", "rule"):
        value = _first_present_path(_path_value(payload, bag_name), names)
        if _present(value):
            return value
    return None


def _first_present_path(value: Any, names: Sequence[str]) -> Any:
    if not isinstance(value, Mapping):
        return None
    for name in names:
        candidate = _path_value(value, name)
        if _present(candidate):
            return candidate
    return None


def _path_value(payload: Mapping[str, Any], path: str) -> Any:
    if path in payload:
        return payload[path]
    lower_map = {str(key).lower(): key for key in payload}
    direct_key = lower_map.get(path.lower())
    if direct_key is not None:
        return payload[direct_key]
    value: Any = payload
    for part in path.split("."):
        if not isinstance(value, Mapping):
            return None
        lower = {str(key).lower(): key for key in value}
        key = lower.get(part.lower())
        if key is None:
            return None
        value = value[key]
    return value


def _present(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = _text(value).strip()
    if not text:
        return False
    text_lower = text.lower()
    if text_lower in {"false", "no", "n", "0", "off", "disabled"}:
        return False
    return True


def _to_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp_float(value: Any) -> float:
    clean = _to_float(value)
    if clean is None:
        return 0.0
    return max(0.0, min(1.0, clean))



def _event_time(event: Mapping[str, Any]) -> datetime | None:
    return _parse_time(_field(event, "event_time", "@timestamp", "timestamp", "time", "created_at", "ingest_time"))


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    text = _text(value)
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _canonical_time(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _sort_time(event: Mapping[str, Any]) -> tuple[int, str]:
    when = _event_time(event)
    return (0, _canonical_time(when)) if when is not None else (1, "")


def _within_after(start: Mapping[str, Any], candidate: Mapping[str, Any], minutes: int) -> bool:
    start_time = _event_time(start)
    candidate_time = _event_time(candidate)
    if start_time is None or candidate_time is None:
        return False
    return start_time <= candidate_time <= start_time + timedelta(minutes=minutes)


def _within_before(candidate: Mapping[str, Any], end: Mapping[str, Any], minutes: int) -> bool:
    candidate_time = _event_time(candidate)
    end_time = _event_time(end)
    if candidate_time is None or end_time is None:
        return False
    return end_time - timedelta(minutes=minutes) <= candidate_time <= end_time


def _window_for_events(events: Sequence[Mapping[str, Any]]) -> tuple[str, str]:
    times = sorted(time for time in (_event_time(event) for event in events) if time is not None)
    if not times:
        stamp = utc_now_iso()
        return (stamp, stamp)
    return (_canonical_time(times[0]), _canonical_time(times[-1]))


def _time_bucket(value: datetime | None, minutes: int) -> str:
    if value is None:
        return "unknown-time"
    minute = (value.minute // max(minutes, 1)) * max(minutes, 1)
    bucket = value.astimezone(timezone.utc).replace(minute=minute, second=0, microsecond=0)
    return _canonical_time(bucket)


def _basename(value: Any) -> str:
    text = _text(value).replace("\\", "/")
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    return text.strip().lower()
