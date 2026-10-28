# LLM: 内容发现与读取只消费原 owner 安装表和摘要地址；不扫描目录、启进程、解包或另建权限账。
# 模块用途: 把已启用内容包装成当前轮快照，并在每次私有成员读取前后复核同一安装代次。
from __future__ import annotations

from functools import partial
from pathlib import Path

from ..plugin_install_store import PluginInstallStore
from ..plugin_installation import PluginInstallation, PluginInstallationError
from ..user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from .package_snapshot import CapabilityPackageSnapshot, package_assets


# LLM: 输入是宿主可信owner_home，仅接受原布局；不扫描用户目录或将正文中的owner编号当权限。
# 函数用途: 从规范Owner路径恢复原解析器对象，不创建目录。
def _owner_from_home(owner_home):
    home = Path(owner_home).absolute()
    parts = home.parts
    if len(parts) >= 3 and parts[-3:-1] == ('owners', 'local'):
        identity = OwnerIdentity.local_main() if home.name == 'main' else OwnerIdentity.local_user(home.name)
        root = home.parents[2]
    elif (len(parts) >= 5 and parts[-5:-3] == ('owners', 'providers')
          and parts[-2] in ('users', 'groups')):
        identity = (OwnerIdentity.provider_user(parts[-3], home.name) if parts[-2] == 'users'
                    else OwnerIdentity.provider_group(parts[-3], home.name))
        root = home.parents[4]
    else:
        raise ValueError('需要规范Owner目录')
    owner = resolve_owner_home(root, identity)
    if owner.home_dir != home.resolve():
        raise ValueError('Owner目录不匹配')
    return owner


# LLM: 总表直接读唯一安装记录，不建第二份注册表；私有只读本人，公共只读main已发布scope，不暴露设置/凭据。
# 函数用途: 列出已安装内容包，自动反映停用和卸载。
def list_installed_capability_packages(owner_home, *, include_disabled=True):
    owner = _owner_from_home(owner_home)
    own = PluginInstallStore(owner).snapshot()
    public = (() if owner.identity == OwnerIdentity.local_main() else
              PluginInstallStore(resolve_owner_home(owner.root, OwnerIdentity.local_main())).snapshot())
    rows = []
    for entry in (*own, *(item for item in public if item.installation_scope == 'public')):
        if not entry.manifest.is_content_only or not include_disabled and not entry.enabled:
            continue
        manifest = entry.manifest
        assets = package_assets(manifest.files)
        identifier = ('public/' if entry.installation_scope == 'public' else '') + manifest.plugin_id
        rows.append({'package_id': identifier, 'name': identifier, 'version': manifest.version, 'summary': manifest.summary,
            'enabled': entry.enabled, 'installation_scope': entry.installation_scope,
            'application_scope': manifest.capability.to_payload().get('application_scope', {}),
            'assets': assets, 'asset_counts': {key: len(value) for key, value in assets.items()},
            'digest': entry.package_sha256, 'revision': entry.revision})
    return sorted(rows, key=lambda row: row['package_id'])


# LLM: 排序只是软偏好，不启用/授权包；显式ID优先，来源限定ID精确匹配，稳定保留同分原顺序。
# 函数用途: 根据工作台、领域、项目标签和显式偏好提升已启用包。
def rank_packages_for_context(packages, *, workbench_id='', domain='', workspace_tags=(), preferred_pack_ids=()):
    tags, preferred = set(workspace_tags), set(preferred_pack_ids)
    # LLM: 权重只取公开匹配元数据，禁用包不提升，不读正文或改变有效授权。
    # 函数用途: 给单包计算稳定排序分。
    def score(package):
        get = package.get if isinstance(package, dict) else lambda key, default=None: getattr(package, key, default)
        if get('enabled', True) is False:
            return -1
        scope = get('application_scope', {}) or {}
        identifier = get('package_id', '')
        return (100 * (identifier in preferred or 'capability:' + identifier in preferred)
                + 60 * bool(workbench_id and workbench_id in scope.get('workbenches', ()))
                + 40 * bool(domain and domain in scope.get('domains', ()))
                + 20 * bool(tags.intersection(scope.get('workspace_tags', ()))))
    return sorted(packages, key=score, reverse=True)


# LLM: owner 必须来自原宿主解析器；这里只读安装声明，不读取全部文件正文，错误由 SkillsService 单独记录。
# 函数用途: 列出本 owner 当前已启用的独立能力包，旧 v3 随包 Skill 仍走原 plugin_roots。
def enabled_capability_packages(owner) -> tuple[CapabilityPackageSnapshot, ...]:
    if owner is None or owner.identity != OwnerIdentity.local_main():
        return ()
    store = PluginInstallStore(owner)
    return tuple(_package_snapshot(store, entry) for entry in store.snapshot()
                 if entry.enabled and entry.manifest.is_content_only and entry.installation_scope == "private")


# LLM: 公共发布源只由规范 main owner 推出；筛选原安装表的 public 内容记录，不投影设置/凭据或私有安装，不给执行用户 main 身份。
# 函数用途: 给当前用户提供管理员已启用的公共能力目录，包字节只在明确读取时取出。
def enabled_public_capability_packages(owner) -> tuple[CapabilityPackageSnapshot, ...]:
    if owner is None:
        return ()
    store = PluginInstallStore(resolve_owner_home(owner.root, OwnerIdentity.local_main()))
    return tuple(_package_snapshot(store, entry) for entry in store.snapshot()
                 if entry.enabled and entry.manifest.is_content_only and entry.installation_scope == "public")


# LLM: 私有内容仍受插件总闸；关闭插件时仅当前可信快照中的 public 内容可用，不扩展任何插件工具执行权。
# 函数用途: 让主选包与子代理入口准备统一识别公共内容能力，避免为用公共包打开插件权限。
def package_usage_enabled(agent: object) -> bool:
    if not getattr(getattr(agent, "config", None), "enable_tools", False):
        return False
    if getattr(agent.config, "enable_plugins", False):
        return True
    return any(package.installation_scope == "public" for package in agent.current_skill_snapshot().packages)


# LLM: 闭包固定完整安装而非仅包名，避免卸载重装后旧快照借新代复活；不保存第二份持久安装记录。
# 函数用途: 将一条有效安装投影为只含包摘要和声明成员的冻结发现对象。
def _package_snapshot(store: PluginInstallStore, installation: PluginInstallation) -> CapabilityPackageSnapshot:
    manifest = installation.manifest
    capability = manifest.capability
    return CapabilityPackageSnapshot(
        package_id=f"public/{manifest.plugin_id}" if installation.installation_scope == "public" else manifest.plugin_id,
        installation_scope=installation.installation_scope,
        version=manifest.version, summary=manifest.summary,
        description=capability.description, keywords=capability.keywords,
        entry_document=capability.entry_document, package_sha256=installation.package_sha256,
        activation_id=installation.activation_id, members=manifest.files,
        application_scope=capability.to_payload().get('application_scope', {}),
        reader=partial(read_capability_member, store, installation),
    )


# LLM: 同 owner Store 的原准入事实先读后读；归档字节已经完整复验，返回前完整 installation 必须仍相等。
#   reason 区分读前已失效（activation_unavailable：停用、换代或卸载早于本次读取）与读中变化
#   （activation_changed_during_read），只供诊断；两者都拒绝交付，调用方对外错误码不因此改变。
# 函数用途: 有界读取一个包内成员；撤销、换代、包篡改和未声明路径都失败，不启动或执行资源。
def read_capability_member(store: PluginInstallStore, expected: PluginInstallation, member_path: str) -> bytes:
    from ..plugin_package import PackageReadLimits, PluginPackageSnapshot, read_plugin_member

    before = store.require_activation(expected.manifest.plugin_id, expected.activation_id)
    if before != expected or not before.manifest.is_content_only:
        raise PluginInstallationError("activation_unavailable", "能力包原安装代次已变化。")
    package = PluginPackageSnapshot(before.manifest, store.package_bytes(before))
    content = read_plugin_member(package, member_path, max_bytes=PackageReadLimits().member_bytes)
    try:
        after = store.require_activation(expected.manifest.plugin_id, expected.activation_id)
    except PluginInstallationError as exc:
        raise PluginInstallationError("activation_changed_during_read", "能力包读取期间安装代次已变化。") from exc
    if after != before:
        raise PluginInstallationError("activation_changed_during_read", "能力包读取期间安装代次已变化。")
    return content
