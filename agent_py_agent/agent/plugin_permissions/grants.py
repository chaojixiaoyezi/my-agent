# LLM: 授权事实须绑定安装、包与激活；此模块不启动程序或保存状态。
# 模块用途: 为老格式插件生成管理员确认内容，确认不代表 OS 隔离已实施。
from __future__ import annotations

from dataclasses import asdict, dataclass

from ..plugin_activation_record import PluginActivation
from .paths import permission_paths


# LLM: 路径身份仅描述明确管理员选择，不从插件设置推断授权。
# 类用途: 保存一个有限根的地址与身份，便于再次确认时识别漂移。
@dataclass(frozen=True)
class PermissionPath:
    path: str
    device: int
    inode: int
    content_sha256: str = ""


# LLM: 四个维度均为宿主授权，网络 true 包含回环、公网与监听。
# 类用途: 保存管理员选定的读、写、联网和额外程序权限。
@dataclass(frozen=True)
class PermissionSelection:
    read_roots: tuple[PermissionPath, ...] = ()
    write_roots: tuple[PermissionPath, ...] = ()
    network: bool = False
    program_roots: tuple[PermissionPath, ...] = ()


# LLM: 与原计划绑定，不由模型提供执行身份；确认前只读取静态事实。
# 函数用途: 将明确结构化权限转换为可确认的有限路径，不运行插件。
def select_permissions(value: object, forbidden_roots=()) -> PermissionSelection:
    if not isinstance(value, dict) or not set(value) <= {"read_roots", "write_roots", "network", "program_roots"}:
        raise ValueError("授权字段无效，桌面能力尚未支持")
    network = value.get("network", False)
    if type(network) is not bool:
        raise ValueError("网络授权必须是布尔值")
    return PermissionSelection(
        permission_paths(value.get("read_roots", []), forbidden_roots),
        permission_paths(value.get("write_roots", []), forbidden_roots), network,
        permission_paths(value.get("program_roots", []), forbidden_roots, program=True),
    )


# LLM: 新默认只影响后续授权，不降级已经收紧的固定代次。
# 函数用途: 选择本次授权模式，不热改现有进程。
def permission_mode(default: bool, previous: str = "") -> str:
    if type(default) is not bool or previous not in {"", "restricted", "wide", "legacy_compat"}:
        raise ValueError("权限策略无效")
    if previous == "restricted":
        return "restricted"
    return "restricted" if default else "wide"


# LLM: 确认覆盖包、计划、解释器和全部权限；返回值不是执行权。
# 函数用途: 生成人能核对的授权事实，调用者沿原确认码合同处理。
def permission_details(entry, plan, selection: PermissionSelection, mode: str) -> dict:
    if mode not in {"restricted", "wide"} or not isinstance(selection, PermissionSelection):
        raise ValueError("确认授权不接受兼容豁免")
    if entry.manifest.is_content_only or entry.manifest.permissions is not None:
        raise ValueError("该授权仅属于 v1–v6 可执行插件")
    if (entry.package_sha256 != plan.package_sha256 or entry.manifest.plugin_id != plan.plugin_id
            or entry.settings_revision != plan.settings_revision or entry.revision != plan.installation_revision):
        raise ValueError("授权计划与安装不符")
    return {
        "kind": "plugin_legacy_permissions", "policy_version": 1, "mode": mode,
        "plugin_id": entry.manifest.plugin_id, "package_version": entry.manifest.version,
        "installation_ref": entry.installation_ref, "package_sha256": entry.package_sha256,
        "activation_id": PluginActivation(plan, "preparing").activation_id,
        "plan": asdict(plan), "permissions": asdict(selection),
        "entry_module": entry.manifest.entry_module, "entry": asdict(entry.manifest.entry) if entry.manifest.entry else None,
        "host_api": list(entry.manifest.host_api), "sandbox_status": "pending_b7" if mode == "restricted" else "not_required",
    }
