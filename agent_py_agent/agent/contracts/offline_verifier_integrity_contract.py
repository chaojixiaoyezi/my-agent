# LLM: Offline verifier integrity contracts protect pass/fail checks from forged evidence.
# 模块用途: 校验证据内容、证据引用、ToolTrace 来源、新鲜度、超时和核心验收无 LLM 依赖。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# LLM: OfflineVerifierIntegrityValidation reports verifier-integrity findings.
# 类用途: 返回验收器完整性是否通过、错误码和结构化 finding。
@dataclass(frozen=True)
class OfflineVerifierIntegrityValidation:
    ok: bool
    error_codes: tuple[str, ...]
    findings: tuple[dict[str, object], ...]


# LLM: validate_verifier_integrity checks structured verifier facts and evidence refs.
# 函数用途: 防止标题堆字、伪造工具证据、缺失证据、过期证据、超时和 LLM 依赖。
def validate_verifier_integrity(facts: dict[str, Any]) -> OfflineVerifierIntegrityValidation:
    findings: list[dict[str, object]] = []
    _validate_section_content(facts, findings)
    _validate_evidence_claims(facts, findings)
    _validate_verifier_runtime(facts, findings)
    return OfflineVerifierIntegrityValidation(
        ok=not findings,
        error_codes=tuple(dict.fromkeys(_text(item.get("code")) for item in findings)),
        findings=tuple(findings),
    )


# LLM: _validate_section_content rejects explicit keyword-only section markers.
# 函数用途: report_sections[].content_hash=same_as_heading 表示只有标题/关键词，验收失败。
def _validate_section_content(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    for section in _dict_items(facts.get("report_sections")):
        if _text(section.get("content_hash")) == "same_as_heading":
            findings.append(_finding("VERIFIER_KEYWORD_ONLY_CONTENT", {"section": _text(section.get("name"))}))
            return


# LLM: _validate_evidence_claims binds claims to evidence store refs and tool trace rows.
# 函数用途: 证据声明必须引用存在的 evidence_ref，source_tool 必须有成功 ToolTrace。
def _validate_evidence_claims(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    store_refs = set(_string_tuple(facts.get("evidence_store_refs")))
    tool_names = _successful_tool_names(facts.get("tool_trace"))
    max_age = _max_evidence_age(facts)
    for claim in _dict_items(facts.get("evidence_claims")):
        _validate_one_claim_ref(claim, store_refs, findings)
        _validate_one_claim_tool(claim, tool_names, findings)
        _validate_one_claim_freshness(claim, max_age, findings)


# LLM: _validate_one_claim_ref checks evidence_ref presence in the structured evidence store.
# 函数用途: evidence_ref 不存在时返回 EVIDENCE_REF_MISSING。
def _validate_one_claim_ref(
    claim: dict[str, Any],
    store_refs: set[str],
    findings: list[dict[str, object]],
) -> None:
    ref = _text(claim.get("evidence_ref"))
    if ref and ref not in store_refs:
        findings.append(_finding("EVIDENCE_REF_MISSING", {"evidence_ref": ref}))


# LLM: _validate_one_claim_tool ensures source_tool is backed by ToolTrace.
# 函数用途: source_tool 不在成功工具调用列表中时返回 EVIDENCE_TOOL_TRACE_MISSING。
def _validate_one_claim_tool(
    claim: dict[str, Any],
    tool_names: set[str],
    findings: list[dict[str, object]],
) -> None:
    source_tool = _text(claim.get("source_tool"))
    if source_tool and source_tool not in tool_names:
        findings.append(_finding("EVIDENCE_TOOL_TRACE_MISSING", {"source_tool": source_tool}))


# LLM: _validate_one_claim_freshness enforces explicit evidence freshness budgets.
# 函数用途: age_minutes 超过 evidence_freshness.max_age_minutes 时返回 EVIDENCE_STALE。
def _validate_one_claim_freshness(
    claim: dict[str, Any],
    max_age: int,
    findings: list[dict[str, object]],
) -> None:
    if max_age <= 0:
        return
    if _optional_int(claim.get("age_minutes")) > max_age:
        findings.append(_finding("EVIDENCE_STALE", {"evidence_ref": _text(claim.get("evidence_ref"))}))


# LLM: _validate_verifier_runtime rejects unbounded or LLM-dependent core verifiers.
# 函数用途: duration_ms 超过 timeout_ms 或 requires_llm=true 都会生成 finding。
def _validate_verifier_runtime(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    verifier = facts.get("verifier")
    if not isinstance(verifier, dict):
        return
    timeout_ms = _optional_int(verifier.get("timeout_ms"))
    if timeout_ms > 0 and _optional_int(verifier.get("duration_ms")) > timeout_ms:
        findings.append(_finding("VERIFIER_TIMEOUT_EXCEEDED"))
    if verifier.get("requires_llm") is True:
        findings.append(_finding("CORE_VERIFIER_LLM_DEPENDENCY"))


# LLM: _successful_tool_names extracts successful tool names from ToolTrace facts.
# 函数用途: 只把 ok=true 或 success=true 的工具调用视为证据来源。
def _successful_tool_names(value: object) -> set[str]:
    tools: set[str] = set()
    for item in _dict_items(value):
        ok = item.get("ok") is True or item.get("success") is True
        tool = _text(item.get("tool"))
        if ok and tool:
            tools.add(tool)
    return tools


# LLM: _max_evidence_age reads the explicit freshness budget.
# 函数用途: 从 evidence_freshness.max_age_minutes 获取分钟预算。
def _max_evidence_age(facts: dict[str, Any]) -> int:
    freshness = facts.get("evidence_freshness")
    if not isinstance(freshness, dict):
        return 0
    return _optional_int(freshness.get("max_age_minutes"))


# LLM: _dict_items returns dict entries from structured arrays.
# 函数用途: 过滤非 dict 项，避免从字符串内容中猜事实。
def _dict_items(value: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, dict))


# LLM: _string_tuple normalizes explicit string collections.
# 函数用途: 把结构化数组规整成去空字符串元组。
def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set)):
        return ()
    return tuple(text for item in value for text in (_text(item),) if text)


# LLM: _optional_int reads numeric fields without inventing facts.
# 函数用途: 将显式数字转成 int，缺失或非法时返回 0。
def _optional_int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# LLM: _finding creates compact verifier findings.
# 函数用途: 统一生成 code 和可选结构字段。
def _finding(code: str, extra: dict[str, object] | None = None) -> dict[str, object]:
    return {"code": code, **(extra or {})}


# LLM: _text normalizes scalar fields for exact comparisons only.
# 函数用途: 将 None 或标量转成去空白字符串，不解析自然语言语义。
def _text(value: object) -> str:
    return str(value or "").strip()


__all__ = ["OfflineVerifierIntegrityValidation", "validate_verifier_integrity"]
