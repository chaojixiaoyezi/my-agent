"""LLM 热路径 RED + token 埋点(审计 #19):agent_core 每次模型调用发指标到默认 registry。

原状:已有自建 Prometheus metrics 但只在 ingress/worker 平面接线,agent_core 的 LLM 调用热路径
零埋点——最该被企业告警消费的 RED(速率/错误/延迟)指标在跑起来的 agent 里是空的。本模块在
LLM 调用 seam 发 calls/duration/tokens 指标到 default_registry(),/metrics 即可被告警消费。

铁律:埋点全程异常隔离——发指标出错绝不冒泡、绝不影响 LLM 调用本身(热路径稳定性第一)。
指标实例一次创建复用(MetricsRegistry.counter 每次调用都新建实例,不能在热路径反复调)。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from .usage import input_token_usage, output_token_usage


@dataclass(frozen=True)
class _LlmMetrics:
    calls: object       # Counter:按 backend/outcome
    duration: object    # Histogram:调用耗时秒
    input_tokens: object   # Counter:输入 token 累计
    output_tokens: object  # Counter:输出 token 累计
    cost: object        # Counter:累计 USD 成本(按 model,审计 #19 残余)


_METRICS: _LlmMetrics | None = None
_LOCK = threading.Lock()


def _metrics() -> _LlmMetrics:
    global _METRICS
    with _LOCK:
        if _METRICS is None:
            from ...observability.metrics import default_registry

            reg = default_registry()
            _METRICS = _LlmMetrics(
                calls=reg.counter("agent_llm_calls_total", "LLM 调用数(按 backend/outcome)"),
                duration=reg.histogram("agent_llm_duration_seconds", "LLM 调用耗时(秒)"),
                input_tokens=reg.counter("agent_llm_input_tokens_total", "LLM 输入 token 累计"),
                output_tokens=reg.counter("agent_llm_output_tokens_total", "LLM 输出 token 累计"),
                cost=reg.counter("agent_llm_cost_usd_total", "LLM 累计 USD 成本(按 model)"),
            )
        return _METRICS


def record_llm_call(backend_label: str, duration_seconds: float, response: object, ok: bool) -> None:
    """发一次 LLM 调用的 RED + token 指标。异常隔离:埋点失败只吞不冒泡(绝不破坏 LLM 调用)。"""
    try:
        metrics = _metrics()
        labels = {"backend": backend_label}
        metrics.calls.inc(labels={"backend": backend_label, "outcome": "ok" if ok else "error"})
        metrics.duration.observe(max(0.0, duration_seconds), labels=labels)
        _record_tokens(metrics, labels, response)
    except Exception:
        pass  # 埋点绝不影响 LLM 调用(审计 #19,热路径稳定性第一)


def _record_tokens(metrics: _LlmMetrics, labels: dict, response: object) -> None:
    if response is None:
        return
    inp = input_token_usage(response) or 0
    out = output_token_usage(response) or 0
    if inp:
        metrics.input_tokens.inc(inp, labels=labels)
    if out:
        metrics.output_tokens.inc(out, labels=labels)


def record_run_cost(owner: str, run_id: str, model: str, response: object) -> None:
    """按真实 token 算成本,累计到全局台账的 tenant(owner)/run 维度(审计 #19/#2:成本接到 run 层)。

    在 generate_model_response 层调用——那里有 owner(config)+run_id(params)+model(backend),
    无需把上下文穿透到 worker 线程里的深层 seam。异常隔离:埋点失败只吞不冒泡。
    """
    if not model or response is None:
        return
    try:
        from ...llm_scale.cost_ledger import global_cost_ledger
        from ...llm_scale.model_pricing import cost_usd

        cost = cost_usd(model, input_token_usage(response) or 0, output_token_usage(response) or 0)
        if cost > 0:
            global_cost_ledger().record(cost, tenant=owner or "", run_id=run_id or "")
    except Exception:
        pass  # 成本记账绝不影响 LLM 调用


def record_llm_cost(model: str, response: object) -> None:
    """按模型单价算这次响应的真实 USD 成本,累计到 agent_llm_cost_usd_total{model}(审计 #19 残余)。

    接 LLM 热路径,让"花了多少钱"在 /metrics 可观测可告警。异常隔离:埋点失败只吞不冒泡。
    按 model 维度(seam 处可达 backend.model_name);按租户/run 累计需请求上下文穿透,留专项。
    """
    if not model or response is None:
        return
    try:
        from ...llm_scale.model_pricing import cost_usd

        cost = cost_usd(model, input_token_usage(response) or 0, output_token_usage(response) or 0)
        if cost > 0:
            _metrics().cost.inc(cost, labels={"model": model})
    except Exception:
        pass  # 成本埋点绝不影响 LLM 调用


def reset_for_test() -> None:
    """测试钩子:清掉已创建的指标实例(下次按当前 default_registry 重建)。"""
    global _METRICS
    with _LOCK:
        _METRICS = None
