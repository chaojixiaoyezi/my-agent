# LLM: 安装及资产摘要沿本owner原store和冻结快照读取；权限先于库存，白名单不带配置、旧授权或Skill正文。
# 模块用途: 分开能力包安装/构建/普通方法及插件库存，提供权威位置，不复制授权状态。
import re
from dataclasses import asdict

from .capability.learnpack_projection import build_metadata_report
from .capability.learnpack_service import open_orders
from .capability.learnpack_store import LearnpackStore
from .capability.package_build import KIND_CAPABILITY_PACK
from .capability.package_snapshot import package_read_parameters
from .plugin_install_store import PluginInstallStore
from .tooling.self_map_registry import SectionDescriptor, SectionProjection, map_item, next_read
from .tooling.self_map_tools import public_catalog_specs
from .user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from .user_space.self_map_paths import location_items


# LLM: 插件/包保留原总览，资产聚合只作按需导航，普通Skill或构建不能误算安装库存。
# 函数用途: 接入能力包、插件及按需资产入口。
def register_install_sections(registry):
    registry.register(SectionDescriptor("packages", "能力包", 40, package_projection, "能力包安装、启用、授权与构建库存",
                                        summary_reader=package_summary, item_reader=package_item))
    registry.register(SectionDescriptor("plugins", "插件", 50, plugin_projection, "插件安装和启用数量及贡献"))
    registry.register(SectionDescriptor("assets", "技能与自动化资产", 160, assets_projection,
                                        "Skill和能力包数量摘要、脚本/工作流/数据源工具入口", include_in_overview=False))


# LLM: 只复用同owner冻结Skill及包summary，不读正文、订单或构建历史；工具状态来自本次公开快照，不执行或展开工具。
# 函数用途: 汇总资产计数及合法导航，未知数量保留null，未注册/禁用入口不冒充可调用。
def assets_projection(context):
    from .capability.self_map_skills import skill_summary

    skills, packages = skill_summary(context), package_summary(context)
    skill_count = skills.summary_count if skills.completeness == "complete" and skills.count_known else None
    package_count = packages.summary_count if packages.completeness == "complete" and packages.count_known else None
    items = [map_item("skills", "普通Skill", state=skills.state, fields={
        "count": skill_count, "counts": {"ordinary_skills": skill_count}, "completeness": skills.completeness,
        "next_read": next_read("self_map", {"section": "skills"})}),
        map_item("packages", "能力包", state=packages.state, fields={
        "count": package_count, "counts": packages.counts or {"authorized": package_count}, "completeness": packages.completeness,
        "next_read": next_read("self_map", {"section": "packages"})})]
    states = {row["name"]: row for row in context.public_context.get("tool_states", ())}
    for name in ("scripts", "workflows", "datasources"):
        state = states.get(name, {}).get("state", "unavailable")
        candidate = next_read(name, {"action": "list"}) if state == "ready" else next_read("tool_search", {"query": name, "limit": 1})
        items.append(map_item(name, name, state=state, fields={"kind": "asset_tool", "count": None,
            "reason": states.get(name, {}).get("reason", "不在本次授权工具面。"), "next_read": candidate}))
    errors = tuple(sorted(set(skills.errors + packages.errors)))
    partial = errors or skills.completeness != "complete" or packages.completeness != "complete"
    return SectionProjection(tuple(items), "partial" if partial else "complete", errors=errors,
                             source="SkillSnapshot / package_summary / frozen tool states", count_known=not partial,
                             counts={"ordinary_skills": skill_count, "authorized_packages": (packages.counts or {}).get("authorized", package_count)})


# LLM: 必须先通过完整本机身份；home.owner_id是规范scope ID，不是OwnerIdentity原始ID；复用本机身份工厂并核对路径/scope。
# 函数用途: 从当前可信身份读取原安装表，不初始化目录或迁移。
def _installed(context):
    if not context.admin:
        return ()
    home = context.agent.home_paths
    owner = resolve_owner_home(home.root, OwnerIdentity.local_main())
    if owner.home_dir != home.owner_home_dir or owner.owner_id != home.owner_id:
        raise ValueError("owner binding mismatch")
    key = ("plugin_install_snapshot", owner.owner_id, str(owner.home_dir))
    if key not in context.memo:
        context.memo[key] = PluginInstallStore(owner).snapshot()
    return context.memo[key]


# LLM: 安装、授权事实只取当前owner绑定；调用内共享安装观察，跨调用无缓存，不读订单或构建历史。
# 函数用途: 组合当前包的安装和授权显示项，供总览/列表/准确详情复用。
def _package_base(context):
    snapshot = getattr(context.agent, "_current_skill_snapshot", None)
    if snapshot is not None and snapshot.owner_id != context.agent.home_paths.owner_id:
        raise ValueError("snapshot owner mismatch")
    packages = tuple(getattr(snapshot, "packages", ()) or ())
    readable = {package.package_id: package for package in packages}
    items = {"active:" + package.package_id: _active_package(package) for package in packages}
    entries = _installed(context)
    installed = [entry for entry in entries if entry.manifest.is_content_only]
    for entry in installed:
        items.pop("active:" + entry.manifest.plugin_id, None)
        items["installed:" + entry.manifest.plugin_id] = _package_install(entry, readable)
    return snapshot, items, entries, installed


# LLM: 构建/订单未枚举必须unknown；只读取当前授权安装元信息，不访问历史或ZIP。
# 函数用途: 提供轻量能力包总览，不能据此声称完整库存或已验证构建。
def package_summary(context):
    snapshot, items, _entries, installed = _package_base(context)
    if not context.admin:
        return SectionProjection(tuple(items.values()), "partial" if snapshot is None else "complete",
                                 state="not_observed" if snapshot is None else "known", source="SkillSnapshot.packages",
                                 count_known=snapshot is not None, summary_count=len(items) if snapshot is not None else None)
    counts = _package_counts(snapshot, installed, ()) | {"observed_builds": None}
    return SectionProjection((), "partial", source="PluginInstallStore.snapshot / SkillSnapshot.packages",
                             count_known=False, counts=counts)


# LLM: 订单字段只用于显示，确认安装仍独立读取并执行前置检查。
# 函数用途: 生成一张待确认订单的公共目录项。
def _order_item(order):
    return map_item('order:' + order.order_id, order.package_id, state='pending_confirmation', fields={
        'inventory_kind': 'order', 'version': order.version, 'sha256': order.sha256, 'order_id': order.order_id})


# LLM: 接收的字段已白名单准入；verified仅来自准确详情的原store.build，不由元信息列表推断。
# 函数用途: 将构建元信息转成可导航目录项，不给安装许可。
def _build_item(record):
    fields = {key: record[key] for key in ('version', 'sha256', 'built_at', 'integrity_state')}
    return map_item('build:' + record['sha256'], record['package_id'], state='self_built',
                    fields={'inventory_kind': 'build', **fields})


# LLM: 管理员资格先于构建/订单读取；准确构建仅校验该ZIP，不回退全量扫描，损坏保留不可用事实。
# 函数用途: 按准确item读取包详情，构建走原完整性校验入口。
def package_item(context, identity):
    if identity.startswith('build:'):
        if not context.admin or re.fullmatch('[0-9a-f]{64}', identity[6:]) is None:
            return None
        record = LearnpackStore(context.agent.home_paths.owner_home_dir).build(identity[6:])
        if record is None:
            row = map_item(identity, identity, state='unavailable', fields={'integrity_state': 'unavailable'})
        elif record.kind != KIND_CAPABILITY_PACK:
            return None
        else:
            row = _build_item(asdict(record) | {'integrity_state': 'verified'})
    else:
        _snapshot, items, entries, _installed_rows = _package_base(context)
        row = items.get(identity)
        if row is None and identity.startswith('order:') and context.admin:
            store = LearnpackStore(context.agent.home_paths.owner_home_dir)
            order = next((order for order in open_orders(store, entries)
                          if order.kind == KIND_CAPABILITY_PACK and 'order:' + order.order_id == identity), None)
            row = _order_item(order) if order else None
        if row is None:
            return None
    return SectionProjection((row,), 'partial', source='exact package item', count_known=False)


# LLM: 快照授权、原安装、原有界构建各自计数；构建不算安装，普通方法不算内容包，非管理员不读库存。
# 函数用途: 列包及数量摘要，管理员首页可答安装/启用/构建区别和存放位置。
def package_projection(context):
    snapshot, items, entries, installed = _package_base(context)
    if snapshot is None and not context.admin:
        return SectionProjection((), "partial", "not_observed", source="SkillSnapshot.packages", count_known=False)
    if not context.admin:
        return SectionProjection(tuple(items.values()), source="SkillSnapshot.packages", counts={"authorized": len(items)})
    store = LearnpackStore(context.agent.home_paths.owner_home_dir)
    items.update({'order:' + order.order_id: _order_item(order)
                  for order in open_orders(store, entries) if order.kind == KIND_CAPABILITY_PACK})
    report = build_metadata_report(store)
    builds = tuple(record for record in report.rows if record['kind'] == KIND_CAPABILITY_PACK)
    items.update({'build:' + record['sha256']: _build_item(record) for record in builds})
    counts = _package_counts(snapshot, installed, builds)
    if report.completeness == 'complete':
        counts['builds'] = len(builds)
    reasons = {key: '来源未绑定或扫描不完整，不能确认数量。' for key, value in counts.items() if value is None}
    return SectionProjection(tuple(items.values()), 'partial', errors=(*report.errors, 'SELF_MAP_ORDER_DECODE_MAY_SKIP'),
                             source='PluginInstallStore.snapshot / open_orders / build_metadata_report', counts=counts, count_reasons=reasons,
                             locations=_install_locations(context, store), count_reason='订单解码可能跳过，合计目录数量未知；分项另列。')


# LLM: 业务分项只来自当前已读安装表和同一冻结快照；builds_for有界，观测到的记录数不能冒充全量。
# 函数用途: 分开安装、启用、授权、普通方法与已观察构建。
def _package_counts(snapshot, installed, builds):
    ordinary = len(snapshot.enabled_entries()) if snapshot is not None and not snapshot.errors else None
    return {"installed": len(installed), "enabled": sum(entry.enabled for entry in installed),
            "authorized": len(snapshot.packages) if snapshot is not None else None,
            "ordinary_skills": ordinary, "builds": None, "observed_builds": len(builds)}


# LLM: 位置来自规范home和已构造原store.root；调用方已管理员准入，路径层读写门仍复用原裁决。
# 函数用途: 给安装库和构建/订单库的位置，不扫描磁盘或授权模型直读。
def _install_locations(context, build_store=None):
    paths = {"installed": context.agent.home_paths.owner_plugins_dir}
    if build_store is not None:
        paths["builds"] = build_store.root
    return location_items(context, paths)


# LLM: 原包身份和激活代参数由package_snapshot生成；在途快照不证明当前仍可读，读取时必须走原激活代复核。
# 函数用途: 投影当前可读包及其原正文入口。
def _active_package(package):
    return map_item("active:" + package.package_id, package.package_id, state="authorized", fields={
        "inventory_kind": "authorized", "version": package.version, "summary": package.summary, "readable": None,
        "authorization_rechecked_on_read": True,
        "next_read": next_read("skill_search", package_read_parameters(package.to_ref()))})


# LLM: enabled只表示原安装激活事实，readable仍同代授权；库存不借同名包生成入口。
# 函数用途: 投影明确的安装记录，与构建记录区分。
def _package_install(entry, readable):
    package = readable.get(entry.manifest.plugin_id)
    authorized = bool(entry.enabled and package and package.package_sha256 == entry.package_sha256
                      and package.activation_id == entry.activation_id)
    fields = {"inventory_kind": "installed", "version": entry.manifest.version, "summary": entry.manifest.summary, "installed": True,
              "enabled": entry.enabled, "readable": authorized, "sha256": entry.package_sha256}
    if authorized:
        fields["next_read"] = next_read("skill_search", package_read_parameters(package.to_ref()))
    return map_item("installed:" + entry.manifest.plugin_id, entry.manifest.plugin_id,
                    state="enabled" if entry.enabled else "disabled", fields=fields)


# LLM: 普通身份仅当前授权贡献；管理员只本owner安装表，安装/启用数不以声明工具数代替。
# 函数用途: 列插件贡献与安装/启用数量，并给管理员原安装库位置。
def plugin_projection(context):
    specs = public_catalog_specs(context)
    authorized = {spec.hints.provider_id[7:] for spec in specs if spec.hints.provider_id.startswith("plugin:")}
    if not context.admin:
        items = tuple(map_item(name, name, fields={"authorized_tools": [spec.name for spec in specs if spec.hints.provider_id == "plugin:" + name],
                      "installation_state": "not_observed", "panels": None, "events": None}) for name in sorted(authorized))
        return SectionProjection(items, source="ToolRuntimeSnapshot plugin contributions")
    entries = tuple(entry for entry in _installed(context) if not entry.manifest.is_content_only)
    return SectionProjection(tuple(_plugin_install(entry, specs) for entry in entries),
                             source="PluginInstallStore.snapshot / validated manifest",
                             counts={"installed": len(entries), "enabled": sum(entry.enabled for entry in entries)},
                             locations=_install_locations(context))


# LLM: 声明不代表已执行或已挂载；事件/面板缺运行观测保留unknown，不返回私有配置和权限对象。
# 函数用途: 将安装清单投影为安全贡献元数据。
def _plugin_install(entry, specs):
    manifest = entry.manifest
    visible = [spec.name for spec in specs if spec.hints.provider_id == "plugin:" + manifest.plugin_id]
    return map_item(manifest.plugin_id, manifest.plugin_id, state="enabled" if entry.enabled else "disabled", fields={
        "version": manifest.version, "summary": manifest.summary, "installed": True, "enabled": entry.enabled,
        "declared_tools": [tool.name for tool in manifest.tools], "authorized_tools": visible,
        "panels": [{"id": panel.id, "declared": True, "mounted": "not_observed"} for panel in manifest.panels],
        "events": [{"name": event.type, "declared": True, "executed": "not_observed"} for event in manifest.events]})
