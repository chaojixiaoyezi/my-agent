"""compact 上下文 bundle：匹配判定与 refs 读取的权威实现（原 compact_context_bundle/ 包并入）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..common.json_io import read_json_object_report
from ..runtime_errors import runtime_error_report

_SCOPE_FIELDS = ("request_id", "run_id", "task_id")


def compact_context_bundle_match(plan_scope: dict[str, Any], bundle: dict[str, Any], *, explicit: bool) -> dict[str, Any]:
    ref = str(bundle.get("ref", "") or "")
    if not ref:
        return _report("no_context_bundle", explicit=explicit, ref="", mismatches=[])
    if not bundle.get("loaded"):
        return _report(str(bundle.get("error") or "context_bundle_not_loaded"), explicit=explicit, ref=ref, mismatches=[])
    plan = _scope_values(plan_scope)
    candidate = _scope_values(bundle.get("scope", {}) if isinstance(bundle.get("scope"), dict) else {})
    comparable = {key: value for key, value in plan.items() if value}
    if not comparable:
        return _report("explicit_no_plan_scope" if explicit else "unchecked_no_plan_scope", explicit=explicit, ref=ref, mismatches=[])
    mismatches = [
        {"field": key, "expected": value, "actual": candidate.get(key, "")}
        for key, value in comparable.items()
        if candidate.get(key, "") and candidate.get(key, "") != value
    ]
    if mismatches:
        return _report("explicit_scope_mismatch" if explicit else "scope_mismatch", explicit=explicit, ref=ref, mismatches=mismatches)
    return _report("matched", explicit=explicit, ref=ref, mismatches=[])


def context_bundle_match_allows_attach(report: dict[str, Any]) -> bool:
    return str(report.get("status") or "") in {"matched", "explicit_scope_mismatch", "explicit_no_plan_scope", "unchecked_no_plan_scope"}


def _scope_values(scope: dict[str, Any]) -> dict[str, str]:
    return {field: str(scope.get(field) or "").strip() for field in _SCOPE_FIELDS}


def _report(status: str, *, explicit: bool, ref: str, mismatches: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "status": status,
        "explicit_ref": bool(explicit),
        "candidate_ref": ref,
        "mismatches": mismatches,
    }


__all__ = ["compact_context_bundle_match", "context_bundle_match_allows_attach"]


def load_main_context_bundle_ref(workspace: str | Path, ref: str | Path | None) -> dict[str, Any]:
    if not ref:
        return _empty_payload("")
    path = _resolve_ref(workspace, ref)
    if not path.exists():
        return {
            **_empty_payload(str(path)),
            "error": "missing_context_bundle",
            "load_error": _load_error(path, FileNotFoundError(str(path))),
        }
    report = read_json_object_report(path, context="compact_context_bundle.main_context_bundle")
    if report.load_error:
        return {**_empty_payload(str(path)), "error": _bundle_error_code(report.load_error), "load_error": report.load_error}
    return _summary_payload(path, report.payload)


def main_context_bundle_source_refs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    ref = str(payload.get("ref", "") or "")
    if not ref:
        return []
    scope = payload.get("scope", {}) if isinstance(payload.get("scope"), dict) else {}
    load_error = payload.get("load_error")
    row = {
        "path": ref,
        "loaded": bool(payload.get("loaded")),
        "schema": str(payload.get("schema", "") or ""),
        "request_id": str(scope.get("request_id", "") or ""),
        "run_id": str(scope.get("run_id", "") or ""),
        "task_id": str(scope.get("task_id", "") or ""),
        "size_bytes": _safe_size(Path(ref)),
    }
    if isinstance(load_error, dict) and load_error:
        row["load_error"] = load_error
    return [row]


def compact_context_bundle_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "ref": str(payload.get("ref", "") or ""),
        "loaded": bool(payload.get("loaded")),
        "schema": str(payload.get("schema", "") or ""),
        "scope": dict(payload.get("scope", {}) if isinstance(payload.get("scope"), dict) else {}),
        "workspace_refs": dict(
            payload.get("workspace_refs", {}) if isinstance(payload.get("workspace_refs"), dict) else {}
        ),
        "task": dict(payload.get("task", {}) if isinstance(payload.get("task"), dict) else {}),
        "error": str(payload.get("error", "") or ""),
        "load_error": dict(payload.get("load_error", {}) if isinstance(payload.get("load_error"), dict) else {}),
    }


def main_context_bundle_recommended_paths(payload: dict[str, Any]) -> list[str]:
    ref = str(payload.get("ref", "") or "")
    return [ref] if ref else []


def _summary_payload(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "ref": str(path),
        "loaded": True,
        "schema": str(payload.get("schema", "") or ""),
        "version": int(payload.get("version", 0) or 0),
        "identity": _dict_section(payload, "identity"),
        "scope": _dict_section(payload, "scope"),
        "workspace_refs": _dict_section(payload, "workspace_refs"),
        "task": _dict_section(payload, "task"),
        "memory_refs": _dict_section(payload, "memory_refs"),
        "recovery_refs": _dict_section(payload, "recovery_refs"),
        "error": "",
        "load_error": {},
    }


def _empty_payload(ref: str) -> dict[str, Any]:
    return {
        "ref": ref,
        "loaded": False,
        "schema": "",
        "version": 0,
        "identity": {},
        "scope": {},
        "workspace_refs": {},
        "task": {},
        "memory_refs": {},
        "recovery_refs": {},
        "error": "",
        "load_error": {},
    }


def _dict_section(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key, {})
    return dict(value) if isinstance(value, dict) else {}


def _resolve_ref(workspace: str | Path, ref: str | Path) -> Path:
    path = Path(ref).expanduser()
    return path if path.is_absolute() else Path(workspace) / path


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _bundle_error_code(load_error: dict[str, object]) -> str:
    category = str(load_error.get("category") or "")
    if category == "data_parse":
        return "invalid_context_bundle"
    if category == "data_encoding":
        return "invalid_context_bundle_encoding"
    return "context_bundle_read_error"


def _load_error(path: Path, exc: BaseException) -> dict[str, Any]:
    report = runtime_error_report(exc, context="compact_context_bundle.main_context_bundle")
    report["path"] = str(path)
    return report


__all__ = [
    "compact_context_bundle_summary",
    "load_main_context_bundle_ref",
    "main_context_bundle_recommended_paths",
    "main_context_bundle_source_refs",
]
