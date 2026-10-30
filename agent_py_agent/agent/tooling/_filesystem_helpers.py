# LLM: 文件参数与内部引用共用原owner/path边界；发现先剪枝，glob按路径段解释，指纹不能制造强快照。
# 模块用途: 校验参数、定位可读产物及有界枚举安全文件，不从文档、缓存或忽略参数推导权限。
from __future__ import annotations

import fnmatch
import heapq
import json
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..common.cancellation import raise_if_cancelled
from ..contracts.error_taxonomy import ERROR_CONTRACTS
from .models import ToolFailureStage, ToolHandlerOutcome

# 路径最多 4096 字符：超长拒绝，防异常输入。
_MAX_PATH_CHARS = 4096
# 搜索查询最多 4000 字符：超长拒绝，防异常输入。
_MAX_SEARCH_QUERY_CHARS = 4000
# 搜索命中行最多 500 字符：超长行截断，防撑爆结果。
_MAX_SEARCH_LINE_CHARS = 500
# 写文件正文最多 100 万字符：超长拒绝，防异常输入。
_MAX_WRITE_TEXT_CHARS = 1_000_000
# 没有 ripgrep 等外部工具时，Python 后备遍历的存活上限（文件查找与内容搜索共用一份）：超过这么多候选文件或这么多秒就停，
# 结果标为不完整，不能据此判定“没有匹配”；只保护单个 Gateway 不被大目录长期占住。
_FALLBACK_SCAN_MAX_FILES = 20_000
# 无 ripgrep 时后备遍历最多 10 秒：超时标不完整，保护 Gateway 不被大目录占住。
_FALLBACK_SCAN_MAX_SECONDS = 10.0
# 文件发现命令参数允许累计的最大字节数，避免超过系统参数上限。
_DISCOVERY_ARG_MAX_BYTES = 192 * 1024
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


# LLM: 只读异常上的结构化 access_code，绝不解析消息文字；码必须已在错误合同登记且属于 permission 才按原码上报。
# 函数用途: 把路径解析失败的异常转成工具结果，权限类拒绝报原权限码并标 authorization，其余仍报参数错误。
def path_resolution_error_outcome(tool_name: str, exc: BaseException) -> ToolHandlerOutcome:
    code = str(getattr(exc, "access_code", "") or "").strip()
    contract = ERROR_CONTRACTS.get(code) if code else None
    if contract is not None and contract.category == "permission":
        return ToolHandlerOutcome(
            tool_name,
            False,
            str(exc),
            error_code=code,
            failure_stage=ToolFailureStage.AUTHORIZATION.value,
        )
    return ToolHandlerOutcome(
        tool_name,
        False,
        str(exc),
        error_code="TOOL_INVALID_ARGUMENTS",
    )


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


# LLM: 只有实际观察到链接才允许跳过，任意OSError消息不能代替结构化类型。
# 类用途: 区分发现入口的链接排除与需要披露的I/O失败。
class _DiscoverySymlink(OSError):
    pass


# LLM: 发现入口不继承直接读取链接的语义；逐级lstat只是变化检测，不声称原子文件系统快照。
# 函数用途: 拒绝发现路径中的链接分量，保留普通读取工具原有语义。
def _discovery_nofollow_stat(path: Path):
    info = None
    for component in reversed((path, *path.parents)):
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise _DiscoverySymlink()
    return info


# LLM: 只记录已观察身份和代次，不以相同mtime证明强快照。
# 函数用途: 统一目录、文件与已打开FD的变化检测字段。
def _discovery_identity(info) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


# LLM: include_ignored只改变噪声过滤，路径权限与宿主托管存储仍由原策略裁决；链接仅跳过而非终止整轮。
# 函数用途: 在目录下降和文件交出前统一执行忽略、nofollow及权限检查。
def _safe_discovery_path(tool, item: Path, *, root: Path, include_ignored: bool, ignored_names) -> Path | None:
    try:
        relative = item.relative_to(root)
    except ValueError:
        return None
    if not include_ignored and any(part in ignored_names for part in relative.parts[:-1]):
        return None
    try:
        info = _discovery_nofollow_stat(item)
    except _DiscoverySymlink:
        return None
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        return None
    if not include_ignored and stat.S_ISDIR(info.st_mode) and relative.parts and item.name in ignored_names:
        return None
    if tool.path_access_policy.host_managed_store(item) is not None:
        return None
    try:
        safe = tool.resolve_path(item)
    except ValueError:
        return None
    return safe if safe == item else None


# LLM: 目录排序键加尾斜线，使文件与子目录按同一Unicode路径序产出；候选入堆前即做权限剪枝。
# 函数用途: 供find/search共用有界、可取消的安全候选流。
def _iter_discovery_files(tool, target: Path, *, include_ignored: bool, ignored_names,
                          scan_state, admit, observe=None):
    pending = [('', target)]
    examined = 0
    try:
        while pending:
            raise_if_cancelled()
            if time.monotonic() - scan_state.started_at >= _FALLBACK_SCAN_MAX_SECONDS:
                scan_state.limited = True
                scan_state.limit_reason = scan_state.limit_reason or 'time_limit'
                return
            _, item = heapq.heappop(pending)
            safe = _safe_discovery_path(tool, item, root=target, include_ignored=include_ignored,
                                        ignored_names=ignored_names)
            if safe is None:
                continue
            info = _discovery_nofollow_stat(safe)
            if observe is not None:
                observe(safe)
            if stat.S_ISREG(info.st_mode):
                if not admit(scan_state):
                    return
                yield safe
                continue
            with os.scandir(safe) as entries:
                for entry in entries:
                    raise_if_cancelled()
                    examined += 1
                    if examined > _FALLBACK_SCAN_MAX_FILES:
                        scan_state.limited = True
                        scan_state.limit_reason = scan_state.limit_reason or 'entry_limit'
                        return
                    if time.monotonic() - scan_state.started_at >= _FALLBACK_SCAN_MAX_SECONDS:
                        scan_state.limited = True
                        scan_state.limit_reason = scan_state.limit_reason or 'time_limit'
                        return
                    child = safe / entry.name
                    accepted = _safe_discovery_path(tool, child, root=target,
                        include_ignored=include_ignored, ignored_names=ignored_names)
                    if accepted is None:
                        continue
                    child_info = _discovery_nofollow_stat(accepted)
                    if observe is not None:
                        observe(accepted)
                    key = accepted.relative_to(target).as_posix()
                    if stat.S_ISDIR(child_info.st_mode):
                        key += '/'
                    heapq.heappush(pending, (key, accepted))
            if observe is not None:
                observe(safe)
    except OSError:
        scan_state.limited = True
        scan_state.limit_reason = scan_state.limit_reason or 'filesystem_error'


# LLM: 两后端共用按段glob；整段双星可在任意位置消费零或多段，普通星号不跨目录，始终区分大小写。
# 函数用途: 用有界位置集合匹配路径，避免多处双星的指数变体展开或递归回溯。
def _discovery_matches_glob(display_path: str, basename: str, pattern: str) -> bool:
    if '/' not in pattern:
        return fnmatch.fnmatchcase(basename, pattern)
    parts = display_path.split('/')
    positions = {0}
    for segment in pattern.split('/'):
        if not positions:
            return False
        if segment == '**':
            positions = set(range(min(positions), len(parts) + 1))
        else:
            positions = {index + 1 for index in positions if index < len(parts)
                         and fnmatch.fnmatchcase(parts[index], segment)}
    return len(parts) in positions


# LLM: 原生程序不得自行遍历未经策略准入的目录；argv保留有帽，触顶是partial而非完整无匹配。
# 函数用途: 生成可交给rg的有限文件批次，目录下降与文件筛选只执行共同发现政策。
def _discovery_file_batch(tool, target: Path, *, include_ignored: bool, ignored_names,
                          scan_state, admit, file_glob: str = '') -> list[Path]:
    result: list[Path] = []
    retained = 0
    for item in _iter_discovery_files(tool, target, include_ignored=include_ignored,
            ignored_names=ignored_names, scan_state=scan_state, admit=admit):
        if file_glob and not _discovery_matches_glob(tool.display_path(item), item.name, file_glob):
            continue
        cost = len(os.fsencode(item)) + 64
        if retained + cost > _DISCOVERY_ARG_MAX_BYTES:
            scan_state.limited = True
            scan_state.limit_reason = scan_state.limit_reason or 'candidate_byte_limit'
            break
        retained += cost
        result.append(item)
    return result


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
