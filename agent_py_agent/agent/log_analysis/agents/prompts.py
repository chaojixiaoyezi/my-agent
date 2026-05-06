from __future__ import annotations

"""Security prompt switch for log-analysis workflows."""

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

SECURITY_PROMPT_MODES = {"off", "minimal", "analyst", "incident"}
SECURITY_SUBAGENT_ROLES = {
    "triage-agent",
    "route-agent",
    "hunt-agent",
    "forensic-agent",
    "response-agent",
    "review-agent",
    "analyst-agent",
    "reviewer-agent",
}


@dataclass(frozen=True)
class SecurityPromptConfig:
    """Security prompt config, defaulting to ordinary agent behavior."""

    security_prompt_enabled: bool = False
    security_prompt_mode: str = "off"

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> SecurityPromptConfig:
        if not payload:
            return cls()
        enabled = bool(payload.get("security_prompt_enabled", False))
        mode = str(payload.get("security_prompt_mode", "off") or "off").strip().lower()
        if mode not in SECURITY_PROMPT_MODES:
            mode = "off"
        if not enabled:
            mode = "off"
        return cls(security_prompt_enabled=enabled, security_prompt_mode=mode)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SecurityPromptScope:
    role: str = ""
    case_summary: str = ""
    is_security_command: bool = False
    is_logs_command: bool = False
    is_security_case: bool = False

    @classmethod
    def from_kwargs(cls, **kwargs: Any) -> SecurityPromptScope:
        return cls(
            role=str(kwargs.get("role", "")),
            case_summary=str(kwargs.get("case_summary", "")),
            is_security_command=bool(kwargs.get("is_security_command", False)),
            is_logs_command=bool(kwargs.get("is_logs_command", False)),
            is_security_case=bool(kwargs.get("is_security_case", False)),
        )


def should_inject_security_prompt(
    config: SecurityPromptConfig | Mapping[str, Any] | None,
    *,
    scope: SecurityPromptScope | None = None,
    **kwargs: Any,
) -> bool:
    prompt_config = (
        config
        if isinstance(config, SecurityPromptConfig)
        else SecurityPromptConfig.from_mapping(config)
    )
    if not prompt_config.security_prompt_enabled:
        return False
    if prompt_config.security_prompt_mode == "off":
        return False

    prompt_scope = scope or SecurityPromptScope.from_kwargs(**kwargs)
    normalized_role = prompt_scope.role.strip().lower()
    return bool(
        prompt_scope.is_security_command
        or prompt_scope.is_logs_command
        or prompt_scope.is_security_case
        or normalized_role in SECURITY_SUBAGENT_ROLES
    )


def security_prompt_fragment(
    config: SecurityPromptConfig | Mapping[str, Any] | None,
    *,
    scope: SecurityPromptScope | None = None,
    **kwargs: Any,
) -> str:
    prompt_config = _prompt_config(config)
    prompt_scope = scope or SecurityPromptScope.from_kwargs(**kwargs)
    if not should_inject_security_prompt(prompt_config, scope=prompt_scope):
        return ""

    lines = _security_prompt_lines(prompt_config.security_prompt_mode)
    _append_case_summary(lines, prompt_scope.case_summary)

    return "\n".join(lines).strip()


def build_security_prompt(base_prompt: str, config: SecurityPromptConfig | Mapping[str, Any] | None = None, **scope: Any) -> str:
    """Append security instructions only when explicitly enabled and scoped.

    With the default config, the return value is byte-for-byte the input prompt.
    """

    fragment = security_prompt_fragment(
        config,
        scope=SecurityPromptScope.from_kwargs(**scope),
    )
    if not fragment:
        return base_prompt
    if not base_prompt:
        return fragment
    return f"{base_prompt.rstrip()}\n\n{fragment}"


append_security_prompt = build_security_prompt


def _prompt_config(config: SecurityPromptConfig | Mapping[str, Any] | None) -> SecurityPromptConfig:
    return config if isinstance(config, SecurityPromptConfig) else SecurityPromptConfig.from_mapping(config)


def _security_prompt_lines(mode: str) -> list[str]:
    lines = [
        "# Security Log Analysis Context",
        "- Work from case summaries, route summaries, and evidence_refs only.",
        "- Do not paste raw events, long query results, or long transcripts into the parent session.",
        "- Treat evidence refs and evidence files as the auditable source of truth.",
    ]
    if mode in {"minimal", "analyst", "incident"}:
        lines.extend(_minimal_security_lines())
    if mode in {"analyst", "incident"}:
        lines.extend(_analyst_security_lines())
    if mode == "incident":
        lines.extend(_incident_security_lines())
    return lines


def _minimal_security_lines() -> list[str]:
    return [
        "- Every conclusion must cite evidence_refs.",
        "- Separate facts, inferences, gaps, and next actions.",
        "- Use only authorized bounded tools such as security_query, security_hunt_ip, and security_trace_case.",
        "- Security tools return summary, preview_rows, and evidence_refs; do not request or paste full raw rows into the prompt.",
    ]


def _analyst_security_lines() -> list[str]:
    return [
        "- Analyst output must follow the AnalystReport contract: case_id, summary, evidence_refs, facts, inferences, gaps, next_actions, confidence.",
        "- Reviewer output must reject reports without evidence_refs.",
        "- Ask for targeted follow-up queries instead of expanding raw log context.",
    ]


def _incident_security_lines() -> list[str]:
    return [
        "- Prioritize P0/P1 routing, affected entities, containment options, and time-bounded gaps.",
        "- Recommendations are recommend-only unless an explicit response authority is granted.",
    ]


def _append_case_summary(lines: list[str], case_summary: str) -> None:
    if case_summary:
        lines.extend(["", "## Case Summary", case_summary.strip()])
