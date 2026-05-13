# LLM: Home runtime query helpers are the read-side contract for ~/.my-agent memory and task workspaces.
# 模块用途: 读取 home daily memory、workspace/tasks、家目录健康状态，并给 resume/CLI/doctor 复用。

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .home_layout import MyAgentHomePaths, home_paths, safe_task_slug


# LLM: DailyMemoryQuery keeps daily ledger reads bundle-based as filters grow.
# 类用途: 打包 home/memory/daily 查询条件，供 CLI、memory search 和后续前端复用。
@dataclass(frozen=True)
class DailyMemoryQuery:
    query: str = ""
    date_key: str | None = None
    role: str = ""
    kind: str = ""
    limit: int = 20


# LLM: TaskWorkspaceQuery keeps task workspace list reads stable and extensible.
# 类用途: 打包 workspace/tasks 查询条件，按日期、关键词和数量限制列出任务目录。
@dataclass(frozen=True)
class TaskWorkspaceQuery:
    query: str = ""
    date_key: str | None = None
    limit: int = 20


# LLM: read_daily_memory_records is refs-light; it reads daily JSONL records without touching legacy memory_path.
# 函数用途: 从 home/memory/daily 按日期、角色、类型和关键词读取记忆记录。
def read_daily_memory_records(paths: MyAgentHomePaths | str | Path, request: DailyMemoryQuery) -> list[dict[str, Any]]:
    home = _coerce_home_paths(paths)
    records: list[dict[str, Any]] = []
    for path in _daily_memory_files(home, request.date_key):
        records.extend(_read_daily_file(path, request))
        if _limit_reached(records, request.limit):
            break
    return _apply_limit(records, request.limit)


# LLM: list_task_workspaces is the canonical read side for ~/.my-agent/workspace/tasks.
# 函数用途: 列出主代理任务工作区，并附带 state、timeline、outputs/runtime/agents 路径。
def list_task_workspaces(paths: MyAgentHomePaths | str | Path, request: TaskWorkspaceQuery) -> list[dict[str, Any]]:
    home = _coerce_home_paths(paths)
    items: list[dict[str, Any]] = []
    for state_path in _task_state_files(home, request.date_key):
        item = _task_workspace_payload(state_path)
        if not _matches_query(item, request.query):
            continue
        items.append(item)
        if _limit_reached(items, request.limit):
            break
    return _apply_limit(items, request.limit)


# LLM: find_task_workspace resolves human task ids, run ids, request ids, and slugs to a home task workspace.
# 函数用途: 根据 task/run/request/slug 找到一个任务工作区，用于 memory-resume 补充权威事实源。
def find_task_workspace(paths: MyAgentHomePaths | str | Path, task_ref: str) -> dict[str, Any] | None:
    ref = str(task_ref or "").strip()
    if not ref:
        return None
    home = _coerce_home_paths(paths)
    for item in list_task_workspaces(home, TaskWorkspaceQuery(limit=0)):
        if _task_workspace_ref_matches(item, ref):
            return item
    return None


# LLM: home_task_workspace_payload returns a resume-compatible payload instead of exposing raw directory details.
# 函数用途: 把 home task workspace 转换成 memory-resume 可直接使用的 task_fact_source。
def home_task_workspace_payload(paths: MyAgentHomePaths | str | Path, task_ref: str) -> dict[str, Any] | None:
    item = find_task_workspace(paths, task_ref)
    if item is None:
        return None
    state = item["state"]
    reads = _recommended_task_reads(item)
    return {
        "run_id": str(state.get("run_id") or task_ref),
        "task_id": str(state.get("task_id") or state.get("task_name") or task_ref),
        "exists": True,
        "source": "home_task_workspace",
        "status": str(state.get("status") or state.get("source") or "workspace"),
        "verification_status": str(state.get("verification_status") or ""),
        "goal": str(state.get("task_name") or state.get("task_id") or task_ref),
        "updated_at": str(state.get("updated_at") or ""),
        "task_dir": item["root"],
        "state_path": item["state_path"],
        "timeline_path": item["timeline_path"],
        "recommended_read_paths": reads,
        "authority_validation": _validate_paths(reads),
    }


# LLM: home_runtime_status provides one stable doctor payload for setup and migration checks.
# 函数用途: 返回 home 根目录、入口文件、关键目录和轻量计数，供 memory-doctor 展示。
def home_runtime_status(paths: MyAgentHomePaths | str | Path) -> dict[str, Any]:
    home = _coerce_home_paths(paths)
    return {
        "root": str(home.root),
        "entry_files": {
            "soul_md": _path_status(home.soul_md),
            "user_md": _path_status(home.user_md),
            "agents_md": _path_status(home.agents_md),
            "memory_md": _path_status(home.memory_md),
        },
        "directories": {
            "memory_daily": _path_status(home.memory_daily_dir),
            "memory_raw": _path_status(home.memory_raw_dir),
            "memory_hooks": _path_status(home.memory_hooks_dir),
            "memory_indexes": _path_status(home.memory_indexes_dir),
            "workspace_tasks": _path_status(home.workspace_tasks_dir),
            "scripts": _path_status(home.scripts_dir),
            "role_templates": _path_status(home.role_templates_dir),
            "workflows": _path_status(home.workflows_dir),
        },
        "counts": {
            "daily_files": len(_daily_memory_files(home, None)),
            "task_workspaces": len(_task_state_files(home, None)),
        },
    }


# LLM: _coerce_home_paths accepts either the frozen path map or a root path for small tests and CLI helpers.
# 函数用途: 归一化 home path 输入，保证后续 helper 总能使用 MyAgentHomePaths。
def _coerce_home_paths(paths: MyAgentHomePaths | str | Path) -> MyAgentHomePaths:
    if isinstance(paths, MyAgentHomePaths):
        return paths
    return home_paths(paths)


# LLM: _daily_memory_files keeps daily scans deterministic and date-filterable.
# 函数用途: 找到 daily JSONL 文件；传日期时只读当天，不传时按文件名倒序读取。
def _daily_memory_files(paths: MyAgentHomePaths, date_key: str | None) -> list[Path]:
    directory = paths.memory_daily_dir
    if date_key:
        path = directory / f"{date_key}.jsonl"
        return [path] if path.exists() and path.is_file() else []
    if not directory.exists():
        return []
    return sorted((path for path in directory.glob("*.jsonl") if path.is_file()), reverse=True)


# LLM: _read_daily_file tolerates bad lines so one corrupt record does not hide the rest of the day.
# 函数用途: 读取单个 daily JSONL 文件，并补充 date/path/line_no 元数据。
def _read_daily_file(path: Path, request: DailyMemoryQuery) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    date_key = path.stem
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        obj = _parse_json_line(line)
        if not obj or not _daily_record_matches(obj, request):
            continue
        obj["date"] = date_key
        obj["path"] = str(path)
        obj["line_no"] = line_no
        records.append(obj)
    return records


# LLM: _daily_record_matches applies simple literal filters without introducing a search dependency.
# 函数用途: 判断 daily 记录是否匹配关键词、角色和类型过滤条件。
def _daily_record_matches(record: dict[str, Any], request: DailyMemoryQuery) -> bool:
    if request.role and str(record.get("role") or "") != request.role:
        return False
    if request.kind and str(record.get("kind") or "") != request.kind:
        return False
    return _text_contains(record, request.query)


# LLM: _task_state_files enumerates task state files without reading outputs or agent artifacts.
# 函数用途: 找到 workspace/tasks 下的 state.json 文件；可按日期目录收窄。
def _task_state_files(paths: MyAgentHomePaths, date_key: str | None) -> list[Path]:
    root = paths.workspace_tasks_dir
    if not root.exists():
        return []
    date_dirs = [root / date_key] if date_key else sorted((path for path in root.iterdir() if path.is_dir()), reverse=True)
    state_files: list[Path] = []
    for date_dir in date_dirs:
        if not date_dir.exists():
            continue
        state_files.extend(sorted(date_dir.glob("*/state.json"), reverse=True))
    return state_files


# LLM: _task_workspace_payload keeps CLI/debug output small and points readers to authority files.
# 函数用途: 把一个 state.json 所在任务目录组装成结构化任务工作区摘要。
def _task_workspace_payload(state_path: Path) -> dict[str, Any]:
    root = state_path.parent
    state = _read_json_object(state_path)
    timeline = root / "timeline.jsonl"
    return {
        "root": str(root),
        "date": root.parent.name,
        "slug": root.name,
        "exists": root.exists(),
        "state_path": str(state_path),
        "timeline_path": str(timeline),
        "task_yaml_path": str(root / "task.yaml"),
        "outputs_dir": str(root / "outputs"),
        "runtime_dir": str(root / "runtime"),
        "agents_dir": str(root / "agents"),
        "logs_dir": str(root / "logs"),
        "state": state,
    }


# LLM: _task_workspace_ref_matches resolves both semantic ids and filesystem slugs.
# 函数用途: 判断任务工作区是否对应某个 task_id、run_id、request_id、任务名或 slug。
def _task_workspace_ref_matches(item: dict[str, Any], ref: str) -> bool:
    state = item.get("state", {}) if isinstance(item.get("state"), dict) else {}
    candidates = [
        item.get("slug", ""),
        safe_task_slug(ref),
        state.get("task_id", ""),
        state.get("task_name", ""),
        state.get("run_id", ""),
        state.get("request_id", ""),
    ]
    return ref in {str(value) for value in candidates} or safe_task_slug(ref) == str(item.get("slug") or "")


# LLM: _recommended_task_reads orders refs from strongest state to richer timeline without reading bodies.
# 函数用途: 为 memory-resume 生成推荐读取路径，先 state，再 timeline，再 task yaml。
def _recommended_task_reads(item: dict[str, Any]) -> list[str]:
    paths = [
        item.get("state_path", ""),
        item.get("timeline_path", ""),
        item.get("task_yaml_path", ""),
    ]
    return [str(path) for path in paths if path and Path(str(path)).exists()]


# LLM: _validate_paths mirrors legacy task authority validation for home task workspaces.
# 函数用途: 检查推荐事实源路径是否仍存在，返回 resume 可展示的校验结果。
def _validate_paths(paths: list[str]) -> dict[str, Any]:
    missing = [path for path in paths if path and not Path(path).exists()]
    return {"ok": not missing, "missing_paths": missing}


# LLM: _path_status is the common doctor shape for files and directories.
# 函数用途: 返回路径存在性、类型和大小，避免 doctor 输出格式分叉。
def _path_status(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "exists": path.exists(),
        "is_file": path.is_file(),
        "is_dir": path.is_dir(),
        "size_bytes": path.stat().st_size if path.exists() and path.is_file() else 0,
    }


# LLM: _read_json_object keeps corrupt state files visible as empty payloads instead of crashing list commands.
# 函数用途: 安全读取 JSON object；非 object 或解析失败时返回空字典。
def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


# LLM: _parse_json_line is the tolerant JSONL parser shared by daily record reads.
# 函数用途: 解析单行 JSON；空行、坏行或非对象行返回空字典。
def _parse_json_line(line: str) -> dict[str, Any]:
    if not line.strip():
        return {}
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


# LLM: _matches_query avoids loading large files by matching only already-loaded metadata.
# 函数用途: 判断 payload 的结构化小字段是否包含关键词。
def _matches_query(item: dict[str, Any], query: str) -> bool:
    return _text_contains(item, query)


# LLM: _text_contains implements lightweight case-insensitive literal matching for JSON-like records.
# 函数用途: 在结构化记录的 JSON 文本中查找关键词；空关键词表示匹配。
def _text_contains(value: dict[str, Any], query: str) -> bool:
    text = str(query or "").strip().lower()
    if not text:
        return True
    return text in json.dumps(value, ensure_ascii=False, sort_keys=True).lower()


# LLM: _limit_reached centralizes the convention that 0 means unlimited for debug reads.
# 函数用途: 判断结果是否达到 limit；limit 小于等于 0 表示不限制。
def _limit_reached(items: list[dict[str, Any]], limit: int) -> bool:
    return int(limit or 0) > 0 and len(items) >= int(limit)


# LLM: _apply_limit keeps public read helpers deterministic when callers pass negative or zero limits.
# 函数用途: 应用数量上限；0 或负数返回全部。
def _apply_limit(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    value = int(limit or 0)
    return items[:value] if value > 0 else items


__all__ = [
    "DailyMemoryQuery",
    "TaskWorkspaceQuery",
    "find_task_workspace",
    "home_runtime_status",
    "home_task_workspace_payload",
    "list_task_workspaces",
    "read_daily_memory_records",
]
