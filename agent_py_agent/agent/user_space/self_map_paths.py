# LLM: 路径说明投影原布局、A类政策和调用冻结B类边界；不遍历磁盘，也不把目录说明当新授权。
# 模块用途: 登记目录与权限分块，区分模型直写、宿主专用入口与未知观测。
from pathlib import Path

from ..path_access_policy import PathAccessPolicy, host_path_descriptions
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


# LLM: 普通owner只见本人布局；child/隔离仅列本调用工作根，不借共享home披露父库存；裁决复用原冻结路径门。
# 函数用途: 一页列本owner业务目录及项目位置，不把保护规则名当成目录权限。
def directory_projection(context):
    home = context.agent.home_paths
    roots = {"owner_home": home.owner_home_dir, "workspace": context.agent.tools.workspace_root,
             "tasks": home.owner_tasks_dir, "artifacts": home.owner_artifacts_dir, "tmp": home.owner_tmp_dir,
             "owner_skills": home.owner_home_dir / "skills",
             "workspace_skills": Path(context.agent.tools.workspace_root) / ".agents" / "skills"}
    if context.child or context.context_scope in {"isolated", "task_local", "control_plane"}:
        filesystem = context.public_context.get("filesystem", {})
        roots = {"workspace": context.agent.tools.workspace_root,
                 **{"read_root:" + str(index): path for index, path in enumerate(filesystem.get("read_roots", ()))},
                 **{"write_root:" + str(index): path for index, path in enumerate(filesystem.get("write_roots", ()))}}
        return SectionProjection(tuple(_directory_item(key, value, context) for key, value in roots.items()),
                                 source="invocation workspace / frozen filesystem boundary")
    items = [_directory_item(key, value, context) for key, value in roots.items()]
    builtin = _directory_item("builtin_skills", home.shared_builtin_dir, context) if context.admin else map_item(
        "builtin_skills", "内置方法层", state="not_observed", fields={"path": None, "read_access": None, "write_access": None,
        "classification": "admin_location_hidden", "summary": "位置不在当前披露范围", "operation_entry": next_read("self_map", {"section": "skills"})})
    items.append(builtin)
    return SectionProjection(tuple(items), source="MyAgentHomePaths / PathAccessPolicy / frozen write context",
                             locations=location_items(context, {"owner_home": home.owner_home_dir, "workspace": context.agent.tools.workspace_root}))


# LLM: 路径声明来自原领域/布局；先检查完整管理员判据，再复用原路径及冻结读写门，不探测磁盘或增加权限。
# 函数用途: 提供简短位置卡；位置可见不代表文件工具有权读写。
def location_items(context, paths):
    if not context.admin:
        return ()
    keys = {"id", "path", "classification", "read_access", "write_access", "denial_code", "operation_entry"}
    return tuple({key: value for key, value in _directory_item(identity, path, context).items() if key in keys}
                 for identity, path in paths.items())


# LLM: 路径仅做绝对位置展开；原政策拒绝仍明确拒绝，政策放行但无调用冻结范围时仅标未知，不据此宣称实际可读写。
# 函数用途: 分开提供目录标签、完整绝对路径和当前访问裁决，不据显示内容增加授权。
def _directory_item(key, path, context):
    path = Path(path).expanduser().absolute()
    policy = _policy(context)
    read = context.invocation.workspace_read_context.check(path) if context.invocation.workspace_read_context else policy.check(path)
    write = context.invocation.workspace_write_context.check(path) if context.invocation.workspace_write_context else policy.check_write(path)
    read_access = read.allowed if context.invocation.workspace_read_context is not None or not read.allowed else None
    write_access = write.allowed if context.invocation.workspace_write_context is not None or not write.allowed else None
    access_state = "known" if read_access is not None and write_access is not None else "not_observed"
    physical = policy.check_write(path)
    classification = "credential" if physical.code == "PATH_HOST_CREDENTIAL_BLOCKED" else "host_managed" if physical.code in {
        "PATH_HOST_MANAGED_STORE_BLOCKED", "PATH_HOST_CONFIG_WRITE_BLOCKED", "PATH_HOST_STATE_WRITE_BLOCKED"} else "direct"
    return map_item(key, key, fields={"directory_label": key, "path": str(path), "classification": classification,
                    "summary": "目录访问：读" + ("未观测" if read_access is None else "允许" if read_access else "拒绝")
                               + "、写" + ("未观测" if write_access is None else "允许" if write_access else "拒绝"),
                    "read_access": read_access, "write_access": write_access, "access_state": access_state,
                    "denial_code": write.code or read.code,
                    "operation_entry": next_read("self_map", {"section": "permissions"}),
                    "write_subject_to_frozen_boundary": True})


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


# LLM: 本人审批/身份/读写网络声明沿main公开self_context，不以config猜审批；声明不等于OS沙箱或真实网络验证。
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
