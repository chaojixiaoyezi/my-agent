
from __future__ import annotations

from pathlib import Path
from typing import Any

from ...artifacts.registry import registry_path
from .artifacts import _relative_report_ref, _write_report


def write_non_terminal_closeout_report(
    closeout: object,
    workspace_root: Path,
    *,
    reason: str,
    contract: dict[str, Any] | None = None,
) -> None:
    params = getattr(closeout, "params", None)
    contract = contract or {}
    report = {
        "schema_version": "main_agent_delivery_closeout.v1",
        "ok": False,
        "case_id": str(contract.get("case_id") or ""),
        "request_id": str(getattr(params, "request_id", "") or ""),
        "run_id": str(getattr(params, "run_id", "") or ""),
        "task_id": str(getattr(params, "task_id", "") or ""),
        "workspace_root": str(workspace_root),
        "canonical_artifact_registry_ref": _relative_report_ref(registry_path(workspace_root), workspace_root),
        "artifacts": [],
        "non_terminal": True,
        "reason": reason,
        "message_zh": "本次 submit_for_acceptance 已记录，但没有足够的结构化交付合同可做最终验收；任务不因此终止。",
    }
    _write_report(workspace_root, report)


__all__ = ["write_non_terminal_closeout_report"]
