
from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path

from .config import CapabilityConfig, load_capability_config
from .runtime_config_models import (
    CapabilityConfigPatch,
    CapabilityConfigPatchRequest,
    CapabilityConfigPatchResult,
)
from .runtime_config_patch_io import (
    atomic_write_text,
    replace_yaml_fields,
    write_config_patch_audit,
    write_config_patch_notice,
)
from .runtime_config_reload import capability_config_version

_MANUAL_ONLY_FIELDS = frozenset({"enable_capability_routing"})
_SAFE_AUTO_FIELDS = frozenset(CapabilityConfig.__dataclass_fields__) - _MANUAL_ONLY_FIELDS


@dataclass(frozen=True)
class PatchResultBuildRequest:
    ok: bool
    applied: bool
    before: str
    after: str
    config: CapabilityConfig
    changed_fields: list[str] | None = None
    suggestions: list[dict[str, object]] | None = None
    blocked_fields: list[str] | None = None
    message: str = ""


@dataclass(frozen=True)
class PatchPlan:
    safe_changes: dict[str, object]
    suggestions: list[dict[str, object]]
    blocked: list[str]


@dataclass(frozen=True)
class PatchDecisionRequest:
    config: CapabilityConfig
    known: set[str]
    safe_changes: dict[str, object]
    suggestions: list[dict[str, object]]
    blocked: list[str]


@dataclass(frozen=True)
class EarlyPatchResultRequest:
    patch_request: CapabilityConfigPatchRequest
    before: str
    current_config: CapabilityConfig
    plan: PatchPlan


def apply_capability_config_patch(request: CapabilityConfigPatchRequest) -> CapabilityConfigPatchResult:
    path = Path(request.config_path)
    if not path.exists():
        return _missing_file_result()
    before = capability_config_version(path)
    current_config = load_capability_config(path)
    conflict = _version_conflict_result(request, before, current_config)
    if conflict is not None:
        return conflict
    plan = _patch_plan(current_config, request.patches)
    early = _early_patch_result(EarlyPatchResultRequest(request, before, current_config, plan))
    if early is not None:
        return early
    result = _apply_safe_changes(path, before, plan)
    write_config_patch_audit(request, result)
    write_config_patch_notice(request, result)
    return result


def _missing_file_result() -> CapabilityConfigPatchResult:
    return _patch_result(
        PatchResultBuildRequest(False, False, "missing", "missing", CapabilityConfig(), message="config_file_missing")
    )


def _version_conflict_result(
    request: CapabilityConfigPatchRequest,
    before: str,
    current_config: CapabilityConfig,
) -> CapabilityConfigPatchResult | None:
    if not request.expected_version or request.expected_version == before:
        return None
    return _patch_result(
        PatchResultBuildRequest(
            False,
            False,
            before,
            before,
            current_config,
            message="capability_config_version_mismatch",
        )
    )


def _patch_plan(config: CapabilityConfig, patches: list[CapabilityConfigPatch]) -> PatchPlan:
    safe_changes: dict[str, object] = {}
    suggestions: list[dict[str, object]] = []
    blocked: list[str] = []
    known = {item.name for item in fields(CapabilityConfig)}
    request = PatchDecisionRequest(config, known, safe_changes, suggestions, blocked)
    for patch in patches:
        _add_patch_decision(request, patch)
    return PatchPlan(safe_changes=safe_changes, suggestions=suggestions, blocked=blocked)


def _add_patch_decision(request: PatchDecisionRequest, patch: CapabilityConfigPatch) -> None:
    field_name = str(patch.field or "").strip()
    if field_name not in request.known:
        request.blocked.append(field_name or "<empty>")
        return
    if field_name in _MANUAL_ONLY_FIELDS:
        request.suggestions.append(_manual_suggestion(patch))
        return
    _add_safe_change(request, patch, field_name)


def _add_safe_change(request: PatchDecisionRequest, patch: CapabilityConfigPatch, field_name: str) -> None:
    if field_name not in _SAFE_AUTO_FIELDS:
        request.blocked.append(field_name)
        return
    try:
        value = _coerce_field_value(request.config, field_name, patch.value)
    except ValueError:
        request.blocked.append(field_name)
        return
    if getattr(request.config, field_name) != value:
        request.safe_changes[field_name] = value


def _early_patch_result(request: EarlyPatchResultRequest) -> CapabilityConfigPatchResult | None:
    if request.plan.blocked:
        return _blocked_patch_result(request.before, request.current_config, request.plan)
    if request.patch_request.apply and request.plan.safe_changes:
        return None
    return _patch_result(
        PatchResultBuildRequest(
            True,
            False,
            request.before,
            request.before,
            request.current_config,
            suggestions=request.plan.suggestions,
            message="dry_run" if not request.patch_request.apply else "no_auto_changes",
        )
    )


def _blocked_patch_result(
    before: str,
    current_config: CapabilityConfig,
    plan: PatchPlan,
) -> CapabilityConfigPatchResult:
    return _patch_result(
        PatchResultBuildRequest(
            False,
            False,
            before,
            before,
            current_config,
            suggestions=plan.suggestions,
            blocked_fields=plan.blocked,
            message="capability_config_patch_blocked",
        )
    )


def _apply_safe_changes(
    path: Path,
    before: str,
    plan: PatchPlan,
) -> CapabilityConfigPatchResult:
    text = path.read_text(encoding="utf-8")
    atomic_write_text(path, replace_yaml_fields(text, plan.safe_changes))
    after = capability_config_version(path)
    return _patch_result(
        PatchResultBuildRequest(
            True,
            True,
            before,
            after,
            load_capability_config(path),
            changed_fields=list(plan.safe_changes),
            suggestions=plan.suggestions,
            message="applied",
        )
    )


def _manual_suggestion(patch: CapabilityConfigPatch) -> dict[str, object]:
    return {
        "field": patch.field,
        "value": patch.value,
        "reason": "manual_approval_required",
        "note": patch.reason or "该字段会改变全局行为，当前只给建议，不自动写入。",
    }


def _coerce_field_value(config: CapabilityConfig, field_name: str, raw: object) -> object:
    current = getattr(config, field_name)
    if isinstance(current, bool):
        return _coerce_bool(raw)
    if isinstance(current, int):
        return int(raw)
    return raw


def _coerce_bool(raw: object) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in {"true", "1"}:
        return True
    if text in {"false", "0"}:
        return False
    raise ValueError(f"invalid bool: {raw!r}")


def _patch_result(request: PatchResultBuildRequest) -> CapabilityConfigPatchResult:
    return CapabilityConfigPatchResult(
        ok=request.ok,
        applied=request.applied,
        version_before=request.before,
        version_after=request.after,
        config=request.config,
        changed_fields=request.changed_fields or [],
        suggestions=request.suggestions or [],
        blocked_fields=request.blocked_fields or [],
        message=request.message,
    )
