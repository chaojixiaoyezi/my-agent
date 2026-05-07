"""Subagent workflow normalization service."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SubagentWorkflowWarningParams:
    # LLM: warning fields are a bundle so config expansion does not change service signatures.
    field_name: str
    raw_value: object
    fallback_value: object
    reason: str


class SubagentWorkflowWarningService:
    """Service for handling subagent workflow config warnings."""

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


class SubagentWorkflowConfigService:
    """Service for validating and coercing subagent workflow config fields."""

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


def _coerce_review_rounds_value(raw_review_rounds: object) -> int | None:
    # LLM: review-round parsing stays outside normalization so warning writes stay shallow.
    if isinstance(raw_review_rounds, bool):
        return None
    if isinstance(raw_review_rounds, int):
        return raw_review_rounds
    if isinstance(raw_review_rounds, str) and raw_review_rounds.strip().isdigit():
        return int(raw_review_rounds.strip())
    return None
