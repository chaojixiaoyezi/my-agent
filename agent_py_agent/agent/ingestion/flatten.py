"""事件压平:任意 schema 的 JSON 事件 → (字段路径, 标量值) 对。纯结构操作,不看语义。"""

from __future__ import annotations

_MAX_DEPTH = 8
_MAX_PAIRS = 128
_MAX_ARRAY_SCAN = 16


def flatten_event(event: object) -> list[tuple[str, object]]:
    """递归压平:dict 用 . 连接路径,list 收敛为 [](不展开下标,避免变长数组撑爆路径空间),
    并附带 __len__ 数值特征;标量原样。深度/对数/数组扫描量都有上限(超限截断,不炸)。"""
    pairs: list[tuple[str, object]] = []
    _walk("", event, pairs, 0)
    return pairs


def _walk(path: str, value: object, pairs: list[tuple[str, object]], depth: int) -> None:
    if len(pairs) >= _MAX_PAIRS:
        return
    if depth >= _MAX_DEPTH:
        pairs.append((path or "$", "<max-depth>"))
        return
    if isinstance(value, dict):
        _walk_mapping(path, value, pairs, depth)
        return
    if isinstance(value, (list, tuple)):
        _walk_sequence(path, value, pairs, depth)
        return
    pairs.append((path or "$", value))


def _walk_mapping(path: str, value: dict, pairs: list[tuple[str, object]], depth: int) -> None:
    for key in value:
        child = f"{path}.{key}" if path else str(key)
        _walk(child, value[key], pairs, depth + 1)


def _walk_sequence(path: str, value: object, pairs: list[tuple[str, object]], depth: int) -> None:
    items = list(value)  # type: ignore[arg-type]
    pairs.append((f"{path}.__len__" if path else "$.__len__", len(items)))
    for item in items[:_MAX_ARRAY_SCAN]:
        _walk(f"{path}[]" if path else "$[]", item, pairs, depth + 1)


__all__ = ["flatten_event"]
