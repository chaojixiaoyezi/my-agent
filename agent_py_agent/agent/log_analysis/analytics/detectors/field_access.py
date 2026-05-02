"""LLM: Low-level field access, event normalisation, type converters, and time helpers.

给人看的解释：
本模块提供日志事件分析的底层工具函数——把事件转成 dict、按名字/路径取值、
类型安全转换（int/float/bool/text）、以及时间解析/排序/窗口计算。
由 field_extractors.py 和 classifiers.py 进一步构建在此之上。
"""

from __future__ import annotations

import ipaddress
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from ...models import utc_now_iso

JsonDict = dict[str, Any]
EventLike = Mapping[str, Any] | object


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Event normalisation
# ---------------------------------------------------------------------------

def _event_dict(event: EventLike) -> JsonDict:
    """LLM: Convert any event-like object into a plain dict for uniform access.

    新手说明:
    不管事件是 dict、dataclass 还是普通对象，都转成 dict 方便后续取值。
    """
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


# ---------------------------------------------------------------------------
# Generic field access
# ---------------------------------------------------------------------------

def _field(payload: Mapping[str, Any], *names: str) -> Any:
    """LLM: Return the first present value for any of *names*, searching nested bags.

    新手说明:
    按顺序查找字段名，先在顶层找，再到 attributes/raw_fields 等子字典里找。
    """
    for name in names:
        value = _path_value(payload, name)
        if _present(value):
            return value
    for bag_name in ("attributes", "raw_fields", "event", "security", "network", "http", "process", "file", "rule"):
        bag = _path_value(payload, bag_name)
        if isinstance(bag, Mapping):
            for name in names:
                value = _path_value(bag, name)
                if _present(value):
                    return value
    return None


def _path_value(payload: Mapping[str, Any], path: str) -> Any:
    """LLM: Resolve a dotted path with case-insensitive key matching.

    新手说明:
    支持 "source.ip" 这样的点号路径，且键名不区分大小写。
    """
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
    """LLM: Return True if *value* is neither None nor an empty scalar.

    新手说明:
    判断值是否"有效"——排除 None、空字符串、空列表、空字典。
    """
    return value is not None and value != "" and value != [] and value != {}


def _text(value: Any) -> str:
    """LLM: Coerce *value* to a stripped string; return '' for None.

    新手说明:
    把任意值转成字符串，None 返回空串，布尔值转 "true"/"false"。
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _truthy(value: Any) -> bool:
    """LLM: Return True if *value* looks like a truthy flag.

    新手说明:
    判断值是否为"真"——布尔/数值按常规，字符串匹配 1/true/yes/y/new/rare/unusual。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = _text(value).lower()
    return text in {"1", "true", "yes", "y", "new", "rare", "unusual"}


def _to_int(value: Any) -> int | None:
    """LLM: Safely convert *value* to int; return None on failure or for bools.

    新手说明:
    安全转整数，失败返回 None，布尔值也返回 None。
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    """LLM: Safely convert *value* to float; return None on failure or for bools.

    新手说明:
    安全转浮点数，失败返回 None，布尔值也返回 None。
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp_float(value: Any) -> float:
    """LLM: Convert to float and clamp to [0.0, 1.0].

    新手说明:
    把值限制在 0.0 到 1.0 之间，常用于置信度和风险分数。
    """
    clean = _to_float(value)
    if clean is None:
        return 0.0
    return max(0.0, min(1.0, clean))


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _event_time(event: Mapping[str, Any]) -> datetime | None:
    """LLM: Extract and parse the primary timestamp from *event*.

    新手说明:
    从事件中提取时间戳字段并解析成 datetime 对象。
    """
    return _parse_time(_field(event, "event_time", "@timestamp", "timestamp", "time", "created_at", "ingest_time"))


def _parse_time(value: Any) -> datetime | None:
    """LLM: Parse ISO-8601, epoch, or datetime values; always return UTC-aware or None.

    新手说明:
    把各种时间格式（ISO 字符串、时间戳、datetime 对象）统一转成带时区的 datetime。
    """
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
    """LLM: Return an ISO-8601 UTC string with 'Z' suffix, or '' for None.

    新手说明:
    把 datetime 转成标准的 UTC ISO 字符串（以 Z 结尾），None 返回空串。
    """
    if value is None:
        return ""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _sort_time(event: Mapping[str, Any]) -> tuple[int, str]:
    """LLM: Return a sort key (timed=0/untimed=1, canonical_time) for *event*.

    新手说明:
    为事件排序生成排序键——有时间戳的排前面，同级别按时间字符串排。
    """
    when = _event_time(event)
    return (0, _canonical_time(when)) if when is not None else (1, "")


def _within_after(start: Mapping[str, Any], candidate: Mapping[str, Any], minutes: int) -> bool:
    """LLM: Return True if *candidate* falls within *minutes* after *start*.

    新手说明:
    判断 candidate 事件是否在 start 事件之后的 minutes 分钟内。
    """
    start_time = _event_time(start)
    candidate_time = _event_time(candidate)
    if start_time is None or candidate_time is None:
        return False
    return start_time <= candidate_time <= start_time + timedelta(minutes=minutes)


def _within_before(candidate: Mapping[str, Any], end: Mapping[str, Any], minutes: int) -> bool:
    """LLM: Return True if *candidate* falls within *minutes* before *end*.

    新手说明:
    判断 candidate 事件是否在 end 事件之前的 minutes 分钟内。
    """
    candidate_time = _event_time(candidate)
    end_time = _event_time(end)
    if candidate_time is None or end_time is None:
        return False
    return end_time - timedelta(minutes=minutes) <= candidate_time <= end_time


def _window_for_events(events: Sequence[Mapping[str, Any]]) -> tuple[str, str]:
    """LLM: Return (earliest, latest) canonical-time window for *events*.

    新手说明:
    计算一组事件的时间范围（最早到最晚），返回两个 ISO 时间字符串。
    """
    times = sorted(time for time in (_event_time(event) for event in events) if time is not None)
    if not times:
        stamp = utc_now_iso()
        return (stamp, stamp)
    return (_canonical_time(times[0]), _canonical_time(times[-1]))


def _time_bucket(value: datetime | None, minutes: int) -> str:
    """LLM: Return a canonical-time string for the bucket containing *value*.

    新手说明:
    按 minutes 间隔把时间"桶化"，用于把相近事件归到同一时间段。
    """
    if value is None:
        return "unknown-time"
    minute = (value.minute // max(minutes, 1)) * max(minutes, 1)
    bucket = value.astimezone(timezone.utc).replace(minute=minute, second=0, microsecond=0)
    return _canonical_time(bucket)


def _basename(value: Any) -> str:
    """LLM: Return the lowercase basename of a path-like *value*.

    新手说明:
    取路径的最后一部分并转小写，如 "C:\\Windows\\cmd.exe" 变成 "cmd.exe"。
    """
    text = _text(value).replace("\\", "/")
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    return text.strip().lower()
