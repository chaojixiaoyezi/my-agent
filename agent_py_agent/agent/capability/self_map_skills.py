# LLM: 只读当轮快照及服务规范根声明，不发现、刷新或读取正文；子代理沿精确restricted子集。
# 模块用途: 登记普通方法、数量和管理员权威位置，保留stable_id及合法正文入口。
from ..tooling.self_map_registry import SectionDescriptor, SectionProjection, map_item, next_read
from ..user_space.self_map_paths import location_items
from .skill_learning_versions import frozen_learned_metadata


# LLM: 普通方法领域装配固定用途，schema和工具说明不维护第二份skills编号。
# 函数用途: 登记普通方法分块。
def register_skill_section(registry):
    registry.register(SectionDescriptor("skills", "方法", 30, skill_projection, "普通Skill方法清单、短用途与数量"))


# LLM: 未绑定是未观测，错误仅安全码；普通方法数不混入内容包，位置不从条目私有路径推导。
# 函数用途: 列当前授权方法数量/用途及管理员位置，不执行发现或reload。
def skill_projection(context):
    snapshot = getattr(context.agent, "_current_skill_snapshot", None)
    locations = _skill_locations(context)
    if snapshot is None:
        return SectionProjection((), "partial", "not_observed", source="SkillSnapshot", count_known=False,
                                 counts={"ordinary_skills": None}, count_reasons={"ordinary_skills": "本轮方法快照未绑定。"}, locations=locations)
    if snapshot.owner_id != context.agent.home_paths.owner_id:
        raise ValueError("snapshot owner mismatch")
    items = tuple(_skill_item(entry) for entry in snapshot.enabled_entries())
    errors = tuple(sorted({error.code for error in snapshot.errors}))
    return SectionProjection(items, "partial" if errors else "complete", errors=errors, source="SkillSnapshot.enabled_entries",
                             counts={"ordinary_skills": None if errors else len(items)},
                             count_reasons={"ordinary_skills": "快照来源不完整，不能确认完整方法数量。"} if errors else {}, locations=locations)


# LLM: 完整管理员判据之后，只取已绑定服务的base_roots；不调用插件根、正文发现或快照刷新。
# 函数用途: 给方法根的原读写裁决，重复来源根保持分别可见。
def _skill_locations(context):
    if not context.admin:
        return ()
    roots = context.agent.skills_service.base_roots(context.agent.tools.workspace_root)
    paths = {}
    for index, root in enumerate(roots):
        identity = root.source if root.source not in paths else f"{root.source}:{index}"
        paths[identity] = root.path
    return location_items(context, paths)


# LLM: 普通快照只给读取建议，不验证正文；托管自学Skill沿原生命周期公开读口复核撤回，不reload或读取版本正文。
# 函数用途: 保留方法用途和稳定引用，同时准确标注停用、删除或来源不可用。
def _skill_item(entry):
    fields = {"source_layer": entry.source, "summary": entry.description, "category": entry.category,
              "recommended": entry.recommended, "body_observed": "not_observed", "readable": None}
    state = "known"
    if entry.frozen_path:
        try:
            metadata = frozen_learned_metadata(entry)
            fields.update(lifecycle=metadata, readable=metadata["authorized"])
            state = "known" if metadata["authorized"] else "revoked"
        except (OSError, ValueError, IndexError):
            state = "unavailable"
    if state == "known":
        fields["next_read"] = next_read("skill_search", {"action": "get", "skill_id": entry.stable_id})
    return map_item(entry.stable_id, entry.name, state=state, fields=fields)
