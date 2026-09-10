# LLM: 文件参数与内部状态引用的共享读取辅助；上下文交接材料不是运行状态 API，读取仍服从 owner/path 边界。
# 模块用途: 校验文件参数、定位可读产物并说明误读内部状态的原因；不能从文档内容推导执行权限。
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_MAX_PATH_CHARS = 4096
_MAX_SEARCH_QUERY_CHARS = 4000
_MAX_SEARCH_LINE_CHARS = 500
_MAX_WRITE_TEXT_CHARS = 1_000_000
_INTERNAL_AGENT_STATUS_FILES = frozenset(
    {
        "final_report.md",
        "state.json",
        "summary.md",
        "checkpoint.json",
        "canonical_state.json",
    }
)
_INTERNAL_AGENT_STATUS_DIRS = frozenset({"compactions", "progress"})
_IGNORED_DISCOVERY_FALLBACK_NOTICE = (
    "说明：显式 glob 在默认可见文件中没有命中，已自动检查常见忽略目录；"
    "如需严格排除这些目录，请显式传 include_ignored=false。"
)


@dataclass(frozen=True)
class TextParamOptions:
    name: str = "value"
    max_chars: int = _MAX_WRITE_TEXT_CHARS
    allow_empty: bool = False
    strip: bool = False


def _has_control_chars(text: str) -> bool:
    return any(ord(char) < 32 for char in text)


def _required_path(value: Any, *, name: str = "path") -> str:
    if value is None:
        raise ValueError(f"缺少必填参数 {name}")
    if not isinstance(value, (str, Path)):
        raise ValueError(f"{name} 参数必须是字符串路径")
    text = str(value).strip()
    if not text:
        raise ValueError(f"{name} 不能为空")
    if len(text) > _MAX_PATH_CHARS:
        raise ValueError(f"{name} 过长，最多 {_MAX_PATH_CHARS} 个字符")
    if _has_control_chars(text):
        raise ValueError(f"{name} 包含不支持的控制字符")
    return text


def _optional_path(value: Any, *, default: str = ".") -> str:
    if value is None:
        return default
    return _required_path(value)


def _text_param(
    value: Any,
    *,
    options: TextParamOptions | None = None,
    name: str = "value",
    max_chars: int = _MAX_WRITE_TEXT_CHARS,
    allow_empty: bool = False,
    strip: bool = False,
) -> str:
    values = options or TextParamOptions(name, max_chars, allow_empty, strip)
    name = str(values.name)
    max_chars = int(values.max_chars)
    allow_empty = bool(values.allow_empty)
    strip = bool(values.strip)
    if value is None:
        raise ValueError(f"缺少必填参数 {name}")
    if not isinstance(value, (str, int, float, bool)):
        raise ValueError(f"{name} 参数必须是字符串或标量文本")
    text = str(value)
    if strip:
        text = text.strip()
    if not allow_empty and text == "":
        raise ValueError(f"{name} 不能为空")
    if len(text) > max_chars:
        raise ValueError(f"{name} 过长，最多 {max_chars} 个字符")
    return text


def _int_param(value: Any, *, name: str, default: int, min_value: int | None = None) -> int:
    if value is None:
        parsed = default
    else:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} 必须是整数") from exc
    if min_value is not None and parsed < min_value:
        raise ValueError(f"{name} 不能小于 {min_value}")
    return parsed


def _bool_param(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true"}:
            return True
        if text in {"0", "false"}:
            return False
    return default


def _discovery_result_envelope(
    page_window: dict[str, object],
    *,
    included_ignored_fallback: bool,
) -> dict[str, object]:
    envelope: dict[str, object] = {"page_window": page_window}
    if included_ignored_fallback:
        envelope["discovery"] = {
            "included_ignored": True,
            "reason": "explicit_pattern_no_visible_matches",
        }
    return envelope


def _ignored_discovery_fallback_notice() -> str:
    return _IGNORED_DISCOVERY_FALLBACK_NOTICE


def _normalized_workspace_roots(primary: Path, roots: list[Path] | None) -> list[Path]:
    resolved: list[Path] = []
    for raw in [primary, *(roots or [])]:
        path = Path(raw).resolve()
        if path not in resolved:
            resolved.append(path)
    return resolved


# LLM: 仅把运行控制/状态投影转为结构化指引；runner 显式交给模型的 context bundle 必须可按普通文件读取。
# 函数用途: 识别误当产物读取的内部状态路径，不阻止子代理阅读自己的任务上下文材料。
def _internal_agent_status_ref(
    path: Path,
    *,
    include_agent_directory: bool = False,
) -> dict[str, object] | None:
    parts = path.parts
    for index in range(len(parts) - 1):
        if parts[index] != "work" or index + 1 >= len(parts) or parts[index + 1] != "agents":
            continue
        relative = parts[index + 2 :]
        if include_agent_directory and not relative:
            return _internal_agent_status_payload(path, "")
        if not relative:
            continue
        run_id = relative[0]
        if not str(run_id).startswith(("subagent-", "run-")):
            continue
        if include_agent_directory:
            return _internal_agent_status_payload(path, run_id)
        leaf = relative[1] if len(relative) > 1 else ""
        if leaf not in _INTERNAL_AGENT_STATUS_FILES and leaf not in _INTERNAL_AGENT_STATUS_DIRS:
            continue
        return _internal_agent_status_payload(path, run_id)
    return None


# LLM: The host-generated final report is the one model-visible handoff file inside an agent
# workspace. This exact leaf exception must not authorize sibling state files or directory access.
# 函数用途: 判断路径是否为完成信封明确暴露、可由 read_file 读取的精确子代理交接报告。
def _readable_agent_final_report(path: Path) -> bool:
    parts = path.parts
    for index in range(len(parts) - 1):
        if parts[index : index + 2] != ("work", "agents"):
            continue
        relative = parts[index + 2 :]
        return bool(
            len(relative) == 2
            and str(relative[0]).startswith(("subagent-", "run-"))
            and relative[1] == "final_report.md"
        )
    return False


# LLM: A model may retain the correct run id but combine it with a stale durable-task
# directory. Resolve only the exact final_report leaf through the owner's typed agent
# projection, then validate the canonical run directory and every filesystem boundary.
# 函数用途: 当子代理报告路径里的任务目录过期时，按唯一 run_id 找回其真实最终报告；其他内部文件不做跳转。
def _canonical_agent_final_report_target(
    path: Path,
) -> Path | None:
    location = _agent_final_report_location(path)
    if location is None:
        return None
    owner_home, run_id = location
    state = _read_json_object(owner_home / "agents" / run_id / "state.json")
    if str(state.get("run_id") or "").strip() != run_id:
        return None
    final_ref = str(state.get("final_report_ref") or "").strip()
    run_dir_ref = str(state.get("agent_run_workspace_dir") or "").strip()
    if not final_ref or not run_dir_ref:
        return None
    try:
        owner_root = owner_home.expanduser().resolve(strict=False)
        run_dir = Path(run_dir_ref).expanduser().resolve(strict=False)
        candidate = Path(final_ref).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None
    if (
        run_dir.name != run_id
        or candidate != run_dir / "final_report.md"
        or not _readable_agent_final_report(candidate)
        or not candidate.is_file()
        or not _path_inside(candidate, owner_root)
    ):
        return None
    return candidate


# LLM: Owner lookup is permitted only for the canonical tasks/.../work/agents/<run>/final_report
# shape. The run id remains opaque and no fuzzy basename search participates in authority.
# 函数用途: 从一个精确子代理报告地址拆出 owner home 和 run_id，供权威索引定位使用。
def _agent_final_report_location(path: Path) -> tuple[Path, str] | None:
    parts = path.expanduser().resolve(strict=False).parts
    for work_index in range(len(parts) - 3):
        if parts[work_index : work_index + 2] != ("work", "agents"):
            continue
        relative = parts[work_index + 2 :]
        if len(relative) != 2 or relative[1] != "final_report.md":
            continue
        run_id = str(relative[0])
        if not run_id.startswith(("subagent-", "run-")):
            return None
        task_indexes = [
            index for index, part in enumerate(parts[:work_index]) if part == "tasks"
        ]
        if not task_indexes:
            return None
        return Path(*parts[: task_indexes[-1]]), run_id
    return None


# LLM: Path containment must use resolved path components, never string prefixes.
# 函数用途: 判断一个已解析路径是否位于给定根目录内。
def _path_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


# LLM: 内部 agent 文件不是模型状态 API；拒绝结果只给事件等待和真实产物读取顺序，
# 不能建议已从模型 surface 删除的 inspect 工具。
# 函数用途: 把误读子代理内部状态文件转换成安全的结构化指引。
def _internal_agent_status_payload(path: Path, run_id: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "ok": False,
        "error": "internal_agent_status_ref",
        "message": "This path is an internal agent status surface; do not list_files/copy it directly. Wait for the direct-child lifecycle event; the child's real output files are listed in child_result_index_row.read_order below.",
        "run_id": run_id,
        "path": str(path),
        "next_action": "await_direct_child_lifecycle_event",
        "result_fields_to_read": ["child_result_index.read_order", "child_result_index.expected_outputs"],
    }
    result_surface = _internal_agent_result_surface(path, run_id)
    if result_surface:
        payload["child_result_index_row"] = result_surface
    return payload


def _internal_agent_result_surface(path: Path, run_id: str) -> dict[str, object]:
    run_dir = _internal_agent_run_dir(path, run_id)
    if run_dir is None:
        return {}
    state = _read_json_object(run_dir / "canonical_state.json")
    if not state:
        state = _read_json_object(run_dir / "state.json")
    if not state:
        return {}
    attrs = state.get("attributes") if isinstance(state.get("attributes"), dict) else {}
    artifact_registry_refs = _artifact_registry_refs(attrs)
    artifact_refs = _string_list(state.get("artifact_refs"))
    declared_output_refs = _declared_output_refs(state, attrs)
    primary_refs = _existing_ref_paths([
        *[str(item.get("path") or "") for item in artifact_registry_refs],
        *artifact_refs,
        *declared_output_refs,
    ])
    return {
        "run_id": run_id,
        "status": _text(state.get("status")),
        "verification_status": _text(state.get("verification_status")),
        "read_order": primary_refs,
        "primary_artifact_refs": primary_refs,
        "primary_artifact_stats": _artifact_stats(primary_refs),
        "expected_outputs": declared_output_refs,
        "artifact_registry_refs": artifact_registry_refs,
        "last_progress_summary": _text(state.get("last_progress_summary")),
    }


def _internal_agent_run_dir(path: Path, run_id: str) -> Path | None:
    if not run_id:
        return None
    parts = path.parts
    for index in range(len(parts) - 2):
        if parts[index] == "work" and parts[index + 1] == "agents" and parts[index + 2] == run_id:
            return Path(*parts[: index + 3])
    return None


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _artifact_registry_refs(attrs: object) -> list[dict[str, object]]:
    if not isinstance(attrs, dict):
        return []
    rows = attrs.get("artifact_registry_refs")
    if not isinstance(rows, list):
        return []
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in rows:
        if not isinstance(item, dict):
            continue
        path = _text(item.get("path"))
        if not path or path in seen:
            continue
        seen.add(path)
        result.append({
            "artifact_id": _text(item.get("artifact_id")),
            "path": path,
            "kind": _text(item.get("kind")),
            "status": _text(item.get("status")),
            "size_bytes": item.get("size_bytes") if isinstance(item.get("size_bytes"), int) else 0,
        })
    return result


def _declared_output_refs(state: dict[str, object], attrs: object) -> list[str]:
    refs: list[str] = []
    for source in (state, attrs if isinstance(attrs, dict) else {}):
        for key in ("declared_output_refs", "output_files", "output_refs", "artifact_refs"):
            refs.extend(_string_list(source.get(key)))
    return _unique_strings(refs)


def _existing_ref_paths(paths: list[str]) -> list[str]:
    refs: list[str] = []
    for raw in paths:
        if text := _existing_ref_path(raw):
            refs.append(text)
    return _unique_strings(refs)


def _existing_ref_path(raw: object) -> str:
    text = _text(raw)
    if not text:
        return ""
    try:
        return text if Path(text).expanduser().is_file() else ""
    except OSError:
        return ""


def _artifact_stats(paths: list[str]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for raw in paths:
        path = Path(raw).expanduser()
        try:
            stat = path.stat()
        except OSError:
            continue
        row: dict[str, object] = {"path": str(raw), "size_bytes": stat.st_size}
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            rows.append(row)
            continue
        row["line_count"] = len(text.splitlines())
        row["char_count"] = len(text)
        rows.append(row)
    return rows


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list | tuple | set):
        return []
    return [_text(item) for item in value if _text(item)]


def _unique_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = _text(value)
        if text and text not in result:
            result.append(text)
    return result


def _text(value: object) -> str:
    return str(value or "").strip()


def _is_under_any_root(path: Path, roots: list[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _read_text_safe(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None


def _parse_count_param(value: Any) -> int:
    if value is None:
        return 1
    try:
        return int(value)
    except (TypeError, ValueError):
        return 1
