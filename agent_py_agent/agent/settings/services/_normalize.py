"""Domain-specific normalize services for config fields."""

from __future__ import annotations

import os

from ._coercion import CoercionService


class ModelFieldsService:
    """Normalize model-related config fields."""

    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        """Normalize model-related config fields."""
        warnings: list[str] = []
        out = dict(data)

        def apply(key: str, coerced: object, warn: str | None) -> None:
            out[key] = coerced
            if warn:
                warnings.append(warn)

        # model_backend
        v, w = CoercionService.coerce_choice(
            "model_backend", out.get("model_backend"), defaults.model_backend,
            ("echo", "anthropic_compatible", "openai_compatible"),
        )
        apply("model_backend", v, w)

        # request_timeout
        v, w = CoercionService.coerce_int(
            "request_timeout", out.get("request_timeout"), defaults.request_timeout,
            min_val=1, max_val=600,
        )
        apply("request_timeout", v, w)

        # max_tokens
        v, w = CoercionService.coerce_int(
            "max_tokens", out.get("max_tokens"), defaults.max_tokens, min_val=1,
        )
        apply("max_tokens", v, w)

        # temperature (stored as str in AgentConfig, but validate as float)
        raw_temp = out.get("temperature", defaults.temperature)
        if isinstance(raw_temp, str):
            try:
                temp_val = float(raw_temp.strip())
                if 0.0 <= temp_val <= 2.0:
                    out["temperature"] = raw_temp.strip()
                else:
                    warnings.append(f"temperature: expected 0.0-2.0, got {temp_val}; using {defaults.temperature}")
                    out["temperature"] = defaults.temperature
            except ValueError:
                warnings.append(f"temperature: expected a float string, got {raw_temp!r}; using {defaults.temperature}")
                out["temperature"] = defaults.temperature
        elif isinstance(raw_temp, (int, float)):
            temp_val = float(raw_temp)
            if 0.0 <= temp_val <= 2.0:
                out["temperature"] = str(temp_val)
            else:
                warnings.append(f"temperature: expected 0.0-2.0, got {temp_val}; using {defaults.temperature}")
                out["temperature"] = defaults.temperature

        return out, warnings


class GatewayFieldsService:
    """Normalize gateway-related config fields."""

    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        """Normalize gateway-related config fields."""
        warnings: list[str] = []
        out = dict(data)

        def apply(key: str, coerced: object, warn: str | None) -> None:
            out[key] = coerced
            if warn:
                warnings.append(warn)

        # gateway_heartbeat_interval
        v, w = CoercionService.coerce_int(
            "gateway_heartbeat_interval", out.get("gateway_heartbeat_interval"),
            defaults.gateway_heartbeat_interval, min_val=5,
        )
        apply("gateway_heartbeat_interval", v, w)

        # gateway_stale_seconds
        v, w = CoercionService.coerce_int(
            "gateway_stale_seconds", out.get("gateway_stale_seconds"),
            defaults.gateway_stale_seconds, min_val=30,
        )
        apply("gateway_stale_seconds", v, w)

        # gateway_stop_timeout
        v, w = CoercionService.coerce_int(
            "gateway_stop_timeout", out.get("gateway_stop_timeout"),
            defaults.gateway_stop_timeout, min_val=1,
        )
        apply("gateway_stop_timeout", v, w)

        # gateway_request_timeout
        v, w = CoercionService.coerce_int(
            "gateway_request_timeout", out.get("gateway_request_timeout"),
            defaults.gateway_request_timeout, min_val=1,
        )
        apply("gateway_request_timeout", v, w)

        # gateway_request_poll_interval
        v, w = CoercionService.coerce_int(
            "gateway_request_poll_interval", out.get("gateway_request_poll_interval"),
            defaults.gateway_request_poll_interval, min_val=1,
        )
        apply("gateway_request_poll_interval", v, w)

        # gateway_request_workers
        v, w = CoercionService.coerce_int(
            "gateway_request_workers", out.get("gateway_request_workers"),
            defaults.gateway_request_workers, min_val=1,
        )
        apply("gateway_request_workers", v, w)

        # gateway_processing_timeout_seconds
        v, w = CoercionService.coerce_int(
            "gateway_processing_timeout_seconds", out.get("gateway_processing_timeout_seconds"),
            defaults.gateway_processing_timeout_seconds, min_val=30,
        )
        apply("gateway_processing_timeout_seconds", v, w)

        # gateway_request_max_attempts
        v, w = CoercionService.coerce_int(
            "gateway_request_max_attempts", out.get("gateway_request_max_attempts"),
            defaults.gateway_request_max_attempts, min_val=1,
        )
        apply("gateway_request_max_attempts", v, w)

        # gateway_port
        v, w = CoercionService.coerce_int(
            "gateway_port", out.get("gateway_port"),
            defaults.gateway_port, min_val=0, max_val=65535,
        )
        apply("gateway_port", v, w)

        return out, warnings


class DaemonFieldsService:
    """Normalize daemon-related config fields."""

    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        """Normalize daemon-related config fields."""
        warnings: list[str] = []
        out = dict(data)

        def apply(key: str, coerced: object, warn: str | None) -> None:
            out[key] = coerced
            if warn:
                warnings.append(warn)

        # daemon_interval
        v, w = CoercionService.coerce_int(
            "daemon_interval", out.get("daemon_interval"),
            defaults.daemon_interval, min_val=1,
        )
        apply("daemon_interval", v, w)

        # daemon_limit
        v, w = CoercionService.coerce_int(
            "daemon_limit", out.get("daemon_limit"),
            defaults.daemon_limit, min_val=0,
        )
        apply("daemon_limit", v, w)

        # daemon_max_cycles
        v, w = CoercionService.coerce_int(
            "daemon_max_cycles", out.get("daemon_max_cycles"),
            defaults.daemon_max_cycles, min_val=0,
        )
        apply("daemon_max_cycles", v, w)

        # daemon_max_cards
        v, w = CoercionService.coerce_int(
            "daemon_max_cards", out.get("daemon_max_cards"),
            defaults.daemon_max_cards, min_val=0,
        )
        apply("daemon_max_cards", v, w)

        # daemon_apply
        v, w = CoercionService.coerce_bool(
            "daemon_apply", out.get("daemon_apply"), defaults.daemon_apply,
        )
        apply("daemon_apply", v, w)

        # daemon_execute_runners
        v, w = CoercionService.coerce_bool(
            "daemon_execute_runners", out.get("daemon_execute_runners"), defaults.daemon_execute_runners,
        )
        apply("daemon_execute_runners", v, w)

        return out, warnings


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


class AgentConfigNormalizer:
    """Service for normalizing all non-memory AgentConfig fields."""

    @staticmethod
    def normalize(data: dict[str, object]) -> tuple[dict[str, object], list[str]]:
        """Validate and coerce all non-memory AgentConfig fields with safe fallbacks."""
        from ..config import AgentConfig
        warnings: list[str] = []
        out: dict[str, object] = dict(data)

        defaults = AgentConfig()

        # Apply per-domain normalization in sequence
        out, w1 = ModelFieldsService.normalize(out, defaults)
        warnings.extend(w1)

        out, w2 = GatewayFieldsService.normalize(out, defaults)
        warnings.extend(w2)

        out, w3 = DaemonFieldsService.normalize(out, defaults)
        warnings.extend(w3)

        out, w4 = ToolFieldsService.normalize(out, defaults)
        warnings.extend(w4)

        out, w5 = SubagentBasicFieldsService.normalize(out, defaults)
        warnings.extend(w5)

        out, w6 = AdapterFieldsService.normalize(out, defaults)
        warnings.extend(w6)

        out, w7 = UserFieldsService.normalize(out, defaults)
        warnings.extend(w7)

        out, w8 = TimeoutFieldsService.normalize(out, defaults)
        warnings.extend(w8)

        out, w9 = SubagentAdvancedFieldsService.normalize(out, defaults)
        warnings.extend(w9)

        return out, warnings