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


def should_inject_security_prompt(
    config: SecurityPromptConfig | Mapping[str, Any] | None,
    *,
    role: str = "",
    is_security_command: bool = False,
    is_logs_command: bool = False,
    is_security_case: bool = False,
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

    normalized_role = role.strip().lower()
    return bool(
        is_security_command
        or is_logs_command
        or is_security_case
        or normalized_role in SECURITY_SUBAGENT_ROLES
    )


def security_prompt_fragment(
    config: SecurityPromptConfig | Mapping[str, Any] | None,
    *,
    role: str = "",
    case_summary: str = "",
    is_security_command: bool = False,
    is_logs_command: bool = False,
    is_security_case: bool = False,
) -> str:
    prompt_config = (
        config
        if isinstance(config, SecurityPromptConfig)
        else SecurityPromptConfig.from_mapping(config)
    )
    if not should_inject_security_prompt(
        prompt_config,
        role=role,
        is_security_command=is_security_command,
        is_logs_command=is_logs_command,
        is_security_case=is_security_case,
    ):
        return ""

    mode = prompt_config.security_prompt_mode
    lines = [
        "# Security Log Analysis Context",
        "- Work from case summaries, route summaries, and evidence_refs only.",
        "- Do not paste raw events, long query results, or long transcripts into the parent session.",
        "- Treat evidence refs and evidence files as the auditable source of truth.",
    ]

    if mode in {"minimal", "analyst", "incident"}:
        lines.extend(
            [
                "- Every conclusion must cite evidence_refs.",
                "- Separate facts, inferences, gaps, and next actions.",
                "- Use only authorized bounded tools such as security_query, security_hunt_ip, and security_trace_case.",
                "- Security tools return summary, preview_rows, and evidence_refs; do not request or paste full raw rows into the prompt.",
            ]
        )

    if mode in {"analyst", "incident"}:
        lines.extend(
            [
                "- Analyst output must follow the AnalystReport contract: case_id, summary, evidence_refs, facts, inferences, gaps, next_actions, confidence.",
                "- Reviewer output must reject reports without evidence_refs.",
                "- Ask for targeted follow-up queries instead of expanding raw log context.",
            ]
        )

    if mode == "incident":
        lines.extend(
            [
                "- Prioritize P0/P1 routing, affected entities, containment options, and time-bounded gaps.",
                "- Recommendations are recommend-only unless an explicit response authority is granted.",
            ]
        )

    if case_summary:
        lines.extend(["", "## Case Summary", case_summary.strip()])

    return "\n".join(lines).strip()


def build_security_prompt(
    base_prompt: str,
    config: SecurityPromptConfig | Mapping[str, Any] | None = None,
    *,
    role: str = "",
    case_summary: str = "",
    is_security_command: bool = False,
    is_logs_command: bool = False,
    is_security_case: bool = False,
) -> str:
    """Append security instructions only when explicitly enabled and scoped.

    With the default config, the return value is byte-for-byte the input prompt.
    """

    fragment = security_prompt_fragment(
        config,
        role=role,
        case_summary=case_summary,
        is_security_command=is_security_command,
        is_logs_command=is_logs_command,
        is_security_case=is_security_case,
    )
    if not fragment:
        return base_prompt
    if not base_prompt:
        return fragment
    return f"{base_prompt.rstrip()}\n\n{fragment}"


append_security_prompt = build_security_prompt
