# LLM: Capability module; keep skill/tool routing contracts stable for planner and dispatch callers.
# 模块用途: 描述和路由 agent 能力、技能、工具和执行条件。

from __future__ import annotations

"""统一能力路由模块。

这里把 skill 和 tool 都抽象成 Capability Card。
后续无论是父代理给子代理下发 skill，还是下发 tool，都可以走同一套路由协议。
"""

from dataclasses import dataclass, field
from typing import Any

from ..tools import ToolSpec
from .config import CapabilityConfig
from .grants import CapabilityGrantScope, filter_cards_by_grant_scope
from .mcp import McpToolDescriptor, from_mcp_tool
from .scoring import score_card, tokenize
from .skills import SkillCard, SkillRegistry
from .usage import CapabilityUsageStore

_PLAYWRIGHT_CAPABILITIES = [
    "playwright",
    "browser_automation",
    "frontend_e2e",
    "ui_testing",
    "screenshot",
]
_PLAYWRIGHT_WHEN_TO_USE = [
    "需要真实浏览器打开页面、点击按钮、截图或验证前端流程时使用",
    "购物、登录、设置页、仪表盘等 UI E2E 验收需要可追溯证据时使用",
]
_PLAYWRIGHT_NOT_WHEN_TO_USE = [
    "只需要读取静态文件或做纯文本检查时不必启动浏览器",
    "没有父级授权 command/path scope 的子代理不能自行执行 shell",
]
_PLAYWRIGHT_KEYWORDS = [
    "playwright",
    "browser",
    "chrome",
    "chromium",
    "e2e",
    "ui",
    "frontend",
    "screenshot",
    "click",
    "form",
    "浏览器",
    "前端",
    "端到端",
    "截图",
    "按钮",
    "登录",
    "购物",
]


# LLM: CapabilityCard is a 能力路由 boundary object; coordinate field or method changes with callers, docs, and focused tests.
# 类用途: 统一能力卡片。 `kind` 当前主要是 `skill` 或 `tool`，但刻意保留成普通字符串。 后续如果要加入 resource、mcp、remote_agent，也不用改 schema。
@dataclass
class CapabilityCard:
    """统一能力卡片。

    `kind` 当前主要是 `skill` 或 `tool`，但刻意保留成普通字符串。
    后续如果要加入 resource、mcp、remote_agent，也不用改 schema。"""

    id: str
    kind: str
    name: str
    description: str
    capabilities: list[str] = field(default_factory=list)
    when_to_use: list[str] = field(default_factory=list)
    not_when_to_use: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    risk_level: str = "low"
    side_effects: list[str] = field(default_factory=list)
    source: str = ""
    path: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    # LLM: CapabilityCard.render_compact belongs to 能力路由; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 渲染短卡片。 `max_chars=0` 表示不限制。这里先用字符数兜底，未来接 tokenizer 后 再替换成精确 token 控制。。
    def render_compact(self, *, max_chars: int = 0) -> str:
        """渲染短卡片。

        `max_chars=0` 表示不限制。这里先用字符数兜底，未来接 tokenizer 后
        再替换成精确 token 控制。"""

        lines = [
            f"- {self.kind}:{self.name} [{self.risk_level}]",
            f"  说明：{self.description}",
        ]
        if self.capabilities:
            lines.append(f"  能力：{', '.join(self.capabilities)}")
        if self.when_to_use:
            lines.append(f"  何时使用：{'; '.join(self.when_to_use[:3])}")
        if self.not_when_to_use:
            lines.append(f"  不适用：{'; '.join(self.not_when_to_use[:2])}")
        if self.side_effects:
            lines.append(f"  副作用：{', '.join(self.side_effects)}")
        text = "\n".join(lines)
        if max_chars and len(text) > max_chars:
            return text[:max_chars] + "\n  ... 已截断"
        return text

    # LLM: CapabilityCard.render_detail is for disclosure only; it must not imply authorization or execution.
    # 函数用途: 渲染能力详情，给 CLI 和 capability 描述工具披露能力边界。
    def render_detail(self, *, max_chars: int = 0) -> str:
        lines = [
            f"# {self.kind}:{self.name}",
            f"id: {self.id}",
            f"risk_level: {self.risk_level}",
            f"source: {self.source or '-'}",
            "",
            self.description,
        ]
        if self.capabilities:
            lines.extend(["", "capabilities:", *[f"- {item}" for item in self.capabilities]])
        if self.when_to_use:
            lines.extend(["", "when_to_use:", *[f"- {item}" for item in self.when_to_use]])
        if self.not_when_to_use:
            lines.extend(["", "not_when_to_use:", *[f"- {item}" for item in self.not_when_to_use]])
        if self.side_effects:
            lines.extend(["", "side_effects:", *[f"- {item}" for item in self.side_effects]])
        if self.keywords:
            lines.extend(["", f"keywords: {', '.join(self.keywords)}"])
        text = "\n".join(lines)
        if max_chars and len(text) > max_chars:
            return text[:max_chars].rstrip() + "\n... 已截断"
        return text


# LLM: CapabilitySearchHit is a 能力路由 boundary object; coordinate field or method changes with callers, docs, and focused tests.
# 类用途: 能力检索命中结果。
@dataclass
class CapabilitySearchHit:
    """能力检索命中结果。"""

    card: CapabilityCard
    score: float
    reasons: list[str]


# LLM: CapabilityRouter is a 能力路由 boundary object; coordinate field or method changes with callers, docs, and focused tests.
# 类用途: 统一能力路由器。 它不负责执行工具，也不负责展开 skill 正文。 它只回答一个问题：当前能力缺口最可能需要哪些 skill/tool card？
class CapabilityRouter:
    """统一能力路由器。

    它不负责执行工具，也不负责展开 skill 正文。
    它只回答一个问题：当前能力缺口最可能需要哪些 skill/tool card？"""

    # LLM: CapabilityRouter.__init__ belongs to 能力路由; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 初始化实例依赖和字段，不应在构造阶段做难以回滚的重副作用；它是 CapabilityRouter 的方法，通常依赖实例字段。
    def __init__(
        self,
        *,
        config: CapabilityConfig | None = None,
        skill_registry: SkillRegistry | None = None,
        tool_specs: list[ToolSpec] | None = None,
        extra_cards: list[CapabilityCard] | None = None,
        usage_store: CapabilityUsageStore | None = None,
        grant_scope: CapabilityGrantScope | None = None,
    ):
        self.config = config or CapabilityConfig()
        self._cards: dict[str, CapabilityCard] = {}
        self.usage_store = usage_store
        self.grant_scope = grant_scope
        if skill_registry is not None:
            for card in skill_registry.cards():
                self.register(from_skill_card(card))
        for spec in tool_specs or []:
            self.register(from_tool_spec(spec))
        for card in default_capability_cards():
            self.register(card)
        for card in extra_cards or []:
            self.register(card)

    # LLM: CapabilityRouter.register belongs to 能力路由; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 注册或覆盖一张能力卡。。
    def register(self, card: CapabilityCard) -> None:
        """注册或覆盖一张能力卡。"""

        self._cards[card.id] = card

    # LLM: CapabilityRouter.cards belongs to 能力路由; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 返回当前能力卡。。
    def cards(self, *, kinds: set[str] | None = None) -> list[CapabilityCard]:
        """返回当前能力卡。"""

        cards = list(self._cards.values())
        if kinds is None:
            return filter_cards_by_grant_scope(cards, self.grant_scope)
        return filter_cards_by_grant_scope([card for card in cards if card.kind in kinds], self.grant_scope)

    # LLM: CapabilityRouter.get supports disclosure by stable id or unambiguous name without reading skill bodies.
    # 函数用途: 按 id、kind:name 或唯一 name 查找能力卡。
    def get(self, identifier: str) -> CapabilityCard | None:
        key = str(identifier or "").strip()
        if not key:
            return None
        if key in self._cards:
            return self._cards[key]
        matches = [
            card
            for card in self._cards.values()
            if card.name == key or f"{card.kind}:{card.name}" == key
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    # LLM: CapabilityRouter.describe exposes capability boundaries; it does not grant or execute anything.
    # 函数用途: 渲染单张能力卡的详情，供用户/模型确认何时使用。
    def describe(self, identifier: str, *, max_chars: int = 0) -> str:
        card = self.get(identifier)
        if card is None:
            raise KeyError(f"未知 capability: {identifier}")
        return card.render_detail(max_chars=max_chars)

    # LLM: CapabilityRouter.render_catalog gives a bounded visible inventory for CLI and model-facing catalog tools.
    # 函数用途: 输出能力目录摘要，支持按 kind 过滤和数量上限。
    def render_catalog(
        self,
        *,
        kinds: set[str] | None = None,
        limit: int = 0,
        max_chars: int = 0,
    ) -> str:
        cards = sorted(self.cards(kinds=kinds), key=lambda item: (item.kind, item.name, item.id))
        if limit > 0:
            cards = cards[:limit]
        if not cards:
            return "# Capability Catalog\n当前没有匹配的 capability card。"
        text = "# Capability Catalog\n" + "\n\n".join(card.render_compact() for card in cards)
        if max_chars and len(text) > max_chars:
            return text[:max_chars].rstrip() + "\n... 已截断"
        return text

    # LLM: CapabilityRouter.search belongs to 能力路由; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 检索候选能力。 `limit=None` 时使用配置里的 `capability_candidate_limit`。 `limit=0` 表示不限制。。
    def search(
        self,
        query: str,
        *,
        limit: int | None = None,
        kinds: set[str] | None = None,
    ) -> list[CapabilitySearchHit]:
        """检索候选能力。

        `limit=None` 时使用配置里的 `capability_candidate_limit`。
        `limit=0` 表示不限制。"""

        effective_limit = self.config.capability_candidate_limit if limit is None else limit
        hits: list[CapabilitySearchHit] = []
        for card in self.cards(kinds=kinds):
            score, reasons = score_card(query, card)
            score, reasons = self._apply_usage_feedback(card, score, reasons)
            if score > 0:
                hits.append(CapabilitySearchHit(card=card, score=score, reasons=reasons[:4]))
        hits.sort(key=lambda item: (-item.score, item.card.kind, item.card.name))
        if effective_limit == 0:
            return hits
        return hits[:effective_limit]

    # LLM: _apply_usage_feedback lets routing learn from accepted/rejected choices without changing base keyword scoring.
    # 函数用途: 根据能力使用记录调整分数，并追加可解释原因。
    def _apply_usage_feedback(
        self,
        card: CapabilityCard,
        score: float,
        reasons: list[str],
    ) -> tuple[float, list[str]]:
        if self.usage_store is None:
            return score, reasons
        stats = self.usage_store.stats_for(card.id)
        bonus = stats.score_bonus
        if bonus > 0:
            return score + bonus, [*reasons, f"历史成功 +{stats.successes}"]
        if bonus < 0:
            return score + bonus, [*reasons, f"历史失败 +{stats.failures}"]
        return score, reasons

    # LLM: CapabilityRouter.render_candidates belongs to 能力路由; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 把候选能力渲染成给代理看的短说明。。
    def render_candidates(
        self,
        query: str,
        *,
        limit: int | None = None,
        kinds: set[str] | None = None,
    ) -> str:
        """把候选能力渲染成给代理看的短说明。"""

        hits = self.search(query, limit=limit, kinds=kinds)
        if not hits:
            return "# Candidate Capabilities\n当前没有明显匹配的 skill/tool card。"
        blocks: list[str] = []
        for hit in hits:
            reason_text = "；".join(hit.reasons) or "与当前能力缺口相关"
            blocks.append(f"{hit.card.render_compact()}\n  推荐理由：{reason_text}")
        return "# Candidate Capabilities\n" + "\n\n".join(blocks)


# LLM: from_skill_card belongs to 能力路由; keep caller-visible returns, errors, and side effects aligned with focused tests.
# 函数用途: 把 Skill Card 映射成统一能力卡。。
def from_skill_card(card: SkillCard) -> CapabilityCard:
    """把 Skill Card 映射成统一能力卡。"""

    return CapabilityCard(
        id=f"skill:{card.name}",
        kind="skill",
        name=card.name,
        description=card.description,
        capabilities=card.capabilities,
        when_to_use=[card.when_to_use] if card.when_to_use else [],
        keywords=[*card.tags, *card.capabilities, *card.tools_required],
        risk_level=card.risk_level,
        source=card.source,
        path=str(card.path),
        metadata={
            "scope": card.scope,
            "tools_required": card.tools_required,
        },
    )


# LLM: from_tool_spec belongs to 能力路由; keep caller-visible returns, errors, and side effects aligned with focused tests.
# 函数用途: 把现有 ToolSpec 映射成统一能力卡。。
def from_tool_spec(spec: ToolSpec) -> CapabilityCard:
    """把现有 ToolSpec 映射成统一能力卡。"""

    side_effects, risk_level = classify_tool_risk(spec)
    return CapabilityCard(
        id=f"tool:{spec.name}",
        kind="tool",
        name=spec.name,
        description=spec.description,
        capabilities=[spec.category, spec.name],
        when_to_use=spec.use_cases,
        not_when_to_use=spec.avoid_when,
        keywords=spec.keywords,
        risk_level=risk_level,
        side_effects=side_effects,
        source="builtin_tool_registry",
        metadata={
            "parameters": spec.parameters,
            "examples": spec.examples,
        },
    )


# LLM: default_capability_cards maps built-in higher-level abilities onto stable execution tools.
# 函数用途: 提供默认能力卡；例如 Playwright 前端/E2E 能力先路由到 controlled_exec，后续有专用工具时可只替换这里的卡片映射。
def default_capability_cards() -> list[CapabilityCard]:
    return [_playwright_capability_card()]


# LLM: _playwright_capability_card keeps browser/E2E routing centralized and easy to swap.
# 函数用途: 构造 Playwright 默认能力卡，把前端浏览器测试需求映射到受控执行工具。
def _playwright_capability_card() -> CapabilityCard:
    return CapabilityCard(
        id="builtin:playwright-browser-testing",
        kind="tool",
        name="controlled_exec",
        description=(
            "Playwright browser automation ability for frontend E2E, screenshots, "
            "click/form checks, and UI smoke tests inside authorized workspaces."
        ),
        capabilities=list(_PLAYWRIGHT_CAPABILITIES),
        when_to_use=list(_PLAYWRIGHT_WHEN_TO_USE),
        not_when_to_use=list(_PLAYWRIGHT_NOT_WHEN_TO_USE),
        keywords=list(_PLAYWRIGHT_KEYWORDS),
        risk_level="medium",
        side_effects=["local_process", "browser_automation", "filesystem_read"],
        source="builtin_capability_card",
        metadata={"package": "playwright", "execution_tool": "controlled_exec"},
    )


# LLM: classify_tool_risk belongs to 能力路由; keep caller-visible returns, errors, and side effects aligned with focused tests.
# 函数用途: 给现有工具补一层基础风险分类。 这不是最终安全策略，只是 Tool Card 的初始风险信号。 后续可以在 tool card 里继续扩展更细的权限和确认机制。。
def classify_tool_risk(spec: ToolSpec) -> tuple[list[str], str]:
    """给现有工具补一层基础风险分类。

    这不是最终安全策略，只是 Tool Card 的初始风险信号。
    后续可以在 tool card 里继续扩展更细的权限和确认机制。"""

    name = spec.name
    if name in {"write_file", "append_file", "replace_in_file"}:
        return ["filesystem_write"], "high"
    if name == "http_request":
        return ["network_request"], "medium"
    if name == "fetch_url":
        return ["network_read"], "medium"
    if spec.category == "filesystem":
        return ["filesystem_read"], "low"
    return [], "low"
