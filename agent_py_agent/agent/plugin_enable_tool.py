# LLM: 启用复用原 HostCommand/ToolExecutor 和安装表；只有新计划声明候选资源，联测重复启用和缺失拒绝，不建立第二套账。
# 模块用途: 验证隔离环境及工具目录后发布贡献；非 Python 插件先给用户看确认回执、凭确认码才继续；已启用版本保持原代，不创建空资源声明或新候选，私有值不进回执。

from __future__ import annotations

from dataclasses import asdict, replace

from .capability_verifier_consent import (
    verifier_confirmation_code,
    verifier_confirmation_details,
    verifier_consent_sha256,
)
from .plugin_activation import PLUGIN_ENABLE_TOOL, PluginActivationRequest
from .plugin_activation_record import PluginActivation, plugin_catalog_digest
from .plugin_content_activation import PluginContentActivation
from .plugin_environment import prepare_plugin_environment
from .plugin_environment_plan import plan_plugin_environment
from .plugin_environment_process import EnvironmentPreparationError, PluginEnvironmentOperation
from .plugin_files_environment import inspect_plugin_files
from .plugin_install_store import PluginInstallStore
from .plugin_installation import PluginInstallationError
from .plugin_package import inspect_plugin_package
from .plugin_runtime import PluginMCPClient
from .plugin_runtime_facts import (
    PluginRuntimeError,
    PluginRuntimeFacts,
    confirmation_code,
    confirmation_details,
    resolve_plugin_runtime,
)
from .plugin_sandbox import plugin_sandbox_problem
from .tooling.background_process_launch import BackgroundLaunchError
from .tooling.mcp_client import MCPError
from .tooling.models import (
    ApprovalPolicy,
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ResourceScopePolicy,
    ToolHandlerOutcome,
    ToolModelSpec,
    ToolRuntimePolicy,
)


# LLM: owner/binding/installation 由宿主冻结；构造只为新激活声明计划，无计划路径不得创建资源，保持原幂等链。
# 类用途: 接入明确管理员启用，对已启用版本保持不变，对准备失败继续拒绝使用。
class PluginEnableTool(BaseTool):
    # LLM: never 仅免管理动作重复询问；只有未激活版本生成计划及资源声明，无计划分支只核对原激活或返回拒绝。
    #   process_sandbox 来自管理上下文的配置 plugin_process_sandbox，决定候选进程是否套平台沙箱。
    # 函数用途: 可执行包在领取前声明环境；内容包、已启用或缺失插件均不构造虚假资源域。
    def __init__(self, owner, repository, binding, installation, catalog_revision, *, process_sandbox: bool = False):
        self.owner, self.repository, self.binding = owner, repository, binding
        self.installation, self.catalog_revision = installation, catalog_revision
        self.process_sandbox = process_sandbox
        self._verifier_consent = ""
        pending = (installation is not None and installation.activation is None
                   and not installation.manifest.is_content_only)
        self.runtime, self.runtime_error = _runtime_facts(installation) if pending else (None, "")
        self.plan = (plan_plugin_environment(installation, binding.request.operation_id,
                                             runtime_fingerprint=self.runtime.fingerprint if self.runtime else None)
                     if pending and not self.runtime_error else None)
        keys = ("plugin", "catalog_revision", "workspace", "permission_digest")
        self.model_spec = ToolModelSpec(PLUGIN_ENABLE_TOOL, "启用已验证内容；可执行插件另行准备环境并核验工具目录。", {
            "type": "object", "properties": {**{key: {"type": "string"} for key in keys}, "confirm": {"type": "string"}},
            "required": list(keys), "additionalProperties": False,
        })
        self.runtime_policy = ToolRuntimePolicy(
            effect_resolver=EffectResolverPolicy("dangerous"), approval_policy=ApprovalPolicy("never"),
            idempotency_policy=IdempotencyPolicy("operation"), mutates_workspace=False,
            resource_scopes=(ResourceScopePolicy("declared", static_scopes=(self.plan.resource_scope,))
                             if self.plan else ResourceScopePolicy("none")),
        )

    # LLM: 原请求重放由执行器完成；失败保留原准备事实与资源，不换 operation 或重建候选来掩盖未知结果。
    # 函数用途: 处理版本/缺失拒绝、非 Python 插件的用户确认和完整启用，将实际提交状态交回原工具账。
    def execute(self, params: dict) -> ToolHandlerOutcome:
        if params["catalog_revision"] != self.catalog_revision:
            return self._failure("stale_catalog", "PLUGIN_CATALOG_STALE", "not_started")
        if self.installation is None or self.installation.manifest.plugin_id != params["plugin"]:
            return self._failure("plugin_missing", "TOOL_INVALID_ARGUMENTS", "not_started")
        if self.runtime_error:
            return self._failure(self.runtime_error, "TOOL_EXECUTION_FAILED", "not_started")
        if self.plan is not None and plugin_sandbox_problem(self.process_sandbox, self.owner.home_dir):
            # 沙箱开关已开但本机沙箱不可用：在准备环境、启动候选之前拒绝，不退回无沙箱
            return self._failure("sandbox_unavailable", "TOOL_EXECUTION_FAILED", "not_started")
        if self.runtime is not None:
            confirmation = self._confirmation()
            if params.get("confirm") != confirmation["confirm_code"]:
                return _confirmation_required(confirmation)
        if self._needs_verifier_consent():
            # 能力包 v2：声明了检查程序的内容包，启用即同意宿主自动运行这些包内程序，必须先给管理员看并凭确认码继续
            details = verifier_confirmation_details(self.installation.manifest, self.installation.package_sha256)
            if params.get("confirm") != verifier_confirmation_code(details):
                return _confirmation_required({**details, "confirm_code": verifier_confirmation_code(details)})
            self._verifier_consent = verifier_consent_sha256(details)
        try:
            result = self._enable()
        except PluginInstallationError as exc:
            return self._failure(exc.reason, "TOOL_EXECUTION_FAILED",
                                 "unknown" if exc.commit_state == "unknown" else "failed", exc.commit_state)
        except EnvironmentPreparationError as exc:
            return self._failure(exc.reason, "TOOL_EXECUTION_FAILED", "failed" if exc.exit_confirmed else "unknown")
        except MCPError:
            return self._failure("plugin_endpoint_failed", "TOOL_EXECUTION_FAILED", "failed")
        except BackgroundLaunchError as exc:
            return self._failure("plugin_launch_failed", "TOOL_EXECUTION_FAILED",
                                 "failed" if exc.cleanup_confirmed else "unknown")
        except Exception:  # noqa: BLE001 原执行链保留未确认结果，不能按异常文本推断已清理或重新准备
            return self._failure("activation_unconfirmed", "TOOL_EXECUTION_FAILED", "unknown", "unknown")
        return ToolHandlerOutcome(PLUGIN_ENABLE_TOOL, True, "包版本已启用，新任务将读取该代贡献。",
                                  result_envelope={PLUGIN_ENABLE_TOOL: result})

    # LLM: 激活 CAS、环境副作用、握手目录和退出确认按序发生；候选只作验收，新运行通过原 Registry 按同代创建业务连接。
    # 函数用途: 在固定版本上完成启用；确认候选退出后才发布，不让冷管理入口留下临时服务。
    def _enable(self) -> dict:
        store, entry = PluginInstallStore(self.owner), self.installation
        if entry.manifest.is_content_only:
            return self._enable_content(store, entry)
        if self.plan is None:
            if entry.enabled and store.require_activation(entry.manifest.plugin_id, entry.activation_id) == entry:
                return {"plugin_id": entry.manifest.plugin_id, "activation_id": entry.activation_id,
                        "revision": entry.revision, "enabled": True, "outcome": "unchanged"}
            raise PluginInstallationError("activation_unsettled", "原插件激活尚未清理，不能准备新一代。")
        package = inspect_plugin_package(store.package_bytes(entry))
        operation = PluginEnvironmentOperation.bind(self.owner, self.repository, self.binding, self.plan)
        prepared = store.change_activation(PluginActivationRequest(
            self.plan.operation_id, entry.revision, PluginActivation(self.plan, "preparing"),
        ))
        prepare_plugin_environment(self.owner, package, operation)
        operation.authorize()
        client = PluginMCPClient(self.owner, prepared.installation, process_sandbox=self.process_sandbox)
        try:
            transport = client.start()
            tools = client.discover_tools(transport)
            operation.authorize()
        finally:
            cleanup = client.stop()
            if not cleanup.confirmed:
                raise EnvironmentPreparationError("candidate_cleanup_unconfirmed", started=True, exit_confirmed=False)
        operation.authorize()
        active = replace(prepared.installation.activation, phase="active", catalog_sha256=plugin_catalog_digest(entry.manifest))
        published = store.change_activation(PluginActivationRequest(self.plan.operation_id, prepared.installation.revision, active))
        return {"plugin_id": entry.manifest.plugin_id, "enabled": True, "activation_id": active.activation_id,
                "revision": published.installation.revision, "environment_ref": self.plan.environment_ref,
                "tools": [tool.model_spec.name for tool in tools], "commit_state": published.commit_state,
                "candidate_cleanup": {"session_id": cleanup.record["session_id"], "confirmed": cleanup.confirmed,
                                      "terminations": [asdict(item) for item in cleanup.terminations]}}

    # LLM: 内容启用沿原 operation 与安装 CAS，先复验固定 blob，再直接发布无进程代；不调用环境、沙箱或 MCP。
    # 函数用途: 启用纯内容能力包，重复启用只核对同一代，撤销未释放时拒绝重开。
    def _enable_content(self, store, entry) -> dict:
        store.package_bytes(entry)
        if entry.activation is not None:
            if entry.enabled and store.require_activation(entry.manifest.plugin_id, entry.activation_id) == entry:
                return {"plugin_id": entry.manifest.plugin_id, "activation_id": entry.activation_id,
                        "revision": entry.revision, "enabled": True, "outcome": "unchanged", "runtime": "none"}
            raise PluginInstallationError("activation_unsettled", "原内容激活尚未释放。")
        operation_id = self.binding.request.operation_id
        active = PluginContentActivation(operation_id, entry.manifest.plugin_id, entry.package_sha256,
                                         entry.revision, entry.settings_revision,
                                         verifier_consent_sha256=self._verifier_consent)
        published = store.change_activation(PluginActivationRequest(operation_id, entry.revision, active))
        return {"plugin_id": entry.manifest.plugin_id, "enabled": True, "activation_id": active.activation_id,
                "revision": published.installation.revision, "outcome": published.outcome,
                "commit_state": published.commit_state, "runtime": "none"}

    # LLM: 只读安装快照与宿主已解析的运行时事实，不执行任何程序；确认码由这些事实生成，包或解释器变化即作废。
    # 函数用途: 生成非 Python 插件启用前给用户看的确认内容与确认码。
    def _confirmation(self) -> dict:
        package = inspect_plugin_package(PluginInstallStore(self.owner).package_bytes(self.installation))
        details = confirmation_details(self.installation.manifest, package.sha256, self.runtime, inspect_plugin_files(package))
        return {**details, "confirm_code": confirmation_code(details)}

    # LLM: 只对尚未激活、声明了检查程序的纯内容包要求执行同意；已激活版本走原“保持不变”分支，不重复询问。
    # 函数用途: 判断这次启用是否需要检查程序的执行确认。
    def _needs_verifier_consent(self) -> bool:
        entry = self.installation
        capability = getattr(entry.manifest, "capability", None) if entry is not None else None
        verification = getattr(capability, "verification", None)
        return (entry is not None and entry.activation is None and entry.manifest.is_content_only
                and verification is not None and verification.runs_package_code)

    # LLM: 回执只包含稳定原因和提交分类，不回显设置、安装日志、候选路径或底层错误正文。
    # 函数用途: 让原请求状态查询保留启用失败与未知的区别。
    def _failure(self, reason, error_code, effect, commit_state="not_committed") -> ToolHandlerOutcome:
        return ToolHandlerOutcome(PLUGIN_ENABLE_TOOL, False, "插件启用尚未得到完整确认，请查询原请求。",
                                  error_code=error_code, effect_outcome=effect,
                                  result_envelope={PLUGIN_ENABLE_TOOL: {"reason": reason, "commit_state": commit_state}})


# LLM: 只对尚未激活的 v6 非 Python 包解析运行时事实（读 PATH 与解释器文件，不执行）；失败原因交给 execute 结构化返回。
# 函数用途: 为启用工具取得本机运行时事实或失败原因；Python 包返回 (None, "")。
def _runtime_facts(installation) -> tuple[PluginRuntimeFacts | None, str]:
    if installation.manifest.entry is None:
        return None, ""
    try:
        return resolve_plugin_runtime(installation.manifest), ""
    except PluginRuntimeError as exc:
        return None, exc.reason


# LLM: 启用前确认预览：结构化 state 供回执层识别，专用错误码不是参数错误；v6 程序确认与能力包检查程序确认共用。
# 函数用途: 生成“需要先确认”的启用回执，不提交任何激活。
def _confirmation_required(confirmation: dict) -> ToolHandlerOutcome:
    return ToolHandlerOutcome(
        PLUGIN_ENABLE_TOOL, False, "启用前需要你确认这个插件会运行的程序。",
        error_code="PLUGIN_CONFIRMATION_REQUIRED", effect_outcome="not_started",
        result_envelope={PLUGIN_ENABLE_TOOL: {"reason": "confirmation_required", "state": "confirmation_required",
                                              "commit_state": "not_committed", "confirmation": confirmation}})
