# LLM: 老格式（v1–v6）插件的启动复核只读固定激活的 permission_json 与宿主事实：安装/包/激活身份、
#   受限根 inode 与程序内容、解释器指纹、模式与网络。任何变化都拒绝启动，不自动换绑、扩权或降 wide；
#   restricted 的策略在这里投影成 B7 的 PluginRestrictedSandbox，平台规则仍只在统一底座，不复制第二套。
# 模块用途: 为老格式插件的每次启动复核完整授权事实，并把 restricted 模式交给 B7 公共沙箱入口。

from __future__ import annotations

from dataclasses import asdict, fields
from pathlib import Path

from ..plugin_environment_plan import PluginEnvironmentPlan
from ..plugin_installation import PluginInstallationError
from ..plugin_runtime_facts import PluginRuntimeError, verified_runtime_command
from ..plugin_sandbox import PluginRestrictedSandbox
from .paths import permission_paths
from .state import canonical_permission_json, legacy_permissions


# LLM: 启动复核失败必须是结构化拒绝；一律未提交（拒绝发生在任何启动之前），调用方按 reason 展示或记录。
# 类用途: 表示固定授权事实与当前宿主事实不一致，或缺少可用的固定授权记录。
class LegacyLaunchDenied(PluginInstallationError):
    # 函数用途: 保存稳定失败分类与不含私有值的中文说明。
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(reason, message)


# LLM: 每次启动（含重连）都重新读固定激活授权并重算事实；v8 与内容包不走这份授权账，返回 None。
# 函数用途: 复核老格式插件启动前的完整授权事实；restricted 返回 B7 受限策略，其余模式返回 None。
def legacy_launch_policy(owner, installation) -> PluginRestrictedSandbox | None:
    manifest = installation.manifest
    if manifest.permissions is not None or manifest.is_content_only:
        return None
    grant = _fixed_grant(installation)
    _verify_identity(installation, grant)
    if grant["mode"] != "restricted":
        return None
    _verify_restricted_facts(owner, installation, grant)
    return policy_from_grant(grant)


# LLM: 缺记录不推导豁免，也不把缺失当旧兼容；老格式可执行插件启用时必须已写固定授权。
# 函数用途: 读取固定激活的授权记录，缺失时以结构化原因拒绝启动。
def _fixed_grant(installation) -> dict:
    grant = legacy_permissions(installation)
    if grant is None:
        raise LegacyLaunchDenied("legacy_permission_missing", "插件缺少固定授权记录，本次没有启动。")
    return grant


# LLM: 迁移兼容记录没有 plan/权限，只有身份字段；新授权比对完整计划与身份，两者都不接受换绑。
#   legacy_compat 走专用比对（记录里没有 plugin_id，见 _verify_legacy_compat_identity 的注释）。
#   installation_ref 随每次提交（preparing/active）漂移，只用于确认码绑定，不作为启动复核判据。
# 函数用途: 比对固定授权与当前插件、包、激活身份及计划，任何差异都拒绝启动。
def _verify_identity(installation, grant) -> None:
    mode = grant.get("mode")
    if mode not in {"restricted", "wide", "legacy_compat"}:
        raise LegacyLaunchDenied("legacy_permission_invalid", "固定授权记录无效，本次没有启动。")
    if mode == "legacy_compat":
        _verify_legacy_compat_identity(installation, grant)
        return
    identity = (installation.manifest.plugin_id, installation.package_sha256, installation.activation_id)
    recorded = (grant.get("plugin_id"), grant.get("package_sha256"), grant.get("activation_id"))
    if recorded != identity:
        raise LegacyLaunchDenied("legacy_permission_changed", "安装或激活身份已变化，本次没有启动。")
    plan = grant.get("plan")
    if not isinstance(plan, dict) or set(plan) != {field.name for field in fields(PluginEnvironmentPlan)}:
        raise LegacyLaunchDenied("legacy_permission_invalid", "固定授权计划无效，本次没有启动。")
    if canonical_permission_json(asdict(installation.activation.plan)) != canonical_permission_json(plan):
        raise LegacyLaunchDenied("legacy_permission_changed", "激活计划已变化，本次没有启动。")


# LLM: v3 迁移记录只冻结 activation_id 与 package_sha256（validate_legacy_permission 的六字段集合）；
#   plugin_id 不在记录里——旧表按插件名查找、不重复存，插件名由当前安装 manifest 提供。
#   所以这里只比对记录中实际存在的字段：有的字段必须全部相等，缺字段有结构化理由，不放宽成不比对。
# 函数用途: 复核 legacy_compat 记录的包摘要与激活号仍与当前安装一致，按冻结策略继续启动。
def _verify_legacy_compat_identity(installation, grant) -> None:
    recorded = (grant.get("package_sha256"), grant.get("activation_id"))
    expected = (installation.package_sha256, installation.activation_id)
    if recorded != expected:
        raise LegacyLaunchDenied("legacy_permission_changed", "安装或激活身份已变化，本次没有启动。")


# LLM: 受限根重读 inode/内容，解释器经运行时 pin 复核（stat 优先、变化才重算摘要）；任一变化都拒绝。
# 函数用途: 复核 restricted 固定授权的受限根与解释器事实仍与记录一致。
def _verify_restricted_facts(owner, installation, grant) -> None:
    permissions = _restricted_permissions(grant)
    for key, program in (("read_roots", False), ("write_roots", False), ("program_roots", True)):
        _verify_roots(permissions[key], program)
    entry = installation.manifest.entry
    if entry is None:
        return
    activation = installation.activation
    environment = owner.plugins_dir / "environments" / activation.plan.environment_ref
    try:
        verified_runtime_command(owner.root, environment, entry.kind, activation.plan.interpreter_fingerprint)
    except (PluginRuntimeError, OSError, ValueError) as exc:
        raise LegacyLaunchDenied("legacy_permission_changed", "解释器事实已变化，本次没有启动。") from exc


# LLM: 逐条重读路径身份并比较规范 JSON；路径变成链接、消失或程序内容变化都按"已变化"拒绝。
# 函数用途: 复核一组固定授权根与记录完全一致。
def _verify_roots(rows, program: bool) -> None:
    if not isinstance(rows, list):
        raise LegacyLaunchDenied("legacy_permission_invalid", "固定权限记录无效，本次没有启动。")
    try:
        fresh = [asdict(item) for item in permission_paths([row["path"] for row in rows], (), program=program)]
    except (KeyError, TypeError, ValueError, OSError):
        raise LegacyLaunchDenied("legacy_permission_changed", "授权根或程序内容已变化，本次没有启动。") from None
    if canonical_permission_json(fresh) != canonical_permission_json(rows):
        raise LegacyLaunchDenied("legacy_permission_changed", "授权根或程序内容已变化，本次没有启动。")


# LLM: 四个维度必须完整且网络是严格布尔；结构问题按记录无效拒绝，不按缺字段补默认。
# 函数用途: 从固定授权取出受限权限结构，形状不符时拒绝。
def _restricted_permissions(grant) -> dict:
    permissions = grant.get("permissions")
    expected = {"read_roots", "write_roots", "network", "program_roots"}
    if (not isinstance(permissions, dict) or set(permissions) != expected
            or type(permissions.get("network")) is not bool):
        raise LegacyLaunchDenied("legacy_permission_invalid", "固定权限记录无效，本次没有启动。")
    return permissions


# LLM: 隐藏 Gateway 用户家目录，与 v8 共用同一底座语义；这里只投影策略，不生成平台规则。
# 函数用途: 从已复核的固定授权生成 B7 受限策略，供启动施加与启用预检共用。
def policy_from_grant(grant) -> PluginRestrictedSandbox:
    permissions = _restricted_permissions(grant)
    return PluginRestrictedSandbox(
        read_roots=tuple(Path(row["path"]) for row in permissions["read_roots"]),
        write_roots=tuple(Path(row["path"]) for row in permissions["write_roots"]),
        execute_roots=tuple(Path(row["path"]) for row in permissions["program_roots"]),
        network=permissions["network"],
        hidden_read_root=Path.home(),
    )
