"""wake_intent reconciler（#233 第 5 步）——低频审计 + 补写 intent 的安全网。

与 dispatcher 分离：reconciler **绝不调模型、绝不创建 attempt**（硬不变量 1/2，
spec §4/§6）。它只做两件事：
- **补写**：对「link active + ledger RUNNING + 权威非终态」（unfinished_task_ids
  判据）但缺非终态 intent 的合法未完成任务，经 producer `register_wake_intent`
  补一条 intent（走同一 dedup/授权门，多 gateway 同窗幂等）。
- **标记**：对「无 policy / 无 run / legacy_unbound」的任务 fail-closed 标台账
  （ORPHANED / RECONCILIATION_REQUIRED，带 reason/provenance），不补写——绝不
  从 tasks.status 推导唤醒资格（preserve step 0 止血：任务状态 ≠ 唤醒资格）。

扫描判据（全结构化，不做任何自然语言判断）：
1. `task_id ∈ unfinished_task_ids(owner_home)` = 权威「需驱动」集。
2. `repo.wake_intent_for_task(...)` 无 → 补写目标；有非终态 intent → already_present。
3. 授权门（硬不变量 §7）：continuation_policy+policy_generation 缺失 → 拒绝。
   - 无 async policy → 不补写，标台账（无 run → ORPHANED；有 run → RECONCILIATION_REQUIRED）。
   - policy 存在但 allowed_sources 与 backfill 优先级无交集 → RECONCILIATION_REQUIRED。
   - interactive-only 任务永不补写（事件驱动，禁周期扫描）。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from .owner_wake_discovery import unfinished_task_ids

_LOGGER = logging.getLogger(__name__)

#: 补写来源优先级：按 policy.allowed_sources 命中第一个。只允许 async 策略类来源。
_BACKFILL_SOURCE_PRIORITY = ("heartbeat", "cron", "interrupted_run_recovery")
#: 各来源的 wake_reason（受控枚举，spec §5）。
_REASON_FOR_SOURCE = {
    "heartbeat": "heartbeat_due",
    "cron": "cron_due",
    "interrupted_run_recovery": "interrupted_run_recovery",
}

#: dedup 时间窗（秒）：同窗多 gateway 同 owner/task → 同 source_event_id/due_window
#: → 同 dedup_key → create_wake_intent 幂等（existing=True），不重复写。
_RECONCILER_WINDOW_SECONDS = 600

#: 台账标记 ref 前缀。
_RECONCILER_REF_PREFIX = "reconciler:"


def reconcile_missing_wake_intents(
    repo: object,
    *,
    owner_id: str,
    owner_home: Path,
    now: float | None = None,
    limit: int = 20,
) -> dict[str, int]:
    """低频审计 + 补写 intent（安全网）。返回结构化计数。单任务异常不阻断整批。

    绝不含任何模型/attempt 入口：只 import repository/producer/discovery helper。
    """
    now = time.time() if now is None else now
    counts = {
        "backfilled": 0,
        "already_present": 0,
        "orphaned": 0,
        "reconciliation_required": 0,
        "skipped": 0,
        "errors": 0,
    }
    try:
        candidate_ids = unfinished_task_ids(Path(owner_home))
    except Exception:  # noqa: BLE001 判活源异常不阻断主循环
        counts["errors"] += 1
        return counts
    processed = 0
    for task_id in candidate_ids:
        if processed >= max(1, int(limit)):
            break
        processed += 1
        try:
            _reconcile_one(repo, owner_id=owner_id, task_id=task_id, now=now, counts=counts)
        except Exception:  # noqa: BLE001 单任务异常不阻断整批
            counts["errors"] += 1
    return counts


def _reconcile_one(repo, *, owner_id: str, task_id: str, now: float, counts: dict) -> None:
    """单任务裁决：已有 intent / 补写 / 标台账（fail-closed）。"""
    # 已有非终态 intent（pending/claimed/handed_off）→ 无需补写。
    existing = _wake_intent_for_task(repo, owner_id, task_id)
    if existing is not None:
        counts["already_present"] += 1
        return
    # 授权门：continuation_policy+policy_generation 缺失 → 拒绝（fail-closed）。
    policy = _current_async_policy(repo, owner_id)
    if policy is None:
        _mark_unauthorized(repo, owner_id, task_id, now, counts)
        return
    # source 授权：allowed_sources 与 backfill 优先级取交集；空交集 → 拒。
    source = _pick_backfill_source(policy)
    if source is None:
        _record_reconciliation_required(
            repo, owner_id, task_id, now,
            reason="authorization_gap_source_not_allowed",
            source="reconciler",
        )
        counts["reconciliation_required"] += 1
        return
    # 补写（走 producer 同一 dedup/授权门；幂等）。
    result = _backfill_intent(
        repo, owner_id=owner_id, task_id=task_id, source=source,
        policy=policy, now=now,
    )
    if result.get("created"):
        counts["backfilled"] += 1
    elif result.get("existing"):
        counts["already_present"] += 1
    else:
        counts["skipped"] += 1


def _wake_intent_for_task(repo, owner_id: str, task_id: str):
    try:
        return repo.wake_intent_for_task(owner_id, task_id)
    except AttributeError:
        return None  # 旧 repo 无该方法 → 按无 intent 处理（保守补写经 producer 校验）


def _current_async_policy(repo, owner_id: str):
    try:
        return repo.current_wake_policy(owner_id, "async")
    except AttributeError:
        return None


def _pick_backfill_source(policy: dict) -> str | None:
    allowed = str(policy.get("allowed_sources") or "").strip()
    allowed_set = {s.strip() for s in allowed.split(",") if s.strip()}
    for priority in _BACKFILL_SOURCE_PRIORITY:
        if priority in allowed_set:
            return priority
    return None


def _mark_unauthorized(repo, owner_id: str, task_id: str, now: float, counts: dict) -> None:
    """无 async policy → 不补写，标台账（无 run → ORPHANED；有 run → RECONCILIATION_REQUIRED）。"""
    has_run = _has_main_run(repo, task_id)
    if not has_run:
        _record_orphaned(
            repo, owner_id, task_id, now,
            reason="legacy_unbound_no_run_no_policy",
        )
        counts["orphaned"] += 1
        return
    _record_reconciliation_required(
        repo, owner_id, task_id, now,
        reason="no_policy_no_authorization",
    )
    counts["reconciliation_required"] += 1


def _has_main_run(repo, task_id: str) -> bool:
    try:
        return repo.main_agent_run_for_task(task_id) is not None
    except Exception:  # noqa: BLE001 查 run 失败按无 run 处理（不误补写）
        return False


def _backfill_intent(repo, *, owner_id: str, task_id: str, source: str,
                     policy: dict, now: float) -> dict[str, object]:
    """经 producer register_wake_intent 补写（同窗 dedup_key 稳定 → 幂等）。

    source_event_id = reconciler:{owner}:{task}:{window}，due_window 同窗——
    多 gateway 同 owner/task 同窗补写 → 同 dedup_key → existing=True，不重复。
    """
    from .wake_producer import register_wake_intent

    window = int(now // _RECONCILER_WINDOW_SECONDS)
    return register_wake_intent(
        repo,
        owner_id=owner_id,
        task_id=task_id,
        source=source,
        wake_reason=_REASON_FOR_SOURCE.get(source, "heartbeat_due"),
        continuation_policy="async",
        provider_scope_ref=str(policy.get("provider_scope_ref") or ""),
        policy_generation=int(policy.get("policy_generation") or 0),
        next_wake_at=now,
        source_event_id=f"{_RECONCILER_REF_PREFIX}{owner_id}:{task_id}:{window}",
        due_window=f"reconciler:{window}",
        provenance_ref=f"{_RECONCILER_REF_PREFIX}unfinished_task_ids:{task_id}",
        now=now,
    )


def _record_orphaned(repo, owner_id: str, task_id: str, now: float, *, reason: str) -> None:
    try:
        repo.upsert_legacy_migration(
            task_id=task_id, owner_id=owner_id,
            isolation_state="ORPHANED", reason=reason,
            last_seen=now, source="reconciler",
            provenance_ref=f"{_RECONCILER_REF_PREFIX}orphaned:{task_id}",
            migration_version="", now=now,
        )
    except Exception:  # noqa: BLE001 台账失败不影响主流程
        _LOGGER.warning("reconciler orphan ledger failed task=%s", task_id, exc_info=True)


def _record_reconciliation_required(repo, owner_id: str, task_id: str, now: float,
                                    *, reason: str, source: str = "reconciler") -> None:
    try:
        repo.upsert_legacy_migration(
            task_id=task_id, owner_id=owner_id,
            isolation_state="RECONCILIATION_REQUIRED", reason=reason,
            last_seen=now, source=source,
            provenance_ref=f"{_RECONCILER_REF_PREFIX}reconcile_required:{task_id}",
            migration_version="", now=now,
        )
    except Exception:  # noqa: BLE001 台账失败不影响主流程
        _LOGGER.warning("reconciler reconcile-required ledger failed task=%s", task_id, exc_info=True)


__all__ = ["reconcile_missing_wake_intents"]
