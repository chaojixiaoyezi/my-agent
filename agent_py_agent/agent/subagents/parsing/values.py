
from __future__ import annotations


def _dict_list(value: object) -> list[dict[str, object]]:
    """把任意值规范成 dict list。"""

    if not isinstance(value, list):
        return []
    result: list[dict[str, object]] = []
    for item in value:
        if isinstance(item, dict):
            result.append({str(key): val for key, val in item.items()})
    return result


def _int_value(value: object) -> int:
    """把任意值尽量转成整数，失败时返回 0。"""

    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(float(value.strip()))
        except ValueError:
            return 0
    return 0


def _string_dict(value: object) -> dict[str, str]:
    """把任意值规范成字符串字典。"""

    if not isinstance(value, dict):
        return {}
    return {str(key): str(val) for key, val in value.items()}


def _split_allowed_items(items: list[str], allowed: set[str]) -> tuple[list[str], list[str]]:
    """拆分授权项和未授权项。"""

    accepted: list[str] = []
    ignored: list[str] = []
    for item in items:
        if item in allowed:
            accepted.append(item)
        else:
            ignored.append(item)
    return accepted, ignored


def _normalize_runner_items(items: list[dict[str, object]]) -> list[dict[str, object]]:
    """把 runner item 转成稳定可 JSON 化的浅层对象。"""

    normalized: list[dict[str, object]] = []
    for item in items:
        normalized.append(_normalize_single_runner_item(item))
    return normalized


def _normalize_single_runner_item(item: dict[str, object]) -> dict[str, object]:
    """Normalize one runner item dict to JSON-serializable form."""
    payload: dict[str, object] = {}
    for key, value in item.items():
        payload[str(key)] = _normalize_runner_value(value)
    return payload


def _normalize_runner_value(value: object) -> object:
    """Normalize a single runner field value to JSON-serializable form."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [str(entry) for entry in value]
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    return str(value)
