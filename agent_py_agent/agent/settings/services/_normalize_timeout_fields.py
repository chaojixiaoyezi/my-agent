"""Timeout and limit config normalization services."""

# LLM: Timeout-related numeric fields are separated so runtime config normalization stays below code-size risk.
# 模块用途: 归一化超时、扫描数量和默认展示数量等整数配置。

from __future__ import annotations

from ._coercion import CoercionService


# LLM: TimeoutFieldsService centralizes runtime integer limits that should be configurable.
# 类用途: 负责把超时、扫描限制和展示数量配置归一成安全整数。
class TimeoutFieldsService:
    """Normalize timeout-related config fields."""

    # LLM: TimeoutFieldsService.normalize belongs to the config pipeline; update tests when adding fields.
    # 函数用途: 归一化超时和数量类配置，并把非法值回退到 AgentConfig 默认值。
    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        out = dict(data)
        warnings = []
        for key, min_val, max_val in _TIMEOUT_INT_FIELDS:
            value, warn = CoercionService.coerce_int(
                key,
                out.get(key),
                getattr(defaults, key),
                min_val=min_val,
                max_val=max_val,
            )
            out[key] = value
            if warn:
                warnings.append(warn)
        return out, warnings


_TIMEOUT_INT_FIELDS = (
    ("lease_heartbeat_interval_seconds", 10, None),
    ("lease_stale_without_heartbeat_seconds", 30, None),
    ("task_lock_timeout_seconds", 1, None),
    ("gateway_worker_join_timeout_seconds", 0, None),
    ("gateway_ready_timeout_seconds", 1, None),
    ("gateway_service_command_timeout_seconds", 1, None),
    ("gateway_service_stop_timeout_seconds", 1, None),
    ("notification_channel_timeout_seconds", 1, None),
    ("memory_artifact_default_read_chars", 0, None),
    ("memory_archive_preview_level_0_chars", 0, None),
    ("memory_archive_preview_level_1_chars", 0, None),
    ("memory_archive_preview_level_2_chars", 0, None),
    ("memory_archive_preview_level_3_chars", 0, None),
    ("memory_archive_summary_chars", 0, None),
    ("memory_archive_search_file_limit", 0, None),
    ("memory_query_default_limit", 0, None),
    ("memory_query_default_page_size", 1, None),
    ("memory_query_content_preview_chars", 0, None),
    ("memory_resume_archive_scan_limit", 0, None),
    ("memory_resume_recommended_read_paths_limit", 0, None),
    ("memory_doctor_recent_archive_file_limit", 0, None),
    ("dispatch_default_max_runners", 0, None),
    ("collaboration_auto_dispatch_max_runners", 0, None),
    ("collaboration_default_deadline_seconds", 0, None),
    ("dispatch_default_limit", 0, None),
    ("dispatch_pending_runner_scan_limit", 1, None),
    ("watchdog_interval", 1, None),
    ("watchdog_max_restarts", 0, None),
    ("watchdog_restart_delay", 0, None),
)
