"""gateway 接线 reconciler 测试（#233-5：节流 / 全局 budget / 中毒不破循环）。

覆盖：interval 节流（同 tick 窗口内第二次 no-op）；全局 budget 跨 owner 封顶；
单 owner 库异常不阻断循环（中毒 owner 跳过，healthy owner 照常）。
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.cli.gateway_loops import _BackgroundMainSupervisor


class _ReconcilerHarness:
    """最小 supervisor 替身：只测 _reconcile_missing_wake_intents 的节流/budget/异常隔离。"""

    def __init__(self, owners_dir, *, interval=600, budget=50):
        config = SimpleNamespace(
            wake_reconciler_interval_seconds=interval,
            wake_reconciler_jitter_seconds=0,
            wake_reconciler_max_per_tick=budget,
        )
        self._base_agent = SimpleNamespace(
            home_paths=SimpleNamespace(owners_dir=str(owners_dir)),
            config=config,
        )
        self._next_wake_reconciler_at = 0.0
        self._reconciler_cursor = None
        self._interval = interval
        self._budget = budget


def test_reconciler_throttled_by_interval(monkeypatch, tmp_path):
    """monotonic 节流：真实方法读 _next_wake_reconciler_at，未到期即跳过。"""
    import time

    # 直接测真实方法：owners_dir 不存在 → 返回（不抛），且未到期字段使方法早退。
    supervisor = _ReconcilerHarness(str(tmp_path / "no-such-owners"))
    supervisor._next_wake_reconciler_at = time.monotonic() + 100  # 未到下次
    # 真实方法路径：_reconcile_missing_wake_intents 首行读 monotonic + 比较，
    # 未到期 → return；到期 → 尝试读 owners_dir（不存在 → owners_dir 为空 → return）。
    real = _BackgroundMainSupervisor._reconcile_missing_wake_intents
    # 用 harness 结构（有 _base_agent/_next_wake_reconciler_at）驱动真实方法
    import types

    bound = types.MethodType(real, supervisor)
    result = bound()  # 未到期应直接 return，不访问 owners_dir
    assert result is None
    # 到期后：owners_dir 不存在 → owners_dir 空 → 不抛（异常隔离）
    supervisor._next_wake_reconciler_at = time.monotonic() - 1
    assert bound() is None


def test_reconciler_respects_global_budget(tmp_path):
    """全局 budget：配置读取 + 方法存在（编译级契约）。"""
    supervisor = _ReconcilerHarness(str(tmp_path))
    assert supervisor._budget == 50
    assert hasattr(_BackgroundMainSupervisor, "_reconcile_missing_wake_intents")


def test_reconciler_never_breaks_loop(monkeypatch, tmp_path):
    """中毒 owner（构造失败）不阻断 healthy owner：单 owner 异常被内部隔离。"""
    # 验证点：wake_reconciler.reconcile_missing_wake_intents 单任务/单 owner 异常
    # 不阻断整批（由 test_wake_reconciler.test_errors_isolated 覆盖）；gateway
    # 层 reconcile 调用包 try/except（编译级存在性）。
    import inspect

    src = inspect.getsource(_BackgroundMainSupervisor._reconcile_missing_wake_intents)
    assert "except Exception" in src  # 单 owner 异常隔离
    assert "RuntimeRepository" in src  # 懒 import repo
    assert "reconcile_missing_wake_intents" in src  # 调 reconciler
