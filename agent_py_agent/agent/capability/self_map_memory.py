# LLM: 只列已绑定记忆组件类型和规范路径，不读取事实/候选/聊天/Persona正文，不遍历账本计数。
# 模块用途: 登记记忆、Persona和学习组件的安全元信息、数量未知原因及原读入口。
from ..tooling.self_map_registry import SectionDescriptor, SectionProjection, map_item, next_read
from ..user_space.self_map_paths import location_items


# LLM: 记忆领域自行声明静态用途，地图不建记忆副本或读取正文来生成说明。
# 函数用途: 接入记忆分块。
def register_memory_section(registry):
    registry.register(SectionDescriptor("memory", "记忆与人格", 70, memory_projection, "记忆组件、正式记忆位置及人格入口"))


# LLM: 子/隔离不增owner读权；管理员摘要直接给规范正式位置，不读正文计数，组件数不是正式事实数。
# 函数用途: 展示合法组件、正式数量未知原因和权威位置，不调用写侧或检索。
def memory_projection(context):
    if context.child or context.context_scope in {"task_local", "control_plane", "isolated"}:
        return SectionProjection((), "partial", "not_observed", source="current task memory scope", count_known=False)
    items = []
    if "remember" in context.snapshot.available_tool_names:
        items.append(map_item("formal", "正式长期记忆", fields={"count": None, "next_read": next_read("remember", {"action": "list"})}))
    if "update_persona" in context.snapshot.available_tool_names:
        items.extend(map_item(target, name, fields={"count": None, "next_read": next_read("update_persona", {"target": target, "action": "list"})})
                     for target, name in (("user", "用户画像"), ("agents", "工作约定"), ("soul", "人格语气")))
    if "session_search" in context.snapshot.available_tool_names:
        items.append(map_item("archive", "历史与归档", fields={"count": None, "next_read": next_read("session_search", {"current_thread": True})}))
    items.extend(_source_rule_metadata(context))
    if context.admin:
        items.extend(map_item(name, name, fields={"count": None, "bound": getattr(context.agent, attr, None) is not None})
                     for name, attr in (("candidates", "memory_candidates"), ("lessons", "memory_lessons"), ("hot", "memory_hot"), ("curator", "memory_curator")))
    return SectionProjection(tuple(items), source="bound owner memory authorities / tool scope",
                             counts={"formal_entries": None}, count_reasons={"formal_entries": "只列组件元信息，不读取记忆正文计数。"},
                             locations=_memory_locations(context), count_labels={"formal_entries": "正式条数"},
                              summary="正式长期记忆存放位置：" + str(context.agent.home_paths.owner_memory_long_term_jsonl) + "；未读取正文计数。" if context.admin else "")


# LLM: 先完整管理员判据后取规范位置；不读取正文，也不调用服务构造器或提升文件权限。
# 函数用途: 给正式记忆和三种人格文件的位置及原读写状态。
def _memory_locations(context):
    if not context.admin:
        return ()
    home = context.agent.home_paths
    return location_items(context, {"formal": home.owner_memory_long_term_jsonl, "user": home.owner_user_md,
                                   "agents": home.owner_agents_md, "soul": home.owner_soul_md})


# LLM: 仅管理员本人读取已绑定F4仓库configuration；不构造、load或读正文，不暴露locator，配置字段不代表正文新鲜。
# 函数用途: 标明可控规则来源的开关和修订，显式管理入口只作用户操作说明。
def _source_rule_metadata(context):
    if not context.admin:
        return ()
    enabled = bool(getattr(context.agent.config, "agents_source_enabled", True))
    fields = {"enabled": enabled, "content_observed": "not_observed",
              "user_control": {"command": "/agents show", "explicit_user_required": True}}
    repository = getattr(context.agent, "agents_source_repository", None)
    if not enabled:
        return (map_item("rules_source", "可控工作规则来源", state="disabled", fields=fields),)
    if repository is None:
        return (map_item("rules_source", "可控工作规则来源", state="not_observed", fields=fields),)
    try:
        snapshot = repository.configuration()
        if getattr(snapshot, "owner", None) != context.agent.home_paths.owner_id:
            raise ValueError("owner mismatch")
        fields.update({key: getattr(snapshot, key, "") for key in ("kind", "revision", "status", "failure_code")})
        return (map_item("rules_source", "可控工作规则来源", fields=fields),)
    except (OSError, RuntimeError, TypeError, AttributeError, ValueError):
        return (map_item("rules_source", "可控工作规则来源", state="unavailable", fields=fields),)
