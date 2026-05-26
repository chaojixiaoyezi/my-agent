# LLM: Tool-failure recovery turns failed tool records into reusable delivery repair actions.
# 模块用途: 将 archive_tool_calls 里的结构化 error_code 转成临时返工动作，并在对应目标修复后自动失效。

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..contracts.error_taxonomy import error_contract
from ..contracts.staged_checkpoint_acceptance import json_checkpoint_status

_PATH_KEYS = (
    "path",
    "file_path",
    "target_path",
    "output_path",
    "artifact_ref",
    "source_ref",
)

_JSON_WRITER_TOOLS = {"write_file"}


# LLM: tool_failure_recovery_actions reads tool archive machine fields only.
# 函数用途: 从失败工具记录恢复 code/action/path，给下一轮返工提示和 repair guard 使用；不解析工具输出文案。
def tool_failure_recovery_actions(
    records: list[Any],
    *,
    workspace_root: Path,
) -> list[dict[str, object]]:
    actions: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()
    usable_records = [record for record in records if isinstance(record, dict)]
    for index, record in enumerate(usable_records):
        if record.get("ok") is not False:
            continue
        error_code = str(record.get("error_code") or "").strip()
        if not error_code or _failure_resolved(record, usable_records[index + 1 :], workspace_root):
            continue
        action = _action_from_failed_record(record, error_code, workspace_root)
        identity = (
            str(action.get("code") or ""),
            str(action.get("recommended_action") or ""),
            str(action.get("checkpoint_ref") or action.get("artifact_ref") or ""),
        )
        if identity in seen:
            continue
        seen.add(identity)
        actions.append(action)
    return actions


# LLM: attach_tool_failure_recovery_actions adds active failed-tool repairs after normal closeout enrichment.
# 函数用途: 把工具层结构化错误补进 recovery_actions，供下一轮返工循环继续使用。
def attach_tool_failure_recovery_actions(
    report: dict[str, Any],
    archive_tool_calls: list[Any],
    workspace_root: Path,
) -> dict[str, Any]:
    if report.get("ok") is True:
        return report
    progress = report.get("delivery_progress")
    if not isinstance(progress, dict):
        return report
    progress["recovery_actions"] = _merge_recovery_actions(
        _action_list(progress.get("recovery_actions")),
        tool_failure_recovery_actions(archive_tool_calls, workspace_root=workspace_root),
    )
    return report


# LLM: _action_from_failed_record copies stable error taxonomy and target refs.
# 函数用途: 将工具错误码映射到统一恢复动作，额外携带 checkpoint/tool/call_id 等机器字段。
def _action_from_failed_record(
    record: dict[str, Any],
    error_code: str,
    workspace_root: Path,
) -> dict[str, object]:
    contract = error_contract(error_code)
    action: dict[str, object] = {
        "code": contract.code,
        "category": contract.category,
        "retryable": contract.retryable,
        "recommended_action": contract.recommended_action,
        "recovery_hint": contract.recovery_hint,
        "failed_tool": str(record.get("tool") or ""),
        "source_tool_call_id": str(record.get("scoped_call_id") or record.get("call_id") or record.get("id") or ""),
    }
    target_ref = _target_ref(record, workspace_root)
    if target_ref:
        action["artifact_ref"] = target_ref
    if _is_json_checkpoint_action(record, target_ref):
        action["checkpoint_ref"] = target_ref
        action["writer_tool"] = "write_file"
        action["write_tools"] = ["write_file"]
    return action


# LLM: _failure_resolved drops stale failed-tool repairs after later success or valid checkpoint state.
# 函数用途: 如果同一路径后续已经成功写入，或 JSON checkpoint 已经通过结构校验，就不再保留旧失败动作。
def _failure_resolved(
    record: dict[str, Any],
    later_records: list[dict[str, Any]],
    workspace_root: Path,
) -> bool:
    target_ref = _target_ref(record, workspace_root)
    if target_ref and _later_success_for_target(record, later_records, target_ref, workspace_root):
        return True
    target_path = _target_path(record, workspace_root)
    if target_path is None or not target_path.exists() or target_path.suffix.lower() != ".json":
        return False
    return json_checkpoint_status(target_path)["code"] == "OK"


# LLM: _later_success_for_target checks structured archive refs rather than prompt text.
# 函数用途: 只要后续同目标成功，旧失败就不再作为当前返工动作。
def _later_success_for_target(
    failed_record: dict[str, Any],
    later_records: list[dict[str, Any]],
    target_ref: str,
    workspace_root: Path,
) -> bool:
    failed_tool = str(failed_record.get("tool") or "").strip()
    for record in later_records:
        if record.get("ok") is not True:
            continue
        if failed_tool and str(record.get("tool") or "").strip() != failed_tool:
            continue
        if _same_ref(_target_ref(record, workspace_root), target_ref):
            return True
    return False


# LLM: _is_json_checkpoint_action recognizes staged JSON repairs from tool and target shape.
# 函数用途: 为 JSON checkpoint 失败补上 writer_tool/write_tools 字段，让 repair guard 能执行。
def _is_json_checkpoint_action(record: dict[str, Any], target_ref: str) -> bool:
    tool_name = str(record.get("tool") or "").strip()
    return tool_name in _JSON_WRITER_TOOLS or target_ref.lower().endswith(".json")


# LLM: _target_ref extracts the model-supplied target path as a stable workspace ref.
# 函数用途: 优先保留相对路径；绝对路径在工作区内时转成相对 ref，避免提示里泄漏临时目录。
def _target_ref(record: dict[str, Any], workspace_root: Path) -> str:
    for source in (record, _mapping(record.get("parameters")), _mapping(record.get("tool_result_envelope"))):
        if ref := _source_target_ref(source, workspace_root):
            return ref
    return ""


def _source_target_ref(source: dict[str, Any], workspace_root: Path) -> str:
    for key in _PATH_KEYS:
        if ref := _workspace_ref(source.get(key), workspace_root):
            return ref
    return ""


# LLM: _target_path resolves one target ref without accepting arbitrary prose.
# 函数用途: 把归档记录中的路径字段解析回文件系统路径，用于判断旧失败是否已经修复。
def _target_path(record: dict[str, Any], workspace_root: Path) -> Path | None:
    ref = _target_ref(record, workspace_root)
    if not ref:
        return None
    path = Path(ref).expanduser()
    candidate = path.resolve(strict=False) if path.is_absolute() else (workspace_root / path).resolve(strict=False)
    try:
        candidate.relative_to(workspace_root)
    except ValueError:
        return None
    return candidate


def _workspace_ref(value: object, workspace_root: Path) -> str:
    if isinstance(value, dict):
        return _workspace_ref_from_mapping(value, workspace_root)
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return ""
    path = Path(text).expanduser()
    if not path.is_absolute():
        return text.strip("/")
    resolved = path.resolve(strict=False)
    try:
        return str(resolved.relative_to(workspace_root)).replace("\\", "/")
    except ValueError:
        return ""


def _workspace_ref_from_mapping(value: dict[str, object], workspace_root: Path) -> str:
    for key in ("raw", "path", "artifact_ref", "resolved"):
        if ref := _workspace_ref(value.get(key), workspace_root):
            return ref
    return ""


def _same_ref(left: str, right: str) -> bool:
    left_text = str(left or "").strip().replace("\\", "/").strip("/")
    right_text = str(right or "").strip().replace("\\", "/").strip("/")
    return bool(left_text and right_text) and left_text == right_text


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _merge_recovery_actions(
    current: list[dict[str, object]],
    added: list[dict[str, object]],
) -> list[dict[str, object]]:
    merged: list[dict[str, object]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for action in [*current, *added]:
        identity = (
            str(action.get("code") or ""),
            str(action.get("recommended_action") or ""),
            str(action.get("checkpoint_ref") or ""),
            str(action.get("artifact_ref") or ""),
        )
        if identity in seen:
            continue
        seen.add(identity)
        merged.append(dict(action))
    return merged


def _action_list(value: object) -> list[dict[str, object]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


__all__ = ["attach_tool_failure_recovery_actions", "tool_failure_recovery_actions"]
