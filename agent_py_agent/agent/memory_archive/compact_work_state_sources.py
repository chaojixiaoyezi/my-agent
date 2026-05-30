# LLM: Compact work-state source scanning must stay bounded to workspace task/run fact files.
# 模块用途: 从 task/run 事实源提取验收、约束和最近测试状态，供 compact work_state_snapshot 使用。

from __future__ import annotations

"""bounded fact-source scanner for compact work-state snapshots."""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_ACCEPTANCE_FILES = ("ACCEPTANCE.md", "acceptance.md")
_CONSTRAINT_FILES = ("CONSTRAINTS.md", "constraints.md")
_TEST_FILES = ("TEST_CHECKLIST.md", "test_checklist.md", "failing_tests.json", "next_actions.json")


# LLM: WorkStateFieldSourceRequest keeps source scanning input bundle-based and workspace-scoped.
# 类用途: 描述字段扫描需要的 compact plan 和 snapshot source_state；不携带可写路径。
@dataclass(frozen=True)
class WorkStateFieldSourceRequest:
    plan: dict[str, Any]
    source_state: dict[str, Any]


# LLM: WorkStateFieldSources keeps sourced acceptance/constraints/tests together for snapshot assembly.
# 类用途: 保存从 workspace 事实源读取到的验收、约束、测试和路径线索。
@dataclass(frozen=True)
class WorkStateFieldSources:
    goal: str
    next_actions: list[str]
    acceptance: dict[str, Any]
    constraints: dict[str, Any]
    latest_tests: dict[str, Any]
    read_files: list[str]


# LLM: build_work_state_field_sources only reads bounded workspace fact files and never invents missing state.
# 函数用途: 从 task/run 事实源提取验收、约束和最近测试；找不到时保持 not_recorded。
def build_work_state_field_sources(request: WorkStateFieldSourceRequest) -> WorkStateFieldSources:
    roots = _candidate_fact_roots(request)
    goal = _first_item(_field_items(roots, ("task.json",), json_keys=("goal",)))
    next_actions = _field_items(roots, ("task.json", "next_actions.json"), json_keys=("next_actions",))
    acceptance = _field_payload(_field_items(roots, _ACCEPTANCE_FILES, json_keys=("acceptance_checks", "acceptance")))
    constraints = _field_payload(_field_items(roots, _CONSTRAINT_FILES, json_keys=("constraints", "hard_constraints")))
    latest_tests = _test_payload(
        _field_items(roots, _TEST_FILES, json_keys=("latest_tests", "tests", "failing_tests", "test_status"))
    )
    read_files = _dedupe([*acceptance["source_paths"], *constraints["source_paths"], *latest_tests["source_paths"]])
    return WorkStateFieldSources(goal, list(next_actions["items"]), acceptance, constraints, latest_tests, read_files)


# LLM: _candidate_fact_roots scopes work-state reads to current workspace and compact task/run ids.
# 函数用途: 计算可读取的 task/run 事实源目录，支持旧 subagents 目录和新 tasks/*/agents 目录。
def _candidate_fact_roots(request: WorkStateFieldSourceRequest) -> list[Path]:
    workspace = Path(str(request.plan["workspace_root"]))
    scope = request.plan.get("scope", {}) if isinstance(request.plan.get("scope"), dict) else {}
    ids = _dedupe([
        str(scope.get("request_id") or ""),
        str(scope.get("session_id") or ""),
        str(scope.get("task_id") or ""),
        str(scope.get("run_id") or ""),
        *request.source_state["task_refs"],
    ])
    roots = [_path_root(workspace, item) for item in request.source_state["content_paths"]]
    for item_id in ids:
        roots.extend(_id_roots(workspace, item_id))
    return _existing_dirs(_dedupe_paths(roots), workspace)


# LLM: _id_roots maps a task/run id to legacy and task-workspace candidate directories.
# 函数用途: 根据 run_id/task_id 返回旧 subagents、新 tasks 和 tasks/*/agents 路径。
def _id_roots(workspace: Path, item_id: str) -> list[Path]:
    if not item_id:
        return []
    task_agent_roots = [
        path
        for path in sorted((workspace / "tasks").glob("*/agents/*"))
        if path.is_dir() and path.name == item_id
    ]
    return [
        workspace / "subagents" / item_id,
        workspace / "tasks" / item_id,
        workspace / "memory_archive" / "runtime_facts" / _safe_runtime_fact_id(item_id),
        *task_agent_roots,
    ]


# LLM: _safe_runtime_fact_id mirrors runtime_fact_source directory sanitization for scoped request IDs.
# 函数用途: 将 request/session/task/run id 按字面目录名解析，避免 glob 元字符扩大扫描范围。
def _safe_runtime_fact_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "run"


# LLM: _field_items gathers field values from exact files and task JSON without broad filesystem crawling.
# 函数用途: 读取候选目录里的 Markdown/JSON 事实源，返回去重后的字段项和来源路径。
def _field_items(roots: list[Path], file_names: tuple[str, ...], *, json_keys: tuple[str, ...]) -> dict[str, Any]:
    parsed = [_parsed_source(path, json_keys) for root in roots for path in _candidate_files(root, file_names)]
    parsed.extend(_parsed_source(root / "task.json", json_keys) for root in roots if (root / "task.json").exists())
    return {
        "items": _dedupe([item for items, _path in parsed for item in items]),
        "source_paths": _dedupe([path for items, path in parsed if items and path]),
    }


# LLM: _parsed_source keeps file parsing and source path pairing in one small helper.
# 函数用途: 读取单个事实源文件并返回条目及来源路径。
def _parsed_source(path: Path, json_keys: tuple[str, ...]) -> tuple[list[str], str]:
    return (_items_from_file(path, json_keys), str(path))


# LLM: _field_payload keeps unknown state explicit for resume/action guard consumers.
# 函数用途: 生成 acceptance/constraints 字段 payload，区分已记录和未记录。
def _field_payload(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "items": list(source["items"]),
        "source_status": "recorded" if source["items"] else "not_recorded",
        "source_paths": list(source["source_paths"]),
    }


# LLM: _first_item pulls single-value task metadata such as goal from candidate fact sources.
# 函数用途: 从字段扫描结果中取第一条非空文本，供 compact work_state 兜底恢复目标。
def _first_item(source: dict[str, Any]) -> str:
    return next((item for item in source["items"] if item), "")


# LLM: _test_payload mirrors field payload but keeps the historical latest_tests.status key.
# 函数用途: 生成 latest_tests 字段 payload，供 self-check 和 action guard 判断测试状态是否有事实源。
def _test_payload(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "recorded" if source["items"] else "not_recorded",
        "items": list(source["items"]),
        "source_paths": list(source["source_paths"]),
    }


# LLM: _candidate_files reads only exact known fact-source filenames under a candidate root.
# 函数用途: 返回某个 task/run 目录下匹配的事实源文件，避免递归扫描和越界读取。
def _candidate_files(root: Path, file_names: tuple[str, ...]) -> list[Path]:
    return [path for name in file_names if (path := root / name).exists() and path.is_file()]


# LLM: _items_from_file normalizes Markdown and JSON task fact files into compact work-state items.
# 函数用途: 根据文件类型读取验收、约束或测试条目；失败时返回空列表。
def _items_from_file(path: Path, json_keys: tuple[str, ...]) -> list[str]:
    if path.suffix.lower() == ".json":
        return _items_from_json(path, json_keys)
    return _items_from_markdown(path)


# LLM: _items_from_markdown extracts human checklist lines without keeping headings or prose blocks.
# 函数用途: 从 Markdown 事实源提取 bullet/checklist 行，作为 work_state 的可核对条目。
def _items_from_markdown(path: Path) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [item for line in lines if (item := _strip_markdown_item(line))]


# LLM: _items_from_json extracts known list/dict keys from task metadata JSON files.
# 函数用途: 从 task.json、failing_tests.json 等结构化事实源提取可读条目。
def _items_from_json(path: Path, keys: tuple[str, ...]) -> list[str]:
    payload = _read_json_dict(path)
    values = [item for key in keys if key in payload for item in _value_items(payload[key])]
    if not values and path.name == "failing_tests.json":
        values.extend(_value_items(payload))
    return values


# LLM: _value_items keeps JSON fact values readable without depending on a single schema version.
# 函数用途: 将字符串、列表和字典值转成简短条目，供 work_state 展示和 guard 判断。
def _value_items(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list | tuple):
        return [item for raw in value for item in _value_items(raw)]
    if isinstance(value, dict):
        return [f"{key}: {_compact_value(item)}" for key, item in value.items() if _compact_value(item)]
    return [str(value).strip()] if value not in (None, "") else []


# LLM: _strip_markdown_item keeps checklists compact and skips headings.
# 函数用途: 清理 Markdown bullet / checkbox 前缀，返回可写入 JSON 的短文本。
def _strip_markdown_item(line: str) -> str:
    text = line.strip()
    if not text or text.startswith("#") or text.startswith("```"):
        return ""
    for prefix in ("- [x]", "- [X]", "- [ ]", "- ", "* "):
        if text.startswith(prefix):
            return text[len(prefix):].strip()
    return text if len(text) <= 160 else ""


# LLM: _compact_value gives dict-based JSON facts a stable one-line representation.
# 函数用途: 将 JSON 值压成短字符串，避免 work_state 写入复杂对象。
def _compact_value(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool | int | float):
        return str(value)
    if isinstance(value, list | tuple):
        return ", ".join(str(item).strip() for item in value if str(item).strip())
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return ""


# LLM: _path_root resolves snapshot content paths only when they stay inside the workspace.
# 函数用途: 把 content_paths 转成可读取目录，防止 compact apply 越界读取本机文件。
def _path_root(workspace: Path, value: str) -> Path:
    path = Path(value)
    candidate = path if path.is_absolute() else workspace / path
    try:
        resolved = candidate.resolve()
        workspace_resolved = workspace.resolve()
    except OSError:
        return workspace
    if workspace_resolved not in (resolved, *resolved.parents):
        return workspace
    return resolved if resolved.is_dir() else resolved.parent


# LLM: _existing_dirs filters candidate roots after workspace-bound path resolution.
# 函数用途: 去除不存在或越界的目录，保证字段扫描范围可审计。
def _existing_dirs(paths: list[Path], workspace: Path) -> list[Path]:
    return [path for path in paths if path.exists() and path.is_dir() and _inside_workspace(path, workspace)]


# LLM: _inside_workspace enforces task/run fact-source reads stay below the workspace root.
# 函数用途: 判断路径是否在当前 workspace 内，避免读取用户其他目录。
def _inside_workspace(path: Path, workspace: Path) -> bool:
    try:
        resolved = path.resolve()
        root = workspace.resolve()
    except OSError:
        return False
    return root in (resolved, *resolved.parents)


# LLM: _read_json_dict is a best-effort reader for existing fact-source snapshots.
# 函数用途: 读取 JSON 对象，失败时返回空 dict，避免 compact apply 因单个旧文件损坏而崩溃。
def _read_json_dict(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


# LLM: _dedupe_paths preserves source priority while removing duplicate Path objects.
# 函数用途: 对候选事实源目录去重，保持 workspace、旧 run、新 run 的读取顺序稳定。
def _dedupe_paths(values: list[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for value in values:
        key = str(value)
        if key not in seen:
            result.append(value)
            seen.add(key)
    return result


# LLM: _dedupe preserves source order for short string lists.
# 函数用途: 去重字段条目和来源路径，避免同一事实源重复写入 work_state。
def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


__all__ = ["WorkStateFieldSourceRequest", "WorkStateFieldSources", "build_work_state_field_sources"]
