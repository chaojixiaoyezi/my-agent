"""LLM: Soft detector package for local log analysis.

给人看的解释：
本包是日志分析检测器的主入口，包含：
  - rules.py          : 高层检测器规则（WAF/VPN/暴力破解等）和 Finding 构造
  - field_access.py   : 底层字段访问、类型转换、时间工具
  - field_extractors.py: 字段提取器（source_ip、user 等）
  - classifiers.py    : 事件分类、实体比较、弱信号
  - helpers.py        : 向后兼容的 re-export 模块

所有公开符号均可从本包直接导入。
"""

from __future__ import annotations

from ...models import Finding  # noqa: F401
from .classifiers import (  # noqa: F401
    _asset_candidates,
    _destination,
    _entities_from_events,
    _first_entity,
    _gap_details,
    _is_alert_event,
    _is_auth_event,
    _is_egress_event,
    _is_failure,
    _is_http_success_or_error,
    _is_internal_ip,
    _is_success,
    _is_suspicious_file_write,
    _is_suspicious_web_process_event,
    _is_vpn_event,
    _is_waf_event,
    _normalize_entities,
    _primary_asset,
    _same_asset,
    _same_auth_scope,
    _same_source,
    _same_user,
    _unique_json_values,
    _unique_texts,
    _weak_signal,
)

# Re-export from sub-modules for backward compatibility.
# Existing ``from .detectors import X`` and ``from .analytics.detectors import X``
# statements will continue to work after the file→package migration.
from .field_access import (  # noqa: F401
    SUSPICIOUS_CHILD_PROCESSES,
    WEB_PARENT_PROCESSES,
    EventLike,
    JsonDict,
    _canonical_time,
    _clamp_float,
    _event_dict,
    _event_time,
    _field,
    _parse_time,
    _path_value,
    _present,
    _sort_time,
    _text,
    _time_bucket,
    _to_float,
    _to_int,
    _truthy,
    _window_for_events,
    _within_after,
    _within_before,
)
from .field_extractors import (  # noqa: F401
    _asset_ip,
    _basename,
    _cmdline,
    _destination_ip,
    _domain,
    _dst_port,
    _event_action,
    _event_class,
    _host,
    _outcome,
    _parent_process_name,
    _process_name,
    _severity,
    _source_ip,
    _source_product,
    _user,
    _victim_ip,
)
from .rules import (  # noqa: F401
    DETECTORS,
    bruteforce_then_success,
    detect_bruteforce_then_success,
    detect_multi_source_weak_signal,
    detect_rare_egress_after_alert,
    detect_vpn_new_geo_login,
    detect_waf_attack_success_candidate,
    detect_web_to_process_anomaly,
    multi_source_weak_signal,
    rare_egress_after_alert,
    run_soft_detectors,
    vpn_new_geo_login,
    waf_attack_success_candidate,
    web_to_process_anomaly,
)

__all__ = [
    "DETECTORS",
    "Finding",
    "SUSPICIOUS_CHILD_PROCESSES",
    "WEB_PARENT_PROCESSES",
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
]
