
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from ..task_progress import invalid_item_statuses, read_task_progress, write_task_progress
from ..tooling.models import BaseTool, ToolExecutionResult
from .orchestration.tool_specs import build_task_progress_spec
from .runner.context import current_subagent_run_id
from .runtime.owner_roots import runtime_owner_root

if TYPE_CHECKING:
    from ..core import SimpleAgent


class TaskProgressTool(BaseTool):
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_task_progress_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        action = _normalized_action(params.get("action"))
        run_id = _target_run_id(self.agent, params, allow_explicit=action == "read")
        if not run_id:
            run_id = "main"
        root = runtime_owner_root(self.agent)
        if action == "update":
            if status_error := _invalid_status_result(params):
                return status_error
            payload = write_task_progress(root, run_id, params)
            payload = _with_write_feedback(payload)
            payload = _with_evidence_source_feedback(self.agent, payload)
        else:
            payload = read_task_progress(root, run_id)
        return ToolExecutionResult("task_progress", True, json.dumps(payload, ensure_ascii=False, indent=2))


def _invalid_status_result(params: dict[str, object]) -> ToolExecutionResult | None:
    invalid = invalid_item_statuses(params)
    if not invalid:
        return None
    payload = {
        "ok": False,
        "error": "task_progress items[].status must be one of pending/in_progress/done/skipped/blocked.",
        "invalid_statuses": invalid[:12],
        "allowed_statuses": ["pending", "in_progress", "done", "skipped", "blocked"],
        "how_to_fix": "Move labels such as completed/read/ok into notes or summary, and use status=done when the item is complete.",
    }
    return ToolExecutionResult(
        "task_progress",
        False,
        json.dumps(payload, ensure_ascii=False, indent=2),
        error_code="TOOL_INVALID_ARGUMENTS",
    )


def _normalized_action(value: object) -> str:
    action = str(value or "read").strip().lower()
    if action in {
        "update",
        "create",
        "init",
        "initialize",
        "start",
        "begin",
        "set",
        "save",
        "record",
        "write",
    }:
        return "update"
    return "read"


def _target_run_id(agent: object, params: dict[str, object], *, allow_explicit: bool) -> str:
    explicit = str(params.get("run_id") or "").strip()
    if explicit and allow_explicit:
        return explicit
    scoped = _scope_run_id(params.get("__run_scope"))
    if scoped:
        return scoped
    return str(
        current_subagent_run_id(agent)
        or getattr(agent, "_main_agent_run_id", "")
        or getattr(agent, "_current_request_id", "")
        or "main"
    ).strip()


def _scope_run_id(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    return str(value.get("run_id") or value.get("task_id") or value.get("request_id") or "").strip()


def _with_write_feedback(payload: dict[str, object]) -> dict[str, object]:
    hints = payload.get("quality_hints")
    if not isinstance(hints, dict) or not hints.get("messages"):
        return payload
    missing = _missing_evidence_ids(hints)
    feedback = {
        "severity": "soft",
        "blocking": False,
        "message": str(hints.get("soft_prompt") or "软提醒：有些进度项缺少证据，建议补上文件、来源或产物引用。"),
        "missing_evidence_item_ids": missing,
        "next_suggestions": list(hints.get("next_suggestions") or []),
    }
    return {**payload, "soft_feedback": feedback}


def _with_evidence_source_feedback(agent: object, payload: dict[str, object]) -> dict[str, object]:
    archive_calls = _current_archive_tool_calls(agent)
    if not archive_calls:
        return payload
    refs = _progress_evidence_refs(payload)
    local_refs = [ref for ref in refs if _looks_like_local_ref(ref)]
    if not local_refs:
        return payload
    successful, failed = _tool_path_index(archive_calls)
    failed_refs = [ref for ref in local_refs if _matches_any_ref(agent, ref, failed)]
    unseen_refs = [
        ref
        for ref in local_refs
        if ref not in failed_refs and not _matches_any_ref(agent, ref, successful)
    ]
    if not failed_refs and not unseen_refs:
        return payload
    feedback = dict(payload.get("soft_feedback") if isinstance(payload.get("soft_feedback"), dict) else {})
    feedback.setdefault("severity", "soft")
    feedback.setdefault("blocking", False)
    message = str(feedback.get("message") or "").strip()
    source_message = _evidence_source_message(failed_refs, unseen_refs)
    feedback["message"] = f"{message} {source_message}".strip() if message else source_message
    suggestions = list(feedback.get("next_suggestions") if isinstance(feedback.get("next_suggestions"), list) else [])
    suggestions.append("把失败或未确认的证据路径重新用 read_file/list_files 确认后，再把它写成结论。")
    feedback["next_suggestions"] = _dedupe_texts(suggestions)
    feedback["evidence_source_warnings"] = {
        "failed_refs": failed_refs[:12],
        "unseen_refs": unseen_refs[:12],
        "failed_count": len(failed_refs),
        "unseen_count": len(unseen_refs),
    }
    hints = dict(payload.get("quality_hints") if isinstance(payload.get("quality_hints"), dict) else {})
    messages = list(hints.get("messages") if isinstance(hints.get("messages"), list) else [])
    messages.append(source_message)
    hints["messages"] = _dedupe_texts(messages)
    hints["severity"] = "soft"
    hints["evidence_source_warning_count"] = len(failed_refs) + len(unseen_refs)
    if failed_refs:
        hints["evidence_failed_refs"] = failed_refs[:12]
    if unseen_refs:
        hints["evidence_unseen_refs"] = unseen_refs[:12]
    return {**payload, "quality_hints": hints, "soft_feedback": feedback}


def _current_archive_tool_calls(agent: object) -> list[dict[str, object]]:
    params = getattr(agent, "_current_tool_loop_params", None)
    records = getattr(params, "archive_tool_calls", None)
    if not isinstance(records, list):
        return []
    return [dict(item) for item in records if isinstance(item, dict)]


def _progress_evidence_refs(payload: dict[str, object]) -> list[str]:
    refs: list[str] = []
    for item in _dict_items(payload.get("items")):
        refs.extend(_string_items(item.get("evidence")))
    coverage = payload.get("coverage")
    targets = coverage.get("targets") if isinstance(coverage, dict) else []
    for target in _dict_items(targets):
        refs.extend(_string_items(target.get("evidence")))
    return _dedupe_texts(refs)


def _tool_path_index(archive_calls: list[dict[str, object]]) -> tuple[list[str], list[str]]:
    successful: list[str] = []
    failed: list[str] = []
    for record in archive_calls:
        paths = _record_path_refs(record)
        if not paths:
            continue
        if bool(record.get("ok")):
            successful.extend(paths)
        elif _path_failure(record):
            failed.extend(paths)
    return _dedupe_texts(successful), _dedupe_texts(failed)


def _record_path_refs(record: dict[str, object]) -> list[str]:
    refs: list[str] = []
    for source in (
        record,
        _mapping(record.get("parameters")),
        _mapping(record.get("tool_result_envelope")),
        _mapping(_mapping(record.get("tool_result_envelope")).get("output")),
    ):
        refs.extend(_path_values(source))
    tool_refs = record.get("tool_result_refs")
    if isinstance(tool_refs, list):
        for item in tool_refs:
            refs.extend(_path_values(_mapping(item)))
    return _dedupe_texts(refs)


def _path_values(source: dict[str, object]) -> list[str]:
    refs: list[str] = []
    for key in ("path", "root", "artifact_ref", "source_ref", "target_path", "output_path", "uri", "url"):
        refs.extend(_string_items(source.get(key), allow_scalar=True))
    return refs


def _path_failure(record: dict[str, object]) -> bool:
    code = str(record.get("error_code") or "").strip()
    if code in {"PATH_NOT_FOUND", "PERMISSION_DENIED", "INVALID_PATH", "TOOL_INPUT_INVALID"}:
        return True
    envelope = record.get("tool_result_envelope")
    if isinstance(envelope, dict):
        return str(envelope.get("error_code") or "").strip() in {
            "PATH_NOT_FOUND",
            "PERMISSION_DENIED",
            "INVALID_PATH",
            "TOOL_INPUT_INVALID",
        }
    return False


def _matches_any_ref(agent: object, ref: str, candidates: list[str]) -> bool:
    return any(_same_ref(agent, ref, candidate) for candidate in candidates)


def _same_ref(agent: object, left: str, right: str) -> bool:
    left = _clean_ref(left)
    right = _clean_ref(right)
    if not left or not right:
        return False
    if left == right:
        return True
    if _has_scheme(left) or _has_scheme(right):
        return False
    left_path = _resolved_local_ref(agent, left)
    right_path = _resolved_local_ref(agent, right)
    if left_path == right_path:
        return True
    left_posix = left_path.as_posix()
    right_posix = right_path.as_posix()
    return left_posix.endswith(f"/{right}") or right_posix.endswith(f"/{left}")


def _resolved_local_ref(agent: object, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(getattr(agent, "root", ".")).expanduser() / path
    return path.resolve(strict=False)


def _clean_ref(value: str) -> str:
    text = str(value or "").strip()
    if not text or _has_scheme(text):
        return text
    if "#" in text:
        text = text.split("#", 1)[0].strip()
    return text


def _looks_like_local_ref(value: str) -> bool:
    text = _clean_ref(value)
    if not text or _has_scheme(text):
        return False
    return "/" in text or "\\" in text or "." in Path(text).name


def _has_scheme(value: str) -> bool:
    return "://" in value


def _evidence_source_message(failed_refs: list[str], unseen_refs: list[str]) -> str:
    parts: list[str] = []
    if failed_refs:
        parts.append(f"这些证据路径本轮读失败：{', '.join(failed_refs[:6])}")
    if unseen_refs:
        parts.append(f"这些证据路径本轮没有成功工具记录：{', '.join(unseen_refs[:6])}")
    return "软提醒：" + "；".join(parts) + "。"


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _dict_items(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _string_items(value: object, *, allow_scalar: bool = False) -> list[str]:
    if isinstance(value, list | tuple | set):
        return [str(item).strip() for item in value if str(item or "").strip()]
    if allow_scalar:
        text = str(value or "").strip()
        return [text] if text else []
    return []


def _dedupe_texts(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _missing_evidence_ids(hints: dict[str, object]) -> list[str]:
    values: list[str] = []
    for key in ("done_without_evidence_ids", "result_without_evidence_ids", "coverage_done_without_evidence_ids"):
        raw = hints.get(key)
        if isinstance(raw, list):
            values.extend(str(item) for item in raw if str(item or "").strip())
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


__all__ = ["TaskProgressTool"]
