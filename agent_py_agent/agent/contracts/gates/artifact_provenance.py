# LLM: Artifact provenance gate ties accepted artifacts to current-run tool evidence.
# 模块用途: 从 archive_tool_calls 里的结构化工具记录提取产物来源，防止旧文件或口头声明冒充本轮交付。

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .models import GateDecision, GateFinding


# LLM: evaluate_artifact_provenance_gate validates one artifact's current-run provenance.
# 函数用途: 要求已通过验收的产物带 run_id/tool/operation/idempotency 证据，且来源属于当前 run。
def evaluate_artifact_provenance_gate(item: dict[str, Any], *, run_id: str = "") -> GateDecision:
    if item.get("ok") is not True:
        return GateDecision.allow("artifact_provenance", evidence={"skipped": "artifact_not_accepted"})
    provenance = item.get("provenance")
    if not isinstance(provenance, dict) or provenance.get("ok") is not True:
        return GateDecision.repair("artifact_provenance", [GateFinding("ARTIFACT_PROVENANCE_MISSING")])
    provenance_run_id = str(provenance.get("run_id") or "").strip()
    if run_id and provenance_run_id != run_id:
        return GateDecision.repair(
            "artifact_provenance",
            [
                GateFinding(
                    "ARTIFACT_PROVENANCE_RUN_MISMATCH",
                    evidence={"artifact_run_id": provenance_run_id, "current_run_id": run_id},
                )
            ],
        )
    findings = [
        GateFinding(code)
        for code, value in (
            ("ARTIFACT_PROVENANCE_TOOL_MISSING", provenance.get("tool_name")),
            ("ARTIFACT_PROVENANCE_OPERATION_MISSING", provenance.get("operation_id")),
            ("ARTIFACT_PROVENANCE_IDEMPOTENCY_MISSING", provenance.get("idempotency_key")),
            ("ARTIFACT_PROVENANCE_REF_MISSING", provenance.get("artifact_ref") or provenance.get("path") or item.get("path")),
        )
        if not str(value or "").strip()
    ]
    if provenance.get("created_by_current_run") is not True:
        findings.append(GateFinding("ARTIFACT_PROVENANCE_NOT_CURRENT_RUN"))
    findings.extend(_hash_chain_findings(item, provenance))
    if findings:
        return GateDecision.repair("artifact_provenance", findings)
    return GateDecision.allow(
        "artifact_provenance",
        evidence={
            "artifact_ref": str(provenance.get("artifact_ref") or provenance.get("path") or ""),
            "tool_name": str(provenance.get("tool_name") or ""),
            "operation_id": str(provenance.get("operation_id") or ""),
            "run_id": provenance_run_id,
            "build_output_hash": str(provenance.get("build_output_hash") or ""),
        },
    )


# LLM: artifact_provenance_from_archive builds provenance from structured archive records.
# 函数用途: 在 closeout 报告里为产物附加本 run 的工具来源；只读 archive_tool_calls 机器字段。
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
    for record in archive_tool_calls:
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
            return provenance
        old_run_match = old_run_match or provenance
    if old_run_match:
        return {**old_run_match, "created_by_current_run": False, "code": "ARTIFACT_PROVENANCE_RUN_MISMATCH"}
    return {"ok": False, "code": "ARTIFACT_PROVENANCE_MISSING"}


# LLM: _hash_chain_findings verifies declared build/source hashes against current files.
# 函数用途: 本 run 写过只能证明来源存在，hash 链才能证明最终产物来自最新 source/checkpoint。
def _hash_chain_findings(item: dict[str, Any], provenance: dict[str, Any]) -> list[GateFinding]:
    findings: list[GateFinding] = []
    source_hashes = provenance.get("source_artifact_hashes")
    if isinstance(source_hashes, dict):
        findings.extend(_source_hash_findings(source_hashes))
    artifact_ref = str(provenance.get("artifact_ref") or provenance.get("path") or item.get("path") or "").strip()
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


# LLM: _provenance_from_record copies operation facts from a tool archive row.
# 函数用途: 把 runtime_gate evidence 和 tool/refs 合并成产物 provenance，不读工具输出正文。
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
        return {"ok": False, "code": "ARTIFACT_TOOL_GATE_MISSING"}
    run_id = str(record.get("run_id") or "").strip()
    tool_name = str(evidence.get("tool_name") or record.get("tool") or "").strip()
    return {
        "ok": True,
        "artifact_ref": str(artifact_path),
        "run_id": run_id,
        "task_id": str(record.get("task_id") or ""),
        "tool_name": tool_name,
        "operation_id": str(evidence.get("operation_id") or record.get("operation_id") or ""),
        "idempotency_key": str(evidence.get("idempotency_key") or record.get("idempotency_key") or ""),
        "call_id": str(record.get("call_id") or record.get("id") or ""),
        "created_by_current_run": bool(current_run_id and run_id == current_run_id),
    }


# LLM: _record_targets_artifact compares structured refs and parameters with the artifact path.
# 函数用途: 从 parameters/tool_result_refs 中找写入路径，支持文件产物和目录产物的子路径匹配。
def _record_targets_artifact(record: dict[str, Any], artifact_path: Path, workspace_root: Path) -> bool:
    return any(_path_matches_artifact(path, artifact_path) for path in _record_paths(record, workspace_root))


# LLM: _record_materialized_artifact treats post-write validation failures as valid provenance if the file exists.
# 函数用途: 写工具可能已产生文件但随后因完整性门返回 ok=false；产物来源仍应记录为本 run 的写入事实。
def _record_materialized_artifact(record: dict[str, Any], artifact_path: Path, workspace_root: Path) -> bool:
    return any(path.exists() and _path_matches_artifact(path, artifact_path) for path in _record_paths(record, workspace_root))


# LLM: _record_paths extracts path-like refs from archive rows without parsing prose.
# 函数用途: 支持 write_file and tool 和工具 result refs 的结构化路径字段。
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


# LLM: _paths_from_value normalizes scalar and nested target_path refs.
# 函数用途: 兼容 legacy session target_path 的 raw/resolved 对象形态。
def _paths_from_value(value: object, workspace_root: Path) -> list[Path]:
    if isinstance(value, dict):
        candidates = [value.get("resolved"), value.get("raw"), value.get("path"), value.get("artifact_ref")]
    else:
        candidates = [value]
    return [path for candidate in candidates if (path := _resolved_path(candidate, workspace_root)) is not None]


# LLM: _resolved_path bounds relative refs to the current workspace.
# 函数用途: 将产物和工具路径统一成 resolve 后路径；无法解析时返回 None。
def _resolved_path(value: object, workspace_root: Path) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    return path.resolve(strict=False) if path.is_absolute() else (workspace_root / path).resolve(strict=False)


# LLM: _path_matches_artifact supports exact file and contained directory matches.
# 函数用途: 目录型产物允许子文件写入作为来源，文件型产物必须路径相同。
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


# LLM: _mapping returns a dict only for structured payloads.
# 函数用途: 防止 helper 对字符串输出做自然语言解析。
def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


__all__ = ["artifact_provenance_from_archive", "evaluate_artifact_provenance_gate"]
