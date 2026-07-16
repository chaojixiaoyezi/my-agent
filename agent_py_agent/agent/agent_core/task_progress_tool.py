
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

from ..task_progress import (
    invalid_coverage_statuses,
    invalid_item_statuses,
    read_task_progress,
    requirement_done_without_evidence,
    write_task_progress,
)
from ..tooling.models import BaseTool, ToolExecutionResult
from .delivery_closeout.dispatch_coverage_reconcile import reconcile_dispatch_coverage
from .delivery_closeout.task_progress_gate import closeout_ledger_run_id
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
        if action_error := _invalid_action_result(action):
            return action_error
        if action == "select":
            return _select_conversation_task(self.agent, params)
        if action == "start":
            return _start_conversation_task(self.agent)
        run_id = _target_run_id(self.agent, params, allow_explicit=action == "read")
        if not run_id:
            run_id = "main"
        root = runtime_owner_root(self.agent)
        if action == "update":
            if workspace_error := _workspace_decision_required(self.agent):
                return workspace_error
            if status_error := _invalid_status_result(params):
                return status_error
            if evidence_error := _requirement_done_evidence_result(self.agent, root, run_id, params):
                return evidence_error
            from ..conversation.task_promotion import promote_current_conversation_task

            promote_current_conversation_task(self.agent)
            payload = write_task_progress(root, run_id, params)
            payload = _with_write_feedback(payload)
            payload = _with_evidence_source_feedback(self.agent, payload)
        else:
            _reconcile_dispatch_coverage_before_read(self.agent, root, run_id)
            payload = read_task_progress(root, run_id)
        return ToolExecutionResult("task_progress", True, json.dumps(payload, ensure_ascii=False, indent=2))


def _reconcile_dispatch_coverage_before_read(agent: object, root: Path, run_id: str) -> None:
    """读账前先跑一遍派工路 coverage 对账(P1):子代理 DONE 后模型中途看账就是真进度
    (covers 绑定项已按 id 打勾),不用等收口门。只在主代理语境、读的就是本 run 的账时跑
    (子代理读自己的账 / 显式 run_id 读别的账都不沾);对账本身只增不减、solo 路空转,
    这里再兜一层异常——读账绝不因对账失败受影响。"""
    try:
        if current_subagent_run_id(agent):
            return
        params = getattr(agent, "_current_run_params", None)
        if params is None:
            return
        shim = SimpleNamespace(agent=agent, params=params)
        if closeout_ledger_run_id(shim) != run_id:
            return
        reconcile_dispatch_coverage(shim, None, root, run_id)
    except Exception:  # noqa: BLE001 - 对账是增强,读账主链路绝不受影响
        pass


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


def _requirement_done_evidence_result(
    agent: object, root: Path, run_id: str, update: dict[str, object]
) -> ToolExecutionResult | None:
    """需求项 done 证据闸(不足1,g8 升级为产物存在判据):自动种的需求枚举项标 done,
    evidence 必须指向真实存在的非占位交付产物(相对任务工作区/owner home 或绝对路径);
    空 evidence 或解析不出任何实存产物 → 拒绝本次写入,教两条出口(补真产物路径 /
    非功能碎片改 skipped+reason)。校验失败保守放行(闸是增强,绝不因读账异常卡死主链路)。"""
    try:
        roots = _artifact_evidence_roots(agent, root)
        violations = requirement_done_without_evidence(
            read_task_progress(root, run_id), update, artifact_roots=roots
        )
    except Exception:  # noqa: BLE001 - 证据闸是增强,校验异常不拦写入
        return None
    if not violations:
        return None
    payload = {
        "ok": False,
        "error": "requirement coverage targets need evidence pointing at a real existing deliverable before they can be marked done.",
        "targets_missing_evidence": violations[:12],
        "artifact_roots_checked": [str(item) for item in roots[:4]],
        "how_to_fix": (
            "真做完的项:status=done 时 evidence 必须写【真实存在的产物路径】(文件或非空目录,"
            "相对任务工作区如 output/auth/,或绝对路径;凭空写一句说明不算证据,系统会查路径存在);"
            "不是功能需求的项(字面枚举混入的约束/指令碎片,本就没有对应产物):改标 status=skipped "
            "并在 notes 写原因(skipped 不需要证据,也算闭环;别硬标 done)。"
        ),
    }
    return ToolExecutionResult(
        "task_progress",
        False,
        json.dumps(payload, ensure_ascii=False, indent=2),
        error_code="TOOL_INVALID_ARGUMENTS",
    )


def _artifact_evidence_roots(agent: object, owner_root: Path) -> tuple[Path, ...]:
    """证据路径解析根(证据闸的产物存在性判据用):当前 run 的任务工作区三目录
    (task_root/output_dir/work_dir)+ 子代理自己的任务工作区(树深处 run 写自己账时)
    + owner home 兜底。全部结构化来源,失败缺哪个就少哪个,owner root 恒在。"""
    run_params = getattr(agent, "_current_run_params", None)
    attrs = getattr(run_params, "task_attributes", None)
    workspace = attrs.get("run_workspace") if isinstance(attrs, dict) else None
    workspace = workspace if isinstance(workspace, dict) else {}
    texts = [str(workspace.get(key) or "").strip() for key in ("task_root", "output_dir", "work_dir")]
    texts.append(_current_subagent_workspace(agent))
    roots = [Path(text).expanduser() for text in texts if text]
    roots.append(owner_root)
    deduped: dict[str, Path] = {}
    for item in roots:
        deduped.setdefault(str(item), item)
    return tuple(deduped.values())


def _current_subagent_workspace(agent: object) -> str:
    try:
        run_id = current_subagent_run_id(agent)
        if not run_id:
            return ""
        task = agent.subagents.load(run_id)
        return str(getattr(task, "task_workspace_dir", "") or "").strip()
    except Exception:  # noqa: BLE001 - 根解析是增强,失败回落 owner root
        return ""


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


def _start_conversation_task(agent: object) -> ToolExecutionResult:
    from ..conversation.task_promotion import promote_current_conversation_task

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
    from ..conversation.task_promotion import select_current_conversation_task

    task_id = str(params.get("task_id") or "").strip()
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
            },
            ensure_ascii=False,
        ),
    )


def _target_run_id(agent: object, params: dict[str, object], *, allow_explicit: bool) -> str:
    """账本键解析(与收尾门 task_progress_gate._run_id、派工 seed 同一套语义,三处必须同本)。
    唯一特殊分支=【后台唤醒轮】(_current_run_params.source=="background_main_agent"):
    其 run_id 是新的(bg-main-*),task_id 仍是主任务——账本按【任务】延续,否则派工 seed
    立的账在唤醒轮里读写不到、模型只能另立新账(真机§7-3:主账 6 项全 open 却 ok=True
    收口,P4(a) 账本跨唤醒轮分裂的机制根因)。其余场景原链不动(子代理按自己 run 隔离)。"""
    explicit = str(params.get("run_id") or "").strip()
    if explicit and allow_explicit:
        return explicit
    current = getattr(agent, "_current_run_params", None)
    attrs = getattr(current, "task_attributes", None) if current is not None else None
    selected = (
        str(attrs.get("conversation_task_id") or "").strip()
        if isinstance(attrs, dict)
        else ""
    )
    if selected:
        return selected
    if str(getattr(current, "source", "") or "").strip() == "background_main_agent":
        task_id = str(getattr(current, "task_id", "") or "").strip()
        if task_id:
            return task_id
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
