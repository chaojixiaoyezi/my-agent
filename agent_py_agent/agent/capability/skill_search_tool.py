# LLM: skill_search 模型工具(skill 树第一期,千级 search-first 冷路):prompt
#   常驻只有类目索引,具体技能由模型按需检索——千级 skill 的索引成本从 prompt
#   层(几十 K token)移到工具调用(单次 ~5ms,score_card 含中文 n-gram)。契约:
#   ①只读检索零副作用;②返回卡片摘要+正文路径(渐进加载:模型 read_file 跟进);
#   ③category 过滤可选;④空库/无命中返回结构化提示不报错。改动时同步检查
#   tests/test_skill_search_tool.py 与 router 的类目索引。
# 模块用途: 模型的"技能书架检索台":说一句需求,给出最相关的几个技能和它们的
#   正文位置,书架上千本也不用把目录全背进对话里。
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ..tooling.models import BaseTool, ToolExecutionResult, ToolSpec
from .router import CapabilityRouter

if TYPE_CHECKING:
    from ..core import SimpleAgent


# 函数用途: skill_search 的工具说明书(进工具目录,引导模型在需要领域方法时先搜)。
def build_skill_search_spec() -> ToolSpec:
    return ToolSpec(
        name="skill_search",
        category="capability",
        effect="read_only",
        description="按需求检索技能库（领域方法/工具链知识），返回最相关技能的摘要与正文路径。",
        use_cases=[
            "任务需要特定领域的方法论（如深度代码分析、文档翻译工具链）时先搜一下",
            "不确定系统有没有现成做法时，用一句话描述需求来检索",
        ],
        avoid_when=["普通问答或已明确知道怎么做时不必检索"],
        keywords=["技能", "skill", "方法", "工具链", "怎么做", "检索技能"],
        parameters={
            "query": "必填。用一句话描述你要做的事或需要的方法。",
            "category": "可选。限定类目（见 Skill Categories 索引）。",
            "limit": "可选。最多返回几条，默认 5。",
        },
        examples=[
            '{"tool":"skill_search","query":"把一份英文资料翻译成中文文档"}',
            '{"tool":"skill_search","query":"深入分析一个代码项目的架构","category":"research"}',
        ],
    )


class SkillSearchTool(BaseTool):
    # 类用途: 把 CapabilityRouter 的 skill 检索暴露成模型可调用的只读工具。
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_skill_search_spec()

    # 函数用途: 执行一次检索;命中给卡片+正文路径,未命中给类目索引当线索。
    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        query = str(params.get("query") or "").strip()
        if not query:
            return ToolExecutionResult(
                "skill_search",
                False,
                json.dumps({"error": "query 不能为空", "hint": "用一句话描述要做的事"}, ensure_ascii=False),
                error_code="TOOL_INVALID_ARGUMENTS",
            )
        router = _router_for(self.agent)
        category = str(params.get("category") or "").strip()
        limit = _safe_limit(params.get("limit"))
        hits = [
            hit
            for hit in router.search(query, limit=0, kinds={"skill"})
            if not category or str(hit.card.metadata.get("category") or "") == category
        ][:limit]
        if not hits:
            payload = {
                "matches": [],
                "hint": "没有命中的技能;可以换关键词,或按下方类目浏览。",
                "categories": router.render_category_index(),
            }
            return ToolExecutionResult("skill_search", True, json.dumps(payload, ensure_ascii=False, indent=2))
        matches = [
            {
                "name": hit.card.name,
                "category": str(hit.card.metadata.get("category") or "general"),
                "description": hit.card.description,
                "when_to_use": hit.card.when_to_use[:1],
                "body_path": hit.card.path,
                "score": round(hit.score, 1),
            }
            for hit in hits
        ]
        payload = {"matches": matches, "hint": "用 read_file 读 body_path 获取完整方法/工具链。"}
        return ToolExecutionResult("skill_search", True, json.dumps(payload, ensure_ascii=False, indent=2))


# 函数用途: 取 agent 上挂的路由器(没有就建默认——自动带内置 skill)。
def _router_for(agent) -> CapabilityRouter:
    router = getattr(agent, "capability_router", None)
    if isinstance(router, CapabilityRouter):
        return router
    router = CapabilityRouter()
    agent.capability_router = router
    return router


# 函数用途: 解析 limit 参数(坏值回退 5,上限 20 防刷屏)。
def _safe_limit(value: object) -> int:
    try:
        limit = int(value or 5)
    except (TypeError, ValueError):
        return 5
    return max(1, min(limit, 20))


__all__ = ["SkillSearchTool", "build_skill_search_spec"]
