# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

"""rule matching entrypoint for log analysis detectors.

新手说明:
这个文件保留检测器 public API。具体规则按职责拆到 rule_web.py 和
rule_identity.py，避免单个文件继续膨胀。
"""

from collections.abc import Sequence
from typing import Any

from .field_access import EventLike
from .rule_helpers import _dedupe_findings
from .rule_identity import bruteforce_then_success, multi_source_weak_signal, vpn_new_geo_login
from .rule_web import rare_egress_after_alert, waf_attack_success_candidate, web_to_process_anomaly


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 run_soft_detectors 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 基于规则或事件字段计算 run soft detectors 的判定结果，避免把推测当作事实写入。
def run_soft_detectors(events: Sequence[EventLike], *, baselines: Any = None) -> list[Any]:
    """Run all soft detectors on *events* and return deduplicated findings."""
    from ..baselines import ensure_baselines

    baseline_obj = ensure_baselines(baselines)
    findings: list[Any] = []
    for detector in (
        waf_attack_success_candidate,
        web_to_process_anomaly,
        vpn_new_geo_login,
        bruteforce_then_success,
        rare_egress_after_alert,
        multi_source_weak_signal,
    ):
        findings.extend(detector(events, baselines=baseline_obj))
    return _dedupe_findings(findings)


__all__ = [
    "bruteforce_then_success",
    "multi_source_weak_signal",
    "rare_egress_after_alert",
    "run_soft_detectors",
    "vpn_new_geo_login",
    "waf_attack_success_candidate",
    "web_to_process_anomaly",
]
