# LLM: Memory routing module; keep context selection and read-receipt records stable.
# 模块用途: 根据任务上下文选择可注入记忆，并记录读取路径。

from __future__ import annotations

"""LLM contract: load memory route indexes from simple JSON or Markdown files.

新手说明:
这里专门处理'人工维护的索引文件怎么变成 `MemoryRoute` 对象'。
第一版不用第三方依赖，JSON 给程序最稳定，Markdown 给人更好改，二者都只支持很克制的字段。
"""

import json
import re
from pathlib import Path
from typing import Any

from .models import MemoryRoute

LIST_FIELDS = {"trigger_keywords", "aliases"}


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 load_memory_routes 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 读取 load memory routes 需要的文件、记录或配置，并整理成调用方可直接使用的结果。
def load_memory_routes(index_path: str | Path) -> list[MemoryRoute]:

    path = Path(index_path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read memory route index {path}: {exc}") from exc
    if path.suffix.lower() == ".json":
        return parse_json_routes(text, source_path=path)
    return parse_markdown_routes(text, source_path=path)


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 load_routes 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 读取 load routes 需要的文件、记录或配置，并整理成调用方可直接使用的结果。
def load_routes(index_path: str | Path) -> list[MemoryRoute]:

    return load_memory_routes(index_path)


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 parse_json_routes 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 从外部数据还原 parse json routes 需要的领域对象，统一缺省值和兼容字段。
def parse_json_routes(text: str, *, source_path: str | Path = "") -> list[MemoryRoute]:

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


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 parse_markdown_routes 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 从外部数据还原 parse markdown routes 需要的领域对象，统一缺省值和兼容字段。
def parse_markdown_routes(text: str, *, source_path: str | Path = "") -> list[MemoryRoute]:

    routes: list[MemoryRoute] = []
    current_id = ""
    current_fields: dict[str, Any] = {}

    # LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 flush 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 flush 在当前模块中的核心转换或协调步骤，衔接 memory routing 读取项目规则、路径和上下文片段来决定注入范围。
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


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 _route_from_mapping 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 route from mapping 在当前模块中的核心转换或协调步骤，衔接 memory routing 读取项目规则、路径和上下文片段来决定注入范围。
def _route_from_mapping(data: dict[str, Any], *, source_path: str | Path = "") -> MemoryRoute:

    return MemoryRoute(
        route_id=str(data.get("route_id") or "").strip(),
        topic=str(data.get("topic") or "").strip(),
        trigger_keywords=_as_list(data.get("trigger_keywords")),
        aliases=_as_list(data.get("aliases")),
        when_to_read=str(data.get("when_to_read") or "").strip(),
        authority_path=str(data.get("authority_path") or "").strip(),
        inject_mode=str(data.get("inject_mode") or "on_hit").strip().lower(),
        scope=str(data.get("scope") or "global").strip(),
        priority=_as_int(data.get("priority"), default=0),
        stale_check=str(data.get("stale_check") or "").strip(),
        last_verified_at=str(data.get("last_verified_at") or "").strip(),
        source_file=str(data.get("source_file") or "").strip(),
        source_path=str(source_path),
    )


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 _as_list 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 把 as list 对应对象转换成字典、JSON 或文本形态，供持久化和输出层复用。
def _as_list(value: Any) -> list[str]:

    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return _split_list(str(value))


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 _split_list 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 split list 在当前模块中的核心转换或协调步骤，衔接 memory routing 读取项目规则、路径和上下文片段来决定注入范围。
def _split_list(value: str) -> list[str]:

    return [item.strip() for item in re.split(r"[,，;；|]", value) if item.strip()]


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 _as_int 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 把 as int 对应对象转换成字典、JSON 或文本形态，供持久化和输出层复用。
def _as_int(value: Any, *, default: int) -> int:

    try:
        return int(value)
    except (TypeError, ValueError):
        return default
