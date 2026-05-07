# LLM: Compact apply id helpers; keep ids deterministic where possible and path-safe everywhere.
# 模块用途: 生成 memory compact apply 的 plan_id、apply_id、scope hash、候选计数和风险等级。

from __future__ import annotations

"""id helpers for non-destructive compact apply records."""

import hashlib
import json
from typing import Any


# LLM: compact_apply_plan_id must stay deterministic for the same scope and source summary.
# 函数用途: 根据 compact plan 的范围和源摘要生成可复现 plan id，供手动 resume 指定。
def compact_apply_plan_id(plan: dict[str, Any]) -> str:
    return "plan-" + _sha256_json(_identity_payload(plan))[:16]


# LLM: compact_apply_id names one concrete apply attempt while preserving its source plan id.
# 函数用途: 根据 plan id 和时间生成文件名安全的 apply id。
def compact_apply_id(plan_id: str, now: str) -> str:
    return "apply-" + plan_id.removeprefix("plan-") + "-" + _safe_time_segment(now)


# LLM: compact_scope_hash gives reviewers a short stable fingerprint for the compact scope.
# 函数用途: 对 compact scope 生成短 hash，方便 ledger 和报告对照范围是否一致。
def compact_scope_hash(plan: dict[str, Any]) -> str:
    return _sha256_json({"workspace_root": plan["workspace_root"], "scope": plan["scope"]})[:16]


# LLM: compact_candidate_counts keeps source counts machine-readable without opening every source file.
# 函数用途: 汇总 dry-run plan 中 archive、snapshot 和 token ledger 的候选数量。
def compact_candidate_counts(plan: dict[str, Any]) -> dict[str, int]:
    return {
        "archive_records": int(plan["archive"]["record_count"]),
        "archive_files": int(plan["archive"]["file_count"]),
        "snapshot_files": int(plan["snapshots"]["file_count"]),
        "token_ledgers": int(plan["tokens"]["ledger_count"]),
    }


# LLM: compact_risk_level keeps the first apply flow conservative without model judgment.
# 函数用途: 根据 dry-run risks 和 invalid source counts 给出确定性风险等级。
def compact_risk_level(plan: dict[str, Any]) -> str:
    if plan["risks"]:
        return "medium"
    if int(plan["snapshots"].get("invalid_count", 0) or 0) or int(plan["tokens"].get("invalid_count", 0) or 0):
        return "medium"
    return "low"


# LLM: _identity_payload is the canonical plan id input; changing it is a schema decision.
# 函数用途: 选择影响 plan_id 的稳定字段，避免时间戳影响同一 plan 的复现。
def _identity_payload(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "workspace_root": plan["workspace_root"],
        "scope": plan["scope"],
        "candidate_counts": compact_candidate_counts(plan),
        "estimated_compactable_bytes": plan["estimated_compactable_bytes"],
        "risks": list(plan["risks"]),
    }


# LLM: _sha256_json provides stable ids without adding dependencies.
# 函数用途: 对 JSON 可序列化对象做稳定 sha256，供 plan/apply id 使用。
def _sha256_json(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# LLM: _safe_time_segment keeps apply ids path-safe and stable across platforms.
# 函数用途: 把 ISO 时间转成文件名安全片段。
def _safe_time_segment(value: str) -> str:
    return value.replace(":", "").replace("-", "").replace("+", "Z")


__all__ = [
    "compact_apply_id",
    "compact_apply_plan_id",
    "compact_candidate_counts",
    "compact_risk_level",
    "compact_scope_hash",
]
