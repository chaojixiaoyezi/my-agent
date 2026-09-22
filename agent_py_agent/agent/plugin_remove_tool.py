# LLM: 显式卸载沿原管理运行和工具执行器；先释放固定代次再删原安装，包回收留到原结果持久化之后。
# 模块用途: 提供模型不可见的卸载管理工具，保留用户产物、原操作历史及未确认清理证据。

from __future__ import annotations

from dataclasses import asdict

from .plugin_deactivation import deactivate_plugin
from .plugin_install_store import PluginInstallStore
from .plugin_installation import PluginInstallationError
from .plugin_removal import PLUGIN_REMOVE_TOOL, PluginRemovalResult, prepare_removal
from .tooling.models import (
    ApprovalPolicy,
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ToolHandlerOutcome,
    ToolModelSpec,
    ToolRuntimePolicy,
)


# LLM: 原安装与目录来自同次读取，缺失也必须在原锁内确认；构造不加载插件、不删除文件或扩大管理权限。
# 类用途: 固定一次卸载的目标，防止旧请求作用于后来重新安装的插件。
class PluginRemoveTool(BaseTool):
    # LLM: never 只免重复询问明确管理员动作，owner、工具禁用和运行执行权继续由原链核验。
    # 函数用途: 保存本次卸载需要的身份与快照，声明原执行器所需工具合同。
    def __init__(self, owner, repository, operation_id, installation, catalog_revision):
        self.owner, self.repository, self.operation_id = owner, repository, operation_id
        self.installation, self.catalog_revision = installation, catalog_revision
        keys = ("plugin", "catalog_revision", "workspace", "permission_digest")
        self.model_spec = ToolModelSpec(PLUGIN_REMOVE_TOOL, "停用并卸载指定插件，保留用户产物。", {
            "type": "object", "properties": {key: {"type": "string"} for key in keys},
            "required": list(keys), "additionalProperties": False,
        })
        self.runtime_policy = ToolRuntimePolicy(
            effect_resolver=EffectResolverPolicy("mutating"), approval_policy=ApprovalPolicy("never"),
            idempotency_policy=IdempotencyPolicy("operation"), mutates_workspace=False,
        )

    # LLM: 不重读插件名换绑；提交异常只按原锁严格读回和完整回执裁决，确定已删可成功收尾，UNKNOWN 保留。
    # 函数用途: 经原停用释放链卸载固定安装，实际包字节由管理写入口在成功落账后回收。
    def execute(self, params: dict) -> ToolHandlerOutcome:
        report = {"plugin_id": params["plugin"], "removed": False}
        if params["catalog_revision"] != self.catalog_revision:
            return self._failure(report, "stale_catalog", "PLUGIN_CATALOG_STALE", "not_started")
        entry = self.installation
        if entry is not None and entry.manifest.plugin_id != params["plugin"]:
            return self._failure(report, "plugin_conflict", "TOOL_INVALID_ARGUMENTS", "not_started")
        try:
            if entry is not None:
                deactivated = deactivate_plugin(self.owner, self.repository, entry, self.operation_id)
                report.update(deactivated.report)
                entry = deactivated.installation
                if not report["cleanup_confirmed"] or not report["released"]:
                    return self._failure(report, "release_unconfirmed", "PLUGIN_CLEANUP_UNCONFIRMED", "unknown")
            result = PluginInstallStore(self.owner).remove(self.operation_id, params["plugin"], entry)
        except PluginInstallationError as exc:
            if (exc.commit_state == "committed" and exc.receipt is not None
                    and exc.receipt.action == "remove" and entry is not None and entry.activation is None
                    and exc.receipt == prepare_removal(self.operation_id, params["plugin"], entry, (entry,)).receipt):
                result = PluginRemovalResult("removed", "committed", exc.receipt)
                report["storage_warning"] = exc.reason
            else:
                report.update(commit_state=exc.commit_state, receipt=asdict(exc.receipt) if exc.receipt else None)
                return self._failure(report, exc.reason, "TOOL_EXECUTION_FAILED",
                                     "unknown" if exc.commit_state == "unknown" else "failed")
        except Exception:  # noqa: BLE001 已进入可能写入的原链，不能猜未发生或替换新目标重试
            report["commit_state"] = "unknown"
            return self._failure(report, "removal_unconfirmed", "TOOL_EXECUTION_FAILED", "unknown")
        report.update(removed=True, enabled=False, released=True, cleanup_confirmed=True,
                      outcome=result.outcome, commit_state=result.commit_state,
                      receipt=asdict(result.receipt) if result.receipt else None)
        return ToolHandlerOutcome(PLUGIN_REMOVE_TOOL, True, "插件已卸载，用户产物与操作历史保留。",
                                  result_envelope={PLUGIN_REMOVE_TOOL: report})

    # LLM: 失败沿结构化原结果保存，不回显私有路径或配置；removed=false 与可能已停用分别可见。
    # 函数用途: 报告卸载未完成的具体阶段，供查询和后续明确管理动作判断。
    def _failure(self, report: dict, reason: str, error_code: str, effect: str) -> ToolHandlerOutcome:
        return ToolHandlerOutcome(PLUGIN_REMOVE_TOOL, False, "插件卸载尚未完整确认，请查看原请求。",
                                  error_code=error_code, effect_outcome=effect,
                                  result_envelope={PLUGIN_REMOVE_TOOL: {**report, "reason": reason}})
