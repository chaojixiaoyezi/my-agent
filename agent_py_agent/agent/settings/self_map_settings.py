# LLM: 模型沿原owner公开投影，配置沿参数登记，压缩沿原四层解析器；不输出请求地址、凭据或私有复合值。
# 模块用途: 登记模型库存和有效配置元数据，普通用户只开放原 decision-only 面。
from dataclasses import replace

from ..tooling.self_map_registry import SectionDescriptor, SectionProjection, map_item, next_read
from ..user_space.self_map_paths import location_items
from .model_profiles import execute_model_profile_operation
from .parameter_registry import SOURCE_AGENT, listed_parameters
from .shared_model_catalog import shared_profile_key


# LLM: 设置领域统一登记固定用途，压缩专项不进入原总览，schema不维护第二份编号清单。
# 函数用途: 接入模型、配置和按需压缩设置分块。
def register_setting_sections(registry):
    registry.register(SectionDescriptor("models", "模型", 80, model_projection, "当前模型与可用模型档案"))
    registry.register(SectionDescriptor("config", "配置", 90, config_projection, "配置文件位置、有效参数与设置入口"))
    registry.register(SectionDescriptor("settings", "压缩与会话设置", 140, settings_projection,
                                        "当前多级压缩比例、闲置压缩及配置工具导航", list_mode=False, include_in_overview=False))


# LLM: 受限范围先拒绝父策略库，错线程不回退默认；原配置工具仍走mutating中央门，目录只给schema导航和公开读参数，不绕过effect门。
# 函数用途: 只读显示当前owner/线程的四层比例、强制开关及闲置压缩设置。
def settings_projection(context):
    from ..conversation.compact_threshold_policy import resolve_thread_compact_policy
    from ..tooling.self_map_remote_peers import restricted_owner_scope
    from .config_layers import compact_idle_parameters

    if restricted_owner_scope(context):
        return SectionProjection((), source="restricted child/isolated scope")
    agent = context.agent
    thread, error = agent.conversation_store.threads.load_report(context.thread_id) if context.thread_id else (None, None)
    if context.thread_id and (error or thread is None or thread.owner_id != agent.home_paths.owner_id
                              or thread.owner_home != str(agent.home_paths.owner_home_dir)):
        return SectionProjection((), "partial", "unavailable", ("SELF_MAP_THREAD_UNAVAILABLE",), count_known=False)
    policy = resolve_thread_compact_policy(agent, thread, use_frozen=False)
    threshold, seconds = compact_idle_parameters(agent.config)
    states = {row["name"]: row["state"] for row in context.public_context.get("tool_states", ())}
    fields = {"trigger_percent": policy["effective_trigger_percent"], "source_layer": policy["effective_source"],
              "admin_force_enabled": policy["force_locked"], "idle_compact_enabled": bool(threshold and seconds),
              "idle_trigger_percent": threshold, "idle_seconds": seconds,
              "thread_override_percent": policy["thread_override_percent"], "user_default_percent": policy["user_default_percent"],
              "observation_point": "current_resolved_policy", "status_command": "/compact status",
              "configuration_tool": "compact_policy", "configuration_tool_state": states.get("compact_policy", "unavailable"),
              "next_read": next_read("tool_search", {"query": "compact_policy", "limit": 1})}
    if context.admin:
        fields["admin_default_percent"] = policy["admin_default_percent"]
    items = [map_item("compact_policy", "当前压缩策略", fields=fields)]
    if "user_config" in context.snapshot.available_tool_names:
        arguments = {"action": "view", "key": "memory_compact_trigger_percent"} if context.admin else {"action": "decision_read"}
        items.append(map_item("user_config", "配置入口", state=states.get("user_config", "unavailable"),
                              fields={"scope": "admin" if context.admin else "decision-only", "read_parameters": arguments,
                                      "next_read": next_read("tool_search", {"query": "user_config", "limit": 1})}))
    return SectionProjection(tuple(items), source="resolve_thread_compact_policy / compact_idle_parameters")


# LLM: 沿原鉴权投影读取，子范围不读父库存；完整性只看结构化库存状态，正式数量按权威来源身份去重。
# 函数用途: 保留全部可选行及别名，分清配置数量、来源缺口和选中不可用警告。
def model_projection(context):
    if context.child or context.context_scope in {"isolated", "task_local", "control_plane"}:
        return _model_counts(SectionProjection((_current_model(context),), source="resolved agent config"), inventory_known=False)
    if not all(getattr(context.agent.home_paths, key, "") for key in ("owner_provider", "owner_kind", "owner_id")):
        return _model_counts(SectionProjection((_current_model(context),), "partial", "unavailable", source="owner model catalog", count_known=False))
    view = execute_model_profile_operation(context.agent, "list", {}, thread_id="")
    rows = view.get("profiles", ())
    if not context.admin:
        rows = [row for row in rows if row.get("available_for")]
    credentials = {row["id"]: bool(row.get("has_key") or row.get("signed_in")) for row in view.get("providers", ())}
    items = [_model_item(row, view.get("default_selected", ""), credentials) for row in rows]
    items.append(_current_model(context))
    shared_unavailable = view["inventory_status"]["shared"] != "complete"
    return _model_counts(SectionProjection(tuple(items), "partial" if shared_unavailable else "complete",
                             errors=("SELF_MAP_SHARED_MODELS_UNAVAILABLE",) if shared_unavailable else (),
                             source="read_model_profiles / original model selection projection"),
                         profile_count=_profile_count(context, rows))


# LLM: 共享ID沿原shared_profile_key；管理员自己的共享行与私有行同源去重，普通owner的私有/共享身份不混并。
# 函数用途: 计已准入的正式档案，不再读取或输出管理员模型目录路径。
def _profile_count(context, rows):
    return len({("owner" if context.admin or not shared_profile_key(row["id"]) else "shared",
                 shared_profile_key(row["id"]) or row["id"])
                for row in rows if row["id"] != "default"})

# LLM: 正式数量由已准入行的权威身份聚合，缺来源保留null；items总数仍是展示行数，不替代正式档案数。
# 函数用途: 为列表和总览提供同源档案/别名计数，明确别名和端点未验证的边界。
def _model_counts(projection, inventory_known=True, profile_count=0):
    known = inventory_known and projection.count_known and projection.completeness == "complete"
    profiles = profile_count
    aliases = sum(row["entry_type"] == "alias" for row in projection.items)
    summary = "正式档案" + (str(profiles) if known else "未统计") + f"；别名{aliases}（不计入档案）。库存声明不代表模型端点已验证。"
    return replace(projection, counts={"profiles": profiles if known else None, "aliases": aliases},
                   count_labels={"profiles": "正式档案", "aliases": "别名"}, summary=summary,
                   count_reason=projection.count_reason or ("模型来源未查全。" if not projection.count_known or projection.completeness != "complete" else ""),
                   count_reasons={"profiles": "档案库存未完整观测。"} if not known else {})


# LLM: default保留ID由原模型目录合同定义；只标其alias类型，不改选择、授权或凭据白名单。
# 函数用途: 给正式档案和默认引用明确类型，防止同名模型或默认别名混入配置数量。
def _model_item(row, default_selected, credentials=None):
    fields = {key: row.get(key) for key in ("provider_id", "provider_name", "capability", "enabled", "available_for", "model_context_window_tokens", "input_modalities")}
    presence = True if row.get("has_key") else (credentials or {}).get(row.get("provider_id"))
    fields.update(has_credential=presence, has_api_key=bool(row.get("has_key")), new_thread_default=row["id"] == default_selected,
                  entry_type="alias" if row["id"] == "default" else "profile",
                  next_read=next_read("manage_models", {"action": "list"}))
    return map_item("profile:" + row["id"], row.get("model_name") or row["id"],
                    state="available" if row.get("available_for") else "unavailable", fields=fields)


# LLM: 当前模型只反映原冻结config；明确为alias但保留原ID/kind，不据此反推正式库存或父模型。
# 函数用途: 显示实际解析模型，同时说明这不是新增加的一份模型配置。
def _current_model(context):
    config = context.agent.config
    return map_item("current", getattr(config, "model_name", "") or "当前模型", fields={"kind": "resolved_current", "entry_type": "alias",
                    "capability": "agentic", "input_modalities": list(getattr(config, "model_input_modalities", ()) or ()),
                    "endpoint_observed": "not_verified", "model_context_window_tokens": getattr(config, "model_context_window_tokens", None)})


# LLM: 非管理员仍decision-only；摘要与位置卡共用绝对路径投影，保留原owner硬墙裁决，不按owner目录猜配置位置。
# 函数用途: 同源显示全局配置目录和生效文件的完整路径、原访问限制及设置入口。
def config_projection(context):
    if not context.admin:
        items = (map_item("decision", "自己的决策设置", fields={"next_read": next_read("user_config", {"action": "decision_read"})}),) if "user_config" in context.snapshot.available_tool_names and not context.child else ()
        return SectionProjection(items, source="user_config decision-only scope")
    items = tuple(_parameter_item(spec, context.agent.config) for spec in listed_parameters().values())
    paths = {"config": context.agent.home_paths.config_dir}
    if getattr(context.agent.config, "config_path", ""):
        paths["effective_file"] = context.agent.config.config_path
    locations = location_items(context, paths)
    paths = {row["id"]: row["path"] for row in locations}
    summary = "全局配置目录：" + paths["config"] + "；当前生效配置文件：" + (paths.get("effective_file") or "未提供位置") + "。"
    return SectionProjection(items, source="parameter_registry / effective AgentConfig", locations=locations, summary=summary)


# LLM: 只展示已绑定主配置的公开标量；未读来源的凭据presence留null，不把未知说成未配置。
# 函数用途: 投影一个参数的登记事实和安全运行值。
def _parameter_item(spec, config):
    observed = spec.source == SOURCE_AGENT and hasattr(config, spec.key)
    value = getattr(config, spec.key, None) if observed else None
    public_value = value if type(value) in (int, float, bool) and not spec.masked else None
    return map_item(spec.key, spec.key, fields={"summary": spec.description, "source_layer": spec.source,
                    "value": public_value, "value_observed": public_value is not None,
                    "credential_configured": bool(value) if spec.masked and observed else None,
                    "masked": spec.masked, "writable": spec.writable, "effect": spec.effect,
                    "next_read": next_read("user_config", {"action": "view", "key": spec.key})})
