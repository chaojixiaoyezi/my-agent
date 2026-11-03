# LLM: 指标埋点全程异常隔离，不影响真实链路；runner 在飞数对应的上限是配置 runner_concurrency。
# 模块用途: 登记并更新 Gateway、后台 tick、子代理 runner 与模型调用的并发占用指标。
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
- agent_subagent_runners_inflight:子代理 runner 线程在飞数(单派工上限 runner_concurrency)。
- agent_llm_inflight:真正压在模型 API 上的并发调用数(§6-A2 的"扇出倍数"实测值)。

铁律(同 llm_metrics):埋点全程异常隔离,发指标出错绝不冒泡、绝不影响真实链路;
指标实例一次创建复用,不在热路径反复建。
"""

from __future__ import annotations

import math
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from weakref import WeakKeyDictionary, WeakSet, WeakValueDictionary

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
_OWNER_ACTIVITY_PID = os.getpid()
_OWNER_ACTIVITY_LOCK = threading.RLock()
_OWNER_ACTIVITY_AGENTS = WeakKeyDictionary()
_OWNER_ACTIVITY_SOURCES = WeakValueDictionary()


# LLM: 身份只来自显式物理home与owner，不从cwd、全局gauge或显示台账推断；坏路径保持不可用。
# 函数用途: 生成当前执行来源的规范命名空间。
def _owner_activity_identity(owner_home, owner_id):
    try:
        if not isinstance(owner_id, str) or not owner_id.strip():
            return None
        home = Path(owner_home)
        return (str(home.resolve()), owner_id) if home.is_absolute() else None
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


# LLM: 只读取真实构造已绑定的home_paths；轻量门面、缺字段或已改绑对象不能贡献旧owner活动。
# 函数用途: 取得一个执行对象当前的物理owner。
def _agent_activity_identity(agent):
    paths = getattr(agent, 'home_paths', None)
    return _owner_activity_identity(getattr(paths, 'owner_home_dir', None), getattr(paths, 'owner_id', None))


# LLM: 来源只保留弱执行对象；provider可被观察服务缓存，但不能因此延长Agent或旧进程的生命。
# 类用途: 聚合同一进程和物理owner的真实工作线程占用。
@dataclass(eq=False)
class _OwnerModelActivity:
    identity: tuple[str, str]
    process_id: int
    members: WeakSet = field(default_factory=WeakSet, repr=False)
    active_model_calls: int = 0

    # LLM: 先拒绝fork后继再碰锁；仅已登记且仍有相同绑定的活执行对象可报告0，返回值不可改写来源。
    # 函数用途: 为当前进程生成不可变owner活动快照。
    def snapshot(self):
        if self.process_id != os.getpid():
            return None
        with _OWNER_ACTIVITY_LOCK:
            if _OWNER_ACTIVITY_SOURCES.get(self.identity) is not self:
                return None
            if not any(_agent_activity_identity(agent) == self.identity for agent in self.members):
                return None
            home, owner = self.identity
            return MappingProxyType({'owner_home': home, 'owner_id': owner,
                'process_id': self.process_id, 'active_model_calls': self.active_model_calls})


# LLM: 由SimpleAgent在home_paths绑定后登记；弱引用按活对象有界，同一物理owner共享，失效旧provider不借新对象复活。
# 函数用途: 初始化本地执行来源，尚未调用模型也能诚实报告空闲0。
def register_owner_model_activity(agent):
    if os.getpid() != _OWNER_ACTIVITY_PID:
        return None
    try:
        identity = _agent_activity_identity(agent)
        if identity is None:
            return None
        with _OWNER_ACTIVITY_LOCK:
            previous = _OWNER_ACTIVITY_AGENTS.get(agent)
            if previous is not None and previous.identity != identity:
                previous.members.discard(agent)
            source = _OWNER_ACTIVITY_SOURCES.get(identity)
            if source is None or source.snapshot() is None:
                source = _OwnerModelActivity(identity, _OWNER_ACTIVITY_PID)
                _OWNER_ACTIVITY_SOURCES[identity] = source
            source.members.add(agent)
            _OWNER_ACTIVITY_AGENTS[agent] = source
            return source.snapshot
    except Exception:
        # 探针失败不能中断原Agent构造。
        return None


# LLM: 只查询当前进程已登记的物理owner；读取本身不创建执行源，冷owner、旧来源和不支持进程保持None。
# 函数用途: 向业务观察装配提供一个无需访问私有gauge的只读来源。
def owner_model_activity_provider(owner_home, owner_id):
    if os.getpid() != _OWNER_ACTIVITY_PID:
        return None
    identity = _owner_activity_identity(owner_home, owner_id)
    if identity is None:
        return None
    with _OWNER_ACTIVITY_LOCK:
        source = _OWNER_ACTIVITY_SOURCES.get(identity)
        return source.snapshot if source is not None and source.snapshot() is not None else None


# LLM: 必须在实际worker及原准入之后进入；只在该栈真正退出时减计，caller取消/账本终态无权释放它。
# 函数用途: 平衡一次物理模型工作的owner占用，遥测异常不改变模型结果。
@contextmanager
def owner_model_activity_scope(agent):
    counted = None
    try:
        if os.getpid() == _OWNER_ACTIVITY_PID:
            identity = _agent_activity_identity(agent)
            with _OWNER_ACTIVITY_LOCK:
                source = _OWNER_ACTIVITY_AGENTS.get(agent)
                if (source is not None and source.identity == identity
                        and _OWNER_ACTIVITY_SOURCES.get(identity) is source):
                    source.active_model_calls += 1
                    counted = source
    except Exception:
        pass
    try:
        yield
    finally:
        if counted is not None and counted.process_id == os.getpid():
            try:
                with _OWNER_ACTIVITY_LOCK:
                    counted.active_model_calls -= 1
            except Exception:
                pass


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
    "owner_model_activity_provider",
    "owner_model_activity_scope",
    "register_owner_model_activity",
    "record_gateway_queue_wait",
    "reset_concurrency_metrics_for_test",
    "subagent_runner_inflight",
]
