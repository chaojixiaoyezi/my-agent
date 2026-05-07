# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""Patch review serialization, status updates, and log persistence."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..reports import PatchReviewRecord, PatchReviewReport


# LLM: PatchReviewStatusUpdate 属于子代理补丁应用的类边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 类用途: 集中保存补丁审查状态update字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发补丁文件、预演结果和应用报告相关副作用，需保持公开契约稳定。
@dataclass(frozen=True)
class PatchReviewStatusUpdate:
    patches: list[dict]
    ok: bool
    reviewer: str
    now: float
    note: str


# LLM: serialize_patch_review_record 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 函数用途: 处理serialize补丁审查记录相关的数据流，连接当前职责的前后步骤；关键副作用: 会改动补丁文件、预演结果和应用报告，调用方依赖写入顺序和文件格式。
def serialize_patch_review_record(record: PatchReviewRecord) -> dict:
    return {
        "id": record.id,
        "run_id": record.run_id,
        "dry_run": record.dry_run,
        "applied": record.applied,
        "ok": record.ok,
        "decision": record.decision,
        "message": record.message,
        "patch_count": record.patch_count,
        "approved_count": record.approved_count,
        "blocked_count": record.blocked_count,
        "reviewer": record.reviewer,
        "note": record.note,
        "evidence_paths": record.evidence_paths,
        "patches": record.patches,
        "created_at": record.created_at,
    }


# LLM: serialize_patch_review_report 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 函数用途: 处理serialize补丁审查报告相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持补丁文件、预演结果和应用报告上的返回值和副作用边界稳定。
def serialize_patch_review_report(report: PatchReviewReport) -> dict:
    return {
        "generated_at": report.generated_at,
        "dry_run": report.dry_run,
        "summary": report.summary,
        "records": [serialize_patch_review_record(record) for record in report.records],
    }


# LLM: write_patch_review_report_json 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 函数用途: 写入补丁审查报告JSON的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动补丁文件、预演结果和应用报告，调用方依赖写入顺序和文件格式。
def write_patch_review_report_json(manager, report: PatchReviewReport) -> None:
    (manager.workspace / "subagent_patch_review_report.json").write_text(
        json.dumps(serialize_patch_review_report(report), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# LLM: write_patch_review_record_files 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 函数用途: 写入补丁审查记录文件的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动补丁文件、预演结果和应用报告，调用方依赖写入顺序和文件格式。
def write_patch_review_record_files(manager, record: PatchReviewRecord) -> None:
    from .patch_renderer import render_patch_review_record_markdown

    try:
        task = manager.load(record.run_id)
    except FileNotFoundError:
        return
    (Path(task.reports_dir) / "patch_review.json").write_text(
        json.dumps(serialize_patch_review_record(record), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (Path(task.task_dir) / "PATCH_REVIEW.md").write_text(
        render_patch_review_record_markdown(record),
        encoding="utf-8",
    )


# LLM: apply_review_status 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 函数用途: 更新审查状态对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新补丁文件、预演结果和应用报告，需避免破坏既有状态机约定。
def apply_review_status(update: PatchReviewStatusUpdate) -> None:
    if update.ok:
        _apply_approved_patches(update)
    else:
        _apply_rejected_patches(update)


# LLM: append_patch_review_log 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 函数用途: 写入补丁审查log的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动补丁文件、预演结果和应用报告，调用方依赖写入顺序和文件格式。
def append_patch_review_log(manager, record: PatchReviewRecord) -> None:
    from ...file_io import append_jsonl

    jsonl = manager.workspace / "subagent_patch_review_log.jsonl"
    append_jsonl(jsonl, serialize_patch_review_record(record))

    markdown = manager.workspace / "PATCH_REVIEW_LOG.md"
    if not markdown.exists():
        markdown.write_text("# PATCH REVIEW LOG\n\n", encoding="utf-8")
    with markdown.open("a", encoding="utf-8") as handle:
        status = "OK" if record.ok else "FAIL"
        handle.write(
            f"- [{status}] {record.id} run={record.run_id} decision={record.decision} "
            f"applied={record.applied} message={record.message}\n"
        )
    manager._index_patch_review(record)


# LLM: _apply_approved_patches 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 函数用途: 更新approvedpatches对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新补丁文件、预演结果和应用报告，需避免破坏既有状态机约定。
def _apply_approved_patches(update: PatchReviewStatusUpdate) -> None:
    for item in update.patches:
        item["review_status"] = "APPROVED"
        item["reviewed_by"] = update.reviewer
        item["reviewed_at"] = update.now
        if update.note:
            item["review_note"] = update.note


# LLM: _apply_rejected_patches 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 函数用途: 更新rejectedpatches对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新补丁文件、预演结果和应用报告，需避免破坏既有状态机约定。
def _apply_rejected_patches(update: PatchReviewStatusUpdate) -> None:
    for item in update.patches:
        _apply_rejected_patch_status(item, reviewer=update.reviewer, now=update.now, note=update.note)


# LLM: _apply_rejected_patch_status 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 函数用途: 更新rejected补丁状态对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新补丁文件、预演结果和应用报告，需避免破坏既有状态机约定。
def _apply_rejected_patch_status(item: dict, *, reviewer: str, now: float, note: str) -> None:
    if str(item.get("status", "")).lower() == "applied":
        return
    item["review_status"] = "NEEDS_ACTION"
    item["reviewed_by"] = reviewer
    item["reviewed_at"] = now
    if note:
        item["review_note"] = note
