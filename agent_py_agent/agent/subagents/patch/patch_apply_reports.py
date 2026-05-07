# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

"""Patch apply record serialization helpers.

Human version:
这个模块处理 patch apply 记录的字典序列化，避免在 patch_apply.py 中重复代码。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..reports import PatchApplyRecord


# LLM: patch_apply_record_to_dict 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 函数用途: 处理补丁应用记录todict相关的数据流，连接当前职责的前后步骤；关键副作用: 会改动补丁文件、预演结果和应用报告，调用方依赖写入顺序和文件格式。
def patch_apply_record_to_dict(record: PatchApplyRecord) -> dict:
    """Convert PatchApplyRecord to dict for JSON serialization."""

    return {
        "id": record.id,
        "run_id": record.run_id,
        "dry_run": record.dry_run,
        "applied": record.applied,
        "ok": record.ok,
        "decision": record.decision,
        "message": record.message,
        "patch_count": record.patch_count,
        "applied_count": record.applied_count,
        "blocked_count": record.blocked_count,
        "rollback_performed": record.rollback_performed,
        "applier": record.applier,
        "note": record.note,
        "evidence_paths": record.evidence_paths,
        "test_commands": record.test_commands,
        "test_results": record.test_results,
        "patches": record.patches,
        "created_at": record.created_at,
    }


# LLM: patch_apply_report_to_dict 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 函数用途: 处理补丁应用报告todict相关的数据流，连接当前职责的前后步骤；关键副作用: 会更新补丁文件、预演结果和应用报告，需避免破坏既有状态机约定。
def patch_apply_report_to_dict(report) -> dict:
    """Convert PatchApplyReport to dict for JSON serialization."""

    return {
        "generated_at": report.generated_at,
        "dry_run": report.dry_run,
        "summary": report.summary,
        "records": [patch_apply_record_to_dict(r) for r in report.records],
    }
