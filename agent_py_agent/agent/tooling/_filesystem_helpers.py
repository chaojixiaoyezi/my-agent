

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
        "context_bundle.json",
        "CONTEXT_BUNDLE.md",
    }
)
_INTERNAL_AGENT_STATUS_DIRS = frozenset({"compactions", "progress"})


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


def _normalized_workspace_roots(primary: Path, roots: list[Path] | None) -> list[Path]:
    resolved: list[Path] = []
    for raw in [primary, *(roots or [])]:
        path = Path(raw).resolve()
        if path not in resolved:
            resolved.append(path)
    return resolved


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


def _internal_agent_status_payload(path: Path, run_id: str) -> dict[str, object]:
    suggestion: dict[str, object] = {"tool": "inspect_agent_tree"}
    if run_id:
        suggestion["run_id"] = run_id
    payload: dict[str, object] = {
        "ok": False,
        "error": "internal_agent_status_ref",
        "message": "This path is an internal agent status surface; do not list_files/copy it directly. The child's real output files are listed in child_result_index_row.read_order below — read_file those paths directly. Use inspect_agent_tree only for run status.",
        "run_id": run_id,
        "path": str(path),
        "suggested_tool_call": suggestion,
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
