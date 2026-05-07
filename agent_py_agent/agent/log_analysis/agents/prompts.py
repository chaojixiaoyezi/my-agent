# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

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


# LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 SecurityPromptConfig 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 SecurityPromptConfig 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class SecurityPromptConfig:
    """Security prompt config, defaulting to ordinary agent behavior."""

    security_prompt_enabled: bool = False
    security_prompt_mode: str = "off"

    # LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 from_mapping 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 从外部数据还原 from mapping 需要的领域对象，统一缺省值和兼容字段。
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

    # LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 to_dict 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 把 to dict 对应对象转换成字典、JSON 或文本形态，供持久化和输出层复用。
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 SecurityPromptScope 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 SecurityPromptScope 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class SecurityPromptScope:
    # LLM: Scope stays bundled so security prompt APIs do not grow loose kwargs again.
    role: str = ""
    case_summary: str = ""
    is_security_command: bool = False
    is_logs_command: bool = False
    is_security_case: bool = False


# LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 should_inject_security_prompt 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 should inject security prompt 在当前模块中的核心转换或协调步骤，衔接 agent 协作层定义日志分析 prompt、契约和总结结构。
def should_inject_security_prompt(
    config: SecurityPromptConfig | Mapping[str, Any] | None,
    *,
    scope: SecurityPromptScope | None = None,
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

    prompt_scope = scope or SecurityPromptScope()
    normalized_role = prompt_scope.role.strip().lower()
    return bool(
        prompt_scope.is_security_command
        or prompt_scope.is_logs_command
        or prompt_scope.is_security_case
        or normalized_role in SECURITY_SUBAGENT_ROLES
    )


# LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 security_prompt_fragment 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 security prompt fragment 在当前模块中的核心转换或协调步骤，衔接 agent 协作层定义日志分析 prompt、契约和总结结构。
def security_prompt_fragment(
    config: SecurityPromptConfig | Mapping[str, Any] | None,
    *,
    scope: SecurityPromptScope | None = None,
) -> str:
    prompt_config = _prompt_config(config)
    prompt_scope = scope or SecurityPromptScope()
    if not should_inject_security_prompt(prompt_config, scope=prompt_scope):
        return ""

    lines = _security_prompt_lines(prompt_config.security_prompt_mode)
    _append_case_summary(lines, prompt_scope.case_summary)

    return "\n".join(lines).strip()


# LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 build_security_prompt 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 build security prompt 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
def build_security_prompt(
    base_prompt: str,
    config: SecurityPromptConfig | Mapping[str, Any] | None = None,
    *,
    scope: SecurityPromptScope | None = None,
) -> str:
    """Append security instructions only when explicitly enabled and scoped.

    With the default config, the return value is byte-for-byte the input prompt."""

    fragment = security_prompt_fragment(
        config,
        scope=scope,
    )
    if not fragment:
        return base_prompt
    if not base_prompt:
        return fragment
    return f"{base_prompt.rstrip()}\n\n{fragment}"


append_security_prompt = build_security_prompt


# LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 _prompt_config 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 prompt config 在当前模块中的核心转换或协调步骤，衔接 agent 协作层定义日志分析 prompt、契约和总结结构。
def _prompt_config(config: SecurityPromptConfig | Mapping[str, Any] | None) -> SecurityPromptConfig:
    return config if isinstance(config, SecurityPromptConfig) else SecurityPromptConfig.from_mapping(config)


# LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 _security_prompt_lines 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 security prompt lines 在当前模块中的核心转换或协调步骤，衔接 agent 协作层定义日志分析 prompt、契约和总结结构。
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


# LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 _minimal_security_lines 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 minimal security lines 在当前模块中的核心转换或协调步骤，衔接 agent 协作层定义日志分析 prompt、契约和总结结构。
def _minimal_security_lines() -> list[str]:
    return [
        "- Every conclusion must cite evidence_refs.",
        "- Separate facts, inferences, gaps, and next actions.",
        "- Use only authorized bounded tools such as security_query, security_hunt_ip, and security_trace_case.",
        "- Security tools return summary, preview_rows, and evidence_refs; do not request or paste full raw rows into the prompt.",
    ]


# LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 _analyst_security_lines 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 analyst security lines 在当前模块中的核心转换或协调步骤，衔接 agent 协作层定义日志分析 prompt、契约和总结结构。
def _analyst_security_lines() -> list[str]:
    return [
        "- Analyst output must follow the AnalystReport contract: case_id, summary, evidence_refs, facts, inferences, gaps, next_actions, confidence.",
        "- Reviewer output must reject reports without evidence_refs.",
        "- Ask for targeted follow-up queries instead of expanding raw log context.",
    ]


# LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 _incident_security_lines 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 incident security lines 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _incident_security_lines() -> list[str]:
    return [
        "- Prioritize P0/P1 routing, affected entities, containment options, and time-bounded gaps.",
        "- Recommendations are recommend-only unless an explicit response authority is granted.",
    ]


# LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 _append_case_summary 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 append case summary 相关记录，集中处理目标路径、格式化和状态更新。
def _append_case_summary(lines: list[str], case_summary: str) -> None:
    if case_summary:
        lines.extend(["", "## Case Summary", case_summary.strip()])
