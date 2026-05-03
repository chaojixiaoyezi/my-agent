from __future__ import annotations

"""LLM: thin entry point for soft detector rules.

新手说明:
这个文件是检测器规则的主入口点，只做组合和导出。
实际逻辑分散在 rule_models、rule_evaluator、rule_loader 中。
"""

from .rule_evaluator import (
    bruteforce_then_success,
    multi_source_weak_signal,
    rare_egress_after_alert,
    run_soft_detectors,
    vpn_new_geo_login,
    waf_attack_success_candidate,
    web_to_process_anomaly,
)
from .rule_helpers import (
    _dedupe_findings,
    _evidence_id,
    _evidence_ref,
    _make_finding,
    _query,
    _stable_id,
)
from .rule_loader import clear_rule_cache, load_rule, preload_rules

# Backward-compatible aliases
detect_waf_attack_success_candidate = waf_attack_success_candidate
detect_web_to_process_anomaly = web_to_process_anomaly
detect_vpn_new_geo_login = vpn_new_geo_login
detect_bruteforce_then_success = bruteforce_then_success
detect_rare_egress_after_alert = rare_egress_after_alert
detect_multi_source_weak_signal = multi_source_weak_signal

DETECTORS = {
    "waf_attack_success_candidate": waf_attack_success_candidate,
    "web_to_process_anomaly": web_to_process_anomaly,
    "vpn_new_geo_login": vpn_new_geo_login,
    "bruteforce_then_success": bruteforce_then_success,
    "rare_egress_after_alert": rare_egress_after_alert,
    "multi_source_weak_signal": multi_source_weak_signal,
}

__all__ = [
    "DETECTORS",
    "_dedupe_findings",
    "_evidence_id",
    "_evidence_ref",
    "_make_finding",
    "_query",
    "_stable_id",
    "bruteforce_then_success",
    "detect_bruteforce_then_success",
    "detect_multi_source_weak_signal",
    "detect_rare_egress_after_alert",
    "detect_vpn_new_geo_login",
    "detect_waf_attack_success_candidate",
    "detect_web_to_process_anomaly",
    "multi_source_weak_signal",
    "rare_egress_after_alert",
    "run_soft_detectors",
    "vpn_new_geo_login",
    "waf_attack_success_candidate",
    "web_to_process_anomaly",
    # rule_loader
    "clear_rule_cache",
    "load_rule",
    "preload_rules",
]
