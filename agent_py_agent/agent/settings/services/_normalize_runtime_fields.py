"""Tool, adapter, user, timeout, and advanced subagent config normalizers."""

# LLM: 这里维护运行期安全上限，新增字段要同步 warning 路径。
# 模块用途: 工具、适配器、用户空间、超时和高级子代理字段的归一化规则。

from __future__ import annotations

import os

from ._coercion import CoercionService


# LLM: _append_warning 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 向结果或告警集合加入 append_warning，同时保留调用方依赖的顺序。
def _append_warning(warnings: list[str], warn: str | None) -> None:
    if warn:
        warnings.append(warn)


# LLM: _apply_int_fields 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 把 配置系统 的归一化结果写回配置对象。
def _apply_int_fields(
    out: dict[str, object],
    defaults: object,
    specs: tuple[tuple[str, int | None, int | None], ...],
) -> list[str]:
    warnings: list[str] = []
    for key, min_val, max_val in specs:
        value, warn = CoercionService.coerce_int(
            key,
            out.get(key),
            getattr(defaults, key),
            min_val=min_val,
            max_val=max_val,
        )
        out[key] = value
        _append_warning(warnings, warn)
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
        value, warn = CoercionService.coerce_bool(key, out.get(key), getattr(defaults, key))
        out[key] = value
        _append_warning(warnings, warn)
    return warnings


# LLM: ToolFieldsService 属于 配置系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: ToolFieldsService 封装 配置系统 的一组相关操作，供上层组合调用。
class ToolFieldsService:
    """Normalize tool-related config fields."""

    # LLM: ToolFieldsService.normalize 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 归一化 ToolFieldsService 负责的配置字段并追加告警。
    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        out = dict(data)
        warnings = _normalize_tool_int_fields(out, defaults)
        warnings.extend(_normalize_tool_bool_fields(out, defaults))
        warnings.extend(_normalize_tool_catalog_fields(out, defaults))
        warnings.extend(_normalize_dispatch_watch_interval(out, defaults))
        return out, warnings


# LLM: _normalize_tool_int_fields keeps ToolFieldsService.normalize below code-size limits.
# 函数用途: 归一化工具、聊天和 CLI 展示相关整数配置。
def _normalize_tool_int_fields(out: dict[str, object], defaults: object) -> list[str]:
    return _apply_int_fields(out, defaults, _TOOL_INT_FIELDS)


# LLM: _normalize_tool_bool_fields keeps boolean tool prompt switches in one audited list.
# 函数用途: 归一化工具开关和工具目录展示布尔配置。
def _normalize_tool_bool_fields(out: dict[str, object], defaults: object) -> list[str]:
    return _apply_bool_fields(
        out,
        defaults,
        ("stream_enabled", "tool_catalog_include_examples", "tool_catalog_show_truncated_notice"),
    )


# LLM: _normalize_tool_catalog_fields keeps catalog prompt-shaping knobs safe and config-backed.
# 函数用途: 归一化工具目录展示模式和类别过滤列表。
def _normalize_tool_catalog_fields(out: dict[str, object], defaults: object) -> list[str]:
    warnings: list[str] = []
    mode, warn = CoercionService.coerce_choice(
        "tool_catalog_mode",
        out.get("tool_catalog_mode"),
        defaults.tool_catalog_mode,
        choices=("compact", "full", "retrieval_only", "off"),
    )
    out["tool_catalog_mode"] = mode
    _append_warning(warnings, warn)
    out["tool_catalog_categories"] = _normalize_string_list(
        out.get("tool_catalog_categories", defaults.tool_catalog_categories)
    )
    return warnings


# LLM: _normalize_dispatch_watch_interval handles the one float in the runtime tool slice.
# 函数用途: 归一化 dispatch 默认 watch 间隔，并返回配置告警。
def _normalize_dispatch_watch_interval(out: dict[str, object], defaults: object) -> list[str]:
    warnings: list[str] = []
    value, warn = CoercionService.coerce_float(
        "dispatch_default_watch_interval",
        out.get("dispatch_default_watch_interval"),
        defaults.dispatch_default_watch_interval,
        min_val=0.0,
        max_val=None,
    )
    out["dispatch_default_watch_interval"] = value
    _append_warning(warnings, warn)
    return warnings


_TOOL_INT_FIELDS = (
    ("max_tool_rounds", 0, None),
    ("tool_agent_budget_window_seconds", 0, None),
    ("tool_agent_budget_max_calls", 0, None),
    ("tool_artifact_read_budget_window_seconds", 0, None),
    ("tool_artifact_read_budget_max_chars", 0, None),
    ("tool_read_max_chars", 100, None),
    ("tool_write_inline_max_chars", 100, 100_000),
    ("tool_web_max_chars", 0, None),
    ("tool_http_timeout", 1, None),
    ("tool_shell_timeout", 1, None),
    ("tool_catalog_limit", 0, None),
    ("tool_catalog_offset", 0, None),
    ("tool_catalog_entry_max_chars", 0, None),
    ("tool_detail_max_chars", 0, None),
    ("chat_history_max_turns", 1, None),
    ("chat_history_assistant_preview_chars", 0, None),
    ("chat_transcript_max_chars", 1000, None),
    ("chat_collapse_preview_lines", 0, None),
    ("chat_collapse_preview_chars", 0, None),
    ("chat_context_window_chars", 1000, None),
    ("chat_transcript_scroll_lines", 1, None),
    ("cli_status_limit", 0, None),
    ("cli_timeline_limit", 0, None),
    ("cli_memory_list_limit", 0, None),
    ("cli_memory_search_limit", 0, None),
    ("cli_chat_memory_limit", 0, None),
    ("cli_memory_archive_limit", 0, None),
    ("cli_memory_route_limit", 0, None),
    ("cli_local_search_limit", 0, None),
    ("cli_local_search_preview_chars", -1, None),
    ("cli_local_doctor_limit", 0, None),
    ("cli_task_list_limit", 0, None),
    ("cli_notification_limit", 0, None),
    ("cli_audit_limit", 0, None),
    ("cli_audit_cleanup_days", 0, None),
)


# LLM: SubagentBasicFieldsService 属于 配置系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: SubagentBasicFieldsService 封装 配置系统 的一组相关操作，供上层组合调用。
class SubagentBasicFieldsService:
    """Normalize basic subagent config fields (max_subagents, memory_top_k)."""

    # LLM: SubagentBasicFieldsService.normalize 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 归一化 SubagentBasicFieldsService 负责的配置字段并追加告警。
    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        out = dict(data)
        warnings = _apply_int_fields(
            out,
            defaults,
            (
                ("memory_top_k", 0, None),
                ("max_subagents", 0, None),
                ("subagent_board_limit", 0, None),
                ("subagent_spawn_default_count", 0, None),
                ("subagent_cli_default_limit", 0, None),
                ("subagent_probe_default_limit", 0, None),
                ("subagent_hierarchy_default_max_depth", 0, None),
                ("subagent_hierarchy_recovery_max_nodes", 0, None),
                ("subagent_hierarchy_max_children_per_tool_call", 1, None),
                ("subagent_descendant_scan_limit", 1, None),
                ("subagent_takeover_chain_max_depth", 0, None),
                ("subagent_context_summary_inline_json_chars", 0, None),
                ("subagent_context_summary_inline_text_chars", 0, None),
            ),
        )
        out["subagent_allowed_tools"] = _normalize_string_list(
            out.get("subagent_allowed_tools", defaults.subagent_allowed_tools)
        )
        out["subagent_role_template_dirs"] = _normalize_string_list(
            out.get("subagent_role_template_dirs", defaults.subagent_role_template_dirs)
        )
        return out, warnings


# LLM: AdapterFieldsService 属于 配置系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: AdapterFieldsService 封装 配置系统 的一组相关操作，供上层组合调用。
class AdapterFieldsService:
    """Normalize adapter-related config fields (feishu, qq, etc.)."""

    # LLM: AdapterFieldsService.normalize 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 归一化 AdapterFieldsService 负责的配置字段并追加告警。
    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        """Normalize adapter-related config fields."""
        warnings: list[str] = []
        out = dict(data)

        # LLM: AdapterFieldsService.apply 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
        # 函数用途: 把 AdapterFieldsService 的归一化结果写回配置对象。
        def apply(key: str, coerced: object, warn: str | None) -> None:
            out[key] = coerced
            if warn:
                warnings.append(warn)

        for key in ("feishu_app_id", "feishu_app_secret", "feishu_verification_token", "feishu_encrypt_key"):
            out[key] = _string_config_value(out.get(key, defaults.feishu_app_id if key == "feishu_app_id" else ""))

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
                out[key] = _string_config_value(out.get(key, defaults.qq_app_id if key == "qq_app_id" else ""))

        return out, warnings


# LLM: _string_config_value 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 完成 配置系统 中的 string_config_value 步骤，并保持调用方依赖的数据形状。
def _string_config_value(value: object) -> str:
    return value if isinstance(value, str) else ""


# LLM: _normalize_string_list keeps list-like config fields predictable for runtime call sites.
# 函数用途: 把字符串、列表或元组配置归一成去空白的字符串列表，避免 MagicMock 或坏配置漏进业务接口。
def _normalize_string_list(value: object) -> list[str]:
    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, (list, tuple)):
        raw_items = value
    else:
        return []
    return [str(item).strip() for item in raw_items if item is not None and str(item).strip()]


# LLM: UserFieldsService 属于 配置系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: UserFieldsService 封装 配置系统 的一组相关操作，供上层组合调用。
class UserFieldsService:
    """Normalize user-related config fields."""

    # LLM: UserFieldsService.normalize 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 归一化 UserFieldsService 负责的配置字段并追加告警。
    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        """Normalize user-related config fields."""
        warnings: list[str] = []
        out = dict(data)

        # LLM: UserFieldsService.apply 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
        # 函数用途: 把 UserFieldsService 的归一化结果写回配置对象。
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


# LLM: SubagentAdvancedFieldsService 属于 配置系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: SubagentAdvancedFieldsService 封装 配置系统 的一组相关操作，供上层组合调用。
class SubagentAdvancedFieldsService:
    """Normalize advanced subagent config fields."""

    # LLM: SubagentAdvancedFieldsService.normalize 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 归一化 SubagentAdvancedFieldsService 负责的配置字段并追加告警。
    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        out = dict(data)
        warnings = _apply_int_fields(
            out,
            defaults,
            (
                ("subagent_automation_level", 1, 3),
                ("subagent_debug_trace_level", 0, 5),
                ("acceptance_test_timeout_seconds", 1, 300),
                ("dynamic_timeout_min", 10, None),
                ("dynamic_timeout_max", 60, None),
                ("max_auto_split_depth", 0, None),
                ("max_auto_retry_attempts", 1, 10),
            ),
        )
        warnings.extend(_apply_bool_fields(out, defaults, ("acceptance_execute_tests",)))
        value, warn = CoercionService.coerce_float(
            "dynamic_timeout_safety_margin", out.get("dynamic_timeout_safety_margin"),
            defaults.dynamic_timeout_safety_margin, min_val=1.0, max_val=10.0,
        )
        out["dynamic_timeout_safety_margin"] = value
        _append_warning(warnings, warn)
        timeouts, warn = _normalize_runner_timeout_by_role(
            out.get("runner_timeout_by_role", defaults.runner_timeout_by_role)
        )
        out["runner_timeout_by_role"] = timeouts
        _append_warning(warnings, warn)
        return out, warnings


# LLM: runner_timeout_by_role is intentionally permissive because timeout values reuse runner_timeout_seconds grammar.
# 函数用途: 把角色级 runner 超时配置归一成小写 key 的字典；支持行内 dict 或 ["worker=60"] 列表。
def _normalize_runner_timeout_by_role(value: object) -> tuple[dict[str, object], str | None]:
    if value in ({}, None, ""):
        return {}, None
    if isinstance(value, dict):
        return _normalize_runner_timeout_mapping(value), None
    if isinstance(value, list):
        parsed = _timeout_mapping_from_list(value)
        if parsed is not None:
            return parsed, None
    return {}, f"runner_timeout_by_role: expected dict or key=value list, got {value!r}; using default"


# LLM: _normalize_runner_timeout_mapping keeps role names stable and leaves value grammar to runner_dispatch.
# 函数用途: 清理角色名，保留 off/auto/数字等原始超时值，供 runner_gate 按角色取用。
def _normalize_runner_timeout_mapping(value: dict[object, object]) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key or "").strip().lower()
        if key:
            normalized[key] = raw_value
    return normalized


# LLM: _timeout_mapping_from_list supports the repo's simple YAML list syntax without nested maps.
# 函数用途: 把 ["worker=8", "coordinator=off"] 转成角色超时字典；遇到非法项返回 None。
def _timeout_mapping_from_list(value: list[object]) -> dict[str, object] | None:
    parsed: dict[object, object] = {}
    for item in value:
        text = str(item or "").strip()
        if not text or "=" not in text:
            return None
        key, raw_value = text.split("=", 1)
        parsed[key.strip()] = raw_value.strip()
    return _normalize_runner_timeout_mapping(parsed)
