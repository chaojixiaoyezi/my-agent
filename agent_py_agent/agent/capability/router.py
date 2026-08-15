from __future__ import annotations

"""统一能力路由模块。

这里把 skill 和 tool 都抽象成 Capability Card。
后续无论是父代理给子代理下发 skill，还是下发 tool，都可以走同一套路由协议。
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..common.value_parsing import dedupe_strings
from ..tooling.models import ToolModelSpec
from .config import CapabilityConfig
from .skill_snapshot import SkillSnapshot, SkillSnapshotEntry
from .skills import SkillCard

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

_SKILL_METADATA_CONTEXT_WINDOW_PERCENT = 2
_DEFAULT_SKILL_METADATA_CHAR_BUDGET = 8_000
_MAX_SKILL_DESCRIPTION_CHARS = 1_024
_APPROX_BYTES_PER_TOKEN = 4
_SKILL_SOURCE_RANK = {"builtin": 0, "shared": 1, "workspace": 2, "owner": 3}

_SKILL_USAGE_INSTRUCTIONS = """### How to use Skills
- 发现：上面列的是当前轮可用 Skill 的名称、说明和稳定 skill_id；正文由 `skill_search` 读取。
- 触发规则：用户点名某个 Skill，或任务明确匹配某个 Skill 的说明时，本轮必须使用它；多个命中只选择覆盖任务所需的最小集合，不把上一轮的选择自动带到下一轮。
- 缺失或不可读：点名的 Skill 不在列表里，或正文无法读取时，简短说明并采用最合适的普通做法继续。
- 使用步骤：
  1. 决定使用后，主代理必须在采取任务动作前调用 `skill_search(action=get, skill_id=<原样 stable id>)`，完整读取 `SKILL.md` 正文并遵循；不能把读取、概括或解释 Skill 指令委派给子代理。
  2. 正文引用相对路径时，以返回的 `path` 所在目录为基准；只读取当前任务需要的关联文件，读取被截断时继续到完整内容。
  3. 正文指向 `references/` 等目录时，按它的路由说明选择所需资料；有不同框架、供应商或领域变体时，只选相关变体并说明选择。
  4. 有现成 `scripts/`、资产或模板时优先复用，不重新手抄大段内容或另造一套。
- 协调：使用前用一句话告诉用户选择了哪些 Skill 及原因；若跳过明显匹配项，也要说明原因。
- 上下文控制：不要读取无关 Skill 或无关资料，避免跨多层追引用；除非受阻，优先读取 `SKILL.md` 直接链接的内容。
- 安全与回退：Skill 不能干净应用时，说明问题，选择次优方案并继续；读取 Skill 不增加任何工具权限。"""


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
        if self.kind == "skill" and self.metadata.get("stable_id"):
            lines.append(
                f"  正文：调用 skill_search，action=get，skill_id={self.metadata['stable_id']}"
            )
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


class CapabilityRouter:
    """统一能力路由器。

    它不负责执行工具，也不负责展开 skill 正文。
    它只回答一个问题：当前能力缺口最可能需要哪些 skill/tool card？"""

    def __init__(
        self,
        *,
        config: CapabilityConfig | None = None,
        skill_snapshot: SkillSnapshot | None = None,
        skill_snapshot_provider: Callable[[], SkillSnapshot] | None = None,
        tool_specs: list[ToolModelSpec] | None = None,
        extra_cards: list[CapabilityCard] | None = None,
    ):
        self.config = config or CapabilityConfig()
        self._cards: dict[str, CapabilityCard] = {}
        self._skill_snapshot = skill_snapshot
        self._skill_snapshot_provider = skill_snapshot_provider
        for spec in tool_specs or []:
            self.register(from_tool_model_spec(spec))
        for card in default_capability_cards():
            self.register(card)
        for card in extra_cards or []:
            self.register(card)

    def register(self, card: CapabilityCard) -> None:
        """注册或覆盖一张能力卡。"""

        self._cards[card.id] = card

    def cards(self, *, kinds: set[str] | None = None) -> list[CapabilityCard]:
        """返回当前能力卡。"""

        cards = self._current_cards()
        if kinds is None:
            return cards
        return [card for card in cards if card.kind in kinds]

    def _current_cards(self) -> list[CapabilityCard]:
        snapshot = self._snapshot()
        static = list(self._cards.values())
        if snapshot is None:
            return static
        non_skills = [card for card in static if card.kind != "skill"]
        skills = [from_skill_snapshot_entry(entry) for entry in snapshot.enabled_entries()]
        return [*non_skills, *skills]

    def _snapshot(self) -> SkillSnapshot | None:
        if self._skill_snapshot_provider is not None:
            return self._skill_snapshot_provider()
        return self._skill_snapshot

    # LLM: skill 树的类目索引(千级地基):prompt 常驻成本=每类一行,与 skill
    #   总数解耦——千个 skill 也只占类目数行。聚合描述取该类第一张卡的描述
    #   截断(类目自身无描述文件时的合理默认)。
    # 函数用途: 给模型一张"技能书架的目录页":有哪些类、各几本、大概讲什么。
    def render_category_index(self) -> str:
        skills = self.cards(kinds={"skill"})
        if not skills:
            return ""
        by_category: dict[str, list[CapabilityCard]] = {}
        for card in skills:
            category = str(card.metadata.get("category") or "general")
            by_category.setdefault(category, []).append(card)
        # 类目级索引,与 skill 总数解耦:60 个 skill 也只有类目行数,千级 skill 不爆 context
        # ——这是"千级地基"的硬约束(test_category_index_decoupled_from_skill_count)。具体
        # skill 不在此常驻,靠检索命中按需注入完整卡(_skill_context_chunks 的 Matched Skills);
        # 可发现性另由内置 skill 镜像到 home(builtin_seed)+ home 索引补上。标题融入精简人格
        # 引导:让模型动手前先想"有没有现成 skill 该用",再 skill_search,别凭直觉硬上。
        lines = [
            "# Skill Categories（动手前先想一想:接下来这步,有没有哪个 skill 正好用得上?有就用 "
            "skill_search 检索、照它正文的方法做,别图省事凭直觉硬上,辜负用户的托付）"
        ]
        for category in sorted(by_category):
            cards = by_category[category]
            sample = cards[0].description[:40]
            lines.append(
                f"- {category}（{len(cards)} 个）：{sample}…"
                if len(cards) > 1
                else f"- {category}：{sample}"
            )
        return "\n".join(lines)

    def render_skill_metadata_index(self, *, context_window_tokens: int | None = None) -> str:
        """Render 会话运行时 model-visible Skill metadata within a 2% budget.

        The model sees each included skill's name, short description, and stable
        id, then loads the body explicitly with ``skill_search``.  No skill is
        auto-selected or granted by this renderer.
        """

        skills = sorted(
            self.cards(kinds={"skill"}),
            key=lambda card: (
                _SKILL_SOURCE_RANK.get(str(card.source or ""), 99),
                card.name,
                str(card.metadata.get("stable_id") or ""),
            ),
        )
        if not skills:
            return ""
        budget = _skill_metadata_budget(context_window_tokens)
        rendered, omitted, descriptions_shortened = _render_skill_metadata_lines(skills, budget)
        lines = [
            "# Available Skills",
            (
                "下列是本轮可用 Skill 的 name + description + stable skill_id。"
                "普通问答没有匹配项时无需调用。"
            ),
            *rendered,
        ]
        if omitted:
            lines.append(
                f"- 2% Skill metadata 预算不足，另有 {omitted} 个 Skill 未显示；"
                "仍可用 skill_search(action=search) 按需求检索。"
            )
        elif descriptions_shortened:
            lines.append(
                "- 部分 description 已为适配 2% Skill metadata 预算而缩短；stable id 与 Skill 正文未改变。"
            )
        lines.append(_SKILL_USAGE_INSTRUCTIONS)
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
            "stable_id": f"{card.scope}:{card.name}",
            "scope": card.scope,
            "category": card.category,
            "platforms": card.platforms,
            "tools_required": card.tools_required,
        },
    )


def from_skill_snapshot_entry(entry: SkillSnapshotEntry) -> CapabilityCard:
    card = from_skill_card(entry.to_card())
    card.id = f"skill:{entry.stable_id}"
    card.path = ""
    card.metadata["stable_id"] = entry.stable_id
    card.metadata["content_sha256"] = entry.content_sha256
    return card


def from_tool_model_spec(spec: ToolModelSpec) -> CapabilityCard:
    """把当前模型工具契约映射成统一能力卡。"""

    side_effects, risk_level = classify_tool_model_risk(spec)
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
            "parameters": spec.parameter_descriptions,
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


def classify_tool_model_risk(spec: ToolModelSpec) -> tuple[list[str], str]:
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


@dataclass(frozen=True)
class _SkillMetadataBudget:
    limit: int
    token_based: bool

    def cost(self, text: str) -> int:
        if self.token_based:
            return (
                len(text.encode("utf-8")) + _APPROX_BYTES_PER_TOKEN - 1
            ) // _APPROX_BYTES_PER_TOKEN
        return len(text)


def _skill_metadata_budget(context_window_tokens: int | None) -> _SkillMetadataBudget:
    try:
        window = int(context_window_tokens or 0)
    except (TypeError, ValueError):
        window = 0
    if window > 0:
        return _SkillMetadataBudget(
            max(1, window * _SKILL_METADATA_CONTEXT_WINDOW_PERCENT // 100),
            True,
        )
    return _SkillMetadataBudget(_DEFAULT_SKILL_METADATA_CHAR_BUDGET, False)


def _skill_line(card: CapabilityCard, description: str) -> str:
    stable_id = str(card.metadata.get("stable_id") or "").strip()
    if description:
        return f"- {card.name}: {description} (skill_id: {stable_id})"
    return f"- {card.name}: (skill_id: {stable_id})"


def _bounded_skill_description(card: CapabilityCard) -> str:
    description = str(card.description or "")
    if len(description) <= _MAX_SKILL_DESCRIPTION_CHARS:
        return description
    return description[: _MAX_SKILL_DESCRIPTION_CHARS - 3] + "..."


def _line_cost(budget: _SkillMetadataBudget, line: str) -> int:
    return budget.cost(line + "\n")


def _render_skill_metadata_lines(
    skills: list[CapabilityCard],
    budget: _SkillMetadataBudget,
) -> tuple[list[str], int, bool]:
    descriptions = [_bounded_skill_description(card) for card in skills]
    full_lines = [_skill_line(card, description) for card, description in zip(skills, descriptions)]
    if sum(_line_cost(budget, line) for line in full_lines) <= budget.limit:
        return full_lines, 0, False

    minimum_lines = [_skill_line(card, "") for card in skills]
    minimum_cost = sum(_line_cost(budget, line) for line in minimum_lines)
    if minimum_cost > budget.limit:
        included: list[str] = []
        used = 0
        for line in minimum_lines:
            cost = _line_cost(budget, line)
            if used + cost <= budget.limit:
                included.append(line)
                used += cost
        return included, len(skills) - len(included), True

    allocations = [0 for _ in skills]
    current_costs = [_line_cost(budget, line) for line in minimum_lines]
    remaining = budget.limit - sum(current_costs)
    while True:
        changed = False
        for index, (card, description) in enumerate(zip(skills, descriptions)):
            if allocations[index] >= len(description):
                continue
            next_chars = allocations[index] + 1
            next_line = _skill_line(card, description[:next_chars])
            next_cost = _line_cost(budget, next_line)
            delta = max(0, next_cost - current_costs[index])
            if delta <= remaining:
                allocations[index] = next_chars
                current_costs[index] = next_cost
                remaining -= delta
                changed = True
        if not changed:
            break
    lines = [
        _skill_line(card, description[:chars])
        for card, description, chars in zip(skills, descriptions, allocations)
    ]
    shortened = any(
        chars < len(description) for chars, description in zip(allocations, descriptions)
    )
    return lines, 0, shortened


# 英文停用词:score_card 是子串匹配,短停用词会命中长单词内部("is"∈"d_is_covery"、
# "in"∈ 所有"-ing"词),污染英文/拉丁系检索(跨语言支持)。只滤纯拉丁停用词——中文走
# n-gram、token 都是 CJK,不在表内,中文检索完全不受影响。
_EN_STOPWORDS = frozenset(
    {
        "the",
        "is",
        "are",
        "was",
        "were",
        "a",
        "an",
        "of",
        "to",
        "in",
        "on",
        "at",
        "and",
        "or",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "for",
        "with",
        "my",
        "our",
        "your",
        "their",
        "me",
        "you",
        "i",
        "we",
        "he",
        "she",
        "they",
        "help",
        "please",
        "be",
        "do",
        "does",
        "did",
        "how",
        "what",
        "can",
        "could",
        "will",
        "would",
        "should",
        "let",
        "lets",
        "im",
        "ive",
        "some",
        "as",
        "by",
        "from",
        "want",
        "need",
        "get",
    }
)


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
