# LLM: 显式管理员停用沿原宿主命令和 ToolExecutor；旧目录先拒绝，写入与未知收口仍由原安装/操作账决定。
# 模块用途: 为停用提供模型不可见的管理工具，展示执行权关闭与资源清理的分别结果。

from __future__ import annotations

from .plugin_deactivation import deactivate_plugin
from .plugin_installation import PluginInstallationError
from .tooling.models import (
    ApprovalPolicy,
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ToolHandlerOutcome,
    ToolModelSpec,
    ToolRuntimePolicy,
)

PLUGIN_DISABLE_TOOL = "plugin_disable"


# LLM: installation/catalog 来自同次原表读取；构造无副作用，不把管理授权交给插件或模型。
# 类用途: 让明确停用命令复用原幂等链，不等待插件业务请求自行完成。
class PluginDisableTool(BaseTool):
    # LLM: never 只免重复询问显式管理员动作，工具禁用、原执行权和 owner 隔离仍由执行器核验。
    # 函数用途: 冻结本次管理所需的用户、运行账、安装版本与声明。
    def __init__(self, owner, repository, operation_id, installation, catalog_revision):
        self.owner, self.repository, self.operation_id = owner, repository, operation_id
        self.installation, self.catalog_revision = installation, catalog_revision
        keys = ("plugin", "catalog_revision", "workspace", "permission_digest")
        self.model_spec = ToolModelSpec(PLUGIN_DISABLE_TOOL, "停用指定插件并核对其精确资源退出。", {
            "type": "object", "properties": {key: {"type": "string"} for key in keys},
            "required": list(keys), "additionalProperties": False,
        })
        self.runtime_policy = ToolRuntimePolicy(
            effect_resolver=EffectResolverPolicy("mutating"), approval_policy=ApprovalPolicy("never"),
            idempotency_policy=IdempotencyPolicy("operation"), mutates_workspace=False,
        )

    # LLM: 新停用请求只控制冻结代次；原请求重放由 ToolExecutor 读原结果，不能再次读取当前代再停一次。
    # 函数用途: 先撤销权限，再清理准确准备任务和共享服务，原退出证据仍留在原资源账。
    def execute(self, params: dict) -> ToolHandlerOutcome:
        if params["catalog_revision"] != self.catalog_revision:
            return self._failure("stale_catalog", "PLUGIN_CATALOG_STALE", "not_started")
        entry = self.installation
        if entry is None or entry.manifest.plugin_id != params["plugin"]:
            return self._failure("plugin_missing", "TOOL_INVALID_ARGUMENTS", "not_started")
        try:
            result = deactivate_plugin(self.owner, self.repository, entry, self.operation_id)
        except PluginInstallationError as exc:
            return self._failure(exc.reason, "TOOL_EXECUTION_FAILED",
                                 "unknown" if exc.commit_state == "unknown" else "failed", exc.commit_state)
        except Exception:  # noqa: BLE001 写入后意外异常不能猜未撤销或重放
            return self._failure("deactivation_unconfirmed", "TOOL_EXECUTION_FAILED", "unknown", "unknown")
        confirmed = result["cleanup_confirmed"]
        return ToolHandlerOutcome(
            PLUGIN_DISABLE_TOOL, confirmed,
            "插件已停用，所选资源退出已确认。" if confirmed else "插件执行权已撤销，资源清理尚未全部确认。",
            error_code="" if confirmed else "PLUGIN_CLEANUP_UNCONFIRMED",
            effect_outcome="" if confirmed else "unknown", result_envelope={"plugin_disable": result},
        )

    # LLM: 不回显目录、配置或底层异常正文；确定提交、未提交与未知各自保持，不从文案反推状态。
    # 函数用途: 为原请求查询生成不泄露私有内容的失败回执。
    def _failure(self, reason: str, error_code: str, effect: str,
                 commit_state: str = "not_committed") -> ToolHandlerOutcome:
        return ToolHandlerOutcome(PLUGIN_DISABLE_TOOL, False, "插件停用尚未得到完整确认，请查询原请求。",
                                  error_code=error_code, effect_outcome=effect,
                                  result_envelope={"plugin_disable": {"reason": reason, "commit_state": commit_state}})
