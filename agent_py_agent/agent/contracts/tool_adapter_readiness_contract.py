# LLM: Tool-adapter readiness contracts keep real read-only and dry-run adapters safe before live use.
# 模块用途: 校验工具适配器是否声明 effect/schema/test coverage/dry-run/审批/幂等边界。

from __future__ import annotations

from typing import Any

from .offline_contract_report import (
    OfflineContractValidation,
    finding,
    string_tuple,
    text,
    validation_report,
)

READ_ONLY_REQUIRED_TESTS = ("success", "timeout", "auth_failure", "empty_result", "large_output", "secret_redaction")
SIDE_EFFECTS = {"mutating", "dangerous"}
VALID_EFFECTS = {"read_only", "mutating", "dangerous"}


# LLM: validate_tool_adapter_readiness checks adapter metadata before exposing tools to real runs.
# 函数用途: 对每个 adapter 的 effect/modes/schema/tests/approval/idempotency 做结构化验收。
def validate_tool_adapter_readiness(adapters: tuple[dict[str, Any], ...]) -> OfflineContractValidation:
    findings: list[dict[str, object]] = []
    for adapter in adapters:
        _validate_one_adapter(adapter, findings)
    return validation_report(findings)


# LLM: _validate_one_adapter dispatches adapter checks by effect and mode.
# 函数用途: 聚合单个工具适配器的 schema、测试覆盖、副作用和 dry-run 安全检查。
def _validate_one_adapter(adapter: dict[str, Any], findings: list[dict[str, object]]) -> None:
    effect = text(adapter.get("effect"))
    if effect not in VALID_EFFECTS:
        findings.append(finding("ADAPTER_EFFECT_MISSING", {"tool": text(adapter.get("tool"))}))
    if not text(adapter.get("result_schema_ref")):
        findings.append(finding("ADAPTER_RESULT_SCHEMA_MISSING", {"tool": text(adapter.get("tool"))}))
    if effect == "read_only":
        _validate_read_only_tests(adapter, findings)
    if "dry_run" in string_tuple(adapter.get("modes")) or effect in SIDE_EFFECTS:
        _validate_dry_run_mode_field(adapter, findings)
    if effect in SIDE_EFFECTS:
        _validate_side_effect_adapter(adapter, findings)
    if "dry_run" in string_tuple(adapter.get("modes")) or effect in SIDE_EFFECTS:
        _validate_dry_run_only(adapter, findings)


# LLM: _validate_read_only_tests requires fake coverage for common real adapter failures.
# 函数用途: 只读真实工具必须有成功、超时、鉴权失败、空结果、大输出和脱敏测试。
def _validate_read_only_tests(adapter: dict[str, Any], findings: list[dict[str, object]]) -> None:
    missing = tuple(name for name in READ_ONLY_REQUIRED_TESTS if name not in set(string_tuple(adapter.get("contract_tests"))))
    if missing:
        findings.append(finding("ADAPTER_READ_ONLY_TEST_COVERAGE_MISSING", {"tool": text(adapter.get("tool"))}))


# LLM: _validate_dry_run_mode_field requires explicit mode labels for dry-run results.
# 函数用途: dry-run 工具必须有 mode 字段，避免 dry-run 结果被当成真实执行。
def _validate_dry_run_mode_field(adapter: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if not text(adapter.get("mode_field")) and "dry_run" in string_tuple(adapter.get("modes")):
        findings.append(finding("ADAPTER_DRY_RUN_MODE_FIELD_MISSING", {"tool": text(adapter.get("tool"))}))


# LLM: _validate_dry_run_only blocks real-run exposure on adapters declared dry-run-only.
# 函数用途: dry_run_only=false 且 modes 包含 real_run 时返回专门 finding。
def _validate_dry_run_only(adapter: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if adapter.get("dry_run_only") is False and "real_run" in string_tuple(adapter.get("modes")):
        findings.append(finding("ADAPTER_DRY_RUN_ONLY_VIOLATED", {"tool": text(adapter.get("tool"))}))


# LLM: _validate_side_effect_adapter enforces approval and idempotency before mutating/dangerous tools.
# 函数用途: 可变更/高危工具真实执行必须审批，且所有副作用工具必须要求幂等键。
def _validate_side_effect_adapter(adapter: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if adapter.get("real_execution_enabled") is True and adapter.get("requires_approval") is not True:
        findings.append(finding("ADAPTER_REAL_RUN_NOT_APPROVED", {"tool": text(adapter.get("tool"))}))
    if adapter.get("idempotency_key_required") is not True:
        findings.append(finding("ADAPTER_SIDE_EFFECT_IDEMPOTENCY_MISSING", {"tool": text(adapter.get("tool"))}))


__all__ = ["READ_ONLY_REQUIRED_TESTS", "validate_tool_adapter_readiness"]
