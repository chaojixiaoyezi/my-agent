# LLM: Delivery progress ledger records append-only closeout progress events.
# 模块用途: 将 closeout 的失败指纹、恢复动作和 gate 结果写成可回放账本，避免只依赖最新报告快照。

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .main_agent_delivery_closeout_artifacts import CLOSEOUT_DIR
from .main_agent_delivery_closeout_gate_recovery import failed_gate_payloads

PROGRESS_LEDGER = "progress_ledger.jsonl"


# LLM: append_delivery_progress_event persists one compact progress fact row.
# 函数用途: 每次 closeout 后写入 append-only 账本，后续 replay、观察和无进展判断可复用。
def append_delivery_progress_event(
    workspace_root: Path,
    report: dict[str, Any],
    *,
    blocked: bool,
) -> Path:
    path = workspace_root / CLOSEOUT_DIR / PROGRESS_LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    path.open("a", encoding="utf-8").write(
        json.dumps(
            progress_event_payload(report, blocked=blocked),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    return path


def progress_event_payload(report: dict[str, Any], *, blocked: bool) -> dict[str, Any]:
    progress = report.get("delivery_progress")
    progress_payload = progress if isinstance(progress, dict) else {}
    return {
        "schema_version": "delivery_progress_ledger.v1",
        "event_type": "delivery_closeout_progress",
        "case_id": str(report.get("case_id") or ""),
        "request_id": str(report.get("request_id") or ""),
        "run_id": str(report.get("run_id") or ""),
        "task_id": str(report.get("task_id") or ""),
        "ok": bool(report.get("ok") is True and not failed_gate_payloads(report)),
        "blocked": bool(blocked),
        "failure_fingerprint": str(progress_payload.get("failure_fingerprint") or ""),
        "work_progress_fingerprint": str(progress_payload.get("work_progress_fingerprint") or ""),
        "unchanged_failure_count": _safe_int(progress_payload.get("unchanged_failure_count")),
        "no_progress_block_threshold": _safe_int(progress_payload.get("no_progress_block_threshold")),
        "failed_gates": failed_gate_payloads(report),
        "recovery_actions": [dict(item) for item in progress_payload.get("recovery_actions", []) if isinstance(item, dict)]
        if isinstance(progress_payload.get("recovery_actions"), list)
        else [],
        "pending_materialization_targets": [
            dict(item) for item in progress_payload.get("pending_materialization_targets", []) if isinstance(item, dict)
        ]
        if isinstance(progress_payload.get("pending_materialization_targets"), list)
        else [],
    }


def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


__all__ = ["PROGRESS_LEDGER", "append_delivery_progress_event", "progress_event_payload"]
