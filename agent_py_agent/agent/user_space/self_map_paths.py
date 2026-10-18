# LLM: 路径说明投影原布局与冻结边界；内置文件读门和插件cwd读范围分别标注，不遍历磁盘或产生新授权。
# 模块用途: 登记目录与权限分块，区分模型直写、宿主专用入口与未知观测。
from pathlib import Path

from ..path_access_policy import PathAccessDecision, PathAccessPolicy, host_path_descriptions
from ..tooling.self_map_registry import (
    SELF_MAP_UNKNOWN_OBSERVATION,
    SectionDescriptor,
    SectionProjection,
    map_item,
    next_read,
)


# LLM: 两个读者共享原政策；固定用途在原领域登记，具体目录与匹配规则分开，不能让业务路径被规则名误分类。
# 函数用途: 将目录和权限贡献登记到当前 Agent。
def register_path_sections(registry):
    registry.register(SectionDescriptor("directories", "目录", 10, directory_projection, "工作目录位置及当前读写标注"))
    registry.register(SectionDescriptor("permissions", "权限", 110, permission_projection, "当前身份、路径策略与未观测边界", list_mode=False))


# LLM: 优先复用执行器本次invocation已冻结策略；无调用上下文时才读取registry，不重新读取环境或授予权限。
# 函数用途: 让目录、权限摘要和动态边界过滤使用与真实执行门相同的路径政策。
def _policy(context):
    boundary = context.invocation.workspace_write_context or context.invocation.workspace_read_context
    if boundary is not None:
        return boundary.path_policy
    tools = context.agent.tools
    return PathAccessPolicy(mode=tools.path_access_mode,
                            dangerous_roots=tuple(Path(path) for path in tools.path_dangerous_roots),
                            owner_scope_root=Path(tools.owner_scope_root) if tools.owner_scope_root else None,
                            agent_home_root=getattr(context.agent.home_paths, "root", None))


# LLM: 普通owner只见本人布局；workspace优先冻结cwd，Skill位置只从本人已绑定快照的原base_roots读取。
#   child/隔离仅列本调用工作根，不借共享home披露父库存；裁决复用原冻结路径门。
# 函数用途: 一页列本owner业务目录及项目位置，不把保护规则名当成目录权限。
def directory_projection(context):
    home = context.agent.home_paths
    boundary = context.invocation.workspace_read_context or context.invocation.workspace_write_context
    workspace = boundary.cwd if boundary else context.agent.tools.workspace_root
    roots = {"owner_home": home.owner_home_dir, "workspace": workspace,
             "tasks": home.owner_tasks_dir, "artifacts": home.owner_artifacts_dir, "tmp": home.owner_tmp_dir,
             "owner_skills": home.owner_home_dir / "skills"}
    if context.child or context.context_scope in {"isolated", "task_local", "control_plane"}:
        filesystem = context.public_context.get("filesystem", {})
        roots = {"workspace": workspace,
                 **{"read_root:" + str(index): path for index, path in enumerate(filesystem.get("read_roots", ()))},
                 **{"write_root:" + str(index): path for index, path in enumerate(filesystem.get("write_roots", ()))}}
        return SectionProjection(tuple(_directory_item(key, value, context) for key, value in roots.items()),
                                 source="invocation workspace / frozen filesystem boundary")
    items = [_directory_item(key, value, context) for key, value in roots.items()]
    items.append(_workspace_skill_item(context))
    builtin = _directory_item("builtin_skills", home.shared_builtin_dir, context) if context.admin else map_item(
        "builtin_skills", "内置方法层", state="not_observed", fields={"path": None, "read_access": None, "write_access": None,
        "classification": "admin_location_hidden", "summary": "位置不在当前披露范围", "operation_entry": next_read("self_map", {"section": "skills"})})
    items.append(builtin)
    return SectionProjection(tuple(items), source="MyAgentHomePaths / PathAccessPolicy / frozen write context",
                             locations=location_items(context, {"owner_home": home.owner_home_dir, "workspace": workspace}))


# LLM: Skill根只消费本人已绑定快照的workspace_root与服务原声明，不跟随工具cwd猜目录，也不触发发现或reload。
#   多个workspace来源不能被单条旧位置冒称唯一；缺观测只给null，不披露其它owner或父级快照路径。
# 函数用途: 说明当前方法快照的项目来源位置；未绑定或来源不唯一时回到原skills读口。
def _workspace_skill_item(context):
    snapshot = getattr(context.agent, "_current_skill_snapshot", None)
    if snapshot is not None and snapshot.owner_id == context.agent.home_paths.owner_id and snapshot.workspace_root:
        roots = [root.path for root in context.agent.skills_service.base_roots(Path(snapshot.workspace_root)) if root.source == "workspace"]
        if len(roots) == 1 and _policy(context).check(roots[0]).allowed:
            return _directory_item("workspace_skills", roots[0], context)
    return map_item("workspace_skills", "workspace_skills", state="not_observed", fields={
        "path": None, "read_access": None, "write_access": None, "summary": "项目Skill来源位置未观测或不唯一",
        "operation_entry": next_read("self_map", {"section": "skills"})})


# LLM: 路径声明来自原领域/布局；先检查完整管理员判据，再复用原路径及冻结读写门，不探测磁盘或增加权限。
# 函数用途: 提供简短位置卡；位置可见不代表文件工具有权读写。
def location_items(context, paths):
    if not context.admin:
        return ()
    keys = {"id", "path", "classification", "read_access", "write_access", "denial_code", "operation_entry"}
    return tuple({key: value for key, value in _directory_item(identity, path, context).items() if key in keys}
                 for identity, path in paths.items())


# LLM: 内置读门使用执行器冻结的原exact范围/路径政策；插件cwd读取范围另列，不能把它套给所有文件工具。
#   缺builtin冻结字段时政策拒绝仍false、放行只标未知；不读正文，不因位置或写根增加读取授权。
# 函数用途: 分开说明内置文件路径权限与插件工作区范围，保留已知拒绝、未知和原位置披露范围。
def _directory_item(key, path, context):
    path = Path(path).expanduser().absolute()
    policy = _policy(context)
    read, read_observed = _builtin_read_decision(path, context, policy)
    workspace_read = context.invocation.workspace_read_context
    write = context.invocation.workspace_write_context.check(path) if context.invocation.workspace_write_context else policy.check_write(path)
    read_access = read.allowed if read_observed or not read.allowed else None
    write_access = write.allowed if context.invocation.workspace_write_context is not None or not write.allowed else None
    access_state = "known" if read_access is not None and write_access is not None else "not_observed"
    physical = policy.check_write(path)
    classification = "credential" if physical.code == "PATH_HOST_CREDENTIAL_BLOCKED" else "host_managed" if physical.code in {
        "PATH_HOST_MANAGED_STORE_BLOCKED", "PATH_HOST_CONFIG_WRITE_BLOCKED", "PATH_HOST_STATE_WRITE_BLOCKED"} else "direct"
    return map_item(key, key, fields={"directory_label": key, "path": str(path), "classification": classification,
                    "summary": "内置文件路径：读" + ("未观测" if read_access is None else "允许" if read_access else "拒绝")
                               + "、写" + ("未观测" if write_access is None else "允许" if write_access else "拒绝"),
                    "read_access": read_access, "write_access": write_access, "access_state": access_state,
                    "read_access_scope": "builtin_file_tools", "read_denial_code": read.code,
                    "workspace_read_access": workspace_read.check(path).allowed if workspace_read else None,
                    "denial_code": write.code or read.code,
                    "operation_entry": next_read("self_map", {"section": "permissions"}),
                    "write_subject_to_frozen_boundary": True})


# LLM: 只投影执行器冻结的builtin读门；原exact先于owner/path政策，墙外授权还受同次预检根限制。
#   插件上下文仅提供冻结policy与external_policy；缺字段不能退回可变current_run_params或把cwd当全局范围。
# 函数用途: 说明具体路径的内置文件读取裁决及是否有完整观测，不打开文件、不调用业务工具。
def _builtin_read_decision(path, context, policy):
    frozen = context.invocation.execution_permission_context
    if (frozen is None or not (context.invocation.workspace_read_context or context.invocation.workspace_write_context)
            or not {"builtin_read_scope_mode", "builtin_read_roots", "builtin_read_external_roots"} <= frozen.keys()):
        return policy.check(path), False
    mode, roots = frozen["builtin_read_scope_mode"], frozen["builtin_read_roots"]
    if (mode not in {"policy", "exact"} or (mode == "exact" and not isinstance(roots, tuple))
            or (mode == "policy" and roots is not None) or not isinstance(frozen["builtin_read_external_roots"], tuple)):
        return policy.check(path), False
    try:
        target = path.resolve(strict=False)
    except (OSError, RuntimeError):
        return PathAccessDecision(False, "PATH_RESOLUTION_FAILED", "路径解析失败。"), True
    if mode == "exact" and not any(target.is_relative_to(root) for root in roots):
        return PathAccessDecision(False, "PATH_READ_SCOPE_BLOCKED", "目标不在本次精确读取范围内。"), True
    read = context.invocation.workspace_read_context
    external_policy = read.external_policy if read else PathAccessPolicy(
        mode=policy.mode, dangerous_roots=policy.dangerous_roots, agent_home_root=policy.agent_home_root)
    return policy.check_with_external_roots(target, frozen["builtin_read_external_roots"], external_policy=external_policy), True


# LLM: 只消费原调用上下文公共边界，不重新构建账本或推断安装目录；缺观测明确 unknown。
# 函数用途: 列动态写保护与Persona专用门。
def _write_boundaries(context):
    boundary = context.invocation.workspace_write_context
    return [map_item("frozen_write_boundary", "当前任务写边界", state="known" if boundary else "not_observed",
                     fields={"source": "workspace_write_context", "protected": boundary is not None,
                              "write_roots": [str(path) for path in boundary.write_roots if _policy(context).check(path).allowed] if boundary else None,
                              "forbidden_root_count": len(boundary.forbidden_roots) if boundary else None,
                              "locked_file_count": len(boundary.locked_files) if boundary else None}),
            map_item("persona", "人格专用写门", fields={"write_access": False, "soul_requires_approval": True,
                     "operation_entry": next_read("update_persona", {"target": "user", "action": "list"})})]


# LLM: 原政策家族是规则声明，不是具体目录；写保护与专用入口保留原意，不按规则名裁决业务路径。
# 函数用途: 在权限详情列每个托管家族的声明和操作入口，不挤进当前身份卡。
def _policy_item(index, declaration):
    kind = declaration["kind"]
    selector = "/".join(declaration.get("parts", ())) or declaration.get("match", "")
    entry = "config" if kind in {"config", "credential"} else "packages" if kind == "managed" else "work"
    code = {"managed": "PATH_HOST_MANAGED_STORE_BLOCKED", "config": "PATH_HOST_CONFIG_WRITE_BLOCKED",
            "state": "PATH_HOST_STATE_WRITE_BLOCKED", "credential": "PATH_HOST_CREDENTIAL_BLOCKED"}[kind]
    return map_item("policy:" + str(index), selector, fields={"classification": kind, "base": declaration["base"],
                    "declaration": declaration, "read_access": False if kind in {"credential", "managed"} else "subject_to_owner_wall",
                    "write_access": False, "denial_code": code, "operation_entry": next_read("self_map", {"section": entry})})


# LLM: 本人审批/身份/网络沿main公开self_context；filesystem.read_roots明确是插件工作区范围，内置读门另标原模式。
#   不新增展示exact或墙外根路径，声明不等于OS沙箱、文件存在或真实网络验证。
# 函数用途: 显示同次调用的权限来源、精确空根和受保护家族，查询不会增加授权。
def permission_projection(context):
    public = context.public_context
    filesystem = public.get("filesystem", {})
    item = map_item("current", "当前权限", fields={"admin": context.admin, "child": context.child,
        "identity": public.get("identity", {}), "permissions": public.get("permissions", {}),
        "filesystem": filesystem, "network": public.get("network", {}),
        "sandbox_observed": "not_observed", "external_network": "not_verified",
        "unknown_reason": SELF_MAP_UNKNOWN_OBSERVATION,
        "authorized_tool_count": len(context.snapshot.available_tool_names),
        "resource_authorization_required": True})
    items = (item, *_write_boundaries(context),
             *(_policy_item(index, row) for index, row in enumerate(host_path_descriptions())))
    return SectionProjection(items, source="tool_self_context / invocation context / PathAccessPolicy",
        summary="本人审批与文件/网络范围沿当前运行声明；实际沙箱执行和外部网络未验证。")
