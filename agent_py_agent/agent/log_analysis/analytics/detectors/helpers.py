# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

"""Field extraction, event classification, and utility helpers for soft detectors.

给人看的解释：
这个模块包含日志事件分析中用到的各种"小工具函数"——把原始事件字典转成统一格式、
提取字段、判断事件类型（WAF/VPN/认证等）、计算时间窗口、比较实体等。
所有探测器（rules.py）都依赖这里面的函数。

实现已拆分为三个子模块，本文件仅做 re-export，保持向后兼容：
  - field_access.py   : 底层字段访问、类型转换、时间工具
  - field_extractors.py: 字段提取器（source_ip、user 等）
  - classifiers.py    : 事件分类、实体比较、弱信号
"""

from __future__ import annotations

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

# Re-export everything from the three sub-modules so existing
# ``from .helpers import _xxx`` statements keep working.
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
