# LLM: C9 的 owner 级历史 unknown 恢复只处理本 owner 中 thread_id 为空、current attempt=unknown 的根/子代理；
#   历史 TaskRun 即使已有关闭记录也不隐藏，是否 unknown 只以 current attempt 权威状态为准。
#   owner_unknown_attempts 是只读投影，不读 title/goal/会话正文；写入口先用 BEGIN IMMEDIATE 复核完整目标集合和确认码，
#   再逐条调用主链唯一 _recover_unknown_attempt_conn。每条 attempt_recovered 事件带 recovery_target、owner_history 来源和确认码；
#   批次完成事件保存结构化计数，供同一确认命令幂等读回。不得据时间自动处置，也不得跨 owner。
#   改动须同步 test_owner_unknown_recovery.py、gateway_parts/turn_recovery_control.py 和 Gateway 模块文档。
# 模块用途: 为管理员提供不挂会话线程的历史未知执行轮只读清单，以及经目标集合确认后的 owner 级批量恢复。
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .child_recovery import CHILD_RECOVERY_TARGET, ROOT_RECOVERY_TARGET
from .operations import (
    ATTEMPT_EFFECT_DISPOSITIONS,
    ATTEMPT_STATUS_UNKNOWN,
    OP_CLAIMED,
    OP_EXECUTING,
    OP_UNKNOWN,
)
from .repository import _recover_unknown_attempt_conn

OWNER_RECOVERY_SOURCE = "owner_history"
OWNER_RECOVERY_BATCH_TARGET = "owner_unknown_batch"
_BATCH_EVENT_TYPE = "owner_recovery.completed"
# 确认码取结构化目标集合摘要的前 12 位，与插件预览确认的长度一致。
_CONFIRMATION_CODE_LENGTH_CHARS = 12


# LLM: 只承载展示和确认所需的结构化列；target_id 优先用 run_id，旧空 run_id 才回退 agent_run_id。
# 类用途: 描述一条不挂会话线程、当前执行轮结果未知的 owner 历史恢复目标。
@dataclass(frozen=True)
class OwnerRecoveryTarget:
    target_id: str
    agent_run_id: str
    attempt_id: str
    task_run_id: str
    task_id: str
    role: str
    agent_kind: str
    started_at: float
    unsettled_operation_count: int


# LLM: 写请求固定 owner、处置和确认码，避免在事务内重新读取命令正文或调用者身份。
# 类用途: 表示一次管理员已经明确确认的 owner 历史恢复请求。
@dataclass(frozen=True)
class OwnerRecoveryRequest:
    owner_id: str
    disposition: str
    confirmation_code: str


# LLM: 批次把请求和锁内冻结的完整目标集合绑在一起，既降低函数参数数量，也避免循环中误用另一份候选快照。
# 类用途: 保存一次 owner 恢复写事务内已经复核过的批次事实。
@dataclass(frozen=True)
class _OwnerRecoveryBatch:
    request: OwnerRecoveryRequest
    targets: tuple[OwnerRecoveryTarget, ...]


# LLM: 空 owner 身份不能降级成全库扫描；查询只读同一个 owner 的空 thread/current unknown 结构化事实。
# 函数用途: 列出管理员 owner 下没有会话线程归属的历史未知根代理和子代理。
def owner_unknown_attempts(repository: Any, owner_id: str) -> list[OwnerRecoveryTarget]:
    normalized = str(owner_id or "").strip()
    if not normalized:
        return []
    with repository._runtime_connection() as conn:
        return _owner_targets_conn(conn, normalized)


# LLM: 摘要绑定 owner、处置以及排序后的 target_id+attempt_id 集合；角色、文案和时间不参与机器授权。
# 函数用途: 为一次 owner 历史恢复预览生成 12 位确认码；目标换代、增删或处置变化都会换码。
def owner_recovery_confirmation(
    owner_id: str,
    disposition: str,
    targets: list[OwnerRecoveryTarget],
) -> str:
    facts = {
        "schema": "owner-unknown-recovery-confirmation.v1",
        "owner_id": str(owner_id or ""),
        "effect_disposition": str(disposition or ""),
        "targets": sorted((target.target_id, target.attempt_id) for target in targets),
    }
    encoded = json.dumps(facts, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:_CONFIRMATION_CODE_LENGTH_CHARS]


# LLM: 唯一 owner 写入口：先拿 SQLite 写锁，再读已完成批次实现重送幂等；首次确认必须与锁内当前集合摘要一致。
#   集合不一致不写任何行；一致时每条都走共享 unknown→recovered CAS，最终追加一条结构化批次回执事件。
# 函数用途: 按管理员确认码批量处置当前 owner 的无会话历史 unknown 执行轮。
def recover_owner_unknown_attempts(repository: Any, request: OwnerRecoveryRequest) -> dict[str, Any]:
    refusal = _request_refusal(request)
    if refusal:
        return _refused(refusal)
    with repository.transaction() as conn:
        conn.execute("BEGIN IMMEDIATE")
        completed = _completed_outcome(conn, request)
        if completed is not None:
            return completed
        targets = _owner_targets_conn(conn, request.owner_id)
        expected = owner_recovery_confirmation(request.owner_id, request.disposition, targets)
        if not targets or request.confirmation_code != expected:
            return _refused("target_set_changed", len(targets))
        batch = _OwnerRecoveryBatch(request, tuple(targets))
        outcome = _apply_targets(repository, conn, batch)
        _append_batch_event(repository, conn, batch, outcome)
        return outcome


# LLM: 请求字段来自已解析命令和可信 owner；这里仍做最终拒绝式校验，防止内部调用绕过入口。
# 函数用途: 返回 owner 写请求的第一条结构化拒绝原因，空串表示可继续事务复核。
def _request_refusal(request: OwnerRecoveryRequest) -> str:
    if not str(request.owner_id or "").strip():
        return "owner_missing"
    if request.disposition not in ATTEMPT_EFFECT_DISPOSITIONS:
        return "invalid_effect_disposition"
    code = str(request.confirmation_code or "")
    valid_code = len(code) == _CONFIRMATION_CODE_LENGTH_CHARS and all(
        character in "0123456789abcdef" for character in code
    )
    return "" if valid_code else "invalid_confirmation_code"


# LLM: SQL 只取身份、角色、时间和未确认操作计数；owner/空 thread/current unknown 三个结构化条件缺一不可。
#   不用 TaskRun closed_at 隐藏历史 unknown：树账状态不替代当前 attempt 的权威恢复状态。
# 函数用途: 在现有连接上投影 owner 历史 unknown 目标，供只读查看和写事务复核共用。
def _owner_targets_conn(conn: Any, owner_id: str) -> list[OwnerRecoveryTarget]:
    rows = conn.execute(
        "SELECT CASE WHEN ar.run_id!='' THEN ar.run_id ELSE ar.agent_run_id END AS target_id, "
        "ar.agent_run_id, ar.current_attempt_id, tr.task_run_id, tr.task_id, ar.role, "
        "CASE WHEN ar.parent_agent_run_id='' THEN 'root' ELSE 'child' END AS agent_kind, aa.started_at, "
        "(SELECT COUNT(*) FROM tool_operations op WHERE op.attempt_id=aa.attempt_id "
        "AND (op.status IN (?, ?) OR (op.status=? AND op.handler_started_at>0))) AS unsettled_count "
        "FROM agent_runs ar JOIN agent_attempts aa ON aa.attempt_id=ar.current_attempt_id "
        "JOIN task_runs tr ON tr.task_run_id=ar.task_run_id JOIN tasks t ON t.task_id=tr.task_id "
        "WHERE t.owner_id=? AND t.thread_id='' AND aa.status=? "
        "ORDER BY aa.started_at, ar.agent_run_id",
        (OP_EXECUTING, OP_UNKNOWN, OP_CLAIMED, owner_id, ATTEMPT_STATUS_UNKNOWN),
    ).fetchall()
    return [_target_from_row(row) for row in rows]


# LLM: SQLite 行到冻结目标的唯一转换点；数值只做无损标量转换，不读取 JSON 或正文列。
# 函数用途: 把一行 owner 恢复查询结果转换成只读目标对象。
def _target_from_row(row: Any) -> OwnerRecoveryTarget:
    return OwnerRecoveryTarget(
        target_id=str(row["target_id"] or ""), agent_run_id=str(row["agent_run_id"] or ""),
        attempt_id=str(row["current_attempt_id"] or ""), task_run_id=str(row["task_run_id"] or ""),
        task_id=str(row["task_id"] or ""), role=str(row["role"] or ""),
        agent_kind=str(row["agent_kind"] or ""), started_at=float(row["started_at"] or 0),
        unsettled_operation_count=int(row["unsettled_count"] or 0),
    )


# LLM: 每个目标都调用共享 CAS；按原因计数而不把异常正文写进回执。事件字段标明根/子目标与 owner_history 来源。
# 函数用途: 在同一锁定事务里逐条恢复冻结目标，并生成结构化成功/跳过统计。
def _apply_targets(
    repository: Any,
    conn: Any,
    batch: _OwnerRecoveryBatch,
) -> dict[str, Any]:
    reasons: dict[str, int] = {}
    success_count = 0
    for target in batch.targets:
        result = _recover_owner_target(repository, conn, batch, target)
        if result.get("recovered"):
            success_count += 1
            continue
        reason = str(result.get("reason") or "recovery_conflict")
        reasons[reason] = reasons.get(reason, 0) + 1
    return _outcome(batch.request, (len(batch.targets), success_count), reasons, idempotent=False)


# LLM: 单目标事件只增加 owner 来源事实，不改变共享 CAS 的状态机、锁释放或 run 修复语义。
# 函数用途: 恢复 owner 批次中的一条根或子代理执行轮。
def _recover_owner_target(
    repository: Any,
    conn: Any,
    batch: _OwnerRecoveryBatch,
    target: OwnerRecoveryTarget,
) -> dict[str, Any]:
    request = batch.request
    recovery_target = ROOT_RECOVERY_TARGET if target.agent_kind == "root" else CHILD_RECOVERY_TARGET
    return _recover_unknown_attempt_conn(
        repository, conn, attempt_id=target.attempt_id, operator="conversation-control:/recover-owner",
        effect_disposition=request.disposition, reason="管理员用 /recover owner 显式确认历史处置",
        event_facts={
            "recovery_target": recovery_target, "recovery_source": OWNER_RECOVERY_SOURCE,
            "owner_id": request.owner_id, "owner_recovery_confirmation_code": request.confirmation_code,
            "owner_recovery_target_id": target.target_id,
            "owner_recovery_target_count": len(batch.targets),
            "task_run_id": target.task_run_id,
        },
    )


# LLM: 批次事件保存第一次执行的准确计数和原因码；重复确认只读此回执，不再次调用任何 CAS。
# 函数用途: 追加 owner 恢复批次的幂等回执事件。
def _append_batch_event(
    repository: Any,
    conn: Any,
    batch: _OwnerRecoveryBatch,
    outcome: dict[str, Any],
) -> None:
    request, targets = batch.request, batch.targets
    first = targets[0]
    repository._append_event_conn(
        conn, event_type=_BATCH_EVENT_TYPE, attempt_id=first.attempt_id, agent_run_id=first.agent_run_id,
        payload={
            **outcome, "schema_version": "owner-unknown-recovery-outcome.v1",
            "recovery_target": OWNER_RECOVERY_BATCH_TARGET, "recovery_source": OWNER_RECOVERY_SOURCE,
            "owner_id": request.owner_id, "effect_disposition": request.disposition,
            "target_ids": [target.target_id for target in targets],
        },
    )


# LLM: 用确认码先缩小事件查询，再逐条解析并核对 owner/处置/来源；LIKE 不是授权判据，JSON 精确字段才是。
# 函数用途: 读取已完成的同一 owner 恢复批次，支持重复确认幂等返回。
def _completed_outcome(conn: Any, request: OwnerRecoveryRequest) -> dict[str, Any] | None:
    rows = conn.execute(
        "SELECT payload_json FROM runtime_events WHERE event_type=? AND payload_json LIKE ? ORDER BY seq DESC LIMIT 8",
        (_BATCH_EVENT_TYPE, f'%"confirmation_code": "{request.confirmation_code}"%'),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if _same_completed_request(payload, request):
            return _outcome(
                request,
                (int(payload.get("target_count") or 0), int(payload.get("success_count") or 0)),
                dict(payload.get("reason_counts") or {}), idempotent=True,
            )
    return None


# LLM: 幂等命中必须同时核对来源、owner、处置和确认码，任一字段缺失都不能复用旧事件。
# 函数用途: 判断一条批次事件是否正是当前确认命令的既有结果。
def _same_completed_request(payload: object, request: OwnerRecoveryRequest) -> bool:
    return isinstance(payload, dict) and (
        payload.get("recovery_source") == OWNER_RECOVERY_SOURCE
        and payload.get("owner_id") == request.owner_id
        and payload.get("effect_disposition") == request.disposition
        and payload.get("confirmation_code") == request.confirmation_code
    )


# LLM: 对外结果只保留计数、原因码、确认码和幂等标记，不带任务正文、路径或底层异常。
# 函数用途: 生成 owner 恢复写入口的结构化结果字典。
def _outcome(
    request: OwnerRecoveryRequest,
    counts: tuple[int, int],
    reasons: dict[str, int],
    *,
    idempotent: bool,
) -> dict[str, Any]:
    target_count, success_count = counts
    skipped_count = max(0, target_count - success_count)
    return {
        "recovered": skipped_count == 0 and target_count > 0,
        "target_count": target_count, "success_count": success_count, "skipped_count": skipped_count,
        "reason_counts": reasons, "confirmation_code": request.confirmation_code, "idempotent": idempotent,
    }


# LLM: 拒绝结果也给出同一组计数字段，调用方无需解析中文消息判断写入是否发生。
# 函数用途: 生成没有写库时的结构化 owner 恢复拒绝结果。
def _refused(reason: str, target_count: int = 0) -> dict[str, Any]:
    return {
        "recovered": False, "reason": reason, "target_count": target_count,
        "success_count": 0, "skipped_count": target_count, "reason_counts": {reason: target_count or 1},
        "idempotent": False,
    }


__all__ = [
    "OWNER_RECOVERY_BATCH_TARGET",
    "OWNER_RECOVERY_SOURCE",
    "OwnerRecoveryRequest",
    "OwnerRecoveryTarget",
    "owner_recovery_confirmation",
    "owner_unknown_attempts",
    "recover_owner_unknown_attempts",
]
