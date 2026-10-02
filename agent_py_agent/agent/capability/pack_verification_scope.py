# LLM: 能力包 v2 块 3：宿主核验在本回合对哪些包生效。只认结构化事实：
#   - 能力配置开关 capability_pack_host_verification_enabled（仓库默认 false，模型不能改）；
#   - 插件总闸 enable_plugins/enable_tools 与可信 owner（和能力包快照同一来源）；
#   - 本任务的 pins（task_references.pinned_package_references），按 activation_id 从安装表取原安装项，整包摘要必须和 pin 一致；
#   - 包声明里确实有检查程序。
#   基线要覆盖“回合中途才被钉住的包”，所以基线模式取全部已启用、声明了核验的包，而不只是当前已钉住的。
#   不读模型文字、不按包名或文件名猜。改动同步 test_pack_verification_service.py。
# 模块用途: 给写后检查、收尾检查确定生效的包、owner 和需要快照的路径模式。

from __future__ import annotations

from dataclasses import dataclass

from ..plugin_install_store import PluginInstallStore
from ..plugin_installation import PluginInstallationError
from .runtime_config_reload import capability_config_for_agent
from .task_references import pinned_package_references


# 类用途: 本回合钉住、且声明了检查程序的一个能力包（原安装项 + 核验声明）。
@dataclass(frozen=True)
class PinnedVerificationPackage:
    installation: object
    verification: object


# LLM: 开关只从能力配置读；配置对象不是真正的 CapabilityConfig 时 capability_config_for_agent 会回到默认值（false）。
# 函数用途: 判断宿主核验是否打开。
def host_verification_enabled(agent: object) -> bool:
    config = capability_config_for_agent(agent)
    return bool(getattr(config, "capability_pack_host_verification_enabled", False))


# LLM: 和 Agent._capability_packages 同一套 owner 解析；插件总闸关着时返回 None（不核验）。
# 函数用途: 返回当前回合可信的 owner home。
def verification_owner(agent: object) -> object | None:
    config = getattr(agent, "config", None)
    home_paths = getattr(agent, "home_paths", None)
    if not (getattr(config, "enable_plugins", False) and getattr(config, "enable_tools", False)) or home_paths is None:
        return None
    from ..user_space.owner_resolver import owner_identity_from_config, resolve_owner_home

    return resolve_owner_home(home_paths.root, owner_identity_from_config(config))


# LLM: pin 指向的激活代次已撤销、换代或整包摘要对不上的包不核验（它本来也不该再被本任务使用）；没有检查程序的包跳过。
# 函数用途: 列出本回合钉住、可核验的能力包。
def pinned_verification_packages(agent: object, attrs: object, owner: object) -> list[PinnedVerificationPackage]:
    store = PluginInstallStore(owner)
    packages = []
    for ref in pinned_package_references(agent, attrs):
        try:
            entry = store.require_activation(ref["package_id"], ref["activation_id"])
        except (PluginInstallationError, OSError, ValueError):
            continue
        verification = _verification(entry)
        if entry.package_sha256 == ref["content_sha256"] and verification is not None and verification.verifiers:
            packages.append(PinnedVerificationPackage(entry, verification))
    return packages


# LLM: 只看已激活的能力包；交付物和检查程序输入的模式都要进基线（输入要靠基线判断“任务开始时就有”）。
# 函数用途: 汇总全部已启用、声明了核验的包的路径模式。
def enabled_verification_patterns(owner: object) -> tuple[str, ...]:
    patterns: list[str] = []
    for entry in PluginInstallStore(owner).snapshot():
        activation = getattr(entry, "activation", None)
        verification = _verification(entry)
        if verification is None or getattr(activation, "phase", "") != "active":
            continue
        for declaration in (*verification.deliverables, *(item for row in verification.verifiers for item in row.inputs)):
            patterns.extend(pattern for pattern in declaration.path_patterns if pattern not in patterns)
    return tuple(patterns)


# 函数用途: 取安装项里的能力核验声明（不是能力包或没声明时为 None）。
def _verification(entry: object) -> object | None:
    capability = getattr(getattr(entry, "manifest", None), "capability", None)
    return getattr(capability, "verification", None)
