# LLM: skill_search 模型工具(skill 树第一期,千级 search-first 冷路):prompt
#   常驻只有类目索引,具体技能由模型按需检索——千级 skill 的索引成本从 prompt
#   层(几十 K token)移到工具调用(单次 ~5ms,score_card 含中文 n-gram)。契约:
#   ①公开检索不写状态；②search 返回卡片和稳定 id，get 经同一 turn snapshot 读取正文;
#   ③category 过滤可选;④包内成员只在显式 package_id 下检索/读取，不进入全局 Skill。
#   包正文读取由原 task pin 保存精确引用；大结果沿原归档保留可见来源和预览，不执行脚本。
#   next_read/next_search 只投影同代显式调用参数；包命名空间仅指已声明成员，不从正文推断业务路径归属。
#   search 显式携带 resource_path 时在快照读取前报参数错，不静默丢条件或改成 get。
#   错选择器仍失败，只在当前可见包精确命中时建议重试，不自动读取或晋升。
#   修改时同步检查 skill_tree、包发现、选择器恢复和原生归档后精确复制测试。
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
from .package_snapshot import package_read_parameters
from .router import CapabilityRouter, tokenize
from .skill_snapshot import SkillSnapshotError

if TYPE_CHECKING:
    from ..core import SimpleAgent


# LLM: 包范围与动作是结构化参数；resource_path 只接受显式 get，字段约束和 action 默认须与 execute 一致。
# 函数用途: 声明统一方法检索入口和参数边界，不据正文路径、query 或错选择器扩权。
def build_skill_search_model_spec() -> ToolModelSpec:
    return ToolModelSpec(
        name="skill_search",
        description=(
            "检索或读取当前轮可用 Skill 和能力包；任务明确匹配已列出的专业方法时，先读方法再开展工作。"
            "action=search 按需求返回公开 Skill 或能力包摘要；结果的 next_read 可原样作为本工具参数读取。"
            "search 用 query 检索，不能传 resource_path，即使是空串。"
            "action=get 时，普通 Skill 使用 skill_id 读取 SKILL.md，能力包使用 package_id；两种选择器互斥。"
            "包的 stable_id 用于授权和任务引用，不能填入 skill_id。指定 package_id 后可检索内部资源，"
            "get 省略 resource_path 时读取包入口。错选择器仍失败；若返回 next_read，可发起新的显式调用。"
            "已声明的包成员属于包命名空间，不是工作区文件；用 next_search 检索声明，再用匹配项 next_read 读取。"
            "正文中的业务输入/交付路径不因此变成包成员；不要用 read_file 或 find_files 猜包安装位置。"
            "包资源分页读取，has_more=true 时继续 continuation；读取不会执行脚本或授予工具权限。"
            "原样落盘现有脚本或模板时，将完整 source_ref 直接传给 write_file.source_ref，不用手抄正文；落盘不会执行资源。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["search", "get"], "description": "search 或 get；省略时，有 skill_id 则默认 get，否则 search。读取能力包必须显式指定 get。"},
                "query": {"type": "string", "description": "search 时用一句话描述需要的方法。"},
                "skill_id": {"type": "string", "description": "仅普通 Skill 的 get 使用，逐字复制 search 返回的 skill_id；与 package_id 互斥，不能填包的 stable_id。"},
                "category": {"type": "string", "description": "可选，限定 Skill Categories 类目。"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "description": "最多返回几条，默认 5。"},
                "package_id": {"type": "string", "description": "逐字复制能力包摘要的 package_id；与 skill_id 互斥。action=get 读取包入口或资源，action=search 检索包内资源；省略本字段时只检索公开摘要。"},
                "resource_path": {"type": "string", "description": "仅包内 action=get 可传；action=search 即使空串也不能传。使用已声明的包根相对成员路径，不是宿主地址；省略时读取入口。"},
                "offset": {"type": "integer", "minimum": 0, "description": "包内检索的结果偏移，或正文读取的字符偏移。"},
                "max_chars": {"type": "integer", "minimum": 1, "description": "包正文单页字符数，不超过原 tool_read_max_chars 配置。"},
                "expected_package_sha256": {"type": "string", "description": "原样携带 next_read、next_search 或分页 continuation 中的包摘要，防止读到同名新版本。"},
                "expected_activation_id": {"type": "string", "description": "原样携带 next_read、next_search 或分页 continuation 中的激活代次，停用重启后的旧建议不能继续使用。"},
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
                '{"tool":"skill_search","action":"get","package_id":"example-package"}',
                '{"tool":"skill_search","action":"search","package_id":"example-package","query":"review"}',
                '{"tool":"skill_search","action":"get","package_id":"example-package","resource_path":"methods/review.md"}',
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

    # LLM: search 的 resource_path 冲突在读取快照前拒绝，不能丢条件、转换动作或返回未授权成员建议；默认动作保持原合同。
    # 函数用途: 先检查动作与路径参数，再选择公开检索、原 Skill 读取或包内读取，避免成功却查了另一件事。
    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        action = str(params.get("action") or ("get" if params.get("skill_id") else "search")).strip()
        if action == "search" and "resource_path" in params:
            return _invalid("resource_path 仅允许在 action=get 时使用",
                            hint="检索包资源请用 action=search、package_id 和 query；读取资源请显式使用 action=get 和 package_id。")
        if params.get("package_id"):
            return self._package_action(params, action)
        if params.get("resource_path"):
            return _invalid("resource_path 必须同时指定 package_id")
        if action == "get":
            return self._get(params)
        if action != "search":
            return _invalid("action 只接受 search 或 get")
        return self._search(params)

    # LLM: 未限定 package_id 时只查公开 Skill 与包级摘要；next_read 只从公开身份生成，不含成员路径、不预读或 pin。
    # 函数用途: 返回公开方法卡及可原样重用的读取参数，让模型无需从多种身份字段猜调用方式。
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
              "stable_id": hit.card.metadata["stable_id"], "score": round(hit.score, 1),
              "next_read": package_read_parameters(hit.card.metadata)}
             if hit.card.kind == "capability_package" else {
                "kind": "skill",
                "name": hit.card.name,
                "skill_id": str(hit.card.metadata.get("stable_id") or ""),
                "source": str(hit.card.metadata.get("scope") or ""),
                "category": str(hit.card.metadata.get("category") or "general"),
                "description": hit.card.description,
                "when_to_use": hit.card.when_to_use[:1],
                "score": round(hit.score, 1),
                "next_read": {"action": "get", "skill_id": str(hit.card.metadata.get("stable_id") or "")},
            })
            for hit in hits
        ]
        payload = {
            "matches": matches,
            "hint": "选择后原样使用对应 next_read 调用本工具；Skill 用 skill_id，能力包用 package_id，不能混用。",
        }
        return ToolHandlerOutcome("skill_search", True, json.dumps(payload, ensure_ascii=False, indent=2))

    # LLM: scoped 成员只来自本轮包声明，错误 fail closed；成功结果共用有界地址说明和同代导航，不读取其它成员。
    # 函数用途: 执行包范围检索或读取，让小结果和归档大结果都能定位原包；取消、权限及 pin 仍沿原合同。
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
            payload = {**_package_navigation(package), **payload}
            config = getattr(self.agent, "config", None) or default_agent_config()
            return _package_read_outcome(payload, max(0, int(config.tool_output_preview_chars)))
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

    # LLM: 保持普通 Skill resolve 的既有优先级；失败后仅精确匹配当前受限包身份来给建议，不读成员、不 pin、不转换执行。
    # 函数用途: 读取普通 Skill，或为错填包身份的请求返回仍为失败的结构化纠错信息。
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
            package = next((item for item in snapshot.packages if skill_id in {item.package_id, item.stable_id}), None)
            if package is not None:
                return _invalid(
                    "能力包不能通过 skill_id 读取", hint="使用 next_read 重新调用；本次没有读取包正文。",
                    selector_mismatch={"received": "skill_id", "expected": "package_id", "kind": "capability_package"},
                    next_read=package_read_parameters(package.to_ref()),
                )
            return _invalid("当前轮没有这个可用 skill_id", hint="重新 action=search 查找当前可用摘要，再使用结果的 next_read")
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


# LLM: 此说明仅来自已授权包的结构化身份，不解析正文、不枚举成员、不 pin；导航复用原分页参数，source_ref 字段集不变。
# 函数用途: 明确已声明成员属于包命名空间，给出可原样使用的限页搜索；业务文件仍按任务工作区处理。
def _package_navigation(package) -> dict[str, object]:
    return {
        "resource_namespace": {
            "kind": "capability_package", "path_base": "package_root", "declared_members_only": True,
            "filesystem_path": False, "reader_tool": "skill_search", "path_parameter": "resource_path",
        },
        "next_search": {**_package_continuation(package, "search", 0), "limit": 5},
        "resource_access_hint": "只有清单中已声明的包内资源使用 package_id + resource_path；它们不是工作区文件。"
            "正文中的业务输入和交付路径不因此变成包成员。先用 next_search 检索声明，再原样使用匹配项 next_read；不要猜安装位置。",
        "resource_copy_hint": "需要原样复制已定位资源时，将该资源完整 source_ref 传给 write_file.source_ref，"
            "另选工作区目标 path；不要手抄或改写脚本。复制不执行资源。",
    }


# LLM: 只投影当前已校验页；正文/匹配预览、包 continuation 与归档窗口分开，导航和完整引用始终保留。
# 函数用途: 复用 live_prompt_output 展示有界内容，完整页仍沿原归档恢复；零预览也不能丢包地址和复制指引。
def _package_read_outcome(payload: dict[str, object], preview_chars: int) -> ToolHandlerOutcome:
    output = json.dumps(payload, ensure_ascii=False, indent=2)
    envelope = {"source_ref": dict(payload["source_ref"])} if "source_ref" in payload else {}
    if len(output) > preview_chars:
        summary = {key: value for key, value in payload.items() if key not in {"body", "matches"}}
        if "body" in payload:
            body = str(payload["body"])
            summary.update(body_preview=body[:preview_chars], body_preview_complete=len(body) <= preview_chars)
        else:
            matches = payload["matches"]
            preview = _package_match_preview(matches, preview_chars)
            summary.update(matches_preview=preview, matches_preview_complete=len(preview) == len(matches))
        summary["body_read_hint"] = (
            "body_preview_complete 或 matches_preview_complete 为 false 时，预览不是当前页全文；"
            "用原归档锚点 read_artifact 取回当前页，并按归档窗口继续读。"
            "has_more/continuation 只表示包的下一正文页或结果页，不代表当前预览完整；两种游标不可混用。"
        )
        envelope["tool_output_policy"] = {
            "live_prompt_output": json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
            "requires_recovery_artifact": True,
        }
    return ToolHandlerOutcome("skill_search", True, output, result_envelope=envelope)


# LLM: 预览只取原页完整卡片前缀，不能截断 source_ref/next_read，也不能改变原页的 offset/continuation。
# 函数用途: 在原字符预算内展示搜索结果，放不下的完整页仍由归档读取，不枚举额外资源。
def _package_match_preview(matches: list[dict[str, object]], limit: int) -> list[dict[str, object]]:
    preview = []
    used = 2
    for item in matches:
        size = len(json.dumps(item, ensure_ascii=False)) + (2 if preview else 0)
        if used + size > limit:
            break
        preview.append(item)
        used += size
    return preview


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


# LLM: 只匹配声明路径，不扫描正文或创建内部 SkillCard；next_read 保留显式包范围，资源计数不进入全局 Skill。
# 函数用途: 在已选包内分页检索私有资源，同时给出无需猜路径归属的读取参数。
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
                            "source_ref": package_resource_reference(package, item.path),
                            "next_read": package_read_parameters(package.to_ref(), item.path)} for item in selected],
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


# LLM: 结构化建议不把失败改为成功；只有调用方已在受限快照精确命中时才传入选择器信息，不执行建议。
# 函数用途: 返回统一参数错误，可附不会泄漏未知能力的纠错字段。
def _invalid(
    error: str, *, hint: str = "", selector_mismatch: dict[str, str] | None = None,
    next_read: dict[str, object] | None = None,
) -> ToolHandlerOutcome:
    payload: dict[str, object] = {"error": error, "hint": hint}
    if selector_mismatch is not None:
        payload["selector_mismatch"] = selector_mismatch
    if next_read is not None:
        payload["next_read"] = next_read
    return ToolHandlerOutcome(
        "skill_search",
        False,
        json.dumps(payload, ensure_ascii=False),
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
