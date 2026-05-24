"""Model, gateway, and daemon config normalization services."""

# LLM: 字段默认值和范围检查直接影响启动参数，改动时覆盖配置边界测试。
# 模块用途: 模型、gateway 和 daemon 核心字段的归一化规则。

from __future__ import annotations

from ._coercion import CoercionService


# LLM: _apply_int_fields 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 把 配置系统 的归一化结果写回配置对象。
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


# LLM: _apply_bool_fields 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 把 配置系统 的归一化结果写回配置对象。
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


# LLM: _apply_choice_field 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 把 配置系统 的归一化结果写回配置对象。
def _apply_choice_field(
    out: dict[str, object],
    defaults: object,
    key: str,
    choices: tuple[str, ...],
) -> list[str]:
    coerced, warn = CoercionService.coerce_choice(key, out.get(key), getattr(defaults, key), choices)
    out[key] = coerced
    return [warn] if warn else []


# LLM: _normalize_temperature 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 把输入值归一成 配置系统 内部使用的稳定格式。
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


# LLM: _temperature_value 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 完成 配置系统 中的 temperature_value 步骤，并保持调用方依赖的数据形状。
def _temperature_value(raw_temp: object) -> float | None:
    if isinstance(raw_temp, str):
        try:
            return float(raw_temp.strip())
        except ValueError:
            return None
    if isinstance(raw_temp, (int, float)):
        return float(raw_temp)
    return None


# LLM: ModelFieldsService 属于 配置系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: ModelFieldsService 封装 配置系统 的一组相关操作，供上层组合调用。
class ModelFieldsService:
    """Normalize model-related config fields."""

    # LLM: ModelFieldsService.normalize 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 归一化 ModelFieldsService 负责的配置字段并追加告警。
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
            _apply_int_fields(out, defaults, (("request_timeout", 1, 600), ("max_tokens", 1, None)))
        )
        warnings.extend(_apply_bool_fields(out, defaults, ("auto_bench_model_on_first_use",)))
        warnings.extend(_normalize_temperature(out, defaults))
        return out, warnings


# LLM: GatewayFieldsService 属于 配置系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: GatewayFieldsService 封装 配置系统 的一组相关操作，供上层组合调用。
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
        ("gateway_request_max_attempts", 0, None),
        ("gateway_port", 0, 65535),
    )

    # LLM: GatewayFieldsService.normalize 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 归一化 GatewayFieldsService 负责的配置字段并追加告警。
    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        """Normalize gateway-related config fields."""
        out = dict(data)
        warnings = _apply_int_fields(out, defaults, GatewayFieldsService._INT_FIELD_SPECS)
        return out, warnings


# LLM: DaemonFieldsService 属于 配置系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: DaemonFieldsService 封装 配置系统 的一组相关操作，供上层组合调用。
class DaemonFieldsService:
    """Normalize daemon-related config fields."""

    # LLM: DaemonFieldsService.normalize 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 归一化 DaemonFieldsService 负责的配置字段并追加告警。
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
                ("daemon_planner", "daemon_apply", "daemon_execute_runners", "daemon_probe"),
            )
        )
        return out, warnings
