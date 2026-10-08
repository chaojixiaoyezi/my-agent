# LLM: 能力卡来自原注册及逐轮 Skill 快照；目录和动态候选仅指导显式读取，不修改检索范围、快照或授权。
# 模块用途: 提供公开能力名卡、按需读取软指引及有界任务建议，不展开包内资源、固定任务引用或自动执行方法。
from __future__ import annotations

"""统一能力路由模块。

这里把 skill 和 tool 都抽象成 Capability Card。
后续无论是父代理给子代理下发 skill，还是下发 tool，都可以走同一套路由协议。
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..common.value_parsing import dedupe_strings
from ..tooling.models import ToolModelSpec
from ..tooling.write_boundary import WRITE_TOOL_NAMES
from .config import CapabilityConfig
from .method_carry import using_method_cards
from .package_snapshot import CapabilityPackageSnapshot, package_read_parameters
from .skill_snapshot import PACKAGE_PIN_ERROR_MESSAGES, SkillSnapshot, SkillSnapshotEntry
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

# 技能元数据最多占用上下文窗口的比例（%），防止技能说明挤占对话。
_SKILL_METADATA_CONTEXT_WINDOW_PERCENT = 2
# 技能元数据默认字符预算，控制每轮注入的技能说明总量。
_DEFAULT_SKILL_METADATA_MAX_CHARS = 8_000
# 单条技能描述的最大字符数，超长截断。
_MAX_SKILL_DESCRIPTION_CHARS = 1_024
# 每 token 的近似字节数，用于估算上下文占用。
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

_CAPABILITY_PACKAGE_USAGE_INSTRUCTIONS = """### 能力包使用规则
- 采用：用户点名能力包，或任务明确匹配包摘要时，先读入口并按其方法处理；不能因已经会做就跳过。多个匹配只选覆盖任务的最小集合，用一句话说明所用包及原因；没有匹配项时照常处理。
- 入口：当前执行代理先调用 `skill_search(action=get, package_id=<原样包ID>)`，完整读取入口及本步骤所需的指令，再采取相关任务动作；不能只让子代理概括入口代替自己读取。
- 包内资料：相对引用以当前成员所在的包内目录为基准，用相同 `package_id` 和声明的 `resource_path` 读取；可用 `skill_search(action=search, package_id=<包ID>)` 定位声明资源。只读相关资料，不把私有路径当作全局 skill_id 或本地路径。
- 完整性：选中的指令或核验说明，包正文 `has_more=true` 时按 `continuation` 续读；归档窗口 `has_more_after=true` 时按 `next_read`，或保留原归档引用并用 `next_offset` 继续 `read_artifact`。正文预览、单页和归档窗口都不代表全文，两种偏移不能混用。
- 资源复用：现成脚本、核验器、模板或资产需要原样落盘时，使用 `write_file(path=<目标路径>, source_ref=<工具返回的完整原样引用>)`；不要从预览或分页手抄重建。落盘不执行代码，执行仍走已授权的原工具。核验失败应修正产物或说明真实限制，不能改写或削弱原核验器来迁就产物。
- 派工：需要子代理使用包时，在创建参数 `allowed_skills` 中明确传 `capability:<package_id>`；宿主从父级快照固定并继承版本引用，不自行编造 skill_snapshot_refs。孩子须在自身授权范围读取入口和所需资源，不能仅靠任务文字获得包权限。
- 边界：目录匹配和正文读取不增加工具、路径或网络权限。不可读时如实说明并继续可做部分；原任务失效包不能自动换用同名新版，也不能把缺失的核验说成通过。"""


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

    # LLM: Skill 与能力包只渲染各自公开引用；包内文件不会通过能力卡自动进入上下文。
    # 函数用途: 渲染可发现短卡和对应读取方式，不读取正文或增加权限。
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
        elif self.kind == "capability_package":
            lines.append(f"  正文：调用 skill_search，action=get，package_id={self.metadata['package_id']}")
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


# LLM: 原快照及静态注册仍是能力目录；名卡和包候选只用于本次上下文，候选不能重新加入宿主未选包或改变读取权限。
# 类用途: 为模型检索可用能力，渲染预算索引、选中短卡和包级动态建议，不读取私有资源。
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

    # LLM: 原 Skill 与包摘要各自从同一 scoped snapshot 派生，私有成员既不注册也不参加全局计数/搜索。
    # 函数用途: 汇集本轮公开可见的工具、Skill 与能力包卡片。
    def _current_cards(self) -> list[CapabilityCard]:
        snapshot = self._snapshot()
        static = list(self._cards.values())
        if snapshot is None:
            return static
        non_skills = [card for card in static if card.kind not in {"skill", "capability_package"}]
        skills = [from_skill_snapshot_entry(entry) for entry in snapshot.enabled_entries()]
        packages = [from_capability_package(package) for package in snapshot.packages]
        return [*non_skills, *skills, *packages]

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

    # LLM: 在用身份只与当前授权卡相交，按冻结的首次使用顺序优先显示；required 复用原必显，不扩权。
    #   公开 Skill 的原格式由 _render_public_skill_metadata 保持；联测目录字节、快照与提示投影。
    # 函数用途: 渲染公开能力短卡、按需读取说明及不可用包，展示不改变快照和授权。
    def render_skill_metadata_index(
        self, *, context_window_tokens: int | None = None,
        selected_skill_ids: tuple[str, ...] | None = None,
        required_skill_ids: tuple[str, ...] = (),
        in_use_method_ids: tuple[str, ...] = (),
    ) -> str:
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
        budget = _skill_metadata_budget(context_window_tokens)
        unavailable_text = _render_unavailable_package_references(self._snapshot())
        budget = _SkillMetadataBudget(max(0, budget.limit - budget.cost(unavailable_text)), budget.token_based)
        packages = sorted(self.cards(kinds={"capability_package"}), key=lambda card: card.name)
        skills = using_method_cards(skills, in_use_method_ids)
        packages = using_method_cards(packages, in_use_method_ids)
        required_skill_ids = (*required_skill_ids, *in_use_method_ids)
        package_budget = _SkillMetadataBudget(max(1, budget.limit // 2) if skills else budget.limit, budget.token_based)
        package_text, package_cost = _render_package_metadata(packages, package_budget, selected_skill_ids, required_skill_ids)
        package_text = "\n\n".join(text for text in (unavailable_text, package_text) if text)
        if not skills:
            return package_text
        budget = _SkillMetadataBudget(max(0, budget.limit - package_cost), budget.token_based)
        if selected_skill_ids is not None:
            skill_text = _render_selected_skill_metadata(skills, budget, selected_skill_ids, required_skill_ids)
            return "\n\n".join(text for text in (package_text, skill_text) if text)
        return "\n\n".join(text for text in (package_text, _render_public_skill_metadata(skills, budget)) if text)

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

        # 注意：路由器构造时 self.config 是 AgentConfig，上面没有 capability_candidate_limit；watch_service 之后才换成
        # CapabilityConfig 快照。生产调用都显式传 limit，新调用方也必须显式传，不能依赖 limit=None。
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

    # LLM: 仅复用 scoped search 的元数据评分；先按宿主选中集合过滤再限数，完整读取参数随卡保留，不预读、不 pin、不授予权限。
    #   推荐门槛（learnpack 第 6 步）：没有宿主一次选择时只推荐 has_strong_match 的包（声明的名称、能力或关键词整个出现在提问里），
    #   只撞上描述常用词的不推荐；宿主选中的包照旧（选择本身已判断过相关）。改门槛要同步 test_pack_recommendation_threshold。
    # 函数用途: 在现有元数据预算内展示本轮相关包摘要；宿主负责开关与工具权限，空结果保持原提示字节。
    def render_package_recommendations(
        self, query: str, *, limit: int | None = None,
        context_window_tokens: int | None = None,
        selected_skill_ids: tuple[str, ...] | None = None,
    ) -> str:
        hits = self.search(query, limit=0, kinds={"capability_package"})
        if selected_skill_ids is not None:
            cards = [hit.card for hit in hits if hit.card.id in selected_skill_ids]
        else:
            cards = [hit.card for hit in hits if has_strong_match(query, hit.card)]
        # 注意：路由器构造时 self.config 是 AgentConfig，上面没有 capability_candidate_limit；watch_service 之后才换成
        # CapabilityConfig 快照。生产调用都显式传 limit，新调用方也必须显式传，不能依赖 limit=None。
        effective_limit = self.config.capability_candidate_limit if limit is None else limit
        if effective_limit:
            cards = cards[:effective_limit]
        if not cards:
            return ""
        header = ("# 本轮能力包候选\n以下仅是本轮相关的方法建议，不增加授权；"
                  "匹配任务时先将 next_read 原样交给 skill_search 读取入口。")
        budget = _skill_metadata_budget(context_window_tokens)
        remaining = _SkillMetadataBudget(max(0, budget.limit - _line_cost(budget, header)), budget.token_based)
        rendered, _, _ = _render_skill_metadata_lines(cards, remaining, line_renderer=_package_recommendation_line)
        return header + "\n" + "\n".join(rendered) if rendered else ""


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


# LLM: 卡片只能含包级公开声明；不得附带 members、内部文件名、正文或宿主路径。
# 函数用途: 将能力包摘要加入统一软召回，同时保持独立于全局 Skill 的类别。
def from_capability_package(package: CapabilityPackageSnapshot) -> CapabilityCard:
    return CapabilityCard(
        id=package.stable_id, kind="capability_package", name=package.package_id,
        description=package.description, keywords=list(package.keywords),
        when_to_use=[package.summary], source=package.source,
        metadata={**package.to_ref(), "version": package.version},
    )


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


# LLM: Capability cards must classify every canonical filesystem mutation tool
# through WRITE_TOOL_NAMES; a newly exposed editor cannot fall back to read-only risk.
# 函数用途: 给工具卡补基础风险分类，确保所有文件写工具都按高风险写入能力展示。
def classify_tool_model_risk(spec: ToolModelSpec) -> tuple[list[str], str]:
    """给现有工具补一层基础风险分类。

    这不是最终安全策略，只是 Tool Card 的初始风险信号。
    后续可以在 tool card 里继续扩展更细的权限和确认机制。"""

    name = spec.name
    if name in WRITE_TOOL_NAMES:
        return ["filesystem_write"], "high"
    if name == "web_fetch":
        return ["network_read", "network_request"], "medium"
    if name == "web_search":
        return ["network_read"], "medium"
    if spec.category == "filesystem":
        return ["filesystem_read"], "low"
    return [], "low"


# LLM: 本轮能力包候选的推荐门槛（learnpack 第 6 步）：卡片声明的名称、能力或关键词要整个出现在提问里才算强命中（忽略大小写；
#   纯 ASCII 的词两头不能紧挨英文字母，免得 "r" 撞上 "report"）。只撞上描述、适用场景里的词，或提问切出来的片段撞上关键词的
#   一部分（如"工作目录"里的"工作"撞上"相关工作"）都不算。只给 render_package_recommendations 用；score_card 的分数和 search
#   排序不变，skill_search 主动搜照旧能按描述搜到。纯函数。
# 函数用途: 判断提问里有没有出现能力卡声明的名称、能力或关键词。
def has_strong_match(query: str, card: CapabilityCard) -> bool:
    lowered = query.lower()
    return any(_declared_term_in(term.strip().lower(), lowered) for term in (card.name, *card.capabilities, *card.keywords))


# LLM: 空词不算；纯 ASCII 的词要求两头不紧挨英文字母，其余（含中文）按子串。纯函数。
# 函数用途: 判断一个声明的词是否整个出现在（已转小写的）提问里。
def _declared_term_in(term: str, lowered_query: str) -> bool:
    if not term:
        return False
    if term.isascii():
        return re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", lowered_query) is not None
    return term in lowered_query


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


# LLM: 保持未选择、无在用项时的原提示字节；有在用 skill 时仍用普通目录格式（标题和使用规则不变，3a 集成复核 10-08 改回），
#   不切到 Selected Skills 选择模式；预算与省略说明只从原渲染结果取得，不读取线程或新增身份。联测 conversation_method_directory。
# 函数用途: 装配普通 Skill 目录、预算说明与使用规则，供公开能力索引复用。
def _render_public_skill_metadata(skills: list, budget: _SkillMetadataBudget) -> str:
    rendered, omitted, descriptions_shortened = _render_public_skill_lines(skills, budget)
    lines = ["# Available Skills", "下列是本轮可用 Skill 的 name + description + stable skill_id。普通问答没有匹配项时无需调用。", *rendered]
    if omitted:
        lines.append(f"- 2% Skill metadata 预算不足，另有 {omitted} 个 Skill 未显示；仍可用 skill_search(action=search) 按需求检索。")
    elif descriptions_shortened:
        lines.append("- 部分 description 已为适配 2% Skill metadata 预算而缩短；stable id 与 Skill 正文未改变。")
    lines.append(_SKILL_USAGE_INSTRUCTIONS)
    return "\n".join(lines)


# LLM: 在用卡已由 using_method_cards 排在最前；先按"至少放得下名字和编号"的下限渲染在用卡，再用剩余预算渲染其余卡，
#   没有在用卡时等同原来一次渲染全部（字节不变）。省略数只计其余卡，在用卡不会被省略。
# 函数用途: 普通目录下保证本会话在用的 skill 一定显示，其余照原裁剪规则。
def _render_public_skill_lines(skills: list, budget: _SkillMetadataBudget) -> tuple[list[str], int, bool]:
    in_use = [card for card in skills if card.metadata.get("conversation_in_use")]
    if not in_use:
        return _render_skill_metadata_lines(skills, budget)
    others = [card for card in skills if not card.metadata.get("conversation_in_use")]
    minimum = sum(_line_cost(budget, _skill_line(card, "")) for card in in_use)
    first, _, first_short = _render_skill_metadata_lines(in_use, _SkillMetadataBudget(max(budget.limit, minimum), budget.token_based))
    used = sum(_line_cost(budget, line) for line in first)
    rest, omitted, rest_short = _render_skill_metadata_lines(others, _SkillMetadataBudget(max(0, budget.limit - used), budget.token_based))
    return [*first, *rest], omitted, first_short or rest_short


# LLM: ID 仅匹配原 stable_id，不接受名称别名；required 也必须处于授权卡内，预算不足只省略可选项。
# 函数用途: 在当前名卡中渲染宿主选择和必要引用，准确报告省略数，完整检索目录保持原样。
def _render_selected_skill_metadata(skills, budget, selected_skill_ids, required_skill_ids) -> str:
    selected = set(selected_skill_ids)
    required = set(required_skill_ids)
    mandatory = [card for card in skills if card.metadata.get("stable_id") in required]
    optional = [card for card in skills if card.metadata.get("stable_id") in selected - required]
    minimum = sum(_line_cost(budget, _skill_line(card, "")) for card in mandatory)
    required_lines, _, _ = _render_skill_metadata_lines(
        mandatory, _SkillMetadataBudget(max(budget.limit, minimum), budget.token_based),
    )
    remaining = max(0, budget.limit - sum(_line_cost(budget, line) for line in required_lines))
    selected_lines, _, _ = _render_skill_metadata_lines(optional, _SkillMetadataBudget(remaining, budget.token_based))
    shown = [*required_lines, *selected_lines]
    if not shown:
        return ""
    omitted = len(skills) - len(shown)
    lines = ["# Selected Skills", "以下仅是本工作片展示的 Skill 名卡；使用前仍须 skill_search(action=get) 读取正文。", *shown]
    if omitted:
        lines.append(f"- 另有 {omitted} 个授权 Skill 未展示；仍可用 skill_search(action=search) 按需求检索。")
    return "\n".join(lines)


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
    return _SkillMetadataBudget(_DEFAULT_SKILL_METADATA_MAX_CHARS, False)


# LLM: 包卡仅展示公开 package_id；在用标注来自授权卡的结构化元数据，空标注保持原字节。
# 函数用途: 为公开方法或能力包生成一行完整稳定引用。
def _skill_line(card: CapabilityCard, description: str) -> str:
    stable_id = str(card.metadata.get("stable_id") or "").strip()
    locator = (f"package_id: {card.metadata['package_id']}" if card.kind == "capability_package"
               else f"skill_id: {stable_id}")
    name = card.name + ("（本会话在用）" if card.metadata.get("conversation_in_use") else "")
    if description:
        return f"- {name}: {description} ({locator})"
    return f"- {name}: ({locator})"


# LLM: next_read 只从原包引用生成且始终完整；摘要可裁剪，读取代次不能裁剪，私有成员不进入此投影。
# 函数用途: 生成可直接显式调用 skill_search 的包摘要卡，不用 skill_id 冒充包选择器。
def _package_recommendation_line(card: CapabilityCard, description: str) -> str:
    return "- " + json.dumps({"package_id": card.metadata["package_id"], "description": description,
                              "next_read": package_read_parameters(card.metadata)}, ensure_ascii=False)


# LLM: 包摘要复用原必显与裁剪算法；只有授权在用包才补续接软指导，不据任务文字自动加载或赋权。
# 函数用途: 在共享预算内生成独立包名卡并说明采用、完整读取和原资源复用；私有成员仍不进入全局索引。
def _render_package_metadata(packages, budget, selected, required) -> tuple[str, int]:
    if not packages:
        return "", 0
    required_ids = set(required)
    mandatory = [card for card in packages if card.metadata["stable_id"] in required_ids]
    optional = [card for card in packages if card not in mandatory
                and (selected is None or card.metadata["stable_id"] in selected)]
    minimum = sum(_line_cost(budget, _skill_line(card, "")) for card in mandatory)
    first, _, _ = _render_skill_metadata_lines(mandatory, _SkillMetadataBudget(max(budget.limit, minimum), budget.token_based))
    used = sum(_line_cost(budget, line) for line in first)
    remaining, _, _ = _render_skill_metadata_lines(optional, _SkillMetadataBudget(max(0, budget.limit - used), budget.token_based))
    shown = [*first, *remaining]
    omitted = len(packages) - len(shown)
    lines = ["# 能力包", "下列只展示包摘要；用 skill_search(action=get, package_id=包ID) 读取入口，再在该包内选择资源。", *shown]
    if omitted:
        lines.append(f"- 另有 {omitted} 个能力包未展示；可用 skill_search(action=search) 检索包摘要。")
    lines.append(_CAPABILITY_PACKAGE_USAGE_INSTRUCTIONS)
    if any(card.metadata.get("conversation_in_use") for card in packages):
        lines.append("接着做同类的事，继续照在用包的方法和你走到的那一步；不相关的事照常处理。")
    return "\n".join(lines), sum(_line_cost(budget, line) for line in shown)


# LLM: 仅渲染主任务已规范的两种包 pin 诊断；不读取错误原文、宿主路径或正文，最多八条并限制总字符。
# 函数用途: 告知模型哪些原任务能力不能继续读取，保留正常工具和任务收口，不暗示自动换版。
def _render_unavailable_package_references(snapshot: SkillSnapshot | None) -> str:
    if snapshot is None:
        return ""
    errors = sorted({(error.path, error.code) for error in snapshot.errors
                     if error.source == "capability_package" and error.code in PACKAGE_PIN_ERROR_MESSAGES
                     and error.path.startswith("capability:")})
    if not errors:
        return ""
    lines = ["# 当前任务不可用的能力包", "原任务版本引用保留；下列包的正文和资源不可读取，也不能自动换用同名新版本。其它可用能力及正常回复不受影响。"]
    shown = 0
    for reference, code in errors[:8]:
        line = "- " + json.dumps({"reference": reference, "code": code,
                                 "message": PACKAGE_PIN_ERROR_MESSAGES[code]}, ensure_ascii=False)
        if sum(len(item) + 1 for item in lines) + len(line) > 1800:
            break
        lines.append(line)
        shown += 1
    if shown < len(errors):
        lines.append(f"- 另有 {len(errors) - shown} 个原任务包引用不可用；这些包同样不能读取。")
    return "\n".join(lines)


def _bounded_skill_description(card: CapabilityCard) -> str:
    description = str(card.description or "")
    if len(description) <= _MAX_SKILL_DESCRIPTION_CHARS:
        return description
    return description[: _MAX_SKILL_DESCRIPTION_CHARS - 3] + "..."


def _line_cost(budget: _SkillMetadataBudget, line: str) -> int:
    return budget.cost(line + "\n")


# LLM: 预算算法只裁剪说明；默认公开名卡格式保持，包候选复用完整 locator/next_read 渲染，放不下时省略整卡。
# 函数用途: 在原字符或 token 预算内分配描述空间，不产生半个身份或截断的读取参数。
def _render_skill_metadata_lines(
    skills: list[CapabilityCard],
    budget: _SkillMetadataBudget,
    *, line_renderer: Callable[[CapabilityCard, str], str] = _skill_line,
) -> tuple[list[str], int, bool]:
    descriptions = [_bounded_skill_description(card) for card in skills]
    full_lines = [line_renderer(card, description) for card, description in zip(skills, descriptions)]
    if sum(_line_cost(budget, line) for line in full_lines) <= budget.limit:
        return full_lines, 0, False

    minimum_lines = [line_renderer(card, "") for card in skills]
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
            next_line = line_renderer(card, description[:next_chars])
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
        line_renderer(card, description[:chars])
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
