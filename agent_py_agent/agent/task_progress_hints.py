
from __future__ import annotations

from typing import Any

from .common.value_parsing import dedupe_strings, string_list
from .task_progress_coverage import normalize_coverage

_RESULT_FIELDS = ("result", "outcome", "conclusion", "decision", "summary")
_NON_RESULT_STATUSES = {"", "pending", "todo", "in_progress", "doing", "进行中", "未开始", "待处理"}


def quality_hints(
    items: list[dict[str, Any]],
    *,
    incoming: list[dict[str, Any]] | None = None,
    coverage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    coverage = normalize_coverage({"coverage": coverage or {}})
    result_without_evidence = _items_with_result_without_evidence(items)
    messages = _result_messages(result_without_evidence)
    if incoming:
        messages.extend(_incoming_messages(incoming))
        incoming_coverage = normalize_coverage({"coverage": {"targets": incoming}})
        if incoming_coverage["targets"]:
            coverage = incoming_coverage
    coverage_done_without_evidence = _coverage_done_without_evidence(coverage)
    coverage_incomplete = _coverage_incomplete(coverage)
    messages.extend(_coverage_messages(coverage_done_without_evidence, coverage_incomplete))
    next_suggestions = _next_suggestions(
        result_without_evidence=result_without_evidence,
        coverage_done_without_evidence=coverage_done_without_evidence,
        coverage_incomplete=coverage_incomplete,
    )
    return {
        "severity": "soft",
        "result_without_evidence_count": len(result_without_evidence),
        "result_without_evidence_ids": result_without_evidence[:20],
        "done_without_evidence_count": len(result_without_evidence),
        "done_without_evidence_ids": result_without_evidence[:20],
        "coverage_done_without_evidence_count": len(coverage_done_without_evidence),
        "coverage_done_without_evidence_ids": coverage_done_without_evidence[:20],
        "coverage_incomplete_count": len(coverage_incomplete),
        "coverage_incomplete_ids": coverage_incomplete[:20],
        "next_suggestions": next_suggestions,
        "soft_prompt": _soft_prompt(next_suggestions),
        "messages": dedupe_strings(messages),
    }


def _items_with_result_without_evidence(items: list[dict[str, Any]]) -> list[str]:
    return [
        str(item.get("id") or "")
        for item in items
        if _has_result_signal(item) and not string_list(item.get("evidence"))
    ]


def _coverage_done_without_evidence(coverage: dict[str, Any]) -> list[str]:
    return [
        str(target.get("id") or "")
        for target in coverage.get("targets", [])
        if _coverage_target_done(target) and not string_list(target.get("evidence"))
    ]


def _coverage_incomplete(coverage: dict[str, Any]) -> list[str]:
    return [
        str(target.get("id") or "")
        for target in coverage.get("targets", [])
        if not _coverage_target_done(target)
    ]


def _result_messages(result_without_evidence: list[str]) -> list[str]:
    if not result_without_evidence:
        return []
    return [
        "有些条目已经写了状态、结果或结论，但没有 evidence。建议补上看过的文件、产物路径、工具结果或简短证据引用；这只是软提醒，不会阻断任务。"
    ]


def _incoming_messages(incoming: list[dict[str, Any]]) -> list[str]:
    batch_result_without_evidence = [
        str(item.get("id") or "")
        for item in incoming
        if _has_result_signal(item) and not string_list(item.get("evidence"))
    ]
    if len(batch_result_without_evidence) < 3:
        return []
    return ["这次一次性写了多项状态、结果或结论，但缺少 evidence。长任务更稳的做法是边读、边分析、边写报告时同步更新进度和证据。"]


def _coverage_messages(done_without_evidence: list[str], incomplete: list[str]) -> list[str]:
    messages: list[str] = []
    if done_without_evidence:
        messages.append(
            "覆盖清单里有对象看起来已完成，但缺少 evidence。建议补上读过的文件、资料来源或写入报告的位置；这只是软提醒，不会阻断任务。"
        )
    if incomplete:
        messages.append(
            "覆盖清单里还有对象没有逐项完成。建议继续补未完成对象，不要只看 README 或目录；优先读核心源码、记录证据，再把结论写进产物。"
        )
    return messages


def _coverage_target_done(target: dict[str, Any]) -> bool:
    checks = dict(target.get("checks") or {})
    if checks:
        return all(
            str(status or "").strip().lower()
            in {"done", "complete", "completed", "ok", "passed", "skipped"}
            for status in checks.values()
        )
    return str(target.get("status") or "").strip().lower() in {
        "done",
        "complete",
        "completed",
        "ok",
        "passed",
        "skipped",
    }


def _has_result_signal(item: dict[str, Any]) -> bool:
    if any(str(item.get(key) or "").strip() for key in _RESULT_FIELDS):
        return True
    status = str(item.get("status") or "").strip().lower()
    return status not in _NON_RESULT_STATUSES


def _next_suggestions(
    *,
    result_without_evidence: list[str],
    coverage_done_without_evidence: list[str],
    coverage_incomplete: list[str],
) -> list[str]:
    suggestions: list[str] = []
    if coverage_incomplete:
        suggestions.append("继续补未完成对象：先选一个未完成对象，读核心文件或可靠来源，再更新 checks/evidence。")
    if result_without_evidence or coverage_done_without_evidence:
        suggestions.append("补证据引用：不要只打勾；每个有状态、结果或结论的条目最好写一个文件路径、产物路径、工具结果或来源说明。")
    if coverage_incomplete or result_without_evidence or coverage_done_without_evidence:
        suggestions.append("写报告时同步推进账本：读过什么、分析了什么、写进报告哪里，都用 task_progress 轻量记录。")
    return dedupe_strings(suggestions)


def _soft_prompt(suggestions: list[str]) -> str:
    if not suggestions:
        return ""
    return "软提醒，不会阻断任务：" + "；".join(suggestions)


def _list(value: object) -> list:
    return list(value) if isinstance(value, list | tuple) else []


__all__ = ["quality_hints"]
