"""独立流式 idle 超时(seq2173/2174)测试矩阵。

背景(2026-08-15 3×3 端点故障实锤): 连接被吞(服务端不发字节)时,
流读取此前死等总超时派生的大间隔(600s), 期间无任何日志→auto-respawn
判卡死杀进程→重启循环。独立 stream_idle_timeout 让挂起 120s 快速失败
为 provider_timeout, 走模型层重试/退避, 不再被杀。
"""

from __future__ import annotations

import pytest

from agent_py_agent.agent.backends.gateway_helpers import (
    GatewayRequest,
    _stream_idle_interval,
)


def _request(*, timeout: int = 240, stream_idle: int | float | None = None) -> GatewayRequest:
    return GatewayRequest(
        api_base="https://example.invalid/v1",
        api_key="k",
        path="/messages",
        payload={},
        headers={},
        timeout=timeout,
        stream_idle_timeout=stream_idle,
    )


class TestStreamIdleInterval:
    """三条边界: ① idle ≤ 总预算; ② 无效值回退总超时; ③ 同一 cutoff 起点由调用方保证。"""

    def test_unset_follows_total_timeout(self) -> None:
        req = _request(timeout=240, stream_idle=None)
        assert _stream_idle_interval(req) == 240.0

    def test_configured_idle_used_when_smaller(self) -> None:
        req = _request(timeout=240, stream_idle=120)
        assert _stream_idle_interval(req) == 120.0

    def test_idle_larger_than_total_clamps_to_total(self) -> None:
        # 边界①: idle 不能大于总预算(否则 watchdog 比 parser 总 deadline 还晚)
        req = _request(timeout=240, stream_idle=500)
        assert _stream_idle_interval(req) == 240.0

    def test_zero_idle_falls_back_to_total(self) -> None:
        # 边界②: 无效值(<=0)回退总超时, 向后兼容
        req = _request(timeout=240, stream_idle=0)
        assert _stream_idle_interval(req) == 240.0

    def test_negative_idle_falls_back_to_total(self) -> None:
        req = _request(timeout=240, stream_idle=-5)
        assert _stream_idle_interval(req) == 240.0

    def test_fractional_idle_preserved(self) -> None:
        # 与 _stream_deadline_offset 同语义: 浮点预算不截断
        req = _request(timeout=240, stream_idle=30.5)
        assert _stream_idle_interval(req) == 30.5

    def test_total_timeout_floor_applies(self) -> None:
        # 总超时 <1s 时 floor 1.0 仍生效, idle 不小于 floor
        req = _request(timeout=0, stream_idle=None)
        assert _stream_idle_interval(req) == 1.0

    def test_idle_equal_to_total_is_allowed(self) -> None:
        req = _request(timeout=120, stream_idle=120)
        assert _stream_idle_interval(req) == 120.0

    def test_short_total_with_large_idle_clamps(self) -> None:
        req = _request(timeout=30, stream_idle=120)
        assert _stream_idle_interval(req) == 30.0

    def test_missing_attribute_compat(self) -> None:
        # 旧构造路径(无 stream_idle_timeout 参数)的 dataclass 实例
        req = GatewayRequest(
            api_base="https://example.invalid/v1",
            api_key="k",
            path="/messages",
            payload={},
            headers={},
            timeout=240,
        )
        assert _stream_idle_interval(req) == 240.0


class TestGatewayRequestField:
    def test_field_defaults_none(self) -> None:
        req = GatewayRequest(
            api_base="a", api_key="k", path="p", payload={}, headers={}, timeout=240
        )
        assert req.stream_idle_timeout is None

    def test_field_carries_value(self) -> None:
        req = _request(timeout=240, stream_idle=120)
        assert req.stream_idle_timeout == 120


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
