# LLM: 工具证据在候选合并前及正式写前逐项回查原verifier；模型自填成功状态不构成证据。
# 模块用途: 拦住失败、未知或身份不完整的工具事实来源。
"""工具事实证据的统一终态门；在候选合并前及正式晋升前分别使用。"""
from __future__ import annotations


# LLM: 必须逐条回查宿主verifier；声明为空与任一失败都有稳定拒绝码，不采信引用内自填的终态。
# 函数用途: 核实工具事实和所有声明产物的生产调用成功状态。
def tool_evidence_failure(subject: object, verifier: object) -> str:
    if subject.origin == "tool_verified" and not subject.source_tool_refs:
        return "TOOL_EVIDENCE_MISSING"
    # 用户明说也不能赋予附带的失败工具引用成功资格；所有容器执行同一门。
    refs = [*subject.source_tool_refs, *subject.source_artifact_refs]
    if any(ref.get("kind") == "unresolved_tool" for ref in refs):
        return "TOOL_EVIDENCE_MISSING"
    for ref in refs:
        if not verified_tool_reference(ref, verifier):
            return "TOOL_EVIDENCE_NOT_SUCCEEDED"
    return ""


# LLM: 统一准入和拒绝约束使用相同成功门，None核验器永不授权工具或产物。
# 函数用途: 回查单条工具引用是否有宿主确认的成功终态。
def verified_tool_reference(ref, verifier) -> bool:
    verified = verifier.verify(ref) if verifier is not None else None
    return bool(verified is not None and verified.successful and verified.terminal_status == "succeeded"
                and verified.effect_outcome not in {"unknown", "not_started"})


# LLM: 合法批次保留原observe_many入口；有坏证据时交给仓库同一锁内隔离处理，绝不由候选ID重新判断该操作。
# 函数用途: 拒绝坏证据操作而不连坐合法候选。
def observe_qualified(candidates: object, verifier: object, observations: list) -> list:
    failures = [tool_evidence_failure(item, verifier) for item in observations]
    if not any(failures):
        return candidates.observe_many(observations)
    return candidates.observe_evidence_batch(observations, failures)


# LLM: 调用方持quota与候选锁；先冻结拒绝观察的版本约束，再合并合法观察；拒绝来源不能贡献晋升资格。
# 函数用途: 生成一次原子候选写入及原序逐操作回执，保留后续拒绝和混合批次中的旧版本责任。
def assemble_evidence_batch(rows: list, observations: list, failures: list[str]) -> tuple[list, list]:
    from .candidates import merge_candidate_observations

    rows = list(rows)
    rejected = {}
    for index, (item, failure) in enumerate(zip(observations, failures, strict=True)):
        if not failure:
            continue
        _, prospective = merge_candidate_observations([], [item])
        candidate = prospective[0]
        _retain_rejected_binding(rows, candidate, failure)
        rejected[index] = {"candidate_id": candidate.candidate_id, "promoted": False,
            "status": "blocked_missing_evidence", "reason_code": failure, "promotion_ref": "",
            "scope_type": item.scope.scope_type, "scope_key": item.scope.scope_key}
    accepted = [item for item, failure in zip(observations, failures, strict=True) if not failure]
    rows, committed = merge_candidate_observations(rows, accepted) if accepted else (rows, [])
    observed = iter(committed)
    results = [rejected[index] if failure else next(observed) for index, failure in enumerate(failures)]
    return rows, results


# LLM: 既有行只累积规范身份键及版本，不合并失败引用/正文/学习资格/审批态；新ID才保留blocked草稿。
# 函数用途: 让每次拒绝留下版本约束，同时不污染合法候选，也不复活删除隐私。
def _retain_rejected_binding(rows: list, candidate: object, failure: str) -> None:
    from dataclasses import replace

    from .candidate_models import utc_now_iso
    from .candidate_target_binding import binding_history

    for index, current in enumerate(rows):
        if current.candidate_id != candidate.candidate_id:
            continue
        history = [*binding_history(current), *binding_history(candidate)]
        merged = replace(current, target_evidence_bindings=history)
        rows[index] = replace(current, target_evidence_bindings=binding_history(merged))
        return
    now = utc_now_iso()
    event = dict(status="blocked_missing_evidence", actor="evidence_gate", note=failure, at=now)
    rows.append(replace(candidate, status="blocked_missing_evidence", reviewer="evidence_gate",
        review_note=failure, updated_at=now, status_history=[*candidate.status_history, event]))
