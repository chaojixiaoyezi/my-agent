# LLM: skill_search 模型工具(skill 树第一期,千级 search-first 冷路):prompt
#   常驻只有类目索引,具体技能由模型按需检索——千级 skill 的索引成本从 prompt
#   层(几十 K token)移到工具调用(单次 ~5ms,score_card 含中文 n-gram)。契约:
#   ①只读检索零副作用;②search 返回卡片和稳定 id，get 经同一 turn snapshot 读取正文;
#   ③category 过滤可选;④空库/无命中返回结构化提示不报错。改动时同步检查
#   tests/test_skill_search_tool.py 与 router 的类目索引。
# 模块用途: 模型的"技能书架检索台":说一句需求,给出最相关的几个技能和它们的
#   稳定引用和按需正文,书架上千本也不用把目录全背进对话里。
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ..tooling.models import (
    BaseTool,
    ConcurrencyPolicy,
    EffectResolverPolicy,
    ResourceScopePolicy,
    ToolHandlerOutcome,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)
from .router import CapabilityRouter
from .skill_snapshot import SkillSnapshotError

if TYPE_CHECKING:
    from ..core import SimpleAgent


# 函数用途: skill_search 的工具说明书(进工具目录,引导模型在需要领域方法时先搜)。
def build_skill_search_model_spec() -> ToolModelSpec:
    return ToolModelSpec(
        name="skill_search",
        description=(
            "检索或读取当前轮可用技能。action=search 按需求返回摘要和稳定 skill_id；"
            "action=get 用 skill_id 读取同一不可变快照里的完整 SKILL.md。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["search", "get"], "description": "search 或 get；省略时默认 search。"},
                "query": {"type": "string", "description": "search 时用一句话描述需要的方法。"},
                "skill_id": {"type": "string", "description": "get 时逐字使用 search 返回的稳定 skill_id。"},
                "category": {"type": "string", "description": "可选，限定 Skill Categories 类目。"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "description": "最多返回几条，默认 5。"},
            },
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="capability",
            use_cases=(
                "任务需要特定领域的方法论时先检索",
                "不确定系统有没有现成做法时，用一句话描述需求来检索",
            ),
            avoid_when=("普通问答或已明确知道怎么做时不必检索",),
            keywords=("技能", "skill", "方法", "工具链", "怎么做", "检索技能"),
            examples=(
                '{"tool":"skill_search","action":"search","query":"把一份英文资料翻译成中文文档"}',
                '{"tool":"skill_search","action":"get","skill_id":"builtin:pdf-translate-toolchain"}',
            ),
        ),
    )


class SkillSearchTool(BaseTool):
    model_spec = build_skill_search_model_spec()
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("read_only"),
        concurrency_policy=ConcurrencyPolicy("parallel_safe"),
        resource_scopes=ResourceScopePolicy(parameter_names=("skill_id", "query"),
            parameter_kinds={"skill_id": "logical", "query": "logical"}),
    )

    # 类用途: 把 CapabilityRouter 的 skill 检索暴露成模型可调用的只读工具。
    def __init__(self, agent: SimpleAgent):
        self.agent = agent

    # 函数用途: 执行一次检索;命中给卡片+稳定 id,未命中给类目索引当线索。
    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        action = str(params.get("action") or ("get" if params.get("skill_id") else "search")).strip()
        if action == "get":
            return self._get(params)
        if action != "search":
            return _invalid("action 只接受 search 或 get")
        return self._search(params)

    def _search(self, params: dict[str, object]) -> ToolHandlerOutcome:
        query = str(params.get("query") or "").strip()
        if not query:
            return _invalid("query 不能为空", hint="用一句话描述要做的事")
        router = _router_for(self.agent)
        if router is None:
            return _unavailable()
        category = str(params.get("category") or "").strip()
        limit = _safe_limit(params.get("limit"))
        try:
            hits = [
                hit
                for hit in router.search(query, limit=0, kinds={"skill"})
                if not category or str(hit.card.metadata.get("category") or "") == category
            ][:limit]
        except SkillSnapshotError as exc:
            return _snapshot_unavailable(exc)
        if not hits:
            payload = {
                "matches": [],
                "hint": "没有命中的技能;可以换关键词,或按下方类目浏览。",
                "categories": router.render_category_index(),
            }
            return ToolHandlerOutcome("skill_search", True, json.dumps(payload, ensure_ascii=False, indent=2))
        matches = [
            {
                "name": hit.card.name,
                "skill_id": str(hit.card.metadata.get("stable_id") or ""),
                "source": str(hit.card.metadata.get("scope") or ""),
                "category": str(hit.card.metadata.get("category") or "general"),
                "description": hit.card.description,
                "when_to_use": hit.card.when_to_use[:1],
                "score": round(hit.score, 1),
            }
            for hit in hits
        ]
        payload = {
            "matches": matches,
            "hint": "选择合适项后，用 skill_search action=get 和原样 skill_id 读取完整方法。",
        }
        return ToolHandlerOutcome("skill_search", True, json.dumps(payload, ensure_ascii=False, indent=2))

    def _get(self, params: dict[str, object]) -> ToolHandlerOutcome:
        skill_id = str(params.get("skill_id") or "").strip()
        if not skill_id:
            return _invalid("skill_id 不能为空", hint="先 search，再逐字使用返回的 skill_id")
        try:
            snapshot = _snapshot_for(self.agent)
        except SkillSnapshotError as exc:
            return _snapshot_unavailable(exc)
        if snapshot is None:
            return _unavailable()
        entry = snapshot.resolve(skill_id)
        if entry is None:
            return _invalid("当前轮没有这个可用 skill_id", hint="重新 search 获取当前轮稳定 id")
        try:
            body = snapshot.read_body(skill_id)
        except SkillSnapshotError as exc:
            return ToolHandlerOutcome(
                "skill_search",
                False,
                json.dumps({"error": str(exc), "skill_id": skill_id}, ensure_ascii=False),
                error_code="SKILL_SNAPSHOT_UNAVAILABLE",
            )
        payload = {
            "skill_id": entry.stable_id,
            "name": entry.name,
            "source": entry.source,
            "path": entry.path,
            "content_sha256": entry.content_sha256,
            "body": body,
        }
        return ToolHandlerOutcome("skill_search", True, json.dumps(payload, ensure_ascii=False, indent=2))


# 函数用途: 只取 composition root 装配的唯一路由器；缺失时 fail closed。
def _router_for(agent) -> CapabilityRouter | None:
    router = getattr(agent, "capability_router", None)
    if isinstance(router, CapabilityRouter):
        return router
    return None


def _snapshot_for(agent):
    provider = getattr(agent, "current_skill_snapshot", None)
    if callable(provider):
        return provider()
    return getattr(agent, "_current_skill_snapshot", None)


def _invalid(error: str, *, hint: str = "") -> ToolHandlerOutcome:
    return ToolHandlerOutcome(
        "skill_search",
        False,
        json.dumps({"error": error, "hint": hint}, ensure_ascii=False),
        error_code="TOOL_INVALID_ARGUMENTS",
    )


def _unavailable() -> ToolHandlerOutcome:
    return ToolHandlerOutcome(
        "skill_search",
        False,
        json.dumps({"error": "Skill 服务未装配"}, ensure_ascii=False),
        error_code="TOOL_UNAVAILABLE",
    )


def _snapshot_unavailable(exc: SkillSnapshotError) -> ToolHandlerOutcome:
    return ToolHandlerOutcome(
        "skill_search",
        False,
        json.dumps({"error": str(exc)}, ensure_ascii=False),
        error_code="SKILL_SNAPSHOT_UNAVAILABLE",
    )


# 函数用途: 解析 limit 参数(坏值回退 5,上限 20 防刷屏)。
def _safe_limit(value: object) -> int:
    try:
        limit = int(value or 5)
    except (TypeError, ValueError):
        return 5
    return max(1, min(limit, 20))


__all__ = ["SkillSearchTool", "build_skill_search_model_spec"]
