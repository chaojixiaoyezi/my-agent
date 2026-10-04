# LLM: 启用沿用原 HostCommand/ToolExecutor 与安装表；v8 仅在总开关、owner 和共用进程沙箱就绪后继续，拒绝须先于环境副作用。
#   老格式（v1–v6）由宿主冻结的授权上下文确认 R/W/N/E，确认/启动/发布复核固定授权，旧代真实释放后换代，restricted 缺 B7 拒启动。
# 模块用途: 在固定安装快照上安全准备并发布插件，私有值不进入回执；不将路径检查、报告存在或授权当作 OS 隔离证明。

from __future__ import annotations

import shutil
from dataclasses import asdict, dataclass, replace
from pathlib import Path

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
from .plugin_permissions.confirmation import permission_confirm_command
from .plugin_permissions.enable import (
    LegacyEnableContext,
    authorization_identity,
    prepare_enable_authorization,
    refresh_enable_authorization,
    require_enable_authorization,
    retire_for_enable,
    retirement_committed,
)
from .plugin_permissions.state import canonical_permission_json
from .plugin_runtime import PluginMCPClient
from .plugin_runtime_facts import (
    PluginRuntimeError,
    PluginRuntimeFacts,
    confirmation_code,
    confirmation_details,
    resolve_plugin_runtime,
)
from .plugin_sandbox import (
    PLUGIN_GATEWAY_PORT_ISOLATION_ERROR_CODE,
    PluginInterpreterPaths,
    PluginV8Sandbox,
    plugin_sandbox_problem,
    plugin_v8_sandbox_options,
)
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


# LLM: owner/binding/installation 由宿主冻结；v8 的强制沙箱、开关与本机 owner 门均在确认和准备副作用前检查。
# 类用途: 接入明确管理员启用，安全策略未就绪就结构化拒绝，不让插件环境留在 preparing。
# LLM: B7 启用策略：process_sandbox 来自配置 plugin_process_sandbox；events_enabled 是 M 线总开关；
#   events_owner_allowed 是当前 owner 是否本机管理员。三项都是宿主裁决的结构化事实，打包传给启用工具，避免参数过多。
#   老格式（v1–v6）的授权输入（arguments、沙箱默认档、宿主保护根）同份承载，工具内部组装 LegacyEnableContext。
# 类用途: 承载一次插件启用的安全策略判定输入。
@dataclass(frozen=True)
class PluginEnablePolicy:
    process_sandbox: bool = False
    events_enabled: bool = False
    events_owner_allowed: bool = False
    legacy_arguments: dict | None = None
    legacy_sandbox_default: bool = True
    legacy_forbidden_roots: tuple = ()


# LLM: 管理候选只消费宿主固定安装快照和结构化策略；启用预检与运行时复用 plugin_sandbox_spec，不另建平台规则。
#   老格式上下文由管理员宿主冻结，预览不改旧代，确认后沿原撤销链；restricted 缺 B7 不进入候选。
# 类用途: 校验启用门、沙箱与确认码后执行唯一的插件激活流程。
class PluginEnableTool(BaseTool):
    # LLM: never 仅免管理动作重复询问；只有未激活的旧版生成计划及资源声明。policy 带来 B7 的沙箱/总开关/owner 判定。
    #   老格式授权输入在构造时组装成 LegacyEnableContext，确认预先绑定下一 HostCommand，未确认不声明可执行资源。
    # 函数用途: 可启用的旧版包在领取前声明环境；关闭的 v8、内容包、已启用或缺失插件均不构造资源域。
    def __init__(self, owner, repository, binding, installation, catalog_revision,
                 *, policy: PluginEnablePolicy | None = None):
        policy = policy or PluginEnablePolicy()
        self.owner, self.repository, self.binding = owner, repository, binding
        self.installation, self.catalog_revision = installation, catalog_revision
        self.events_enabled, self.events_owner_allowed = policy.events_enabled, policy.events_owner_allowed
        # v8 事件/收紧插件（permissions 非空）强制进沙箱，不管全局 plugin_process_sandbox 开没开（B7 安全底座）。
        self._is_v8 = installation is not None and installation.manifest.permissions is not None
        # v1–v6 可执行包（permissions 为空、不是内容包）：宿主冻结的授权输入在这里组装成原授权上下文。
        self._is_legacy = (installation is not None and installation.manifest.permissions is None
                           and not installation.manifest.is_content_only)
        self.process_sandbox = policy.process_sandbox or self._is_v8
        self._verifier_consent = ""
        self.previous_cleanup = None
        self.target = self.plan = self.grant = None
        self.context = self._legacy_context(policy) if self._is_legacy else None
        # v8 与旧版可执行插件一样要构造运行计划（它们都有 entry、不是内容包）；老格式不管激活状态都要预测撤旧后的目标。
        needs_plan = installation is not None and ((self._is_v8 and installation.activation is None)
                                                   or self._is_legacy)
        self.runtime, self.runtime_error = _runtime_facts(installation) if needs_plan else (None, "")
        if needs_plan and not self.runtime_error:
            self._prepare_authorization(installation)
        keys = ("plugin", "catalog_revision", "workspace", "permission_digest")
        self.model_spec = ToolModelSpec(PLUGIN_ENABLE_TOOL, "启用已验证内容；可执行插件另行准备环境并核验工具目录。", {
            "type": "object", "properties": {
                **{key: {"type": "string"} for key in (*keys, "confirm", "authorization", "authorization_revision")},
                **{key: {"type": "array", "items": {"type": "string"}} for key in ("read_roots", "write_roots", "program_roots")},
                "network": {"type": "boolean"}},
            "required": list(keys), "additionalProperties": False,
        })
        declared = bool(self.plan) and (self.context is None or bool(self.context.arguments.get("authorization")))
        self.runtime_policy = ToolRuntimePolicy(
            effect_resolver=EffectResolverPolicy("dangerous"), approval_policy=ApprovalPolicy("never"),
            idempotency_policy=IdempotencyPolicy("operation"), mutates_workspace=False,
            resource_scopes=(ResourceScopePolicy("declared", static_scopes=(self.plan.resource_scope,))
                             if declared else ResourceScopePolicy("none")),
        )

    # LLM: 原请求重放由执行器完成；v8 按总开关关闭在确认前返回 not_started/not_committed，旧版失败保持原事实。
    #   老格式先核对权限码与授权身份（漂移另发下一确认请求），确认后才进入撤旧与启用。
    # 函数用途: 先拒绝未具安全底座的订阅包，再处理老格式授权与旧版确认，将实际提交状态交回原工具账。
    def execute(self, params: dict) -> ToolHandlerOutcome:
        failure = self._preflight_failure(params)
        if failure is not None:
            return failure
        if self.grant is not None:
            confirmation = self._permission_confirmation(params)
            if confirmation is not None:
                return confirmation
        elif self.runtime is not None:
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

    # LLM: 启用前只检查固定快照、宿主策略和本机沙箱能力；拒绝结果不准备环境，也不改变激活状态。
    # 函数用途: 把启用前置检查集中起来，保证安全拒绝都发生在确认与副作用之前。
    def _preflight_failure(self, params: dict) -> ToolHandlerOutcome | None:
        if params["catalog_revision"] != self.catalog_revision:
            return self._failure("stale_catalog", "PLUGIN_CATALOG_STALE", "not_started")
        if self.installation is None or self.installation.manifest.plugin_id != params["plugin"]:
            return self._failure("plugin_missing", "TOOL_INVALID_ARGUMENTS", "not_started")
        if self._is_v8 and not self.events_enabled:
            return self._failure("plugin_events_disabled", "TOOL_EXECUTION_FAILED", "not_started")
        if self._is_v8 and not self.events_owner_allowed:
            return self._failure("plugin_events_owner_not_allowed", "TOOL_EXECUTION_FAILED", "not_started")
        if self.runtime_error:
            return self._failure(self.runtime_error, "TOOL_EXECUTION_FAILED", "not_started")
        problem = self._sandbox_problem()
        if problem:
            return self._failure(problem, _sandbox_error_code(problem), "not_started")
        return None

    # LLM: 沙箱检查使用 v8 清单中的网络、解释器和隐藏根规格；不能因规格推导失败而退回普通沙箱。
    # 函数用途: 在启用准备开始前确认当前插件所需的进程沙箱可用。
    def _sandbox_problem(self) -> str:
        if self.plan is None:
            return ""
        v8_sandbox = None
        if self._is_v8:
            v8_sandbox, problem = plugin_v8_sandbox_options(
                self.owner.home_dir,
                network=bool(self.installation.manifest.permissions.network),
                interpreter=_v8_interpreter_paths(self.installation.manifest, self.runtime),
            )
            if problem:
                return problem
        if v8_sandbox is None:
            return plugin_sandbox_problem(self.process_sandbox, self.owner.home_dir)
        return plugin_sandbox_problem(self.process_sandbox, self.owner.home_dir, sandbox_policy=v8_sandbox)

    # LLM: 构造与执行间刷新 self.* 是有意的；旧码对不上新事实就重给完整预览，只有码/身份匹配最新授权才进入 _enable。
    # 函数用途: 让 _enable 消费用户最后确认的那份授权，不用构造时缓存，刷新无效事实不启动、不撤旧代。
    def _permission_confirmation(self, params: dict):
        if self.grant is None:
            return None
        try:
            # 跨方法刷新固定状态有意保持同源；下方码与身份双重比较失败时只能重新预览，不能自动采用新授权执行。
            self.runtime, self.target, self.plan, self.grant = refresh_enable_authorization(self.context, self.installation)
        except PluginInstallationError as exc:
            return self._failure(exc.reason, "TOOL_EXECUTION_FAILED", "not_started")
        confirmation = self._confirmation()
        if params.get("confirm") == confirmation["confirm_code"] and params.get("authorization") == confirmation["authorization_id"]:
            return None
        if params.get("authorization"):
            self.context = replace(self.context, arguments={**self.context.arguments, "authorization": ""})
            self.target, self.plan, self.grant = prepare_enable_authorization(self.context, self.installation, self.runtime)
            confirmation = self._confirmation()
        return _confirmation_required(confirmation)

    # LLM: 按包类型分流：老格式组装授权身份与预测 target 的计划；v8 只按未激活事实生成运行计划。
    # 函数用途: 为启用构造准备计划、授权与撤旧预测；失败原因收进 runtime_error 供 execute 结构化返回。
    def _prepare_authorization(self, installation) -> None:
        if not self._is_legacy:
            self.plan = plan_plugin_environment(installation, self.binding.request.operation_id,
                                                runtime_fingerprint=self.runtime.fingerprint if self.runtime else None)
            return
        try:
            self.target, self.plan, self.grant = prepare_enable_authorization(self.context, installation, self.runtime)
        except PluginInstallationError as exc:
            self.runtime_error = exc.reason
        except (OSError, ValueError):
            self.runtime_error = "legacy_permission_invalid"

    # LLM: 上下文只由管理宿主注入的冻结输入组装；owner/repo/binding 与工具构造参数同源，不重复传参。
    # 函数用途: 把 policy 里的老格式授权输入组装成 LegacyEnableContext。
    def _legacy_context(self, policy: PluginEnablePolicy) -> LegacyEnableContext:
        return LegacyEnableContext(self.owner, self.repository, self.binding,
                                   dict(policy.legacy_arguments or {}), self.process_sandbox,
                                   policy.legacy_sandbox_default, tuple(policy.legacy_forbidden_roots))

    # LLM: 激活 CAS、环境副作用、握手目录和退出确认按序发生；老格式先精确撤旧代并复核预测 target 与清理确认。
    #   原安装先复核 CAS，启动/发布重核固定授权，restricted 缺 B7 拒启动；候选只作验收，确认退出后才发布。
    # 函数用途: 在固定版本上完成启用；安装或权限漂移不发布，旧资源退出未知不开放新代。
    def _enable(self) -> dict:
        store, entry = PluginInstallStore(self.owner), self.installation
        if entry.manifest.is_content_only:
            return self._enable_content(store, entry)
        if self.plan is None:
            if entry.enabled and store.require_activation(entry.manifest.plugin_id, entry.activation_id) == entry:
                return {"plugin_id": entry.manifest.plugin_id, "activation_id": entry.activation_id,
                        "revision": entry.revision, "enabled": True, "outcome": "unchanged"}
            raise PluginInstallationError("activation_unsettled", "原插件激活尚未清理，不能准备新一代。")
        if self._is_legacy:
            if next((row for row in store.snapshot() if row.manifest.plugin_id == entry.manifest.plugin_id), None) != entry:
                raise PluginInstallationError("revision_conflict", "安装事实已变化，请重新取得完整预览。")
            self._require_authorization()
            entry, self.previous_cleanup = retire_for_enable(self.context, entry)
            if self.previous_cleanup is not None and (not self.previous_cleanup["cleanup_confirmed"] or not self.previous_cleanup["released"]):
                raise PluginInstallationError("previous_cleanup_unconfirmed", "旧代退出尚未确认，未开放新代。", commit_state="unknown")
            if entry != self.target:
                raise PluginInstallationError("revision_conflict", "清理后安装版本与确认不符。")
            if self.grant["mode"] == "restricted":
                raise PluginInstallationError("legacy_sandbox_pending", "统一 B7 底座尚未接入，未启动收紧插件。",
                                              commit_state="committed" if retirement_committed(self.installation, entry,
                                                  self.binding.request.operation_id) else "not_committed")
        package = inspect_plugin_package(store.package_bytes(entry))
        operation = PluginEnvironmentOperation.bind(self.owner, self.repository, self.binding, self.plan)
        preparing = (PluginActivation(self.plan, "preparing", permission_json=canonical_permission_json(self.grant))
                     if self._is_legacy else PluginActivation(self.plan, "preparing"))
        prepared = store.change_activation(PluginActivationRequest(self.plan.operation_id, entry.revision, preparing))
        self._require_authorization()
        prepare_plugin_environment(self.owner, package, operation)
        operation.authorize()
        tools, cleanup = self._candidate(operation, prepared.installation)
        self._require_authorization()
        operation.authorize()
        active = replace(prepared.installation.activation, phase="active", catalog_sha256=plugin_catalog_digest(entry.manifest))
        published = store.change_activation(PluginActivationRequest(self.plan.operation_id, prepared.installation.revision, active))
        return {"plugin_id": entry.manifest.plugin_id, "enabled": True, "activation_id": active.activation_id,
                "revision": published.installation.revision, "environment_ref": self.plan.environment_ref,
                "tools": [tool.model_spec.name for tool in tools], "commit_state": published.commit_state,
                **({"previous_cleanup": self.previous_cleanup} if self.previous_cleanup else {}),
                "candidate_cleanup": {"session_id": cleanup.record["session_id"], "confirmed": cleanup.confirmed,
                                      "terminations": [asdict(item) for item in cleanup.terminations]}}

    # LLM: 候选连接前及发现后均复核原授权；finally 精确停止本候选，未确认退出必须 UNKNOWN，不能发布。
    # 函数用途: 在固定权限仍成立时核验工具目录，不把候选连接当作已启用。
    def _candidate(self, operation, installation):
        self._require_authorization()
        client = PluginMCPClient(self.owner, installation, process_sandbox=self.process_sandbox)
        try:
            self._require_authorization()
            transport = client.start()
            tools = client.discover_tools(transport)
            self._require_authorization()
            operation.authorize()
        finally:
            cleanup = client.stop()
            if not cleanup.confirmed:
                raise EnvironmentPreparationError("candidate_cleanup_unconfirmed", started=True, exit_confirmed=False)
        return tools, cleanup

    # LLM: 老格式在候选启动与发布前复核固定授权；v8 没有这条授权账，保持原 B7 门。
    # 函数用途: 复核本次授权事实未变，变化时拒绝继续，不自动扩权或换代。
    def _require_authorization(self) -> None:
        if self._is_legacy:
            require_enable_authorization(self.context, self.installation, self.grant)

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

    # LLM: 老格式四维、策略、原安装/旧代、精确未来操作/新代及解释器进同一码，v6 原程序和文件事实一并绑定；
    #   v8 沿用宿主解析的运行时事实生成确认内容，不回显私有配置。
    # 函数用途: 为启用生成完整预览与确认码；老格式附确认命令，v8 保持原确认结构。
    def _confirmation(self) -> dict:
        package = inspect_plugin_package(PluginInstallStore(self.owner).package_bytes(self.installation))
        if self._is_legacy:
            details = {**self.grant, "authorization_id": authorization_identity(self.context).request_id,
                       "catalog_revision": self.catalog_revision,
                       "previous_activation_id": self.installation.activation_id,
                       "previous_installation_ref": self.installation.installation_ref}
            if self.runtime is not None:
                details["runtime"] = confirmation_details(self.installation.manifest, package.sha256, self.runtime,
                                                          inspect_plugin_files(package))
            details["confirm_code"] = confirmation_code(details)
            return {**details, "confirm_command": permission_confirm_command(details)}
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

    # LLM: 失败保存旧代清理事实，UNKNOWN 不洗成未发生；不回显配置或底层异常。
    # 函数用途: 让原请求区分新启用失败和旧代已撤销、退出未知。
    def _failure(self, reason, error_code, effect, commit_state="not_committed") -> ToolHandlerOutcome:
        return ToolHandlerOutcome(PLUGIN_ENABLE_TOOL, False, "插件启用尚未得到完整确认，请查询原请求。",
                                  error_code=error_code, effect_outcome=effect,
                                  result_envelope={PLUGIN_ENABLE_TOOL: {"reason": reason, "commit_state": commit_state,
                                      **({"previous_cleanup": self.previous_cleanup} if self.previous_cleanup else {})}})


# LLM: 只对尚未激活的 v6 非 Python 包解析运行时事实（读 PATH 与解释器文件，不执行）；失败原因交给 execute 结构化返回。
# 函数用途: 为启用工具取得本机运行时事实或失败原因；Python 包返回 (None, "")。
def _runtime_facts(installation) -> tuple[PluginRuntimeFacts | None, str]:
    if installation.manifest.entry is None:
        return None, ""
    try:
        return resolve_plugin_runtime(installation.manifest), ""
    except PluginRuntimeError as exc:
        return None, exc.reason


# LLM: Python/Node 解释器路径来自启用时解析并固定的 runtime，exec 别名与真实目标一起交给收窄读校验。
# 函数用途: 为 v8 运行时整理宿主已确认的解释器路径；无外部解释器的随包可执行入口返回 None。
def _v8_interpreter_paths(manifest, runtime: PluginRuntimeFacts | None) -> PluginInterpreterPaths | None:
    entry = manifest.entry
    if entry is None or entry.kind != "interpreter" or runtime is None or runtime.kind != "interpreter":
        return None
    candidate = shutil.which(runtime.interpreter_name) or runtime.interpreter_path
    return PluginInterpreterPaths(runtime.interpreter_name, Path(candidate), Path(runtime.interpreter_path))


# LLM: 只有 Linux network:true 的端口边界缺口使用专用 taxonomy code；其余启用拒绝保持原工具失败合同。
# 函数用途: 把沙箱拒绝原因映射成稳定的启用错误码。
def _sandbox_error_code(reason: str) -> str:
    if reason == "gateway_port_isolation_unavailable":
        return PLUGIN_GATEWAY_PORT_ISOLATION_ERROR_CODE
    return "TOOL_EXECUTION_FAILED"


# LLM: 启用前确认预览：结构化 state 供回执层识别，专用错误码不是参数错误；v6 程序确认与能力包检查程序确认共用。
# 函数用途: 生成“需要先确认”的启用回执，不提交任何激活。
def _confirmation_required(confirmation: dict) -> ToolHandlerOutcome:
    return ToolHandlerOutcome(
        PLUGIN_ENABLE_TOOL, False, "启用前需要你确认这个插件会运行的程序。",
        error_code="PLUGIN_CONFIRMATION_REQUIRED", effect_outcome="not_started",
        result_envelope={PLUGIN_ENABLE_TOOL: {"reason": "confirmation_required", "state": "confirmation_required",
                                              "commit_state": "not_committed", "confirmation": confirmation}})
