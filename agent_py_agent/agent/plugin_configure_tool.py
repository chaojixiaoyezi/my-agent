# LLM: 宿主配置工具只在原授权执行链使用，值不进入参数、公开目录或结果；写入仅经唯一安装 Store。
# 模块用途: 从明确文件读取并验证完整配置，让停用插件可设置后再进入后续启用流程。

from __future__ import annotations

from pathlib import Path

from .common.strict_json import load_strict_json
from .path_access_policy import PathAccessPolicy
from .plugin_configuration import PluginConfigureRequest
from .plugin_install_store import PluginInstallStore
from .plugin_installation import PluginInstallation, PluginInstallationError
from .plugin_manifest import PLUGIN_SETTINGS_BYTES, canonical_plugin_settings
from .plugin_sources import read_plugin_source
from .tooling.models import (
    ApprovalPolicy,
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ToolHandlerOutcome,
    ToolModelSpec,
    ToolRuntimePolicy,
)

PLUGIN_CONFIGURE_TOOL = "plugin_configure"


# LLM: 构造时冻结宿主最新读取的安装和目录版本；执行前比对已提交版本，锁内仍须 CAS，不能只信目录摘要。
# 类用途: 将完整私有配置替换接入既有幂等操作和工具禁用策略。
class PluginConfigureTool(BaseTool):
    # LLM: 不读取来源或创建文件；never 仅免重复询问明确管理员动作，不豁免权限与原工具准入。
    # 函数用途: 绑定所需安装、权限、工作根和原操作身份，准备模型不可见的工具声明。
    def __init__(self, store: PluginInstallStore, policy: PathAccessPolicy, workspace: Path,
                 operation_id: str, installation: PluginInstallation | None, catalog_revision: str) -> None:
        self.store, self.policy, self.workspace = store, policy, workspace
        self.operation_id, self.installation, self.catalog_revision = operation_id, installation, catalog_revision
        keys = ("plugin", "source", "catalog_revision", "workspace", "permission_digest")
        self.model_spec = ToolModelSpec(PLUGIN_CONFIGURE_TOOL, "校验并保存停用插件的完整私有配置。", {
            "type": "object", "properties": {key: {"type": "string"} for key in keys},
            "required": list(keys), "additionalProperties": False,
        })
        self.runtime_policy = ToolRuntimePolicy(
            effect_resolver=EffectResolverPolicy("mutating"), approval_policy=ApprovalPolicy("never"),
            idempotency_policy=IdempotencyPolicy("operation"), mutates_workspace=False,
        )

    # LLM: 配置正文始终是 handler 私有值；来源字节只读一次，失败结果仅含分类和版本，不透传验证器动态键/值。
    # 函数用途: 验证已见版本和配置后提交完整替换，失败保持原配置或报告未知。
    def execute(self, params: dict) -> ToolHandlerOutcome:
        if params["catalog_revision"] != self.catalog_revision:
            return self._failure("stale_catalog", "PLUGIN_CATALOG_STALE", "not_started")
        existing = self.installation
        if existing is None or existing.manifest.plugin_id != params["plugin"]:
            return self._failure("plugin_missing", "TOOL_INVALID_ARGUMENTS", "not_started")
        try:
            content = read_plugin_source(params["source"], self.workspace, self.policy, max_bytes=PLUGIN_SETTINGS_BYTES)
            settings = canonical_plugin_settings(load_strict_json(content), existing.manifest.settings_schema)
        except (OSError, ValueError, RuntimeError, TypeError, RecursionError):
            return self._failure("invalid_settings_source", "TOOL_INVALID_ARGUMENTS", "not_started")
        try:
            result = self.store.configure(PluginConfigureRequest(
                existing.manifest.plugin_id, existing.package_sha256, self.operation_id, existing.revision, settings,
            ))
        except PluginInstallationError as exc:
            return self._failure(exc.reason, "TOOL_EXECUTION_FAILED",
                                 "unknown" if exc.commit_state == "unknown" else "failed", exc.commit_state)
        except Exception:  # noqa: BLE001 存储写入后意外异常不能推断未提交或重试
            return self._failure("configuration_unconfirmed", "TOOL_EXECUTION_FAILED", "unknown", "unknown")
        return ToolHandlerOutcome(PLUGIN_CONFIGURE_TOOL, True, "配置已保存，插件仍保持停用。",
                                  result_envelope={"plugin_configure": {
                                      "plugin_id": result.installation.manifest.plugin_id, "enabled": False,
                                      "revision": result.installation.revision, "configured": True,
                                      "outcome": result.outcome, "commit_state": result.commit_state,
                                  }})

    # LLM: 配置值、来源路径及值摘要不能进入原工具结果；真实提交分类由 Store 给出，不由异常正文推断。
    # 函数用途: 生成可查询且不泄露私有配置的失败回执。
    def _failure(self, reason: str, error_code: str, effect: str,
                 commit_state: str = "not_committed") -> ToolHandlerOutcome:
        return ToolHandlerOutcome(PLUGIN_CONFIGURE_TOOL, False, "插件配置未得到成功结果，请查询原请求。",
                                  error_code=error_code, effect_outcome=effect,
                                  result_envelope={"plugin_configure": {
                                      "reason": reason, "commit_state": commit_state,
                                  }})
