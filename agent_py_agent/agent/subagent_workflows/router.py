from __future__ import annotations

"""Route parent goals to reusable subagent workflow templates."""

from dataclasses import dataclass, field
from typing import Any

from .store import WorkflowTemplateStore, load_template_store

SINGLE_WORKER_TEMPLATE_ID = "single_worker_verified"
CODE_FEATURE_TEMPLATE_ID = "code_feature_split"
PRODUCER_CRITIC_TEMPLATE_ID = "producer_critic_repair"
DEFAULT_MODE = "auto"
VALID_MODES = {"off", "manual", "auto"}


@dataclass
class WorkflowRouteDecision:
    """Structured decision returned by the workflow router."""

    mode: str
    selected_template_id: str
    reason: str
    task_type: str
    risk_tags: list[str] = field(default_factory=list)
    needs_confirmation: bool = False
    available_template_ids: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


def route_workflow(
    goal: str,
    *,
    config: Any = None,
    template_store: WorkflowTemplateStore | None = None,
    explicit_template_id: str = "",
) -> WorkflowRouteDecision:
    """Choose a workflow template for a parent goal."""

    store = template_store or load_template_store()
    available_template_ids = [template.id for template in store.all()]
    issues = [_format_store_issue(issue) for issue in store.issues]

    mode = _workflow_mode(config, issues)
    task_type, preferred_template_id, risk_tags = _classify_goal(goal)

    if mode == "off":
        return WorkflowRouteDecision(
            mode=mode,
            selected_template_id="",
            reason="Subagent workflow routing is disabled by config.",
            task_type=task_type,
            risk_tags=risk_tags,
            needs_confirmation=False,
            available_template_ids=available_template_ids,
            issues=issues,
        )

    selected_template_id, reason = _select_template(
        explicit_template_id=explicit_template_id,
        preferred_template_id=preferred_template_id,
        store=store,
        available_template_ids=available_template_ids,
        issues=issues,
    )

    if selected_template_id and mode == "manual":
        reason = f"{reason} Manual mode requires parent confirmation before use."

    return WorkflowRouteDecision(
        mode=mode,
        selected_template_id=selected_template_id,
        reason=reason,
        task_type=task_type,
        risk_tags=risk_tags,
        needs_confirmation=mode == "manual",
        available_template_ids=available_template_ids,
        issues=issues,
    )


def _workflow_mode(config: Any, issues: list[str]) -> str:
    raw_mode = getattr(config, "subagent_workflow_mode", DEFAULT_MODE)
    if isinstance(raw_mode, str):
        mode = raw_mode.strip().lower()
        if mode in VALID_MODES:
            return mode

    issues.append(f"invalid subagent_workflow_mode {raw_mode!r}; falling back to auto")
    return DEFAULT_MODE


def _select_template(
    *,
    explicit_template_id: str,
    preferred_template_id: str,
    store: WorkflowTemplateStore,
    available_template_ids: list[str],
    issues: list[str],
) -> tuple[str, str]:
    explicit_template_id = explicit_template_id.strip()
    if explicit_template_id:
        if store.get(explicit_template_id) is not None:
            return explicit_template_id, f"Explicit workflow template requested: {explicit_template_id}."
        issues.append(f"explicit workflow template not found: {explicit_template_id}")

    if store.get(preferred_template_id) is not None:
        return preferred_template_id, f"Selected {preferred_template_id} for the classified task type."

    issues.append(f"preferred workflow template not available: {preferred_template_id}")
    if available_template_ids:
        fallback_template_id = available_template_ids[0]
        return fallback_template_id, f"Fell back to available workflow template: {fallback_template_id}."

    issues.append("no workflow templates are available")
    return "", "No workflow template could be selected."


def _classify_goal(goal: str) -> tuple[str, str, list[str]]:
    text = goal.casefold()

    if _contains_any(goal, ("高质量", "文档", "报告", "界面", "翻译", "论文", "交付", "排版", "验收")):
        return "quality_deliverable", PRODUCER_CRITIC_TEMPLATE_ID, ["quality_bar", "review_needed"]

    if _contains_any(goal, ("代码", "修复", "报错", "功能", "开发", "实现", "测试", "重构", "日志", "安全", "模块", "命令")):
        return "code_or_bugfix", CODE_FEATURE_TEMPLATE_ID, ["code_change", "verification_needed"]

    quality_keywords = (
        "doc",
        "docs",
        "documentation",
        "readme",
        "pdf",
        "ui",
        "ux",
        "interface",
        "report",
        "presentation",
        "polish",
        "quality",
        "deliverable",
        "高质量",
        "文档",
        "报告",
        "界面",
    )
    if any(keyword in text for keyword in quality_keywords):
        return "quality_deliverable", PRODUCER_CRITIC_TEMPLATE_ID, ["quality_bar", "review_needed"]

    code_keywords = (
        "bug",
        "bugfix",
        "fix",
        "error",
        "exception",
        "traceback",
        "code",
        "feature",
        "implement",
        "refactor",
        "test",
        "api",
        "cli",
        "compile",
        "代码",
        "修复",
        "报错",
        "功能",
    )
    if any(keyword in text for keyword in code_keywords):
        return "code_or_bugfix", CODE_FEATURE_TEMPLATE_ID, ["code_change", "verification_needed"]

    return "simple", SINGLE_WORKER_TEMPLATE_ID, ["low_scope"]


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def _format_store_issue(issue: Any) -> str:
    parts = [str(getattr(issue, "message", issue))]
    template_id = getattr(issue, "template_id", "")
    source_path = getattr(issue, "source_path", "")
    field_name = getattr(issue, "field", "")
    if template_id:
        parts.append(f"template={template_id}")
    if field_name:
        parts.append(f"field={field_name}")
    if source_path:
        parts.append(f"source={source_path}")
    return " | ".join(parts)
