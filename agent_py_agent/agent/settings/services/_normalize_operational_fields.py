"""Operational config normalization services."""


from __future__ import annotations

from ._coercion import CoercionService


class RuntimeBoolFieldsService:
    """Normalize cross-cutting runtime boolean switches."""

    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        out = dict(data)
        warnings = _normalize_runtime_bools(out, defaults)
        warnings.extend(_normalize_runtime_choices(out, defaults))
        return out, warnings


def _normalize_runtime_bools(out: dict[str, object], defaults: object) -> list[str]:
    warnings: list[str] = []
    for key in _RUNTIME_BOOL_FIELDS:
        value, warn = CoercionService.coerce_bool(key, out.get(key), getattr(defaults, key))
        out[key] = value
        if warn:
            warnings.append(warn)
    return warnings


def _normalize_runtime_choices(out: dict[str, object], defaults: object) -> list[str]:
    value, warn = CoercionService.coerce_choice(
        "log_level",
        out.get("log_level"),
        defaults.log_level,
        choices=("debug", "info", "warning", "error", "critical"),
    )
    out["log_level"] = value
    return [warn] if warn else []


_RUNTIME_BOOL_FIELDS = (
    "auto_detect_work_on_startup",
    "auto_save_memory",
    "local_store_fts_enabled",
    "enable_self_learning",
    "enable_subagents",
    "notification_enabled",
    "concurrency_lock_enabled",
    "audit_enabled",
    "watchdog_enabled",
)
