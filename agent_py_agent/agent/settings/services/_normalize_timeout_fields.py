"""Timeout and limit config normalization services."""


from __future__ import annotations

from ._coercion import CoercionService


class TimeoutFieldsService:
    """Normalize timeout-related config fields."""

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
        value, warn = _normalize_subagent_watch_interval(
            out.get("subagent_watch_interval_seconds"),
            defaults.subagent_watch_interval_seconds,
        )
        out["subagent_watch_interval_seconds"] = value
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
    ("runner_auto_concurrency", 0, None),
    ("background_context_max_string_chars", 0, None),
    ("background_context_max_list_items", 0, None),
    ("background_context_max_dict_items", 0, None),
    ("background_context_max_depth", 0, None),
    ("background_claim_ttl_seconds", 1, None),
    ("background_claim_heartbeat_interval_seconds", 0, None),
    ("conversation_thread_list_limit", 0, None),
    ("conversation_pending_wake_limit", 0, None),
    ("conversation_context_recent_limit", 0, None),
    ("conversation_unhandled_observation_limit", 0, None),
    ("background_pending_wake_prompt_limit", 0, None),
    ("contract_status_max_scan_files", 0, None),
    ("contract_status_max_report_bytes", 0, None),
    ("contract_status_recent_findings_limit", 0, None),
    ("skill_guard_max_files", 0, None),
    ("skill_guard_max_size_kb", 0, None),
    ("small_real_acceptance_max_runtime_seconds", 0, None),
    ("real_run_review_max_report_bytes", 0, None),
    ("real_run_review_max_log_bytes", 0, None),
    ("dispatch_default_max_runners", 0, None),
    ("collaboration_auto_dispatch_max_runners", 0, None),
    ("collaboration_default_deadline_seconds", 0, None),
    ("dispatch_default_limit", 0, None),
    ("dispatch_pending_runner_scan_limit", 1, None),
    ("watchdog_interval", 1, None),
    ("watchdog_max_restarts", 0, None),
    ("watchdog_restart_delay", 0, None),
)


def _normalize_subagent_watch_interval(value: object, default: int) -> tuple[int, str | None]:
    key = "subagent_watch_interval_seconds"
    if value is None:
        return int(default), None
    parsed = _watch_interval_int(value)
    if parsed is None:
        detail = "boolean" if isinstance(value, bool) else repr(value)
        return 60, f"{key}: expected an integer, got {detail}; using 60"
    if parsed < 60:
        return 60, f"{key}: expected >= 60, got {parsed}; using 60"
    if parsed > 7200:
        return 7200, f"{key}: expected <= 7200, got {parsed}; using 7200"
    return parsed, None


def _watch_interval_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit() or (stripped.startswith("-") and stripped[1:].isdigit()):
            return int(stripped)
    if isinstance(value, float) and value == int(value):
        return int(value)
    return None
