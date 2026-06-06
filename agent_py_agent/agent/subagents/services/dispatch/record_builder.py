
"""Dispatch record creation and building helpers."""

from __future__ import annotations

import time


class DispatchRecordBuilder:
    """Build dispatch audit records."""

    @staticmethod
    def make_record(manager, params) -> DispatchRecord:
        """Create a dispatch audit record from params bundle."""
        from agent_py_agent.agent.subagents.reports import DispatchRecord

        return DispatchRecord(
            id=manager._new_id("dispatch"),
            step=params.step,
            action=params.action,
            run_id=params.run_id,
            dry_run=params.dry_run,
            applied=params.applied,
            ok=params.ok,
            message=params.message,
            before_status=params.before_status,
            after_status=params.after_status,
            before_verification_status=params.before_verification_status,
            after_verification_status=params.after_verification_status,
            evidence_paths=params.evidence_paths or [],
            # 字段用途: 让父级看到 runner 真实创建的下级数量、id 和角色，后续按 refs 继续处理。
            runner_summary=params.runner_summary,
            runner_created_child_count=params.runner_created_child_count,
            runner_created_child_ids=params.runner_created_child_ids or [],
            runner_created_roles=params.runner_created_roles or [],
            runner_child_status_counts=params.runner_child_status_counts or {},
            runner_unfinished_child_ids=params.runner_unfinished_child_ids or [],
            runner_child_load_errors=params.runner_child_load_errors or [],
            collaboration_candidate_load_errors=params.collaboration_candidate_load_errors or [],
            runner_partial_success=params.runner_partial_success,
            runner_instruction=params.runner_instruction,
            suggested_max_runners=params.suggested_max_runners,
            created_at=time.time(),
        )

    @staticmethod
    def build_summary(records) -> dict[str, int]:
        """Summarize dispatch audit records."""
        summary: dict[str, int] = {"total": len(records)}
        for record in records:
            summary[record.step] = summary.get(record.step, 0) + 1
            summary[record.action] = summary.get(record.action, 0) + 1
            summary["ok" if record.ok else "failed"] = summary.get(
                "ok" if record.ok else "failed", 0,
            ) + 1
            summary["applied" if record.applied else "dry_run"] = summary.get(
                "applied" if record.applied else "dry_run", 0,
            ) + 1
            summary["runner_created_children"] = summary.get(
                "runner_created_children", 0,
            ) + int(record.runner_created_child_count or 0)
        return summary
