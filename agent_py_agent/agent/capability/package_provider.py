# LLM: 内容发现与读取只消费原 owner 安装表和摘要地址；不扫描目录、启进程、解包或另建权限账。
# 模块用途: 把已启用内容包装成当前轮快照，并在每次私有成员读取前后复核同一安装代次。
from __future__ import annotations

from functools import partial

from ..plugin_install_store import PluginInstallStore
from ..plugin_installation import PluginInstallation, PluginInstallationError
from .package_snapshot import CapabilityPackageSnapshot


# LLM: owner 必须来自原宿主解析器；这里只读安装声明，不读取全部文件正文，错误由 SkillsService 单独记录。
# 函数用途: 列出本 owner 当前已启用的独立能力包，旧 v3 随包 Skill 仍走原 plugin_roots。
def enabled_capability_packages(owner) -> tuple[CapabilityPackageSnapshot, ...]:
    if owner is None:
        return ()
    store = PluginInstallStore(owner)
    return tuple(_package_snapshot(store, entry) for entry in store.snapshot()
                 if entry.enabled and entry.manifest.is_content_only)


# LLM: 闭包固定完整安装而非仅包名，避免卸载重装后旧快照借新代复活；不保存第二份持久安装记录。
# 函数用途: 将一条有效安装投影为只含包摘要和声明成员的冻结发现对象。
def _package_snapshot(store: PluginInstallStore, installation: PluginInstallation) -> CapabilityPackageSnapshot:
    manifest = installation.manifest
    capability = manifest.capability
    return CapabilityPackageSnapshot(
        package_id=manifest.plugin_id, version=manifest.version, summary=manifest.summary,
        description=capability.description, keywords=capability.keywords,
        entry_document=capability.entry_document, package_sha256=installation.package_sha256,
        activation_id=installation.activation_id, members=manifest.files,
        reader=partial(read_capability_member, store, installation),
    )


# LLM: 同 owner Store 的原准入事实先读后读；归档字节已经完整复验，返回前完整 installation 必须仍相等。
# 函数用途: 有界读取一个包内成员；撤销、换代、包篡改和未声明路径都失败，不启动或执行资源。
def read_capability_member(store: PluginInstallStore, expected: PluginInstallation, member_path: str) -> bytes:
    from ..plugin_package import PackageReadLimits, PluginPackageSnapshot, read_plugin_member

    before = store.require_activation(expected.manifest.plugin_id, expected.activation_id)
    if before != expected or not before.manifest.is_content_only:
        raise PluginInstallationError("activation_unavailable", "能力包原安装代次已变化。")
    package = PluginPackageSnapshot(before.manifest, store.package_bytes(before))
    content = read_plugin_member(package, member_path, max_bytes=PackageReadLimits().member_bytes)
    after = store.require_activation(expected.manifest.plugin_id, expected.activation_id)
    if after != before:
        raise PluginInstallationError("activation_unavailable", "能力包读取期间安装代次已变化。")
    return content
