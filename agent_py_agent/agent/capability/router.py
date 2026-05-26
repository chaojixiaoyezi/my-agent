# LLM: Capability module; keep skill/tool routing contracts stable for planner and dispatch callers.
# 模块用途: 描述和路由 agent 能力、技能、工具和执行条件。

from __future__ import annotations

"""统一能力路由模块。

这里把 skill 和 tool 都抽象成 Capability Card。
后续无论是父代理给子代理下发 skill，还是下发 tool，都可以走同一套路由协议。
"""

import re
from dataclasses import dataclass, field
from typing import Any

from ..tools import ToolSpec
from .config import CapabilityConfig
from .skills import SkillCard, SkillRegistry

_PLAYWRIGHT_CAPABILITIES = [
    "playwright",
    "browser_automation",
    "frontend_e2e",
    "ui_testing",
    "screenshot",
]
_PLAYWRIGHT_WHEN_TO_USE = [
    "需要真实浏览器打开页面、点击按钮、截图或验证前端流程时使用",
    "需要真实浏览器验证页面渲染、表单、导航、状态变化或端到端流程并保留证据时使用",
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
    "页面",
    "表单",
    "交互",
    "渲染",
    "导航",
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
    ):
        self.config = config or CapabilityConfig()
        self._cards: dict[str, CapabilityCard] = {}
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
            return cards
        return [card for card in cards if card.kind in kinds]

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
            if score > 0:
                hits.append(CapabilitySearchHit(card=card, score=score, reasons=reasons[:4]))
        hits.sort(key=lambda item: (-item.score, item.card.kind, item.card.name))
        if effective_limit == 0:
            return hits
        return hits[:effective_limit]

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
    if name in {"write_file", "apply_patch"}:
        return ["filesystem_write"], "high"
    if name == "http_request":
        return ["network_request"], "medium"
    if name in {"fetch_url", "web_fetch", "web_extract", "web_search"}:
        return ["network_read"], "medium"
    if spec.category == "filesystem":
        return ["filesystem_read"], "low"
    return [], "low"


# LLM: score_card belongs to 能力路由; keep caller-visible returns, errors, and side effects aligned with focused tests.
# 函数用途: 用可解释的关键词规则给能力卡打分。。
def score_card(query: str, card: CapabilityCard) -> tuple[float, list[str]]:
    """用可解释的关键词规则给能力卡打分。"""

    tokens = tokenize(query)
    if not tokens:
        return 0.0, []
    haystacks = {
        "name": card.name.lower(),
        "kind": card.kind.lower(),
        "description": card.description.lower(),
        "capabilities": " ".join(card.capabilities).lower(),
        "keywords": " ".join(card.keywords).lower(),
        "when_to_use": " ".join(card.when_to_use).lower(),
        "not_when_to_use": " ".join(card.not_when_to_use).lower(),
    }
    score = 0.0
    reasons: list[str] = []
    for token in tokens:
        token_score = 0.0
        if token in haystacks["name"]:
            token_score += 6.0
            reasons.append(f"命中名称'{token}'")
        if token in haystacks["capabilities"]:
            token_score += 5.0
            reasons.append(f"命中能力'{token}'")
        if token in haystacks["keywords"]:
            token_score += 4.0
            reasons.append(f"命中关键词'{token}'")
        if token in haystacks["kind"]:
            token_score += 2.0
            reasons.append(f"命中类型'{token}'")
        if token in haystacks["description"] or token in haystacks["when_to_use"]:
            token_score += 1.5
            reasons.append(f"命中描述'{token}'")
        score += token_score
    return score, _dedupe(reasons)


# LLM: tokenize belongs to 能力路由; keep caller-visible returns, errors, and side effects aligned with focused tests.
# 函数用途: 把查询切成适合粗检索的 token。。
def tokenize(text: str) -> list[str]:
    """把查询切成适合粗检索的 token。"""

    lowered = text.lower()
    tokens = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", lowered)
    expanded: list[str] = []
    for token in tokens:
        expanded.append(token)
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            expanded.extend(_chinese_ngrams(token))
    return _dedupe(expanded)


# LLM: _chinese_ngrams belongs to 能力路由; keep caller-visible returns, errors, and side effects aligned with focused tests.
# 函数用途: 提取中文字符的 n-gram（2-4 gram）。。
def _chinese_ngrams(token: str) -> list[str]:
    """提取中文字符的 n-gram（2-4 gram）。"""

    ngrams: list[str] = []
    for size in (2, 3, 4):
        for idx in range(0, max(len(token) - size + 1, 0)):
            ngrams.append(token[idx : idx + size])
    return ngrams


# LLM: _dedupe belongs to 能力路由; keep caller-visible returns, errors, and side effects aligned with focused tests.
# 函数用途: 保持顺序去重。。
def _dedupe(items: list[str]) -> list[str]:
    """保持顺序去重。"""

    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result
