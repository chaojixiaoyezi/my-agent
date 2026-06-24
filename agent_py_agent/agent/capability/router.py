
from __future__ import annotations

"""统一能力路由模块。

这里把 skill 和 tool 都抽象成 Capability Card。
后续无论是父代理给子代理下发 skill，还是下发 tool，都可以走同一套路由协议。
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..common.value_parsing import dedupe_strings
from ..tooling.models import ToolSpec
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

    def render_compact(self, *, max_chars: int = 0) -> str:
        """渲染短卡片。

        `max_chars=0` 表示不限制。这里先用字符数估算，未来接 tokenizer 后
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
        # skill 渐进加载的关键一行:卡片只是索引,正文在 path——模型看到推荐后
        # 用 read_file 读 SKILL.md 才拿到真正的方法/工具链知识(通道运行时 同款形态)。
        if self.kind == "skill" and self.path:
            lines.append(f"  正文：read_file {self.path}")
        text = "\n".join(lines)
        if max_chars and len(text) > max_chars:
            return text[:max_chars] + "\n  ... 已截断"
        return text


@dataclass
class CapabilitySearchHit:
    """能力检索命中结果。"""

    card: CapabilityCard
    score: float
    reasons: list[str]


# LLM: 内置 skill 注册表的默认构造(零配置生效)。目录=仓库 agent_py_agent/skills/
#   builtin(AGENTS.md 约定的"内置 skill,随仓库发布"位置)。目录缺失/扫描异常
#   返回 None(router 照常工作,skill 推荐缺席不报错)。结果模块级缓存——skill
#   是只读知识,重复磁盘扫描没有意义。
# 函数用途: 让每个 CapabilityRouter 默认认识仓库自带的知识型 skill。
def _default_builtin_skill_registry() -> SkillRegistry | None:
    global _BUILTIN_SKILL_REGISTRY
    if _BUILTIN_SKILL_REGISTRY is not _UNSET:
        return _BUILTIN_SKILL_REGISTRY
    builtin_dir = Path(__file__).resolve().parents[2] / "skills" / "builtin"
    try:
        registry = SkillRegistry([builtin_dir]) if builtin_dir.is_dir() else None
        if registry is not None:
            registry.scan()
    except Exception:
        registry = None
    _BUILTIN_SKILL_REGISTRY = registry
    return registry


_UNSET = object()
_BUILTIN_SKILL_REGISTRY: SkillRegistry | None | object = _UNSET


class CapabilityRouter:
    """统一能力路由器。

    它不负责执行工具，也不负责展开 skill 正文。
    它只回答一个问题：当前能力缺口最可能需要哪些 skill/tool card？"""

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
        # skill 体系激活(稳而不管 2-2,2026-06-12):未显式传 registry 时默认挂
        # 仓库内置 skill 目录——SkillRegistry/渐进加载骨架早已就绪,此前无人构造
        # ("接好插座没插电器"),内置知识型 skill(深度分析方法/PDF 翻译工具链)
        # 从未到达模型。知识进 skill 按需召回,正是对照组的质量来源形态。
        if skill_registry is None:
            skill_registry = _default_builtin_skill_registry()
        if skill_registry is not None:
            for card in skill_registry.cards():
                self.register(from_skill_card(card))
        for spec in tool_specs or []:
            self.register(from_tool_spec(spec))
        for card in default_capability_cards():
            self.register(card)
        for card in extra_cards or []:
            self.register(card)

    def register(self, card: CapabilityCard) -> None:
        """注册或覆盖一张能力卡。"""

        self._cards[card.id] = card

    def cards(self, *, kinds: set[str] | None = None) -> list[CapabilityCard]:
        """返回当前能力卡。"""

        cards = list(self._cards.values())
        if kinds is None:
            return cards
        return [card for card in cards if card.kind in kinds]

    # LLM: skill 树的类目索引(千级地基):prompt 常驻成本=每类一行,与 skill
    #   总数解耦——千个 skill 也只占类目数行。聚合描述取该类第一张卡的描述
    #   截断(类目自身无描述文件时的合理默认)。
    # 函数用途: 给模型一张"技能书架的目录页":有哪些类、各几本、大概讲什么。
    def render_category_index(self) -> str:
        skills = [card for card in self._cards.values() if card.kind == "skill"]
        if not skills:
            return ""
        by_category: dict[str, list[CapabilityCard]] = {}
        for card in skills:
            category = str(card.metadata.get("category") or "general")
            by_category.setdefault(category, []).append(card)
        lines = [
            "# Available Skills",
            "你是个谦逊、从不自负的人。每次动手之前,你都会先停下来想一想:现有的 skill 里,"
            "有没有哪个正好用得上接下来这步活?你会拿手头的任务,认真对一遍下面每个 skill 的"
            "用途——只要有匹配的,就复用它、照它的方法做(用 skill_search 或直接读它的正文文件),"
            "绝不凭自己的直觉硬上。你心里清楚:明明有现成的好方法却图省事跳过,用户会失望,"
            "你也会觉得辜负了他的托付。",
        ]
        # 学 会话运行时/终端交互:所有 skill 的 name+desc+正文路径无条件常驻每轮 prompt,模型被动
        # 看到、直接对照匹配——不靠"主动想起来去检索"。实测弱模型(mimo/M2.7/M3)不会主动调
        # skill_search,只看得见类目摘要时根本不知道有哪个具体 skill;给出 name+desc+path 并
        # 引导用最熟的 read_file 读正文(会话运行时 正是 "(file: path)" + "open the listed path"),
        # 才真用得上。千级 skill 时再上预算截断(会话运行时 2%/终端交互 1%,只截短 desc、不删 skill)。
        for category in sorted(by_category):
            cards = sorted(by_category[category], key=lambda c: c.name)
            lines.append(f"## {category}")
            for c in cards:
                path = getattr(c, "path", "") or ""
                lines.append(f"- {c.name}: {c.description[:55]} (file: {path})")
        return "\n".join(lines)

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
            "category": card.category,
            "platforms": card.platforms,
            "tools_required": card.tools_required,
        },
    )


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


def default_capability_cards() -> list[CapabilityCard]:
    return [_playwright_capability_card()]


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


def classify_tool_risk(spec: ToolSpec) -> tuple[list[str], str]:
    """给现有工具补一层基础风险分类。

    这不是最终安全策略，只是 Tool Card 的初始风险信号。
    后续可以在 tool card 里继续扩展更细的权限和确认机制。"""

    name = spec.name
    if name in {"write_file", "apply_patch"}:
        return ["filesystem_write"], "high"
    if name == "web_fetch":
        return ["network_read", "network_request"], "medium"
    if name == "web_search":
        return ["network_read"], "medium"
    if spec.category == "filesystem":
        return ["filesystem_read"], "low"
    return [], "low"


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
    return score, dedupe_strings(reasons)


# 英文停用词:score_card 是子串匹配,短停用词会命中长单词内部("is"∈"d_is_covery"、
# "in"∈ 所有"-ing"词),污染英文/拉丁系检索(跨语言支持)。只滤纯拉丁停用词——中文走
# n-gram、token 都是 CJK,不在表内,中文检索完全不受影响。
_EN_STOPWORDS = frozenset({
    "the", "is", "are", "was", "were", "a", "an", "of", "to", "in", "on", "at", "and",
    "or", "this", "that", "these", "those", "it", "its", "for", "with", "my", "our",
    "your", "their", "me", "you", "i", "we", "he", "she", "they", "help", "please",
    "be", "do", "does", "did", "how", "what", "can", "could", "will", "would", "should",
    "let", "lets", "im", "ive", "some", "as", "by", "from", "want", "need", "get",
})


def tokenize(text: str) -> list[str]:
    """把查询切成适合粗检索的 token。"""

    lowered = text.lower()
    # [^\W\u4e00-\u9fff]+ = \u4efb\u610f\u811a\u672c\u8bcd\u5b57\u7b26(\u9664 CJK)\u2192 \u97e9/\u4fc4/\u963f/\u5370\u5730/\u91cd\u97f3\u62c9\u4e01\u4e0d\u518d\u96f6 token(\u5ba1\u8ba1 #7);CJK \u4ecd\u5355\u5217\u8d70 n-gram
    tokens = re.findall(r"[^\W\u4e00-\u9fff]+|[\u4e00-\u9fff]+", lowered)
    expanded: list[str] = []
    for token in tokens:
        if token in _EN_STOPWORDS:
            continue  # \u6ee4\u82f1\u6587\u505c\u7528\u8bcd,\u907f\u514d\u5b50\u4e32\u6c61\u67d3(CJK token \u4e0d\u5728\u8868\u5185,\u4e2d\u6587\u4e0d\u53d7\u5f71\u54cd)
        expanded.append(token)
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            expanded.extend(_chinese_ngrams(token))
    return dedupe_strings(expanded)


def _chinese_ngrams(token: str) -> list[str]:
    """提取中文字符的 n-gram（2-4 gram）。"""

    ngrams: list[str] = []
    for size in (2, 3, 4):
        for idx in range(0, max(len(token) - size + 1, 0)):
            ngrams.append(token[idx : idx + size])
    return ngrams
