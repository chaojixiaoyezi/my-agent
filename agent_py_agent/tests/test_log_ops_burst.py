"""速率突变检测集成测试(组件⑥):已知IP短时高频浮出 + 低频静默 + _burst_entity 辅助。"""

from agent.common.resilience import BurstTracker
from agent.tooling.log_ops import baseline
from agent.tooling.log_ops.daemon import _TriageCtx, _burst_entity, _triage_new_lines
from agent.tooling.log_ops.store import SourceSpec


def _baseline_knowing(ip, n=150):
    bl = baseline.SourceBaseline()
    for _ in range(n):
        bl.observe_line(f"2026-06-18 session {ip} active ok")  # 学成"已知正常"
    return bl


def test_burst_known_ip_surfaces():
    """同一已知IP短时高频(35次>阈值30)→速率突变让它产候选,即便基线把它当'已知正常'。"""
    spec = SourceSpec(kind="api", locator="x", source_id="s1")
    ctx = _TriageCtx(spec, [], _baseline_knowing("9.9.9.9"), BurstTracker(window=200, threshold=30))
    lines = ["2026-06-18 session 9.9.9.9 active ok" for _ in range(35)]
    candidates = _triage_new_lines(ctx, lines, base_line_no=1)
    assert len(candidates) >= 5  # 突破阈值后多行浮出(暴力破解/扫描即便用已知IP也抓得到)
    assert any("速率突变" in str(c) for c in candidates)


def test_known_ip_low_frequency_silent():
    """已知IP低频(5次<阈值)→不突发,不产候选(不误报正常流量)。"""
    spec = SourceSpec(kind="api", locator="x", source_id="s1")
    ctx = _TriageCtx(spec, [], _baseline_knowing("9.9.9.9"), BurstTracker(window=200, threshold=30))
    lines = ["2026-06-18 session 9.9.9.9 active ok" for _ in range(5)]
    candidates = _triage_new_lines(ctx, lines, base_line_no=1)
    assert len(candidates) == 0  # 已知IP低频静默,不误报


def test_burst_entity_helper():
    tracker = BurstTracker(window=100, threshold=3)
    line = "2026-06-18 login from 1.2.3.4 failed"
    assert _burst_entity(tracker, line) is None  # 第1次,不突发
    _burst_entity(tracker, line)  # 第2次
    assert _burst_entity(tracker, line) == "1.2.3.4"  # 第3次达阈值→返回突发IP


def test_burst_no_ip_line_safe():
    """无 IP 的行不喂追踪器、不崩。"""
    tracker = BurstTracker(window=100, threshold=2)
    assert _burst_entity(tracker, "2026-06-18 system healthcheck ok") is None
