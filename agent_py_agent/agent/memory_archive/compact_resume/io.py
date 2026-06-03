from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...common.json_io import read_json_object, read_json_object_report, read_jsonl_objects
from ...runtime_errors import runtime_error_report


@dataclass(frozen=True)
class CompactApplyArtifactsReport:
    artifacts: dict[str, Any] = field(default_factory=dict)
    load_errors: list[dict[str, object]] = field(default_factory=list)


def resolve_compact_metadata_path(workspace: Path, apply_ref: str) -> Path:
    candidate = Path(apply_ref).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    if candidate.exists():
        return _metadata_path_from_existing(candidate)
    indexed = _metadata_path_from_global_ledger(workspace, apply_ref)
    if indexed is not None and indexed.exists():
        return indexed
    return workspace / "memory_archive" / "compact_applies" / f"{apply_ref}.json"


def read_compact_apply_artifacts(metadata: dict[str, Any]) -> dict[str, Any]:
    return read_compact_apply_artifacts_report(metadata).artifacts


def read_compact_apply_artifacts_report(metadata: dict[str, Any]) -> CompactApplyArtifactsReport:
    refs = metadata.get("refs", {}) if isinstance(metadata.get("refs"), dict) else {}
    artifact_specs = {
        "apply_bundle": ("apply_bundle", "json"),
        "restore_refs": ("restore_refs", "json"),
        "work_state": ("work_state_snapshot", "json"),
        "compaction_state": ("compaction_state", "json"),
        "handoff_summary": ("handoff_summary", "text"),
        "self_check": ("post_compact_self_check", "json"),
        "compact_context": ("compact_context", "text"),
        "main_context_bundle": ("main_context_bundle", "json"),
    }
    artifacts: dict[str, Any] = {}
    load_errors: list[dict[str, object]] = []
    for artifact_name, (ref_name, kind) in artifact_specs.items():
        if kind == "text":
            value, error = _read_text_artifact(refs.get(ref_name), artifact_name)
        else:
            value, error = _read_json_artifact(refs.get(ref_name), artifact_name)
        artifacts[artifact_name] = value
        if error:
            load_errors.append(error)
    return CompactApplyArtifactsReport(artifacts=artifacts, load_errors=load_errors)


def _metadata_path_from_existing(path: Path) -> Path:
    name = path.name
    for suffix in (
        ".apply_bundle.json",
        ".restore_refs.json",
        ".work_state_snapshot.json",
        ".self_check.json",
        ".self_check_failed.json",
    ):
        if name.endswith(suffix):
            return path.with_name(name[: -len(suffix)] + ".json")
    return path


def _metadata_path_from_global_ledger(workspace: Path, apply_ref: str) -> Path | None:
    target = str(apply_ref or "").strip()
    if not target:
        return None
    ledger = workspace / "memory_archive" / "compact_applies" / "ledger.jsonl"
    for record in _read_jsonl_dicts(ledger):
        if str(record.get("apply_id") or record.get("event_id") or "") != target:
            continue
        refs = record.get("refs", {}) if isinstance(record.get("refs"), dict) else {}
        path = Path(str(refs.get("metadata") or ""))
        if path.exists():
            return path
    return None


def _read_jsonl_dicts(path: Path) -> list[dict[str, Any]]:
    return read_jsonl_objects(path)


def _read_json_path(value: object) -> dict[str, Any]:
    return read_json_object(Path(str(value))) if value else {}


def _read_json_artifact(value: object, artifact_name: str) -> tuple[dict[str, Any], dict[str, object] | None]:
    if not value:
        return {}, None
    path = Path(str(value))
    if not path.exists():
        return {}, _artifact_load_error(artifact_name, path, FileNotFoundError(str(path)))
    report = read_json_object_report(path, context=f"compact_resume.artifact.{artifact_name}")
    error = _with_artifact(artifact_name, report.load_error) if report.load_error else None
    return report.payload, error


def _read_text_path(value: object) -> str:
    if not value:
        return ""
    try:
        return Path(str(value)).read_text(encoding="utf-8")
    except OSError:
        return ""


def _read_text_artifact(value: object, artifact_name: str) -> tuple[str, dict[str, object] | None]:
    if not value:
        return "", None
    path = Path(str(value))
    if not path.exists():
        return "", _artifact_load_error(artifact_name, path, FileNotFoundError(str(path)))
    try:
        return path.read_text(encoding="utf-8"), None
    except (OSError, UnicodeDecodeError) as exc:
        return "", _artifact_load_error(artifact_name, path, exc)


def _artifact_load_error(artifact_name: str, path: Path, exc: BaseException) -> dict[str, object]:
    report = runtime_error_report(exc, context=f"compact_resume.artifact.{artifact_name}")
    report["path"] = str(path)
    return _with_artifact(artifact_name, report)


def _with_artifact(artifact_name: str, report: dict[str, object] | None) -> dict[str, object] | None:
    if not report:
        return None
    enriched = dict(report)
    enriched["artifact"] = artifact_name
    return enriched


__all__ = [
    "CompactApplyArtifactsReport",
    "read_compact_apply_artifacts",
    "read_compact_apply_artifacts_report",
    "read_json_object",
    "resolve_compact_metadata_path",
]
