# LLM: 安装事实由本owner原store读取；权限先于构建/订单读取，公开白名单不带settings或旧授权正文。
# 模块用途: 分开能力包安装/构建/普通方法及插件库存，提供权威位置，不复制授权状态。
from .capability.learnpack_service import open_orders
from .capability.learnpack_store import LearnpackStore
from .capability.package_build import KIND_CAPABILITY_PACK
from .capability.package_snapshot import package_read_parameters
from .plugin_install_store import PluginInstallStore
from .tooling.self_map_registry import SectionDescriptor, SectionProjection, map_item, next_read
from .tooling.self_map_tools import public_catalog_specs
from .user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from .user_space.self_map_paths import location_items


# LLM: 插件/包登记不同静态用途，schema同源生成，避免把普通Skill或构建记录误认安装库存。
# 函数用途: 接入能力包和插件分块。
def register_install_sections(registry):
    registry.register(SectionDescriptor("packages", "能力包", 40, package_projection, "能力包安装、启用、授权与构建库存"))
    registry.register(SectionDescriptor("plugins", "插件", 50, plugin_projection, "插件安装和启用数量及贡献"))


# LLM: 必须先通过完整本机身份；home.owner_id是规范scope ID，不是OwnerIdentity原始ID；复用本机身份工厂并核对路径/scope。
# 函数用途: 从当前可信身份读取原安装表，不初始化目录或迁移。
def _installed(context):
    if not context.admin:
        return ()
    home = context.agent.home_paths
    owner = resolve_owner_home(home.root, OwnerIdentity.local_main())
    if owner.home_dir != home.owner_home_dir or owner.owner_id != home.owner_id:
        raise ValueError("owner binding mismatch")
    return PluginInstallStore(owner).snapshot()


# LLM: 快照授权、原安装、原有界构建各自计数；构建不算安装，普通方法不算内容包，非管理员不读库存。
# 函数用途: 列包及数量摘要，管理员首页可答安装/启用/构建区别和存放位置。
def package_projection(context):
    snapshot = getattr(context.agent, "_current_skill_snapshot", None)
    if snapshot is None and not context.admin:
        return SectionProjection((), "partial", "not_observed", source="SkillSnapshot.packages", count_known=False)
    packages = tuple(getattr(snapshot, "packages", ()) or ())
    readable = {package.package_id: package for package in packages}
    items = {"active:" + package.package_id: _active_package(package) for package in packages}
    if not context.admin:
        return SectionProjection(tuple(items.values()), source="SkillSnapshot.packages", counts={"authorized": len(packages)})
    entries = _installed(context)
    installed = [entry for entry in entries if entry.manifest.is_content_only]
    for entry in installed:
        items.pop("active:" + entry.manifest.plugin_id, None)
    items.update({"installed:" + entry.manifest.plugin_id: _package_install(entry, readable) for entry in installed})
    store = LearnpackStore(context.agent.home_paths.owner_home_dir)
    items.update({"order:" + order.order_id: map_item("order:" + order.order_id, order.package_id, state="pending_confirmation",
                  fields={"inventory_kind": "order", "version": order.version, "sha256": order.sha256, "order_id": order.order_id})
                  for order in open_orders(store, entries) if order.kind == KIND_CAPABILITY_PACK})
    builds = tuple(record for record in store.builds_for(None) if record.kind == KIND_CAPABILITY_PACK)
    items.update({"build:" + record.sha256: map_item("build:" + record.sha256, record.package_id, state="self_built",
                  fields={"inventory_kind": "build", "version": record.version, "sha256": record.sha256, "built_at": record.built_at})
                  for record in builds})
    counts = _package_counts(snapshot, installed, builds)
    reasons = {key: "本轮方法快照未绑定或来源不完整。" for key, value in counts.items() if value is None}
    reasons["builds"] = "原构建扫描有界，只能确认observed_builds，不能确认全部构建数量。"
    return SectionProjection(tuple(items.values()), "partial", errors=("SELF_MAP_BUILD_SCAN_BOUNDED", "SELF_MAP_ORDER_DECODE_MAY_SKIP"),
                             source="PluginInstallStore.snapshot / open_orders / builds_for", counts=counts, count_reasons=reasons,
                             locations=_install_locations(context, store),
                             count_reason="构建扫描有界且部分订单可能跳过，不能确认合计目录条目数；安装/授权分项另列。")


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
