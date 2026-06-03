
"""Patch apply record serialization helpers.

Human version:
这个模块处理 patch apply 记录的字典序列化，避免在 patch_apply.py 中重复代码。
owner policy、批量验证和失败恢复字段也在这里统一序列化，保证报告和日志看到同一份审计证据。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..reports import PatchApplyRecord


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
        "load_errors": record.load_errors,
        "owner_policy": record.owner_policy,
        "batch_validation": record.batch_validation,
        "failure_recovery": record.failure_recovery,
        "created_at": record.created_at,
    }


def patch_apply_report_to_dict(report) -> dict:
    """Convert PatchApplyReport to dict for JSON serialization."""

    return {
        "generated_at": report.generated_at,
        "dry_run": report.dry_run,
        "summary": report.summary,
        "records": [patch_apply_record_to_dict(r) for r in report.records],
    }
