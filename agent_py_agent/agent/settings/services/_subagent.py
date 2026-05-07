"""Subagent workflow normalization service."""

# LLM: 这些规则控制自动化授权和工作区范围，改动前核对安全含义。
# 模块用途: 子代理工作流模式、目录策略和 review 轮数归一化。

from __future__ import annotations

from dataclasses import dataclass


# LLM: SubagentWorkflowWarningParams 属于 配置系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: SubagentWorkflowWarningParams 参数包，把相关输入集中传给 配置系统 的服务函数。
@dataclass(frozen=True)
class SubagentWorkflowWarningParams:
    field_name: str
    raw_value: object
    fallback_value: object
    reason: str


# LLM: SubagentWorkflowWarningService 属于 配置系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: SubagentWorkflowWarningService 封装 配置系统 的一组相关操作，供上层组合调用。
class SubagentWorkflowWarningService:
    """Service for handling subagent workflow config warnings."""

    # LLM: SubagentWorkflowWarningService.add_warning 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 向结果或告警集合加入 add_warning，同时保留调用方依赖的顺序。
    @staticmethod
    def add_warning(
        warnings: list[dict[str, object]],
        params: SubagentWorkflowWarningParams,
    ) -> None:
        """Append a structured warning dict for a subagent workflow config field."""
        warnings.append(
            {
                "field_name": params.field_name,
                "raw_value": params.raw_value,
                "fallback_value": params.fallback_value,
                "reason": params.reason,
            }
        )


# LLM: SubagentWorkflowConfigService 属于 配置系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: SubagentWorkflowConfigService 封装 配置系统 的一组相关操作，供上层组合调用。
class SubagentWorkflowConfigService:
    """Service for validating and coercing subagent workflow config fields."""

    # LLM: SubagentWorkflowConfigService.normalize 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 归一化 SubagentWorkflowConfigService 负责的配置字段并追加告警。
    @staticmethod
    def normalize(config: object) -> list[dict[str, object]]:
        """Validate and coerce subagent workflow config fields on an AgentConfig instance."""
        from ..config import AgentConfig
        warnings: list[dict[str, object]] = []
        defaults = AgentConfig()

        SubagentWorkflowConfigService._normalize_mode(config, defaults, warnings)
        SubagentWorkflowConfigService._normalize_builtin(config, defaults, warnings)
        SubagentWorkflowConfigService._normalize_dirs(config, defaults, warnings)
        SubagentWorkflowConfigService._normalize_review_rounds(config, defaults, warnings)
        config.subagent_workflow_config_warnings = warnings
        return warnings

    # LLM: SubagentWorkflowConfigService._normalize_mode 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 把输入值归一成 配置系统 内部使用的稳定格式。
    @staticmethod
    def _normalize_mode(config: object, defaults: object, warnings: list[dict[str, object]]) -> None:
        """Normalize subagent workflow mode."""
        raw_mode = config.subagent_workflow_mode
        if isinstance(raw_mode, str) and raw_mode.strip().lower() in {"auto", "manual", "off"}:
            config.subagent_workflow_mode = raw_mode.strip().lower()
        else:
            config.subagent_workflow_mode = defaults.subagent_workflow_mode
            SubagentWorkflowWarningService.add_warning(
                warnings,
                SubagentWorkflowWarningParams(
                    "subagent_workflow_mode",
                    raw_mode,
                    defaults.subagent_workflow_mode,
                    "expected one of ['auto', 'manual', 'off']",
                ),
            )

    # LLM: SubagentWorkflowConfigService._normalize_builtin 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 把输入值归一成 配置系统 内部使用的稳定格式。
    @staticmethod
    def _normalize_builtin(config: object, defaults: object, warnings: list[dict[str, object]]) -> None:
        """Normalize builtin workflow enablement."""
        raw_builtin = config.subagent_builtin_workflows
        if isinstance(raw_builtin, bool):
            config.subagent_builtin_workflows = raw_builtin
        else:
            config.subagent_builtin_workflows = defaults.subagent_builtin_workflows
            SubagentWorkflowWarningService.add_warning(
                warnings,
                SubagentWorkflowWarningParams(
                    "subagent_builtin_workflows",
                    raw_builtin,
                    defaults.subagent_builtin_workflows,
                    "expected a boolean value",
                ),
            )

    # LLM: SubagentWorkflowConfigService._normalize_dirs 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 把输入值归一成 配置系统 内部使用的稳定格式。
    @staticmethod
    def _normalize_dirs(config: object, defaults: object, warnings: list[dict[str, object]]) -> None:
        """Normalize user workflow directories."""
        raw_dirs = config.subagent_user_workflow_dirs
        if (
            isinstance(raw_dirs, list)
            and all(isinstance(item, str) and item.strip() for item in raw_dirs)
        ):
            config.subagent_user_workflow_dirs = [item.strip() for item in raw_dirs]
        else:
            config.subagent_user_workflow_dirs = list(defaults.subagent_user_workflow_dirs)
            SubagentWorkflowWarningService.add_warning(
                warnings,
                SubagentWorkflowWarningParams(
                    "subagent_user_workflow_dirs",
                    raw_dirs,
                    list(defaults.subagent_user_workflow_dirs),
                    "expected a list of non-empty strings",
                ),
            )

    # LLM: SubagentWorkflowConfigService._normalize_review_rounds 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 把输入值归一成 配置系统 内部使用的稳定格式。
    @staticmethod
    def _normalize_review_rounds(config: object, defaults: object, warnings: list[dict[str, object]]) -> None:
        """Normalize workflow review rounds."""
        raw_review_rounds = config.subagent_workflow_review_rounds
        review_rounds = _coerce_review_rounds_value(raw_review_rounds)

        if review_rounds is not None and 0 <= review_rounds <= 5:
            config.subagent_workflow_review_rounds = review_rounds
        else:
            config.subagent_workflow_review_rounds = defaults.subagent_workflow_review_rounds
            SubagentWorkflowWarningService.add_warning(
                warnings,
                SubagentWorkflowWarningParams(
                    "subagent_workflow_review_rounds",
                    raw_review_rounds,
                    defaults.subagent_workflow_review_rounds,
                    "expected an integer between 0 and 5",
                ),
            )


# LLM: _coerce_review_rounds_value 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 把原始配置值转换成目标类型，失败时回退默认值并记录告警。
def _coerce_review_rounds_value(raw_review_rounds: object) -> int | None:
    if isinstance(raw_review_rounds, bool):
        return None
    if isinstance(raw_review_rounds, int):
        return raw_review_rounds
    if isinstance(raw_review_rounds, str) and raw_review_rounds.strip().isdigit():
        return int(raw_review_rounds.strip())
    return None
