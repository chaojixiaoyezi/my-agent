"""wake_intent producer 统一入口（#233 第 3/4 步）。

硬不变量（规格 §4）：producer 只写 intent/outbox，**禁调模型、禁建 attempt**
（硬不变量 1/2）。dispatcher 才是唯一 claim+attempt 入口。

- dedup_key 按规格 §3 稳定计算（effective_source_event_id = retry_event_id or
  source_event_id；不含 attempt_id / provider key）。
- fail-closed 显式拒绝（seq2427④）：source 不在受控枚举 / continuation_policy
  不在 {interactive,async} / provider_scope_ref 空 / policy_generation<0 →
  **不写 intent**，绝不让默认值伪装成合法授权。
- interactive 仅事件类来源（inbound/user_continue）；async 允许策略类来源
  （cron/heartbeat/sleep/subagent_completed/provider_recovery 等）——与
  dispatcher 授权同一套枚举（wake_dispatcher 导入）。
- ensure_wake_policy：注册/更新 canonical wake_policies（幂等，经 repo
  set_wake_policy；generation 更新与 revoked 原子化由 repository 保证）。
"""

from __future__ import annotations

import hashlib

from .common.id_generator import new_id
from .wake_dispatcher import ASYNC_SOURCES, INTERACTIVE_SOURCES

#: 受控来源全集（与 dispatcher 同一套授权枚举）。
_ALL_SOURCES = INTERACTIVE_SOURCES | ASYNC_SOURCES


def compute_wake_intent_dedup_key(
    *,
    owner_id: str,
    task_id: str,
    run_id: str,
    policy_generation: int,
    source: str,
    wake_reason: str,
    source_event_id: str,
    retry_event_id: str = "",
    due_window: str = "",
) -> str:
    """规格 §3 dedup_key 稳定计算（不含 attempt_id / provider key）。

    effective_source_event_id = retry_event_id or source_event_id（retry 优先）。
    """
    effective = str(retry_event_id or source_event_id or "")
    payload = "{}:{}:{}:{}:{}:{}:{}:{}".format(
        owner_id, task_id, run_id or "", int(policy_generation),
        source, wake_reason, effective, due_window,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def register_wake_intent(
    repo: object,
    *,
    owner_id: str,
    task_id: str,
    source: str,
    wake_reason: str,
    continuation_policy: str,
    provider_scope_ref: str,
    policy_generation: int,
    next_wake_at: float,
    run_id: str = "",
    parent_run_id: str = "",
    root_run_id: str = "",
    source_event_id: str = "",
    retry_event_id: str = "",
    provenance_ref: str = "",
    priority: int = 0,
    not_before: float = 0,
    due_window: str = "",
    retry_after: float = 0,
    expires_at: float | None = None,
    now: float | None = None,
) -> dict[str, object]:
    """producer 写一条 intent（fail-closed 校验 + dedup 幂等）。

    只写 intent，不 claim、不建 attempt。返回与 repo.create_wake_intent 一致
    （{created/existing/intent_id}）。校验失败抛 ValueError（结构化拒绝，
    不落库）。
    """
    import time

    now = time.time() if now is None else now
    owner_id = str(owner_id or "").strip()
    task_id = str(task_id or "").strip()
    source = str(source or "").strip()
    wake_reason = str(wake_reason or "").strip()
    continuation_policy = str(continuation_policy or "").strip()
    provider_scope_ref = str(provider_scope_ref or "").strip()
    if not owner_id:
        raise ValueError("wake_intent: owner_id 必填")
    if not task_id:
        raise ValueError("wake_intent: task_id 必填（intent 必须绑定目标任务）")
    if source not in _ALL_SOURCES:
        raise ValueError(f"wake_intent: source 不在受控枚举: {source!r}")
    if continuation_policy not in {"interactive", "async"}:
        raise ValueError(f"wake_intent: continuation_policy 无效: {continuation_policy!r}")
    if continuation_policy == "interactive" and source not in INTERACTIVE_SOURCES:
        raise ValueError(f"wake_intent: interactive 禁 {source} 来源（只允许事件类）")
    if continuation_policy == "async" and source not in ASYNC_SOURCES:
        raise ValueError(f"wake_intent: async 禁 {source} 来源（只允许策略类）")
    if not provider_scope_ref:
        raise ValueError("wake_intent: provider_scope_ref 必填（provider circuit 路由）")
    try:
        policy_generation = int(policy_generation)
    except (TypeError, ValueError) as exc:
        raise ValueError("wake_intent: policy_generation 必须为整数") from exc
    if policy_generation < 0:
        raise ValueError("wake_intent: policy_generation 缺失/无效（fail-closed）")
    if next_wake_at <= 0:
        raise ValueError("wake_intent: next_wake_at 必须为正时间戳")
    dedup_key = compute_wake_intent_dedup_key(
        owner_id=owner_id, task_id=task_id, run_id=run_id,
        policy_generation=policy_generation, source=source,
        wake_reason=wake_reason, source_event_id=source_event_id,
        retry_event_id=retry_event_id, due_window=due_window,
    )
    intent_id = new_id("wake_intent_id")
    return repo.create_wake_intent(
        intent_id=intent_id, dedup_key=dedup_key, owner_id=owner_id,
        task_id=task_id, run_id=run_id, parent_run_id=parent_run_id,
        root_run_id=root_run_id, source=source, wake_reason=wake_reason,
        source_event_id=source_event_id, retry_event_id=retry_event_id,
        provenance_ref=provenance_ref, provider_scope_ref=provider_scope_ref,
        continuation_policy=continuation_policy,
        policy_generation=policy_generation,
        priority=int(priority), not_before=not_before, next_wake_at=next_wake_at,
        due_window=due_window, retry_after=retry_after, expires_at=expires_at,
        now=now,
    )


def ensure_wake_policy(
    repo: object,
    *,
    owner_id: str,
    continuation_policy: str,
    policy_generation: int,
    allowed_sources: str,
    provider_scope_ref: str,
    scope_ref: str = "",
    policy_id: str = "",
    now: float | None = None,
) -> dict[str, object]:
    """注册/更新 canonical wake_policies（幂等，seq2463 合同）。

    allowed_sources / provider_scope_ref / scope_ref 均必填非空（fail-closed，
    dispatcher wake_policy_allows 对空值一律拒绝）。policy_id 缺省由框架铸造。
    """
    import time

    now = time.time() if now is None else now
    allowed_sources = str(allowed_sources or "").strip()
    provider_scope_ref = str(provider_scope_ref or "").strip()
    scope_ref = str(scope_ref or "").strip()
    if not allowed_sources:
        raise ValueError("wake_policy: allowed_sources 必填（空白名单 fail-closed）")
    if not provider_scope_ref:
        raise ValueError("wake_policy: provider_scope_ref 必填")
    if not scope_ref:
        raise ValueError("wake_policy: scope_ref 必填（owner-task-run 作用域）")
    policy_id = policy_id or new_id("wake_intent_id").replace("wakeint", "wpol", 1)
    return repo.set_wake_policy(
        policy_id=policy_id, owner_id=owner_id,
        continuation_policy=continuation_policy,
        policy_generation=int(policy_generation),
        allowed_sources=allowed_sources, provider_scope_ref=provider_scope_ref,
        scope_ref=scope_ref, now=now,
    )


__all__ = [
    "compute_wake_intent_dedup_key",
    "ensure_wake_policy",
    "register_wake_intent",
]
