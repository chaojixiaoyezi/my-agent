# LLM: Contract doctor validates machine-readable task contracts before any run starts.
# 模块用途: 检查合同 schema、字段类型、规则冲突、不可能完成项和版本迁移。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

CURRENT_VERSION = 2
DEFAULT_KNOWN_VERIFIERS = ("artifact_acceptance", "tool_trace", "approval_gate")
IMPOSSIBLE_MIN_SIZE = 100_000_000_000


# LLM: ContractDoctorReport is the stable machine result for contract linting.
# 类用途: 返回合同预检是否通过、错误码和结构化 finding。
@dataclass(frozen=True)
class ContractDoctorReport:
    ok: bool
    error_codes: tuple[str, ...]
    findings: tuple[dict[str, object], ...]


# LLM: lint_contract rejects malformed or conflicting structured contracts.
# 函数用途: 在任务启动前检查合同自身，不从自然语言说明里推断任何机器事实。
def lint_contract(
    contract: dict[str, Any],
    *,
    known_verifiers: tuple[str, ...] = DEFAULT_KNOWN_VERIFIERS,
) -> ContractDoctorReport:
    findings: list[dict[str, object]] = []
    normalized = _safe_migrate(contract, findings)
    _validate_schema(normalized, findings)
    _validate_field_types(normalized, findings)
    _validate_unknown_rules(normalized, set(known_verifiers), findings)
    _validate_conflicts(normalized, findings)
    _validate_impossible_artifacts(normalized, findings)
    return ContractDoctorReport(
        ok=not findings,
        error_codes=tuple(dict.fromkeys(_text(item.get("code")) for item in findings)),
        findings=tuple(findings),
    )


# LLM: migrate_contract converts supported old structured contracts to the current shape.
# 函数用途: 把 version=1 的 artifact_path 转为 version=2 artifacts.required.path。
def migrate_contract(contract: dict[str, Any]) -> dict[str, Any]:
    version = contract.get("version", CURRENT_VERSION)
    if version == CURRENT_VERSION:
        return dict(contract)
    if version != 1:
        return dict(contract)
    migrated = {key: value for key, value in contract.items() if key != "artifact_path"}
    artifact_path = _text(contract.get("artifact_path"))
    migrated["version"] = CURRENT_VERSION
    migrated["artifacts"] = {"required": [{"path": artifact_path}]} if artifact_path else {"required": []}
    return migrated


# LLM: _safe_migrate keeps linting total even when callers pass unsupported versions.
# 函数用途: 对未知版本生成 finding，然后继续做保守检查。
def _safe_migrate(contract: dict[str, Any], findings: list[dict[str, object]]) -> dict[str, Any]:
    version = contract.get("version", CURRENT_VERSION)
    if version not in (1, CURRENT_VERSION):
        findings.append(_finding("CONTRACT_VERSION_UNSUPPORTED", {"version": version}))
        return dict(contract)
    return migrate_contract(contract)


# LLM: _validate_schema rejects legacy fields that should have been migrated.
# 函数用途: version=2 中出现 artifact_path 或缺少 artifacts 对象时返回 schema finding。
def _validate_schema(contract: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if contract.get("version", CURRENT_VERSION) != CURRENT_VERSION:
        return
    artifacts = contract.get("artifacts")
    if "artifact_path" in contract or (artifacts is not None and not isinstance(artifacts, dict)):
        findings.append(_finding("CONTRACT_SCHEMA_INVALID"))


# LLM: _validate_field_types validates scalar and collection field shapes.
# 函数用途: 检查 max_steps、required_tools、forbidden_tools、rules 等结构化字段类型。
def _validate_field_types(contract: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if "max_steps" in contract and not isinstance(contract.get("max_steps"), int):
        findings.append(_finding("CONTRACT_FIELD_TYPE_INVALID", {"field": "max_steps"}))
    for field in ("required_tools", "forbidden_tools", "rules"):
        value = contract.get(field)
        if value is not None and not _is_string_sequence(value):
            findings.append(_finding("CONTRACT_FIELD_TYPE_INVALID", {"field": field}))


# LLM: _validate_unknown_rules ensures verifier names are explicit and supported.
# 函数用途: 对 rules 中不存在的 verifier 生成 UNKNOWN_VERIFIER。
def _validate_unknown_rules(
    contract: dict[str, Any],
    known_verifiers: set[str],
    findings: list[dict[str, object]],
) -> None:
    for rule in _string_tuple(contract.get("rules")):
        if rule not in known_verifiers:
            findings.append(_finding("UNKNOWN_VERIFIER", {"rule": rule}))


# LLM: _validate_conflicts rejects directly conflicting allow/deny tool requirements.
# 函数用途: required_tools 与 forbidden_tools 交集非空时返回 CONTRACT_RULE_CONFLICT。
def _validate_conflicts(contract: dict[str, Any], findings: list[dict[str, object]]) -> None:
    required = set(_string_tuple(contract.get("required_tools")))
    forbidden = set(_string_tuple(contract.get("forbidden_tools")))
    overlap = sorted(required & forbidden)
    if overlap:
        findings.append(_finding("CONTRACT_RULE_CONFLICT", {"tools": tuple(overlap)}))


# LLM: _validate_impossible_artifacts catches artifact requirements that cannot be satisfied.
# 函数用途: 检查过大 min_size 和 required_sections/forbidden_words 的直接冲突。
def _validate_impossible_artifacts(contract: dict[str, Any], findings: list[dict[str, object]]) -> None:
    for artifact in _required_artifacts(contract):
        min_size = _optional_int(artifact.get("min_size"))
        required_sections = set(_string_tuple(artifact.get("required_sections")))
        forbidden_words = set(_string_tuple(artifact.get("forbidden_words")))
        if min_size > IMPOSSIBLE_MIN_SIZE or required_sections & forbidden_words:
            findings.append(_finding("CONTRACT_IMPOSSIBLE", {"path": _text(artifact.get("path"))}))
            return


# LLM: _required_artifacts reads the current artifacts.required list only.
# 函数用途: 返回结构化产物要求，忽略非 dict 项以交给 schema/type finding 处理。
def _required_artifacts(contract: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    artifacts = contract.get("artifacts")
    if not isinstance(artifacts, dict):
        return ()
    required = artifacts.get("required")
    if not isinstance(required, (list, tuple)):
        return ()
    return tuple(item for item in required if isinstance(item, dict))


# LLM: _is_string_sequence checks collection fields without parsing embedded text.
# 函数用途: 判断字段是否是字符串数组。
def _is_string_sequence(value: object) -> bool:
    return isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value)


# LLM: _string_tuple normalizes explicit string collections.
# 函数用途: 把结构化数组规整成去空字符串元组。
def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set)):
        return ()
    return tuple(text for item in value for text in (_text(item),) if text)


# LLM: _optional_int reads numeric limits without inventing values.
# 函数用途: 将显式数字转成 int，缺失或非法时返回 0。
def _optional_int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# LLM: _finding creates compact doctor findings.
# 函数用途: 统一生成 code 和可选结构字段。
def _finding(code: str, extra: dict[str, object] | None = None) -> dict[str, object]:
    return {"code": code, **(extra or {})}


# LLM: _text normalizes scalar fields for exact comparisons only.
# 函数用途: 将 None 或标量转成去空白字符串，不解析自然语言语义。
def _text(value: object) -> str:
    return str(value or "").strip()


__all__ = ["ContractDoctorReport", "lint_contract", "migrate_contract"]
