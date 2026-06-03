from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..io import append_jsonl


@dataclass(frozen=True)
class RollupEventRequest:
    compact_root: Path
    task_root: Path
    rollup: dict[str, object]
    package_dir: Path
    child_runs: list[dict[str, object]]


def append_rollup_event_if_changed(request: RollupEventRequest) -> None:
    ledger = request.compact_root / "rollup_ledger.jsonl"
    signature = rollup_signature(request.rollup)
    if last_rollup_signature(ledger) == signature:
        return
    append_jsonl(
        ledger,
        {
            "schema_version": "task-compact-rollup-event.v1",
            "task_workspace": str(request.task_root),
            "rollup_json": str(request.compact_root / "task_rollup.json"),
            "compact_package": str(request.package_dir),
            "child_count": len(request.child_runs),
            "status_counts": request.rollup.get("status_counts", {}),
            "pending_run_ids": request.rollup.get("pending_run_ids", []),
            "blocked_run_ids": request.rollup.get("blocked_run_ids", []),
            "signature": signature,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        sort_keys=True,
    )


def rollup_signature(rollup: dict[str, object]) -> str:
    payload = {
        "child_count": rollup.get("child_count", 0),
        "status_counts": rollup.get("status_counts", {}),
        "completed_run_ids": rollup.get("completed_run_ids", []),
        "pending_run_ids": rollup.get("pending_run_ids", []),
        "blocked_run_ids": rollup.get("blocked_run_ids", []),
        "load_error_count": len(rollup.get("load_errors", []) if isinstance(rollup.get("load_errors"), list) else []),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def last_rollup_signature(ledger: Path) -> str:
    if not ledger.exists():
        return ""
    try:
        lines = [line for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return ""
    for line in reversed(lines):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        signature = str(payload.get("signature") or "").strip()
        if signature:
            return signature
    return ""
