# LLM: 配置准入是安装领域的纯计算，持久化仍由 PluginInstallStore 负责；没有独立配置表或执行器。
# 模块用途: 验证完整配置替换、原请求重放与版本冲突，私有值不进入工具回执。

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace

from .common.strict_json import load_strict_json
from .plugin_installation import (
    PluginCommitReceipt,
    PluginInstallation,
    PluginInstallationError,
    PluginMutationResult,
    plugin_input_digest,
    validate_install_identity,
)
from .plugin_manifest import PLUGIN_SETTINGS_BYTES, canonical_plugin_settings


# LLM: 配置值已由一次有界读取冻结，不能把对象引用或来源文件用作后续可变输入；版本来自宿主已见目录。
# 类用途: 固定一次管理配置的目标、版本和完整私有值，禁止 repr 输出值。
@dataclass(frozen=True)
class PluginConfigureRequest:
    plugin_id: str
    package_sha256: str
    operation_id: str
    expected_revision: int
    settings_json: str = field(repr=False)

    # LLM: 输入格式先于持久操作检查；匹配的 manifest 与 schema 在原安装锁内由 prepare_configuration 验证。
    # 函数用途: 拒绝错误操作身份和未冻结的配置值。
    def __post_init__(self) -> None:
        validate_install_identity(self.operation_id, self.expected_revision)
        if not isinstance(self.settings_json, str) or len(self.settings_json.encode("utf-8")) > PLUGIN_SETTINGS_BYTES:
            raise ValueError("配置超过读取预算或类型错误")

    # LLM: 摘要用于原请求匹配，不是授权；目录和用户结果不可输出配置正文或该摘要。
    # 函数用途: 为同一配置操作形成可重复核对的输入身份。
    @property
    def input_digest(self) -> str:
        return plugin_input_digest("configure", self.plugin_id, self.package_sha256,
                                   self.expected_revision, hashlib.sha256(self.settings_json.encode()).hexdigest())


# LLM: Store 在原锁内传当前完整记录；新请求只改停用插件，重放不写文件，相同值不制造新版本。
# 函数用途: 纯计算完整配置替换后的安装记录和回执，或报告跨版本、包变化和旧操作冲突。
def prepare_configuration(request: PluginConfigureRequest,
                          entries: tuple[PluginInstallation, ...]) -> PluginMutationResult:
    for entry in entries:
        if entry.last_commit.operation_id == request.operation_id:
            if entry.last_commit.action != "configure" or entry.last_commit.input_digest != request.input_digest:
                raise PluginInstallationError("operation_conflict", "同一插件操作不能改换输入。")
            return PluginMutationResult(entry, "replayed", "committed", entry.last_commit)
    existing = next((row for row in entries if row.manifest.plugin_id == request.plugin_id), None)
    if existing is None:
        raise PluginInstallationError("plugin_missing", "插件尚未安装。")
    if existing.revision != request.expected_revision:
        raise PluginInstallationError("revision_conflict", "安装版本已变化，请先读取当前状态。")
    if existing.package_sha256 != request.package_sha256:
        raise PluginInstallationError("package_conflict", "当前插件包与已见内容不同。")
    if existing.enabled:
        raise PluginInstallationError("plugin_enabled", "请先停用插件再修改配置。")
    try:
        canonical = canonical_plugin_settings(load_strict_json(request.settings_json), existing.manifest.settings_schema)
        if canonical != request.settings_json:
            raise ValueError("配置不是规范 JSON")
    except (ValueError, TypeError, RecursionError) as exc:
        raise PluginInstallationError("invalid_settings", "插件配置格式或内容不符合声明。") from exc
    if existing.settings_json == canonical:
        return PluginMutationResult(existing, "unchanged", "not_committed", None)
    receipt = PluginCommitReceipt(
        request.operation_id, request.input_digest, request.plugin_id, request.package_sha256,
        existing.revision, existing.revision + 1, "configure", hashlib.sha256(canonical.encode()).hexdigest(),
    )
    updated = replace(existing, revision=receipt.after_revision, last_commit=receipt,
                      settings_json=canonical, settings_revision=receipt.after_revision)
    return PluginMutationResult(updated, "configured", "committed", receipt)
