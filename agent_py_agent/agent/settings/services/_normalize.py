# LLM: 各配置域共用默认和校验；文件语法反馈、Compact原文回查与决策跳过记录开关独立归一为布尔值，须同步各自配置加载测试；规范化不发请求或写日志。
# 模块用途: 将 YAML/覆盖配置转成运行字段，避免字符串 false 意外启用工具反馈、回查提示或决策记录。
"""Domain-specific normalize services for config fields.

Was split across _normalize_core_fields / _normalize_home_fields /
_normalize_identity_fields / _normalize_operational_fields /
_normalize_runtime_fields / _normalize_timeout_fields — now merged.
"""

from __future__ import annotations

import math
import os
from dataclasses import fields
from functools import cache

from ...backends.reasoning_control import REASONING_CONTROLS, REASONING_LEVELS
from ...backends.sampling import validate_top_p
from ...backends.structured_output_mode import STRUCTURED_OUTPUT_MODES
from ...path_access_policy import normalize_path_access_mode
from ..defaults import default_agent_config
from ..user_config_capability import is_credential_key
from ._coercion import CoercionService
from .runtime_tool_field_specs import TOOL_INT_FIELDS

# ---------------------------------------------------------------------------
# shared coercion helpers (canonical versions, was duplicated across files)
# ---------------------------------------------------------------------------

def _append_warning(warnings: list[str], warn: str | None) -> None:
    if warn:
        warnings.append(warn)


# LLM: 告警会经 /settings 显示给用户（TUI 与飞书），原样回显配置值会泄露凭据；键在生成告警时本来就知道，
#   所以在这里判类型而不是在展示层解析文字。凭据键不回显值，其他键截短。
# 函数用途: 把要写进告警的配置值转成可安全展示的文字。
_MAX_WARNING_VALUE_CHARS = 80


def describe_raw_value(key: object, value: object) -> str:
    if is_credential_key(key):
        return "已隐藏（凭据不显示原值）"
    text = repr(value)
    if len(text) <= _MAX_WARNING_VALUE_CHARS:
        return text
    return text[:_MAX_WARNING_VALUE_CHARS] + "…（已截断）"


def _apply_int_fields(
    out: dict[str, object],
    defaults: object,
    specs: tuple[tuple[str, int | None, int | None], ...],
) -> list[str]:
    warnings: list[str] = []
    for key, min_val, max_val in specs:
        value, warn = CoercionService.coerce_int(
            key, out.get(key), getattr(defaults, key),
            min_val=min_val, max_val=max_val,
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


def _apply_float_fields(
    out: dict[str, object],
    defaults: object,
    specs: tuple[tuple[str, float | None, float | None], ...],
) -> list[str]:
    warnings: list[str] = []
    for key, min_val, max_val in specs:
        value, warn = CoercionService.coerce_float(
            key, out.get(key), getattr(defaults, key),
            min_val=min_val, max_val=max_val,
        )
        out[key] = value
        _append_warning(warnings, warn)
    return warnings


def _apply_choice_field(
    out: dict[str, object],
    defaults: object,
    key: str,
    choices: tuple[str, ...],
) -> list[str]:
    value, warn = CoercionService.coerce_choice(key, out.get(key), getattr(defaults, key), choices)
    out[key] = value
    return [warn] if warn else []


def _string_config_value(value: object) -> str:
    if isinstance(value, str):
        return value
    # 纯数字配置(QQ 号/手机号/账号)即便没加引号被推断成 int,也按字符串还原而不是丢空。
    # 排除 bool(它是 int 子类):feishu_app_id: true 不该变成 "True"。
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return ""


# LLM: 名单只从配置类声明推出：键名是凭据（is_credential_key）且声明为 str 的字段；新增凭据字段自动纳入，
#   别处不得复制这份名单。结果只依赖类定义，按类缓存；只读，不读配置值。
# 函数用途: 列出配置类里所有声明为字符串的凭据字段，供统一的类型校验使用。
@cache
def credential_string_fields(config_cls: type) -> tuple[str, ...]:
    return tuple(
        item.name for item in fields(config_cls)
        if is_credential_key(item.name) and item.type in ("str", str)
    )


# LLM: 返回（归一后的值, 告警或 None）。字符串原样；整数沿用“纯数字没加引号也按字符串还原”的旧口径（bool 除外）；
#   None 与 YAML 留空读成的 [] 按“没填”取默认值、不告警；其它类型告警并回落默认值。告警只经 describe_raw_value
#   描述原值，凭据键一律写“已隐藏”，不回显。纯函数，不写日志。
# 函数用途: 按统一口径把一个凭据字段的原始配置值转成字符串，类型不符时给出不泄露原值的告警。
def _credential_string_value(key: str, raw: object, default: object) -> tuple[object, str | None]:
    if isinstance(raw, str):
        return raw, None
    if isinstance(raw, int) and not isinstance(raw, bool):
        return str(raw), None
    if raw is None or raw == []:
        return default, None
    return default, f"{key}: expected a string, got {describe_raw_value(key, raw)}; using default"


# LLM: 必须排在归一服务最前面：之后的服务（例如飞书/QQ 字段的字符串还原）只会看到字符串，不会再把错误类型静默改成
#   空串，也不会让列表等原样进入运行配置。只处理用户配置里真的写了的键；缺省键保持 dataclass 默认值。
# 类用途: 统一校验凭据类字符串配置的类型，类型写错时告警（不回显原值）并回落默认值。
class CredentialFieldsService:
    # LLM: 输入是正在归一的配置字典与默认配置对象；返回新字典与告警列表，不修改入参。
    # 函数用途: 对所有凭据字符串字段逐个做类型校验并收集告警。
    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        out = dict(data)
        warnings: list[str] = []
        for key in credential_string_fields(type(defaults)):
            if key in out:
                out[key], warn = _credential_string_value(key, out[key], getattr(defaults, key))
                _append_warning(warnings, warn)
        return out, warnings


def _normalize_string_list(value: object) -> list[str]:
    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, (list, tuple)):
        raw_items = value
    else:
        return []
    return [str(item).strip() for item in raw_items if item is not None and str(item).strip()]


# ---------------------------------------------------------------------------
# temperature
# ---------------------------------------------------------------------------

# LLM: temperature 是唯一采样温度旋钮（原 model_temperature_explicit 已并入）：空 = None = 不发送，0.0-2.0 的数字按
#   字符串保存并发送；乱填告警后回到默认（空，不发送），绝不悄悄换成某个软件温度。只改 out，不发请求。
# 函数用途: 校验并规范化采样温度配置。
def _normalize_temperature(out: dict[str, object], defaults: object) -> list[str]:
    raw_temp = out.get("temperature", defaults.temperature)
    # YAML 里 `temperature:` 留空会读成 []，与空串、None 一样表示不发送。
    if raw_temp is None or raw_temp == [] or (isinstance(raw_temp, str) and not raw_temp.strip()):
        out["temperature"] = None
        return []
    temp_val = _temperature_value(raw_temp)
    if temp_val is not None and 0.0 <= temp_val <= 2.0:
        out["temperature"] = raw_temp.strip() if isinstance(raw_temp, str) else str(temp_val)
        return []
    out["temperature"] = defaults.temperature
    if temp_val is None:
        return [f"temperature: expected a number or blank, got {raw_temp!r}; not sending temperature"]
    return [f"temperature: expected 0.0-2.0, got {temp_val}; not sending temperature"]


def _temperature_value(raw_temp: object) -> float | None:
    if isinstance(raw_temp, str):
        try:
            return float(raw_temp.strip())
        except ValueError:
            return None
    if isinstance(raw_temp, (int, float)):
        return float(raw_temp)
    return None


# ---------------------------------------------------------------------------
# Model / Gateway / Daemon (was _normalize_core_fields.py)
# ---------------------------------------------------------------------------

# LLM: 规范化显式协议选择；空值保持未配置，非法协议也不能回退到可生成回复的模型。
# 类用途: 检查模型连接、容量和请求选项的配置值。
class ModelFieldsService:
    # LLM: 不发模型请求或改持久配置；top_p 与模型表单共用范围校验，智能程度档位/控制方式按枚举校验，非法 YAML 告警回默认。
    #   上下文窗口和温度留空都是 None：窗口交给服务商元数据/探测，温度不发送。
    # 函数用途: 统一模型字段的类型和范围，供 YAML/overlay 共用；None 保留为不覆盖供应商采样。
    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        out = dict(data)
        warnings = _apply_choice_field(
            out, defaults, "model_backend",
            ("", "echo", "anthropic_compatible", "openai_compatible", "openai_responses"),
        )
        warnings.extend(_apply_int_fields(
            out, defaults,
            (
                ("request_timeout", 1, 600),
                ("max_tokens", 1, None),
                ("model_context_window_tokens", 1, None),
            ),
        ))
        warnings.extend(_apply_bool_fields(out, defaults, ("reasoning_control_auto_probe",)))
        # 智能程度档位与控制方式取值与 backends/reasoning_control 一致；非法值告警并回默认。
        warnings.extend(_apply_choice_field(out, defaults, "model_reasoning_effort", REASONING_LEVELS))
        warnings.extend(_apply_choice_field(out, defaults, "model_reasoning_control", REASONING_CONTROLS))
        # 结构化输出方式取值与 backends/structured_output_mode 一致；非法值告警并回默认。
        warnings.extend(_apply_choice_field(out, defaults, "model_structured_output", STRUCTURED_OUTPUT_MODES))
        warnings.extend(_normalize_temperature(out, defaults))
        try:
            out["top_p"] = validate_top_p(out.get("top_p", defaults.top_p))
        except ValueError as exc:
            out["top_p"] = defaults.top_p
            warnings.append(str(exc))
        return out, warnings


# LLM: Gateway 限额和空闲寿命都在本入口归一，0 秒明确关闭回收；不得让非法数字进入维护循环。
# 类用途: 统一队列、连接、实例寿命和媒体数量/字节预算的数值校验。
class GatewayFieldsService:
    _INT_FIELD_SPECS = (
        ("gateway_stop_timeout", 1, None),
        ("gateway_request_timeout", 1, None),
        ("gateway_user_inflight_limit", 1, None),
        ("gateway_global_inflight_limit", 1, None),
        ("gateway_processing_timeout_seconds", 30, None),
        ("gateway_request_max_attempts", 0, None),
        ("gateway_port", 0, 65535),
        ("input_media_max_bytes", 1, None),
        ("input_media_max_files", 1, None),
    )
    _FLOAT_FIELD_SPECS = (("owner_agent_idle_seconds", 0.0, None),)

    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        out = dict(data)
        warnings = _apply_int_fields(out, defaults, GatewayFieldsService._INT_FIELD_SPECS)
        warnings.extend(_apply_float_fields(out, defaults, GatewayFieldsService._FLOAT_FIELD_SPECS))
        return out, warnings


# LLM: 参数减量第 3 批 B 组（2026-09-27）起 daemon 只剩两个布尔安全边界项要归一（数字项与 planner/probe 已降级为 cli/daemon 常量）。
# 类用途: 归一前台 daemon 仍保留的配置开关。
class DaemonFieldsService:
    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        out = dict(data)
        warnings = _apply_bool_fields(out, defaults, ("daemon_mutate_state", "daemon_start_runners"))
        return out, warnings


# ---------------------------------------------------------------------------
# Home layout (was _normalize_home_fields.py)
# ---------------------------------------------------------------------------

# 参数减量第 1 批（2026-09-27）：external_knowledge_* 与 home_lesson_auto_read_limit 没有读取方，已连同字段删除。
_HOME_STRING_FIELDS = (
    "my_agent_home", "my_agent_owner_provider", "my_agent_owner_kind",
    "my_agent_owner_id", "workspace_task_path_template",
)
_HOME_RUNTIME_BOOL_FIELDS = (
    "home_context_enabled", "run_task_workspace_enabled",
)
def _normalize_home_strings(out: dict[str, object], defaults: object) -> list[str]:
    warnings: list[str] = []
    for key in _HOME_STRING_FIELDS:
        explicit = key in out
        raw = out.get(key, getattr(defaults, key))
        if isinstance(raw, str) and raw.strip():
            out[key] = raw.strip()
            continue
        out[key] = getattr(defaults, key)
        if not explicit and str(raw or "").strip() == "":
            continue
        warnings.append(f"{key}: expected a non-empty string, got {describe_raw_value(key, raw)}; using default")
    return warnings


class HomeLayoutFieldsService:
    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        out = dict(data)
        warnings = _normalize_home_strings(out, defaults)
        warnings.extend(_apply_bool_fields(out, defaults, _HOME_RUNTIME_BOOL_FIELDS))
        return out, warnings


# ---------------------------------------------------------------------------
# Adapter / User identity (was _normalize_identity_fields.py)
# ---------------------------------------------------------------------------

class AdapterFieldsService:
    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        warnings: list[str] = []
        out = dict(data)

        def apply(key: str, coerced: object, warn: str | None) -> None:
            out[key] = coerced
            if warn:
                warnings.append(warn)

        for key in ("feishu_app_id", "feishu_app_secret", "feishu_verification_token", "feishu_encrypt_key"):
            out[key] = _string_config_value(out.get(key, defaults.feishu_app_id if key == "feishu_app_id" else ""))

        v, w = CoercionService.coerce_int(
            "feishu_callback_port", out.get("feishu_callback_port"),
            defaults.feishu_callback_port, min_val=1024, max_val=65535,
        )
        apply("feishu_callback_port", v, w)

        for key in ("qq_app_id", "qq_app_secret"):
            env_key = key.upper()
            env_val = os.environ.get(env_key, "")
            if env_val:
                out[key] = env_val
            else:
                out[key] = _string_config_value(out.get(key, defaults.qq_app_id if key == "qq_app_id" else ""))

        return out, warnings


def _normalize_user_id(out: dict[str, object], defaults: object, warnings: list[str]) -> None:
    raw_user_id = out.get("user_id", defaults.user_id)
    if isinstance(raw_user_id, str) and raw_user_id.strip():
        out["user_id"] = raw_user_id.strip()
        return
    out["user_id"] = defaults.user_id
    warnings.append(f"user_id: expected a non-empty string, got {describe_raw_value('user_id', raw_user_id)}; using default")


def _normalize_user_auth(out: dict[str, object], defaults: object, warnings: list[str]) -> None:
    value, warn = CoercionService.coerce_bool("auth_enabled", out.get("auth_enabled"), defaults.auth_enabled)
    out["auth_enabled"] = value
    if warn:
        warnings.append(warn)


def _normalize_admin_user(out: dict[str, object], defaults: object) -> None:
    raw_admin = out.get("admin_user_id", defaults.admin_user_id)
    out["admin_user_id"] = raw_admin.strip() if isinstance(raw_admin, str) and raw_admin.strip() else defaults.admin_user_id


class UserFieldsService:
    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        warnings: list[str] = []
        out = dict(data)
        _normalize_user_id(out, defaults, warnings)
        _normalize_user_auth(out, defaults, warnings)
        _normalize_admin_user(out, defaults)
        return out, warnings


# ---------------------------------------------------------------------------
# Runtime bool switches (was _normalize_operational_fields.py)
# ---------------------------------------------------------------------------

# 带引号的 "false" 必须转成真实布尔值，否则会被当成真值打开开关（含自学习 enable_self_learning）。
_RUNTIME_BOOL_FIELDS = (
    "auto_save_memory", "local_store_fts_enabled",
    "enable_file_syntax_diagnostics", "decision_skip_records_enabled",
    "decision_observe_sampling_enabled",
    "conversation_terminal_tool_fold_enabled", "compact_recall_hint_enabled",
    "enable_subagents", "enable_self_learning",
    "audit_enabled",
)


def _normalize_runtime_bools(out: dict[str, object], defaults: object) -> list[str]:
    warnings: list[str] = []
    for key in _RUNTIME_BOOL_FIELDS:
        value, warn = CoercionService.coerce_bool(key, out.get(key), getattr(defaults, key))
        out[key] = value
        _append_warning(warnings, warn)
    return warnings


def _normalize_runtime_choices(out: dict[str, object], defaults: object) -> list[str]:
    value, warn = CoercionService.coerce_choice(
        "log_level", out.get("log_level"), defaults.log_level,
        choices=("debug", "info", "warning", "error", "critical"),
    )
    out["log_level"] = value
    return [warn] if warn else []


class RuntimeBoolFieldsService:
    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        out = dict(data)
        warnings = _normalize_runtime_bools(out, defaults)
        warnings.extend(_normalize_runtime_choices(out, defaults))
        return out, warnings


# ---------------------------------------------------------------------------
# Tool / Subagent (was _normalize_runtime_fields.py)
# ---------------------------------------------------------------------------

def _normalize_tool_int_fields(out: dict[str, object], defaults: object) -> list[str]:
    return _apply_int_fields(out, defaults, TOOL_INT_FIELDS)


# LLM: 工具与插件开关共用布尔转换和 dataclass 默认；新增开关同时检查 YAML、管理入口和配置回归。
# 函数用途: 把配置中的开关转成真实布尔值，避免带引号的 false 在权限判断中变成真。
def _normalize_tool_bool_fields(out: dict[str, object], defaults: object) -> list[str]:
    return _apply_bool_fields(
        out, defaults,
        (
            "enable_plugins",
            "plugin_process_sandbox",
            "shell_sandbox_hide_user_home",
            "stream_enabled",
            "tool_catalog_include_examples",
            "tool_vector_search_enabled",
            "computer_use_enabled",
        ),
    )


def _normalize_tool_catalog_fields(out: dict[str, object], defaults: object) -> list[str]:
    warnings: list[str] = []
    mode, warn = CoercionService.coerce_choice(
        "tool_catalog_mode", out.get("tool_catalog_mode"), defaults.tool_catalog_mode,
        choices=("compact", "full", "retrieval_only", "off"),
    )
    out["tool_catalog_mode"] = mode
    _append_warning(warnings, warn)
    out["tool_catalog_categories"] = _normalize_string_list(
        out.get("tool_catalog_categories", defaults.tool_catalog_categories)
    )
    return warnings


def _normalize_background_tool_fields(out: dict[str, object], defaults: object) -> list[str]:
    out["background_main_agent_allowed_tools"] = _normalize_string_list(
        out.get("background_main_agent_allowed_tools", defaults.background_main_agent_allowed_tools)
    )
    return []


def _normalize_path_access_fields(out: dict[str, object], defaults: object) -> list[str]:
    raw_mode = out.get("path_access_mode", defaults.path_access_mode)
    mode = normalize_path_access_mode(raw_mode)
    warnings: list[str] = []
    normalized_raw = str(raw_mode or "").strip().lower().replace("_", "-")
    known_values = {"normal", "full", ""}
    if normalized_raw not in known_values:
        warnings.append(f"path_access_mode: unknown value {describe_raw_value('path_access_mode', raw_mode)}; using {mode!r}")
    out["path_access_mode"] = mode
    roots = _normalize_string_list(out.get("path_dangerous_roots", defaults.path_dangerous_roots))
    out["path_dangerous_roots"] = roots or list(defaults.path_dangerous_roots)
    return warnings


def _normalize_command_access_mode(out: dict[str, object], defaults: object) -> list[str]:
    warnings: list[str] = []
    raw = out.get("access_mode", defaults.access_mode)
    normalized_raw = str(raw).strip().lower().replace("_", "-") if raw is not None else ""
    mode, warn = CoercionService.coerce_choice(
        "access_mode", normalized_raw, defaults.access_mode,
        choices=("restricted", "workspace-write", "full-access"),
    )
    out["access_mode"] = mode
    _append_warning(warnings, warn)
    return warnings


def _normalize_runner_timeout_by_role(value: object) -> tuple[dict[str, object], str | None]:
    if value in ({}, None, ""):
        return {}, None
    if isinstance(value, dict):
        return _normalize_runner_timeout_mapping(value), None
    if isinstance(value, list):
        parsed = _timeout_mapping_from_list(value)
        if parsed is not None:
            return parsed, None
    return {}, f"runner_timeout_by_role: expected dict or key=value list, got {describe_raw_value('runner_timeout_by_role', value)}; using default"


def _normalize_runner_timeout_mapping(value: dict[object, object]) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key or "").strip().lower()
        if key:
            normalized[key] = raw_value
    return normalized


def _timeout_mapping_from_list(value: list[object]) -> dict[str, object] | None:
    parsed: dict[object, object] = {}
    for item in value:
        text = str(item or "").strip()
        if not text or "=" not in text:
            return None
        key, raw_value = text.split("=", 1)
        parsed[key.strip()] = raw_value.strip()
    return _normalize_runner_timeout_mapping(parsed)


class ToolFieldsService:
    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        out = dict(data)
        warnings = _normalize_tool_int_fields(out, defaults)
        warnings.extend(_normalize_tool_bool_fields(out, defaults))
        warnings.extend(_normalize_tool_catalog_fields(out, defaults))
        warnings.extend(_normalize_path_access_fields(out, defaults))
        warnings.extend(_normalize_command_access_mode(out, defaults))
        warnings.extend(_normalize_background_tool_fields(out, defaults))
        return out, warnings


class SubagentBasicFieldsService:
    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        out = dict(data)
        warnings = _apply_int_fields(
            out, defaults,
            (
                ("memory_top_k", 0, None),
                ("max_subagents", 0, None),
                ("subagent_hierarchy_max_children_per_tool_call", 0, None),
                ("subagent_takeover_chain_max_depth", 0, None),
            ),
        )
        out["subagent_allowed_tools"] = _normalize_string_list(
            out.get("subagent_allowed_tools", defaults.subagent_allowed_tools)
        )
        out["subagent_role_template_dirs"] = _normalize_string_list(
            out.get("subagent_role_template_dirs", defaults.subagent_role_template_dirs)
        )
        return out, warnings


class SubagentAdvancedFieldsService:
    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        out = dict(data)
        warnings = _apply_int_fields(
            out, defaults,
            (
                ("subagent_debug_trace_level", 0, 5),
                ("dynamic_timeout_min", 10, None),
                ("dynamic_timeout_max", 60, None),
            ),
        )
        for name in ("estimated_output_tokens_per_second",):
            value, warn = CoercionService.coerce_float(
                name,
                out.get(name),
                getattr(defaults, name),
                min_val=1.0,
            )
            out[name] = value
            _append_warning(warnings, warn)
        timeouts, warn = _normalize_runner_timeout_by_role(
            out.get("runner_timeout_by_role", defaults.runner_timeout_by_role)
        )
        out["runner_timeout_by_role"] = timeouts
        _append_warning(warnings, warn)
        return out, warnings


# ---------------------------------------------------------------------------
# Timeout (was _normalize_timeout_fields.py)
# ---------------------------------------------------------------------------

_TIMEOUT_INT_FIELDS = (
    ("lease_stale_without_heartbeat_seconds", 30, None),
    ("gateway_ready_timeout_seconds", 1, None),
    ("gateway_restart_turn_wait_seconds", 0, None),
    ("gateway_restart_drain_timeout_seconds", 0, None),
    ("gateway_restart_cooldown_seconds", 0, None),
    ("self_learning_min_tool_rounds", 1, None),
    ("self_learning_daily_limit", 0, None),
    ("self_learning_max_skills", 0, None),
    ("self_learning_timeout_seconds", 10, None),
    ("runner_failure_retry_limit", 0, None),
    ("background_context_max_total_tokens", 0, None),
    ("background_claim_ttl_seconds", 1, None),
    ("skill_guard_max_files", 0, None),
    ("skill_guard_max_size_kb", 0, None),
    ("collaboration_auto_dispatch_max_runners", 0, None),
)


def _normalize_runner_timeout_seconds(value: object, default: object) -> tuple[str, str | None]:
    key = "runner_timeout_seconds"
    if value is None:
        return str(default), None
    if isinstance(value, bool):
        return str(default), f"{key}: expected 'off', 'auto', or a non-negative number, got boolean; using default {default!r}"
    if isinstance(value, (int, float)):
        if math.isfinite(float(value)) and value >= 0:
            return _format_timeout_number(value), None
        return str(default), f"{key}: expected >= 0, got {describe_raw_value(key, value)}; using default {default!r}"
    if isinstance(value, str):
        stripped = value.strip().lower()
        if stripped in {"off", "auto"}:
            return stripped, None
        parsed = _timeout_float(stripped)
        if parsed is not None and math.isfinite(parsed) and parsed >= 0:
            return _format_timeout_number(parsed), None
    return str(default), f"{key}: expected 'off', 'auto', or a non-negative number, got {describe_raw_value(key, value)}; using default {default!r}"


def _timeout_float(value: str) -> float | None:
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed


def _format_timeout_number(value: int | float) -> str:
    parsed = float(value)
    if parsed == int(parsed):
        return str(int(parsed))
    return str(parsed)


class TimeoutFieldsService:
    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        out = dict(data)
        warnings = []
        for key, min_val, max_val in _TIMEOUT_INT_FIELDS:
            value, warn = CoercionService.coerce_int(
                key, out.get(key), getattr(defaults, key),
                min_val=min_val, max_val=max_val,
            )
            out[key] = value
            if warn:
                warnings.append(warn)
        value, warn = _normalize_runner_timeout_seconds(
            out.get("runner_timeout_seconds"),
            defaults.runner_timeout_seconds,
        )
        out["runner_timeout_seconds"] = value
        if warn:
            warnings.append(warn)
        return out, warnings


# ---------------------------------------------------------------------------
# top-level normalizer (was original _normalize.py)
# ---------------------------------------------------------------------------

_NORMALIZE_SERVICES = (
    CredentialFieldsService,
    ModelFieldsService,
    GatewayFieldsService,
    DaemonFieldsService,
    ToolFieldsService,
    SubagentBasicFieldsService,
    AdapterFieldsService,
    HomeLayoutFieldsService,
    UserFieldsService,
    RuntimeBoolFieldsService,
    TimeoutFieldsService,
    SubagentAdvancedFieldsService,
)


class AgentConfigNormalizer:
    """Service for normalizing all non-memory AgentConfig fields."""

    @staticmethod
    def normalize(data: dict[str, object]) -> tuple[dict[str, object], list[str]]:
        """Validate and coerce all non-memory AgentConfig fields with safe defaults."""
        warnings: list[str] = []
        out: dict[str, object] = dict(data)
        defaults = default_agent_config()

        for service in _NORMALIZE_SERVICES:
            out, service_warnings = service.normalize(out, defaults)
            warnings.extend(service_warnings)

        return out, warnings
