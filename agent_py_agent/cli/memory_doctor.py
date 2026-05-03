from __future__ import annotations

"""LLM: implements the memory-doctor CLI command and its supporting helpers.

新手说明:
这里放 memory doctor 体检命令入口和它专用的 helper 函数。
doctor 会告诉你配置最终生效成什么、配置有没有回退 warning、
默认或指定路由索引能不能读，以及 hook/raw 目录里现在有没有归档文件。
route 相关的功能在 memory_commands.py。
"""

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..agent.memory_archive import raw_event_path_for, snapshot_path_for
from ..agent.memory_routing import (
    MemoryRoute,
    load_routes,
    validate_routes,
)
from .common import make_agent

DEFAULT_ROUTE_INDEX = Path("memory") / "routing" / "INDEX.md"
RECENT_ARCHIVE_FILE_LIMIT = 5


def cmd_memory_doctor(args) -> int:
    """LLM: inspect effective memory config, route index health, and archive directories.

    新手说明:
    这是 memory 专用体检命令。它会告诉你配置最终生效成什么、配置有没有回退 warning、
    默认或指定路由索引能不能读，以及 hook/raw 目录里现在有没有归档文件。

    参数说明:
    `args` 是 argparse 解析后的对象，包含 `index`、`json` 和通用 agent 参数。

    返回说明:
    返回进程退出码；报告已打印时返回 0。
    """

    agent = make_agent(args)
    index_path = _resolve_index_path(agent.root, args.index)
    warnings = _config_warnings(agent.config)
    routing = _build_routing_doctor(agent.root, index_path)
    archive = _build_archive_doctor(agent.root, agent.config)
    payload = {
        "ok": routing["load_error"] == "",
        "workspace_root": str(agent.root),
        "config": _memory_config_payload(agent.config),
        "warnings": warnings,
        "routing": routing,
        "archive": archive,
    }
    _print_memory_doctor_report(payload, json_output=args.json)
    return 0


def _resolve_index_path(root: Path, raw_index: str | None) -> Path:
    """LLM: resolve CLI index paths against the agent workspace root.

    新手说明:
    不传参数时用 `memory/routing/INDEX.md`。传相对路径时按当前 agent 工作区找，
    这样测试 fixture 和真实工作区都会落在同一套规则里。

    参数说明:
    `root` 是 agent 工作区根目录；`raw_index` 是用户传入的索引路径或空值。

    返回说明:
    返回解析后的绝对路径。这个 helper 只解析，不检查文件是否存在。
    """

    candidate = Path(raw_index).expanduser() if raw_index else DEFAULT_ROUTE_INDEX
    if candidate.is_absolute():
        return candidate.resolve()
    return (root / candidate).resolve()


def _build_routing_doctor(root: Path, index_path: Path) -> dict[str, Any]:
    """LLM: load and validate the route index for doctor output without raising to callers.

    新手说明:
    doctor 要像体检报告，不能因为索引文件缺失或写坏就直接中断。
    所以这里把缺失、加载错误、校验 warning 都收进一个稳定 JSON 结构。

    参数说明:
    `root` 是工作区根目录；`index_path` 是要检查的索引文件路径。

    返回说明:
    返回 routing 体检字典，包含索引状态、route 列表、warning 和加载错误。
    """

    payload: dict[str, Any] = {
        "index": _index_payload(index_path),
        "routes": [],
        "route_count": 0,
        "validation_warnings": [],
        "load_error": "",
    }
    if not index_path.exists():
        payload["load_error"] = f"memory route index not found: {index_path}"
        return payload
    try:
        routes = load_routes(index_path)
    except Exception as exc:
        payload["load_error"] = f"{type(exc).__name__}: {exc}"
        return payload
    payload["routes"] = [_route_payload(route) for route in routes]
    payload["route_count"] = len(routes)
    payload["validation_warnings"] = validate_routes(routes, root)
    return payload


def _build_archive_doctor(root: Path, config: object) -> dict[str, Any]:
    """LLM: summarize memory hook and raw archive directories using public archive path helpers.

    新手说明:
    archive 模块负责定义 hook/raw 文件落在哪里。doctor 只顺着公开 helper 找目录，
    然后数一数文件、列出最近几个文件，方便用户判断归档骨架有没有在工作。

    参数说明:
    `root` 是工作区根目录；`config` 是 agent 的有效配置对象。

    返回说明:
    返回 archive 体检字典，包含留存配置、hook 目录状态和 raw 目录状态。
    """

    hook_today_path = snapshot_path_for(root)
    raw_today_path = raw_event_path_for(root)
    return {
        "retention": {
            "memory_hook_retention_days": int(getattr(config, "memory_hook_retention_days", 7)),
            "memory_hook_enabled": bool(getattr(config, "memory_hook_enabled", True)),
            "memory_hook_archive_level": int(getattr(config, "memory_hook_archive_level", 3)),
            "memory_archive_level": int(getattr(config, "memory_archive_level", 3)),
        },
        "hook": _archive_dir_payload(hook_today_path.parent, hook_today_path),
        "raw": _archive_dir_payload(raw_today_path.parent, raw_today_path),
    }


def _archive_dir_payload(directory: Path, today_path: Path) -> dict[str, Any]:
    """LLM: produce stable file-count and recent-file metadata for one archive directory.

    新手说明:
    这里不读 JSONL 内容，只看目录和文件状态；最近文件按修改时间倒序排列，
    输出里保留名字、路径、大小和修改时间，足够排查目录有没有动静。

    参数说明:
    `directory` 是 hook 或 raw 目录；`today_path` 是今天理论上会写入的 JSONL 文件路径。

    返回说明:
    返回目录是否存在、文件数量、今天路径和最近文件列表。
    """

    files = sorted(
        [path for path in directory.glob("*.jsonl") if path.is_file()] if directory.exists() else [],
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )
    return {
        "dir": str(directory),
        "exists": directory.exists(),
        "file_count": len(files),
        "today_path": str(today_path),
        "recent_files": [_archive_file_payload(path) for path in files[:RECENT_ARCHIVE_FILE_LIMIT]],
    }


def _archive_file_payload(path: Path) -> dict[str, Any]:
    """LLM: serialize one archive file's filesystem metadata for doctor JSON.

    新手说明:
    把最近文件变成稳定字段，测试和人都能看：文件名、完整路径、大小和 mtime。

    参数说明:
    `path` 是一个 JSONL 文件路径。

    返回说明:
    返回文件名、路径、字节数和修改时间。
    """

    stat = path.stat()
    return {
        "name": path.name,
        "path": str(path),
        "size_bytes": stat.st_size,
        "modified_at": stat.st_mtime,
    }


def _memory_config_payload(config: object) -> dict[str, Any]:
    """LLM: serialize the effective normalized memory config fields.

    新手说明:
    doctor 展示的是程序真正会使用的值，不是 YAML 里原始写法。
    如果配置写坏后被回退，这里会显示回退后的安全值。

    参数说明:
    `config` 是 agent 的有效配置对象。

    返回说明:
    返回 memory 相关配置字段和值。
    """

    fields = [
        "memory_archive_level",
        "memory_hook_enabled",
        "memory_hook_archive_level",
        "memory_hook_retention_days",
        "memory_rule_routing_enabled",
        "memory_rule_routing_mode",
        "memory_rule_auto_read_limit",
        "memory_rule_receipt_enabled",
    ]
    return {field: getattr(config, field) for field in fields}


def _config_warnings(config: object) -> list[dict[str, Any]]:
    """LLM: return normalized memory config fallback warnings as dictionaries.

    新手说明:
    配置加载器已经把 warning 存在 config 上。这里再做一层轻量兼容，
    避免未来 warning 对象或 dict 混用时 CLI 输出变形。

    参数说明:
    `config` 是 agent 的有效配置对象。

    返回说明:
    返回 warning 字典列表；没有 warning 时返回空列表。
    """

    warnings = getattr(config, "memory_config_warnings", []) or []
    normalized: list[dict[str, Any]] = []
    for item in warnings:
        if isinstance(item, dict):
            normalized.append(dict(item))
        elif hasattr(item, "to_dict"):
            normalized.append(item.to_dict())
        elif hasattr(item, "__dataclass_fields__"):
            normalized.append(asdict(item))
        else:
            normalized.append({"message": str(item)})
    return normalized


def _index_payload(index_path: Path) -> dict[str, Any]:
    """LLM: serialize route index location and existence state.

    新手说明:
    所有命令都用同一种字段说明索引在哪里、存不存在、是不是文件。

    参数说明:
    `index_path` 是要展示的 route index 路径。

    返回说明:
    返回 `path`、`exists`、`is_file` 三个字段。
    """

    return {
        "path": str(index_path),
        "exists": index_path.exists(),
        "is_file": index_path.is_file(),
    }


def _route_payload(route: MemoryRoute) -> dict[str, Any]:
    """LLM: serialize a route without leaking dataclass implementation details.

    新手说明:
    doctor 的 routes 列表只放维护索引时最需要扫的字段，顺序稳定、内容克制。

    参数说明:
    `route` 是索引中的一条 `MemoryRoute`。

    返回说明:
    返回 JSON 友好的 route 字典。
    """

    return {
        "route_id": route.route_id,
        "topic": route.topic,
        "trigger_keywords": route.trigger_keywords,
        "aliases": route.aliases,
        "when_to_read": route.when_to_read,
        "authority_path": route.authority_path,
        "scope": route.scope,
        "priority": route.priority,
        "stale_check": route.stale_check,
        "last_verified_at": route.last_verified_at,
        "source_path": route.source_path,
    }


def _print_memory_doctor_report(payload: dict[str, Any], *, json_output: bool) -> None:
    """LLM: render the memory-doctor payload as either stable JSON or compact text.

    新手说明:
    doctor 文本输出只保留最关键的配置、索引和目录状态；完整字段在 `--json` 里。

    参数说明:
    `payload` 是 `cmd_memory_doctor()` 构造好的完整报告；`json_output` 控制输出格式。

    返回说明:
    不返回值；直接打印到标准输出。
    """

    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return

    print("MY-AGENT MEMORY DOCTOR")
    print(f"ok={payload['ok']}")
    print(f"workspace={payload['workspace_root']}")
    print("Config")
    for key, value in payload["config"].items():
        print(f"- {key}={value}")
    print(f"warnings={len(payload['warnings'])}")
    for warning in payload["warnings"]:
        field_name = warning.get("field_name", "warning")
        reason = warning.get("reason", warning.get("message", ""))
        fallback = warning.get("fallback_value", "-")
        print(f"- {field_name}: {reason} fallback={fallback}")
    routing = payload["routing"]
    print("Routing")
    print(f"- index={routing['index']['path']} exists={routing['index']['exists']}")
    print(f"- routes={routing['route_count']} validation_warnings={len(routing['validation_warnings'])}")
    if routing["load_error"]:
        print(f"- load_error={routing['load_error']}")
    for item in routing["validation_warnings"]:
        print(f"- warning: {item}")
    archive = payload["archive"]
    print("Archive")
    print(
        "- retention="
        + json.dumps(archive["retention"], ensure_ascii=False, sort_keys=True)
    )
    for label in ("hook", "raw"):
        state = archive[label]
        recent = state["recent_files"][0]["name"] if state["recent_files"] else "-"
        print(
            f"- {label}: dir={state['dir']} exists={state['exists']} "
            f"files={state['file_count']} recent={recent}"
        )
