# LLM: Compact context-bundle matching prevents old compact scopes from binding the newest unrelated run card.
# 模块用途: 对比 compact plan scope 和 main context bundle scope，自动 latest 不匹配时拒绝绑定。

from __future__ import annotations

from typing import Any

_SCOPE_FIELDS = ("request_id", "run_id", "task_id")


# LLM: compact_context_bundle_match returns a machine-readable warning/allow decision.
# 函数用途: 检查 context bundle 是否属于当前 compact scope；显式指定时保留 ref 但记录错配。
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


# LLM: context_bundle_match_allows_attach centralizes attach policy for compact apply.
# 函数用途: 非显式 scope_mismatch 不绑定；显式错配、匹配和无 plan scope 保留引用但带报告。
def context_bundle_match_allows_attach(report: dict[str, Any]) -> bool:
    return str(report.get("status") or "") in {"matched", "explicit_scope_mismatch", "explicit_no_plan_scope", "unchecked_no_plan_scope"}


# LLM: _scope_values extracts only fields shared by plan scope and context bundle scope.
# 函数用途: 标准化 request/run/task 三个范围字段，避免比较 session_id 这类 bundle 没有的字段。
def _scope_values(scope: dict[str, Any]) -> dict[str, str]:
    return {field: str(scope.get(field) or "").strip() for field in _SCOPE_FIELDS}


# LLM: _report keeps match payload stable for CLI, metadata, and tests.
# 函数用途: 生成 context bundle 匹配报告，供 compact apply 输出和日志展示。
def _report(status: str, *, explicit: bool, ref: str, mismatches: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "status": status,
        "explicit_ref": bool(explicit),
        "candidate_ref": ref,
        "mismatches": mismatches,
        "reserved": {},
    }


__all__ = ["compact_context_bundle_match", "context_bundle_match_allows_attach"]
