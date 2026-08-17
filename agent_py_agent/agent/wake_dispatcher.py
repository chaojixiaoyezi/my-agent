"""wake_intent dispatcher（#233 第 2 步）——唯一执行入口。

规格 docs/design/WAKE_INTENT_SCHEDULING_SPEC.md：
- dispatcher 是唯一「due 选择 + CAS claim + 创建 attempt + handoff」入口；
  producer/queue/reconciler 禁调模型禁建 attempt（硬不变量 1/2）。
- 只消费「到期 + policy 有效 + 来源授权 + 配额/circuit 可用」的 intent。
- interactive 仅 inbound/user_continue 事件来源（禁周期扫描）；async 才允许
  cron/heartbeat/sleep/retry/recovery 按策略来源（seq2416/2420 修正）。
- claim 失败/叫醒失败不丢 intent：保持 claimed 直到 lease 回收或标
  last_error_ref 进 reconciler。
- 429：冻结该 provider 的 due intent 集合（retry_after），恢复后按显式
  策略一次新 attempt。

旧「信号源→拉起」路径按群一致（seq2432/2433 A）直接停掉（shadow 计数
比对），seed_registry/discover 保留为第 5 步 reconciler 素材，本模块不调用。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

#: 来源授权：interactive 仅允许事件类来源（禁周期扫描）。
INTERACTIVE_SOURCES = frozenset({"inbound", "user_continue"})
#: 来源授权：async 允许的策略类来源。
ASYNC_SOURCES = frozenset(
    {"cron", "heartbeat", "sleep", "subagent_completed", "provider_recovery",
     "interrupted_run_recovery", "retry_after_due"}
)
_ALL_SOURCES = INTERACTIVE_SOURCES | ASYNC_SOURCES


@dataclass(frozen=True)
class WakeDispatchOutcome:
    """一次 dispatch 尝试的结构化结果（审计/验收证据）。"""

    intent_id: str
    claimed: bool
    reason: str  # dispatched / rejected_policy / rejected_source / rejected_circuit / rejected_not_due / claimed_failed
    generation: int = 0


def validate_wake_intent_authorization(
    row: dict,
    *,
    policy_validator: object = None,
    provider_circuit_open: object = None,
) -> tuple[bool, str]:
    """授权校验（#236，不可绕过入口门）：policy 有效 + 来源授权 + 空值拒绝。

    结构化信号铁律：只读 intent 行字段，不解析任何自然语言。
    弹性门（综合群复核 seq2435-2444，不做死硬门）：字段级校验始终执行
    （fail-closed）；policy_validator / provider_circuit_open 为可选注入——
    注入存在时叠加 ledger/circuit 核对（拒绝证据落 last_error_ref），未注入
    时以字段门为准（policy 枚举 + source 枚举 + generation>=0 + scope 非空），
    绝不因基建未接入让 dispatcher 整体瘫痪（seq2444 的「无注入全拒」收窄为
    字段兜底 + 打警告日志可审计）。
    拒绝条件：
      - continuation_policy 空或不在 {interactive, async} → 拒绝
      - source 不在受控枚举 → 拒绝
      - interactive + source 不在 INTERACTIVE_SOURCES → 拒绝（禁周期扫描）
      - async + source 不在 ASYNC_SOURCES → 拒绝
      - provider_scope_ref 空（interactive/async 都要）→ 拒绝（provider circuit
        路由统一需要，seq2436：不能只对 async）
      - policy_generation < 0 → 拒绝
      - policy_validator 返回 False → 拒绝（匹配当前 policy ledger 且未撤销）
      - provider_circuit_open 返回 True → 拒绝（429/配额冻结该 provider）
    返回 (ok, reason)。
    """
    policy = str(row.get("continuation_policy") or "").strip()
    if policy not in {"interactive", "async"}:
        return False, "policy_missing_or_invalid"
    source = str(row.get("source") or "").strip()
    if source not in _ALL_SOURCES:
        return False, "source_not_authorized"
    if policy == "interactive" and source not in INTERACTIVE_SOURCES:
        return False, "source_not_allowed_for_interactive"
    if policy == "async" and source not in ASYNC_SOURCES:
        return False, "source_not_allowed_for_async"
    provider_scope_ref = str(row.get("provider_scope_ref") or "").strip()
    if not provider_scope_ref:
        return False, "provider_scope_ref_missing"
    try:
        generation = int(row.get("policy_generation") or -1)
    except (TypeError, ValueError):
        return False, "policy_generation_invalid"
    if generation < 0:
        return False, "policy_generation_missing"
    # 可选注入（不做死硬门）：有注入叠加 ledger/circuit 核对；无注入走
    # 字段兜底放行（上面的字段门已 fail-closed），并打警告日志留审计线索
    # （policy/circuit 基建落成后从 owner 依赖注入即可自动升级校验）。
    if callable(policy_validator):
        try:
            if not policy_validator(policy, generation):
                return False, "policy_generation_not_current_or_revoked"
        except Exception:  # noqa: BLE001 validator 异常保守拒绝（fail-closed）
            return False, "policy_validator_error"
    else:
        _LOGGER.warning(
            "wake dispatcher: policy_validator 未注入，policy ledger 核对跳过"
            "（字段门兜底：policy=%s generation=%s）", policy, generation,
        )
    if callable(provider_circuit_open):
        try:
            if provider_circuit_open(provider_scope_ref):
                return False, "provider_circuit_open_or_quota_frozen"
        except Exception:  # noqa: BLE001 circuit 检查异常保守拒绝（fail-closed）
            return False, "provider_circuit_check_error"
    else:
        _LOGGER.warning(
            "wake dispatcher: provider_circuit_open 未注入，circuit 核对跳过"
            "（scope=%s）", provider_scope_ref,
        )
    return True, ""


def dispatch_due_wake_intent(
    repo: object,
    row: dict,
    *,
    lease_owner: str,
    lease_seconds: float,
    policy_validator: object,
    provider_circuit_open: object,
    now: float | None = None,
) -> WakeDispatchOutcome:
    """对单条 due intent 执行授权校验 + CAS claim（不创建 attempt——attempt
    由调用方在 claim 成功后创建，保证「仅 dispatcher claim 后可建 attempt」）。

    claim 前的授权校验是不可绕过入口门（#236）：字段级门始终执行（policy 枚举 /
    source 枚举 / generation>=0 / scope 非空，fail-closed）；policy_validator /
    provider_circuit_open 为可选注入（有注入叠加 ledger/circuit 核对，无注入走
    字段兜底不瘫痪 dispatcher——综合群复核 seq2435-2444 弹性门）。校验失败 →
    标 last_error_ref 并保持 pending（可被 reconciler 审计），不 claim、不执行。
    """
    now = time.time() if now is None else now
    intent_id = str(row.get("intent_id") or "")
    ok, reject_reason = validate_wake_intent_authorization(
        row,
        policy_validator=policy_validator,
        provider_circuit_open=provider_circuit_open,
    )
    if not ok:
        try:
            repo.mark_wake_intent_rejected(intent_id, error_ref=f"authorization:{reject_reason}", now=now)
        except Exception:  # noqa: BLE001 标记失败不阻断返回
            pass
        return WakeDispatchOutcome(intent_id, claimed=False, reason=f"rejected_{reject_reason}")

    claim = repo.claim_wake_intent(
        intent_id,
        lease_owner=lease_owner,
        lease_seconds=lease_seconds,
        claim_token=f"{lease_owner}:{int(now * 1000)}",
        now=now,
    )
    if not claim.get("claimed"):
        return WakeDispatchOutcome(
            intent_id, claimed=False, reason=str(claim.get("reason") or "claimed_failed")
        )
    return WakeDispatchOutcome(
        intent_id,
        claimed=True,
        reason="dispatched",
        generation=int(claim.get("claim_generation") or 0),
    )


__all__ = [
    "ASYNC_SOURCES",
    "INTERACTIVE_SOURCES",
    "WakeDispatchOutcome",
    "dispatch_due_wake_intent",
    "validate_wake_intent_authorization",
]
