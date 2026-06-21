"""USD 成本台账(审计 #19):按 run/tenant 累计真实花费,支撑"公司 X 本月花多少钱"审计/计费。

纯观测累加(不挡请求),与 UsdBudget(窗口熔断)互补:一个回答"花了多少",一个"别超额"。线程安全。
tenant 维度由客户数天然有界;run 维度按 FIFO 上界逐出最旧(防长跑无界,与审计 #16 同律)。
在途内存累加 + 查询;落库做长期计费报表是更大的集成,留专项(本类提供累加核心 + 查询)。
"""

from __future__ import annotations

import threading

_MAX_RUNS = 10000  # run 维度水位:超过逐出最旧(运行级成本是短期观测,不长期留)


class CostLedger:
    def __init__(self, *, max_runs: int = _MAX_RUNS) -> None:
        self._tenant_usd: dict[str, float] = {}
        self._run_usd: dict[str, float] = {}
        self._max_runs = max(1, int(max_runs))
        self._lock = threading.Lock()

    def record(self, cost_usd: float, *, tenant: str = "", run_id: str = "") -> None:
        """累加一次调用的 USD 成本到 tenant / run(任一为空则该维度不计)。非正成本忽略。"""
        if cost_usd <= 0:
            return
        with self._lock:
            if tenant:
                self._tenant_usd[tenant] = self._tenant_usd.get(tenant, 0.0) + cost_usd
            if run_id:
                self._record_run_locked(run_id, cost_usd)

    def _record_run_locked(self, run_id: str, cost_usd: float) -> None:
        if run_id not in self._run_usd and len(self._run_usd) >= self._max_runs:
            self._run_usd.pop(next(iter(self._run_usd)))  # FIFO 逐出最旧 run(防无界)
        self._run_usd[run_id] = self._run_usd.get(run_id, 0.0) + cost_usd

    def tenant_cost(self, tenant: str) -> float:
        with self._lock:
            return self._tenant_usd.get(tenant, 0.0)

    def run_cost(self, run_id: str) -> float:
        with self._lock:
            return self._run_usd.get(run_id, 0.0)

    def total_cost(self) -> float:
        with self._lock:
            return sum(self._tenant_usd.values())
