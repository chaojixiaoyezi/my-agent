# LLM: Compact gate bridge connects memory compact apply/resume to the shared compaction gate.
# 模块用途: 从 compact metadata、restore refs 和 work_state 组装机器事实，避免 compact 门只停留在孤立单测。

from __future__ import annotations

from typing import Any

from ..contracts.gates.compaction_gate import CompactionGateFacts, evaluate_compaction_gate


# LLM: build_compaction_gate_state extracts the stable state that must survive compact/resume.
# 函数用途: 只读取结构化 compact/work_state 字段，生成 compaction_gate 的 pre/post 对比状态。
def build_compaction_gate_state(
    metadata: dict[str, Any],
    restore_refs: dict[str, Any],
    work_state: dict[str, Any],
) -> dict[str, Any]:
    scope = _dict_value(work_state.get("scope")) or _dict_value(metadata.get("scope"))
    refs = _dict_value(metadata.get("refs"))
    source_plan = _dict_value(metadata.get("source_plan"))
    work_state_restore_payload = _dict_value(work_state.get("restore_refs"))
    return {
        "task_id": _scope_id(scope, "task_id") or _scope_id(scope, "request_id") or str(work_state.get("apply_id") or ""),
        "run_id": _scope_id(scope, "run_id") or str(work_state.get("apply_id") or ""),
        "contract": _contract_state(work_state),
        "approval_refs": _dict_value(work_state.get("approval_refs")),
        "artifact_refs": _artifact_refs(work_state, restore_refs),
        "recovery_packet": _recovery_packet(refs, work_state_restore_payload, restore_refs),
        "pending_actions": _pending_actions(work_state, source_plan),
        "failed_actions": _failed_actions(work_state, source_plan),
    }


# LLM: evaluate_pre_compaction_state runs the gate before metadata becomes authoritative.
# 函数用途: 生成 pre_compact 裁决和可序列化状态快照，供 metadata 和后续 resume 使用。
def evaluate_pre_compaction_state(
    metadata: dict[str, Any],
    restore_refs: dict[str, Any],
    work_state: dict[str, Any],
) -> dict[str, Any]:
    state = build_compaction_gate_state(metadata, restore_refs, work_state)
    decision = evaluate_compaction_gate(
        CompactionGateFacts(
            task_id=str(state.get("task_id") or ""),
            run_id=str(state.get("run_id") or ""),
            pre_compact_state=state,
            phase="pre_compact",
        )
    )
    return {"pre": decision.to_dict(), "state_snapshot": state}


# LLM: evaluate_post_compaction_state compares resume state against the apply-time snapshot.
# 函数用途: 在 memory-resume 阶段确认 compact 后关键状态没有丢失。
def evaluate_post_compaction_state(metadata: dict[str, Any], artifacts: dict[str, Any]) -> dict[str, Any]:
    recorded = _dict_value(metadata.get("compaction_gate"))
    pre_state = _dict_value(recorded.get("state_snapshot"))
    if not pre_state:
        return {
            "present": False,
            "post": {
                "gate": "compaction_gate",
                "status": "SKIPPED",
                "allowed": True,
                "findings": [],
                "recommended_action": "continue",
                "evidence": {"reason": "missing_pre_compaction_snapshot"},
            },
        }
    work_state = _dict_value(artifacts.get("work_state"))
    restore_refs = _dict_value(artifacts.get("restore_refs"))
    post_state = build_compaction_gate_state(metadata, restore_refs, work_state)
    decision = evaluate_compaction_gate(
        CompactionGateFacts(
            task_id=str(pre_state.get("task_id") or ""),
            run_id=str(pre_state.get("run_id") or ""),
            pre_compact_state=pre_state,
            post_compact_state=post_state,
            phase="post_compact",
        )
    )
    return {"present": True, "post": decision.to_dict(), "state_snapshot": post_state}


# LLM: _contract_state keeps acceptance/constraints as structured contract signals.
# 函数用途: compact 不发明验收事实；只保留 work_state 已经记录的结构化合同字段。
def _contract_state(work_state: dict[str, Any]) -> dict[str, Any]:
    return {
        "acceptance": work_state.get("acceptance", []),
        "constraints": work_state.get("constraints", []),
        "latest_tests": work_state.get("latest_tests", []),
    }


def _artifact_refs(work_state: dict[str, Any], restore_refs: dict[str, Any]) -> list[dict[str, str]]:
    refs = work_state.get("artifact_refs")
    if isinstance(refs, list) and refs:
        return [dict(item) for item in refs if isinstance(item, dict)]
    return _source_ref_paths(_dict_value(restore_refs.get("source_refs")))


def _source_ref_paths(source_refs: dict[str, Any]) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    for group in ("archive_files", "snapshot_files", "token_ledgers"):
        refs.extend(_source_ref_group_paths(source_refs, group))
    return refs


def _source_ref_group_paths(source_refs: dict[str, Any], group: str) -> list[dict[str, str]]:
    items = source_refs.get(group, [])
    if not isinstance(items, list):
        return []
    return [{"path": str(item["path"]), "kind": group} for item in items if isinstance(item, dict) and item.get("path")]


def _recovery_packet(
    refs: dict[str, Any],
    work_state_restore_payload: dict[str, Any],
    restore_refs: dict[str, Any],
) -> dict[str, Any] | None:
    if not work_state_restore_payload:
        return None
    return {
        "apply_bundle": refs.get("apply_bundle", ""),
        "restore_refs": refs.get("restore_refs", ""),
        "work_state_snapshot": refs.get("work_state_snapshot", ""),
        "restore_source_count": _restore_source_count(restore_refs),
    }


def _pending_actions(work_state: dict[str, Any], source_plan: dict[str, Any]) -> list[str]:
    actions = _string_list(work_state.get("next_actions")) or _string_list(source_plan.get("recommended_actions"))
    return actions or ["manual_resume_review"]


def _failed_actions(work_state: dict[str, Any], source_plan: dict[str, Any]) -> list[str]:
    return _string_list(work_state.get("missing_fields")) + _string_list(source_plan.get("risks"))


def _restore_source_count(restore_payload: dict[str, Any]) -> int:
    source_refs = _dict_value(restore_payload.get("source_refs"))
    return sum(
        len(source_refs.get(group, []))
        for group in ("archive_files", "snapshot_files", "token_ledgers")
        if isinstance(source_refs.get(group), list)
    )


def _scope_id(scope: dict[str, Any], key: str) -> str:
    return str(scope.get(key) or "").strip()


def _dict_value(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


__all__ = [
    "build_compaction_gate_state",
    "evaluate_post_compaction_state",
    "evaluate_pre_compaction_state",
]
