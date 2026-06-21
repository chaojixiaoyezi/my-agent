"""Prometheus 指标暴露(Tier 4.1 企业规模化,**全自建零依赖**)。

研究确认:claw 手写 Prometheus 文本(无 prometheus_client 依赖)是三家里的"自建典范",
企业告警最先消费 /metrics。my-agent 现状 = 无 metrics 暴露(真空白),本模块补上。

自建取舍:Prometheus 文本暴露格式简单(# HELP / # TYPE / name{labels} value),纯 stdlib 手写
即可,**不引 prometheus_client**(符合"能自建就自建")。线程安全(指标被多线程并发更新)。
提供 RED 所需的 Counter(速率/错误)+ Gauge(在飞)+ Histogram(延迟分位)。
"""

from __future__ import annotations

import math
import threading
from collections import defaultdict
from typing import Any


def _label_str(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in labels)
    return "{" + inner + "}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _key(labels: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((labels or {}).items()))


class Counter:
    """单调递增计数(速率/错误数)。"""

    def __init__(self, name: str, help_text: str = "") -> None:
        self.name = name
        self.help = help_text
        self._vals: dict[tuple[tuple[str, str], ...], float] = defaultdict(float)
        self._lock = threading.Lock()

    def inc(self, amount: float = 1.0, labels: dict[str, str] | None = None) -> None:
        with self._lock:
            self._vals[_key(labels)] += amount

    def render(self) -> list[str]:
        return _render_simple(self.name, self.help, "counter", self._vals)


class Gauge:
    """可增可减(在飞请求数/队列深度)。"""

    def __init__(self, name: str, help_text: str = "") -> None:
        self.name = name
        self.help = help_text
        self._vals: dict[tuple[tuple[str, str], ...], float] = defaultdict(float)
        self._lock = threading.Lock()

    def set(self, value: float, labels: dict[str, str] | None = None) -> None:
        with self._lock:
            self._vals[_key(labels)] = value

    def inc(self, amount: float = 1.0, labels: dict[str, str] | None = None) -> None:
        with self._lock:
            self._vals[_key(labels)] += amount

    def render(self) -> list[str]:
        return _render_simple(self.name, self.help, "gauge", self._vals)


_DEFAULT_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, math.inf)


class Histogram:
    """延迟分位(RED 的 Duration):累积桶 + _sum + _count。"""

    def __init__(self, name: str, help_text: str = "", buckets: tuple[float, ...] = _DEFAULT_BUCKETS) -> None:
        self.name = name
        self.help = help_text
        self._buckets = tuple(sorted(buckets))
        self._counts: dict[tuple[tuple[str, str], ...], list[int]] = defaultdict(lambda: [0] * len(self._buckets))
        self._sum: dict[tuple[tuple[str, str], ...], float] = defaultdict(float)
        self._lock = threading.Lock()

    def observe(self, value: float, labels: dict[str, str] | None = None) -> None:
        k = _key(labels)
        with self._lock:  # _bump_buckets 假定锁已持有(由本方法持)
            self._sum[k] += value
            self._bump_buckets(k, value)

    def _bump_buckets(self, k: tuple[tuple[str, str], ...], value: float) -> None:
        counts = self._counts[k]
        for i, edge in enumerate(self._buckets):
            if value <= edge:  # 累积桶:value 落进所有 le>=value 的桶
                counts[i] += 1

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} histogram"]
        for k, counts in self._counts.items():
            base = _label_str(k)
            for i, edge in enumerate(self._buckets):
                le = "+Inf" if math.isinf(edge) else _num(edge)
                lab = base[:-1] + f',le="{le}"' + "}" if base else f'{{le="{le}"}}'
                lines.append(f"{self.name}_bucket{lab} {counts[i]}")
            lines.append(f"{self.name}_sum{base} {_num(self._sum[k])}")
            lines.append(f"{self.name}_count{base} {counts[-1]}")
        return lines


def _render_simple(name: str, help_text: str, type_: str, vals: dict[tuple[tuple[str, str], ...], float]) -> list[str]:
    lines = [f"# HELP {name} {help_text}", f"# TYPE {name} {type_}"]
    for k, v in vals.items():
        lines.append(f"{name}{_label_str(k)} {_num(v)}")
    return lines


def _num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else repr(v)


class MetricsRegistry:
    """指标注册表;render() 输出完整 Prometheus 文本暴露(供 /metrics 端点)。"""

    def __init__(self) -> None:
        self._metrics: list[Counter | Gauge | Histogram] = []

    def counter(self, name: str, help_text: str = "") -> Counter:
        return self._add(Counter(name, help_text))

    def gauge(self, name: str, help_text: str = "") -> Gauge:
        return self._add(Gauge(name, help_text))

    def histogram(self, name: str, help_text: str = "") -> Histogram:
        return self._add(Histogram(name, help_text))

    def _add(self, metric: Any) -> Any:  # noqa: ANN401 - 泛型注册
        self._metrics.append(metric)
        return metric

    def render(self) -> str:
        out: list[str] = []
        for metric in self._metrics:
            out.extend(metric.render())
        return "\n".join(out) + "\n"


_DEFAULT_REGISTRY: MetricsRegistry | None = None
_DEFAULT_REGISTRY_LOCK = threading.Lock()


def default_registry() -> MetricsRegistry:
    """进程级默认指标注册表(审计 #19):agent_core 热路径往它发指标,/metrics 暴露它——无需把
    registry 穿透核心循环。懒创建,线程安全。asgi_ingress 未显式传 registry 时也默认用它,
    于是 LLM/工具热路径指标与入站指标合并暴露在同一个 /metrics。"""
    global _DEFAULT_REGISTRY
    with _DEFAULT_REGISTRY_LOCK:
        if _DEFAULT_REGISTRY is None:
            _DEFAULT_REGISTRY = MetricsRegistry()
        return _DEFAULT_REGISTRY


def reset_default_registry_for_test() -> None:
    """测试钩子:清空全局默认 registry(下次 default_registry() 重建)。"""
    global _DEFAULT_REGISTRY
    with _DEFAULT_REGISTRY_LOCK:
        _DEFAULT_REGISTRY = None
