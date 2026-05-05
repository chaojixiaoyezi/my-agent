"""Tool, adapter, user, timeout, and advanced subagent config normalizers."""

from __future__ import annotations

import os

from ._coercion import CoercionService


class ToolFieldsService:
    """Normalize tool-related config fields."""

    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        """Normalize tool-related config fields."""
        warnings: list[str] = []
        out = dict(data)

        def apply(key: str, coerced: object, warn: str | None) -> None:
            out[key] = coerced
            if warn:
                warnings.append(warn)

        # max_tool_rounds
        v, w = CoercionService.coerce_int(
            "max_tool_rounds", out.get("max_tool_rounds"),
            defaults.max_tool_rounds, min_val=1,
        )
        apply("max_tool_rounds", v, w)

        # tool_read_max_chars
        v, w = CoercionService.coerce_int(
            "tool_read_max_chars", out.get("tool_read_max_chars"),
            defaults.tool_read_max_chars, min_val=100,
        )
        apply("tool_read_max_chars", v, w)

        # tool_http_timeout
        v, w = CoercionService.coerce_int(
            "tool_http_timeout", out.get("tool_http_timeout"),
            defaults.tool_http_timeout, min_val=1,
        )
        apply("tool_http_timeout", v, w)

        # tool_shell_timeout
        v, w = CoercionService.coerce_int(
            "tool_shell_timeout", out.get("tool_shell_timeout"),
            defaults.tool_shell_timeout, min_val=1,
        )
        apply("tool_shell_timeout", v, w)

        # stream_enabled
        v, w = CoercionService.coerce_bool(
            "stream_enabled", out.get("stream_enabled"), defaults.stream_enabled,
        )
        apply("stream_enabled", v, w)

        return out, warnings


class SubagentBasicFieldsService:
    """Normalize basic subagent config fields (max_subagents, memory_top_k)."""

    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        """Normalize basic subagent config fields."""
        warnings: list[str] = []
        out = dict(data)

        def apply(key: str, coerced: object, warn: str | None) -> None:
            out[key] = coerced
            if warn:
                warnings.append(warn)

        # memory_top_k
        v, w = CoercionService.coerce_int(
            "memory_top_k", out.get("memory_top_k"),
            defaults.memory_top_k, min_val=0,
        )
        apply("memory_top_k", v, w)

        # max_subagents
        v, w = CoercionService.coerce_int(
            "max_subagents", out.get("max_subagents"),
            defaults.max_subagents, min_val=0,
        )
        apply("max_subagents", v, w)

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
        """Normalize timeout-related config fields."""
        warnings: list[str] = []
        out = dict(data)

        def apply(key: str, coerced: object, warn: str | None) -> None:
            out[key] = coerced
            if warn:
                warnings.append(warn)

        # lease_heartbeat_interval_seconds
        v, w = CoercionService.coerce_int(
            "lease_heartbeat_interval_seconds", out.get("lease_heartbeat_interval_seconds"),
            defaults.lease_heartbeat_interval_seconds, min_val=10,
        )
        apply("lease_heartbeat_interval_seconds", v, w)

        # lease_stale_without_heartbeat_seconds
        v, w = CoercionService.coerce_int(
            "lease_stale_without_heartbeat_seconds", out.get("lease_stale_without_heartbeat_seconds"),
            defaults.lease_stale_without_heartbeat_seconds, min_val=30,
        )
        apply("lease_stale_without_heartbeat_seconds", v, w)

        return out, warnings


class SubagentAdvancedFieldsService:
    """Normalize advanced subagent config fields."""

    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        """Normalize advanced subagent config fields."""
        warnings: list[str] = []
        out = dict(data)

        def apply(key: str, coerced: object, warn: str | None) -> None:
            out[key] = coerced
            if warn:
                warnings.append(warn)

        # subagent_automation_level
        v, w = CoercionService.coerce_int(
            "subagent_automation_level", out.get("subagent_automation_level"),
            defaults.subagent_automation_level, min_val=1, max_val=3,
        )
        apply("subagent_automation_level", v, w)

        # dynamic_timeout_safety_margin
        v, w = CoercionService.coerce_float(
            "dynamic_timeout_safety_margin", out.get("dynamic_timeout_safety_margin"),
            defaults.dynamic_timeout_safety_margin, min_val=1.0, max_val=10.0,
        )
        apply("dynamic_timeout_safety_margin", v, w)

        # dynamic_timeout_min
        v, w = CoercionService.coerce_int(
            "dynamic_timeout_min", out.get("dynamic_timeout_min"),
            defaults.dynamic_timeout_min, min_val=10,
        )
        apply("dynamic_timeout_min", v, w)

        # dynamic_timeout_max
        v, w = CoercionService.coerce_int(
            "dynamic_timeout_max", out.get("dynamic_timeout_max"),
            defaults.dynamic_timeout_max, min_val=60,
        )
        apply("dynamic_timeout_max", v, w)

        # max_auto_split_depth
        v, w = CoercionService.coerce_int(
            "max_auto_split_depth", out.get("max_auto_split_depth"),
            defaults.max_auto_split_depth, min_val=0,
        )
        apply("max_auto_split_depth", v, w)

        # max_auto_retry_attempts
        v, w = CoercionService.coerce_int(
            "max_auto_retry_attempts", out.get("max_auto_retry_attempts"),
            defaults.max_auto_retry_attempts, min_val=1, max_val=10,
        )
        apply("max_auto_retry_attempts", v, w)

        return out, warnings
