# LLM: 更新 handler 只能由已授权宿主经原 ToolExecutor 调用；只读一次授权包字节，写入事实归 PluginInstallStore.update_package。
#   模型不可见（组装处 exposure 指定）；never 只免重复询问已明确提交的管理员动作，不豁免身份、输入或路径硬门。
# 模块用途: 把"用新版本包替换已停用插件"接到原工具副作用合同，结果带配置是否保留的结构化事实。

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from .path_access_policy import PathAccessPolicy
from .plugin_install_store import PluginInstallStore
from .plugin_installation import PluginInstallationError
from .plugin_package import PackageReadLimits, inspect_plugin_package
from .plugin_sources import read_plugin_source
from .plugin_update import PLUGIN_UPDATE_TOOL, PluginUpdateRequest
from .tooling.models import (
    ApprovalPolicy,
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ToolHandlerOutcome,
    ToolModelSpec,
    ToolRuntimePolicy,
)


# LLM: 依赖是宿主固定的 owner Store、路径策略、工作根和操作 ID，不接受模型或包自报 owner；目标插件来自显式参数。
# 类用途: 执行一次静态包替换，不创建虚拟环境、不导入或启动插件，不切换激活。
class PluginUpdateTool(BaseTool):
    # 函数用途: 准备本次更新工具声明，构造不读写文件。
    def __init__(self, store: PluginInstallStore, policy: PathAccessPolicy, workspace: Path, operation_id: str) -> None:
        self.store, self.policy, self.workspace, self.operation_id = store, policy, workspace, operation_id
        keys = ("plugin", "source", "catalog_revision", "workspace", "permission_digest")
        self.model_spec = ToolModelSpec(
            PLUGIN_UPDATE_TOOL, "用同一插件的新版本本地包替换已停用插件，配置结构不变时保留私有配置。",
            {"type": "object", "properties": {key: {"type": "string"} for key in keys},
             "required": list(keys), "additionalProperties": False},
        )
        self.runtime_policy = ToolRuntimePolicy(
            effect_resolver=EffectResolverPolicy("mutating"), approval_policy=ApprovalPolicy("never"),
            idempotency_policy=IdempotencyPolicy("operation"), mutates_workspace=False,
        )

    # LLM: 源权限与严格打开先于写入，bytes 固定后不再打开来源；领域失败按 reason/commit_state 结构化返回，
    #   未知提交状态保留 unknown，不由文案推断未执行；成功回执带 settings_restored/settings_reason 与新旧版本号。
    # 函数用途: 读取新包、按当前安装版本提交替换，返回可对账的领域回执。
    def execute(self, params: dict) -> ToolHandlerOutcome:
        plugin_id = str(params["plugin"])
        try:
            limits = PackageReadLimits()
            package = inspect_plugin_package(read_plugin_source(
                params["source"], self.workspace, self.policy, max_bytes=limits.archive_bytes,
            ), limits=limits)
        except (OSError, ValueError, RuntimeError):
            return ToolHandlerOutcome(PLUGIN_UPDATE_TOOL, False, "插件包来源不可读、未获授权或格式无效。",
                                      error_code="TOOL_INVALID_ARGUMENTS", effect_outcome="not_started")
        previous = next((entry for entry in self.store.snapshot() if entry.manifest.plugin_id == plugin_id), None)
        report = {"plugin_id": plugin_id, "enabled": False,
                  "previous_version": previous.manifest.version if previous is not None else "",
                  "package_version": package.manifest.version}
        try:
            result = self.store.update_package(PluginUpdateRequest(
                plugin_id, package, self.operation_id, previous.revision if previous is not None else 0,
            ))
        except PluginInstallationError as exc:
            report.update(reason=exc.reason, commit_state=exc.commit_state, receipt=asdict(exc.receipt) if exc.receipt else None)
            return ToolHandlerOutcome(
                PLUGIN_UPDATE_TOOL, False, "插件更新未得到成功结果，请查看该请求状态。",
                error_code="TOOL_INVALID_ARGUMENTS" if exc.reason in {"plugin_missing", "plugin_conflict"} else "TOOL_EXECUTION_FAILED",
                effect_outcome="unknown" if exc.commit_state == "unknown" else ("not_started" if exc.commit_state == "not_committed" else "failed"),
                result_envelope={PLUGIN_UPDATE_TOOL: report},
            )
        except Exception:  # noqa: BLE001 进入安装存储后意外异常保留未知，不自动执行第二次
            report["commit_state"] = "unknown"
            return ToolHandlerOutcome(PLUGIN_UPDATE_TOOL, False, "插件更新结果尚未确认。",
                                      error_code="TOOL_EXECUTION_FAILED", effect_outcome="unknown",
                                      result_envelope={PLUGIN_UPDATE_TOOL: report})
        report.update(outcome=result.outcome, commit_state=result.commit_state,
                      receipt=asdict(result.receipt) if result.receipt else None,
                      settings_restored=result.settings_restored, settings_reason=result.settings_reason,
                      revision=result.installation.revision)
        message = {"updated": ("插件已更新到新版本，当前停用；私有配置已保留，可直接 enable。" if result.settings_restored
                               else "插件已更新到新版本，当前停用；" + ("新版本配置结构变化，原配置未保留，请先 configure 再 enable。"
                                                                if result.settings_reason == "incompatible" else "可直接 enable。")),
                   "unchanged": "新包与当前安装相同，没有变化。",
                   "replayed": "同一更新请求已提交过，复读原结果。"}[result.outcome]
        return ToolHandlerOutcome(PLUGIN_UPDATE_TOOL, True, message, result_envelope={PLUGIN_UPDATE_TOOL: report})
