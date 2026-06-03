
"""Patch apply summary builder helper."""

from __future__ import annotations


class PatchApplySummary:
    """Build patch apply summary statistics."""

    @staticmethod
    def build(records) -> dict[str, int]:
        """Build summary from patch apply records."""
        summary = {"total": len(records)}
        for record in records:
            summary[record.decision] = summary.get(record.decision, 0) + 1
            summary["ok" if record.ok else "failed"] = summary.get(
                "ok" if record.ok else "failed",
                0,
            ) + 1
            summary["dry_run" if record.dry_run else "applied"] = summary.get(
                "dry_run" if record.dry_run else "applied",
                0,
            ) + 1
            if record.rollback_performed:
                summary["rolled_back"] = summary.get("rolled_back", 0) + 1
        return summary