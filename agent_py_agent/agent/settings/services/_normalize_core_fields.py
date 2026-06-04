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


def _apply_bool_fields(
    out: dict[str, object],
    defaults: object,
    keys: tuple[str, ...],
) -> list[str]:
    warnings: list[str] = []
    for key in keys:
        coerced, warn = CoercionService.coerce_bool(key, out.get(key), getattr(defaults, key))
        out[key] = coerced
        if warn:
            warnings.append(warn)
    return warnings


def _apply_choice_field(
    out: dict[str, object],
    defaults: object,
    key: str,
    choices: tuple[str, ...],
) -> list[str]:
    coerced, warn = CoercionService.coerce_choice(key, out.get(key), getattr(defaults, key), choices)
    out[key] = coerced
    return [warn] if warn else []


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


class ModelFieldsService:
    """Normalize model-related config fields."""

    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        out = dict(data)
        warnings = _apply_choice_field(
            out,
            defaults,
            "model_backend",
            ("echo", "anthropic_compatible", "openai_compatible"),
        )
        warnings.extend(
            _apply_int_fields(
                out,
                defaults,
                (
                    ("request_timeout", 1, 600),
                    ("max_tokens", 1, None),
                    ("model_context_window_tokens", 1, None),
                ),
            )
        )
        warnings.extend(_apply_bool_fields(out, defaults, ("auto_bench_model_on_first_use",)))
        warnings.extend(_normalize_temperature(out, defaults))
        return out, warnings


class GatewayFieldsService:
    """Normalize gateway-related config fields."""

    _INT_FIELD_SPECS = (
        ("gateway_heartbeat_interval", 5, None),
        ("gateway_stale_seconds", 30, None),
        ("gateway_stop_timeout", 1, None),
        ("gateway_request_timeout", 1, None),
        ("gateway_request_poll_interval", 1, None),
        ("gateway_request_workers", 3, None),
        ("gateway_processing_timeout_seconds", 30, None),
        ("gateway_request_max_attempts", 0, None),
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
        out = dict(data)
        warnings = _apply_int_fields(
            out,
            defaults,
            (
                ("daemon_interval", 1, None),
                ("daemon_limit", 0, None),
                ("daemon_max_cycles", 0, None),
                ("daemon_max_cards", 0, None),
            ),
        )
        warnings.extend(
            _apply_bool_fields(
                out,
                defaults,
                ("daemon_planner", "daemon_mutate_state", "daemon_start_runners", "daemon_probe"),
            )
        )
        return out, warnings
