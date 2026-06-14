# LLM: create_skill 模型工具。
#   my-agent 原有 lessons→memory 是"经验复用",这里补"方法→skill 沉淀":agent 做对
#   一件有方法论价值的事,主动把可复用方法写成 SKILL.md 存进 owner skill 库,未来任务
#   用 skill_search 检索复用。契约:①写到 owner skills(隔离 home,不污染项目 builtin);
#   ②写后立即 register 进 router(本 run 即可召回);③owner skills 也进 router 扫描
#   (跨 run 持久,见 core.py wire)。改动时同步 tests/test_create_skill_tool.py。
# 模块用途: 让 agent 把"这次真正验证有效的方法"沉淀成技能,越用越会。
from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from ..agent_core.runtime.owner_roots import runtime_owner_root
from ..tooling.models import BaseTool, ToolExecutionResult, ToolSpec

if TYPE_CHECKING:
    from ..core import SimpleAgent


def build_create_skill_spec() -> ToolSpec:
    return ToolSpec(
        name="create_skill",
        category="capability",
        effect="write",
        description=(
            "把你这次任务真正验证有效、有复用价值的方法/工具链沉淀成一个 skill(SKILL.md),"
            "供未来任务用 skill_search 检索复用。这是自学习:做对了一件有方法论价值的事就把方法存下来。"
        ),
        use_cases=[
            "完成一个有复用价值的方法(某类分析/某工具链的正确用法),想让未来任务检索到",
            "踩坑后总结出可复用的正确做法,沉淀成技能",
        ],
        avoid_when=["一次性琐碎、无复用价值的操作不必创建 skill"],
        keywords=["创建技能", "沉淀方法", "自学习", "create skill", "学到的方法"],
        parameters={
            "name": "必填。skill 短名(kebab-case,如 arxiv-paper-fetch)。",
            "category": "必填。类目(如 research/documents/general)。",
            "description": "必填。一句话说明这是什么方法。",
            "when_to_use": "必填。什么场景该用它。",
            "body": "必填。完整的方法/步骤/工具链正文(markdown)。",
        },
        examples=[
            '{"tool":"create_skill","name":"arxiv-paper-fetch","category":"research",'
            '"description":"从 arXiv 全字段+日期倒序检索最新论文","when_to_use":"找特定主题最新论文时",'
            '"body":"# 方法\\n1. 用 site:arxiv.org + 主题词\\n2. 按日期倒序..."}',
        ],
    )


class CreateSkillTool(BaseTool):
    # 类用途: 把"沉淀方法成 skill"暴露成模型可调用工具,写 owner skill 库并即时注册到 router。
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_create_skill_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        name = _slug(params.get("name"))
        category = _slug(params.get("category")) or "general"
        description = str(params.get("description") or "").strip()
        when_to_use = str(params.get("when_to_use") or "").strip()
        body = str(params.get("body") or "").strip()
        missing = [k for k, v in (("name", name), ("description", description), ("body", body)) if not v]
        if missing:
            return ToolExecutionResult(
                "create_skill",
                False,
                json.dumps({"error": f"缺少必填: {', '.join(missing)}", "hint": "name/description/body 必填"}, ensure_ascii=False),
                error_code="TOOL_INVALID_ARGUMENTS",
            )
        skills_root = runtime_owner_root(self.agent) / "skills"
        target = skills_root / category / name / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        fields = {"name": name, "description": description, "when_to_use": when_to_use, "category": category}
        target.write_text(_render_skill_md(fields, body), encoding="utf-8")
        registered = _register_to_router(self.agent, target, skills_root)
        payload = {
            "ok": True,
            "skill": name,
            "category": category,
            "path": str(target),
            "registered": registered,
            "hint": "已沉淀为 skill;未来任务可用 skill_search 检索复用。",
        }
        return ToolExecutionResult("create_skill", True, json.dumps(payload, ensure_ascii=False, indent=2))


def _slug(value: object) -> str:
    return re.sub(r"[^\w一-鿿-]+", "-", str(value or "").strip().lower()).strip("-")


def _render_skill_md(fields: dict[str, str], body: str) -> str:
    front = ["---", *[f"{key}: {value}" for key, value in fields.items()], "scope: owner", "risk_level: low", "---", ""]
    return "\n".join(front) + body.rstrip() + "\n"


def _register_to_router(agent: object, target: object, source_root: object) -> bool:
    try:
        from .router import CapabilityRouter, from_skill_card
        from .skills import parse_skill_file

        router = getattr(agent, "capability_router", None)
        if not isinstance(router, CapabilityRouter):
            return False
        router.register(from_skill_card(parse_skill_file(target, source=str(source_root))))
        return True
    except (OSError, ValueError, TypeError, KeyError):
        return False


def register_owner_skills(router: object, agent: object) -> int:
    """启动时把 owner skills 目录(含 create_skill 历次沉淀的 SKILL.md)扫进 router,实现跨 run 持久。
    builtin registry 有模块级缓存、不能污染(多 agent 共享),故此处 per-agent 补扫 owner 库。"""
    from .router import CapabilityRouter, from_skill_card
    from .skills import SkillRegistry

    if not isinstance(router, CapabilityRouter):
        return 0
    try:
        owner_skills = runtime_owner_root(agent) / "skills"
        if not owner_skills.is_dir():
            return 0
        count = 0
        for card in SkillRegistry([owner_skills]).scan():
            router.register(from_skill_card(card))
            count += 1
        return count
    except (OSError, ValueError, TypeError):
        return 0


__all__ = ["CreateSkillTool", "build_create_skill_spec", "register_owner_skills"]
