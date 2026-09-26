# LLM: skill_search 模型工具(skill 树第一期,千级 search-first 冷路):prompt
#   常驻只有类目索引,具体技能由模型按需检索——千级 skill 的索引成本从 prompt
#   层(几十 K token)移到工具调用(单次 ~5ms,score_card 含中文 n-gram)。契约:
#   ①公开检索不写状态；②search 返回卡片和稳定 id，get 经同一 turn snapshot 读取正文;
#   ③category 过滤可选;④包内成员只在显式 package_id 下检索/读取，不进入全局 Skill。
#   包正文读取由原 task pin 保存精确引用；大结果沿原归档保留可见来源和预览，不执行脚本。
#   修改时同步检查 skill_tree、包发现和原生归档后精确复制测试。
# 模块用途: 模型的"技能书架检索台":说一句需求,给出最相关的几个技能和它们的
#   稳定引用和按需正文,书架上千本也不用把目录全背进对话里。
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ..settings.defaults import default_agent_config
from ..tooling.models import (
    BaseTool,
    ConcurrencyPolicy,
    EffectResolverPolicy,
    ResourceScopePolicy,
    ToolHandlerOutcome,
    ToolModelHints,
    ToolModelSpec,
    ToolParameterCondition,
    ToolRuntimePolicy,
)
from .package_resources import package_resource_reference
from .router import CapabilityRouter, tokenize
from .skill_snapshot import SkillSnapshotError

if TYPE_CHECKING:
    from ..core import SimpleAgent


# LLM: 包范围是结构化参数；匹配建议只给模型，不据 query 推权限；分页继续携带同包摘要和激活 ID。
# 函数用途: 声明统一方法检索入口，同时区分公开 Skill 和能力包内按需资源。
def build_skill_search_model_spec() -> ToolModelSpec:
    return ToolModelSpec(
        name="skill_search",
        description=(
            "检索或读取当前轮可用 Skill 和能力包；任务明确匹配已列出的专业方法时，先读方法再开展工作。"
            "action=search 按需求返回摘要和稳定 skill_id；"
            "action=get 用 skill_id 读取同一不可变快照里的完整 SKILL.md。"
            "能力包先返回包摘要；指定 package_id 后可检索内部资源，get 省略 resource_path 时读取包入口。"
            "包资源分页读取，has_more=true 时继续 continuation；读取不会执行脚本或授予工具权限。"
            "原样落盘现有脚本或模板时，将完整 source_ref 直接传给 write_file.source_ref，不用手抄正文；落盘不会执行资源。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["search", "get"], "description": "search 或 get；省略时默认 search。"},
                "query": {"type": "string", "description": "search 时用一句话描述需要的方法。"},
                "skill_id": {"type": "string", "description": "get 时逐字使用 search 返回的稳定 skill_id。"},
                "category": {"type": "string", "description": "可选，限定 Skill Categories 类目。"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "description": "最多返回几条，默认 5。"},
                "package_id": {"type": "string", "description": "显式选择能力包；省略时只检索公开 Skill 和包摘要。"},
                "resource_path": {"type": "string", "description": "包内 get 使用声明的相对成员路径，省略读取入口文档。"},
                "offset": {"type": "integer", "minimum": 0, "description": "包内检索的结果偏移，或正文读取的字符偏移。"},
                "max_chars": {"type": "integer", "minimum": 1, "description": "包正文单页字符数，不超过原 tool_read_max_chars 配置。"},
                "expected_package_sha256": {"type": "string", "description": "分页时原样携带 continuation 的包摘要。"},
                "expected_activation_id": {"type": "string", "description": "分页时原样携带 continuation 的激活代次。"},
            },
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="capability",
            use_cases=(
                "任务需要特定领域的方法论时先检索",
                "不确定系统有没有现成做法时，用一句话描述需求来检索",
            ),
            avoid_when=("普通问答且没有匹配的 Skill 或能力包时不必检索",),
            keywords=("技能", "skill", "方法", "工具链", "怎么做", "检索技能"),
            examples=(
                '{"tool":"skill_search","action":"search","query":"把一份英文资料翻译成中文文档"}',
                '{"tool":"skill_search","action":"get","skill_id":"builtin:pdf-translate-toolchain"}',
            ),
        ),
    )


# LLM: 同一快照承接公开 Skill 与显式包范围；包成员读取前后复核原安装且成功后固定原任务引用。
# 类用途: 为模型提供只读方法检索和分页正文读取，任务引用写入不代表执行或扩权。
class SkillSearchTool(BaseTool):
    model_spec = build_skill_search_model_spec()
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("read_only"),
        concurrency_policy=ConcurrencyPolicy("parallel_safe"),
        resource_scopes=ResourceScopePolicy(parameter_names=("skill_id", "query", "package_id", "resource_path"),
            parameter_kinds={"skill_id": "logical", "query": "logical", "package_id": "logical", "resource_path": "logical"}),
        promotes_task_when=(ToolParameterCondition("action", "equals", "get"),
                            ToolParameterCondition("package_id", "nonempty_string")),
    )

    # 类用途: 把 CapabilityRouter 的 skill 检索暴露成模型可调用的只读工具。
    def __init__(self, agent: SimpleAgent):
        self.agent = agent

    # LLM: 包参数只能进入明确包分支；不能把未声明资源路径交给全局 Skill 或文件工具兜底。
    # 函数用途: 按结构化动作选择公开检索、原 Skill 读取或包内按需读取。
    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        action = str(params.get("action") or ("get" if params.get("skill_id") else "search")).strip()
        if params.get("package_id"):
            return self._package_action(params, action)
        if params.get("resource_path"):
            return _invalid("resource_path 必须同时指定 package_id")
        if action == "get":
            return self._get(params)
        if action != "search":
            return _invalid("action 只接受 search 或 get")
        return self._search(params)

    # LLM: 未限定 package_id 时只查公开 Skill 与包级摘要，绝不遍历私有成员来给包加召回分数。
    # 函数用途: 返回公开方法卡或独立能力包摘要。
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
                for hit in router.search(query, limit=0, kinds={"skill", "capability_package"})
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
            ({"kind": "capability_package", "package_id": hit.card.metadata["package_id"],
              "name": hit.card.name, "description": hit.card.description,
              "stable_id": hit.card.metadata["stable_id"], "score": round(hit.score, 1)}
             if hit.card.kind == "capability_package" else {
                "name": hit.card.name,
                "skill_id": str(hit.card.metadata.get("stable_id") or ""),
                "source": str(hit.card.metadata.get("scope") or ""),
                "category": str(hit.card.metadata.get("category") or "general"),
                "description": hit.card.description,
                "when_to_use": hit.card.when_to_use[:1],
                "score": round(hit.score, 1),
            })
            for hit in hits
        ]
        payload = {
            "matches": matches,
            "hint": "选择后用 action=get 读取：Skill 携带原样 skill_id，能力包携带原样 package_id。",
        }
        return ToolHandlerOutcome("skill_search", True, json.dumps(payload, ensure_ascii=False, indent=2))

    # LLM: scoped 成员只来自本轮包声明，错误 fail closed；资源引用沿原回执和归档展示，取消不降级。
    # 函数用途: 执行包范围检索或读取，保留同代 continuation，并让大正文归档后仍能按原引用复制。
    def _package_action(self, params: dict[str, object], action: str) -> ToolHandlerOutcome:
        if action not in {"search", "get"} or params.get("skill_id"):
            return _invalid("包范围只接受 search/get，不能同时传 skill_id")
        try:
            snapshot = _snapshot_for(self.agent)
            if snapshot is None:
                return _unavailable()
            package_id = str(params["package_id"]).strip()
            package = snapshot.resolve_package(package_id)
            if package is None:
                raise SkillSnapshotError("CAPABILITY_PACKAGE_NOT_AVAILABLE")
            _validate_package_continuation(package, params)
            offset = _nonnegative_int(params, "offset", 0)
            if action == "search":
                payload = _package_matches(package, params, offset)
            else:
                payload = self._package_body(snapshot, package, params, offset)
                config = getattr(self.agent, "config", None) or default_agent_config()
                return _package_read_outcome(payload, max(0, int(config.tool_output_preview_chars)))
            return ToolHandlerOutcome("skill_search", True, json.dumps(payload, ensure_ascii=False, indent=2))
        except SkillSnapshotError as exc:
            return _snapshot_unavailable(exc)
        except (OSError, ValueError) as exc:
            return _invalid(str(exc))

    # LLM: bytes 校验后先 pin 原任务引用；仅 UTF-8 正文按原配置分页，来源元数据排在正文前，pin 错误不能降级。
    # 函数用途: 返回一页包正文与原样复制引用、完整性和续读参数，避免引用落在长正文尾部。
    def _package_body(self, snapshot, package, params, offset) -> dict[str, object]:
        from .task_references import pin_package_reference

        member_path = str(params.get("resource_path") or "")
        member = package.resolve(member_path)
        if member is None:
            raise SkillSnapshotError("CAPABILITY_RESOURCE_NOT_AVAILABLE")
        raw = snapshot.read_in_package(package.package_id, member.path)
        try:
            body = raw.decode("utf-8")
        except UnicodeError as exc:
            raise SkillSnapshotError("CAPABILITY_RESOURCE_NOT_TEXT") from exc
        config = getattr(self.agent, "config", None) or default_agent_config()
        default_limit = max(1, int(config.tool_read_max_chars))
        requested = _nonnegative_int(params, "max_chars", default_limit)
        if requested == 0 or offset > len(body):
            raise ValueError("max_chars 必须大于零，offset 不能超过正文长度")
        limit = min(requested, default_limit)
        window = body[offset:offset + limit]
        next_offset = offset + len(window)
        pin_package_reference(self.agent, package.to_ref())
        payload = {"source_ref": package_resource_reference(package, member.path),
                   "kind": "capability_package", **package.to_ref(), "resource_path": member.path,
                   "resource_sha256": member.sha256, "body": window, "offset": offset,
                   "total_chars": len(body), "has_more": next_offset < len(body)}
        if payload["has_more"]:
            payload["continuation"] = {**_package_continuation(package, "get", next_offset),
                                       "resource_path": member.path, "max_chars": limit}
        return payload

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


# LLM: 只投影当前已校验页；原正文、包 continuation 和归档游标各守原合同，不把预览标成已读全文。
# 函数用途: 复用原 live_prompt_output 保留精确复制引用和有界预览，完整内容继续由原归档保存/读取。
def _package_read_outcome(payload: dict[str, object], preview_chars: int) -> ToolHandlerOutcome:
    output = json.dumps(payload, ensure_ascii=False, indent=2)
    envelope = {"source_ref": dict(payload["source_ref"])}
    if len(output) > preview_chars:
        body = str(payload["body"])
        summary = {key: value for key, value in payload.items() if key != "body"}
        summary.update({
            "body_preview": body[:preview_chars],
            "body_preview_complete": len(body) <= preview_chars,
            "resource_copy_hint": "原样复制本资源时，将完整 source_ref 传给 write_file.source_ref；不要手抄或改写脚本。",
            "body_read_hint": "body_preview_complete=false 时预览不是当前页全文；需要完整正文时用原归档锚点 read_artifact，并按归档窗口继续读。包的 continuation 只用于下一包正文页，两种游标不可混用。",
        })
        envelope["tool_output_policy"] = {
            "live_prompt_output": json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
            "requires_recovery_artifact": True,
        }
    return ToolHandlerOutcome("skill_search", True, output, result_envelope=envelope)


# LLM: continuation 绑定完整包字节和原激活，页码不能在换代后静默套到新资源。
# 函数用途: 拒绝来自其它包版本或已重新安装代次的分页参数。
def _validate_package_continuation(package, params) -> None:
    for key, expected in (("expected_package_sha256", package.package_sha256),
                          ("expected_activation_id", package.activation_id)):
        if key in params and params[key] != expected:
            raise SkillSnapshotError("SKILL_SNAPSHOT_STALE")


# LLM: 分页参数只有宿主当前声明身份，没有路径猜测或隐式授权。
# 函数用途: 生成继续读取同一能力包的结构化工具参数。
def _package_continuation(package, action: str, offset: int) -> dict[str, object]:
    return {"action": action, "package_id": package.package_id, "offset": offset,
            "expected_package_sha256": package.package_sha256, "expected_activation_id": package.activation_id}


# LLM: 只匹配声明路径，不扫描正文或创建内部 SkillCard；结果数量与外部 Skill 计数完全独立。
# 函数用途: 在显式包范围内分页列出或按名称检索私有资源。
def _package_matches(package, params, offset: int) -> dict[str, object]:
    query = str(params.get("query") or "").strip()
    tokens = tokenize(query)
    members = [member for member in package.members
               if not query or any(token in member.path.lower() for token in tokens)]
    members.sort(key=lambda member: member.path)
    limit = _safe_limit(params.get("limit"))
    selected = members[offset:offset + limit]
    next_offset = offset + len(selected)
    payload = {"package_id": package.package_id,
               "matches": [{"resource_path": item.path, "content_sha256": item.sha256,
                            "executable": item.executable,
                            "source_ref": package_resource_reference(package, item.path)} for item in selected],
               "offset": offset, "total_matches": len(members), "has_more": next_offset < len(members)}
    if payload["has_more"]:
        payload["continuation"] = {**_package_continuation(package, "search", next_offset), "query": query, "limit": limit}
    return payload


# LLM: 偏移和预算是结构化整数，不能用 bool、文本或负值触发隐式强转。
# 函数用途: 检查分页参数并使用明确默认值。
def _nonnegative_int(params, key: str, default: int) -> int:
    value = params.get(key, default)
    if type(value) is not int or value < 0:
        raise ValueError(f"{key} 必须为非负整数")
    return value


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
