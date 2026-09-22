# LLM: 卸载仅计算原安装表的固定记录 CAS；原操作账拥有永久结果，缺失记录不能证明旧请求已成功。
# 模块用途: 定义卸载回执与删除准入，避免卸载后重装被旧请求误删。

from __future__ import annotations

from dataclasses import dataclass

from .plugin_installation import (
    PluginCommitReceipt,
    PluginInstallation,
    PluginInstallationError,
    plugin_input_digest,
    validate_install_identity,
)

PLUGIN_REMOVE_TOOL = "plugin_remove"


# LLM: 删除后的结果不伪造 installation；receipt 只交原操作结果保存，不在安装表另建墓碑或历史。
# 类用途: 区分实际卸载与原锁内确认缺失，保留确定的提交事实。
@dataclass(frozen=True)
class PluginRemovalResult:
    outcome: str
    commit_state: str
    receipt: PluginCommitReceipt | None


# LLM: expected 必须来自本次冻结或原停用释放返回值；全记录 CAS 防止版本重置后误删，激活未释放绝不删除。
# 函数用途: 在原锁内的完整快照上确认卸载目标并生成删除回执，本函数不读写文件。
def prepare_removal(operation_id: str, plugin_id: str, expected: PluginInstallation | None,
                    entries: tuple[PluginInstallation, ...]) -> PluginRemovalResult:
    validate_install_identity(operation_id, expected.revision if expected is not None else 0)
    if not isinstance(plugin_id, str) or not plugin_id:
        raise ValueError("卸载缺少插件身份")
    current = next((row for row in entries if row.manifest.plugin_id == plugin_id), None)
    if current != expected or (expected is not None and expected.manifest.plugin_id != plugin_id):
        raise PluginInstallationError("revision_conflict", "插件安装已变化，未删除新的安装。")
    if current is None:
        return PluginRemovalResult("absent", "not_committed", None)
    if current.activation is not None:
        raise PluginInstallationError("activation_in_use", "原插件激活尚未释放，不能卸载。")
    receipt = PluginCommitReceipt(
        operation_id, plugin_input_digest("remove", plugin_id, current.package_sha256, current.revision),
        plugin_id, current.package_sha256, current.revision, current.revision + 1, action="remove",
    )
    return PluginRemovalResult("removed", "committed", receipt)
