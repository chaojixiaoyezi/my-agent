"""wake_intent bounded reaper（#233 生产场景定稿 seq2481/2484①）。

回收执行者——防「僵尸卡单」：claimed + 租约过期但无人 accept 的 intent。
与 dispatcher claim 分离（独立函数/合同，不塞进 claim）；单机内由 Gateway
主循环每轮限量调用，重启后必执行；以后可提为独立 worker。

回收裁决（全结构化，禁自动重放）：
- dispatch 行 status='dispatched' 且 attempt_id 空（执行席从未建 attempt）→
  无已知副作用 → 先终态化 dispatch（release_wake_dispatch，seq2461 要求
  dispatch 先行终态化）再 CAS release intent 回 pending（下次 due 再派，
  覆盖「claim 后崩溃没派出去」）。
- dispatch 行已带 attempt_id（执行已开始/副作用可能已发生）→
  dispatch 标 reconciliation + intent 保持 claimed + last_error_ref，
  **禁止自动重放**——未知副作用必须由对账流程显式接管。
- 其他状态（无 dispatch 行/已 accepted 等）→ 不属于本回收范围，跳过
  （accept 后 intent 已 handed_off，由执行结果流程收尾）。
"""

from __future__ import annotations

import logging
import time

_LOGGER = logging.getLogger(__name__)

#: 副作用未知时的 reconciliation 标记（保持 claimed，等人接管）。
_RECONCILIATION_REF = "reaper:execution_in_progress_unknown_effect"


def reap_expired_claimed_intents(
    repo: object,
    *,
    now: float | None = None,
    limit: int = 20,
) -> dict[str, int]:
    """回收一批租约过期的 claimed intent。

    返回 {released, reconciled, skipped, errors} 结构化计数（审计/观测）。
    单 intent 异常不阻断整批（try/except 逐条）。
    """
    now = time.time() if now is None else now
    counts = {"released": 0, "reconciled": 0, "skipped": 0, "errors": 0}
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
        if not active:
            # 无 dispatched dispatch（已终态化/被接管）→ 非本回收范围
            counts["skipped"] += 1
            continue
        dispatch_id = str(active[0].get("dispatch_id") or "")
        # 执行席已建 attempt（执行已开始/副作用可能已发生）→ 绝不自动重放：
        # dispatch 标 reconciliation + intent 保持 claimed + last_error_ref
        if any(str(d.get("attempt_id") or "") for d in active):
            try:
                repo.mark_wake_dispatch_reconciliation(
                    dispatch_id,
                    error_ref=_RECONCILIATION_REF,
                    claim_token=claim_token,
                    expected_generation=generation,
                    lease_owner=lease_owner,
                    now=now,
                )
                repo.mark_wake_intent_lease_expired(
                    intent_id,
                    error_ref=_RECONCILIATION_REF,
                    claim_token=claim_token,
                    expected_generation=generation,
                    now=now,
                )
                counts["reconciled"] += 1
            except Exception:  # noqa: BLE001
                counts["errors"] += 1
            continue
        # 无 attempt（从未建过执行记录）→ 无已知副作用 → 先终态化 dispatch
        # （seq2461：存在 dispatched dispatch 时 release 禁止）再回 pending
        try:
            released = repo.release_wake_dispatch(
                dispatch_id,
                claim_token=claim_token,
                expected_generation=generation,
                lease_owner=lease_owner,
                now=now,
            )
            if not released.get("released"):
                counts["skipped"] += 1
                continue
            result = repo.release_wake_intent_lease(
                intent_id,
                claim_token=claim_token,
                expected_generation=generation,
                now=now,
            )
            if result.get("released"):
                counts["released"] += 1
            else:
                counts["skipped"] += 1  # 租约未过期/token 不匹配（已被接管）
        except Exception:  # noqa: BLE001
            counts["errors"] += 1
    return counts


__all__ = ["reap_expired_claimed_intents"]
