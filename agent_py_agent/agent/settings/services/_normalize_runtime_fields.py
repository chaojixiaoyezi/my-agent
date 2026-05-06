"""Tool, adapter, user, timeout, and advanced subagent config normalizers."""

from __future__ import annotations

import os

from ._coercion import CoercionService


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
        warnings = _apply_int_fields(
            out,
            defaults,
            (
                ("max_tool_rounds", 1, None),
                ("tool_read_max_chars", 100, None),
                ("tool_http_timeout", 1, None),
                ("tool_shell_timeout", 1, None),
            ),
        )
        warnings.extend(_apply_bool_fields(out, defaults, ("stream_enabled",)))
        return out, warnings


class SubagentBasicFieldsService:
    """Normalize basic subagent config fields (max_subagents, memory_top_k)."""

    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        out = dict(data)
        warnings = _apply_int_fields(
            out,
            defaults,
            (("memory_top_k", 0, None), ("max_subagents", 0, None)),
        )
        return out, warnings


class AdapterFieldsService:
    """Normalize adapter-related config fields (feishu, qq, etc.)."""

    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        """Normalize adapter-related config fields."""
        warnings: list[str] = []
        out = dict(data)

        def apply(key: str, coerced: object, warn: str | None) -> None:
            out[key] = coerced
            if warn:
                warnings.append(warn)

        # Feishu string fields pass through as-is
        for key in ("feishu_app_id", "feishu_app_secret", "feishu_verification_token", "feishu_encrypt_key"):
            val = out.get(key, defaults.feishu_app_id if key == "feishu_app_id" else "")
            if isinstance(val, str):
                out[key] = val
            else:
                out[key] = ""

        # feishu_callback_port
        v, w = CoercionService.coerce_int(
            "feishu_callback_port", out.get("feishu_callback_port"),
            defaults.feishu_callback_port, min_val=1024, max_val=65535,
        )
        apply("feishu_callback_port", v, w)

        # QQ fields (support env var override)
        for key in ("qq_app_id", "qq_app_secret"):
            env_key = key.upper()
            env_val = os.environ.get(env_key, "")
            if env_val:
                out[key] = env_val
            else:
                val = out.get(key, defaults.qq_app_id if key == "qq_app_id" else "")
                if isinstance(val, str):
                    out[key] = val
                else:
                    out[key] = ""

        return out, warnings


class UserFieldsService:
    """Normalize user-related config fields."""

    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        """Normalize user-related config fields."""
        warnings: list[str] = []
        out = dict(data)

        def apply(key: str, coerced: object, warn: str | None) -> None:
            out[key] = coerced
            if warn:
                warnings.append(warn)

        # user_id - validate non-empty string
        raw_user_id = out.get("user_id", defaults.user_id)
        if isinstance(raw_user_id, str) and raw_user_id.strip():
            out["user_id"] = raw_user_id.strip()
        else:
            out["user_id"] = defaults.user_id
            warnings.append(f"user_id: expected a non-empty string, got {raw_user_id!r}; using default")

        # user_data_root - validate non-empty string
        raw_user_data_root = out.get("user_data_root", defaults.user_data_root)
        if isinstance(raw_user_data_root, str) and raw_user_data_root.strip():
            out["user_data_root"] = raw_user_data_root.strip()
        else:
            out["user_data_root"] = defaults.user_data_root
            warnings.append(f"user_data_root: expected a non-empty string, got {raw_user_data_root!r}; using default")

        # auth_enabled
        v, w = CoercionService.coerce_bool("auth_enabled", out.get("auth_enabled"), defaults.auth_enabled)
        apply("auth_enabled", v, w)

        # admin_user_id
        raw_admin = out.get("admin_user_id", defaults.admin_user_id)
        if isinstance(raw_admin, str) and raw_admin.strip():
            out["admin_user_id"] = raw_admin.strip()
        else:
            out["admin_user_id"] = defaults.admin_user_id

        return out, warnings


class TimeoutFieldsService:
    """Normalize timeout-related config fields."""

    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        out = dict(data)
        warnings = _apply_int_fields(
            out,
            defaults,
            (
                ("lease_heartbeat_interval_seconds", 10, None),
                ("lease_stale_without_heartbeat_seconds", 30, None),
            ),
        )
        return out, warnings


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
                ("dynamic_timeout_min", 10, None),
                ("dynamic_timeout_max", 60, None),
                ("max_auto_split_depth", 0, None),
                ("max_auto_retry_attempts", 1, 10),
            ),
        )
        value, warn = CoercionService.coerce_float(
            "dynamic_timeout_safety_margin", out.get("dynamic_timeout_safety_margin"),
            defaults.dynamic_timeout_safety_margin, min_val=1.0, max_val=10.0,
        )
        out["dynamic_timeout_safety_margin"] = value
        _append_warning(warnings, warn)
        return out, warnings
