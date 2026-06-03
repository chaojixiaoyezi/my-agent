
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contract_validation_recovery import recovery_for_findings


@dataclass(frozen=True)
class RuntimeConfigValidation:
    ok: bool
    error_codes: tuple[str, ...]
    findings: tuple[dict[str, str], ...]
    recovery: dict[str, object] | None = None


def validate_runtime_config(config: dict[str, Any]) -> RuntimeConfigValidation:
    findings: list[dict[str, str]] = []
    _validate_required_text(config, "workspace_root", "CONFIG_WORKSPACE_ROOT_MISSING", findings)
    _validate_required_text(config, "artifact_dir", "CONFIG_ARTIFACT_DIR_MISSING", findings)
    _validate_positive_number(config, "tool_timeout", "CONFIG_TOOL_TIMEOUT", findings)
    _validate_positive_integer(config, "max_steps", "CONFIG_MAX_STEPS", findings)
    _validate_dangerous_combinations(config, findings)
    return RuntimeConfigValidation(
        ok=not findings,
        error_codes=tuple(dict.fromkeys(item["code"] for item in findings)),
        findings=tuple(findings),
        recovery=recovery_for_findings("runtime_config", findings),
    )


def _validate_required_text(
    config: dict[str, Any],
    field: str,
    missing_code: str,
    findings: list[dict[str, str]],
) -> None:
    if str(config.get(field) or "").strip():
        return
    findings.append(_finding(missing_code, field, "required"))


def _validate_positive_number(
    config: dict[str, Any],
    field: str,
    code_prefix: str,
    findings: list[dict[str, str]],
) -> None:
    if field not in config or config.get(field) is None:
        findings.append(_finding(f"{code_prefix}_MISSING", field, "required"))
        return
    value = config.get(field)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        findings.append(_finding(f"{code_prefix}_INVALID", field, "positive_number_required"))


def _validate_positive_integer(
    config: dict[str, Any],
    field: str,
    code_prefix: str,
    findings: list[dict[str, str]],
) -> None:
    if field not in config or config.get(field) is None:
        findings.append(_finding(f"{code_prefix}_MISSING", field, "required"))
        return
    value = config.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        findings.append(_finding(f"{code_prefix}_INVALID", field, "positive_integer_required"))


def _validate_dangerous_combinations(config: dict[str, Any], findings: list[dict[str, str]]) -> None:
    root = str(config.get("workspace_root") or "").strip()
    if root and Path(root).expanduser() == Path("/"):
        findings.append(_finding("CONFIG_WORKSPACE_ROOT_DANGEROUS", "workspace_root", "root_workspace_forbidden"))
    _validate_artifact_dir_boundary(config, findings)
    if bool(config.get("allow_shell")):
        findings.append(_finding("CONFIG_ALLOW_SHELL_ENABLED", "allow_shell", "shell_requires_explicit_review"))
    if bool(config.get("allow_dangerous_actions")) and config.get("approval_required") is False:
        findings.append(
            _finding(
                "CONFIG_DANGEROUS_ACTIONS_WITHOUT_APPROVAL",
                "approval_required",
                "dangerous_actions_need_approval",
            )
        )


def _validate_artifact_dir_boundary(config: dict[str, Any], findings: list[dict[str, str]]) -> None:
    workspace = _path_or_none(config.get("workspace_root"))
    artifact_dir = _path_or_none(config.get("artifact_dir"))
    if workspace is None or artifact_dir is None:
        return
    resolved = artifact_dir if artifact_dir.is_absolute() else workspace / artifact_dir
    if _path_inside(resolved, workspace):
        return
    findings.append(_finding("CONFIG_ARTIFACT_DIR_OUTSIDE_WORKSPACE", "artifact_dir", "artifact_dir_must_stay_in_workspace"))


def _path_inside(path: Path, base: Path) -> bool:
    try:
        path.expanduser().resolve(strict=False).relative_to(base.expanduser().resolve(strict=False))
        return True
    except ValueError:
        return False


def _path_or_none(value: object) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    return Path(text).expanduser()


def _finding(code: str, field: str, detail: str) -> dict[str, str]:
    return {"code": code, "field": field, "detail": detail}


__all__ = ["RuntimeConfigValidation", "validate_runtime_config"]
