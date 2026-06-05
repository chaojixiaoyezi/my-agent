from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...runtime_errors import runtime_error_report
from ...subagents.role_templates import role_template_snapshot_for_task

_QUALITY_SCAN_MAX_NODES = 96


def quality_repair_advice_payload(agent: Any, parent_run_id: str) -> dict[str, object]:
    descendants, scan_error = _descendant_tasks(agent, parent_run_id)
    if scan_error:
        return {
            "qa_scan_error": runtime_error_report(scan_error, context="qa.descendant_scan"),
            "qa_scan_error_hint": "QA 子代理列表读取失败；这不是 QA 全部通过，也不是没有 QA 子代理。",
        }
    failing = [_qa_failure_signal(item) for item in descendants]
    failing = [item for item in failing if item]
    if not failing:
        return {}
    failed_ids = [str(item["run_id"]) for item in failing if item.get("run_id")]
    return {
        "qa_repair_advice": {
            "phase": "repair_wave_recommended",
            "failed_or_conflicting_qa_run_ids": failed_ids,
            "qa_signal_refs": list(failing),
            "llm_next_step": (
                "先读取失败 QA 的 output/report refs，再由 LLM 创建 scoped repair worker；"
                "修复后重新 dispatch tester，不要让单个检查结果覆盖失败证据。"
            ),
            "suggested_tool_call": _repair_child_tool_call(failed_ids),
        },
        "needs_repair_wave": True,
    }


def _descendant_tasks(agent: Any, parent_run_id: str) -> tuple[list[Any], BaseException | None]:
    try:
        runs = list(agent.subagents.list_runs())
    except Exception as exc:
        return [], exc
    by_parent: dict[str, list[Any]] = {}
    for item in runs:
        by_parent.setdefault(str(getattr(item, "parent_id", "") or ""), []).append(item)
    queue = list(by_parent.get(parent_run_id, []))
    seen: set[str] = set()
    descendants: list[Any] = []
    while queue and len(descendants) < _QUALITY_SCAN_MAX_NODES:
        item = queue.pop(0)
        run_id = str(getattr(item, "id", "") or "")
        if not run_id or run_id in seen:
            continue
        seen.add(run_id)
        descendants.append(item)
        queue.extend(by_parent.get(run_id, []))
    return descendants, None


def _qa_failure_signal(item: Any) -> dict[str, object]:
    if not _is_quality_check_task(item):
        return {}
    output_path = _output_json_path(item)
    payload, load_error = _read_output_payload(output_path)
    if load_error:
        return _qa_output_load_error_signal(item, output_path, load_error)
    if not _payload_has_negative_signal(item, payload):
        return {}
    return {
        "run_id": str(getattr(item, "id", "") or ""),
        "role": str(getattr(item, "role", "") or ""),
        "status": str(getattr(item, "status", "") or ""),
        "verification_status": str(getattr(item, "verification_status", "") or ""),
        "output_ref": str(output_path) if output_path else "",
        "summary": _payload_summary(payload),
    }


def _is_quality_check_task(item: Any) -> bool:
    snapshot = role_template_snapshot_for_task(item)
    if not snapshot:
        return False
    if bool(snapshot.get("can_run_tests")):
        return True
    return bool(snapshot.get("depends_on_outputs"))


def _output_json_path(item: Any) -> Path | None:
    explicit = str(getattr(item, "output_json", "") or "")
    if explicit:
        return Path(explicit)
    task_dir = str(getattr(item, "task_dir", "") or "")
    if task_dir:
        return Path(task_dir) / "output.json"
    return None


def _qa_output_load_error_signal(item: Any, output_path: Path | None, exc: BaseException) -> dict[str, object]:
    return {
        "run_id": str(getattr(item, "id", "") or ""),
        "role": str(getattr(item, "role", "") or ""),
        "status": str(getattr(item, "status", "") or ""),
        "verification_status": str(getattr(item, "verification_status", "") or ""),
        "output_ref": str(output_path) if output_path else "",
        "summary": "QA 输出文件读取失败；这不是 QA 通过。",
        "output_load_error": runtime_error_report(exc, context="qa.output_json"),
    }


def _read_output_payload(path: Path | None) -> tuple[dict[str, Any], BaseException | None]:
    if path is None or not path.exists():
        return {}, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, exc
    return (payload, None) if isinstance(payload, dict) else ({}, None)


def _payload_has_negative_signal(item: Any, payload: dict[str, Any]) -> bool:
    status = str(getattr(item, "status", "") or "").upper()
    if status in {"BLOCKED", "FAILED", "TIMEOUT", "CHANNEL_ERROR"}:
        return True
    structured = payload.get("structured_output") if isinstance(payload.get("structured_output"), dict) else {}
    structured_status = str(structured.get("status") or payload.get("status") or "").upper()
    if structured_status in {"BLOCKED", "FAILED", "FAIL", "ERROR"}:
        return True
    if payload.get("ok") is False or structured.get("ok") is False:
        return True
    if payload.get("passed") is False or structured.get("passed") is False:
        return True
    if payload.get("blockers"):
        return True
    for test in payload.get("tests") or []:
        if isinstance(test, dict) and (test.get("ok") is False or test.get("passed") is False):
            return True
    return False


def _textual_signal_payload(payload: dict[str, Any]) -> dict[str, Any]:
    structured = payload.get("structured_output") if isinstance(payload.get("structured_output"), dict) else {}
    return {
        "summary": payload.get("summary") or structured.get("summary") or "",
        "acceptance": payload.get("acceptance") or [],
        "blockers": payload.get("blockers") or [],
        "next_actions": payload.get("next_actions") or [],
    }


def _payload_summary(payload: dict[str, Any]) -> str:
    summary = _textual_signal_payload(payload).get("summary")
    return str(summary or "")[:400]


def _repair_child_tool_call(failed_ids: list[str]) -> dict[str, object]:
    joined = ", ".join(failed_ids)
    return {
        "tool": "schedule_child_subagents",
        "dry_run": False,
        "children": [
            {
                "role": "worker",
                "agent_name": "qa-repair-worker",
                "goal": (
                    "根据失败或冲突 QA refs 修复最小业务范围："
                    f"{joined}。先读取这些 QA run 的 output/report refs，"
                    "只改被证据点名的文件；修复后让 tester 复测。"
                ),
                "allowed_tools": [
                    "inspect_agent_tree",
                    "list_files",
                    "read_file",
                    "search_text",
                    "apply_patch",
                    "write_file",
                ],
            }
        ],
    }
