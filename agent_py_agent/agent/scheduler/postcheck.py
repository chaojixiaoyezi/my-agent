"""定时任务完成后的宿主机只读产物后验契约与纯评估器。"""
# LLM: 后验评估只读运行根目录下的显式相对路径与最后一段单层通配，禁止符号链接越界、绝对路径、正文泄漏或无界扫描。
# 模块用途: 提供 POSTCHECK_SCHEMA、配置规范化函数 normalize_postcheck 与无锁纯只读评估函数 evaluate_postcheck。
from __future__ import annotations

import errno
import fnmatch
import json
import math
import os
import re
import stat
import sys
from pathlib import Path

from ..common.nofollow_fs import open_readonly_file_beneath
from .schedule import ScheduleValidationError

# 数值边界收拢在私有映射内，避免模块级数值常量被静态目录重复登记。
_LIMITS = {
    "max_artifacts": 8,
    "max_path_length": 512,
    "max_min_lines": 10_000_000,
    "max_age_seconds": 604_800,
    "max_scan_entries": 64,
    "line_chunk_bytes": 65_536,
}

_ALLOWED_ARTIFACT_KEYS = frozenset(
    {
        "path",
        "min_bytes",
        "min_lines",
        "max_age_seconds",
        "json_pointer",
        "equals",
        "min_value",
    }
)
_BUSINESS_CODES = frozenset(
    {
        "ARTIFACT_MISSING",
        "ARTIFACT_TOO_SMALL",
        "ARTIFACT_TOO_FEW_LINES",
        "ARTIFACT_JSON_MISMATCH",
    }
)
_STALLED_CODES = frozenset({"ARTIFACT_STALE"})
_UNAVAILABLE_CODES = frozenset(
    {
        "ARTIFACT_UNREADABLE",
        "ARTIFACT_OUTSIDE_ROOT",
        "ARTIFACT_SCAN_BUDGET_EXHAUSTED",
    }
)
_RFC6901_TOKEN_RE = re.compile(r"(?:[^~]|~[01])*")
_ARRAY_INDEX_RE = re.compile(r"0|[1-9][0-9]*")

POSTCHECK_CHECK_CODES = (
    "ARTIFACT_MISSING",
    "ARTIFACT_TOO_SMALL",
    "ARTIFACT_TOO_FEW_LINES",
    "ARTIFACT_STALE",
    "ARTIFACT_JSON_MISMATCH",
    "ARTIFACT_OUTSIDE_ROOT",
    "ARTIFACT_UNREADABLE",
    "ARTIFACT_SCAN_BUDGET_EXHAUSTED",
)

POSTCHECK_SCHEMA = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "description": (
        "可选的宿主机只读产物后验规则：在运行确认 done 前检查执行根目录下 1 到 8 个产物的存在性、"
        "大小、行数、新鲜度或 JSON Pointer 标量/数值下界。省略或传 null 关闭。"
    ),
    "properties": {
        "artifacts": {
            "type": "array",
            "minItems": 1,
            "maxItems": _LIMITS["max_artifacts"],
            "description": "待检查产物规则数组（1 到 8 项）；每项须指定相对路径与至少一个判定条件。",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path"],
                "properties": {
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": _LIMITS["max_path_length"],
                        "description": (
                            "相对本次运行根目录的正斜杠路径（1～512 字符），"
                            "仅最后一段允许含通配符 *；禁止绝对路径与 ..。"
                        ),
                    },
                    "min_bytes": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "产物最小字节数（>= 0 的整数）。",
                    },
                    "min_lines": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": _LIMITS["max_min_lines"],
                        "description": "产物最少行数（0 到 10000000 的整数）。",
                    },
                    "max_age_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": _LIMITS["max_age_seconds"],
                        "description": "产物允许的最大修改距今秒数（1 到 604800 的整数，超时判定停滞）。",
                    },
                    "json_pointer": {
                        "type": "string",
                        "description": (
                            "RFC 6901 JSON 指针（空串表示根节点，或以 / 开头）；"
                            "要求文件为不超过读取上限的 JSON，并与 equals 或 min_value 搭配。"
                        ),
                    },
                    "equals": {
                        "type": ["string", "number", "boolean", "null"],
                        "description": "json_pointer 命中值的期望 JSON 标量（字符串、有限数字、布尔或 null）。",
                    },
                    "min_value": {
                        "type": "number",
                        "description": "json_pointer 命中数值的最小下界（闭区间 >= min_value）。",
                    },
                },
            },
        },
    },
    "required": ["artifacts"],
}


def normalize_postcheck(value: object) -> dict[str, object] | None:
    """校验并规范化 postcheck 契约，精确在报错中指明字段路径与修正动作。"""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ScheduleValidationError(
            "postcheck 必须是对象或 null；尚未保存计划，下一步：传入包含 artifacts 列表的对象或传 null 关闭。"
        )
    unknown_top = sorted(str(key) for key in value if key != "artifacts")
    if unknown_top:
        raise ScheduleValidationError(
            f"postcheck.{unknown_top[0]} 是不支持的字段（发现额外字段：{', '.join(unknown_top)}）；"
            "尚未保存计划，下一步：仅保留 postcheck.artifacts 字段后重试。"
        )
    if "artifacts" not in value:
        raise ScheduleValidationError(
            "postcheck.artifacts 缺失；尚未保存计划，下一步：传入包含 1 到 8 项规则的 artifacts 数组。"
        )
    raw_artifacts = value["artifacts"]
    max_artifacts = _LIMITS["max_artifacts"]
    if not isinstance(raw_artifacts, list) or not (1 <= len(raw_artifacts) <= max_artifacts):
        raise ScheduleValidationError(
            f"postcheck.artifacts 必须是包含 1 到 {max_artifacts} 项规则的数组；"
            "尚未保存计划，下一步：将 artifacts 调整为 1～8 项有效规则。"
        )
    normalized = [_normalize_artifact(item, index) for index, item in enumerate(raw_artifacts)]
    return {"artifacts": normalized}


def _normalize_artifact(raw_item: object, index: int) -> dict[str, object]:
    prefix = f"postcheck.artifacts[{index}]"
    if not isinstance(raw_item, dict):
        raise ScheduleValidationError(
            f"{prefix} 必须是对象；尚未保存计划，下一步：为该条目提供 path 与至少一个判定条件。"
        )
    unknown = sorted(str(key) for key in raw_item if key not in _ALLOWED_ARTIFACT_KEYS)
    if unknown:
        raise ScheduleValidationError(
            f"{prefix}.{unknown[0]} 是不支持的字段（发现：{', '.join(unknown)}）；"
            "尚未保存计划，下一步：移除未知字段，仅使用 path/min_bytes/min_lines/max_age_seconds/json_pointer/equals/min_value。"
        )
    normalized: dict[str, object] = {"path": _validate_relative_path(raw_item.get("path"), f"{prefix}.path")}

    if "min_bytes" in raw_item:
        min_bytes = raw_item["min_bytes"]
        if type(min_bytes) is not int or min_bytes < 0:
            raise ScheduleValidationError(
                f"{prefix}.min_bytes 必须是非负整数（>= 0）；尚未保存计划，下一步：传入非负整数或移除 min_bytes。"
            )
        normalized["min_bytes"] = min_bytes

    if "min_lines" in raw_item:
        min_lines = raw_item["min_lines"]
        max_min_lines = _LIMITS["max_min_lines"]
        if type(min_lines) is not int or not (0 <= min_lines <= max_min_lines):
            raise ScheduleValidationError(
                f"{prefix}.min_lines 必须是 0 到 {max_min_lines} 的整数；"
                "尚未保存计划，下一步：调整 min_lines 到合法区间或移除该字段。"
            )
        normalized["min_lines"] = min_lines

    if "max_age_seconds" in raw_item:
        max_age = raw_item["max_age_seconds"]
        max_age_limit = _LIMITS["max_age_seconds"]
        if type(max_age) is not int or not (1 <= max_age <= max_age_limit):
            raise ScheduleValidationError(
                f"{prefix}.max_age_seconds 必须是 1 到 {max_age_limit} 的整数；"
                "尚未保存计划，下一步：传入 1～604800 秒整数或移除 max_age_seconds。"
            )
        normalized["max_age_seconds"] = max_age

    has_pointer = "json_pointer" in raw_item
    has_equals = "equals" in raw_item
    has_min_value = "min_value" in raw_item
    if has_equals and not has_pointer:
        raise ScheduleValidationError(
            f"{prefix}.equals 必须与 json_pointer 一起使用；尚未保存计划，下一步：补充 json_pointer（根节点传 \"\"）或移除 equals。"
        )
    if has_min_value and not has_pointer:
        raise ScheduleValidationError(
            f"{prefix}.min_value 必须与 json_pointer 一起使用；尚未保存计划，下一步：补充 json_pointer（根节点传 \"\"）或移除 min_value。"
        )
    if has_pointer:
        pointer = _validate_json_pointer(raw_item["json_pointer"], f"{prefix}.json_pointer")
        if not has_equals and not has_min_value:
            raise ScheduleValidationError(
                f"{prefix}.json_pointer 必须搭配 equals 或 min_value；"
                "尚未保存计划，下一步：补充 equals 或 min_value，或移除 json_pointer。"
            )
        normalized["json_pointer"] = pointer
        if has_equals:
            normalized["equals"] = _validate_json_scalar(raw_item["equals"], f"{prefix}.equals")
        if has_min_value:
            normalized["min_value"] = _validate_finite_number(raw_item["min_value"], f"{prefix}.min_value")

    if not any(key in normalized for key in ("min_bytes", "min_lines", "max_age_seconds", "json_pointer")):
        raise ScheduleValidationError(
            f"{prefix} 至少需要一个检查条件（min_bytes/min_lines/max_age_seconds/json_pointer）；"
            "尚未保存计划，下一步：补充至少一个产物判定条件。"
        )
    return normalized


def _validate_relative_path(raw_path: object, field_path: str) -> str:
    max_len = _LIMITS["max_path_length"]
    if (
        type(raw_path) is not str
        or not raw_path
        or raw_path.strip() != raw_path
        or len(raw_path) > max_len
        or "\x00" in raw_path
        or "\\" in raw_path
    ):
        raise ScheduleValidationError(
            f"{field_path} 必须是 1 到 {max_len} 字符的非空正斜杠相对路径，不能含反斜杠、NUL 或首尾空白；"
            "尚未保存计划，下一步：改为如 out/summary.json 或 out/*.jsonl。"
        )
    if (
        raw_path.startswith("/")
        or (len(raw_path) >= 2 and raw_path[1] == ":" and raw_path[0].isalpha())
        or Path(raw_path).is_absolute()
    ):
        raise ScheduleValidationError(
            f"{field_path} 不能是绝对路径；尚未保存计划，下一步：改为相对运行根目录的相对路径。"
        )
    segments = raw_path.split("/")
    if any(seg == "" for seg in segments):
        raise ScheduleValidationError(
            f"{field_path} 不能包含空路径段或以 '/' 结尾；尚未保存计划，下一步：移除多余斜杠并指定具体文件段。"
        )
    if any(seg == ".." for seg in segments):
        raise ScheduleValidationError(
            f"{field_path} 不能包含 '..'；尚未保存计划，下一步：改为运行根目录内的正向相对路径。"
        )
    if any(seg == "." for seg in segments):
        raise ScheduleValidationError(
            f"{field_path} 不能包含 '.' 段；尚未保存计划，下一步：直接写相对路径（如 summary.json）。"
        )
    if any("*" in seg for seg in segments[:-1]):
        raise ScheduleValidationError(
            f"{field_path} 只允许最后一段包含通配符 '*'；尚未保存计划，下一步：固定前置目录路径，仅在文件名段使用 '*'。"
        )
    return raw_path


def _validate_json_pointer(raw_pointer: object, field_path: str) -> str:
    if type(raw_pointer) is not str:
        raise ScheduleValidationError(
            f"{field_path} 必须是字符串（空串或以 '/' 开头）；尚未保存计划，下一步：传入合法 RFC 6901 指针。"
        )
    if raw_pointer == "":
        return ""
    if not raw_pointer.startswith("/"):
        raise ScheduleValidationError(
            f"{field_path} 非空时必须以 '/' 开头；尚未保存计划，下一步：改为如 /status 或根节点空串 \"\"。"
        )
    for token in raw_pointer[1:].split("/"):
        if _RFC6901_TOKEN_RE.fullmatch(token) is None:
            raise ScheduleValidationError(
                f"{field_path} 包含非法 RFC 6901 转义（~ 后只能接 0 或 1）；"
                "尚未保存计划，下一步：修正转义序列（~0 表示 ~，~1 表示 /）。"
            )
    return raw_pointer


def _validate_json_scalar(value: object, field_path: str) -> str | int | float | bool | None:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ScheduleValidationError(
        f"{field_path} 必须是 JSON 标量（字符串、有限数字、布尔或 null）；"
        "尚未保存计划，下一步：改为单个标量值后再提交。"
    )


def _validate_finite_number(value: object, field_path: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ScheduleValidationError(
            f"{field_path} 必须是有限数字（不能为布尔、字符串或 NaN/Inf）；"
            "尚未保存计划，下一步：传入数值型下界后再提交。"
        )
    return value


def evaluate_postcheck(
    spec: dict[str, object],
    *,
    root: Path,
    now: float,
    read_bytes: int,
) -> dict[str, object]:
    """在锁外以纯只读方式核验运行根目录下声明的产物契约，观测字段只保留数值与相对路径。"""
    normalized = normalize_postcheck(spec)
    if normalized is None:
        raise ScheduleValidationError("postcheck 不能为空；下一步：传入有效的 postcheck 规范对象。")
    now_value = float(now)
    budget_bytes = 0 if isinstance(read_bytes, bool) else max(0, int(read_bytes))
    resolved_root, root_error = _resolve_root(Path(root))
    checks = [
        _evaluate_artifact(
            artifact,
            raw_root=Path(root),
            resolved_root=resolved_root,
            root_error=root_error,
            now=now_value,
            read_bytes=budget_bytes,
        )
        for artifact in normalized["artifacts"]  # type: ignore[index]
    ]
    status, failure_kind = _aggregate_status(checks)
    return {
        "status": status,
        "failure_kind": failure_kind,
        "checks": checks,
        "evaluated_at": now_value,
    }


def _resolve_root(root: Path) -> tuple[Path | None, str]:
    try:
        return root.resolve(), ""
    except OSError as exc:
        if getattr(exc, "errno", None) == errno.ELOOP:
            return None, "ARTIFACT_OUTSIDE_ROOT"
        return None, "ARTIFACT_UNREADABLE"


def _aggregate_status(checks: list[dict[str, object]]) -> tuple[str, str]:
    codes = {str(item.get("code") or "") for item in checks}
    if codes & _BUSINESS_CODES:
        return "failed", "business"
    if codes & _STALLED_CODES:
        return "stalled", "stalled"
    if codes & _UNAVAILABLE_CODES:
        return "unknown", "postcheck_unavailable"
    return "passed", ""


def _evaluate_artifact(
    artifact: dict[str, object],
    *,
    raw_root: Path,
    resolved_root: Path | None,
    root_error: str,
    now: float,
    read_bytes: int,
) -> dict[str, object]:
    rel_spec = str(artifact["path"])
    if root_error or resolved_root is None:
        return {"path": rel_spec, "code": root_error or "ARTIFACT_UNREADABLE", "observed": {}}

    parts = rel_spec.split("/")
    parent_parts = parts[:-1]
    last_segment = parts[-1]
    parent_rel = "/".join(parent_parts)
    candidate_parent = raw_root.joinpath(*parent_parts) if parent_parts else raw_root

    try:
        resolved_parent = candidate_parent.resolve()
    except OSError as exc:
        code = "ARTIFACT_OUTSIDE_ROOT" if getattr(exc, "errno", None) == errno.ELOOP else "ARTIFACT_UNREADABLE"
        return {"path": rel_spec, "code": code, "observed": {}}

    if not resolved_parent.is_relative_to(resolved_root):
        return {"path": rel_spec, "code": "ARTIFACT_OUTSIDE_ROOT", "observed": {}}

    if "*" in last_segment:
        target_path, reported_path, target_stat, observed, discover_code = _discover_glob_target(
            resolved_parent=resolved_parent,
            resolved_root=resolved_root,
            parent_rel=parent_rel,
            rel_spec=rel_spec,
            pattern=last_segment,
        )
    else:
        target_path, reported_path, target_stat, observed, discover_code = _discover_exact_target(
            resolved_parent=resolved_parent,
            resolved_root=resolved_root,
            rel_spec=rel_spec,
            filename=last_segment,
        )

    if discover_code:
        return {"path": reported_path, "code": discover_code, "observed": observed}

    assert target_path is not None and target_stat is not None
    age_seconds = max(0.0, now - float(target_stat.st_mtime))
    observed["bytes"] = int(target_stat.st_size)
    observed["age_seconds"] = age_seconds

    code = _evaluate_predicates(
        artifact=artifact,
        resolved_root=resolved_root,
        rel_parts=tuple(reported_path.split("/")),
        target_stat=target_stat,
        observed=observed,
        now=now,
        read_bytes=read_bytes,
    )
    return {"path": reported_path, "code": code, "observed": observed}


def _discover_exact_target(
    *,
    resolved_parent: Path,
    resolved_root: Path,
    rel_spec: str,
    filename: str,
) -> tuple[Path | None, str, os.stat_result | None, dict[str, int | float], str]:
    candidate = resolved_parent / filename
    try:
        st = os.lstat(candidate)
    except FileNotFoundError:
        return None, rel_spec, None, {"match_count": 0}, "ARTIFACT_MISSING"
    except NotADirectoryError:
        return None, rel_spec, None, {"match_count": 0}, "ARTIFACT_MISSING"
    except OSError as exc:
        if getattr(exc, "errno", None) == errno.ELOOP:
            return None, rel_spec, None, {}, "ARTIFACT_OUTSIDE_ROOT"
        return None, rel_spec, None, {}, "ARTIFACT_UNREADABLE"

    if stat.S_ISLNK(st.st_mode):
        return None, rel_spec, None, {}, "ARTIFACT_OUTSIDE_ROOT"
    if not candidate.resolve().is_relative_to(resolved_root):
        return None, rel_spec, None, {}, "ARTIFACT_OUTSIDE_ROOT"
    if not stat.S_ISREG(st.st_mode):
        return None, rel_spec, None, {"match_count": 0}, "ARTIFACT_UNREADABLE"
    return candidate, rel_spec, st, {"match_count": 1}, ""


def _discover_glob_target(
    *,
    resolved_parent: Path,
    resolved_root: Path,
    parent_rel: str,
    rel_spec: str,
    pattern: str,
) -> tuple[Path | None, str, os.stat_result | None, dict[str, int | float], str]:
    max_entries = _LIMITS["max_scan_entries"]
    match_count = 0
    examined = 0
    best_key: tuple[int, str] | None = None
    best_path: Path | None = None
    best_rel = rel_spec
    best_stat: os.stat_result | None = None

    try:
        with os.scandir(resolved_parent) as entries:
            for entry in entries:
                examined += 1
                if examined > max_entries:
                    return (
                        None,
                        rel_spec,
                        None,
                        {"match_count": match_count, "entries_examined": examined},
                        "ARTIFACT_SCAN_BUDGET_EXHAUSTED",
                    )
                if not fnmatch.fnmatchcase(entry.name, pattern):
                    continue
                matched_rel = f"{parent_rel}/{entry.name}" if parent_rel else entry.name
                try:
                    if entry.is_symlink():
                        return None, matched_rel, None, {}, "ARTIFACT_OUTSIDE_ROOT"
                    st = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    if getattr(exc, "errno", None) == errno.ELOOP:
                        return None, matched_rel, None, {}, "ARTIFACT_OUTSIDE_ROOT"
                    return None, matched_rel, None, {}, "ARTIFACT_UNREADABLE"

                if not stat.S_ISREG(st.st_mode):
                    continue
                entry_path = Path(entry.path)
                if not entry_path.resolve().is_relative_to(resolved_root):
                    return None, matched_rel, None, {}, "ARTIFACT_OUTSIDE_ROOT"

                match_count += 1
                sort_key = (int(st.st_mtime_ns), entry.name)
                if best_key is None or sort_key > best_key:
                    best_key = sort_key
                    best_path = entry_path
                    best_rel = matched_rel
                    best_stat = st
    except (FileNotFoundError, NotADirectoryError):
        return None, rel_spec, None, {"match_count": 0}, "ARTIFACT_MISSING"
    except OSError as exc:
        if getattr(exc, "errno", None) == errno.ELOOP:
            return None, rel_spec, None, {}, "ARTIFACT_OUTSIDE_ROOT"
        return None, rel_spec, None, {}, "ARTIFACT_UNREADABLE"

    if best_path is None or best_stat is None:
        return None, rel_spec, None, {"match_count": 0}, "ARTIFACT_MISSING"
    return best_path, best_rel, best_stat, {"match_count": match_count}, ""


def _stat_matches_snapshot(expected: os.stat_result, actual: os.stat_result) -> bool:
    if not stat.S_ISREG(actual.st_mode):
        return False
    fields = ("st_dev", "st_ino", "st_nlink", "st_size", "st_mtime_ns")
    if sys.platform != "win32":
        fields += ("st_ctime_ns",)
    return all(getattr(expected, name) == getattr(actual, name) for name in fields)


def _open_artifact_readonly(resolved_root: Path, rel_parts: tuple[str, ...]) -> int:
    if sys.platform != "win32":
        return open_readonly_file_beneath(resolved_root, rel_parts)
    if not rel_parts or any(not p or p in {".", ".."} or Path(p).name != p for p in rel_parts):
        raise ValueError("invalid relative artifact path")
    root_st = os.lstat(resolved_root)
    if stat.S_ISLNK(root_st.st_mode) or not stat.S_ISDIR(root_st.st_mode):
        raise OSError(errno.ELOOP, "unsafe postcheck root")
    current = resolved_root
    for part in rel_parts[:-1]:
        current = current / part
        dir_st = os.lstat(current)
        if stat.S_ISLNK(dir_st.st_mode):
            raise OSError(errno.ELOOP, "symlink parent component")
        if not stat.S_ISDIR(dir_st.st_mode):
            raise OSError(errno.ENOTDIR, "non-directory parent component")
    target = current / rel_parts[-1]
    target.relative_to(resolved_root)
    leaf_st = os.lstat(target)
    if stat.S_ISLNK(leaf_st.st_mode):
        raise OSError(errno.ELOOP, "symlink artifact leaf")
    if not stat.S_ISREG(leaf_st.st_mode) or int(leaf_st.st_nlink) != 1:
        raise OSError(errno.EINVAL, "non-regular artifact leaf")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.open(target, flags)


def _evaluate_predicates(
    *,
    artifact: dict[str, object],
    resolved_root: Path,
    rel_parts: tuple[str, ...],
    target_stat: os.stat_result,
    observed: dict[str, int | float],
    now: float,
    read_bytes: int,
) -> str:
    needs_lines = "min_lines" in artifact
    needs_json = "json_pointer" in artifact
    read_error_code = ""

    try:
        fd = _open_artifact_readonly(resolved_root, rel_parts)
    except (OSError, ValueError) as exc:
        if getattr(exc, "errno", None) == errno.ELOOP:
            return "ARTIFACT_OUTSIDE_ROOT"
        return "ARTIFACT_UNREADABLE"

    try:
        fh = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise

    try:
        with fh:
            fstat_res = os.fstat(fh.fileno())
            if not _stat_matches_snapshot(target_stat, fstat_res):
                return "ARTIFACT_UNREADABLE"
            verified_age = max(0.0, now - float(fstat_res.st_mtime))
            observed["bytes"] = int(fstat_res.st_size)
            observed["age_seconds"] = verified_age

            min_bytes = artifact.get("min_bytes")
            if isinstance(min_bytes, int) and fstat_res.st_size < min_bytes:
                return "ARTIFACT_TOO_SMALL"

            if needs_lines or needs_json:
                content_code, read_error_code = _evaluate_content_predicates(
                    fh=fh,
                    fstat_res=fstat_res,
                    artifact=artifact,
                    observed=observed,
                    read_bytes=read_bytes,
                    needs_lines=needs_lines,
                    needs_json=needs_json,
                )
                if not _stat_matches_snapshot(target_stat, os.fstat(fh.fileno())):
                    return "ARTIFACT_UNREADABLE"
                if content_code:
                    return content_code
    except OSError as exc:
        if getattr(exc, "errno", None) == errno.ELOOP:
            return "ARTIFACT_OUTSIDE_ROOT"
        return "ARTIFACT_UNREADABLE"

    max_age = artifact.get("max_age_seconds")
    if isinstance(max_age, int) and verified_age > max_age:
        return "ARTIFACT_STALE"

    if read_error_code:
        return read_error_code
    return ""


def _evaluate_content_predicates(
    *,
    fh: object,
    fstat_res: os.stat_result,
    artifact: dict[str, object],
    observed: dict[str, int | float],
    read_bytes: int,
    needs_lines: bool,
    needs_json: bool,
) -> tuple[str, str]:
    """在已验证同一 FD 上执行文件内容类检查；返回 (立即失败业务码, 延后不可读码)。"""
    if needs_lines:
        min_lines = int(artifact["min_lines"])  # type: ignore[arg-type]
        lines_seen, buffered_chunks = _count_lines_bounded(
            fh,
            min_lines=min_lines,
            read_bytes=read_bytes,
            collect_bytes=needs_json and fstat_res.st_size <= read_bytes,
        )
        observed["lines_seen"] = lines_seen
        if lines_seen < min_lines:
            if fstat_res.st_size > read_bytes:
                return "", "ARTIFACT_UNREADABLE"
            return "ARTIFACT_TOO_FEW_LINES", ""
    else:
        buffered_chunks = None

    if needs_json:
        if fstat_res.st_size > read_bytes:
            return "", "ARTIFACT_UNREADABLE"
        raw_json = _read_remaining_json_bytes(fh, buffered_chunks, read_bytes=read_bytes)
        if raw_json is None:
            return "", "ARTIFACT_UNREADABLE"
        if not _matches_json_rule(raw_json, artifact):
            return "ARTIFACT_JSON_MISMATCH", ""

    return "", ""


def _count_lines_bounded(
    fh: object,
    *,
    min_lines: int,
    read_bytes: int,
    collect_bytes: bool,
) -> tuple[int, bytearray | None]:
    if min_lines <= 0:
        return 0, bytearray() if collect_bytes else None
    remaining = read_bytes
    chunk_size = _LIMITS["line_chunk_bytes"]
    buf = bytearray(min(chunk_size, max(1, remaining)))
    view = memoryview(buf)
    newlines = 0
    total_read = 0
    last_byte = -1
    collected = bytearray() if collect_bytes else None

    while remaining > 0 and newlines < min_lines:
        to_read = min(len(buf), remaining)
        n = fh.readinto(view[:to_read])  # type: ignore[attr-defined]
        if not n:
            break
        if collected is not None:
            collected.extend(view[:n])
        newlines += buf.count(b"\n", 0, n)
        last_byte = buf[n - 1]
        total_read += n
        remaining -= n

    if newlines >= min_lines:
        return newlines, collected
    lines_seen = newlines + (1 if total_read > 0 and last_byte != 10 else 0)
    return lines_seen, collected


def _read_remaining_json_bytes(
    fh: object,
    prefix_bytes: bytearray | None,
    *,
    read_bytes: int,
) -> bytes | None:
    data = prefix_bytes if prefix_bytes is not None else bytearray()
    remaining_plus_one = (read_bytes - len(data)) + 1
    if remaining_plus_one <= 0:
        return None
    tail = fh.read(remaining_plus_one)  # type: ignore[attr-defined]
    if len(data) + len(tail) > read_bytes:
        return None
    if not data:
        return tail
    data.extend(tail)
    return bytes(data)


def _reject_nonfinite_json(token: str) -> object:
    raise ValueError(f"non-finite JSON constant: {token}")


def _matches_json_rule(raw_bytes: bytes, artifact: dict[str, object]) -> bool:
    try:
        text = raw_bytes.decode("utf-8")
        document = json.loads(text, parse_constant=_reject_nonfinite_json)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return False

    pointer = str(artifact["json_pointer"])
    found, target = _resolve_json_pointer(document, pointer)
    if not found:
        return False

    if "equals" in artifact and not _json_scalar_equal(target, artifact["equals"]):
        return False
    if "min_value" in artifact:
        min_value = artifact["min_value"]
        if (
            isinstance(target, bool)
            or not isinstance(target, (int, float))
            or not math.isfinite(target)
            or target < min_value  # type: ignore[operator]
        ):
            return False
    return True


def _resolve_json_pointer(document: object, pointer: str) -> tuple[bool, object]:
    if pointer == "":
        return True, document
    current = document
    for token in pointer[1:].split("/"):
        key = token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if key not in current:
                return False, None
            current = current[key]
        elif isinstance(current, list):
            if _ARRAY_INDEX_RE.fullmatch(key) is None:
                return False, None
            idx = int(key)
            if idx >= len(current):
                return False, None
            current = current[idx]
        else:
            return False, None
    return True, current


def _json_scalar_equal(actual: object, expected: object) -> bool:
    if isinstance(expected, bool) or isinstance(actual, bool):
        return type(actual) is type(expected) and actual == expected
    if expected is None or actual is None:
        return actual is expected
    if isinstance(expected, (int, float)):
        return (
            not isinstance(actual, bool)
            and isinstance(actual, (int, float))
            and math.isfinite(actual)
            and actual == expected
        )
    if isinstance(expected, str):
        return type(actual) is str and actual == expected
    return False


__all__ = [
    "POSTCHECK_CHECK_CODES",
    "POSTCHECK_SCHEMA",
    "evaluate_postcheck",
    "normalize_postcheck",
]
