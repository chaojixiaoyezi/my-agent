from __future__ import annotations

"""LLM contract: load memory route indexes from simple JSON or Markdown files.

这里专门处理“人工维护的索引文件怎么变成 `MemoryRoute` 对象”。第一版不用第三方依赖：
JSON 给程序最稳定，Markdown 给人更好改，二者都只支持很克制的字段。
"""

import json
import re
from pathlib import Path
from typing import Any

from .models import MemoryRoute

LIST_FIELDS = {"trigger_keywords", "aliases"}


def load_memory_routes(index_path: str | Path) -> list[MemoryRoute]:
    """LLM contract: loads memory routes from a `.json` index or a simple Markdown index.

    大白话：给它一个索引文件路径，它会按后缀判断怎么读。`.json` 走严格 JSON；
    其他文本默认当 Markdown 读。读失败会抛出带文件名的 `ValueError`，方便定位坏索引。
    """

    path = Path(index_path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read memory route index {path}: {exc}") from exc
    if path.suffix.lower() == ".json":
        return parse_json_routes(text, source_path=path)
    return parse_markdown_routes(text, source_path=path)


def load_routes(index_path: str | Path) -> list[MemoryRoute]:
    """LLM contract: compatibility alias for `load_memory_routes`.

    大白话：短名字方便后续 CLI 或测试里调用，真实逻辑仍然只有一份，
    避免两个加载函数慢慢长成不一样。
    """

    return load_memory_routes(index_path)


def parse_json_routes(text: str, *, source_path: str | Path = "") -> list[MemoryRoute]:
    """LLM contract: parses a JSON route index from a list or `{"routes": [...]}` object.

    大白话：JSON 可以写成数组，也可以写成带 `routes` 字段的对象。每一项都是一条规则路由，
    字段名就是 `MemoryRoute` 的字段名，列表字段既可以写数组，也可以写逗号分隔字符串。
    """

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON memory route index {source_path}: {exc}") from exc
    if isinstance(data, dict):
        routes_data = data.get("routes", [])
    else:
        routes_data = data
    if not isinstance(routes_data, list):
        raise ValueError(f"memory route index {source_path} must contain a route list")
    routes: list[MemoryRoute] = []
    for idx, item in enumerate(routes_data):
        if not isinstance(item, dict):
            raise ValueError(f"memory route #{idx} in {source_path} must be an object")
        routes.append(_route_from_mapping(item, source_path=source_path))
    return routes


def parse_markdown_routes(text: str, *, source_path: str | Path = "") -> list[MemoryRoute]:
    """LLM contract: parses Markdown route blocks with headings and `key: value` lines.

    大白话：Markdown 索引给人维护，格式故意简单：
    `## route-id` 开一条规则，下面写 `topic: ...`、`trigger_keywords: a, b` 这些键值。
    不支持复杂表格和嵌套结构，目的是让规则索引稳定、好 diff、好测试。
    """

    routes: list[MemoryRoute] = []
    current_id = ""
    current_fields: dict[str, Any] = {}

    def flush() -> None:
        if not current_id and not current_fields:
            return
        payload = {"route_id": current_id, **current_fields}
        routes.append(_route_from_mapping(payload, source_path=source_path))

    for raw_line in text.splitlines():
        line = raw_line.strip()
        heading = re.match(r"^#{2,6}\s+(.+?)\s*$", line)
        if heading:
            flush()
            current_id = heading.group(1).strip()
            current_fields = {}
            continue
        if not line or line.startswith("#"):
            continue
        key_value = re.match(r"^-?\s*([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*?)\s*$", line)
        if not key_value:
            continue
        key = key_value.group(1).replace("-", "_")
        value = key_value.group(2).strip()
        current_fields[key] = _split_list(value) if key in LIST_FIELDS else value
    flush()
    return routes


def _route_from_mapping(data: dict[str, Any], *, source_path: str | Path = "") -> MemoryRoute:
    """LLM contract: coerces raw route mapping fields into a `MemoryRoute`.

    大白话：索引文件是人写的，可能把列表写成字符串，也可能把优先级写成 `"10"`。
    这里会做温和转换；真正的完整性问题交给 `validate_routes` 统一报出来。
    """

    return MemoryRoute(
        route_id=str(data.get("route_id") or "").strip(),
        topic=str(data.get("topic") or "").strip(),
        trigger_keywords=_as_list(data.get("trigger_keywords")),
        aliases=_as_list(data.get("aliases")),
        when_to_read=str(data.get("when_to_read") or "").strip(),
        authority_path=str(data.get("authority_path") or "").strip(),
        scope=str(data.get("scope") or "global").strip(),
        priority=_as_int(data.get("priority"), default=0),
        stale_check=str(data.get("stale_check") or "").strip(),
        last_verified_at=str(data.get("last_verified_at") or "").strip(),
        source_path=str(source_path),
    )


def _as_list(value: Any) -> list[str]:
    """LLM contract: normalizes a JSON or Markdown field into a string list.

    大白话：人工写索引时，有人喜欢 `["a", "b"]`，有人喜欢 `a, b`。
    这里统一变成 Python 列表，后面的 matcher 就不用关心来源格式。
    """

    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return _split_list(str(value))


def _split_list(value: str) -> list[str]:
    """LLM contract: splits comma, semicolon, Chinese comma, or pipe separated values.

    大白话：触发词可能用英文逗号、中文逗号、分号或竖线隔开，这里都兼容一点，
    让索引文件更容易手写。
    """

    return [item.strip() for item in re.split(r"[,，;；|]", value) if item.strip()]


def _as_int(value: Any, *, default: int) -> int:
    """LLM contract: safely coerces an index priority value into an integer.

    大白话：优先级写坏了也不要让加载器直接炸掉。先按默认值读进来，
    再由校验器或后续 doctor 告诉维护者哪里需要修。
    """

    try:
        return int(value)
    except (TypeError, ValueError):
        return default
