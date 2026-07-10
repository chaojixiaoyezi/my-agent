"""Worker handler(Tier 5 接入点 = 企业规模化各层的咬合处):入站消息 → Tier 3 准入 + Tier 4 追踪 → 下游 agent。

这是 worker_entry 的 WORKER_HANDLER 默认实现,演示各 Tier 如何在"真正处理一条消息"时合到一起:
  1. Tier 4.2 追踪:从消息体取回 ingress 注入的 trace context,建子 span(全链路关联)。
  2. Tier 3 准入:precheck(每租户限流 + token 预算)——超限/超预算直接挡,不浪费 LLM 调用(成本闸)。
  3. Tier 3 并发:占全局并发槽再调 LLM(防打爆 provider RPM/TPM)。
  4. 调下游真实 agent(LLM 在此),返回真实 token 数 → settle 校正预算。

下游 agent 调用经 set_downstream 注入(真实接入主循环的挂载点);默认下游记录后返回 0,便于先把链路跑通。
配额从 env 读(LLM_TENANT_RPS / LLM_TENANT_TOKEN_BUDGET / LLM_MAX_INFLIGHT 等)。
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable

from agent_py_agent.agent.asgi_entry import env_float, env_int
from agent_py_agent.agent.llm_scale import (
    ConcurrencyLimiter,
    CostLedger,
    LLMAdmission,
    TenantRateLimiter,
    TokenBudget,
    UsdBudget,
    cost_usd,
)
from agent_py_agent.agent.observability.tracing import (
    Span,
    TraceContext,
    child_context,
    extract,
    new_trace,
)

# 下游真实 agent 调用:Callable[[payload, trace_ctx], int] 返回真实消耗 token 数。None=未接入(默认)。
Downstream = Callable[[dict, TraceContext], int]
_DOWNSTREAM: Downstream | None = None
_DEFAULT_EST = 2000
_SLOT_TIMEOUT = 30.0


def _build_admission() -> LLMAdmission:
    # 入口配额从 env 读,误配非法值用默认而非崩进程(审计 #24)
    rps = env_float("LLM_TENANT_RPS", 5)
    burst = env_float("LLM_TENANT_BURST", 10)
    budget = env_int("LLM_TENANT_TOKEN_BUDGET", 1_000_000)
    window = env_float("LLM_BUDGET_WINDOW_SEC", 3600)
    max_inflight = env_int("LLM_MAX_INFLIGHT", 32)
    usd_limit = env_float("LLM_TENANT_USD_BUDGET", 0)  # 0=不限额(审计 #19,默认不改行为;配上限才熔断)
    return LLMAdmission(
        TenantRateLimiter(rps, burst),
        TokenBudget(budget, window),
        ConcurrencyLimiter(max_inflight),
        usd_budget=UsdBudget(usd_limit, window),
    )


_ADMISSION = _build_admission()
_COST_LEDGER = CostLedger()  # 按 run/tenant 累计真实 USD 花费(审计 #19,观测/计费)
# 被准入挡下的 (tenant, reason),供指标/测试观察。有界环(只留最近 N 条):限流热路径每拒一条 append
# 一条,用 list 会随拒绝事件无界增长(审计 #16);deque(maxlen) 自动丢最旧,内存恒定。
_REJECTED_MAX = 1000
_REJECTED: deque[tuple[str, str]] = deque(maxlen=_REJECTED_MAX)


def set_downstream(fn: Downstream | None) -> None:
    """注入下游真实 agent 调用(接入主循环时调)。"""
    global _DOWNSTREAM
    _DOWNSTREAM = fn


def reset_for_test(admission: LLMAdmission | None = None) -> None:
    """测试钩子:换一套准入(自定义配额/注入时钟)并清观察缓冲 + 成本台账。"""
    global _ADMISSION, _COST_LEDGER
    if admission is not None:
        _ADMISSION = admission
    _REJECTED.clear()
    _COST_LEDGER = CostLedger()


def handle(payload: dict) -> None:
    """worker 处理一条入站消息:追踪关联 → 准入(限流+token+USD 预算)→ 并发槽 → 下游 → 结算。"""
    tenant = str(payload.get("tenant") or "default")
    ctx = extract(payload) or new_trace()
    est = int(payload.get("estimated_tokens") or _DEFAULT_EST)
    model = str(payload.get("model") or "")
    est_cost = cost_usd(model, est, 0) if model else 0.0  # 无 model 不计 USD(0=不参与 USD 闸)
    verdict = _ADMISSION.precheck(tenant, estimated_tokens=est, estimated_cost_usd=est_cost)
    if not verdict.admitted:
        _REJECTED.append((tenant, verdict.reason))  # 限流/超 token/超 USD 预算:挡下,不调 LLM(成本闸生效)
        return
    actual = _run_admitted(payload, ctx, est)
    _ADMISSION.settle(tenant, est, actual)  # 真实 token 校正预扣
    _account_cost(payload, tenant, est_cost, actual)  # 真实 USD 成本累加台账 + 校正 USD 预扣(审计 #19)


def _account_cost(payload: dict, tenant: str, est_cost: float, actual_tokens: int) -> None:
    """按真实 token 算 USD 成本,累加到 run/tenant 台账并校正 USD 预算预扣。无 model 则不计。"""
    model = str(payload.get("model") or "")
    if not model:
        return
    actual_cost = cost_usd(model, actual_tokens, 0)
    _COST_LEDGER.record(actual_cost, tenant=tenant, run_id=str(payload.get("run_id") or ""))
    _ADMISSION.settle_usd(tenant, est_cost, actual_cost)


def _run_admitted(payload: dict, ctx: TraceContext, est: int) -> int:
    """占并发槽 + 开计时 span,调下游真实 agent,返回真实消耗 token 数。"""
    with _ADMISSION.slot(timeout=_SLOT_TIMEOUT):
        with Span("worker.handle", child_context(ctx)):
            return _invoke_downstream(payload, ctx, est)


def _invoke_downstream(payload: dict, ctx: TraceContext, est: int) -> int:
    if _DOWNSTREAM is None:
        return est  # 未接入下游:按预估计(不偏移预算)
    result = _DOWNSTREAM(payload, ctx)
    return result if isinstance(result, int) and result >= 0 else est
