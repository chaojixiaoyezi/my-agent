# LLM: 参数登记表的元数据推导（单位、范围、归属模块、读取方），只读、纯函数、进程内缓存一次。
#   单位与范围按结构化规则从键名和现有校验规格推导，读取方用与 test_config_field_readers 同一套
#   属性访问/字符串键正则扫产品代码；推不出的一律留空，绝不手工抄一份或硬猜。改动须同步
#   parameter_registry.py、tooling/user_config_tool.py、gateway_parts/settings_control_service.py
#   与 test_parameter_metadata.py。
# 模块用途: 回答“这个参数以什么为单位、合法范围是多少、主要被哪个模块读取”，是登记表元数据的唯一推导来源。
from __future__ import annotations

import re
from dataclasses import fields
from functools import lru_cache
from pathlib import Path

from ..backends.reasoning_control import REASONING_CONTROLS, REASONING_LEVELS
from ..backends.structured_output_mode import STRUCTURED_OUTPUT_MODES
from ._memory_coercion import _FIELDS as MEMORY_FIELDS
from ._memory_coercion import COMPACT_RECOVERY_PERCENT_RANGE, COMPACT_TRIGGER_PERCENT_RANGE
from .config import AgentConfig
from .decision_settings_defaults import decision_config_fields
from .services._normalize import GatewayFieldsService
from .services.runtime_tool_field_specs import TOOL_INT_FIELDS

_PACKAGE = Path(__file__).resolve().parents[2]
_DEFINITION = _PACKAGE / "agent" / "settings" / "config.py"
# 与 test_config_field_readers 同一套“字段名怎么出现在产品代码里”的正则：属性访问或字符串键。
_REFERENCE = re.compile(r"\.([A-Za-z_]\w*)|[\"']([A-Za-z_]\w*)[\"']")
# 推导模块自身和登记表会以字符串引用所有字段名，扫描时必须排除，否则每个字段都“被 settings 读取”。
_SELF_EXCLUDED = {_DEFINITION, Path(__file__).resolve(), _PACKAGE / "agent" / "settings" / "parameter_registry.py"}

# 单位推导：键名结尾的后缀 → 中文单位；没有匹配的键留空（表示没有单位或不适用）。
_UNIT_SUFFIXES = (
    ("_seconds", "秒"),
    ("_ms", "毫秒"),
    ("_chars", "字符"),
    ("_bytes", "字节"),
    ("_tokens", "tokens"),
    ("_percent", "%"),
    ("_days", "天"),
    ("_hour", "小时"),
    ("_turns", "轮"),
    ("_files", "个文件"),
    ("_requests", "次"),
)


# LLM: 只按键名最后一个完整记号匹配；长后缀优先（_seconds 在 _ms 前），推不出返回空串，不猜别名。
# 函数用途: 从键名后缀推导参数的单位（秒/毫秒/字符/字节/tokens/百分比等）。
def unit_for_key(key: str) -> str:
    name = str(key or "")
    for suffix, unit in _UNIT_SUFFIXES:
        if name.endswith(suffix):
            return unit
    return ""


# ---------------------------------------------------------------------------
# 范围：从现有规范化/校验规格收集（键 → 展示文本），与 normalize 链同一组常量。
# ---------------------------------------------------------------------------

# 其它 Service 的整数/浮点范围（ModelFields、Subagent 组），来源是 settings/services/_normalize.py 的字面量。
_EXTRA_INT_RANGES = {
    "request_timeout": (1, 600),
    "max_tokens": (1, None),
    "model_context_window_tokens": (1, None),
    "memory_top_k": (0, None),
    "max_subagents": (0, None),
    "subagent_hierarchy_max_children_per_tool_call": (0, None),
    "subagent_takeover_chain_max_depth": (0, None),
    "subagent_debug_trace_level": (0, 5),
    "dynamic_timeout_min": (10, None),
    "dynamic_timeout_max": (60, None),
    "feishu_callback_port": (1024, 65535),
}
_EXTRA_FLOAT_RANGES = {
    "estimated_output_tokens_per_second": (1.0, None),
    "top_p": (0.0, 1.0),
}
# 枚举选择（choices）范围：与 _normalize.py 的 _apply_choice_field 同一组枚举。
_CHOICES = {
    "model_backend": ("", "echo", "anthropic_compatible", "openai_compatible", "openai_responses"),
    "model_reasoning_effort": REASONING_LEVELS,
    "model_reasoning_control": REASONING_CONTROLS,
    "model_structured_output": STRUCTURED_OUTPUT_MODES,
    "log_level": ("debug", "info", "warning", "error", "critical"),
    "access_mode": ("restricted", "workspace-write", "full-access"),
    "tool_catalog_mode": ("compact", "full", "retrieval_only", "off"),
    "path_access_mode": ("normal", "full"),
}


# LLM: memory 的 _FIELDS 自带类型与范围；kind=choice 转枚举，compact_*_percent 用与运行时同源的百分比常量，
#   其余 int 用 min/max。只读，返回一个稳定的展示文本表。
# 函数用途: 把 memory 字段的校验规格转换成参数登记表用的范围文本。
def _memory_range_texts() -> dict[str, str]:
    texts: dict[str, str] = {}
    for spec in MEMORY_FIELDS:
        text = _memory_spec_range_text(spec)
        if text:
            texts[spec.field_name] = text
    return texts


# LLM: 单独一层 if 链，避免把 for 与分支叠出过深嵌套；推不出的 kind 返回空串。
# 函数用途: 按 memory 字段的类型/范围种类生成单个字段的范围展示文本。
def _memory_spec_range_text(spec: object) -> str:
    if spec.kind == "choice":
        return "|".join(sorted(spec.choices or ()))
    if spec.kind == "compact_trigger_percent":
        return _range_text(COMPACT_TRIGGER_PERCENT_RANGE[0], COMPACT_TRIGGER_PERCENT_RANGE[1])
    if spec.kind == "compact_recovery_percent":
        return _range_text(COMPACT_RECOVERY_PERCENT_RANGE[0], COMPACT_RECOVERY_PERCENT_RANGE[1])
    if spec.kind == "int":
        return _range_text(spec.min_value, spec.max_value)
    return ""


# LLM: 空界用“≥min / ≤max”表示，两端都有就是“min-max”；整数按整数显示，整数值的浮点
#   （如 0.0/1.0）也显示成整数，避免登记表出现“0.0-1.0”这种噪音。
# 函数用途: 把 (min, max) 界对排成给人看的范围文本。
def _range_text(min_value: object, max_value: object) -> str:
    def _fmt(value: object) -> str:
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)

    if min_value is None and max_value is None:
        return ""
    if max_value is None:
        return f"≥{_fmt(min_value)}"
    if min_value is None:
        return f"≤{_fmt(max_value)}"
    return f"{_fmt(min_value)}-{_fmt(max_value)}"


# LLM: 组装全部范围文本：memory 规格、工具整数表、Gateway 整数/浮点表、其余 Service 字面量与枚举；同键不重复。
# 函数用途: 返回参数登记表用的完整范围文本表（键 → 范围/枚举），推不出的键不在表里。
@lru_cache(maxsize=1)
def ranges() -> dict[str, str]:
    merged: dict[str, str] = {**{key: _range_text(lo, hi) for key, lo, hi in TOOL_INT_FIELDS},
                             **{key: _range_text(lo, hi) for key, lo, hi in GatewayFieldsService._INT_FIELD_SPECS},
                             **{key: _range_text(lo, hi) for key, lo, hi in GatewayFieldsService._FLOAT_FIELD_SPECS},
                             **{key: _range_text(lo, hi) for key, (lo, hi) in _EXTRA_INT_RANGES.items()},
                             **{key: _range_text(lo, hi) for key, (lo, hi) in _EXTRA_FLOAT_RANGES.items()},
                             **_memory_range_texts()}
    for key, choices in _CHOICES.items():
        merged[key] = "|".join(choices)
    return {key: text for key, text in merged.items() if text}


# ---------------------------------------------------------------------------
# 读取方与归属模块：与 test_config_field_readers 同一套引用扫描。
# ---------------------------------------------------------------------------


# LLM: 只读规范任务与产品代码；排除自身模块和定义文件，避免字符串键把“登记表”当成读取方。
#   归属模块取引用最多的文件相对 agent_py_agent 的顶层（agent 包内细分到第二段），读取方返回该文件相对名。
#   决策设置字段（decision_*/memory_decision_*）不直接以属性/字符串键出现在产品代码里，按
#   test_config_field_readers 同一判据用 decision_config_fields() 映射补读取方：读取点在
#   decision_settings_defaults.py（decision_defaults 用 getattr 读这些字段），归属 settings。
# 函数用途: 扫描产品代码，为每个配置字段找到主要读取方模块与归属顶层模块。
@lru_cache(maxsize=1)
def field_readers() -> dict[str, tuple[str, str]]:
    counts: dict[str, dict[str, int]] = {}
    for path in _PACKAGE.rglob("*.py"):
        _count_references(path, counts)
    mapped = {field: ("settings", "agent/settings/decision_settings_defaults")
              for _path, (domain, field) in decision_config_fields().items()
              if domain in {"agent", "memory"}}
    picked = {item.name: _reader_for(item.name, counts, mapped) for item in fields(AgentConfig)}
    return {name: reader for name, reader in picked.items() if reader is not None}


# LLM: 跳过测试目录与自身/定义文件；读不出的文件忽略（只影响展示，不影响任何判定）。副作用：只往 counts 累加引用次数。
# 函数用途: 统计一个源码文件里每个属性名/字符串键被引用的次数。
def _count_references(path: Path, counts: dict[str, dict[str, int]]) -> None:
    if "tests" in path.relative_to(_PACKAGE).parts or path.resolve() in _SELF_EXCLUDED:
        return
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return
    relative = str(path.relative_to(_PACKAGE).with_suffix(""))
    for attribute, key in _REFERENCE.findall(text):
        per_module = counts.setdefault(attribute or key, {})
        per_module[relative] = per_module.get(relative, 0) + 1


# LLM: 引用最多的文件为读取方；没有直接引用的决策字段用 decision_config_fields 映射补；都没有返回 None。只读。
# 函数用途: 为一个配置字段选出主要读取方与归属模块。
def _reader_for(name: str, counts: dict[str, dict[str, int]], mapped: dict[str, tuple[str, str]]) -> tuple[str, str] | None:
    per_module = counts.get(name)
    if per_module:
        reader = max(per_module, key=per_module.get)
        return _owner_module(reader), reader
    return mapped.get(name)


# LLM: agent 包内文件多，归属模块细分到 agent/ 的第二段（conversation、agent_core、settings…），
#   其它顶层（cli、scripts、frontend）保持第一段。只读。
# 函数用途: 从一个读取方文件相对名算出它所属的顶层模块名。
def _owner_module(relative: str) -> str:
    parts = relative.split("/")
    if len(parts) >= 2 and parts[0] == "agent":
        return parts[1]
    return parts[0] if parts else ""


__all__ = ["field_readers", "ranges", "unit_for_key"]