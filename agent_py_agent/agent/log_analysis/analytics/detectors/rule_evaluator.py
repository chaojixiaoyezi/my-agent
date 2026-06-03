
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
