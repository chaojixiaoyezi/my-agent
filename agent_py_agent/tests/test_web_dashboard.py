"""Phase 3 Web 仪表盘测试:数据聚合 + fail-closed 安全 + 真起 stdlib server 端到端。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from agent_py_agent.agent.web.dashboard import (
    DashboardConfig,
    DashboardServer,
    DashboardSources,
    build_dashboard_payload,
)


def test_build_payload_aggregates_and_counts() -> None:
    sources = DashboardSources(
        status=lambda: {"gateway": "running", "queue": 3},
        tasks=lambda: [{"id": "1", "status": "running"}, {"id": "2", "status": "done"}, {"id": "3", "status": "done"}],
        sessions=lambda: [{"id": "s1"}, {"id": "s2"}],
    )
    payload = build_dashboard_payload(sources)
    assert payload["status"]["gateway"] == "running"
    assert payload["task_counts"] == {"running": 1, "done": 2}
    assert payload["session_count"] == 2
    assert len(payload["tasks"]) == 3


def test_build_payload_degrades_on_source_error() -> None:
    def boom() -> dict:
        raise RuntimeError("source down")

    payload = build_dashboard_payload(DashboardSources(status=boom, tasks=boom, sessions=boom))
    # 读源失败各自降级,绝不整盘崩
    assert payload["status"] == {}
    assert payload["tasks"] == []
    assert payload["session_count"] == 0


def test_start_refuses_public_bind_without_token() -> None:
    """fail-closed:非回环绑定但没配 admin token → 拒绝启动(不无鉴权暴露公网)。"""
    server = DashboardServer(DashboardSources(), DashboardConfig(host="0.0.0.0", port=0))
    with pytest.raises(PermissionError):
        server.start()


def test_end_to_end_serves_html_and_json() -> None:
    sources = DashboardSources(
        status=lambda: {"gateway": "running"},
        tasks=lambda: [{"id": "t1", "status": "pending"}],
        sessions=lambda: [{"id": "s1"}],
    )
    server = DashboardServer(sources, DashboardConfig(host="127.0.0.1", port=0))
    port = server.start()
    try:
        # HTML 首页
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
            html = r.read().decode("utf-8")
        assert "my-agent 仪表盘" in html
        # JSON API
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/dashboard", timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
        assert data["status"]["gateway"] == "running"
        assert data["task_counts"] == {"pending": 1}
        assert data["session_count"] == 1
        # 404
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/nope", timeout=5)
        assert exc.value.code == 404
    finally:
        server.stop()


def test_loopback_allowed_without_token() -> None:
    """回环地址访问无需 token(本机个人使用,默认放行)。"""
    server = DashboardServer(DashboardSources(), DashboardConfig(host="127.0.0.1", port=0))
    port = server.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/dashboard", timeout=5) as r:
            assert r.status == 200
    finally:
        server.stop()
