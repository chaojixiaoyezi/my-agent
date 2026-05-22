# LLM: Artifact provenance gate ties accepted artifacts to current-run tool evidence.
# 模块用途: 从 archive_tool_calls 里的结构化工具记录提取产物来源，防止旧文件或口头声明冒充本轮交付。

from __future__ import annotations

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
            ("ARTIFACT_PROVENANCE_REF_MISSING", provenance.get("artifact_ref") or provenance.get("path")),
        )
        if not str(value or "").strip()
    ]
    if provenance.get("created_by_current_run") is not True:
        findings.append(GateFinding("ARTIFACT_PROVENANCE_NOT_CURRENT_RUN"))
    if findings:
        return GateDecision.repair("artifact_provenance", findings)
    return GateDecision.allow(
        "artifact_provenance",
        evidence={
            "artifact_ref": str(provenance.get("artifact_ref") or provenance.get("path") or ""),
            "tool_name": str(provenance.get("tool_name") or ""),
            "operation_id": str(provenance.get("operation_id") or ""),
            "run_id": provenance_run_id,
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
        if not isinstance(record, dict) or record.get("ok") is not True:
            continue
        if not _record_targets_artifact(record, artifact_path, workspace_root):
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
    if not isinstance(evidence, dict) or runtime_gate.get("allowed") is not True:
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


# LLM: _record_paths extracts path-like refs from archive rows without parsing prose.
# 函数用途: 支持 write_file、builder、file_write_session 和工具 result refs 的结构化路径字段。
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
# 函数用途: 兼容 file_write_session target_path 的 raw/resolved 对象形态。
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
