"""并发占用探针(接手文档 §6-A"先量化再动手"的量化地基)。

回答"1000 并发瓶颈在哪一层"的四个占用面 + 一个等待面,全部挂 default_registry(),
GET /metrics 一把读走(与既有 LLM RED/token/cost 指标同端点):
- agent_gateway_queue_wait_seconds:请求从进队(created_at)到被认领的等待(两层限流下
  含 admission 排队)——直接回答"solo 用户 0 产出是排队饿死还是认领后卡首轮"。
- agent_gateway_workers_busy:请求执行线程忙数(上限=gateway_global_inflight_limit)。
- agent_gateway_inflight_requests:两层限流的全局在飞数(大坑占用,上限=全局限)。
- agent_gateway_admission_blocked:最近一次派发扫描里被限流挡在 pending 的请求数
  (>0=有请求在排队等坑,配合 queue_wait 分位定位"每人小坑满"还是"全局大坑满")。
- agent_background_owner_ticks_inflight:后台整合/唤醒 tick 在飞数(池上限 8)。
- agent_subagent_runners_inflight:子代理 runner 线程在飞数(单派工上限 runner_auto_concurrency)。
- agent_llm_inflight:真正压在模型 API 上的并发调用数(§6-A2 的"扇出倍数"实测值)。

铁律(同 llm_metrics):埋点全程异常隔离,发指标出错绝不冒泡、绝不影响真实链路;
指标实例一次创建复用,不在热路径反复建。
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass

# 排队等待的分布桶:秒级到分钟级(网关排队饿死的量级是几十秒~几十分钟,默认 RED 桶太细)。
_QUEUE_WAIT_BUCKETS = (0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 900.0, math.inf)


@dataclass(frozen=True)
class _ConcurrencyMetrics:
    llm_inflight: object
    gateway_queue_wait: object
    gateway_workers_busy: object
    background_ticks_inflight: object
    subagent_runners_inflight: object
    gateway_requests_enqueued: object
    gateway_requests_claimed: object
    gateway_inflight: object
    gateway_admission_blocked: object


_METRICS: _ConcurrencyMetrics | None = None
_LOCK = threading.Lock()


def _metrics() -> _ConcurrencyMetrics:
    global _METRICS
    with _LOCK:
        if _METRICS is None:
            from .metrics import default_registry

            reg = default_registry()
            _METRICS = _ConcurrencyMetrics(
                llm_inflight=reg.gauge("agent_llm_inflight", "在飞 LLM 调用数(压在模型 API 上的真实并发)"),
                gateway_queue_wait=reg.histogram(
                    "agent_gateway_queue_wait_seconds",
                    "网关请求从进队到被 worker 认领的等待(秒)",
                    buckets=_QUEUE_WAIT_BUCKETS,
                ),
                gateway_workers_busy=reg.gauge("agent_gateway_workers_busy", "request worker 忙数"),
                background_ticks_inflight=reg.gauge(
                    "agent_background_owner_ticks_inflight", "后台 owner 整合/唤醒 tick 在飞数"
                ),
                subagent_runners_inflight=reg.gauge(
                    "agent_subagent_runners_inflight", "子代理 runner 在飞数"
                ),
                gateway_requests_enqueued=reg.counter(
                    "agent_gateway_requests_enqueued_total", "进队(写入 pending)的网关请求累计数"
                ),
                gateway_requests_claimed=reg.counter(
                    "agent_gateway_requests_claimed_total", "被 worker 认领(进入处理)的网关请求累计数"
                ),
                gateway_inflight=reg.gauge(
                    "agent_gateway_inflight_requests", "两层限流的全局在飞请求数(大坑占用)"
                ),
                gateway_admission_blocked=reg.gauge(
                    "agent_gateway_admission_blocked", "最近一次派发扫描被限流挡在 pending 的请求数"
                ),
            )
        return _METRICS


def ensure_concurrency_metrics_registered() -> None:
    """把 5 个并发探针系列注册进 default_registry(幂等)。/metrics 渲染前调用:探针是首次埋点
    才懒注册的,刚重启无流量时注册表为空 → /metrics 只有空行,"没部署"和"没流量"分不清
    (真机§7-0 冒烟实锤);主动注册后系列以 0 值可见,scrape 侧可以稳定 grep 指标名。"""
    try:
        _metrics()
    except Exception:
        pass


def record_gateway_queue_wait(seconds: float) -> None:
    try:
        _metrics().gateway_queue_wait.observe(max(0.0, float(seconds)))
    except Exception:
        pass


def gateway_request_enqueued() -> None:
    """进队计数(写入 pending)。与 claimed 计数一起,让"排队饿死 vs 认领后卡首轮"一眼可分:
    enqueued 涨而 claimed 不跟=worker 槽饿死(请求根本没被认领);claimed 跟上却无产出
    =认领后卡在首轮(看 llm_inflight/输出)。"""
    try:
        _metrics().gateway_requests_enqueued.inc(1)
    except Exception:
        pass


def gateway_request_claimed() -> None:
    try:
        _metrics().gateway_requests_claimed.inc(1)
    except Exception:
        pass


def gateway_worker_busy(delta: float) -> None:
    try:
        _metrics().gateway_workers_busy.inc(delta)
    except Exception:
        pass


def gateway_inflight(delta: float) -> None:
    try:
        _metrics().gateway_inflight.inc(delta)
    except Exception:
        pass


def gateway_admission_blocked_set(value: float) -> None:
    try:
        _metrics().gateway_admission_blocked.set(value)
    except Exception:
        pass


def background_tick_inflight(delta: float) -> None:
    try:
        _metrics().background_ticks_inflight.inc(delta)
    except Exception:
        pass


def subagent_runner_inflight(delta: float) -> None:
    try:
        _metrics().subagent_runners_inflight.inc(delta)
    except Exception:
        pass


def llm_inflight(delta: float) -> None:
    try:
        _metrics().llm_inflight.inc(delta)
    except Exception:
        pass


def reset_concurrency_metrics_for_test() -> None:
    """测试钩子:与 reset_default_registry_for_test 配套,清掉缓存的指标实例。"""
    global _METRICS
    with _LOCK:
        _METRICS = None


__all__ = [
    "background_tick_inflight",
    "gateway_admission_blocked_set",
    "gateway_inflight",
    "gateway_request_claimed",
    "gateway_request_enqueued",
    "gateway_worker_busy",
    "llm_inflight",
    "record_gateway_queue_wait",
    "reset_concurrency_metrics_for_test",
    "subagent_runner_inflight",
]
