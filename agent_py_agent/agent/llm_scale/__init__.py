"""LLM 层规模化(Tier 3 企业规模化):成本是 10k-100k 并发的绑定约束(资源预计结论:
LLM API ~$100k-$1M+/月,碾压基础设施 ~$2万-8万/月)。本包是每次 LLM 调用前的准入闸:
- 每租户令牌桶限流(rate_limiter):一个租户突发不饿死其他租户。
- 每租户 token 预算(token_budget):窗口内累计消耗封顶——直接的成本闸。
- 全局并发限制(concurrency):在途调用数封顶,防打爆 provider 的 RPM/TPM 限额(稳定性)。
- 准入控制(admission):三关合一,LLM 调用前一次 precheck。

全自建(stdlib threading 够,无外部依赖):令牌桶/预算/信号量都是经典算法,自建可控、可注入时钟做确定性测试。
跨实例共享状态(多副本一致限流)需 Redis——研究确认是"没人做"的空白,待 Redis 可用时落地(见 admission 注释)。
"""

from agent_py_agent.agent.llm_scale.admission import AdmissionResult, LLMAdmission
from agent_py_agent.agent.llm_scale.concurrency import ConcurrencyLimiter, ConcurrencyTimeout
from agent_py_agent.agent.llm_scale.cost_ledger import CostLedger
from agent_py_agent.agent.llm_scale.model_pricing import ModelPrice, cost_usd, resolve_price
from agent_py_agent.agent.llm_scale.rate_limiter import TenantRateLimiter, TokenBucket
from agent_py_agent.agent.llm_scale.redis_admission import (
    RedisAdmissionConfig,
    RedisConcurrencyLimiter,
    RedisTenantRateLimiter,
    RedisTokenBudget,
    RedisUsdBudget,
    build_redis_admission,
    redis_client_from_url,
)
from agent_py_agent.agent.llm_scale.token_budget import TokenBudget
from agent_py_agent.agent.llm_scale.usd_budget import UsdBudget

__all__ = [
    "AdmissionResult",
    "ConcurrencyLimiter",
    "ConcurrencyTimeout",
    "CostLedger",
    "LLMAdmission",
    "ModelPrice",
    "RedisAdmissionConfig",
    "RedisConcurrencyLimiter",
    "RedisTenantRateLimiter",
    "RedisTokenBudget",
    "RedisUsdBudget",
    "TenantRateLimiter",
    "TokenBucket",
    "TokenBudget",
    "UsdBudget",
    "cost_usd",
    "build_redis_admission",
    "redis_client_from_url",
    "resolve_price",
]
