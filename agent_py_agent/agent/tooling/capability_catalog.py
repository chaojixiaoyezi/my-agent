# LLM: Capability catalog tools expose visible skill/tool cards to models without granting execution.
# 模块用途: 提供模型可调用的 capability 搜索和详情工具；只披露目录信息，不执行或授权能力。

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..capability.grants import CapabilityGrantScope, filter_cards_by_grant_scope
from ..capability.router import CapabilityCard, CapabilityRouter, from_skill_card, from_tool_spec
from ..capability.skills import SkillRegistry
from ..capability.usage import CapabilityUsageStore
from .models import BaseTool, ToolExecutionResult, ToolSpec

SCHEMA_VERSION = "capability_catalog.v1"
_SEARCH_TOOL_NAME = "capability_search"
_DESCRIBE_TOOL_NAME = "capability_describe"


# LLM: CapabilitySearchParams is the model-facing search Query bundle after normalization.
# 类用途: 保存 capability_search 的查询文本、类型过滤和结果上限。
@dataclass(frozen=True)
class CapabilitySearchParams:
    query: str
    kind: str = ""
    limit: int = 5


# LLM: CapabilityDescribeParams is the model-facing describe Request bundle after normalization.
# 类用途: 保存 capability_describe 的 id 或 kind/name 详情请求。
@dataclass(frozen=True)
class CapabilityDescribeParams:
    id: str
    kind: str = ""
    name: str = ""


# LLM: CapabilityCatalogRequest wraps normalized catalog params without using a long-lived dict bundle.
# 类用途: 承载 capability 目录工具的结构化请求参数。
@dataclass(frozen=True)
class CapabilityCatalogRequest:
    params: CapabilitySearchParams | CapabilityDescribeParams


# LLM: CapabilitySearchTool exposes visible catalog matches only; it never executes or grants capabilities.
# 类用途: 模型可调用的 capability 搜索工具，返回当前可见的 tool/skill 目录卡片。
class CapabilitySearchTool(BaseTool):
    """Search visible capability cards without executing the underlying tool or skill."""

    # LLM: CapabilitySearchTool.__init__ snapshots visible specs for disclosure-only search.
    # 函数用途: 初始化搜索目录，不执行工具或读取 skill 正文。
    def __init__(
        self,
        *,
        tool_specs: list[ToolSpec],
        skill_registry: SkillRegistry | None = None,
        extra_cards: list[CapabilityCard] | None = None,
        grant_scope: CapabilityGrantScope | None = None,
        usage_store: CapabilityUsageStore | None = None,
    ):
        self._catalog = _CapabilityCatalog(
            tool_specs=tool_specs,
            skill_registry=skill_registry,
            extra_cards=extra_cards,
            grant_scope=grant_scope,
            usage_store=usage_store,
        )
        self.spec = build_capability_search_spec()

    # LLM: CapabilitySearchTool.execute validates the query and returns JSON catalog hits.
    # 函数用途: 执行 capability 目录搜索，输出可解析 JSON。
    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        request = CapabilityCatalogRequest(params=_search_params(params))
        if not request.params.query:
            return _error_result(_SEARCH_TOOL_NAME, "INVALID_CAPABILITY_QUERY", "缺少 query。")
        hits = self._catalog.search(request.params)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "results": [
                _card_payload(hit.card, reasons=hit.reasons)
                for hit in hits
                if self._catalog.is_visible(hit.card.id)
            ],
        }
        return _ok_result(_SEARCH_TOOL_NAME, payload)


# LLM: CapabilityDescribeTool exposes one visible catalog card only; it never executes or grants capabilities.
# 类用途: 模型可调用的 capability 详情工具，披露风险、副作用和来源。
class CapabilityDescribeTool(BaseTool):
    """Describe one visible capability card without executing or authorizing it."""

    # LLM: CapabilityDescribeTool.__init__ snapshots visible specs for detail lookup.
    # 函数用途: 初始化详情目录，不打开未授权资源。
    def __init__(
        self,
        *,
        tool_specs: list[ToolSpec],
        skill_registry: SkillRegistry | None = None,
        extra_cards: list[CapabilityCard] | None = None,
        grant_scope: CapabilityGrantScope | None = None,
        usage_store: CapabilityUsageStore | None = None,
    ):
        self._catalog = _CapabilityCatalog(
            tool_specs=tool_specs,
            skill_registry=skill_registry,
            extra_cards=extra_cards,
            grant_scope=grant_scope,
            usage_store=usage_store,
        )
        self.spec = build_capability_describe_spec()

    # LLM: CapabilityDescribeTool.execute validates id/kind/name and returns one JSON detail object.
    # 函数用途: 执行 capability 详情披露，未知或不可见时返回结构化错误。
    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        request = CapabilityCatalogRequest(params=_describe_params(params))
        capability_id = _describe_id(request.params)
        if not capability_id:
            return _error_result(_DESCRIBE_TOOL_NAME, "INVALID_CAPABILITY_ID", "缺少 id，或缺少 kind/name。")
        card = self._catalog.describe(capability_id)
        if card is None:
            return _error_result(
                _DESCRIBE_TOOL_NAME,
                "CAPABILITY_NOT_FOUND",
                f"未知或不可见 capability: {capability_id}",
            )
        payload = {
            "schema_version": SCHEMA_VERSION,
            "detail": _card_payload(card, include_detail=True),
        }
        return _ok_result(_DESCRIBE_TOOL_NAME, payload)


# LLM: _CapabilityCatalog owns the visible card snapshot used by both catalog tools.
# 类用途: 把传入的 ToolSpec/SkillRegistry 转为可搜索、可描述的可见能力卡集合。
class _CapabilityCatalog:
    # LLM: _CapabilityCatalog.__init__ builds a router while preserving a stricter visible-id filter.
    # 函数用途: 初始化能力目录快照，避免暴露未传入的安全工具。
    def __init__(
        self,
        *,
        tool_specs: list[ToolSpec],
        skill_registry: SkillRegistry | None,
        extra_cards: list[CapabilityCard] | None,
        grant_scope: CapabilityGrantScope | None,
        usage_store: CapabilityUsageStore | None,
    ):
        self._tool_specs = list(tool_specs)
        self._skill_registry = skill_registry
        self._extra_cards = list(extra_cards or [])
        self._grant_scope = grant_scope
        self._usage_store = usage_store
        self._visible_cards: dict[str, CapabilityCard] = {}
        self._visible_ids: set[str] = set()
        self._router = CapabilityRouter(
            skill_registry=skill_registry,
            tool_specs=tool_specs,
            extra_cards=extra_cards,
            grant_scope=grant_scope,
            usage_store=usage_store,
        )
        self._refresh()

    # LLM: _CapabilityCatalog.search filters router hits back to the visible snapshot.
    # 函数用途: 查询能力卡并按 limit 返回可见命中。
    def search(self, params: CapabilitySearchParams):
        self._refresh()
        kinds = {params.kind} if params.kind else None
        visible_hits = []
        for hit in self._router.search(params.query, limit=0, kinds=kinds):
            if hit.card.id in self._visible_ids:
                visible_hits.append(hit)
        if params.limit == 0:
            return visible_hits
        return visible_hits[: params.limit]

    # LLM: _CapabilityCatalog.describe returns only cards present in the visible snapshot.
    # 函数用途: 按 capability id 获取可见详情卡。
    def describe(self, capability_id: str) -> CapabilityCard | None:
        self._refresh()
        if capability_id not in self._visible_ids:
            return None
        return self._visible_cards[capability_id]

    # LLM: _CapabilityCatalog.is_visible is a defensive guard for tool output construction.
    # 函数用途: 判断 capability id 是否属于当前可见目录。
    def is_visible(self, capability_id: str) -> bool:
        return capability_id in self._visible_ids

    # LLM: _CapabilityCatalog._refresh keeps runtime-promoted skills visible without rebuilding ToolRegistry.
    # 函数用途: 重新扫描 skill registry 并刷新可见 card/router 快照。
    def _refresh(self) -> None:
        if self._skill_registry is not None and self._skill_registry.skill_dirs:
            self._skill_registry.scan()
        self._visible_cards = _visible_cards(
            self._tool_specs,
            self._skill_registry,
            self._extra_cards,
            self._grant_scope,
        )
        self._visible_ids = set(self._visible_cards)
        self._router = CapabilityRouter(
            skill_registry=self._skill_registry,
            tool_specs=self._tool_specs,
            extra_cards=self._extra_cards,
            grant_scope=self._grant_scope,
            usage_store=self._usage_store,
        )


# LLM: build_capability_search_spec keeps the model-facing search tool metadata narrow.
# 函数用途: 构建 capability_search 的工具说明。
def build_capability_search_spec() -> ToolSpec:
    return ToolSpec(
        name=_SEARCH_TOOL_NAME,
        category="capability",
        description="Catalog search for visible tools and skills; disclosure only.",
        use_cases=[
            "Inspect the visible tool or skill catalog before selecting an action",
            "Find catalog entries by name, category, keyword, or expected use",
        ],
        avoid_when=["已经知道精确 id 且只需要详情时，使用 capability_describe"],
        keywords=["capability_catalog", "tool_catalog", "skill_catalog", "catalog_search"],
        parameters={
            "query": "搜索文本",
            "kind": "可选；tool 或 skill",
            "limit": "可选；最大结果数，0 表示不限制",
            "request": "可选 request bundle，包含 query/kind/limit",
        },
    )


# LLM: build_capability_describe_spec keeps the model-facing detail tool metadata narrow.
# 函数用途: 构建 capability_describe 的工具说明。
def build_capability_describe_spec() -> ToolSpec:
    return ToolSpec(
        name=_DESCRIBE_TOOL_NAME,
        category="capability",
        description="Catalog detail view for one visible tool or skill; disclosure only.",
        use_cases=["Open one catalog entry after capability_search returns its id"],
        avoid_when=["需要执行工具或申请授权时；本工具不会执行也不会授权"],
        keywords=["capability_catalog", "catalog_detail", "tool_detail", "skill_detail"],
        parameters={
            "id": "capability id，例如 tool:read_file 或 skill:python-debug",
            "kind": "可选；和 name 搭配构造 id",
            "name": "可选；和 kind 搭配构造 id",
            "request": "可选 request bundle，包含 id 或 kind/name",
        },
    )


# LLM: _visible_cards maps visible specs and scanned skills into capability cards.
# 函数用途: 生成工具/技能 capability card 的可见集合。
def _visible_cards(
    tool_specs: list[ToolSpec],
    skill_registry: SkillRegistry | None,
    extra_cards: list[CapabilityCard] | None,
    grant_scope: CapabilityGrantScope | None,
) -> dict[str, CapabilityCard]:
    cards: dict[str, CapabilityCard] = {}
    if skill_registry is not None:
        for skill_card in skill_registry.cards():
            card = from_skill_card(skill_card)
            cards[card.id] = card
    for spec in tool_specs:
        card = from_tool_spec(spec)
        cards[card.id] = card
    for card in extra_cards or []:
        cards[card.id] = card
    if grant_scope is None:
        return cards
    return {card.id: card for card in filter_cards_by_grant_scope(list(cards.values()), grant_scope)}


# LLM: _search_params accepts flat or request-bundled model input and normalizes it.
# 函数用途: 解析 capability_search 参数。
def _search_params(params: dict[str, Any]) -> CapabilitySearchParams:
    normalized = _normalized_params(params)
    return CapabilitySearchParams(
        query=str(normalized.get("query") or "").strip(),
        kind=str(normalized.get("kind") or "").strip(),
        limit=_limit(normalized.get("limit"), default=5),
    )


# LLM: _describe_params accepts flat or request-bundled model input and normalizes it.
# 函数用途: 解析 capability_describe 参数。
def _describe_params(params: dict[str, Any]) -> CapabilityDescribeParams:
    normalized = _normalized_params(params)
    return CapabilityDescribeParams(
        id=str(normalized.get("id") or normalized.get("capability_id") or "").strip(),
        kind=str(normalized.get("kind") or "").strip(),
        name=str(normalized.get("name") or "").strip(),
    )


# LLM: _normalized_params supports request bundles while keeping top-level fields authoritative.
# 函数用途: 合并 flat 参数和 request 参数包。
def _normalized_params(params: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    request = params.get("request")
    if isinstance(request, dict):
        normalized.update(request)
    for key, value in params.items():
        if key != "request":
            normalized[key] = value
    return normalized


# LLM: _limit clamps invalid or negative model-provided limits to a safe non-negative integer.
# 函数用途: 解析结果数量上限。
def _limit(value: Any, *, default: int) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return default
    return max(limit, 0)


# LLM: _describe_id derives a stable capability id from explicit id or kind/name.
# 函数用途: 生成详情查询 id。
def _describe_id(params: CapabilityDescribeParams) -> str:
    if params.id:
        return params.id
    if params.kind and params.name:
        return f"{params.kind}:{params.name}"
    return ""


# LLM: _card_payload serializes a capability card without implying authorization or execution.
# 函数用途: 转换 capability card 为 JSON 结果字段。
def _card_payload(
    card: CapabilityCard,
    *,
    reasons: list[str] | None = None,
    include_detail: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": card.id,
        "kind": card.kind,
        "name": card.name,
        "description": card.description,
        "risk": card.risk_level,
        "side_effects": list(card.side_effects),
        "source": card.source,
        "reasons": list(reasons or []),
    }
    if include_detail:
        payload.update(
            {
                "capabilities": list(card.capabilities),
                "when_to_use": list(card.when_to_use),
                "not_when_to_use": list(card.not_when_to_use),
                "metadata": dict(card.metadata),
                "authorization": "not_granted",
                "execution": "not_executed",
            }
        )
    return payload


# LLM: _ok_result formats successful catalog tool output as JSON.
# 函数用途: 构造成功 ToolExecutionResult。
def _ok_result(tool_name: str, payload: dict[str, Any]) -> ToolExecutionResult:
    return ToolExecutionResult(tool_name, True, json.dumps(payload, ensure_ascii=False, indent=2))


# LLM: _error_result formats catalog validation failures as JSON.
# 函数用途: 构造失败 ToolExecutionResult。
def _error_result(tool_name: str, code: str, message: str) -> ToolExecutionResult:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "error": {
            "code": code,
            "message": message,
        },
    }
    return ToolExecutionResult(tool_name, False, json.dumps(payload, ensure_ascii=False, indent=2), error_code=code)
