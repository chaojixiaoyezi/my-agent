# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

"""Low-level field access, event normalisation, type converters, and time helpers.

给人看的解释：
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



# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _event_dict 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 event dict 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
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



# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _field 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 field 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _field(payload: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        value = _path_value(payload, name)
        if _present(value):
            return value
    return _field_from_nested_bags(payload, names)


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _field_from_nested_bags 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 field from nested bags 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _field_from_nested_bags(payload: Mapping[str, Any], names: Sequence[str]) -> Any:
    for bag_name in ("attributes", "raw_fields", "event", "security", "network", "http", "process", "file", "rule"):
        value = _first_present_path(_path_value(payload, bag_name), names)
        if _present(value):
            return value
    return None


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _first_present_path 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 first present path 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _first_present_path(value: Any, names: Sequence[str]) -> Any:
    if not isinstance(value, Mapping):
        return None
    for name in names:
        candidate = _path_value(value, name)
        if _present(candidate):
            return candidate
    return None


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _path_value 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 path value 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
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


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _present 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 present 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _present(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _text 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 text 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _truthy 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 truthy 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
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


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _to_int 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 把 to int 对应对象转换成字典、JSON 或文本形态，供持久化和输出层复用。
def _to_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _to_float 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 把 to float 对应对象转换成字典、JSON 或文本形态，供持久化和输出层复用。
def _to_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _clamp_float 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 clamp float 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _clamp_float(value: Any) -> float:
    clean = _to_float(value)
    if clean is None:
        return 0.0
    return max(0.0, min(1.0, clean))



# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _event_time 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 event time 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _event_time(event: Mapping[str, Any]) -> datetime | None:
    return _parse_time(_field(event, "event_time", "@timestamp", "timestamp", "time", "created_at", "ingest_time"))


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _parse_time 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 从外部数据还原 parse time 需要的领域对象，统一缺省值和兼容字段。
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


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _canonical_time 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 canonical time 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _canonical_time(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _sort_time 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 sort time 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _sort_time(event: Mapping[str, Any]) -> tuple[int, str]:
    when = _event_time(event)
    return (0, _canonical_time(when)) if when is not None else (1, "")


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _within_after 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 within after 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _within_after(start: Mapping[str, Any], candidate: Mapping[str, Any], minutes: int) -> bool:
    start_time = _event_time(start)
    candidate_time = _event_time(candidate)
    if start_time is None or candidate_time is None:
        return False
    return start_time <= candidate_time <= start_time + timedelta(minutes=minutes)


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _within_before 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 within before 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _within_before(candidate: Mapping[str, Any], end: Mapping[str, Any], minutes: int) -> bool:
    candidate_time = _event_time(candidate)
    end_time = _event_time(end)
    if candidate_time is None or end_time is None:
        return False
    return end_time - timedelta(minutes=minutes) <= candidate_time <= end_time


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _window_for_events 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 window for events 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _window_for_events(events: Sequence[Mapping[str, Any]]) -> tuple[str, str]:
    times = sorted(time for time in (_event_time(event) for event in events) if time is not None)
    if not times:
        stamp = utc_now_iso()
        return (stamp, stamp)
    return (_canonical_time(times[0]), _canonical_time(times[-1]))


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _time_bucket 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 time bucket 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _time_bucket(value: datetime | None, minutes: int) -> str:
    if value is None:
        return "unknown-time"
    minute = (value.minute // max(minutes, 1)) * max(minutes, 1)
    bucket = value.astimezone(timezone.utc).replace(minute=minute, second=0, microsecond=0)
    return _canonical_time(bucket)


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _basename 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 basename 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _basename(value: Any) -> str:
    text = _text(value).replace("\\", "/")
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    return text.strip().lower()
