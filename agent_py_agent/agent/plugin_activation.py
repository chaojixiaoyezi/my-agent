# LLM: 激活迁移是安装领域纯计算，Store 持锁调用；OS 启动、目录验收及清理不在这里，不能凭状态伪造执行结果。
# 模块用途: 将同一次启用的准备和发布、以及明确撤销写成可检查的版本迁移，不另存运行或幂等历史。

from __future__ import annotations

from dataclasses import dataclass, replace

from .common.strict_json import load_strict_json
from .plugin_activation_record import PluginActivation, plugin_catalog_digest
from .plugin_installation import (
    PluginCommitReceipt,
    PluginInstallation,
    PluginInstallationError,
    PluginMutationResult,
    plugin_input_digest,
    validate_install_identity,
)
from .plugin_manifest import canonical_plugin_settings

PLUGIN_ENABLE_TOOL = "plugin_enable"


# LLM: activation 是宿主从固定候选生成的目标状态，不能直接由管理正文或模型传入；operation_id 仍来自原操作。
# 类用途: 将一次准备、发布或撤销绑定到明确安装版本与原激活计划。
@dataclass(frozen=True)
class PluginActivationRequest:
    operation_id: str
    expected_revision: int
    activation: PluginActivation

    # LLM: 准备与发布必须来自该计划的原操作，撤销可由独立获准管理操作发起；这里不授予管理权限。
    # 函数用途: 在写入前拒绝不完整请求和启用操作换绑。
    def __post_init__(self) -> None:
        validate_install_identity(self.operation_id, self.expected_revision)
        if not isinstance(self.activation, PluginActivation):
            raise ValueError("激活请求缺少固定计划")
        if self.activation.phase != "revoked" and self.operation_id != self.activation.plan.operation_id:
            raise ValueError("激活请求与原计划操作不符")

    # LLM: 动作是当前有限状态协议的显式映射，不解析正文或接受任意状态别名。
    # 函数用途: 取得唯一安装回执使用的提交动作。
    @property
    def action(self) -> str:
        return {"preparing": "prepare", "active": "activate", "revoked": "revoke"}[self.activation.phase]

    # LLM: 同一原请求可以有准备/发布两个提交，但各自摘要必须包含阶段、期望版与完整目标；不是新的宿主 operation。
    # 函数用途: 让响应丢失后的同次提交可精确读回。
    @property
    def input_digest(self) -> str:
        plan = self.activation.plan
        return plugin_input_digest(self.action, plan.plugin_id, plan.package_sha256,
                                   self.expected_revision, activation_sha256=self.activation.content_sha256)


# LLM: 调用方持有原安装锁；阶段变化前核对原包、配置与版本，只允许同一计划前进或撤销，旧代不能修改新代。
# 函数用途: 纯计算一个激活提交或已有提交的重放，不启动服务或修改文件。
def prepare_activation(request: PluginActivationRequest, entries: tuple[PluginInstallation, ...]) -> PluginMutationResult:
    plan = request.activation.plan
    for entry in entries:
        if entry.last_commit.operation_id != request.operation_id:
            continue
        if entry.last_commit.input_digest == request.input_digest:
            return PluginMutationResult(entry, "replayed", "committed", entry.last_commit)
        if not (entry.manifest.plugin_id == plan.plugin_id and request.operation_id == plan.operation_id
                and entry.activation is not None and entry.activation.plan == plan
                and entry.last_commit.action in {"prepare", "activate"}
                and request.action in {"activate", "revoke"}
                and entry.last_commit.action != request.action):
            raise PluginInstallationError("operation_conflict", "同一插件操作不能改换输入。")
    existing = next((row for row in entries if row.manifest.plugin_id == plan.plugin_id), None)
    if existing is None:
        raise PluginInstallationError("plugin_missing", "插件尚未安装。")
    if existing.revision != request.expected_revision:
        raise PluginInstallationError("revision_conflict", "安装版本已变化，请先读取当前状态。")
    if existing.package_sha256 != plan.package_sha256 or existing.settings_revision != plan.settings_revision:
        raise PluginInstallationError("activation_binding_conflict", "插件包或配置与激活计划不同。")
    _validate_transition(request, existing)
    if existing.activation == request.activation:
        return PluginMutationResult(existing, "unchanged", "not_committed", None)
    receipt = PluginCommitReceipt(
        request.operation_id, request.input_digest, plan.plugin_id, plan.package_sha256,
        existing.revision, existing.revision + 1, request.action,
        activation_sha256=request.activation.content_sha256,
    )
    updated = replace(existing, revision=receipt.after_revision, last_commit=receipt, activation=request.activation)
    return PluginMutationResult(updated, request.action, "committed", receipt)


# LLM: 配置在预留前按原 schema 验证；发布只接受同一准备，撤销只保留原目录事实，不提供清理成功或自动恢复入口。
# 函数用途: 守住准备、发布、撤销的顺序和不可逆边界。
def _validate_transition(request: PluginActivationRequest, existing: PluginInstallation) -> None:
    target, current = request.activation, existing.activation
    if target.phase == "preparing":
        if current is not None:
            raise PluginInstallationError("activation_unsettled", "原激活尚未清理，不能准备新一代。")
        if target.plan.installation_revision != existing.revision:
            raise PluginInstallationError("activation_binding_conflict", "激活计划与安装版本不同。")
        try:
            canonical_plugin_settings(load_strict_json(existing.settings_json or "{}"), existing.manifest.settings_schema)
        except (ValueError, TypeError, RecursionError) as exc:
            raise PluginInstallationError("invalid_settings", "插件配置尚未满足声明。") from exc
        return
    if current is None or current.plan != target.plan:
        raise PluginInstallationError("activation_binding_conflict", "当前激活与请求代次不同。")
    if target.phase == "active":
        if current.phase != "preparing":
            raise PluginInstallationError("activation_revoked", "原准备已结束或撤销，不能发布。")
        if target.catalog_sha256 != plugin_catalog_digest(existing.manifest):
            raise PluginInstallationError("activation_catalog_conflict", "候选工具目录与原包声明不同。")
    elif target.catalog_sha256 != current.catalog_sha256:
        raise PluginInstallationError("activation_catalog_conflict", "撤销不能改换原工具目录。")


# LLM: 这是 Store 核验资源退出并删除固定环境后的纯 CAS；不接受外部 cleaned 标志，也不能释放活跃或后来换代的安装。
# 函数用途: 清空准确的已撤销激活，保留原回执和私有配置，为下一次启用开放同一安装记录。
def prepare_release(operation_id: str, expected: PluginInstallation,
                    entries: tuple[PluginInstallation, ...]) -> PluginMutationResult:
    validate_install_identity(operation_id, expected.revision)
    activation = expected.activation
    if activation is None or activation.phase != "revoked":
        raise PluginInstallationError("activation_unsettled", "只能释放已撤销且清理确认的原激活。")
    digest = plugin_input_digest("release", expected.manifest.plugin_id, expected.package_sha256,
                                 expected.revision, activation_sha256=activation.content_sha256)
    for row in entries:
        if row.last_commit.operation_id == operation_id:
            if row.last_commit.input_digest == digest:
                return PluginMutationResult(row, "replayed", "committed", row.last_commit)
            if row != expected or row.last_commit.action != "revoke":
                raise PluginInstallationError("operation_conflict", "同一插件操作不能改换输入。")
    current = next((row for row in entries if row.manifest.plugin_id == expected.manifest.plugin_id), None)
    if current != expected:
        raise PluginInstallationError("revision_conflict", "安装或激活已变化，请读取最新状态。")
    receipt = PluginCommitReceipt(operation_id, digest, expected.manifest.plugin_id, expected.package_sha256,
                                  expected.revision, expected.revision + 1, "release",
                                  activation_sha256=activation.content_sha256)
    return PluginMutationResult(replace(expected, revision=receipt.after_revision, last_commit=receipt,
                                        activation=None), "released", "committed", receipt)
