from __future__ import annotations

"""第1项 B 门槛4 验收测试: 动态超时 probe 统计(最小样本/滑窗/去极值/回退/边界)。

steward seq 1500 细化要求(门槛4 独立切片): 最小样本数/异常值去极值/滑窗
上限/无样本回退 fixed-rate/配置边界, 带行为开关(probe_outlier_trim)与
回退证据(source 字段), 不与超时修复混发。
"""

import pytest

from agent_py_agent.agent.agent_core.model.call_monitor import (
    FirstTokenTimeoutOptions,
    FirstTokenTimeoutParams,
    estimate_first_token_timeout,
)
from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallFirstTokenParams,
    ModelCallLedger,
    ModelCallLedgerContext,
    ModelCallStartedParams,
)

# ---------------------------------------------------------------- helpers

def _probe_ledger(tokens: int, latencies: list[float]) -> ModelCallLedger:
    """构造某 probe token 点的 N 条样本: started 时点 0, first_token 时点=latency,
    使 first_token_latency_seconds == latency(可控时钟, 可注入任意时延)。"""
    times: list[float] = []
    for latency in latencies:
        times.extend([0.0, latency])
    state = {"i": 0}

    def now() -> float:
        value = times[state["i"]]
        state["i"] += 1
        return value

    ledger = ModelCallLedger(context=ModelCallLedgerContext(now=now))
    for idx, latency in enumerate(latencies):
        call_id = f"probe-{tokens}-{idx}"
        ledger.started(
            ModelCallStartedParams(
                call_id=call_id,
                backend="test",
                model="m",
                input_tokens=tokens,
                is_probe=True,
            )
        )
        ledger.first_token(
            ModelCallFirstTokenParams(call_id=call_id, output_tokens_seen=1)
        )
    return ledger


def _estimate(ledger: ModelCallLedger, **options_kw) -> object:
    return estimate_first_token_timeout(
        FirstTokenTimeoutParams(
            input_tokens=1000,
            ledger=ledger,
            options=FirstTokenTimeoutOptions(**options_kw),
        )
    )


def _merge(*ledgers: ModelCallLedger) -> ModelCallLedger:
    """把多个 probe ledger 的记录合并进一个 ledger(同源 now 序列无冲突:
    各 ledger 时钟独立, 合并后 estimate 只读 records 不消耗时钟)。"""
    merged = ModelCallLedger()
    merged._records = [r for ledger in ledgers for r in ledger.records()]  # type: ignore[attr-defined]
    merged._rebuild_index()  # type: ignore[attr-defined]
    return merged


# ---------------------------------------------------------------- 1. 最小样本数

def test_min_samples_insufficient_falls_back_to_fixed_rate() -> None:
    """每点 1 条样本(< min_samples=2) -> 回退 fixed-rate 估计(source 证据)。"""
    ledger = _merge(_probe_ledger(5000, [5.0]), _probe_ledger(10000, [8.0]))
    est = _estimate(ledger)
    assert est.source == "estimated_rate"  # 回退证据: 不用 probe


def test_min_samples_met_uses_probe() -> None:
    """每点 2 条样本(= min_samples) -> probe 估计生效, source 带样本数。"""
    ledger = _merge(
        _probe_ledger(5000, [5.0, 5.5]),
        _probe_ledger(10000, [8.0, 8.5]),
    )
    est = _estimate(ledger)
    assert est.source.startswith("probe_")
    assert est.source.endswith("_n2")  # 窗口样本数证据


# ---------------------------------------------------------------- 2. 异常值去极值

def test_outlier_trim_ignores_extreme_latency() -> None:
    """high 点含极端值时 trim 开启 -> 去极值取均值(斜率不被极值主导)。"""
    ledger = _merge(
        _probe_ledger(5000, [5.0, 6.0]),
        _probe_ledger(10000, [8.0, 9.0, 100.0]),  # 100 为异常高时延
    )
    est_trim = _estimate(ledger, probe_outlier_trim=True)
    est_no_trim = _estimate(ledger, probe_outlier_trim=False)
    assert est_trim.source.startswith("probe_")
    # trim: high=9(去极值后中位), slope=0.0007, prefill(1000)=0.7
    # no-trim: high=39, slope=0.0067, prefill=6.7 -> trim 显著更小
    assert est_trim.prefill_seconds == pytest.approx(0.7)
    assert est_trim.prefill_seconds < est_no_trim.prefill_seconds


# ---------------------------------------------------------------- 3. 滑窗上限

def test_window_limits_to_recent_samples() -> None:
    """滑窗上限=5: 最早写入的陈旧极端样本被窗口排除(seq1554 证据缺口补强)。

    陈旧样本 50/80 放在最早写入; trim 关闭 + 精确期望值下断言窗口确实
    排除它——若 50/80 被计入窗口(6 条全取), trim=False 均值 12.83/20.33
    -> prefill=1.5, 与 0.6 显著不同, 区分「滑窗排除」与「去极值」。
    """
    ledger = _merge(
        _probe_ledger(5000, [50.0, 5.0, 5.2, 5.4, 5.6, 5.8]),  # 50 最早写入
        _probe_ledger(10000, [80.0, 8.0, 8.2, 8.4, 8.6, 8.8]),  # 80 最早写入
    )
    est = _estimate(ledger, probe_window_samples=5, probe_outlier_trim=False)
    assert est.source.startswith("probe_")
    assert est.source.endswith("_n5")  # 窗口 5 条(每侧)
    # trim=False 下窗口 5 条均值 5.4/8.4(50/80 不在窗口): slope=0.0006,
    # prefill(1000)=0.6——若 50/80 被计入则 prefill=1.5, 断言精确区分
    assert est.prefill_seconds == pytest.approx(0.6)


def test_asymmetric_window_counts_independent() -> None:
    """两侧窗口独立计数: low 2 条 + high 4 条 -> source 用 min=2(不对称安全)。"""
    ledger = _merge(
        _probe_ledger(5000, [5.0, 5.5]),  # 2 条(>= min_samples=2)
        _probe_ledger(10000, [8.0, 8.1, 8.2, 8.3]),  # 4 条
    )
    est = _estimate(ledger)
    assert est.source.startswith("probe_")
    assert est.source.endswith("_n2")  # min(2, 4) = 2, 两侧独立取窗


# ---------------------------------------------------------------- 4. 行为开关: trim 关闭

def test_trim_off_uses_full_mean() -> None:
    """probe_outlier_trim=False -> 全样本均值(不去极值, 与 trim 结果不同)。"""
    ledger = _merge(
        _probe_ledger(5000, [1.0, 10.0, 100.0]),
        _probe_ledger(10000, [2.0, 12.0, 102.0]),
    )
    est = _estimate(ledger, probe_outlier_trim=False)
    # 全均值 low=37, high=38.67: slope=(38.67-37)/5000, prefill(1000)=0.334
    assert est.prefill_seconds == pytest.approx(
        (38.6667 - 37.0) / 5000.0 * 1000.0, abs=1e-4
    )
    assert est.source.startswith("probe_")


# ---------------------------------------------------------------- 5. 配置边界

def test_clamp_boundary_min_gt_max() -> None:
    """min>max 配置边界 -> clamp 后 max=min, timeout 不低于 min。"""
    ledger = _merge(_probe_ledger(5000, [5.0]), _probe_ledger(10000, [8.0]))
    est = _estimate(
        ledger,
        min_timeout_seconds=100.0,
        max_timeout_seconds=10.0,
        probe_min_samples=1,  # 单样本也放行(边界用例专注 clamp)
    )
    assert est.timeout_seconds >= 100.0  # max 被抬到 min


def test_zero_min_samples_clamped_to_one() -> None:
    """min_samples=0 配置边界 -> 钳到 1(不因 0 样本数判定失效)。"""
    ledger = _merge(_probe_ledger(5000, [5.0]), _probe_ledger(10000, [8.0]))
    est = _estimate(ledger, probe_min_samples=0)
    assert est.source.startswith("probe_")  # 0 -> 钳 1, 单样本可用
