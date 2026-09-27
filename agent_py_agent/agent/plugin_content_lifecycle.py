# LLM: 内容生命周期只在原安装锁内纯计算，不启动环境、不写第二份状态；Store 与原操作账继续裁决提交和重放。
# 模块用途: 检查无进程能力包的启用、撤销及持久身份一致性，保留旧进程生命周期合同。

from __future__ import annotations

from dataclasses import replace

from .common.strict_json import load_strict_json
from .plugin_content_activation import PluginContentActivation
from .plugin_installation import PluginCommitReceipt, PluginInstallationError, PluginMutationResult
from .plugin_manifest import canonical_plugin_settings


# LLM: 调用方持有原安装锁；内容直接从未激活发布 active，撤销只能针对同代，不能换包或换设置。
# 函数用途: 为内容激活生成原表 CAS 结果和提交回执，不产生环境或进程。
def prepare_content_activation(request, entries) -> PluginMutationResult:
    target = request.activation
    for row in entries:
        if row.last_commit.operation_id == request.operation_id:
            if row.last_commit.input_digest == request.input_digest:
                return PluginMutationResult(row, "replayed", "committed", row.last_commit)
            raise PluginInstallationError("operation_conflict", "同一能力操作不能改换输入。")
    existing = next((row for row in entries if row.manifest.plugin_id == target.plugin_id), None)
    if existing is None:
        raise PluginInstallationError("plugin_missing", "能力包尚未安装。")
    if existing.revision != request.expected_revision:
        raise PluginInstallationError("revision_conflict", "安装版本已变化，请先读取当前状态。")
    if (not existing.manifest.is_content_only or existing.package_sha256 != target.package_sha256
            or existing.settings_revision != target.settings_revision):
        raise PluginInstallationError("activation_binding_conflict", "内容激活与安装身份不符。")
    _validate_transition(existing, target)
    if existing.activation == target:
        return PluginMutationResult(existing, "unchanged", "not_committed", None)
    receipt = PluginCommitReceipt(request.operation_id, request.input_digest, target.plugin_id, target.package_sha256,
                                  existing.revision, existing.revision + 1, request.action,
                                  activation_sha256=target.content_sha256)
    updated = replace(existing, revision=receipt.after_revision, last_commit=receipt, activation=target)
    return PluginMutationResult(updated, request.action, "committed", receipt)


# LLM: 无进程内容无需 preparing，但不得越过已激活/已撤销代；正文或资源目录存在不等于可发布。
# 函数用途: 检查直接发布和同代撤销的迁移条件。
def _validate_transition(existing, target: PluginContentActivation) -> None:
    current = existing.activation
    if target.phase == "active":
        if current is not None:
            raise PluginInstallationError("activation_unsettled", "原内容激活尚未释放。")
        if target.installation_revision != existing.revision:
            raise PluginInstallationError("activation_binding_conflict", "内容激活与安装版本不符。")
        try:
            canonical_plugin_settings(load_strict_json(existing.settings_json or "{}"), existing.manifest.settings_schema)
        except (ValueError, TypeError, RecursionError) as exc:
            raise PluginInstallationError("invalid_settings", "能力配置尚未满足声明。") from exc
    elif (not isinstance(current, PluginContentActivation) or current.activation_id != target.activation_id):
        raise PluginInstallationError("activation_binding_conflict", "撤销引用与原内容代次不符。")


# LLM: 回执、包和激活必须属于同次原操作；内容 active/revoked 各前进一步，不沿用进程准备的版本偏移。
# 函数用途: 在安装表读写时验证内容激活的所有关联字段。
def validate_content_installation(installation) -> None:
    activation = installation.activation
    receipt = installation.last_commit
    if (not isinstance(activation, PluginContentActivation) or not installation.manifest.is_content_only
            or receipt.action != {"active": "activate", "revoked": "revoke"}[activation.phase]
            or receipt.activation_sha256 != activation.content_sha256
            or activation.plugin_id != installation.manifest.plugin_id
            or activation.package_sha256 != installation.package_sha256
            or activation.settings_revision != installation.settings_revision
            or installation.revision - activation.installation_revision != {"active": 1, "revoked": 2}[activation.phase]
            or (activation.phase == "active" and receipt.operation_id != activation.operation_id)):
        raise ValueError("内容激活与安装回执不一致")
    canonical_plugin_settings(load_strict_json(installation.settings_json or "{}"), installation.manifest.settings_schema)
