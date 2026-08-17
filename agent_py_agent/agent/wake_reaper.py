"""wake_intent bounded reaper（#233 生产场景定稿 seq2481/2484① + 2490/2492 深化）。

回收执行者——防「僵尸卡单」：claimed + 租约过期但无人 accept 的 intent。
与 dispatcher claim 分离（独立函数/合同，不塞进 claim）；单机内由 Gateway
主循环每轮限量调用（全局 budget），重启后必执行；以后可提为独立 worker。

结构化裁决（全结构化，禁自动重放；seq2492② 不静默 skipped）：
- 多 active dispatched（异常）→ fail-closed 整体 reconciliation。
- active dispatch 无 attempt_id 且作用域无未完成 canonical attempt
  （attempt 关联窗口，seq2490①）→ 无已知副作用 → 原子回收
  （release_wake_intent_and_dispatch 单事务：dispatch 终态化 + intent 回
  pending，seq2493① 崩溃窗口闭合）。
- 作用域存在未完成 attempt / dispatch 已带 attempt_id → reconciliation
  （dispatch 标 reconciliation + intent 保持 claimed + last_error_ref），
  **禁止自动重放**——未知副作用必须由对账流程显式接管。
- 无 active dispatched（历史终态 dispatch / 旧路径 claimed 无 dispatch）→
  裁决：作用域无未完成 attempt 且可安全终态化 → 仅 intent 回 pending；
  否则标 reconciliation_required 并计数告警（不静默 skipped）。
"""

from __future__ import annotations

import logging
import time

_LOGGER = logging.getLogger(__name__)

#: 副作用未知时的 reconciliation 标记（保持 claimed，等人接管）。
_RECONCILIATION_REF = "reaper:execution_in_progress_unknown_effect"
#: 无 active dispatch 但无法确认安全的标记。
_RECONCILIATION_REQUIRED_REF = "reaper:reconciliation_required_no_clean_terminal"


def _unfinished_in_scope(repo: object, row: dict) -> bool:
    """作用域（owner/task/run）存在未完成 canonical attempt（关联窗口检查）。"""
    try:
        return bool(
            repo.has_unfinished_attempt_for_scope(
                str(row.get("owner_id") or ""),
                str(row.get("task_id") or ""),
                str(row.get("run_id") or ""),
            )
        )
    except Exception:  # noqa: BLE001 查不到按未知处理（fail-closed → reconciliation）
        return True


def reap_expired_claimed_intents(
    repo: object,
    *,
    now: float | None = None,
    limit: int = 20,
) -> dict[str, int]:
    """回收一批租约过期的 claimed intent。

    返回 {released, reconciled, reconciled_required, skipped, errors}
    结构化计数（审计/观测）。单 intent 异常不阻断整批。
    """
    now = time.time() if now is None else now
    counts = {
        "released": 0,
        "reconciled": 0,
        "reconciled_required": 0,
        "skipped": 0,
        "errors": 0,
    }
    try:
        stale = repo.stale_claimed_wake_intents(limit=limit, now=now)
    except Exception:  # noqa: BLE001 reaper 查询异常不阻断主循环
        counts["errors"] += 1
        return counts
    for row in stale:
        intent_id = str(row.get("intent_id") or "")
        claim_token = str(row.get("claim_token") or "")
        lease_owner = str(row.get("lease_owner") or "")
        try:
            generation = int(row.get("claim_generation") or -1)
        except (TypeError, ValueError):
            generation = -1
        try:
            dispatches = repo.wake_dispatches_for_intent(intent_id)
        except Exception:  # noqa: BLE001 单 intent 异常不阻断
            counts["errors"] += 1
            continue
        active = [d for d in dispatches if str(d.get("status") or "") == "dispatched"]
        if len(active) > 1:
            # 多 active dispatched（异常）→ fail-closed 整体 reconciliation，
            # 绝不取 active[0] 猜（seq2492②）。
            counts["reconciled"] += _mark_reconciliation(
                repo, intent_id, claim_token, generation, now, counts
            )
            continue
        if active:
            dispatch = active[0]
            # attempt 已回写或作用域存在未完成 attempt（含 acceptance 未回写
            # 窗口）→ reconciliation，禁自动重放。
            if str(dispatch.get("attempt_id") or "") or _unfinished_in_scope(repo, row):
                counts["reconciled"] += _mark_reconciliation(
                    repo, intent_id, claim_token, generation, now, counts
                )
                continue
            # 无 attempt + 作用域无未完成 attempt → 无已知副作用 → 原子回收
            try:
                result = repo.release_wake_intent_and_dispatch(
                    intent_id,
                    dispatch_id=str(dispatch.get("dispatch_id") or ""),
                    claim_token=claim_token,
                    expected_generation=generation,
                    lease_owner=lease_owner,
                    now=now,
                )
                if result.get("released"):
                    counts["released"] += 1
                else:
                    counts["skipped"] += 1  # lease/CAS 校验失败（已被接管）
            except Exception:  # noqa: BLE001
                counts["errors"] += 1
            continue
        # 无 active dispatched：历史终态 dispatch（released/failed/...）或旧路径
        # claimed 无 dispatch → 结构化裁决（seq2492②，不静默 skipped）。
        if _unfinished_in_scope(repo, row):
            counts["reconciled_required"] += _mark_reconciliation_required(
                repo, intent_id, claim_token, generation, now
            )
            continue
        try:
            result = repo.release_wake_intent_lease(
                intent_id,
                claim_token=claim_token,
                expected_generation=generation,
                now=now,
            )
            if result.get("released"):
                counts["released"] += 1
            else:
                counts["skipped"] += 1
        except Exception:  # noqa: BLE001
            counts["errors"] += 1
    return counts


def _mark_reconciliation(repo, intent_id, claim_token, generation, now, counts) -> int:
    """dispatch 标 reconciliation + intent 保持 claimed + last_error_ref。"""
    try:
        dispatches = repo.wake_dispatches_for_intent(intent_id)
        for d in dispatches:
            if str(d.get("status") or "") == "dispatched":
                repo.mark_wake_dispatch_reconciliation(
                    str(d.get("dispatch_id") or ""),
                    error_ref=_RECONCILIATION_REF,
                    claim_token=claim_token,
                    expected_generation=generation,
                    lease_owner=str(d.get("lease_owner") or ""),
                    now=now,
                )
        repo.mark_wake_intent_lease_expired(
            intent_id,
            error_ref=_RECONCILIATION_REF,
            claim_token=claim_token,
            expected_generation=generation,
            now=now,
        )
        return 1
    except Exception:  # noqa: BLE001
        counts["errors"] += 1
        return 0


def _mark_reconciliation_required(repo, intent_id, claim_token, generation, now) -> int:
    """无 active dispatch 但作用域有未完成 attempt → reconciliation_required。"""
    try:
        repo.mark_wake_intent_lease_expired(
            intent_id,
            error_ref=_RECONCILIATION_REQUIRED_REF,
            claim_token=claim_token,
            expected_generation=generation,
            now=now,
        )
        return 1
    except Exception:  # noqa: BLE001
        return 0


__all__ = ["reap_expired_claimed_intents"]
