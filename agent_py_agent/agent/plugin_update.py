# LLM: 插件版本更新的纯领域合同：同一插件 ID 的新版本包替换"已停用且激活已结清"的安装记录。同一临界区内先生成 install 回执
#   （rev+1，配置清空），旧配置能按新声明规范化时紧接 configure 回执（rev+2）恢复配置——只复用既有持久动作与摘要规则，
#   不新增回执动作或安装表字段，旧运行时仍能读安装表。不停进程、不切激活：已启用插件必须先 /plugins disable；
#   旧包 blob 保留给历史引用（与卸载前口径一致）。本模块不写文件，提交由 PluginInstallStore.update_package 完成。
# 模块用途: 把"换新版本、尽量保留私有配置"算成一次原子的安装表更新计划，供管理工具与 Store 调用。
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace

from .common.strict_json import load_strict_json
from .plugin_installation import (
    PluginCommitReceipt,
    PluginInstallation,
    PluginInstallationError,
    plugin_input_digest,
    validate_install_identity,
)
from .plugin_manifest import canonical_plugin_settings
from .plugin_package import PluginPackageSnapshot

PLUGIN_UPDATE_TOOL = "plugin_update"


# LLM: 与安装请求同一身份校验；expected_revision 来自调用方刚读到的当前安装版本，CAS 在计划阶段复核。
# 类用途: 一次版本更新请求：目标插件、新包快照、宿主操作身份与期望版本。
@dataclass(frozen=True)
class PluginUpdateRequest:
    plugin_id: str
    package: PluginPackageSnapshot
    operation_id: str
    expected_revision: int

    # 函数用途: 拒绝无效操作身份、缺包或空插件 ID。
    def __post_init__(self) -> None:
        validate_install_identity(self.operation_id, self.expected_revision)
        if not isinstance(self.package, PluginPackageSnapshot):
            raise ValueError("更新请求缺少包快照")
        if not isinstance(self.plugin_id, str) or not self.plugin_id:
            raise ValueError("更新请求缺少插件 ID")

    # LLM: install 步的输入摘要沿 plugin_input_digest("install", …) 口径，重放判定与普通安装一致。
    # 函数用途: 给 install 回执生成可重复比较的输入标识。
    @property
    def input_digest(self) -> str:
        return plugin_input_digest("install", self.plugin_id, self.package.sha256, self.expected_revision)


# LLM: outcome ∈ updated / replayed / unchanged；settings_reason ∈ none（原本无配置）/ restored / incompatible。
#   receipt 是最后一步的回执（有配置恢复时为 configure 回执），commit_state 沿安装表提交事实。
# 类用途: 更新结果，供管理工具生成结构化回执。
@dataclass(frozen=True)
class PluginUpdateResult:
    installation: PluginInstallation
    outcome: str
    commit_state: str
    receipt: PluginCommitReceipt | None
    settings_restored: bool = False
    settings_reason: str = "none"


# LLM: entries 是提交后的完整安装表；commit 为 None 表示不写文件（重放/未变化）。
# 类用途: 一次更新的完整写入计划。
@dataclass(frozen=True)
class PluginUpdatePlan:
    entries: tuple[PluginInstallation, ...]
    commit: PluginCommitReceipt | None
    result: PluginUpdateResult


# LLM: 准入顺序固定：同操作重放 → 插件存在 → 新包 ID 一致 → 版本 CAS → 激活已结清 → 包确实不同；任一失败抛结构化
#   PluginInstallationError，不写文件。不解析包正文或来源路径判断身份。
# 函数用途: 在 Store 锁内把一次更新请求算成新的安装表和回执。
def plan_package_update(request: PluginUpdateRequest, entries: tuple[PluginInstallation, ...]) -> PluginUpdatePlan:
    for entry in entries:
        if entry.last_commit.operation_id == request.operation_id:
            return PluginUpdatePlan(entries, None, PluginUpdateResult(entry, "replayed", "committed", entry.last_commit,
                                                                      entry.settings_json is not None,
                                                                      "restored" if entry.settings_json is not None else "none"))
    existing = next((row for row in entries if row.manifest.plugin_id == request.plugin_id), None)
    if existing is None:
        raise PluginInstallationError("plugin_missing", "插件尚未安装，请改用 install。")
    if request.package.manifest.plugin_id != request.plugin_id:
        raise PluginInstallationError("plugin_conflict", "新包声明的插件 ID 与更新目标不一致。")
    if existing.revision != request.expected_revision:
        raise PluginInstallationError("revision_conflict", "安装版本已变化，请先读取当前状态。")
    if existing.activation is not None:
        raise PluginInstallationError("activation_unsettled", "请先停用并确认原激活清理完成，再更新版本。")
    if existing.package_sha256 == request.package.sha256:
        return PluginUpdatePlan(entries, None, PluginUpdateResult(existing, "unchanged", "not_committed", None,
                                                                  existing.settings_json is not None,
                                                                  "restored" if existing.settings_json is not None else "none"))
    install_receipt = PluginCommitReceipt(
        request.operation_id, request.input_digest, request.plugin_id, request.package.sha256,
        existing.revision, existing.revision + 1,
    )
    updated = PluginInstallation(request.package.manifest, request.package.sha256, install_receipt.after_revision, install_receipt)
    restored, reason = False, "none"
    if existing.settings_json is not None:
        updated, restored, reason = _restore_settings(request, existing, updated)
    result = PluginUpdateResult(updated, "updated", "committed", updated.last_commit, restored, reason)
    return PluginUpdatePlan(
        tuple(updated if row.manifest.plugin_id == request.plugin_id else row for row in entries), updated.last_commit, result,
    )


# LLM: 旧配置按新声明的 settings_schema 规范化成功才恢复，并以 configure 回执（同一 operation_id）落账；失败只记
#   incompatible，配置清空，由用户 configure 后再 enable。不把不合规配置塞进新版本。
# 函数用途: 尝试把上一版本的私有配置带到新版本。
def _restore_settings(
    request: PluginUpdateRequest, existing: PluginInstallation, installed: PluginInstallation,
) -> tuple[PluginInstallation, bool, str]:
    try:
        canonical = canonical_plugin_settings(load_strict_json(existing.settings_json), request.package.manifest.settings_schema)
    except (ValueError, TypeError, RecursionError):
        return installed, False, "incompatible"
    settings_sha = hashlib.sha256(canonical.encode()).hexdigest()
    receipt = PluginCommitReceipt(
        request.operation_id,
        plugin_input_digest("configure", request.plugin_id, request.package.sha256, installed.revision, settings_sha),
        request.plugin_id, request.package.sha256, installed.revision, installed.revision + 1, "configure", settings_sha,
    )
    configured = replace(installed, revision=receipt.after_revision, last_commit=receipt,
                         settings_json=canonical, settings_revision=receipt.after_revision)
    return configured, True, "restored"
