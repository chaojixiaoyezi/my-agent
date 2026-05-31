# LLM: Task compact rollups summarize child run recovery refs without replacing child facts.
# 模块用途: 给一个 task 下所有 agent/run 生成任务级 compact 汇总，父代理恢复时先读它再按需读子代理细节。

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..io import append_jsonl
from .compact_layout import ensure_compact_package


@dataclass(frozen=True)
class TaskCompactRollupResult:
    task_workspace: Path
    compact_root: Path
    rollup_json: Path
    rollup_markdown: Path
    compact_package_dir: Path
    child_count: int


def sync_task_compact_rollup(task_workspace: str | Path, *, compact_index: int | None = None) -> TaskCompactRollupResult:
    task_root = Path(task_workspace)
    compact_root = task_root / "compact"
    index = compact_index if compact_index is not None else _next_compact_index(compact_root / "compact_ledger.jsonl")
    package = ensure_compact_package(compact_root, compact_index=index, scope="task")
    child_runs = _child_run_records(task_root)
    rollup = _rollup_payload(task_root, child_runs, package.package_dir)
    rollup_json = compact_root / "task_rollup.json"
    rollup_md = compact_root / "task_rollup.md"
    _write_json(rollup_json, rollup)
    rollup_md.write_text(_rollup_markdown(rollup), encoding="utf-8")
    _write_json(package.work_state_snapshot_json, _work_state_payload(rollup))
    _write_json(package.refs_json, _refs_payload(rollup_json, rollup_md, child_runs))
    _write_json(package.continue_packet_json, _continue_packet_payload(rollup))
    package.handoff_summary_md.write_text(_rollup_markdown(rollup), encoding="utf-8")
    append_jsonl(
        compact_root / "rollup_ledger.jsonl",
        {
            "schema_version": "task-compact-rollup-event.v1",
            "task_workspace": str(task_root),
            "rollup_json": str(rollup_json),
            "compact_package": str(package.package_dir),
            "child_count": len(child_runs),
            "updated_at": _now_iso(),
        },
        sort_keys=True,
    )
    return TaskCompactRollupResult(
        task_workspace=task_root,
        compact_root=compact_root,
        rollup_json=rollup_json,
        rollup_markdown=rollup_md,
        compact_package_dir=package.package_dir,
        child_count=len(child_runs),
    )


def _child_run_records(task_root: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    agents_root = task_root / "agents"
    for state_path in sorted(agents_root.glob("*/state.json")):
        payload = _read_json(state_path)
        run_id = str(payload.get("id") or payload.get("run_id") or state_path.parent.name)
        attrs = payload.get("attributes") if isinstance(payload.get("attributes"), dict) else {}
        system_tree = attrs.get("system_tree") if isinstance(attrs.get("system_tree"), dict) else {}
        rows.append(
            {
                "run_id": run_id,
                "status": str(payload.get("status") or system_tree.get("status") or ""),
                "progress": _safe_float(payload.get("progress") or system_tree.get("progress")),
                "summary": str(payload.get("latest_summary") or system_tree.get("latest_summary") or "")[:500],
                "refs": {
                    "state": str(state_path),
                    "compact": str(state_path.parent / "compactions"),
                    "summary": str(state_path.parent / "summary.md"),
                    "final_report": str(state_path.parent / "final_report.md"),
                    "artifact_manifest": str(state_path.parent / "artifacts" / "manifest.jsonl"),
                },
                "artifact_refs": _list_strings(payload.get("artifact_refs") or system_tree.get("artifact_refs")),
                "blockers": _list_strings(payload.get("blockers") or system_tree.get("blockers")),
            }
        )
    return rows


def _rollup_payload(task_root: Path, child_runs: list[dict[str, object]], package_dir: Path) -> dict[str, object]:
    state = _read_json(task_root / "state.json")
    return {
        "schema_version": "task-compact-rollup.v1",
        "task_id": str(state.get("task_id") or task_root.name),
        "task_workspace": str(task_root),
        "status": str(state.get("status") or ""),
        "progress": _safe_float(state.get("progress")),
        "primary_run_id": str(state.get("primary_run_id") or ""),
        "compact_package": str(package_dir),
        "child_runs": child_runs,
        "child_count": len(child_runs),
        "updated_at": _now_iso(),
    }


def _work_state_payload(rollup: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "work-state-snapshot.v1",
        "scope": "task",
        "task_id": rollup.get("task_id", ""),
        "status": rollup.get("status", ""),
        "progress": rollup.get("progress", 0.0),
        "child_count": rollup.get("child_count", 0),
        "updated_at": rollup.get("updated_at", ""),
    }


def _refs_payload(rollup_json: Path, rollup_md: Path, child_runs: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": "compact-refs.v1",
        "refs": [
            {"kind": "task_rollup", "path": str(rollup_json)},
            {"kind": "task_rollup_markdown", "path": str(rollup_md)},
            *[
                {"kind": "child_run_state", "run_id": row.get("run_id", ""), "path": row["refs"]["state"]}
                for row in child_runs
                if isinstance(row.get("refs"), dict)
            ],
        ],
    }


def _continue_packet_payload(rollup: dict[str, object]) -> dict[str, object]:
    pending = [
        f"{row.get('run_id')}: {row.get('status')}"
        for row in rollup.get("child_runs", [])
        if isinstance(row, dict) and str(row.get("status") or "").upper() not in {"DONE", "COMPLETED", "SUCCEEDED"}
    ]
    return {
        "schema_version": "continue-packet.v1",
        "scope": "task",
        "next_action": "先读 task_rollup.json，再按 pending_work 读取对应 child run refs。",
        "completed_headings": [],
        "avoid_repeating": [],
        "active_refs": [str(rollup.get("task_workspace") or "")],
        "pending_work": pending,
    }


def _rollup_markdown(rollup: dict[str, object]) -> str:
    rows = [
        f"- {row.get('run_id')}: {row.get('status')} progress={row.get('progress')} summary={row.get('summary')}"
        for row in rollup.get("child_runs", [])
        if isinstance(row, dict)
    ]
    return (
        "# Task Compact Rollup\n\n"
        f"- task_id: {rollup.get('task_id', '')}\n"
        f"- status: {rollup.get('status', '')}\n"
        f"- child_count: {rollup.get('child_count', 0)}\n"
        f"- updated_at: {rollup.get('updated_at', '')}\n\n"
        "## Child Runs\n\n"
        + ("\n".join(rows) if rows else "- 暂无")
        + "\n"
    )


def _next_compact_index(ledger: Path) -> int:
    if not ledger.exists():
        return 1
    return sum(1 for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()) + 1


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _list_strings(value: object) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    return [str(item) for item in value if str(item)]


def _safe_float(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = ["TaskCompactRollupResult", "sync_task_compact_rollup"]
