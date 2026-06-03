
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..common.json_io import read_json_object_report, write_json_object
from ..common.path_segments import safe_path_segment


@dataclass(frozen=True)
class OwnerCompactIndexRefsReport:
    dangling_refs: list[dict[str, object]]
    load_errors: list[dict[str, object]]


def sync_owner_compact_indexes(task_root: Path, rollup: dict[str, object]) -> None:
    owner_home = _owner_home_for_task(task_root)
    if owner_home is None:
        return
    task_id = _safe_key(rollup.get("task_id") or task_root.name)
    payload = _owner_compact_payload(rollup)
    write_json_object(owner_home / "compact" / "by_task" / f"{task_id}.json", payload)
    for row in rollup.get("child_runs", []):
        if not isinstance(row, dict):
            continue
        run_id = _safe_key(row.get("run_id"))
        if not run_id:
            continue
        child_payload = {**payload, "run_id": str(row.get("run_id") or ""), "status": str(row.get("status") or "")}
        write_json_object(owner_home / "compact" / "by_run" / f"{run_id}.json", child_payload)
        write_json_object(owner_home / "compact" / "by_agent" / f"{run_id}.json", child_payload)


def dangling_owner_compact_index_refs(owner_home: str | Path, *, limit: int = 100) -> list[dict[str, object]]:
    return dangling_owner_compact_index_refs_report(owner_home, limit=limit).dangling_refs


def dangling_owner_compact_index_refs_report(owner_home: str | Path, *, limit: int = 100) -> OwnerCompactIndexRefsReport:
    root = Path(owner_home) / "compact"
    findings: list[dict[str, object]] = []
    load_errors: list[dict[str, object]] = []
    for index_kind, pointer, payload, load_error in _compact_pointer_payloads(root):
        if load_error:
            load_errors.append(load_error)
        findings.extend(_pointer_findings(index_kind=index_kind, pointer=pointer, payload=payload))
        if len(findings) >= limit:
            return OwnerCompactIndexRefsReport(findings[:limit], load_errors)
    return OwnerCompactIndexRefsReport(findings, load_errors)


def _compact_pointer_payloads(root: Path) -> list[tuple[str, Path, dict[str, object], dict[str, object] | None]]:
    rows: list[tuple[str, Path, dict[str, object], dict[str, object] | None]] = []
    for index_kind in ("by_task", "by_run", "by_agent"):
        rows.extend(_read_pointer(index_kind, pointer) for pointer in sorted((root / index_kind).glob("*.json")))
    return rows


def _read_pointer(index_kind: str, pointer: Path) -> tuple[str, Path, dict[str, object], dict[str, object] | None]:
    report = read_json_object_report(pointer, context="owner_compact_index.pointer")
    return index_kind, pointer, report.payload, report.load_error


def _pointer_findings(*, index_kind: str, pointer: Path, payload: dict[str, object]) -> list[dict[str, object]]:
    if payload:
        return _missing_pointer_targets(pointer, index_kind=index_kind, payload=payload)
    return [
        {
            "kind": index_kind,
            "pointer": str(pointer),
            "field": "pointer_json",
            "missing_path": str(pointer),
            "reason": "unreadable_compact_index_pointer",
        }
    ]


def _missing_pointer_targets(pointer: Path, *, index_kind: str, payload: dict[str, object]) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    for field in ("rollup_json", "compact_package"):
        target = str(payload.get(field) or "")
        if target and Path(target).exists():
            continue
        findings.append(
            {
                "kind": index_kind,
                "pointer": str(pointer),
                "field": field,
                "missing_path": target,
                "reason": "missing_compact_index_target" if target else "empty_compact_index_target",
                "task_id": str(payload.get("task_id") or ""),
                "run_id": str(payload.get("run_id") or ""),
            }
        )
    return findings


def _owner_compact_payload(rollup: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "owner-compact-index.v1",
        "task_id": str(rollup.get("task_id") or ""),
        "task_workspace": str(rollup.get("task_workspace") or ""),
        "rollup_json": str(rollup.get("rollup_json") or ""),
        "compact_package": str(rollup.get("compact_package") or ""),
        "status": str(rollup.get("status") or ""),
        "child_count": int(rollup.get("child_count") or 0),
        "updated_at": str(rollup.get("updated_at") or ""),
    }


def _owner_home_for_task(task_root: Path) -> Path | None:
    parts = task_root.parts
    if "tasks" not in parts:
        return None
    index = len(parts) - 1 - list(reversed(parts)).index("tasks")
    if index <= 0:
        return None
    return Path(*parts[:index])


def _safe_key(value: object) -> str:
    return safe_path_segment(value, default="", replacement="_")


__all__ = [
    "OwnerCompactIndexRefsReport",
    "dangling_owner_compact_index_refs",
    "dangling_owner_compact_index_refs_report",
    "sync_owner_compact_indexes",
]
