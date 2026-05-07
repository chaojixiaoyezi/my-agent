# LLM: Subagent workflow planner module; keep route, compile, and acceptance bundle shapes stable.
# 模块用途: 拆分子代理工作流的规划、编译、验收或存储逻辑。

from __future__ import annotations

"""parent-side acceptance planning keeps final verification outside worker self-report."""

from dataclasses import dataclass, field, is_dataclass
from typing import Any

from .models import WorkflowTemplate


# LLM: ParentAcceptanceItem 属于子代理工作流编排的类边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 类用途: 集中保存父级验收条目字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发模板选择、步骤编译和验收策略相关副作用，需保持公开契约稳定。
@dataclass(frozen=True)
class ParentAcceptanceItem:
    """One check the parent session must perform before accepting delivery."""

    id: str
    text: str
    source: str
    category: str = "acceptance"
    required: bool = True


# LLM: ParentAcceptancePlan 属于子代理工作流编排的类边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 类用途: 集中保存父级验收计划字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发模板选择、步骤编译和验收策略相关副作用，需保持公开契约稳定。
@dataclass(frozen=True)
class ParentAcceptancePlan:
    """Structured final-gate checklist assembled for a workflow template."""

    template_id: str
    goal: str
    items: list[ParentAcceptanceItem] = field(default_factory=list)
    final_gate: str = "parent_final_gate"

    # LLM: checklist 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
    # 函数用途: 校验checklist需要的输入和状态，不满足时把错误明确反馈给调用方；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    @property
    def checklist(self) -> list[str]:
        """Return human-readable check text for renderers that need strings."""

        return [item.text for item in self.items]


# LLM: _AcceptanceItemSource 属于子代理工作流编排的类边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 类用途: 集中保存验收条目source字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发模板选择、步骤编译和验收策略相关副作用，需保持公开契约稳定。
@dataclass(frozen=True)
class _AcceptanceItemSource:
    """Bundle shared labels for parent acceptance item expansion."""

    source: str
    category: str
    id_prefix: str
    transform: Any | None = None


_DEFAULT_ANTI_ACCEPTANCE_ITEMS = (
    (
        "anti_worker_pass",
        "Do not accept worker self-reported PASS as proof; parent must verify independently.",
    ),
    (
        "anti_real_artifacts",
        "Inspect the real changed artifacts, files, outputs, or behavior before acceptance.",
    ),
    (
        "anti_evidence_paths",
        "Check that cited evidence paths, commands, logs, or URLs exist and support the claim.",
    ),
    (
        "anti_residual_risk",
        "Review residual risks, deferred work, and allowed degradation before deciding.",
    ),
    (
        "anti_parent_final_gate",
        "Keep final acceptance in the parent session final gate; workers cannot self-accept.",
    ),
)


# LLM: plan_parent_acceptance 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 处理计划父级验收相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持模板选择、步骤编译和验收策略上的返回值和副作用边界稳定。
def plan_parent_acceptance(
    template: WorkflowTemplate,
    *,
    goal: str,
    quality_contract: object | None = None,
) -> ParentAcceptancePlan:
    """Build the parent final-gate checklist for a selected workflow template."""

    items: list[ParentAcceptanceItem] = []
    contract = _contract_mapping(quality_contract)

    _extend_items(
        items,
        template.parent_acceptance,
        _AcceptanceItemSource("template.parent_acceptance", "template", "template"),
    )
    _extend_contract_items(items, contract)
    _add_default_anti_acceptance_items(items)

    if _requires_reviewer_result(template, contract):
        items.append(
            ParentAcceptanceItem(
                id="critic_or_reviewer_result",
                text="Require an existing critic or reviewer result and confirm repair responded to accepted findings.",
                source="workflow.critic_gate",
                category="critic_gate",
            )
        )

    return ParentAcceptancePlan(template_id=template.id, goal=goal, items=_dedupe_items(items))


# LLM: _add_default_anti_acceptance_items 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 处理adddefaultanti验收条目相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持模板选择、步骤编译和验收策略上的返回值和副作用边界稳定。
def _add_default_anti_acceptance_items(items: list[ParentAcceptanceItem]) -> None:
    """Append the default anti-acceptance items that prevent self-acceptance."""
    for item_id, text in _DEFAULT_ANTI_ACCEPTANCE_ITEMS:
        items.append(
            ParentAcceptanceItem(
                id=item_id,
                text=text,
                source="default.anti_acceptance",
                category="anti_acceptance",
            )
        )


# LLM: _extend_contract_items 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 处理extendcontract条目相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持模板选择、步骤编译和验收策略上的返回值和副作用边界稳定。
def _extend_contract_items(
    items: list[ParentAcceptanceItem],
    contract: dict[str, object],
) -> None:
    """Extend items from quality contract fields."""
    field_configs = (
        ("must_check", "quality_contract.must_check", "quality_contract", "must_check"),
        ("sampling_plan", "quality_contract.sampling_plan", "quality_contract", "sampling_plan"),
        ("evidence_required", "quality_contract.evidence_required", "evidence", "evidence_required"),
        (
            "forbidden_delivery",
            "quality_contract.forbidden_delivery",
            "forbidden_delivery",
            "forbidden_delivery",
        ),
    )
    for key, source, category, id_prefix in field_configs:
        transform = (
            (lambda text: f"Reject delivery if it includes forbidden condition: {text}")
            if key == "forbidden_delivery"
            else None
        )
        _extend_items(
            items,
            _as_list(contract.get(key)),
            _AcceptanceItemSource(source, category, id_prefix, transform),
        )


# LLM: _extend_items 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 处理extend条目相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持模板选择、步骤编译和验收策略上的返回值和副作用边界稳定。
def _extend_items(
    items: list[ParentAcceptanceItem],
    values: list[str],
    item_source: _AcceptanceItemSource,
) -> None:
    for index, value in enumerate(values, start=1):
        text = item_source.transform(value) if item_source.transform else value
        items.append(
            ParentAcceptanceItem(
                id=f"{item_source.id_prefix}_{index}",
                text=text,
                source=item_source.source,
                category=item_source.category,
            )
        )


# LLM: _contract_mapping 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 处理contractmapping相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持模板选择、步骤编译和验收策略上的返回值和副作用边界稳定。
def _contract_mapping(value: object | None) -> dict[str, object]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if is_dataclass(value):
        return {
            name: getattr(value, name)
            for name in (
                "must_check",
                "sampling_plan",
                "evidence_required",
                "forbidden_delivery",
                "quality_bar",
            )
            if hasattr(value, name)
        }
    return {
        name: getattr(value, name)
        for name in (
            "must_check",
            "sampling_plan",
            "evidence_required",
            "forbidden_delivery",
            "quality_bar",
        )
        if hasattr(value, name)
    }


# LLM: _as_list 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 转换aslist的数据表示，保持跨模块传递时的字段含义一致；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _as_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item)]
    return [str(value)] if str(value) else []


# LLM: _requires_reviewer_result 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 校验requiresreviewer结果需要的输入和状态，不满足时把错误明确反馈给调用方；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
def _requires_reviewer_result(template: WorkflowTemplate, contract: dict[str, object]) -> bool:
    if template.id == "producer_critic_repair":
        return True
    if any(phase.kind in {"review", "critic", "reviewer"} for phase in template.phases):
        return True
    quality_bar = str(contract.get("quality_bar") or "").lower()
    return "high quality" in quality_bar or "critic" in quality_bar or "reviewer" in quality_bar


# LLM: _dedupe_items 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 处理dedupe条目相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持模板选择、步骤编译和验收策略上的返回值和副作用边界稳定。
def _dedupe_items(items: list[ParentAcceptanceItem]) -> list[ParentAcceptanceItem]:
    seen: set[tuple[str, str]] = set()
    deduped: list[ParentAcceptanceItem] = []
    for item in items:
        key = (item.source or "", (item.text or "").strip().lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped
