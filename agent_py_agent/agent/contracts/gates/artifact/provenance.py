
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from ...recovery import RecoveryAction
from ..models import GateDecision, GateFinding

_WRITE_ARTIFACT_TOOLS = {"write_file"}


def evaluate_artifact_provenance_gate(item: dict[str, Any], *, run_id: str = "") -> GateDecision:
    if item.get("ok") is not True:
        return GateDecision.allow("artifact_provenance", evidence={"skipped": "artifact_not_accepted"})
    provenance = item.get("provenance")
    if not _has_accepted_provenance(provenance):
        return _missing_provenance_decision()
    provenance_run_id = str(provenance.get("run_id") or "").strip()
    if run_id and provenance_run_id != run_id:
        return _run_mismatch_decision(provenance_run_id, run_id)
    findings = _hash_chain_findings(item, provenance)
    if findings:
        return GateDecision.repair("artifact_provenance", findings)
    warnings = _current_run_provenance_warnings(item, provenance)
    if warnings:
        return GateDecision.repair(
            "artifact_provenance",
            [GateFinding(code) for code in warnings],
            recommended_action=RecoveryAction.RECORD_PROVENANCE.value,
            evidence={
                "artifact_ref": str(provenance.get("artifact_ref") or ""),
                "tool_name": str(provenance.get("tool_name") or ""),
                "operation_id": str(provenance.get("operation_id") or ""),
                "run_id": provenance_run_id,
                "build_output_hash": str(provenance.get("build_output_hash") or ""),
                "warning_codes": warnings,
            },
        )
    return GateDecision.allow(
        "artifact_provenance",
        evidence={
            "artifact_ref": str(provenance.get("artifact_ref") or ""),
            "tool_name": str(provenance.get("tool_name") or ""),
            "operation_id": str(provenance.get("operation_id") or ""),
            "run_id": provenance_run_id,
            "build_output_hash": str(provenance.get("build_output_hash") or ""),
            "warning_codes": warnings,
        },
    )


def _has_accepted_provenance(value: object) -> bool:
    return isinstance(value, dict) and value.get("ok") is True


def _missing_provenance_decision() -> GateDecision:
    return GateDecision.repair(
        "artifact_provenance",
        [GateFinding("ARTIFACT_PROVENANCE_MISSING")],
        recommended_action=RecoveryAction.RECORD_PROVENANCE.value,
        evidence={"warning_codes": ["ARTIFACT_PROVENANCE_MISSING"]},
    )


def _run_mismatch_decision(provenance_run_id: str, run_id: str) -> GateDecision:
    return GateDecision.repair(
        "artifact_provenance",
        [GateFinding("ARTIFACT_PROVENANCE_RUN_MISMATCH")],
        recommended_action=RecoveryAction.VERIFY_CROSS_RUN_ARTIFACT.value,
        evidence={
            "warning_codes": ["ARTIFACT_PROVENANCE_RUN_MISMATCH"],
            "artifact_run_id": provenance_run_id,
            "current_run_id": run_id,
        },
    )


def _current_run_provenance_warnings(item: dict[str, Any], provenance: dict[str, Any]) -> list[str]:
    warnings = [
        code
        for code, value in (
            ("ARTIFACT_PROVENANCE_TOOL_MISSING", provenance.get("tool_name")),
            ("ARTIFACT_PROVENANCE_OPERATION_MISSING", provenance.get("operation_id")),
            ("ARTIFACT_PROVENANCE_IDEMPOTENCY_MISSING", provenance.get("idempotency_key")),
            ("ARTIFACT_PROVENANCE_REF_MISSING", provenance.get("artifact_ref")),
        )
        if not str(value or "").strip()
    ]
    if provenance.get("created_by_current_run") is not True:
        warnings.append("ARTIFACT_PROVENANCE_NOT_CURRENT_RUN")
    return warnings


def artifact_provenance_from_archive(
    item: dict[str, Any],
    archive_tool_calls: list[Any],
    *,
    run_id: str,
    workspace_root: Path,
) -> dict[str, Any]:
    artifact_path = _resolved_path(item.get("path"), workspace_root)
    if artifact_path is None:
        return {"ok": False, "code": "ARTIFACT_PATH_INVALID"}
    old_run_match: dict[str, Any] | None = None
    current_run_writes: list[tuple[str, int, dict[str, Any]]] = []
    current_run_matches: list[tuple[str, int, dict[str, Any]]] = []
    for index, record in enumerate(archive_tool_calls):
        if not isinstance(record, dict):
            continue
        if not _record_targets_artifact(record, artifact_path, workspace_root):
            continue
        if record.get("ok") is not True and not _record_materialized_artifact(record, artifact_path, workspace_root):
            continue
        provenance = _provenance_from_record(record, artifact_path=artifact_path, current_run_id=run_id)
        if provenance.get("ok") is not True:
            continue
        if provenance.get("run_id") == run_id:
            row = (str(record.get("created_at") or ""), index, provenance)
            if _record_is_current_run_artifact_write(record, artifact_path=artifact_path, current_run_id=run_id):
                current_run_writes.append(row)
            else:
                current_run_matches.append(row)
            continue
        old_run_match = old_run_match or provenance
    if current_run_writes:
        return max(current_run_writes, key=lambda row: (row[0], row[1]))[2]
    if current_run_matches:
        return max(current_run_matches, key=lambda row: (row[0], row[1]))[2]
    if old_run_match:
        return {**old_run_match, "created_by_current_run": False, "code": "ARTIFACT_PROVENANCE_RUN_MISMATCH"}
    return {"ok": False, "code": "ARTIFACT_PROVENANCE_MISSING"}


def _hash_chain_findings(item: dict[str, Any], provenance: dict[str, Any]) -> list[GateFinding]:
    findings: list[GateFinding] = []
    source_hashes = provenance.get("source_artifact_hashes")
    if isinstance(source_hashes, dict):
        findings.extend(_source_hash_findings(source_hashes))
    artifact_ref = str(provenance.get("artifact_ref") or "").strip()
    if output_finding := _output_hash_finding(artifact_ref, str(provenance.get("build_output_hash") or "")):
        findings.append(output_finding)
    if input_finding := _build_input_hash_finding(source_hashes, str(provenance.get("build_input_hash") or "")):
        findings.append(input_finding)
    return findings


def _source_hash_findings(source_hashes: dict[Any, Any]) -> list[GateFinding]:
    findings: list[GateFinding] = []
    for raw_ref, raw_expected in source_hashes.items():
        ref = str(raw_ref or "").strip()
        expected = _normalize_hash(raw_expected)
        if not ref or not expected:
            findings.append(
                GateFinding(
                    "ARTIFACT_PROVENANCE_SOURCE_HASH_MISSING",
                    evidence={"current_state": {"source_ref": ref, "hash": str(raw_expected or "")}, "required_state": {"source_hash": "sha256"}},
                )
            )
            continue
        path = Path(ref).expanduser().resolve(strict=False)
        actual = _file_hash(path)
        if actual and actual != expected:
            findings.append(
                GateFinding(
                    "BUILDER_PROVENANCE_STALE",
                    evidence={
                        "source_ref": ref,
                        "current_state": {"source_hash": actual},
                        "required_state": {"recorded_source_hash": expected},
                        "repair_action": "rebuild_from_latest_source",
                        "required_tool_calls": ["read_artifact", "rebuild_artifact"],
                        "retryable": True,
                    },
                )
            )
    return findings


def _build_input_hash_finding(source_hashes: object, build_input_hash: str) -> GateFinding | None:
    if not isinstance(source_hashes, dict) or len(source_hashes) != 1 or not build_input_hash:
        return None
    expected = _normalize_hash(next(iter(source_hashes.values())))
    current = _normalize_hash(build_input_hash)
    if expected and current and expected != current:
        return GateFinding(
            "BUILDER_PROVENANCE_INPUT_HASH_MISMATCH",
            evidence={
                "current_state": {"build_input_hash": current},
                "required_state": {"source_hash": expected},
                "repair_action": "rebuild_from_latest_source",
            },
        )
    return None


def _output_hash_finding(artifact_ref: str, build_output_hash: str) -> GateFinding | None:
    if not artifact_ref or not build_output_hash:
        return None
    actual = _file_hash(Path(artifact_ref).expanduser().resolve(strict=False))
    expected = _normalize_hash(build_output_hash)
    if actual and expected and actual != expected:
        return GateFinding(
            "BUILDER_PROVENANCE_OUTPUT_HASH_MISMATCH",
            evidence={
                "artifact_ref": artifact_ref,
                "current_state": {"output_hash": actual},
                "required_state": {"build_output_hash": expected},
                "repair_action": "rebuild_or_revalidate_artifact",
            },
        )
    return None


def _file_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _normalize_hash(value: object) -> str:
    text = str(value or "").strip()
    return text.split(":", 1)[1] if text.startswith("sha256:") else text


def _provenance_from_record(
    record: dict[str, Any],
    *,
    artifact_path: Path,
    current_run_id: str,
) -> dict[str, Any]:
    runtime_gate = record.get("runtime_gate")
    evidence = runtime_gate.get("evidence") if isinstance(runtime_gate, dict) else {}
    if (
        not isinstance(runtime_gate, dict)
        or not isinstance(evidence, dict)
        or runtime_gate.get("allowed") is not True
    ):
        if _record_is_current_run_artifact_write(record, artifact_path=artifact_path, current_run_id=current_run_id):
            return _write_record_provenance(record, artifact_path=artifact_path, current_run_id=current_run_id)
        return {"ok": False, "code": "ARTIFACT_TOOL_GATE_MISSING"}
    run_id = str(record.get("run_id") or "").strip()
    tool_name = str(evidence.get("tool_name") or "").strip()
    return {
        "ok": True,
        "artifact_ref": str(artifact_path),
        "run_id": run_id,
        "task_id": str(record.get("task_id") or ""),
        "tool_name": tool_name,
        "operation_id": str(evidence.get("operation_id") or record.get("operation_id") or ""),
        "idempotency_key": str(evidence.get("idempotency_key") or record.get("idempotency_key") or ""),
        "call_id": str(record.get("call_id") or ""),
        "created_by_current_run": bool(current_run_id and run_id == current_run_id),
        "created_at": str(record.get("created_at") or ""),
    }


def _record_is_current_run_artifact_write(
    record: dict[str, Any],
    *,
    artifact_path: Path,
    current_run_id: str,
) -> bool:
    if not current_run_id or str(record.get("run_id") or "").strip() != current_run_id:
        return False
    if str(record.get("tool") or "").strip() not in _WRITE_ARTIFACT_TOOLS:
        return False
    if not artifact_path.is_file():
        return False
    params = _mapping(record.get("parameters"))
    target = params.get("path")
    return bool(str(target or "").strip())


def _write_record_provenance(
    record: dict[str, Any],
    *,
    artifact_path: Path,
    current_run_id: str,
) -> dict[str, Any]:
    call_id = str(record.get("scoped_call_id") or record.get("call_id") or "").strip()
    sha = str(record.get("sha256") or "").strip()
    tool_name = str(record.get("tool") or "").strip()
    return {
        "ok": True,
        "artifact_ref": str(artifact_path),
        "run_id": str(record.get("run_id") or current_run_id),
        "task_id": str(record.get("task_id") or ""),
        "tool_name": tool_name,
        "operation_id": call_id or sha,
        "idempotency_key": sha or call_id,
        "call_id": call_id,
        "created_by_current_run": True,
        "build_output_hash": _file_hash(artifact_path),
        "proof_kind": "tool_output_index",
        "created_at": str(record.get("created_at") or ""),
    }


def _record_targets_artifact(record: dict[str, Any], artifact_path: Path, workspace_root: Path) -> bool:
    return any(_path_matches_artifact(path, artifact_path) for path in _record_paths(record, workspace_root))


def _record_materialized_artifact(record: dict[str, Any], artifact_path: Path, workspace_root: Path) -> bool:
    return any(path.exists() and _path_matches_artifact(path, artifact_path) for path in _record_paths(record, workspace_root))


def _record_paths(record: dict[str, Any], workspace_root: Path) -> list[Path]:
    paths: list[Path] = []
    for source in (record, _mapping(record.get("parameters")), _mapping(record.get("tool_result_envelope"))):
        for key in ("path", "file_path", "target_path", "artifact_ref", "output_path", "source_json_path"):
            paths.extend(_paths_from_value(source.get(key), workspace_root))
    refs = record.get("tool_result_refs")
    if isinstance(refs, list):
        for ref in refs:
            paths.extend(_paths_from_value(_mapping(ref).get("path") or _mapping(ref).get("artifact_ref"), workspace_root))
    return paths


def _paths_from_value(value: object, workspace_root: Path) -> list[Path]:
    if isinstance(value, dict):
        candidates = [value.get("resolved"), value.get("raw"), value.get("path"), value.get("artifact_ref")]
    else:
        candidates = [value]
    return [path for candidate in candidates if (path := _resolved_path(candidate, workspace_root)) is not None]


def _resolved_path(value: object, workspace_root: Path) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    return path.resolve(strict=False) if path.is_absolute() else (workspace_root / path).resolve(strict=False)


def _path_matches_artifact(candidate: Path, artifact_path: Path) -> bool:
    if candidate == artifact_path:
        return True
    if artifact_path.suffix:
        return False
    try:
        candidate.relative_to(artifact_path)
    except ValueError:
        return False
    return True


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


__all__ = ["artifact_provenance_from_archive", "evaluate_artifact_provenance_gate"]
