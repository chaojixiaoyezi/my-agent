"""Domain-specific normalize services for config fields.

Was split across _normalize_core_fields / _normalize_home_fields /
_normalize_identity_fields / _normalize_operational_fields /
_normalize_runtime_fields / _normalize_timeout_fields — now merged.
"""

from __future__ import annotations

import math
import os

from ...path_access_policy import normalize_path_access_mode
from ..defaults import default_agent_config
from ._coercion import CoercionService
from .runtime_tool_field_specs import TOOL_INT_FIELDS

# ---------------------------------------------------------------------------
# shared coercion helpers (canonical versions, was duplicated across files)
# ---------------------------------------------------------------------------

def _append_warning(warnings: list[str], warn: str | None) -> None:
    if warn:
        warnings.append(warn)


def _apply_int_fields(
    out: dict[str, object],
    defaults: object,
    specs: tuple[tuple[str, int | None, int | None], ...],
) -> list[str]:
    warnings: list[str] = []
    for key, min_val, max_val in specs:
        value, warn = CoercionService.coerce_int(
            key, out.get(key), getattr(defaults, key),
            min_val=min_val, max_val=max_val,
        )
        out[key] = value
        _append_warning(warnings, warn)
    return warnings


def _apply_bool_fields(
    out: dict[str, object],
    defaults: object,
    keys: tuple[str, ...],
) -> list[str]:
    warnings: list[str] = []
    for key in keys:
        value, warn = CoercionService.coerce_bool(key, out.get(key), getattr(defaults, key))
        out[key] = value
        _append_warning(warnings, warn)
    return warnings


def _apply_float_fields(
    out: dict[str, object],
    defaults: object,
    specs: tuple[tuple[str, float | None, float | None], ...],
) -> list[str]:
    warnings: list[str] = []
    for key, min_val, max_val in specs:
        value, warn = CoercionService.coerce_float(
            key, out.get(key), getattr(defaults, key),
            min_val=min_val, max_val=max_val,
        )
        out[key] = value
        _append_warning(warnings, warn)
    return warnings


def _apply_choice_field(
    out: dict[str, object],
    defaults: object,
    key: str,
    choices: tuple[str, ...],
) -> list[str]:
    value, warn = CoercionService.coerce_choice(key, out.get(key), getattr(defaults, key), choices)
    out[key] = value
    return [warn] if warn else []


def _string_config_value(value: object) -> str:
    return value if isinstance(value, str) else ""


def _normalize_string_list(value: object) -> list[str]:
    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, (list, tuple)):
        raw_items = value
    else:
        return []
    return [str(item).strip() for item in raw_items if item is not None and str(item).strip()]


# ---------------------------------------------------------------------------
# temperature
# ---------------------------------------------------------------------------

def _normalize_temperature(out: dict[str, object], defaults: object) -> list[str]:
    raw_temp = out.get("temperature", defaults.temperature)
    temp_val = _temperature_value(raw_temp)
    if temp_val is not None and 0.0 <= temp_val <= 2.0:
        out["temperature"] = raw_temp.strip() if isinstance(raw_temp, str) else str(temp_val)
        return []
    out["temperature"] = defaults.temperature
    if temp_val is None:
        return [f"temperature: expected a float string, got {raw_temp!r}; using {defaults.temperature}"]
    return [f"temperature: expected 0.0-2.0, got {temp_val}; using {defaults.temperature}"]


def _temperature_value(raw_temp: object) -> float | None:
    if isinstance(raw_temp, str):
        try:
            return float(raw_temp.strip())
        except ValueError:
            return None
    if isinstance(raw_temp, (int, float)):
        return float(raw_temp)
    return None


# ---------------------------------------------------------------------------
# Model / Gateway / Daemon (was _normalize_core_fields.py)
# ---------------------------------------------------------------------------

class ModelFieldsService:
    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        out = dict(data)
        warnings = _apply_choice_field(
            out, defaults, "model_backend",
            ("echo", "anthropic_compatible", "openai_compatible"),
        )
        warnings.extend(_apply_int_fields(
            out, defaults,
            (
                ("request_timeout", 1, 600),
                ("max_tokens", 1, None),
                ("model_context_window_tokens", 1, None),
            ),
        ))
        warnings.extend(_apply_bool_fields(out, defaults, ("auto_bench_model_on_first_use",)))
        warnings.extend(_normalize_temperature(out, defaults))
        return out, warnings


class GatewayFieldsService:
    _INT_FIELD_SPECS = (
        ("gateway_heartbeat_interval", 5, None),
        ("gateway_stale_seconds", 30, None),
        ("gateway_stop_timeout", 1, None),
        ("gateway_request_timeout", 1, None),
        ("gateway_request_workers", 1, None),
        ("gateway_processing_timeout_seconds", 30, None),
        ("gateway_request_max_attempts", 0, None),
        ("gateway_port", 0, 65535),
    )
    _FLOAT_FIELD_SPECS = (("gateway_request_poll_interval", 0.05, None),)

    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        out = dict(data)
        warnings = _apply_int_fields(out, defaults, GatewayFieldsService._INT_FIELD_SPECS)
        warnings.extend(_apply_float_fields(out, defaults, GatewayFieldsService._FLOAT_FIELD_SPECS))
        return out, warnings


class DaemonFieldsService:
    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        out = dict(data)
        warnings = _apply_int_fields(
            out, defaults,
            (
                ("daemon_interval", 1, None),
                ("daemon_limit", 0, None),
                ("daemon_max_cycles", 0, None),
                ("daemon_max_cards", 0, None),
            ),
        )
        warnings.extend(_apply_bool_fields(
            out, defaults,
            ("daemon_planner", "daemon_mutate_state", "daemon_start_runners", "daemon_probe"),
        ))
        return out, warnings


# ---------------------------------------------------------------------------
# Home layout (was _normalize_home_fields.py)
# ---------------------------------------------------------------------------

_HOME_STRING_FIELDS = (
    "my_agent_home", "my_agent_owner_provider", "my_agent_owner_kind",
    "my_agent_owner_id", "workspace_task_path_template",
    "external_knowledge_index_file_name",
)
_EXTERNAL_KNOWLEDGE_LIST_FIELDS = (
    "external_knowledge_directory_roots",
    "external_knowledge_api_sources",
    "external_knowledge_database_sources",
)
_HOME_RUNTIME_INT_FIELDS = (("home_lesson_auto_read_limit", 0, 20),)
_HOME_RUNTIME_BOOL_FIELDS = (
    "home_context_enabled", "daily_memory_mirror_enabled", "run_task_workspace_enabled",
)
_PROVIDER_SPACE_INT_FIELDS = (
    ("provider_space_default_max_storage_mb", 1, None),
    ("provider_space_max_download_file_mb", 1, None),
    ("provider_space_trash_retention_days", 0, None),
)


def _normalize_home_strings(out: dict[str, object], defaults: object) -> list[str]:
    warnings: list[str] = []
    for key in _HOME_STRING_FIELDS:
        explicit = key in out
        raw = out.get(key, getattr(defaults, key))
        if isinstance(raw, str) and raw.strip():
            out[key] = raw.strip()
            continue
        out[key] = getattr(defaults, key)
        if not explicit and str(raw or "").strip() == "":
            continue
        warnings.append(f"{key}: expected a non-empty string, got {raw!r}; using default")
    return warnings


def _normalize_external_knowledge_lists(out: dict[str, object], defaults: object) -> None:
    for key in _EXTERNAL_KNOWLEDGE_LIST_FIELDS:
        out[key] = _normalize_string_list(out.get(key, getattr(defaults, key)))


class HomeLayoutFieldsService:
    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        out = dict(data)
        warnings = _normalize_home_strings(out, defaults)
        _normalize_external_knowledge_lists(out, defaults)
        warnings.extend(_apply_int_fields(out, defaults, _HOME_RUNTIME_INT_FIELDS))
        warnings.extend(_apply_bool_fields(out, defaults, _HOME_RUNTIME_BOOL_FIELDS))
        warnings.extend(_apply_int_fields(out, defaults, _PROVIDER_SPACE_INT_FIELDS))
        warnings.extend(_apply_bool_fields(out, defaults, ("provider_space_destructive_actions_use_trash",)))
        return out, warnings


# ---------------------------------------------------------------------------
# Adapter / User identity (was _normalize_identity_fields.py)
# ---------------------------------------------------------------------------

class AdapterFieldsService:
    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        warnings: list[str] = []
        out = dict(data)

        def apply(key: str, coerced: object, warn: str | None) -> None:
            out[key] = coerced
            if warn:
                warnings.append(warn)

        for key in ("feishu_app_id", "feishu_app_secret", "feishu_verification_token", "feishu_encrypt_key"):
            out[key] = _string_config_value(out.get(key, defaults.feishu_app_id if key == "feishu_app_id" else ""))

        v, w = CoercionService.coerce_int(
            "feishu_callback_port", out.get("feishu_callback_port"),
            defaults.feishu_callback_port, min_val=1024, max_val=65535,
        )
        apply("feishu_callback_port", v, w)

        for key in ("qq_app_id", "qq_app_secret"):
            env_key = key.upper()
            env_val = os.environ.get(env_key, "")
            if env_val:
                out[key] = env_val
            else:
                out[key] = _string_config_value(out.get(key, defaults.qq_app_id if key == "qq_app_id" else ""))

        return out, warnings


def _normalize_user_id(out: dict[str, object], defaults: object, warnings: list[str]) -> None:
    raw_user_id = out.get("user_id", defaults.user_id)
    if isinstance(raw_user_id, str) and raw_user_id.strip():
        out["user_id"] = raw_user_id.strip()
        return
    out["user_id"] = defaults.user_id
    warnings.append(f"user_id: expected a non-empty string, got {raw_user_id!r}; using default")


def _normalize_user_auth(out: dict[str, object], defaults: object, warnings: list[str]) -> None:
    value, warn = CoercionService.coerce_bool("auth_enabled", out.get("auth_enabled"), defaults.auth_enabled)
    out["auth_enabled"] = value
    if warn:
        warnings.append(warn)


def _normalize_admin_user(out: dict[str, object], defaults: object) -> None:
    raw_admin = out.get("admin_user_id", defaults.admin_user_id)
    out["admin_user_id"] = raw_admin.strip() if isinstance(raw_admin, str) and raw_admin.strip() else defaults.admin_user_id


class UserFieldsService:
    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        warnings: list[str] = []
        out = dict(data)
        _normalize_user_id(out, defaults, warnings)
        _normalize_user_auth(out, defaults, warnings)
        _normalize_admin_user(out, defaults)
        return out, warnings


# ---------------------------------------------------------------------------
# Runtime bool switches (was _normalize_operational_fields.py)
# ---------------------------------------------------------------------------

_RUNTIME_BOOL_FIELDS = (
    "auto_detect_work_on_startup", "auto_save_memory", "local_store_fts_enabled",
    "enable_self_learning", "enable_subagents", "notification_enabled",
    "concurrency_lock_enabled", "audit_enabled", "watchdog_enabled",
)


def _normalize_runtime_bools(out: dict[str, object], defaults: object) -> list[str]:
    warnings: list[str] = []
    for key in _RUNTIME_BOOL_FIELDS:
        value, warn = CoercionService.coerce_bool(key, out.get(key), getattr(defaults, key))
        out[key] = value
        _append_warning(warnings, warn)
    return warnings


def _normalize_runtime_choices(out: dict[str, object], defaults: object) -> list[str]:
    value, warn = CoercionService.coerce_choice(
        "log_level", out.get("log_level"), defaults.log_level,
        choices=("debug", "info", "warning", "error", "critical"),
    )
    out["log_level"] = value
    return [warn] if warn else []


class RuntimeBoolFieldsService:
    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        out = dict(data)
        warnings = _normalize_runtime_bools(out, defaults)
        warnings.extend(_normalize_runtime_choices(out, defaults))
        return out, warnings


# ---------------------------------------------------------------------------
# Tool / Subagent (was _normalize_runtime_fields.py)
# ---------------------------------------------------------------------------

def _normalize_tool_int_fields(out: dict[str, object], defaults: object) -> list[str]:
    return _apply_int_fields(out, defaults, TOOL_INT_FIELDS)


def _normalize_tool_bool_fields(out: dict[str, object], defaults: object) -> list[str]:
    return _apply_bool_fields(
        out, defaults,
        ("stream_enabled", "tool_catalog_include_examples", "tool_catalog_show_truncated_notice"),
    )


def _normalize_tool_catalog_fields(out: dict[str, object], defaults: object) -> list[str]:
    warnings: list[str] = []
    mode, warn = CoercionService.coerce_choice(
        "tool_catalog_mode", out.get("tool_catalog_mode"), defaults.tool_catalog_mode,
        choices=("compact", "full", "retrieval_only", "off"),
    )
    out["tool_catalog_mode"] = mode
    _append_warning(warnings, warn)
    protocol, protocol_warn = CoercionService.coerce_choice(
        "tool_protocol", out.get("tool_protocol"), defaults.tool_protocol,
        choices=("text", "native"),
    )
    out["tool_protocol"] = protocol
    _append_warning(warnings, protocol_warn)
    out["tool_catalog_categories"] = _normalize_string_list(
        out.get("tool_catalog_categories", defaults.tool_catalog_categories)
    )
    return warnings


def _normalize_background_tool_fields(out: dict[str, object], defaults: object) -> list[str]:
    out["background_main_agent_allowed_tools"] = _normalize_string_list(
        out.get("background_main_agent_allowed_tools", defaults.background_main_agent_allowed_tools)
    )
    return []


def _normalize_path_access_fields(out: dict[str, object], defaults: object) -> list[str]:
    raw_mode = out.get("path_access_mode", defaults.path_access_mode)
    mode = normalize_path_access_mode(raw_mode)
    warnings: list[str] = []
    normalized_raw = str(raw_mode or "").strip().lower().replace("_", "-")
    known_values = {"normal", "full", ""}
    if normalized_raw not in known_values:
        warnings.append(f"path_access_mode: unknown value {raw_mode!r}; using {mode!r}")
    out["path_access_mode"] = mode
    roots = _normalize_string_list(out.get("path_dangerous_roots", defaults.path_dangerous_roots))
    out["path_dangerous_roots"] = roots or list(defaults.path_dangerous_roots)
    return warnings


def _normalize_command_access_mode(out: dict[str, object], defaults: object) -> list[str]:
    warnings: list[str] = []
    raw = out.get("access_mode", defaults.access_mode)
    normalized_raw = str(raw).strip().lower().replace("_", "-") if raw is not None else ""
    mode, warn = CoercionService.coerce_choice(
        "access_mode", normalized_raw, defaults.access_mode,
        choices=("restricted", "workspace-write", "full-access"),
    )
    out["access_mode"] = mode
    _append_warning(warnings, warn)
    return warnings


def _normalize_dispatch_watch_interval(out: dict[str, object], defaults: object) -> list[str]:
    warnings: list[str] = []
    value, warn = CoercionService.coerce_float(
        "dispatch_default_watch_interval",
        out.get("dispatch_default_watch_interval"),
        defaults.dispatch_default_watch_interval,
        min_val=0.0, max_val=None,
    )
    out["dispatch_default_watch_interval"] = value
    _append_warning(warnings, warn)
    return warnings


def _normalize_runner_timeout_by_role(value: object) -> tuple[dict[str, object], str | None]:
    if value in ({}, None, ""):
        return {}, None
    if isinstance(value, dict):
        return _normalize_runner_timeout_mapping(value), None
    if isinstance(value, list):
        parsed = _timeout_mapping_from_list(value)
        if parsed is not None:
            return parsed, None
    return {}, f"runner_timeout_by_role: expected dict or key=value list, got {value!r}; using default"


def _normalize_runner_timeout_mapping(value: dict[object, object]) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key or "").strip().lower()
        if key:
            normalized[key] = raw_value
    return normalized


def _timeout_mapping_from_list(value: list[object]) -> dict[str, object] | None:
    parsed: dict[object, object] = {}
    for item in value:
        text = str(item or "").strip()
        if not text or "=" not in text:
            return None
        key, raw_value = text.split("=", 1)
        parsed[key.strip()] = raw_value.strip()
    return _normalize_runner_timeout_mapping(parsed)


class ToolFieldsService:
    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        out = dict(data)
        warnings = _normalize_tool_int_fields(out, defaults)
        warnings.extend(_normalize_tool_bool_fields(out, defaults))
        warnings.extend(_normalize_tool_catalog_fields(out, defaults))
        warnings.extend(_normalize_path_access_fields(out, defaults))
        warnings.extend(_normalize_command_access_mode(out, defaults))
        warnings.extend(_normalize_dispatch_watch_interval(out, defaults))
        warnings.extend(_normalize_background_tool_fields(out, defaults))
        return out, warnings


class SubagentBasicFieldsService:
    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        out = dict(data)
        warnings = _apply_int_fields(
            out, defaults,
            (
                ("memory_top_k", 0, None),
                ("max_subagents", 0, None),
                ("subagent_board_limit", 0, None),
                ("subagent_spawn_default_count", 0, None),
                ("subagent_cli_default_limit", 0, None),
                ("subagent_probe_default_limit", 0, None),
                ("subagent_hierarchy_default_max_depth", 0, None),
                ("subagent_hierarchy_recovery_max_nodes", 0, None),
                ("subagent_hierarchy_max_children_per_tool_call", 0, None),
                ("subagent_descendant_scan_limit", 1, None),
                ("subagent_takeover_chain_max_depth", 0, None),
                ("subagent_context_summary_inline_json_chars", 0, None),
                ("subagent_context_summary_inline_text_chars", 0, None),
            ),
        )
        out["subagent_allowed_tools"] = _normalize_string_list(
            out.get("subagent_allowed_tools", defaults.subagent_allowed_tools)
        )
        out["subagent_role_template_dirs"] = _normalize_string_list(
            out.get("subagent_role_template_dirs", defaults.subagent_role_template_dirs)
        )
        mode, warn = CoercionService.coerce_choice(
            "subagent_mode", out.get("subagent_mode"), defaults.subagent_mode,
            choices=("trusted_local_hardening", "balanced", "strict"),
        )
        out["subagent_mode"] = mode
        _append_warning(warnings, warn)
        return out, warnings


class SubagentAdvancedFieldsService:
    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        out = dict(data)
        warnings = _apply_int_fields(
            out, defaults,
            (
                ("subagent_debug_trace_level", 0, 5),
                ("result_check_timeout_seconds", 1, 300),
                ("dynamic_timeout_min", 10, None),
                ("dynamic_timeout_max", 60, None),
                ("max_auto_split_depth", 0, None),
                ("max_auto_retry_attempts", 1, 10),
                ("subagent_memory_delete_after_days", 0, None),
            ),
        )
        warnings.extend(_apply_bool_fields(
            out, defaults,
            (
                "result_check_execute_tests",
                "closeout_for_all_task_nodes",
                "subagent_destroy_summary_required",
            ),
        ))
        retention_policy = _string_config_value(
            out.get("subagent_memory_retention_policy", defaults.subagent_memory_retention_policy)
        ).strip()
        out["subagent_memory_retention_policy"] = retention_policy or defaults.subagent_memory_retention_policy
        value, warn = CoercionService.coerce_float(
            "dynamic_timeout_safety_margin", out.get("dynamic_timeout_safety_margin"),
            defaults.dynamic_timeout_safety_margin, min_val=1.0, max_val=10.0,
        )
        out["dynamic_timeout_safety_margin"] = value
        _append_warning(warnings, warn)
        timeouts, warn = _normalize_runner_timeout_by_role(
            out.get("runner_timeout_by_role", defaults.runner_timeout_by_role)
        )
        out["runner_timeout_by_role"] = timeouts
        _append_warning(warnings, warn)
        return out, warnings


# ---------------------------------------------------------------------------
# Timeout (was _normalize_timeout_fields.py)
# ---------------------------------------------------------------------------

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
    ("memory_compact_semantic_summary_protect_head", 0, None),
    ("memory_compact_semantic_summary_protect_tail", 0, None),
    ("memory_compact_semantic_summary_min_middle", 1, None),
    ("memory_compact_semantic_summary_max_input_chars", 0, None),
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


def _normalize_runner_timeout_seconds(value: object, default: object) -> tuple[str, str | None]:
    key = "runner_timeout_seconds"
    if value is None:
        return str(default), None
    if isinstance(value, bool):
        return str(default), f"{key}: expected 'off', 'auto', or a non-negative number, got boolean; using default {default!r}"
    if isinstance(value, (int, float)):
        if math.isfinite(float(value)) and value >= 0:
            return _format_timeout_number(value), None
        return str(default), f"{key}: expected >= 0, got {value}; using default {default!r}"
    if isinstance(value, str):
        stripped = value.strip().lower()
        if stripped in {"off", "auto"}:
            return stripped, None
        parsed = _timeout_float(stripped)
        if parsed is not None and math.isfinite(parsed) and parsed >= 0:
            return _format_timeout_number(parsed), None
    return str(default), f"{key}: expected 'off', 'auto', or a non-negative number, got {value!r}; using default {default!r}"


def _timeout_float(value: str) -> float | None:
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed


def _format_timeout_number(value: int | float) -> str:
    parsed = float(value)
    if parsed == int(parsed):
        return str(int(parsed))
    return str(parsed)


class TimeoutFieldsService:
    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        out = dict(data)
        warnings = []
        for key, min_val, max_val in _TIMEOUT_INT_FIELDS:
            value, warn = CoercionService.coerce_int(
                key, out.get(key), getattr(defaults, key),
                min_val=min_val, max_val=max_val,
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
        value, warn = _normalize_runner_timeout_seconds(
            out.get("runner_timeout_seconds"),
            defaults.runner_timeout_seconds,
        )
        out["runner_timeout_seconds"] = value
        if warn:
            warnings.append(warn)
        value, warn = CoercionService.coerce_float(
            "memory_compact_semantic_summary_timeout_seconds",
            out.get("memory_compact_semantic_summary_timeout_seconds"),
            defaults.memory_compact_semantic_summary_timeout_seconds,
            min_val=0.0, max_val=None,
        )
        out["memory_compact_semantic_summary_timeout_seconds"] = value
        if warn:
            warnings.append(warn)
        return out, warnings


# ---------------------------------------------------------------------------
# top-level normalizer (was original _normalize.py)
# ---------------------------------------------------------------------------

_NORMALIZE_SERVICES = (
    ModelFieldsService,
    GatewayFieldsService,
    DaemonFieldsService,
    ToolFieldsService,
    SubagentBasicFieldsService,
    AdapterFieldsService,
    HomeLayoutFieldsService,
    UserFieldsService,
    RuntimeBoolFieldsService,
    TimeoutFieldsService,
    SubagentAdvancedFieldsService,
)


class AgentConfigNormalizer:
    """Service for normalizing all non-memory AgentConfig fields."""

    @staticmethod
    def normalize(data: dict[str, object]) -> tuple[dict[str, object], list[str]]:
        """Validate and coerce all non-memory AgentConfig fields with safe defaults."""
        warnings: list[str] = []
        out: dict[str, object] = dict(data)
        defaults = default_agent_config()

        for service in _NORMALIZE_SERVICES:
            out, service_warnings = service.normalize(out, defaults)
            warnings.extend(service_warnings)

        return out, warnings
