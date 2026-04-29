from __future__ import annotations

"""LLM: normalize memory-related runtime config with safe defaults and fallback warnings.

给人看的解释：
用户会手动改配置文件，所以这里专门负责把 memory 配置“洗干净”。
比如用户把数字写成 abcd、把开关写成乱码，程序不能崩，也不能把权限越放越大。
我们会回到保守默认值，并把原因记录成 warning，后面 memory doctor 可以拿这些 warning 提醒用户。
"""

from dataclasses import asdict, dataclass
import re
from typing import Any, Mapping

__all__ = [
    "MemoryConfigWarning",
    "MemorySettings",
    "normalize_agent_memory_config",
    "normalize_memory_settings",
]


_MISSING = object()
_INT_PATTERN = re.compile(r"-?[0-9]+")


@dataclass(frozen=True)
class MemorySettings:
    """LLM: effective memory config after validation and fallback normalization.

    给人看的解释：
    这是 memory 系统真正会使用的配置，不直接相信配置文件里的原始文本。
    比如配置文件写 `memory_archive_level: abcd`，这里最后仍然会得到默认的 3。
    """

    memory_archive_level: int = 3
    memory_hook_enabled: bool = True
    memory_hook_archive_level: int = 3
    memory_hook_retention_days: int = 7
    memory_rule_routing_enabled: bool = True
    memory_rule_routing_mode: str = "soft"
    memory_rule_auto_read_limit: int = 3
    memory_rule_receipt_enabled: bool = True


@dataclass(frozen=True)
class MemoryConfigWarning:
    """LLM: structured warning emitted when a memory config value falls back to default.

    给人看的解释：
    这不是程序报错，而是“我发现用户写的配置不靠谱，所以帮他用了默认值”。
    后面做配置体检时，可以把这些 warning 展示出来，让用户知道哪一项写错了。
    """

    field_name: str
    raw_value: Any
    fallback_value: Any
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """LLM: return a JSON-serializable warning payload for doctor/log output.

        给人看的解释：
        把 warning 变成普通字典，后面无论是写日志、写 JSON，还是在 CLI 里展示都方便。
        """

        return asdict(self)


def normalize_memory_settings(values: Mapping[str, Any] | object | None = None) -> tuple[MemorySettings, list[MemoryConfigWarning]]:
    """LLM: coerce raw memory config fields into effective MemorySettings plus fallback warnings.

    给人看的解释：
    这个函数是 memory 配置的“安检口”。
    它会逐项检查：等级是不是 0-3，天数是不是非负整数，模式是不是 off/soft/strict。
    写对了就采用，写错了就回到默认值，并告诉调用方“哪一项为什么被回退”。
    """

    source = values if values is not None else {}
    warnings: list[MemoryConfigWarning] = []
    defaults = MemorySettings()

    settings = MemorySettings(
        memory_archive_level=_coerce_int(
            "memory_archive_level",
            _lookup(source, "memory_archive_level"),
            default=defaults.memory_archive_level,
            min_value=0,
            max_value=3,
            warnings=warnings,
        ),
        memory_hook_enabled=_coerce_bool(
            "memory_hook_enabled",
            _lookup(source, "memory_hook_enabled"),
            default=defaults.memory_hook_enabled,
            warnings=warnings,
        ),
        memory_hook_archive_level=_coerce_int(
            "memory_hook_archive_level",
            _lookup(source, "memory_hook_archive_level"),
            default=defaults.memory_hook_archive_level,
            min_value=0,
            max_value=3,
            warnings=warnings,
        ),
        memory_hook_retention_days=_coerce_int(
            "memory_hook_retention_days",
            _lookup(source, "memory_hook_retention_days"),
            default=defaults.memory_hook_retention_days,
            min_value=0,
            max_value=None,
            warnings=warnings,
        ),
        memory_rule_routing_enabled=_coerce_bool(
            "memory_rule_routing_enabled",
            _lookup(source, "memory_rule_routing_enabled"),
            default=defaults.memory_rule_routing_enabled,
            warnings=warnings,
        ),
        memory_rule_routing_mode=_coerce_choice(
            "memory_rule_routing_mode",
            _lookup(source, "memory_rule_routing_mode"),
            default=defaults.memory_rule_routing_mode,
            choices={"off", "soft", "strict"},
            warnings=warnings,
        ),
        memory_rule_auto_read_limit=_coerce_int(
            "memory_rule_auto_read_limit",
            _lookup(source, "memory_rule_auto_read_limit"),
            default=defaults.memory_rule_auto_read_limit,
            min_value=0,
            max_value=None,
            warnings=warnings,
        ),
        memory_rule_receipt_enabled=_coerce_bool(
            "memory_rule_receipt_enabled",
            _lookup(source, "memory_rule_receipt_enabled"),
            default=defaults.memory_rule_receipt_enabled,
            warnings=warnings,
        ),
    )

    return settings, warnings


def normalize_agent_memory_config(config: object) -> list[MemoryConfigWarning]:
    """LLM: mutate an AgentConfig-like object so memory fields always hold safe effective values.

    给人看的解释：
    `load_config()` 读完 YAML 后会调用这里。
    这样外面的业务代码拿到 `config.memory_archive_level` 时，不需要再担心它是 abcd、负数或奇怪注入字符串。
    """

    settings, warnings = normalize_memory_settings(config)
    for field_name, value in asdict(settings).items():
        setattr(config, field_name, value)
    setattr(config, "memory_config_warnings", [warning.to_dict() for warning in warnings])
    return warnings


def _lookup(source: Mapping[str, Any] | object, field_name: str) -> Any:
    """LLM: read a raw config field from a mapping or dataclass-like object.

    给人看的解释：
    测试里可能直接传字典，正式启动时会传 `AgentConfig` 对象。
    这个小函数统一取值方式，字段没出现就返回一个内部的“缺失”标记。
    """

    if isinstance(source, Mapping):
        return source.get(field_name, _MISSING)
    return getattr(source, field_name, _MISSING)


def _warn(
    warnings: list[MemoryConfigWarning],
    field_name: str,
    raw_value: Any,
    fallback_value: Any,
    reason: str,
) -> None:
    """LLM: append one structured fallback warning.

    给人看的解释：
    每次配置被回退，都在这里登记一下。
    我们不把错误悄悄吞掉，后面 CLI 或 doctor 可以清楚告诉用户：这一项写坏了，所以用了哪个默认值。
    """

    warnings.append(
        MemoryConfigWarning(
            field_name=field_name,
            raw_value=raw_value,
            fallback_value=fallback_value,
            reason=reason,
        )
    )


def _coerce_bool(
    field_name: str,
    raw_value: Any,
    *,
    default: bool,
    warnings: list[MemoryConfigWarning],
) -> bool:
    """LLM: parse a boolean config value and reject ambiguous or injected strings.

    给人看的解释：
    布尔开关只认明确的 true/false、yes/no、on/off、1/0。
    像 `maybe`、`false; rm -rf`、乱码这类内容，一律不猜，直接回默认值。
    """

    if raw_value is _MISSING:
        return default
    if isinstance(raw_value, bool):
        return raw_value
    if isinstance(raw_value, int) and raw_value in {0, 1}:
        return bool(raw_value)
    if isinstance(raw_value, str):
        normalized = raw_value.strip().lower()
        if normalized in {"true", "yes", "on", "1"}:
            return True
        if normalized in {"false", "no", "off", "0"}:
            return False
    _warn(warnings, field_name, raw_value, default, "expected a clear boolean value")
    return default


def _coerce_choice(
    field_name: str,
    raw_value: Any,
    *,
    default: str,
    choices: set[str],
    warnings: list[MemoryConfigWarning],
) -> str:
    """LLM: parse an enum-like string config value against an allowlist.

    给人看的解释：
    路由模式只能是 off、soft、strict。
    这里不用“看起来差不多就算”的模糊判断，避免用户写错后系统进入意外模式。
    """

    if raw_value is _MISSING:
        return default
    if isinstance(raw_value, str):
        normalized = raw_value.strip().lower()
        if normalized in choices:
            return normalized
    _warn(warnings, field_name, raw_value, default, f"expected one of {sorted(choices)}")
    return default


def _coerce_int(
    field_name: str,
    raw_value: Any,
    *,
    default: int,
    min_value: int,
    max_value: int | None,
    warnings: list[MemoryConfigWarning],
) -> int:
    """LLM: parse an integer config value using ASCII digits and closed numeric bounds.

    给人看的解释：
    数字项只接受普通阿拉伯数字，比如 0、3、7。
    小数、中文数字、全角数字、脚本片段、负数越界都会回默认值，避免配置文件把系统带偏。
    """

    if raw_value is _MISSING:
        return default
    if isinstance(raw_value, bool):
        _warn(warnings, field_name, raw_value, default, "expected an integer, not a boolean")
        return default
    if isinstance(raw_value, int):
        number = raw_value
    elif isinstance(raw_value, str) and _INT_PATTERN.fullmatch(raw_value.strip()):
        number = int(raw_value.strip())
    else:
        _warn(warnings, field_name, raw_value, default, "expected an integer")
        return default

    if number < min_value:
        _warn(warnings, field_name, raw_value, default, f"expected value >= {min_value}")
        return default
    if max_value is not None and number > max_value:
        _warn(warnings, field_name, raw_value, default, f"expected value <= {max_value}")
        return default
    return number
