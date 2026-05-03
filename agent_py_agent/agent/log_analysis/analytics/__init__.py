from __future__ import annotations

"""Public API for log-analysis analytics."""

from ..models import Finding
from .baselines import SecurityBaselines, ensure_baselines
from .detectors import (
    DETECTORS,
    bruteforce_then_success,
    multi_source_weak_signal,
    rare_egress_after_alert,
    run_soft_detectors,
    vpn_new_geo_login,
    waf_attack_success_candidate,
    web_to_process_anomaly,
)
from .security_rules import SOFT_DETECTOR_RULES, DetectorRule, detector_ids, get_rule

__all__ = [
    "DETECTORS",
    "DetectorRule",
    "Finding",
    "SOFT_DETECTOR_RULES",
    "SecurityBaselines",
    "bruteforce_then_success",
    "detector_ids",
    "ensure_baselines",
    "get_rule",
    "multi_source_weak_signal",
    "rare_egress_after_alert",
    "run_soft_detectors",
    "vpn_new_geo_login",
    "waf_attack_success_candidate",
    "web_to_process_anomaly",
]
