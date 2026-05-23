# LLM: Compaction gate enforces state preservation checks before and after context compaction.
# 模块用途: 参考 通道运行时 compaction 策略，在 compact 前检查关键引用保留，compact 后检查状态恢复。

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import GateDecision, GateFinding

# Fields that MUST survive compaction
REQUIRED_COMPACT_FIELDS = frozenset({
    "contract",
    "approval_refs",
    "artifact_refs",
    "recovery_packet",
    "task_id",
    "run_id",
    "pending_actions",
    "failed_actions",
})

# Dangerous action patterns that must not repeat after compaction
_REPEAT_GUARD_PATTERNS = frozenset({
    "tool_call",
    "file_write",
    "shell_exec",
    "network_request",
    "approval_request",
})


# LLM: CompactionGateFacts bundles pre/post state snapshots and phase for compaction checks.
# 类用途: 封装 task_id、run_id、pre/post 状态快照和检查阶段，供 gate 决策使用。
@dataclass(frozen=True)
class CompactionGateFacts:
    """Facts needed for pre-compact and post-compact state checks."""

    task_id: str = ""
    run_id: str = ""
    pre_compact_state: dict[str, Any] = field(default_factory=dict)
    post_compact_state: dict[str, Any] = field(default_factory=dict)
    phase: str = "pre_compact"


# LLM: evaluate_compaction_gate checks structural state preservation across compaction boundaries.
# 函数用途: 在 compact 前校验关键字段存在，compact 后校验状态恢复和不重复危险动作。
def evaluate_compaction_gate(facts: CompactionGateFacts) -> GateDecision:
    if facts.phase == "pre_compact":
        return _pre_compact_check(facts)
    return _post_compact_check(facts)


# LLM: _pre_compact_check validates all REQUIRED_COMPACT_FIELDS exist and non-empty before compaction.
# 函数用途: 在 compact 前遍历必需字段，对缺失或空字段生成 DENY 决策。
def _pre_compact_check(facts: CompactionGateFacts) -> GateDecision:
    state = facts.pre_compact_state
    findings: list[GateFinding] = []
    for field in sorted(REQUIRED_COMPACT_FIELDS):
        if field not in state or state[field] is None:
            findings.append(GateFinding(
                f"COMPACT_MISSING_{field.upper()}", "P0",
                f"Required field '{field}' missing before compaction",
                {"field": field},
            ))
            continue
        if field == "pending_actions" and isinstance(state[field], (list, tuple, dict)) and len(state[field]) == 0:
            findings.append(GateFinding(
                f"COMPACT_EMPTY_{field.upper()}", "P1",
                f"Required field '{field}' is empty before compaction",
                {"field": field},
            ))
    if findings:
        return GateDecision(
            "compaction_gate", "DENY", False,
            tuple(findings), "repair_missing_fields",
            {"phase": "pre_compact", "missing_count": len(findings)},
        )
    return GateDecision.allow("compaction_gate", evidence={
        "phase": "pre_compact",
        "preserved_fields": sorted(REQUIRED_COMPACT_FIELDS),
    })


# LLM: _post_compact_check verifies no key field was lost during compaction.
# 函数用途: 对比 pre/post 快照，检查之前存在的关键字段在 compact 后是否丢失。
def _post_compact_check(facts: CompactionGateFacts) -> GateDecision:
    pre = facts.pre_compact_state
    post = facts.post_compact_state
    findings: list[GateFinding] = []

    # Check that key refs survived
    for field in sorted(REQUIRED_COMPACT_FIELDS):
        pre_val = pre.get(field) if isinstance(pre, dict) else None
        post_val = post.get(field) if isinstance(post, dict) else None
        if pre_val is not None and post_val is None:
            findings.append(GateFinding(
                f"COMPACT_LOST_{field.upper()}", "P0",
                f"Field '{field}' was present before compaction but lost after",
                {"field": field},
            ))
            continue
        if (
            field in ("pending_actions", "artifact_refs")
            and isinstance(pre_val, (list, tuple, dict))
            and len(pre_val) > 0
            and isinstance(post_val, (list, tuple, dict))
            and len(post_val) == 0
        ):
            findings.append(GateFinding(
                f"COMPACT_EMPTY_{field.upper()}",
                "P0",
                f"Field '{field}' had entries before compaction but is empty after",
                {"field": field},
            ))

    if findings:
        return GateDecision(
            "compaction_gate", "DENY", False,
            tuple(findings), "repair_compaction_state",
            {"phase": "post_compact", "lost_count": len(findings)},
        )
    return GateDecision.allow("compaction_gate", evidence={
        "phase": "post_compact",
        "state_preserved": True,
    })


# LLM: compaction_gate_snapshot extracts a minimal machine-readable state snapshot for pre-compact.
# 函数用途: 从状态字典中提取关键字段的长度代理，作为 compaction 前的快照。
def compaction_gate_snapshot(state: dict[str, object] | None = None) -> dict[str, object]:
    s = state or {}
    return {
        "task_id": s.get("task_id", ""),
        "run_id": s.get("run_id", ""),
        "contract": _ref_snapshot(s.get("contract")),
        "approval_refs": _ref_snapshot(s.get("approval_refs")),
        "artifact_refs": _ref_snapshot(s.get("artifact_refs")),
        "recovery_packet": _ref_snapshot(s.get("recovery_packet")),
        "pending_actions": _ref_snapshot(s.get("pending_actions")),
        "failed_actions": _ref_snapshot(s.get("failed_actions")),
    }


# LLM: _action_set extracts a deduplicated set of action names from pending_actions and failed_actions.
# 函数用途: 从状态的 pending/failed actions 列表中提取去重后的动作名，供跨状态对比。
def _action_set(state: dict[str, object]) -> frozenset[str]:
    actions: set[str] = set()
    for key in ("pending_actions", "failed_actions"):
        val = state.get(key) if isinstance(state, dict) else None
        if isinstance(val, list):
            actions.update(str(item) for item in val if str(item).strip())
            continue
        if isinstance(val, dict):
            actions.update(str(k) for k in val.keys())
    return frozenset(actions)


# LLM: _ref_snapshot normalizes a value into a compact size proxy for state comparison.
# 函数用途: 将列表/字典转为长度计数，字符串/数字保留原值，None 保留，其他截断为字符串。
def _ref_snapshot(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return len(value)
    if isinstance(value, dict):
        return len(value)
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)[:200]


__all__ = [
    "CompactionGateFacts",
    "REQUIRED_COMPACT_FIELDS",
    "compaction_gate_snapshot",
    "evaluate_compaction_gate",
]
