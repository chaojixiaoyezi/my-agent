from __future__ import annotations

# LLM: Skill learning turns structured task feedback into reviewable draft SKILL.md files.
# 模块用途: 将任务经验转成草稿 skill，保持“先草稿、后晋级”的自学习闭环。
from dataclasses import dataclass, field

from ..capability.skills import SkillDraftRequest, SkillLifecycleResult, SkillLifecycleStore


# LLM: LearnedSkillDraftRequest is the Request bundle for model-proposed skill learning.
# 类用途: 保存模型从已完成任务中提炼出的 skill 草稿字段。
@dataclass(frozen=True)
class LearnedSkillDraftRequest:
    name: str
    task_summary: str
    when_to_use: str
    steps: list[str] = field(default_factory=list)
    tools_required: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    risk_level: str = "low"
    reason: str = ""


# LLM: SkillDraftLearningService writes draft skills but never promotes them.
# 类用途: 根据结构化经验生成 SKILL.md 草稿，避免自学习直接污染 active skill。
class SkillDraftLearningService:
    # LLM: SkillDraftLearningService.__init__ stores only the lifecycle store dependency.
    # 函数用途: 初始化学习草稿服务，不主动写文件。
    def __init__(self, store: SkillLifecycleStore):
        self.store = store

    # LLM: create_draft converts task feedback into markdown and stores it as lifecycle draft.
    # 函数用途: 创建可审查的 skill 草稿，返回生命周期结果。
    def create_draft(self, request: LearnedSkillDraftRequest) -> SkillLifecycleResult:
        markdown = render_learned_skill_markdown(request)
        return self.store.create_draft(
            SkillDraftRequest(name=request.name, markdown=markdown, reason=request.reason)
        )


# LLM: render_learned_skill_markdown keeps generated skills deterministic and auditable.
# 函数用途: 把学习请求渲染成最小但可用的 SKILL.md。
def render_learned_skill_markdown(request: LearnedSkillDraftRequest) -> str:
    tags = _yaml_list(request.tags)
    tools = _yaml_list(request.tools_required)
    body = [
        "---",
        f"name: {_plain(request.name)}",
        f"description: {_plain(request.task_summary)}",
        f"when_to_use: {_plain(request.when_to_use)}",
        "tags:",
        *tags,
        "tools_required:",
        *tools,
        f"risk_level: {_plain(request.risk_level or 'low')}",
        "---",
        "",
        "# Workflow",
        "",
        *_numbered_steps(request.steps),
        "",
    ]
    return "\n".join(body)


# LLM: _numbered_steps renders a stable workflow section for generated skill drafts.
# 函数用途: 将步骤列表转为 Markdown 编号步骤，空列表时给保守默认步骤。
def _numbered_steps(steps: list[str]) -> list[str]:
    clean = [_plain(step) for step in steps if _plain(step)]
    if not clean:
        clean = ["Review the task context and apply the learned pattern conservatively."]
    return [f"{index}. {step}" for index, step in enumerate(clean, start=1)]


# LLM: _yaml_list renders frontmatter arrays without pulling in a YAML writer dependency.
# 函数用途: 将字符串列表渲染成简单 YAML 列表，空列表时写入 general。
def _yaml_list(values: list[str]) -> list[str]:
    clean = [_plain(value) for value in values if _plain(value)]
    return [f"  - {value}" for value in clean] or ["  - general"]


# LLM: _plain keeps generated frontmatter scalar values single-line and deterministic.
# 函数用途: 把外部输入整理为单行文本，避免破坏 frontmatter 结构。
def _plain(value: object) -> str:
    return str(value or "").strip().replace("\n", " ")
