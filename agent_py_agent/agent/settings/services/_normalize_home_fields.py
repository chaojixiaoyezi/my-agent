"""Home layout, external knowledge, and provider-space config normalizers."""

# LLM: This module keeps home/provider config normalization out of the crowded runtime normalizer file.
# 模块用途: 归一化 my-agent 家目录、外部知识库和外部平台用户/群空间配置。

from __future__ import annotations

from ._normalize_runtime_fields import _apply_bool_fields, _apply_int_fields, _normalize_string_list


# LLM: HomeLayoutFieldsService keeps install/home fields config-backed and warning-aware.
# 类用途: 归一化家目录、任务路径模板、外部知识库和 provider 空间配置字段。
class HomeLayoutFieldsService:
    """Normalize home layout, external knowledge, and provider-space config fields."""

    # LLM: HomeLayoutFieldsService.normalize is called by the central config normalizer after adapter fields.
    # 函数用途: 归一化 HomeLayoutFieldsService 负责的配置字段并追加告警。
    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        out = dict(data)
        warnings = _normalize_home_strings(out, defaults)
        _normalize_external_knowledge_lists(out, defaults)
        warnings.extend(_apply_int_fields(out, defaults, _HOME_RUNTIME_INT_FIELDS))
        warnings.extend(_apply_bool_fields(out, defaults, _HOME_RUNTIME_BOOL_FIELDS))
        warnings.extend(_apply_int_fields(out, defaults, _PROVIDER_SPACE_INT_FIELDS))
        warnings.extend(_apply_bool_fields(out, defaults, ("provider_space_destructive_actions_use_trash",)))
        return out, warnings


# LLM: _normalize_home_strings handles non-empty string fallbacks for home-space config.
# 函数用途: 归一化家目录、任务路径模板和外部知识库索引文件名。
def _normalize_home_strings(out: dict[str, object], defaults: object) -> list[str]:
    warnings: list[str] = []
    for key in _HOME_STRING_FIELDS:
        raw = out.get(key, getattr(defaults, key))
        if isinstance(raw, str) and raw.strip():
            out[key] = raw.strip()
            continue
        out[key] = getattr(defaults, key)
        warnings.append(f"{key}: expected a non-empty string, got {raw!r}; using default")
    return warnings


# LLM: _normalize_external_knowledge_lists keeps user-facing KB sources simple flat lists.
# 函数用途: 归一化外部知识库的目录、接口和数据库来源列表。
def _normalize_external_knowledge_lists(out: dict[str, object], defaults: object) -> None:
    for key in _EXTERNAL_KNOWLEDGE_LIST_FIELDS:
        out[key] = _normalize_string_list(out.get(key, getattr(defaults, key)))


_HOME_STRING_FIELDS = (
    "my_agent_home",
    "my_agent_owner_provider",
    "my_agent_owner_kind",
    "my_agent_owner_id",
    "workspace_task_path_template",
    "external_knowledge_index_file_name",
)

_EXTERNAL_KNOWLEDGE_LIST_FIELDS = (
    "external_knowledge_directory_roots",
    "external_knowledge_api_sources",
    "external_knowledge_database_sources",
)

_HOME_RUNTIME_INT_FIELDS = (
    ("home_lesson_auto_read_limit", 0, 20),
)

_HOME_RUNTIME_BOOL_FIELDS = (
    "home_runtime_bootstrap_enabled",
    "home_context_enabled",
    "daily_memory_mirror_enabled",
    "run_task_workspace_enabled",
)

_PROVIDER_SPACE_INT_FIELDS = (
    ("provider_space_default_max_storage_mb", 1, None),
    ("provider_space_max_download_file_mb", 1, None),
    ("provider_space_trash_retention_days", 0, None),
)
