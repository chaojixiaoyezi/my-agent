"""Operational config normalization services."""

# LLM: Operational switches live outside domain-specific normalizers to keep each config service small.
# 模块用途: 归一化跨模块运行期开关，比如保存、子代理、审计、watchdog 和日志级别。

from __future__ import annotations

from ._coercion import CoercionService


# LLM: RuntimeBoolFieldsService keeps operational feature switches from staying truthy string values.
# 类用途: 统一归一化运行期布尔开关，避免配置文件里写 "false" 时业务代码仍按真值执行。
class RuntimeBoolFieldsService:
    """Normalize cross-cutting runtime boolean switches."""

    # LLM: RuntimeBoolFieldsService.normalize belongs to the config pipeline; keep field coverage aligned with AgentConfig.
    # 函数用途: 把运行期布尔配置转换成真正的 bool，避免后续每个调用点重复判断字符串。
    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        out = dict(data)
        warnings = _normalize_runtime_bools(out, defaults)
        warnings.extend(_normalize_runtime_choices(out, defaults))
        return out, warnings


# LLM: _normalize_runtime_bools uses explicit field names so new switches cannot silently skip coercion.
# 函数用途: 批量归一化运行期布尔字段，并返回用户可见配置告警。
def _normalize_runtime_bools(out: dict[str, object], defaults: object) -> list[str]:
    warnings: list[str] = []
    for key in _RUNTIME_BOOL_FIELDS:
        value, warn = CoercionService.coerce_bool(key, out.get(key), getattr(defaults, key))
        out[key] = value
        if warn:
            warnings.append(warn)
    return warnings


# LLM: _normalize_runtime_choices keeps string policy fields from drifting into unsupported values.
# 函数用途: 归一化运行期枚举配置；目前 log_level 会直接影响包内 logger。
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
