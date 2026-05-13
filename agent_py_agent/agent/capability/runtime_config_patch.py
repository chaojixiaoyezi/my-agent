# LLM: Runtime config patching is the safe write lane for capability_config self-healing.
# 模块用途: 校验并应用 capability_config 结构化补丁，集中处理 allowlist、审计和通知。

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


# LLM: PatchResultBuildRequest keeps private result construction bundle-shaped for guardrails.
# 类用途: 收拢补丁结果构造字段，避免 helper 函数重新出现散参数。
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


# LLM: PatchPlan carries validated patch decisions before optional file mutation.
# 类用途: 保存补丁计划中的安全改动、人工建议和阻断字段。
@dataclass(frozen=True)
class PatchPlan:
    safe_changes: dict[str, object]
    suggestions: list[dict[str, object]]
    blocked: list[str]


# LLM: PatchDecisionRequest groups mutable plan buckets for one patch decision.
# 类用途: 让单字段校验 helper 走 bundle 入参，避免散参数和重复上下文传递。
@dataclass(frozen=True)
class PatchDecisionRequest:
    config: CapabilityConfig
    known: set[str]
    safe_changes: dict[str, object]
    suggestions: list[dict[str, object]]
    blocked: list[str]


# LLM: EarlyPatchResultRequest bundles pre-write result context.
# 类用途: 封装 dry-run、blocked 和 no-op 分支需要的配置上下文。
@dataclass(frozen=True)
class EarlyPatchResultRequest:
    patch_request: CapabilityConfigPatchRequest
    before: str
    current_config: CapabilityConfig
    plan: PatchPlan


# LLM: apply_capability_config_patch is the only product path that writes capability_config safely.
# 函数用途: 校验字段、版本和风险等级；只自动应用安全字段，并写审计和通知记录。
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


# LLM: _missing_file_result returns a non-throwing tool-friendly error.
# 函数用途: 配置文件缺失时返回稳定结果，避免模型工具调用直接抛异常。
def _missing_file_result() -> CapabilityConfigPatchResult:
    return _patch_result(
        PatchResultBuildRequest(False, False, "missing", "missing", CapabilityConfig(), message="config_file_missing")
    )


# LLM: _version_conflict_result protects user or peer-agent edits from overwrite.
# 函数用途: expected_version 不匹配时生成拒绝结果；匹配或未传时返回 None。
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


# LLM: _patch_plan separates safe auto changes, manual suggestions, and invalid requests.
# 函数用途: 按字段 allowlist 和字段类型拆分补丁，避免危险开关被自动写入。
def _patch_plan(config: CapabilityConfig, patches: list[CapabilityConfigPatch]) -> PatchPlan:
    safe_changes: dict[str, object] = {}
    suggestions: list[dict[str, object]] = []
    blocked: list[str] = []
    known = {item.name for item in fields(CapabilityConfig)}
    request = PatchDecisionRequest(config, known, safe_changes, suggestions, blocked)
    for patch in patches:
        _add_patch_decision(request, patch)
    return PatchPlan(safe_changes=safe_changes, suggestions=suggestions, blocked=blocked)


# LLM: _add_patch_decision keeps one-field validation out of the main loop.
# 函数用途: 校验单个 patch 并追加到 safe/suggestion/blocked 三类之一。
def _add_patch_decision(request: PatchDecisionRequest, patch: CapabilityConfigPatch) -> None:
    field_name = str(patch.field or "").strip()
    if field_name not in request.known:
        request.blocked.append(field_name or "<empty>")
        return
    if field_name in _MANUAL_ONLY_FIELDS:
        request.suggestions.append(_manual_suggestion(patch))
        return
    _add_safe_change(request, patch, field_name)


# LLM: _add_safe_change coerces one allowed field and records real no-op filtered changes.
# 函数用途: 对 allowlist 字段做类型转换；值未变化时不写文件。
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


# LLM: _early_patch_result handles blocked, dry-run, and no-op paths before file mutation.
# 函数用途: 根据补丁计划返回无需写文件的结果；需要写入时返回 None。
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


# LLM: _blocked_patch_result rejects malformed or unknown fields without partial writes.
# 函数用途: 生成字段阻断结果，保留 manual suggestions 供模型/用户参考。
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


# LLM: _apply_safe_changes performs the actual narrow YAML rewrite and reload.
# 函数用途: 写入安全字段、重新加载配置，并返回 applied 结果。
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


# LLM: _manual_suggestion records risky config changes without applying them.
# 函数用途: 把需要人工确认的配置改动转成建议对象，工具可直接展示给模型和用户。
def _manual_suggestion(patch: CapabilityConfigPatch) -> dict[str, object]:
    return {
        "field": patch.field,
        "value": patch.value,
        "reason": "manual_approval_required",
        "note": patch.reason or "该字段会改变全局行为，当前只给建议，不自动写入。",
    }


# LLM: _coerce_field_value converts model-provided values to the dataclass field type.
# 函数用途: 按当前配置字段类型解析布尔和整数，坏值抛出 ValueError 交给上层阻止写入。
def _coerce_field_value(config: CapabilityConfig, field_name: str, raw: object) -> object:
    current = getattr(config, field_name)
    if isinstance(current, bool):
        return _coerce_bool(raw)
    if isinstance(current, int):
        return int(raw)
    return raw


# LLM: _coerce_bool accepts common YAML/CLI spellings for boolean values.
# 函数用途: 解析 true/false、yes/no、1/0 等布尔输入，无法识别时抛错。
def _coerce_bool(raw: object) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in {"true", "yes", "on", "1"}:
        return True
    if text in {"false", "no", "off", "0"}:
        return False
    raise ValueError(f"invalid bool: {raw!r}")


# LLM: _patch_result centralizes result construction so blocked branches stay compact.
# 函数用途: 生成 CapabilityConfigPatchResult，保持默认列表和路径字段一致。
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
