
"""Snapshot registered artifacts around shell execution.

Human version:
Shell can run arbitrary scripts inside its allowed access mode, so it cannot use
the same pre-write validation path as write_file. This module protects existing
ready artifacts by copying them before shell starts and reconciling their status
after shell exits.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from agent_py_agent.agent.contracts.artifact_format_lint import lint_artifact_format

from .registry import ArtifactRegistration, latest_artifact_records, register_artifact


@dataclass(frozen=True)
class ShellArtifactSnapshot:
    """One protected artifact before run_command starts."""

    artifact_id: str
    run_id: str
    task_id: str
    agent_id: str
    kind: str
    mime_type: str
    path: str
    sha256: str
    size_bytes: int
    backup_ref: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def snapshot_ready_artifacts(workspace_root: str | Path) -> list[ShellArtifactSnapshot]:
    """Copy current ready artifacts so shell damage has a restore reference."""

    root = Path(workspace_root).expanduser().resolve(strict=False)
    records = latest_artifact_records(root)
    backup_root = root / "data" / "artifacts" / "shell_backups" / str(time.time_ns())
    snapshots: list[ShellArtifactSnapshot] = []
    for record in records.values():
        if str(record.status or "").lower() != "ready":
            continue
        path = Path(record.path).expanduser().resolve(strict=False)
        if not path.is_file():
            continue
        backup = backup_root / _safe_name(record.artifact_id) / path.name
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
        snapshots.append(
            ShellArtifactSnapshot(
                artifact_id=record.artifact_id,
                run_id=record.run_id,
                task_id=record.task_id,
                agent_id=record.agent_id,
                kind=record.kind,
                mime_type=record.mime_type,
                path=str(path),
                sha256=record.sha256,
                size_bytes=record.size_bytes,
                backup_ref=str(backup),
            )
        )
    return snapshots


def reconcile_shell_artifacts(
    workspace_root: str | Path,
    snapshots: list[ShellArtifactSnapshot],
) -> dict[str, Any]:
    """Update registry entries for shell-changed artifacts and return a prompt-safe summary."""

    root = Path(workspace_root).expanduser().resolve(strict=False)
    changed: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    for snapshot in snapshots:
        path = Path(snapshot.path).expanduser().resolve(strict=False)
        status, findings = _post_shell_status(path, root, snapshot)
        if status == "unchanged":
            continue
        registered = register_artifact(
            ArtifactRegistration(
                workspace_root=root,
                path=path,
                artifact_id=snapshot.artifact_id,
                run_id=snapshot.run_id,
                task_id=snapshot.task_id,
                agent_id=snapshot.agent_id,
                kind=snapshot.kind,
                mime_type=snapshot.mime_type,
                source="shell_postcheck",
                created_by_tool="run_command",
                status="ready" if status == "changed_ready" else "invalid",
                metadata={
                    "change_status": status,
                    "previous_sha256": snapshot.sha256,
                    "previous_size_bytes": snapshot.size_bytes,
                    "backup_ref": snapshot.backup_ref,
                    "findings": findings,
                },
            )
        )
        item = {
            "artifact_id": snapshot.artifact_id,
            "path": str(path),
            "status": registered.status,
            "change_status": status,
            "backup_ref": snapshot.backup_ref,
            "finding_codes": [str(finding.get("code") or "") for finding in findings if finding.get("code")],
        }
        changed.append(item)
        if registered.status == "invalid":
            invalid.append(item)
    return {
        "snapshots": len(snapshots),
        "changed": changed,
        "invalid": invalid,
    }


def shell_artifact_protection_note(summary: dict[str, Any]) -> str:
    """Render concise machine-readable facts for the model after shell exits."""

    changed = list(summary.get("changed") or [])
    invalid = list(summary.get("invalid") or [])
    if not changed and not invalid:
        return ""
    lines = [
        "artifact_protection:",
        f"artifact_protection_changed={len(changed)}",
        f"artifact_protection_invalid={len(invalid)}",
    ]
    for item in invalid[:5]:
        codes = ",".join(str(code) for code in item.get("finding_codes") or [])
        lines.append(
            "artifact_invalid_after_shell="
            f"{item.get('artifact_id')} path={item.get('path')} "
            f"backup_ref={item.get('backup_ref')} codes={codes}"
        )
    return "\n".join(lines)


def _post_shell_status(
    path: Path,
    workspace_root: Path,
    snapshot: ShellArtifactSnapshot,
) -> tuple[str, list[dict[str, Any]]]:
    if not path.exists():
        return "missing_after_shell", [{"code": "ARTIFACT_MISSING_AFTER_SHELL", "message": "artifact path is missing"}]
    if not path.is_file():
        return "invalid_after_shell", [{"code": "ARTIFACT_NOT_FILE_AFTER_SHELL", "message": "artifact path is not a file"}]
    current_sha = _sha256_file(path)
    if current_sha == snapshot.sha256:
        return "unchanged", []
    if path.stat().st_size == 0:
        return "invalid_after_shell", [{"code": "ARTIFACT_EMPTY_AFTER_SHELL", "message": "artifact is empty"}]
    report = lint_artifact_format(path=path, workspace_root=workspace_root)
    findings = [_finding_to_dict(finding) for finding in report.findings]
    return ("changed_ready" if report.ok else "invalid_after_shell", findings)


def _finding_to_dict(value: object) -> dict[str, Any]:
    if hasattr(value, "to_dict") and callable(value.to_dict):  # type: ignore[attr-defined]
        return dict(value.to_dict())
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    return {"message": str(value)}


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in value)
    return cleaned or "artifact"


__all__ = [
    "ShellArtifactSnapshot",
    "reconcile_shell_artifacts",
    "shell_artifact_protection_note",
    "snapshot_ready_artifacts",
]
