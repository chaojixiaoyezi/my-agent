"""Tool, adapter, user, timeout, and advanced subagent config normalizers."""


from __future__ import annotations

from ...path_access_policy import normalize_path_access_mode
from ._coercion import CoercionService
from .runtime_tool_field_specs import TOOL_INT_FIELDS


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
            key,
            out.get(key),
            getattr(defaults, key),
            min_val=min_val,
            max_val=max_val,
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


class ToolFieldsService:
    """Normalize tool-related config fields."""

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


def _normalize_tool_int_fields(out: dict[str, object], defaults: object) -> list[str]:
    return _apply_int_fields(out, defaults, TOOL_INT_FIELDS)


def _normalize_tool_bool_fields(out: dict[str, object], defaults: object) -> list[str]:
    return _apply_bool_fields(
        out,
        defaults,
        ("stream_enabled", "tool_catalog_include_examples", "tool_catalog_show_truncated_notice"),
    )


def _normalize_tool_catalog_fields(out: dict[str, object], defaults: object) -> list[str]:
    warnings: list[str] = []
    mode, warn = CoercionService.coerce_choice(
        "tool_catalog_mode",
        out.get("tool_catalog_mode"),
        defaults.tool_catalog_mode,
        choices=("compact", "full", "retrieval_only", "off"),
    )
    out["tool_catalog_mode"] = mode
    _append_warning(warnings, warn)
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
    known_values = {"normal", "restricted", "workspace-write", "full", "full-access", "all", "open", ""}
    if normalized_raw not in known_values:
        warnings.append(
            f"path_access_mode: unknown value {raw_mode!r}; using {mode!r}"
        )
    out["path_access_mode"] = mode
    roots = _normalize_string_list(out.get("path_dangerous_roots", defaults.path_dangerous_roots))
    out["path_dangerous_roots"] = roots or list(defaults.path_dangerous_roots)
    return warnings


def _normalize_command_access_mode(out: dict[str, object], defaults: object) -> list[str]:
    warnings: list[str] = []
    raw = out.get("access_mode", defaults.access_mode)
    normalized_raw = str(raw).strip().lower().replace("_", "-") if raw is not None else ""
    mode, warn = CoercionService.coerce_choice(
        "access_mode",
        normalized_raw,
        defaults.access_mode,
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
        min_val=0.0,
        max_val=None,
    )
    out["dispatch_default_watch_interval"] = value
    _append_warning(warnings, warn)
    return warnings


class SubagentBasicFieldsService:
    """Normalize basic subagent config fields (max_subagents, memory_top_k)."""

    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        out = dict(data)
        warnings = _apply_int_fields(
            out,
            defaults,
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
            "subagent_mode",
            out.get("subagent_mode"),
            defaults.subagent_mode,
            choices=("trusted_local_hardening", "balanced", "strict"),
        )
        out["subagent_mode"] = mode
        _append_warning(warnings, warn)
        return out, warnings


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


class SubagentAdvancedFieldsService:
    """Normalize advanced subagent config fields."""

    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        out = dict(data)
        warnings = _apply_int_fields(
            out,
            defaults,
            (
                ("subagent_automation_level", 1, 3),
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
            out,
            defaults,
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
