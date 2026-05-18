# LLM: Subagent workflow planner module; keep route, compile, and acceptance bundle shapes stable.
# 模块用途: 拆分子代理工作流的规划、编译、验收或存储逻辑。

from __future__ import annotations

"""route parent goals to reusable workflow templates without mutating task state."""

import re
from dataclasses import dataclass, field
from typing import Any

from .store import WorkflowTemplateStore, load_template_store

SINGLE_WORKER_TEMPLATE_ID = "single_worker_verified"
CODE_FEATURE_TEMPLATE_ID = "code_feature_split"
PRODUCER_CRITIC_TEMPLATE_ID = "producer_critic_repair"
DEFAULT_MODE = "auto"
VALID_MODES = {"off", "manual", "auto"}


# LLM: WorkflowRouteDecision 属于子代理工作流编排的类边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 类用途: 集中保存工作流routedecision字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
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


# LLM: _RouteDecisionFields 属于子代理工作流编排的类边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 类用途: 集中保存routedecision字段字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发模板选择、步骤编译和验收策略相关副作用，需保持公开契约稳定。
@dataclass(frozen=True)
class _RouteDecisionFields:
    mode: str
    selected_template_id: str
    reason: str
    task_type: str
    risk_tags: list[str]
    needs_confirmation: bool
    available_template_ids: list[str]
    issues: list[str]


# LLM: _TemplateSelectionRequest 属于子代理工作流编排的类边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 类用途: 集中保存模板selection请求字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发模板选择、步骤编译和验收策略相关副作用，需保持公开契约稳定。
@dataclass(frozen=True)
class _TemplateSelectionRequest:
    explicit_template_id: str
    preferred_template_id: str
    store: WorkflowTemplateStore
    available_template_ids: list[str]
    issues: list[str]


# LLM: _RouteFieldsRequest 属于子代理工作流编排的类边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 类用途: 集中保存route字段请求字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发模板选择、步骤编译和验收策略相关副作用，需保持公开契约稳定。
@dataclass(frozen=True)
class _RouteFieldsRequest:
    # LLM: route decision projection keeps mode/template/task facts together.
    mode: str
    selected_template_id: str
    task_type: str
    risk_tags: list[str]
    available_template_ids: list[str]
    issues: list[str]
    reason: str = "Subagent workflow routing is disabled by config."


_STRUCTURED_ROUTE_FIELD_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*(?P<tail>.*)$"
)
_WORKFLOW_TEMPLATE_FIELD_NAMES = frozenset(
    {"workflow_template_id", "preferred_workflow_template", "subagent_workflow_template"}
)
_WORKFLOW_TASK_TYPE_FIELD_NAMES = frozenset({"workflow_task_type", "task_type"})
_WORKFLOW_RISK_TAGS_FIELD_NAMES = frozenset({"workflow_risk_tags", "risk_tags"})
_TASK_TYPE_TEMPLATE_MAP = {
    "quality_deliverable": PRODUCER_CRITIC_TEMPLATE_ID,
    "code_or_bugfix": CODE_FEATURE_TEMPLATE_ID,
    "simple": SINGLE_WORKER_TEMPLATE_ID,
}


# LLM: route_workflow 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 处理route工作流相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持模板选择、步骤编译和验收策略上的返回值和副作用边界稳定。
def route_workflow(
    goal: str,
    *,
    config: Any = None,
    template_store: WorkflowTemplateStore | None = None,
    explicit_template_id: str = "",
) -> WorkflowRouteDecision:
    """Choose a workflow template for a parent goal."""

    store, available_template_ids, issues, mode = _workflow_route_inputs(config, template_store)
    task_type, preferred_template_id, risk_tags = _classify_goal(goal)

    if mode == "off":
        return _disabled_route_decision(_route_fields(_RouteFieldsRequest(mode, "", task_type, risk_tags, available_template_ids, issues)))

    selected_template_id, reason = _select_template(
        _TemplateSelectionRequest(
            explicit_template_id=explicit_template_id,
            preferred_template_id=preferred_template_id,
            store=store,
            available_template_ids=available_template_ids,
            issues=issues,
        )
    )

    if selected_template_id and mode == "manual":
        reason = f"{reason} Manual mode requires parent confirmation before use."

    return _make_route_decision(
        _route_fields(
            _RouteFieldsRequest(mode, selected_template_id, task_type, risk_tags, available_template_ids, issues, reason)
        )
    )


# LLM: _disabled_route_decision 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 处理disabledroutedecision相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持模板选择、步骤编译和验收策略上的返回值和副作用边界稳定。
def _disabled_route_decision(fields: _RouteDecisionFields) -> WorkflowRouteDecision:
    """Build the off-mode route result without lengthening the public facade."""
    return _make_route_decision(fields)


# LLM: _route_fields 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 处理route字段相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持模板选择、步骤编译和验收策略上的返回值和副作用边界稳定。
def _route_fields(request: _RouteFieldsRequest) -> _RouteDecisionFields:
    return _RouteDecisionFields(
        mode=request.mode,
        selected_template_id=request.selected_template_id,
        reason=request.reason,
        task_type=request.task_type,
        risk_tags=request.risk_tags,
        needs_confirmation=request.mode == "manual",
        available_template_ids=request.available_template_ids,
        issues=request.issues,
    )


# LLM: _workflow_route_inputs 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 处理工作流routeinputs相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持模板选择、步骤编译和验收策略上的返回值和副作用边界稳定。
def _workflow_route_inputs(
    config: Any,
    template_store: WorkflowTemplateStore | None,
) -> tuple[WorkflowTemplateStore, list[str], list[str], str]:
    store = template_store or load_template_store()
    available_template_ids = [template.id for template in store.all()]
    issues = [_format_store_issue(issue) for issue in store.issues]
    mode = _workflow_mode(config, issues)
    return store, available_template_ids, issues, mode


# LLM: _make_route_decision 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 构建routedecision所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _make_route_decision(fields: _RouteDecisionFields) -> WorkflowRouteDecision:
    return WorkflowRouteDecision(
        mode=fields.mode,
        selected_template_id=fields.selected_template_id,
        reason=fields.reason,
        task_type=fields.task_type,
        risk_tags=fields.risk_tags,
        needs_confirmation=fields.needs_confirmation,
        available_template_ids=fields.available_template_ids,
        issues=fields.issues,
    )


# LLM: _workflow_mode 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 处理工作流mode相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持模板选择、步骤编译和验收策略上的返回值和副作用边界稳定。
def _workflow_mode(config: Any, issues: list[str]) -> str:
    raw_mode = getattr(config, "subagent_workflow_mode", DEFAULT_MODE)
    if isinstance(raw_mode, str):
        mode = raw_mode.strip().lower()
        if mode in VALID_MODES:
            return mode

    issues.append(f"invalid subagent_workflow_mode {raw_mode!r}; falling back to auto")
    return DEFAULT_MODE


# LLM: _select_template 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 读取或查询模板需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _select_template(request: _TemplateSelectionRequest) -> tuple[str, str]:
    explicit_template_id = request.explicit_template_id.strip()
    if explicit_template_id:
        if request.store.get(explicit_template_id) is not None:
            return explicit_template_id, f"Explicit workflow template requested: {explicit_template_id}."
        request.issues.append(f"explicit workflow template not found: {explicit_template_id}")

    if request.store.get(request.preferred_template_id) is not None:
        return request.preferred_template_id, f"Selected {request.preferred_template_id} for the classified task type."

    request.issues.append(f"preferred workflow template not available: {request.preferred_template_id}")
    if request.available_template_ids:
        fallback_template_id = request.available_template_ids[0]
        return fallback_template_id, f"Fell back to available workflow template: {fallback_template_id}."

    request.issues.append("no workflow templates are available")
    return "", "No workflow template could be selected."


# LLM: _classify_goal reads only structured workflow fields; the LLM can choose templates, but code must not infer them from prose keywords.
# 函数用途: 从 workflow_task_type、workflow_template_id、risk_tags 这类机器字段选择模板；普通自然语言目标默认走 single worker。
def _classify_goal(goal: str) -> tuple[str, str, list[str]]:
    fields = _workflow_goal_fields(goal)
    task_type = fields.get("task_type") or ("simple" if fields.get("template_id") else _fallback_task_type(goal))
    preferred = fields.get("template_id") or _TASK_TYPE_TEMPLATE_MAP.get(task_type, SINGLE_WORKER_TEMPLATE_ID)
    risk_tags = fields.get("risk_tags", [])
    if not risk_tags:
        risk_tags = ["explicit_workflow"] if task_type != "simple" or fields.get("template_id") else ["low_scope"]
    return task_type, preferred, risk_tags


# LLM: _fallback_task_type is a small workflow router fallback until LLM/template selection is externalized.
# 函数用途: workflow_mode=plan 时，把明确代码/bugfix/test 任务路由到代码拆分模板；普通任务仍走 single worker。
def _fallback_task_type(goal: str) -> str:
    text = str(goal or "").lower()
    code_tokens = ("bug", "fix", "api", "test", "tests", "code", "refactor", "implement", "function", "class")
    return "code_or_bugfix" if any(token in text for token in code_tokens) else "simple"


# LLM: _workflow_goal_fields extracts shallow protocol fields from a goal without interpreting prose.
# 函数用途: 支持 `workflow_task_type: code_or_bugfix`、`workflow_template_id: ...` 和 `risk_tags: a,b`。
def _workflow_goal_fields(goal: str) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for raw in str(goal or "").splitlines():
        match = _STRUCTURED_ROUTE_FIELD_RE.match(raw.strip())
        if match:
            _apply_workflow_goal_field(fields, match.group("field"), match.group("tail"))
    return fields


# LLM: _apply_workflow_goal_field maps one protocol field to the router field dict.
# 函数用途: 把 workflow 字段归一到 template_id/task_type/risk_tags，保持主解析循环浅。
def _apply_workflow_goal_field(fields: dict[str, Any], field_name: str, value: str) -> None:
    field = field_name.strip().lower()
    tail = value.strip()
    if not tail:
        return
    if field in _WORKFLOW_TEMPLATE_FIELD_NAMES:
        fields["template_id"] = _first_token(tail)
        return
    if field in _WORKFLOW_TASK_TYPE_FIELD_NAMES:
        fields["task_type"] = _first_token(tail)
        return
    if field in _WORKFLOW_RISK_TAGS_FIELD_NAMES:
        fields["risk_tags"] = _list_tokens(tail)


# LLM: _first_token keeps workflow field values machine-shaped.
# 函数用途: 从结构化字段里取第一个 id；忽略普通句子，避免把自然语言当模板 id。
def _first_token(value: str) -> str:
    tokens = _list_tokens(value)
    return tokens[0] if tokens else ""


# LLM: _list_tokens accepts identifier-like route values only.
# 函数用途: 解析逗号、顿号、竖线分隔的机器 token；中文长句不会生成 workflow 决策 token。
def _list_tokens(value: str) -> list[str]:
    return [
        item
        for item in re.split(r"[\s,，、|/]+", str(value or "").strip().lower())
        if re.fullmatch(r"[a-zA-Z0-9_-]+", item or "")
    ]


# LLM: _format_store_issue 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 渲染或汇总存储issue的展示文本，保持命令行、日志和审计输出一致；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
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
