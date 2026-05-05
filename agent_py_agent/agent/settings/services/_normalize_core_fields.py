"""Model, gateway, and daemon config normalization services."""

from __future__ import annotations

from ._coercion import CoercionService


def _apply_int_fields(
    out: dict[str, object],
    defaults: object,
    specs: tuple[tuple[str, int | None, int | None], ...],
) -> list[str]:
    """Coerce a group of integer fields and return warnings."""
    warnings: list[str] = []
    for key, min_val, max_val in specs:
        coerced, warn = CoercionService.coerce_int(
            key,
            out.get(key),
            getattr(defaults, key),
            min_val=min_val,
            max_val=max_val,
        )
        out[key] = coerced
        if warn:
            warnings.append(warn)
    return warnings


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

    _INT_FIELD_SPECS = (
        ("gateway_heartbeat_interval", 5, None),
        ("gateway_stale_seconds", 30, None),
        ("gateway_stop_timeout", 1, None),
        ("gateway_request_timeout", 1, None),
        ("gateway_request_poll_interval", 1, None),
        ("gateway_request_workers", 1, None),
        ("gateway_processing_timeout_seconds", 30, None),
        ("gateway_request_max_attempts", 1, None),
        ("gateway_port", 0, 65535),
    )

    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        """Normalize gateway-related config fields."""
        out = dict(data)
        warnings = _apply_int_fields(out, defaults, GatewayFieldsService._INT_FIELD_SPECS)
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
