
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ..task_progress import (
    invalid_coverage_statuses,
    invalid_item_statuses,
    read_task_progress,
    write_task_progress,
)
from ..tooling.models import BaseTool, ToolExecutionResult
from .orchestration.dispatch_progress_seed import (
    reconcile_completed_child_covers,
    reconcile_completed_child_items,
)
from .orchestration.tool_specs import build_task_progress_spec
from .runner.context import current_subagent_run_id
from .runtime.owner_roots import runtime_owner_root
from .runtime.task_identity import progress_ledger_id

if TYPE_CHECKING:
    from ..core import SimpleAgent


class TaskProgressTool(BaseTool):
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_task_progress_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        action = _normalized_action(params.get("action"))
        if action_error := _invalid_action_result(action):
            return action_error
        if field_error := _invalid_action_fields_result(action, params):
            return field_error
        if action == "select":
            return _select_conversation_task(self.agent, params)
        if action == "start":
            return _start_conversation_task(self.agent, params)
        run_id = _target_run_id(self.agent, params, allow_explicit=action == "read")
        if not run_id:
            run_id = "main"
        root = runtime_owner_root(self.agent)
        if action == "update":
            if workspace_error := _workspace_decision_required(self.agent):
                return workspace_error
            if status_error := _invalid_status_result(params):
                return status_error
            from ..conversation.task_promotion import promote_current_conversation_task

            promote_current_conversation_task(self.agent)
            payload = write_task_progress(root, run_id, params)
            _sync_current_task_compact(self.agent)
            payload = _with_write_feedback(payload)
            payload = _with_evidence_source_feedback(self.agent, payload)
        else:
            _reconcile_completed_child_covers_before_read(self.agent, root, run_id)
            reconcile_completed_child_items(self.agent, root, run_id)
            _sync_current_task_compact(self.agent)
            payload = read_task_progress(root, run_id)
        return ToolExecutionResult("task_progress", True, json.dumps(payload, ensure_ascii=False, indent=2))


def _reconcile_completed_child_covers_before_read(
    agent: object,
    root: Path,
    run_id: str,
) -> None:
    if current_subagent_run_id(agent):
        return
    params = getattr(agent, "_current_run_params", None)
    if params is None or progress_ledger_id(agent, params) != run_id:
        return
    reconcile_completed_child_covers(agent, root, run_id)


def _sync_current_task_compact(agent: object) -> None:
    """Refresh the existing task compact package after progress state changes."""
    params = getattr(agent, "_current_run_params", None)
    attrs = getattr(params, "task_attributes", None)
    workspace = attrs.get("run_workspace") if isinstance(attrs, dict) else None
    value = workspace.get("task_root") if isinstance(workspace, dict) else None
    task_root = str(value or getattr(agent, "_current_run_task_workspace", "") or "").strip()
    if not task_root:
        return
    try:
        from ..user_space.task_compact_rollup import sync_task_compact_rollup

        sync_task_compact_rollup(task_root)
    except Exception:  # noqa: BLE001 - compact projection must not break progress writes
        logging.getLogger(__name__).warning("task compact progress sync failed", exc_info=True)


def _invalid_status_result(params: dict[str, object]) -> ToolExecutionResult | None:
    invalid = invalid_item_statuses(params)
    invalid_coverage = invalid_coverage_statuses(params)
    if not invalid and not invalid_coverage:
        return None
    payload = {
        "ok": False,
        "error": "task_progress status fields must be one of pending/in_progress/done/skipped/blocked.",
        "invalid_statuses": invalid[:12],
        "invalid_coverage_statuses": invalid_coverage[:12],
        "allowed_statuses": ["pending", "in_progress", "done", "skipped", "blocked"],
        "how_to_fix": "Move labels such as completed/read/ok into notes or summary, and use status=done when the item/check is complete.",
    }
    return ToolExecutionResult(
        "task_progress",
        False,
        json.dumps(payload, ensure_ascii=False, indent=2),
        error_code="TOOL_INVALID_ARGUMENTS",
    )


def _normalized_action(value: object) -> str:
    action = str(value or "read").strip()
    return action if action in {"read", "update"} else action or "read"


def _invalid_action_result(action: str) -> ToolExecutionResult | None:
    if action in {"read", "update", "select", "start"}:
        return None
    payload = {
        "ok": False,
        "error": "task_progress action must be exactly read, update, select, or start.",
        "invalid_action": action,
        "allowed_actions": ["read", "update", "select", "start"],
    }
    return ToolExecutionResult(
        "task_progress",
        False,
        json.dumps(payload, ensure_ascii=False, indent=2),
        error_code="TOOL_INVALID_ARGUMENTS",
    )


# LLM: 每个 action 只接受自己的参数；尤其 start 不能夹带 update 字段后静默丢弃并误建工作区。
# 函数用途: 在改变会话任务绑定前拒绝动作不相关字段，让模型基于明确错误重新选择 select/start。
def _invalid_action_fields_result(
    action: str,
    params: dict[str, object],
) -> ToolExecutionResult | None:
    action_fields = {
        "read": frozenset({"action", "run_id"}),
        "update": frozenset({"action", "summary", "next_action", "items", "coverage"}),
        "select": frozenset({"action", "task_id"}),
        "start": frozenset({"action", "new_task"}),
    }
    protocol_fields = frozenset(
        {
            "__run_scope",
            "__tool_call_id",
            "artifact_refs",
            "call_id",
            "idempotency_key",
            "kind",
            "metadata",
            "operation_id",
            "schema_version",
            "tool",
        }
    )
    invalid = sorted(set(params) - action_fields[action] - protocol_fields)
    if not invalid:
        return None
    payload = {
        "ok": False,
        "error": "task_progress fields must match the selected action.",
        "action": action,
        "invalid_fields": invalid,
        "allowed_fields": sorted(action_fields[action] - {"action"}),
        "how_to_fix": (
            "Use action=select with an exact candidate task_id when continuing existing work. "
            "Use action=start only to bind a genuinely new task, then send progress fields in a separate "
            "action=update call."
        ),
    }
    return ToolExecutionResult(
        "task_progress",
        False,
        json.dumps(payload, ensure_ascii=False, indent=2),
        error_code="TOOL_INVALID_ARGUMENTS",
    )


def _workspace_decision_required(agent: object) -> ToolExecutionResult | None:
    from ..conversation.task_promotion import conversation_workspace_decision

    payload = conversation_workspace_decision(agent)
    if payload is None:
        return None
    return ToolExecutionResult(
        "task_progress",
        False,
        json.dumps(payload, ensure_ascii=False),
        error_code="CONVERSATION_WORKSPACE_DECISION_REQUIRED",
    )


def _start_conversation_task(
    agent: object,
    params: dict[str, object],
) -> ToolExecutionResult:
    from ..conversation.task_promotion import (
        conversation_workspace_decision,
        promote_current_conversation_task,
    )

    decision = conversation_workspace_decision(agent)
    if decision is not None:
        load_errors = decision.get("load_errors")
        if load_errors or params.get("new_task") is not True:
            payload = {
                **decision,
                "new_task_confirmation_required": bool(decision.get("candidates")),
            }
            return ToolExecutionResult(
                "task_progress",
                False,
                json.dumps(payload, ensure_ascii=False),
                error_code="CONVERSATION_WORKSPACE_DECISION_REQUIRED",
            )

    link = promote_current_conversation_task(agent)
    if link is None:
        return ToolExecutionResult(
            "task_progress",
            False,
            json.dumps({"ok": False, "error": "current conversation task could not be started"}),
            error_code="CONVERSATION_TASK_START_FAILED",
        )
    return ToolExecutionResult(
        "task_progress",
        True,
        json.dumps(
            {"ok": True, "started": True, "run_id": link.task_id},
            ensure_ascii=False,
        ),
    )


def _select_conversation_task(
    agent: object,
    params: dict[str, object],
) -> ToolExecutionResult:
    from ..conversation.task_promotion import (
        conversation_task_selection_blocker,
        select_current_conversation_task,
    )

    task_id = str(params.get("task_id") or "").strip()
    blocker = conversation_task_selection_blocker(agent, task_id)
    if blocker is not None:
        return _conversation_task_blocked_result(task_id, blocker)
    link = select_current_conversation_task(agent, task_id)
    if link is None:
        return ToolExecutionResult(
            "task_progress",
            False,
            json.dumps(
                {
                    "ok": False,
                    "error": "task_id is not an active, interrupted, or recent completed task candidate in the current conversation.",
                    "task_id": task_id,
                },
                ensure_ascii=False,
            ),
            error_code="CONVERSATION_TASK_NOT_FOUND",
        )
    return ToolExecutionResult(
        "task_progress",
        True,
        json.dumps(
            {
                "ok": True,
                "selected": True,
                "task_id": link.task_id,
                "goal": link.goal,
                "task_path": link.task_path,
                **_selected_task_execution_state(agent, link),
            },
            ensure_ascii=False,
        ),
    )


def _selected_task_execution_state(agent: object, link: object) -> dict[str, object]:
    """Expose exact resume facts so the model can author a truthful acknowledgement."""
    current = getattr(agent, "_current_run_params", None)
    attrs = getattr(current, "task_attributes", None)
    attrs = attrs if isinstance(attrs, dict) else {}
    store = getattr(agent, "conversation_store", None)
    goal_payload: dict[str, object] = {}
    if store is not None:
        try:
            goal = store.load_goal(str(getattr(link, "thread_id", "") or ""))
        except Exception:
            goal = None
        if goal is not None and str(getattr(goal, "task_id", "") or "") == str(
            getattr(link, "task_id", "") or ""
        ):
            goal_payload = {
                "goal_id": str(getattr(goal, "goal_id", "") or ""),
                "task_id": str(getattr(goal, "task_id", "") or ""),
                "status": str(getattr(goal, "status", "") or ""),
                "continuation_pending": attrs.get("thread_goal_activation_pending") is True,
            }
    return {
        "task_status": str(getattr(link, "status", "") or ""),
        "workspace_reused": bool(str(getattr(link, "task_path", "") or "").strip()),
        "goal_state": goal_payload,
    }


def _conversation_task_blocked_result(
    task_id: str,
    blocker: dict[str, object],
) -> ToolExecutionResult:
    state_available = blocker.get("state_available") is True
    message = (
        "The selected task is already executing in the background; "
        "this chat turn cannot become a second executor."
        if state_available
        else "The selected task execution state could not be read safely."
    )
    return ToolExecutionResult(
        "task_progress",
        False,
        json.dumps(
            {
                "ok": False,
                "error": message,
                "task_id": task_id,
                "how_to_fix": (
                    "Answer the current user message as ordinary chat. Use /btw to steer the running task, "
                    "or /stop before changing its execution path."
                ),
            },
            ensure_ascii=False,
        ),
        error_code=(
            "CONVERSATION_TASK_ALREADY_RUNNING"
            if state_available
            else "CONVERSATION_TASK_STATE_UNAVAILABLE"
        ),
    )


def _target_run_id(agent: object, params: dict[str, object], *, allow_explicit: bool) -> str:
    """账本键解析（与派工 seed 和任务工作区使用同一份任务身份）。
    唯一特殊分支=【后台唤醒轮】(_current_run_params.source=="background_main_agent"):
    其 run_id 是新的(bg-main-*),task_id 仍是主任务——账本按【任务】延续,否则派工 seed
    立的账在唤醒轮里读写不到、模型只能另立新账。其余场景原链不动（子代理按自己 run 隔离）。"""
    explicit = str(params.get("run_id") or "").strip()
    if explicit and allow_explicit:
        return explicit
    scoped = _scope_run_id(params.get("__run_scope"))
    current = getattr(agent, "_current_run_params", None)
    if current is None:
        return str(
            scoped
            or current_subagent_run_id(agent)
            or getattr(agent, "_main_agent_run_id", "")
            or getattr(agent, "_current_request_id", "")
            or "main"
        ).strip()
    resolved = progress_ledger_id(agent, current, scoped_id=scoped)
    return resolved or str(current_subagent_run_id(agent) or "main").strip()


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
        record_successful, record_failed = _classified_record_paths(record)
        successful.extend(record_successful)
        failed.extend(record_failed)
    return _dedupe_texts(successful), _dedupe_texts(failed)


def _classified_record_paths(record: dict[str, object]) -> tuple[list[str], list[str]]:
    paths = _record_path_refs(record)
    if not paths:
        return [], []
    if bool(record.get("ok")):
        return paths, []
    return ([], paths) if _path_failure(record) else ([], [])


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
