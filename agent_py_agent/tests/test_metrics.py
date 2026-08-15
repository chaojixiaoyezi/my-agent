"""Tier 4.1 自建 Prometheus 指标测试:Counter/Gauge/Histogram + 文本暴露格式 + 标签 + 线程安全。"""

from __future__ import annotations

import threading

from agent_py_agent.agent.observability.metrics import Counter, Gauge, Histogram, MetricsRegistry


def test_counter_render_prometheus_format() -> None:
    c = Counter("requests_total", "总请求数")
    c.inc()
    c.inc(4)
    lines = c.render()
    assert "# HELP requests_total 总请求数" in lines
    assert "# TYPE requests_total counter" in lines
    assert "requests_total 5" in lines


def test_counter_with_labels() -> None:
    c = Counter("http_requests_total")
    c.inc(labels={"method": "POST", "code": "200"})
    c.inc(2, labels={"method": "POST", "code": "200"})
    c.inc(labels={"method": "GET", "code": "404"})
    rendered = "\n".join(c.render())
    assert 'http_requests_total{code="200",method="POST"} 3' in rendered
    assert 'http_requests_total{code="404",method="GET"} 1' in rendered


def test_gauge_set_and_inc() -> None:
    g = Gauge("inflight")
    g.set(10)
    g.inc()
    g.inc(-3)
    assert "inflight 8" in g.render()


def test_histogram_buckets_sum_count() -> None:
    h = Histogram("latency_seconds", "延迟", buckets=(0.1, 1.0, float("inf")))
    for v in (0.05, 0.2, 0.2, 3.0):
        h.observe(v)
    rendered = "\n".join(h.render())
    assert "# TYPE latency_seconds histogram" in rendered
    assert 'latency_seconds_bucket{le="0.1"} 1' in rendered  # 仅 0.05 ≤ 0.1
    assert 'latency_seconds_bucket{le="1"} 3' in rendered  # 0.05,0.2,0.2 ≤ 1
    assert 'latency_seconds_bucket{le="+Inf"} 4' in rendered  # 全部
    assert "latency_seconds_count 4" in rendered
    assert "latency_seconds_sum 3.45" in rendered


def test_registry_renders_all() -> None:
    reg = MetricsRegistry()
    reg.counter("a_total", "A").inc(2)
    reg.gauge("b_gauge").set(7)
    out = reg.render()
    assert "a_total 2" in out and "b_gauge 7" in out
    assert out.endswith("\n")


def test_label_value_escaping() -> None:
    c = Counter("e")
    c.inc(labels={"path": 'a"b\\c'})
    assert 'path="a\\"b\\\\c"' in "\n".join(c.render())


def test_counter_thread_safe() -> None:
    c = Counter("hits")

    def bump() -> None:
        for _ in range(1000):
            c.inc()

    threads = [threading.Thread(target=bump) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert "hits 8000" in c.render()  # 8 线程 × 1000,无丢更新
