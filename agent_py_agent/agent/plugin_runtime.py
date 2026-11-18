# LLM: 插件连接沿原 MCP 客户端和托管资源账，缓存只保存固定连接的已验代理；安装表仍是唯一激活权威。
# 模块用途: 从已准备环境构造隔离服务，完整核对工具声明，并为原执行器生成固定代次的工具。

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import re
import threading
import time
import zipfile
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus

from .attempt.sandbox import SandboxUnavailableError
from .capability._agents_contract_fields import AgentsContractError
from .capability.agents_source_processes import (
    process_source_change_affects,
    process_source_configurations,
    process_source_configurations_current,
    register_source_registry,
    source_process_boundary,
)
from .common.nofollow_fs import open_directory_beneath
from .common.strict_json import load_strict_json
from .gateway_parts.process_control import is_pid_alive
from .plugin_activation_record import installation_tool_declarations, pinned_catalog_json_from_tools
from .plugin_activation_ref import PluginActivationRef
from .plugin_entry import FILES_DIRECTORY
from .plugin_host_api import HOST_API_READ, issue_host_api_env
from .plugin_install_store import PluginInstallStore
from .plugin_installation import PluginInstallation
from .plugin_manifest import PluginPackageError, PluginToolDeclaration, canonical_plugin_settings
from .plugin_package import (
    PackageReadLimits,
    archive_path_key,
    inspect_plugin_package,
    read_zip_member,
)
from .plugin_permissions.records import validate_activation_permission
from .plugin_permissions.sandbox import LegacyLaunchDenied, legacy_launch_policy
from .plugin_runtime_facts import verified_runtime_command
from .plugin_sandbox import (
    SANDBOX_TMP_DIRECTORY,
    PluginInterpreterPaths,
    PluginV8Sandbox,
    plugin_sandbox_problem,
    plugin_sandbox_spec,
    plugin_v8_sandbox_options,
    sandboxed_plugin_argv,
)
from .plugin_wheels import inspect_plugin_wheels
from .tooling.input_schema import canonicalize_tool_input_schema
from .tooling.mcp_client import (
    MCPError,
    MCPServerConfig,
    MCPStdioClient,
    MCPToolInfo,
    sanitize_credentials,
)
from .tooling.mcp_registration import (
    MCPProxyTool,
    build_proxy_tool,
    disconnect_failed_mcp_transport,
    sanitize_name_component,
)
from .tooling.models import (
    ResourceScopePolicy,
    ToolAvailability,
    ToolHandlerOutcome,
    ToolInvocationContext,
)
from .tooling.observation_binding import ObservationBinding
from .tooling.process_session_store import ProcessSessionStore, process_session_store_root
from .user_space.task_credential_storage import CredentialInputError
from .workspace_read_context import WORKSPACE_READ_EXTENSION, WORKSPACE_READ_VERSION
from .workspace_write_context import WORKSPACE_WRITE_EXTENSION, WORKSPACE_WRITE_VERSION

# 能力包握手等待上限（秒）；仍在来源边界内，D9 后续再缩小锁的持有范围。
PACK_MCP_CONNECT_TIMEOUT_SECONDS: float = float("10.0")
CREDENTIAL_FILL_EXTENSION = "credential_fill"
CREDENTIAL_FILL_VERSION = "1"
_CREDENTIAL_REF_RE = re.compile(r"cred_[0-9a-f]{32}\Z")
# 单个插件代理实例最多保留的携卡浏览器会话绑定数上限。
_MAX_CREDENTIAL_SESSION_BINDINGS_COUNT = 64


# LLM: 仅解析能力包连接阶段的宿主预算；无效、非有限或非正值保留旧值，不扩展外层 deadline。
# 函数用途: 读取可选配置中的能力包 MCP 连接超时，返回有限正浮点秒数。
def _pack_connect_timeout(config: object | None) -> float:
    value = getattr(config, "plugin_pack_mcp_connect_timeout_seconds", PACK_MCP_CONNECT_TIMEOUT_SECONDS)
    try:
        timeout = float(value) if type(value) in (int, float) else 0.0
    except OverflowError:
        timeout = 0.0
    return timeout if math.isfinite(timeout) and timeout > 0 else PACK_MCP_CONNECT_TIMEOUT_SECONDS


# LLM: 展示名由完整大小写敏感身份派生，截断只作用于可读部分；发布还必须检测实际名称冲突，不能覆盖核心工具。
# 函数用途: 为普通模型调用和显式插件命令生成同一个稳定工具名。
def plugin_tool_name(plugin_id: str, tool_name: str) -> str:
    parts = []
    for value in (plugin_id, tool_name):
        parts.append(sanitize_name_component(value)[:16] + "_" + hashlib.sha256(value.encode()).hexdigest()[:8])
    return "plugin__" + "__".join(parts)


# LLM: 代理继承原执行/结果链，补固定激活可用性和能力协商的逐次元数据；不追随新连接或更改共享 cwd。
# 类用途: 将插件接入原权限与撤销链，并把已冻结读取范围交给明确支持的插件。
class PluginProxyTool(MCPProxyTool):
    # LLM: 激活合同只存在于 PluginMCPClient（activation_ref）；用普通 MCPStdioClient 组装的代理（工作区上下文等测试夹具）
    #   没有该合同，不做激活复核、只走原 MCP 连接检查——不是放宽撤销门，因为产品里插件代理一律由 PluginMCPClient 发现并持有
    #   activation_ref。有合同时任何 OSError/ValueError（表坏、撤销、换代）都按已撤销处理，不追随最新安装。
    # 函数用途: 统一目录可用性、审批前后复核、发送前三处的激活复核判定。
    def _activation_revoked(self) -> bool:
        ref = getattr(self.client, "activation_ref", None)
        if ref is None:
            return False
        try:
            ref.require()
        except (OSError, ValueError):
            return True
        return False

    # LLM: 配置和代次只取固定 client；原表坏、撤销或换代都按不可用处理，不追随最新安装。
    # 函数用途: 只读检查原插件和已握手连接能否进入本轮目录。
    def availability(self) -> ToolAvailability:
        ref = getattr(self.client, "activation_ref", None)
        if ref is not None and not ref.available():
            return ToolAvailability.unavailable("原插件已停用或激活不可用")
        return super().availability()

    # LLM: 先复核原激活（一次有界安装表读取），失效报 PLUGIN_ACTIVATION_UNAVAILABLE；再复核连接内存状态。不启动进程、不追随新代次。
    # 函数用途: 审批前/批准后执行前复核原插件是否仍启用且连接仍在。
    def precheck_availability(self) -> ToolAvailability:
        ref = getattr(self.client, "activation_ref", None)
        if ref is not None and not ref.available():
            return ToolAvailability.unavailable("原插件已停用或激活不可用", error_code="PLUGIN_ACTIVATION_UNAVAILABLE")
        return super().precheck_availability()

    # 函数用途: 校验 browser-lite.fill 的 value 与 credential_ref+field 严格二选一及选择器/候选存在性。
    def validate_invocation(
        self,
        params: dict[str, Any],
        context: ToolInvocationContext,
    ) -> ToolHandlerOutcome | None:
        _ = context
        if not self._is_browser_lite_fill():
            return None
        return self._validate_browser_fill_params(params or {})

    def _is_browser_lite_fill(self) -> bool:
        manifest = getattr(getattr(self.client, "installation", None), "manifest", None)
        return getattr(manifest, "plugin_id", "") == "browser-lite" and self.remote_tool == "fill"

    def _validate_browser_fill_params(self, params: Mapping[str, Any]) -> ToolHandlerOutcome | None:
        has_value = "value" in params
        has_ref = "credential_ref" in params
        has_field = "field" in params
        if has_value and (has_ref or has_field):
            return ToolHandlerOutcome(
                self.model_spec.name,
                False,
                '{"error": "参数互斥冲突：value 与 credential_ref/field 严格二选一，不能同时传入。"}',
                error_code="TOOL_INVALID_ARGUMENTS",
                effect_outcome="not_started",
                next_action="普通字面值请只传 value；凭据卡填表请只传 credential_ref 与 field（如 field='secret'），不要混用。",
            )
        if not has_value:
            if has_ref and not has_field:
                return ToolHandlerOutcome(
                    self.model_spec.name,
                    False,
                    '{"error": "缺少参数 field：使用 credential_ref 填表时必须同时提供 field（常规账号密码卡为 username 或 secret）。"}',
                    error_code="TOOL_INVALID_ARGUMENTS",
                    effect_outcome="not_started",
                    next_action="请补充 field 参数（内置账号密码卡的密码键为 'secret'，账号键为 'username'）。",
                )
            if has_field and not has_ref:
                return ToolHandlerOutcome(
                    self.model_spec.name,
                    False,
                    '{"error": "缺少参数 credential_ref：传入 field 时必须同时提供有效的 credential_ref（cred_<32位十六进制>）。"}',
                    error_code="TOOL_INVALID_ARGUMENTS",
                    effect_outcome="not_started",
                    next_action="凭据填表须同时传入 credential_ref 与 field；若是普通非秘密文本，请改用 value 参数。",
                )
            if not has_ref and not has_field:
                return ToolHandlerOutcome(
                    self.model_spec.name,
                    False,
                    '{"error": "缺少填写内容参数：请提供 value（允许空串），或同时提供 credential_ref 与 field。"}',
                    error_code="TOOL_INVALID_ARGUMENTS",
                    effect_outcome="not_started",
                    next_action="请传入 value（普通文本）或 credential_ref + field（宿主凭据卡字段）。",
                )
            ref_text = params.get("credential_ref")
            field_text = params.get("field")
            if not isinstance(ref_text, str) or not _CREDENTIAL_REF_RE.fullmatch(ref_text):
                return ToolHandlerOutcome(
                    self.model_spec.name,
                    False,
                    '{"error": "参数 credential_ref 格式无效，应为 cred_<32位小写十六进制>。"}',
                    error_code="TOOL_INVALID_ARGUMENTS",
                    effect_outcome="not_started",
                    next_action="请使用 request_credentials 返回或发现的真实 credential_ref，不要传入明文或占位符。",
                )
            if not isinstance(field_text, str) or not field_text.strip() or field_text != field_text.strip():
                return ToolHandlerOutcome(
                    self.model_spec.name,
                    False,
                    '{"error": "参数 field 无效，必须是非空凭据字段键名（如 username 或 secret）。"}',
                    error_code="TOOL_INVALID_ARGUMENTS",
                    effect_outcome="not_started",
                    next_action="请填写凭据卡声明的精确字段名（内置账号密码卡密码键为 'secret'）。",
                )
        elif not isinstance(params.get("value"), str):
            return ToolHandlerOutcome(
                self.model_spec.name,
                False,
                '{"error": "参数 value 必须是字符串（允许空串）。"}',
                error_code="TOOL_INVALID_ARGUMENTS",
                effect_outcome="not_started",
                next_action="请将 value 修正为字符串。",
            )
        selector = params.get("selector")
        candidate_id = params.get("candidate_id")
        if not (isinstance(selector, str) and selector) and not (isinstance(candidate_id, str) and candidate_id.strip()):
            return ToolHandlerOutcome(
                self.model_spec.name,
                False,
                '{"error": "缺少定位参数：请提供 selector，或提供上一次 read 返回的 candidate_id。"}',
                error_code="TOOL_INVALID_ARGUMENTS",
                effect_outcome="not_started",
                next_action="请提供唯一匹配表单控件的 CSS selector 或有效的 candidate_id。",
            )
        return None

    # LLM: 免审批调用不经过执行器复核，而 MCPProxyTool 先看连接再做发送准入：停用把连接关掉后，旧快照调用会先撞上
    #   MCP_CONNECTION_CLOSED（映射为可重试的 TOOL_EXECUTION_FAILED），结果码随清理快慢摆动。这里在发送前先鲜活复核原激活，
    #   撤销一律报 TOOL_UNAVAILABLE、effect_outcome=not_started，与审批前/批准后复核同一事实源；
    #   这是生命周期第 6 条"旧快照在执行门检查撤销"的落点。不启动进程、不追随新代次。观察三件套在
    #   tooling/observation_binding.ObservationBinding（与 MCPProxyTool 共用），这里只按声明构造绑定。
    # 函数用途: 停用后的插件调用固定报不可用且未执行；有观察声明的工具走共用的观察执行链，并接入 C7 两阶段凭据填表。
    def _execute(self, params: dict[str, object], context: ToolInvocationContext | None) -> ToolHandlerOutcome:
        if self._activation_revoked():
            return ToolHandlerOutcome(
                self.model_spec.name, False, '{"error": "原插件已停用或激活不可用"}',
                error_code="TOOL_UNAVAILABLE", reported_error_code="PLUGIN_ACTIVATION_UNAVAILABLE",
                effect_outcome="not_started",
            )
        if self.observation_binding is None:
            return self._send_plugin_call(params, context, None)
        return self.observation_binding.execute(params, context, self._send_plugin_call)

    def _send_plugin_call(
        self,
        params: dict[str, Any],
        context: ToolInvocationContext | None,
        extra_meta: dict[str, object] | None,
    ) -> ToolHandlerOutcome:
        call_params = params or {}
        if "credential_ref" in call_params or "field" in call_params:
            if not self._is_browser_lite_fill():
                return ToolHandlerOutcome(
                    self.model_spec.name,
                    False,
                    '{"error": "仅宿主核准的内置 browser-lite fill 工具支持凭据引用填表，第三方或其它插件无权使用凭据。"}',
                    error_code="CREDENTIAL_NOT_AUTHORIZED",
                    effect_outcome="not_started",
                    next_action="请改用已核准的内置 browser-lite fill、web_fetch(credential_ref=...) 或同步 run_command(credential_ref=...)。",
                )
            return self._execute_credential_fill(call_params, context, extra_meta)
        if self._is_browser_lite_fill():
            invalid = self._validate_browser_fill_params(call_params)
            if invalid is not None:
                return invalid
        if not self._negotiated(CREDENTIAL_FILL_EXTENSION, CREDENTIAL_FILL_VERSION):
            return self._execute_with_meta(call_params, context, extra_meta)
        session_meta, early_error, variants = self._prepare_browser_session_meta(context)
        if early_error is not None:
            return early_error
        merged_meta = extra_meta
        if session_meta is not None:
            merged_meta = {**(extra_meta or {}), CREDENTIAL_FILL_EXTENSION: session_meta}
        try:
            outcome = self._execute_with_meta(call_params, context, merged_meta)
            scrubbed = _scrub_plugin_outcome(outcome, context=context, extra_variants=variants)
        finally:
            variants = ()
        if not scrubbed.ok:
            if self.remote_tool != "open":
                parsed_err = _parse_remote_json_result(outcome.output)
                if str((parsed_err or {}).get("code") or "") == "NO_PAGE":
                    _, scope_key = _extract_session_scope(getattr(context, "credential_use", None))
                    if scope_key != ("", "", ""):
                        _forget_credential_session(self.client, scope_key)
            return _normalize_browser_session_remote_error(scrubbed)
        return scrubbed

    def _revoke_remote_browser_session(
        self,
        scope_payload: dict[str, object],
        scope_key: tuple[str, str, str],
    ) -> None:
        _forget_credential_session(self.client, scope_key)
        revoked_meta = {
            "version": CREDENTIAL_FILL_VERSION,
            "phase": "session",
            "session_scope": scope_payload,
            "session_revoked": True,
        }
        try:
            self.client.call_tool(self.remote_tool, {}, request_meta={CREDENTIAL_FILL_EXTENSION: revoked_meta})
        except MCPError:
            pass

    def _prepare_browser_session_meta(
        self,
        context: ToolInvocationContext | None,
    ) -> tuple[dict[str, object] | None, ToolHandlerOutcome | None, tuple[str, ...]]:
        credential_use = getattr(context, "credential_use", None)
        if credential_use is None:
            if _has_credential_sessions(self.client):
                return None, ToolHandlerOutcome(
                    self.model_spec.name,
                    False,
                    '{"error": "当前浏览器存在已绑定凭据的受保护会话，拒绝缺少宿主会话凭据上下文的访问。"}',
                    error_code="CREDENTIAL_NOT_AUTHORIZED",
                    effect_outcome="not_started",
                    next_action="请在原授权用户与原会话内经标准工具执行器访问该浏览器会话，或另启独立授权会话。",
                ), ()
            return None, None, ()
        scope_payload, scope_key = _extract_session_scope(credential_use)
        if scope_key == ("", "", ""):
            if _has_credential_sessions(self.client):
                return None, ToolHandlerOutcome(
                    self.model_spec.name,
                    False,
                    '{"error": "当前浏览器含受保护凭据会话，拒绝缺少有效 owner/actor/thread 绑定的调用。"}',
                    error_code="CREDENTIAL_NOT_AUTHORIZED",
                    effect_outcome="not_started",
                    next_action="请在原授权会话内执行操作，不能无绑定接管已登录页面。",
                ), ()
            return None, None, ()
        if self.remote_tool == "close":
            _forget_credential_session(self.client, scope_key)
            return {"version": CREDENTIAL_FILL_VERSION, "phase": "session", "session_scope": scope_payload}, None, ()
        bound = _get_credential_session(self.client, scope_key)
        if bound is None:
            if not scope_payload.get("active", False):
                if _has_credential_sessions(self.client):
                    return None, ToolHandlerOutcome(
                        self.model_spec.name,
                        False,
                        '{"error": "当前运行作用域已失效，无法访问受保护浏览器环境。"}',
                        error_code="CREDENTIAL_NOT_AUTHORIZED",
                        effect_outcome="not_started",
                        next_action="请回到当前活动会话重新执行。",
                    ), ()
                return None, None, ()
            return {"version": CREDENTIAL_FILL_VERSION, "phase": "session", "session_scope": scope_payload}, None, ()
        # 已绑定凭据的会话：每次后续 open/read/click/fill 前通过 C1 鲜活复核凭据未撤销、未过期、未换代且消费者仍匹配。
        ref_str = str(bound["credential_ref"])
        try:
            current, actual_consumer = self._verify_browser_lite_consumer(ref_str, require_fill_tool=False)
            if actual_consumer != bound["consumer"]:
                raise CredentialInputError(
                    "CREDENTIAL_NOT_AUTHORIZED",
                    "browser-lite 安装或激活代次已变化，旧浏览器会话不再继承原凭据授权。",
                    stage="browser_session_recheck",
                    effect_outcome="not_started",
                )
            _ = current
            with credential_use(
                ref_str,
                consumer="browser",
                target_url=str(bound["origin"]),
                expected_generation=int(bound["generation"]),
                browser_consumer=actual_consumer,
            ) as material:
                variants = _credential_variants_from_values(material.values.values())
        except (CredentialInputError, PermissionError, ValueError, OSError) as exc:
            self._revoke_remote_browser_session(scope_payload, scope_key)
            code = exc.code if isinstance(exc, CredentialInputError) else "CREDENTIAL_INPUT_UNAVAILABLE"
            return None, ToolHandlerOutcome(
                self.model_spec.name,
                False,
                json.dumps({"error": f"受保护浏览器会话绑定的凭据已失效或撤销，浏览器会话已关闭并清理：{exc}"}, ensure_ascii=False),
                error_code=code,
                effect_outcome="not_started",
                next_action="已绑定凭据已失效或被撤销，受保护会话已回收；请用 request_credentials(operation='status') 核对或由本人更新凭据。",
            ), ()
        return {
            "version": CREDENTIAL_FILL_VERSION,
            "phase": "session",
            "session_scope": scope_payload,
            "credential_binding": {
                "credential_ref": ref_str,
                "generation": int(bound["generation"]),
            },
        }, None, variants

    def _execute_credential_fill(
        self,
        params: dict[str, Any],
        context: ToolInvocationContext | None,
        extra_meta: dict[str, object] | None,
    ) -> ToolHandlerOutcome:
        invalid = self._validate_browser_fill_params(params)
        if invalid is not None:
            return invalid
        credential_ref = str(params["credential_ref"])
        field_name = str(params["field"])
        try:
            current_installation, actual_consumer = self._verify_browser_lite_consumer(credential_ref)
        except CredentialInputError as exc:
            return ToolHandlerOutcome(
                self.model_spec.name,
                False,
                json.dumps({"error": str(exc)}, ensure_ascii=False),
                error_code=exc.code,
                effect_outcome="not_started",
                result_envelope={"credential_error": {"stage": exc.stage, "error_code": exc.code}},
                next_action=(
                    f"当前浏览器插件未获准执行凭据填表（effect_outcome=not_started）。"
                    f"可复用同一引用 {credential_ref} 改走同步脚本 run_command(credential_ref=\"{credential_ref}\") "
                    f"或声明式 HTTP web_fetch(credential_ref=\"{credential_ref}\")。"
                ),
            )
        credential_use = getattr(context, "credential_use", None)
        if credential_use is None or not callable(credential_use):
            return ToolHandlerOutcome(
                self.model_spec.name,
                False,
                '{"error": "当前调用缺少宿主凭据授权上下文（credential_use），未执行解密或填表。"}',
                error_code="CREDENTIAL_NOT_AUTHORIZED",
                effect_outcome="not_started",
                next_action=f"请通过宿主受控工具执行器在原授权会话调用，或复用 {credential_ref} 走 web_fetch / run_command。",
            )
        scope_payload, scope_key = _extract_session_scope(credential_use)
        if not scope_payload.get("active", False) or not all(scope_key):
            return ToolHandlerOutcome(
                self.model_spec.name,
                False,
                '{"error": "当前会话或发起人身份无效（缺少有效 owner/actor/thread），无法绑定隔离凭据浏览器会话。"}',
                error_code="CREDENTIAL_NOT_AUTHORIZED",
                effect_outcome="not_started",
                next_action="请回到原发起人的活动会话执行凭据填表。",
            )
        inspect_meta = {
            **(extra_meta or {}),
            CREDENTIAL_FILL_EXTENSION: {
                "version": CREDENTIAL_FILL_VERSION,
                "phase": "inspect",
                "session_scope": scope_payload,
            },
        }
        inspect_outcome = self._execute_with_meta(params, context, inspect_meta)
        if not inspect_outcome.ok:
            return _normalize_credential_fill_remote_error(inspect_outcome, phase="inspect")
        inspected = _parse_remote_json_result(inspect_outcome.output)
        origin = str((inspected or {}).get("origin") or "")
        target_id = str((inspected or {}).get("target_id") or "")
        document_id = str((inspected or {}).get("document_id") or "")
        generation = str((inspected or {}).get("generation") or "")
        if not origin or not target_id or not document_id or not generation:
            return ToolHandlerOutcome(
                self.model_spec.name,
                False,
                '{"error": "浏览器文档检查未返回完整的 origin/target_id/document_id/generation，未读取凭据。"}',
                error_code="TOOL_EXECUTION_FAILED",
                effect_outcome="not_started",
                next_action="请先用 open 打开目标 http(s) 登录页并确认加载完成后再执行 fill。",
            )
        field_value = ""
        variants: tuple[str, ...] = ()
        try:
            try:
                with credential_use(
                    credential_ref,
                    consumer="browser",
                    target_url=origin,
                    browser_consumer=actual_consumer,
                ) as material:
                    card_consumer = getattr(material, "browser_consumer", None)
                    if card_consumer is not None and dict(card_consumer) != actual_consumer:
                        raise CredentialInputError(
                            "CREDENTIAL_NOT_AUTHORIZED",
                            "原凭据卡绑定的浏览器插件安装或激活代次与当前实例不一致，拒绝跨代继承授权。",
                            stage="browser_consumer_check",
                            effect_outcome="not_started",
                        )
                    if material.origin != origin:
                        raise CredentialInputError(
                            "CREDENTIAL_NOT_AUTHORIZED",
                            "浏览器当前页面 origin 与凭据绑定站点不一致，未发送秘密。",
                            stage="browser_origin_check",
                            effect_outcome="not_started",
                        )
                    if field_name not in material.values:
                        available = list(material.values.keys())
                        secret_hint = (
                            "；内置账号密码卡的密码键名为 'secret'（而非 'password'），请改传 field='secret'"
                            if field_name == "password" and "secret" in material.values
                            else ""
                        )
                        return ToolHandlerOutcome(
                            self.model_spec.name,
                            False,
                            json.dumps(
                                {
                                    "error": f"凭据卡中不存在字段 '{field_name}'（可用字段键：{available}）{secret_hint}。",
                                    "missing_field": field_name,
                                    "available_fields": available,
                                },
                                ensure_ascii=False,
                            ),
                            error_code="TOOL_INVALID_ARGUMENTS",
                            effect_outcome="not_started",
                            next_action=f"未执行填表（effect_outcome=not_started）。请将 field 改为 {available} 中的准确键名{secret_hint}。",
                        )
                    field_value = str(material.values[field_name])
                    effective_ref = str(material.credential_ref)
                    effective_gen = int(material.generation)
                    variants = _credential_variants_from_values(material.values.values())
                    # 发送 apply 前最后复核固定插件激活、原连接与运行执行权，避免 inspect 期间停用或撤权。
                    if (self.client.activation_ref.require() != current_installation
                            or not self.client.is_running()
                            or self.client.connection() is not self.transport):
                        raise CredentialInputError(
                            "PLUGIN_ACTIVATION_UNAVAILABLE",
                            "browser-lite 激活或固定连接在检查后已变化，本次未发送凭据字段。",
                            stage="browser_pre_apply",
                            effect_outcome="not_started",
                        )
                    if context is not None and context.execution_authority_check is not None:
                        context.execution_authority_check()
                    apply_meta = {
                        **(extra_meta or {}),
                        CREDENTIAL_FILL_EXTENSION: {
                            "version": CREDENTIAL_FILL_VERSION,
                            "phase": "apply",
                            "origin": origin,
                            "target_id": target_id,
                            "document_id": document_id,
                            "generation": generation,
                            "field_value": field_value,
                            "session_scope": scope_payload,
                            "credential_binding": {
                                "credential_ref": effective_ref,
                                "generation": effective_gen,
                            },
                        },
                    }
                    field_value = ""
                    apply_outcome = self._execute_with_meta(params, context, apply_meta)
                    apply_meta = {}
            except CredentialInputError as exc:
                bound = _get_credential_session(self.client, scope_key)
                if bound is not None and bound.get("credential_ref") == credential_ref:
                    self._revoke_remote_browser_session(scope_payload, scope_key)
                code = "TOOL_UNAVAILABLE" if exc.code == "PLUGIN_ACTIVATION_UNAVAILABLE" else exc.code
                return ToolHandlerOutcome(
                    self.model_spec.name,
                    False,
                    json.dumps({"error": str(exc)}, ensure_ascii=False),
                    error_code=code,
                    reported_error_code=exc.code,
                    effect_outcome=exc.effect_outcome,
                    result_envelope={"credential_error": {"stage": exc.stage, "error_code": exc.code}},
                    next_action=f"{exc}（effect_outcome={exc.effect_outcome}）请核对站点、原授权会话或凭据状态，不要盲目重试。",
                )
            except (PermissionError, ValueError, OSError) as exc:
                bound = _get_credential_session(self.client, scope_key)
                if bound is not None and bound.get("credential_ref") == credential_ref:
                    self._revoke_remote_browser_session(scope_payload, scope_key)
                message = str(exc) or "凭据授权校验未通过"
                explicit_code = str(getattr(exc, "error_code", "") or "")
                if explicit_code in {"CREDENTIAL_INPUT_UNAVAILABLE", "CREDENTIAL_NOT_AUTHORIZED", "CREDENTIAL_BINDING_INVALID"}:
                    code = explicit_code
                else:
                    code = "CREDENTIAL_NOT_AUTHORIZED" if isinstance(exc, PermissionError) else "CREDENTIAL_INPUT_UNAVAILABLE"
                return ToolHandlerOutcome(
                    self.model_spec.name,
                    False,
                    json.dumps({"error": message}, ensure_ascii=False),
                    error_code=code,
                    effect_outcome="not_started",
                    next_action=f"{message}（effect_outcome=not_started）请在原授权会话对匹配的同源页面使用该凭据，不要重复发卡。",
                )
            scrubbed = _scrub_plugin_outcome(apply_outcome, context=context, extra_variants=variants)
        finally:
            field_value = ""
            variants = ()
        if not scrubbed.ok:
            return _normalize_credential_fill_remote_error(scrubbed, phase="apply")
        _remember_credential_session(
            self.client,
            scope_key,
            credential_ref=effective_ref,
            generation=effective_gen,
            origin=origin,
            consumer=actual_consumer,
        )
        return scrubbed

    def _verify_browser_lite_consumer(
        self,
        credential_ref: str,
        *,
        require_fill_tool: bool = True,
    ) -> tuple[PluginInstallation, dict[str, str]]:
        if (not isinstance(self.client, PluginMCPClient)
                or self.transport is None
                or (require_fill_tool and self.remote_tool != "fill")
                or self.remote_tool not in {"open", "read", "click", "fill", "close"}):
            raise CredentialInputError(
                "CREDENTIAL_NOT_AUTHORIZED",
                "当前工具不是宿主托管的固定激活 browser-lite 连接，已拒绝移交凭据。",
                stage="browser_consumer_verify",
                effect_outcome="not_started",
            )
        if not self._negotiated(CREDENTIAL_FILL_EXTENSION, CREDENTIAL_FILL_VERSION):
            raise CredentialInputError(
                "TOOL_UNAVAILABLE",
                f"当前 browser-lite 版本未声明 credential_fill v1 扩展，无法安全填表；请改用 run_command(credential_ref=\"{credential_ref}\") 或 web_fetch(credential_ref=\"{credential_ref}\")。",
                stage="browser_consumer_verify",
                effect_outcome="not_started",
            )
        try:
            current = self.client.activation_ref.require()
            owner = self.client.activation_ref.owner()
        except (OSError, ValueError, PermissionError) as exc:
            raise CredentialInputError(
                "PLUGIN_ACTIVATION_UNAVAILABLE",
                "browser-lite 插件未启用或固定激活已失效，本次未读取凭据。",
                stage="browser_consumer_verify",
                effect_outcome="not_started",
            ) from exc
        if (current != self.client.installation
                or not current.enabled
                or current.activation is None
                or not self.client.is_running()):
            raise CredentialInputError(
                "PLUGIN_ACTIVATION_UNAVAILABLE",
                "browser-lite 激活状态已变化或服务未运行，本次未读取凭据。",
                stage="browser_consumer_verify",
                effect_outcome="not_started",
            )
        try:
            if self.client.connection() is not self.transport:
                raise ValueError("transport changed")
        except (MCPError, ValueError) as exc:
            raise CredentialInputError(
                "PLUGIN_ACTIVATION_UNAVAILABLE",
                "browser-lite 固定连接已失效，不向重连实例转交凭据。",
                stage="browser_consumer_verify",
                effect_outcome="not_started",
            ) from exc
        manifest = current.manifest
        server_info = getattr(self.transport, "server_info", None) or {}
        if (manifest.plugin_id != "browser-lite"
                or manifest.entry_module != "browser_lite"
                or manifest.entry is not None
                or not manifest.entry_wheel.startswith("wheels/my_agent_browser_lite-")
                or server_info.get("name") != "browser-lite"):
            raise CredentialInputError(
                "CREDENTIAL_NOT_AUTHORIZED",
                "仅宿主核准的内置 browser-lite 包可执行凭据填表，同名或第三方插件无此权限。",
                stage="browser_consumer_verify",
                effect_outcome="not_started",
            )
        try:
            validate_activation_permission(current.activation)
            legacy_launch_policy(owner, current)
            archive_bytes = PluginInstallStore(owner).package_bytes(current)
        except (OSError, ValueError) as exc:
            raise CredentialInputError(
                "CREDENTIAL_NOT_AUTHORIZED",
                "browser-lite 安装包摘要或持久授权校验未通过，未读取凭据。",
                stage="browser_consumer_verify",
                effect_outcome="not_started",
            ) from exc
        if not _verify_builtin_browser_lite_wheel(archive_bytes, manifest.entry_wheel):
            raise CredentialInputError(
                "CREDENTIAL_NOT_AUTHORIZED",
                "当前 browser-lite wheel 内容与宿主内置包来源不一致，拒绝向非内置实现移交凭据。",
                stage="browser_consumer_verify",
                effect_outcome="not_started",
            )
        return current, {
            "installation_ref": current.installation_ref,
            "package_sha256": current.package_sha256,
            "activation_id": current.activation_id,
            "tool": "fill",
        }

    # LLM: MCPProxyTool 的构造钩子：公开属性 observation_binding 在构造时按安装描述设好（与普通 MCP 代理同名同义，决策点的自动执行
    #   规划只读这个属性）。身份全部取宿主：provider_id 是 plugin:<id>，activation 来自固定 client 的激活代次，复核库是
    #   client.runtime_repo（owner 权威库）；actions 映射只含本包同 target_kind 的 observation_ref 工具。没有观察声明（含测试夹具的
    #   普通客户端）返回 None。client 的安装描述与激活代次在构造时已固定，不在执行时重算。
    # 函数用途: 按已安装描述与固定激活构造本工具的观察绑定（只在构造时调用一次）。
    def _initial_observation_binding(self) -> ObservationBinding | None:
        declared = self._declared_tool()
        if declared is None or (declared.observation is None and declared.observation_ref is None):
            return None
        manifest = self.client.installation.manifest
        kind = (declared.observation or declared.observation_ref).target_kind
        return ObservationBinding(
            provider_id="plugin:" + manifest.plugin_id, activation_id=self.client.activation_ref.scope.activation_id,
            tool_name=self.model_spec.name, observation=declared.observation, observation_ref=declared.observation_ref,
            action_tools={tool.name: plugin_tool_name(manifest.plugin_id, tool.name) for tool in manifest.tools
                          if tool.observation_ref is not None and tool.observation_ref.target_kind == kind},
            repo=getattr(self.client, "runtime_repo", None),
        )

    # LLM: 声明来自固定 client 的已安装包描述；找不到（测试夹具的普通客户端）返回 None，此时不做任何观察处理。
    # 函数用途: 取本代理对应的工具声明。
    def _declared_tool(self) -> PluginToolDeclaration | None:
        manifest = getattr(getattr(self.client, "installation", None), "manifest", None)
        return next((tool for tool in getattr(manifest, "tools", ()) if tool.name == self.remote_tool), None)

    # LLM: 只看代理固定 transport 的声明；普通 arguments 不能伪造元数据，缺可信上下文须在发送前失败。
    #   写入上下文只给协商了写入扩展、且本工具声明 mutating/dangerous 的调用；只读工具永远拿不到写权限。
    # 函数用途: 为支持当前扩展版本的插件生成本次工作区读取/写入元数据。
    def _request_meta(self, context: ToolInvocationContext | None) -> dict[str, object] | None:
        if getattr(self.client, "lazy", False):
            return None
        meta: dict[str, object] = {}
        if self._negotiated(WORKSPACE_READ_EXTENSION, WORKSPACE_READ_VERSION):
            if context is None or context.workspace_read_context is None:
                raise MCPError("插件缺少本次工作区读取上下文", code="MCP_PROTOCOL_ERROR", effect_outcome="not_started")
            meta[WORKSPACE_READ_EXTENSION] = context.workspace_read_context.to_payload()
        if (self._negotiated(WORKSPACE_WRITE_EXTENSION, WORKSPACE_WRITE_VERSION)
                and self._declared_effect() in {"mutating", "dangerous"}):
            if context is None or context.workspace_write_context is None:
                raise MCPError("插件缺少本次工作区写入上下文", code="MCP_PROTOCOL_ERROR", effect_outcome="not_started")
            meta[WORKSPACE_WRITE_EXTENSION] = context.workspace_write_context.to_payload()
        return meta or None

    # LLM: 只读固定 transport 握手时的 experimental 声明；声明不授予权限，仅决定是否附带宿主上下文。
    # 函数用途: 判断插件是否声明支持某个扩展的指定版本。
    def _negotiated(self, extension: str, version: str) -> bool:
        capabilities = self.transport.capabilities if self.transport is not None else {}
        experimental = capabilities.get("experimental")
        declared = experimental.get(extension) if isinstance(experimental, dict) else None
        versions = declared.get("versions") if isinstance(declared, dict) else None
        return isinstance(versions, list) and version in versions

    # LLM: 效果来自已安装包描述中同名工具的声明；找不到按只读处理，不放宽。
    # 函数用途: 读取本工具声明的副作用类型。
    def _declared_effect(self) -> str:
        tools = getattr(getattr(self.client, "installation", None), "manifest", None)
        for tool in getattr(tools, "tools", ()):
            if tool.name == self.remote_tool:
                return tool.requested_effect
        return "read_only"


class PackMCPProxyTool(PluginProxyTool):
    """稳定的无连接门面；首次调用才启动，所有业务请求仍委托固定 transport 的已验代理。"""

    def availability(self) -> ToolAvailability:
        if not self.client.activation_ref.available():
            return ToolAvailability.unavailable("原能力包已停用或激活不可用", error_code="PLUGIN_ACTIVATION_UNAVAILABLE")
        if self.client.drift:
            return ToolAvailability.unavailable("能力包工具目录已变化，须重新启用", error_code="MCP_PACK_CATALOG_DRIFT")
        return ToolAvailability.ready()

    def precheck_availability(self) -> ToolAvailability:
        available = self.availability()
        if not available.available:
            return available
        if time.monotonic() < self.client.retry_at:
            return ToolAvailability.unavailable("能力包服务暂不可用", error_code="MCP_PACK_SERVER_UNAVAILABLE")
        return available

    def _execute(self, params: dict[str, object], context: ToolInvocationContext | None) -> ToolHandlerOutcome:
        if self._activation_revoked():
            return self._not_started("PLUGIN_ACTIVATION_UNAVAILABLE")
        # 在 ensure 之前预留在途名额，封住 ensure 返回与计数递增之间的 idle-stop 窗口。
        with self.client._lazy_lock:
            self.client._in_flight += 1
        try:
            transport = self.client.ensure_started()
            with self.client._lazy_lock:
                bound = next((tool for tool in self.client.validated_tools
                              if tool.remote_tool == self.remote_tool and tool.transport is transport), None)
            if bound is None:
                return self._not_started("MCP_PACK_SERVER_UNAVAILABLE")
            return bound._execute_with_meta(params, context, None)
        except MCPError as exc:
            return self._not_started(exc.code)
        finally:
            with self.client._lazy_lock:
                self.client._in_flight -= 1
                self.client.last_used = time.monotonic()

    def _not_started(self, code: str) -> ToolHandlerOutcome:
        """只描述门面尚未发业务请求的拒绝，不覆盖已发送请求的 UNKNOWN。"""
        return ToolHandlerOutcome(
            self.model_spec.name, False, json.dumps({"error": code}), error_code="TOOL_UNAVAILABLE",
            reported_error_code=code, effect_outcome="not_started",
        )


PLUGIN_DATA_DIR_ENV = "MY_AGENT_PLUGIN_DATA_DIR"


# LLM: 启动命令只由宿主从安装快照与准备结果推出：Python 包沿 venv 解释器 -I -m 入口模块；可执行入口直接运行随包文件；
#   解释器入口先按计划指纹复核定位文件（解释器被替换即拒绝），再以其绝对路径运行随包脚本。包不能提供宿主路径或额外 argv。
# 函数用途: 返回插件进程的命令、参数与工作目录，并确认所需目录仍在原环境里。
def _launch_argv(owner, manifest, environment, fingerprint: str) -> tuple[str, list[str], object]:
    entry = manifest.entry
    if entry is None:
        os.close(open_directory_beneath(owner.root, (*environment.relative_to(owner.root).parts, "python", "bin")))
        return str(environment / "python" / "bin" / "python"), ["-I", "-m", manifest.entry_module], environment
    files = environment / FILES_DIRECTORY
    os.close(open_directory_beneath(owner.root, files.relative_to(owner.root).parts))
    target = str(files / entry.command)
    interpreter = verified_runtime_command(owner.root, environment, entry.kind, fingerprint)
    if entry.kind == "executable":
        return target, list(entry.args), files
    return interpreter, [target, *entry.args], files


# LLM: 唯一插件数据位置；只由 owner 与已校验的插件 ID 推出，不接受包声明的路径。调用方负责 no-follow 创建。
# 函数用途: 返回某 owner 下某插件的私有数据目录，供启动插件进程和管理命令共用。
def plugin_data_dir(owner, plugin_id: str):
    return owner.plugins_dir / "data" / plugin_id


# LLM: v8 权限、解释器及沙箱能力来自固定安装快照；任何不完整规格都在启动前拒绝，不能退回普通沙箱。
#   就绪门与实际启动消费同一份宿主独占来源，不另算第二套隐藏集合。
# 函数用途: 为插件连接构造 v8 沙箱规格，旧版插件返回 None 以保留原平台沙箱行为。
def _plugin_v8_sandbox(owner, installation: PluginInstallation, command: str,
                       host_exclusive_roots: tuple[Path, ...] = ()) -> PluginV8Sandbox | None:
    permissions = installation.manifest.permissions
    if permissions is None:
        return None
    entry = installation.manifest.entry
    interpreter = None
    if entry is not None and entry.kind == "interpreter":
        interpreter = PluginInterpreterPaths(entry.interpreter, Path(command), Path(command))
    v8, problem = plugin_v8_sandbox_options(
        owner.home_dir, network=bool(permissions.network), interpreter=interpreter)
    if problem:
        raise SandboxUnavailableError(f"SANDBOX_UNAVAILABLE: {problem}")
    problem = plugin_sandbox_problem(True, owner.home_dir, sandbox_policy=v8, host_exclusive_roots=host_exclusive_roots)
    if problem:
        raise SandboxUnavailableError(f"SANDBOX_UNAVAILABLE: {problem}")
    return v8


# LLM: 受限策略与 v8 共用同一就绪门：网络 true 在 Linux 需要端口隔离，平台沙箱不可用一律拒绝，不退回无沙箱。
# 函数用途: 老格式 restricted 在构造与启动前确认统一沙箱底座可用。
def _require_legacy_policy_ready(owner, policy, host_exclusive_roots: tuple[Path, ...] = ()) -> None:
    problem = plugin_sandbox_problem(True, owner.home_dir, sandbox_policy=policy,
                                     host_exclusive_roots=host_exclusive_roots)
    if problem:
        raise SandboxUnavailableError(f"SANDBOX_UNAVAILABLE: {problem}")


class PluginResourceUnsettledError(RuntimeError, ValueError):
    """Raised when plugin runtime previous generation resources remain unsettled."""


# LLM: 一个客户端只属于安装表中的固定代次；沿原 MCP 重连/关闭与资源登记，不接收包提供的 owner、argv 或宿主地址。
# 类用途: 保存插件连接和同连接已验工具，供共享权限视图分别生成目录。
class PluginMCPClient(MCPStdioClient):
    # LLM: 构造冻结宿主继承擦洗来源；明确插件设置/host API 原 native env 保持授权语义，包 effect 不降低危险工具门。
#   同时 no-follow 创建插件数据目录（有副作用：可能新建目录），经 MY_AGENT_PLUGIN_DATA_DIR 传给子进程；
#   声明旧read或显式v9的包获得对应宿主API地址与固定激活令牌（有副作用：登记令牌）。
#   process_sandbox=True（配置 plugin_process_sandbox）时启动命令套进平台沙箱，只可写数据目录，TMPDIR 指向其中的 .tmp；
#   v8 无视该开关强制沙箱、按清单断网、隐藏 Gateway 家目录，只放行插件目录与安装前缀；
#   沙箱不可用时这里抛 SandboxUnavailable，调用方应先用 plugin_sandbox_problem 结构化拒绝。
#   host_exclusive_roots 是宿主冻结的可信来源（含配置外部 file: 凭据），只进隐藏规格，不进插件 argv、env 或公开结果。
#   可选 config 只绑定能力包连接预算及展示缓存容量，不改变普通插件连接默认或激活授权。
    # 函数用途: 将已准备的插件环境（Python 或 v6 随包文件）接到原 MCP 客户端，不在构造时启动进程；runtime_repo 是 owner 权威库，供观察新鲜度复核只读。
    def __init__(self, owner, installation: PluginInstallation, *, runtime_repo: object | None = None,
                 process_sandbox: bool = False, host_credential_env_names: frozenset[str] = frozenset(),
                 host_exclusive_roots: tuple[Path, ...] = (), config: object | None = None):
        self.runtime_repo = runtime_repo
        self.lazy = bool(getattr(installation.manifest, "has_mcp_runtime", False))
        self._lazy_lock = threading.Lock()
        self._credential_session_lock = threading.Lock()
        self._credential_session_bindings: OrderedDict[tuple[str, str, str], dict[str, object]] = OrderedDict()
        self.attempts, self.retry_at = 0, 0.0
        self.drift = False
        self._in_flight = 0
        self.last_used = time.monotonic()
        # 首次启动与重连都只用构造时冻结的这份来源集合；重连沿 _source_sandbox_spec，不重新推导。
        self._host_exclusive_roots = tuple(Path(path) for path in (host_exclusive_roots or ()))
        activation = installation.activation
        if activation is None or activation.phase not in {"preparing", "active"}:
            raise ValueError("插件没有可启动的固定激活")
        self.activation_ref = PluginActivationRef.from_owner(
            owner, installation.manifest.plugin_id, activation.activation_id, config=config)
        if self.lazy and activation.phase == "active":
            # 构造只生成展示门面，可复用同表已解码投影；实际启动与发送仍鲜活 require。
            if not self.activation_ref.available():
                raise ValueError("能力包激活已不可用")
        else:
            current = self.activation_ref.require(allow_preparing=True)
            if current != installation:
                raise ValueError("插件安装已变化")
        environment = owner.plugins_dir / "environments" / activation.plan.environment_ref
        command, args, cwd = _launch_argv(owner, installation.manifest, environment, activation.plan.interpreter_fingerprint)
        self._source_raw_argv = (command, *args)
        self._source_sandbox_spec = None
        self._source_configurations = ()
        # 插件自有数据按 owner + 插件 ID 固定一处，跨版本、停用和重新启用保留；卸载默认也不清（见 PLUGIN_LIFECYCLE）
        data_dir = plugin_data_dir(owner, installation.manifest.plugin_id)
        descriptor = open_directory_beneath(owner.root, data_dir.relative_to(owner.root).parts, create=True)
        os.close(descriptor)
        # B7：v8 事件插件（声明了 permissions）无条件强制进沙箱——不管调用方传的 process_sandbox 开没开；
        # 按声明断网、隐藏 Gateway 整个家目录，只放行插件环境/数据目录与解释器安装前缀。
        # 老格式 restricted 走同一底座：构造前复核固定授权，策略来自固定 permission_json，不再停 pending。
        v8_permissions = installation.manifest.permissions
        legacy_policy = legacy_launch_policy(owner, installation) if v8_permissions is None else None
        sandbox_env = {}
        if process_sandbox or v8_permissions is not None or legacy_policy is not None:
            os.close(open_directory_beneath(owner.root, (*data_dir.relative_to(owner.root).parts, SANDBOX_TMP_DIRECTORY),
                                            create=True))
            policy = (_plugin_v8_sandbox(owner, installation, command, self._host_exclusive_roots)
                      if v8_permissions is not None else legacy_policy)
            if legacy_policy is not None:
                _require_legacy_policy_ready(owner, legacy_policy, self._host_exclusive_roots)
            spec = plugin_sandbox_spec(cwd=cwd, data_dir=data_dir, owner_home=owner.home_dir, sandbox_policy=policy,
                                       host_exclusive_roots=self._host_exclusive_roots)
            self._source_sandbox_spec = spec
            command, *args = sandboxed_plugin_argv([command, *args], spec)
            sandbox_env = {"TMPDIR": str(data_dir / SANDBOX_TMP_DIRECTORY)}
        settings = canonical_plugin_settings(load_strict_json(installation.settings_json or "{}"),
                                             installation.manifest.settings_schema)
        self.installation = installation
        self.validated_tools: tuple[PluginProxyTool, ...] = ()
        # v9不要求read隐含授权；任一显式能力申请都取得自己的v2入口，令牌仍只绑定本激活。
        host_api_env = (issue_host_api_env(self.activation_ref, installation.manifest.plugin_id)
                        if installation.manifest.host_api_v9
                        or HOST_API_READ in installation.manifest.host_api else {})
        super().__init__(MCPServerConfig(
            name="plugin_" + installation.manifest.plugin_id,
            command=command, args=args, cwd=str(cwd),
            env={"MY_AGENT_PLUGIN_SETTINGS": settings, PLUGIN_DATA_DIR_ENV: str(data_dir), **host_api_env, **sandbox_env},
            host_credential_env_names=frozenset(host_credential_env_names),
            catalog_category="plugins",
        ), activation=self.activation_ref)
        if self.lazy:
            self.config.connect_timeout = _pack_connect_timeout(config)
        self.pack_tools: tuple[PackMCPProxyTool, ...] = tuple(
            self._proxy_for_info(MCPToolInfo(tool.name, tool.description, tool.input_schema), None, facade=True)
            for tool in installation_tool_declarations(installation)
        ) if self.lazy else ()
        register_source_registry(self)

    def ensure_started(self) -> object:
        """一把客户端锁串行完成启动/发现/发布；只尝试一次，撤销门和原资源清理不得被缓存绕过。"""
        with self._lazy_lock:
            if self.drift:
                raise MCPError("能力包目录已变化，须重新启用", code="MCP_PACK_CATALOG_DRIFT", effect_outcome="not_started")
            if time.monotonic() < self.retry_at:
                raise MCPError("能力包服务处于退避期", code="MCP_PACK_SERVER_UNAVAILABLE", effect_outcome="not_started")
            try:
                self.activation_ref.require()
            except (OSError, ValueError) as exc:
                raise MCPError("能力包激活已不可用", code="PLUGIN_ACTIVATION_UNAVAILABLE", effect_outcome="not_started") from exc
            if self.is_running() and not self.tools_changed and self.validated_tools:
                transport = self.connection()
                if all(tool.transport is transport for tool in self.validated_tools):
                    return transport
            # 已验连接在调用间崩溃：先记录一次退避，不让其它工具或 prepare 承担重启成本。
            if not self.is_running() and self.validated_tools and not self.attempts:
                self.validated_tools = ()
                self._advance_lazy_retry()
                raise MCPError("能力包服务已退出", code="MCP_PACK_SERVER_UNAVAILABLE", effect_outcome="not_started")
            transport = None
            try:
                transport = self.start() if self._transport is None else self.reconnect()
                tools = self.discover_tools(transport)
                self.publish_discovered_tools(transport, tools)
                self.attempts, self.retry_at = 0, 0.0
                return transport
            except Exception as exc:
                self.validated_tools = ()
                if transport is not None:
                    disconnect_failed_mcp_transport(self, transport)
                if isinstance(exc, MCPError) and exc.code == "MCP_PACK_CATALOG_DRIFT":
                    self.drift = True
                    self.stop()
                    raise
                self._advance_lazy_retry()
                code = "PLUGIN_ACTIVATION_UNAVAILABLE" if not self.activation_ref.available() else "MCP_PACK_SERVER_UNAVAILABLE"
                raise MCPError("能力包服务暂不可用", code=code, effect_outcome="not_started") from exc

    def _advance_lazy_retry(self) -> None:
        """调用方持 lazy 锁；退避与普通 MCP 同公式，只属于这一个固定激活。"""
        self.attempts += 1
        self.retry_at = time.monotonic() + min(60.0, float(2 ** min(self.attempts - 1, 6)))

    def idle_stop_if_due(self, now, idle_stop_seconds):
        """运行边界精确回收空闲 transport，保留客户端；永久 stop 事件不能清除或复活。"""
        if not self._lazy_lock.acquire(blocking=False):
            return None
        try:
            if (not self.lazy or not self.is_running() or self._in_flight or idle_stop_seconds <= 0
                    or now - self.last_used < idle_stop_seconds):
                return None
            transport = self.connection()
            receipt = self.disconnect(transport=transport)
            if receipt.confirmed:
                self.validated_tools = ()
            return receipt
        finally:
            self._lazy_lock.release()

    # LLM: 先拿来源协调锁、再由原MCP取连接锁；只在无旧transport时从未包装argv重建最新投影，不重绑活连接。
    # 函数用途: 鲜活复核固定激活和来源版本后启动插件，旧策略失效须原资源清理再重连。
    def start(self):
        from .tooling.web_fetch_runtime import unmanaged_owner_egress_error
        from .user_space.owner_egress import bound_owner_egress_policy

        owner = self.activation_ref.owner()
        egress = bound_owner_egress_policy(owner.root, owner_id=owner.owner_id, owner_kind=owner.identity.owner_kind)
        denial = unmanaged_owner_egress_error("mcp", egress)
        if denial is not None:
            raise MCPError(f"{denial.output} {denial.next_action}", code=denial.reported_error_code)
        with source_process_boundary():
            self._require_legacy_launch_facts()
            if self.is_running() and not self._source_configurations_current():
                self._retire_source_transport()
                raise MCPError("插件来源版本已变化，须重连", code="MCP_CONNECTION_CLOSED")
            with self._state_lock:
                fresh = self._transport is None and not self.is_closed()
            if fresh:
                self._refresh_source_launch()
            return super().start()

    # LLM: 先鲜活读表确认固定激活仍在（未撤销、未换代），再按固定 permission_json 重算全部事实。
    # 函数用途: 为本次启动复核老格式插件授权；事实变化一律拒绝，不自动换绑、不降 wide。
    def _require_legacy_launch_facts(self) -> None:
        installation = self.installation
        if installation.manifest.permissions is not None or installation.manifest.is_content_only:
            return
        try:
            current = self.activation_ref.require(allow_preparing=True)
        except (OSError, ValueError) as exc:
            raise LegacyLaunchDenied("legacy_permission_changed", "固定激活已不可用，本次没有启动。") from exc
        if current != installation:
            raise LegacyLaunchDenied("legacy_permission_changed", "固定激活已变化，本次没有启动。")
        legacy_launch_policy(self.activation_ref.owner(), current)

    # LLM: 原 Store 同代资源是唯一事实源；其他运行实例可共存，未知和未确认停止不能靠新客户端绕过。
    # 函数用途: 在模型或显式命令建立业务连接前，核对这个激活留下的资源清理结果。
    def require_settled_previous_resources(self) -> None:
        owner = self.activation_ref.owner()
        store = ProcessSessionStore(process_session_store_root(owner.home_dir, owner.home_dir))
        with store.transaction() as transaction:
            records, errors = transaction.list_records()
            if errors:
                raise ValueError("插件原资源目录不可读")
            unsettled: list[dict[str, Any]] = []
            for record in records:
                if record.get("activation_scope") != asdict(self.activation_ref.scope):
                    continue
                is_unsettled = record.get("status") == "unknown" or (
                    record.get("stop_requested")
                    and ((record.get("termination") or {}).get("cleanup") or {}).get("confirmed") is not True
                )
                if not is_unsettled:
                    continue

                host_pid = int(record.get("pid") or 0)
                child_pid = int(record.get("child_pid") or 0)
                host_alive = host_pid > 0 and is_pid_alive(host_pid)
                child_alive = child_pid > 0 and is_pid_alive(child_pid)

                if record.get("status") == "unknown" or host_alive or child_alive:
                    unsettled.append(record)
                else:
                    # Provably dead: auto-clear by updating record in transaction if supported
                    updated = dict(record)
                    termination = dict(updated.get("termination") or {})
                    cleanup = dict(termination.get("cleanup") or {})
                    cleanup["confirmed"] = True
                    termination["cleanup"] = cleanup
                    updated["termination"] = termination
                    if updated.get("status") in ("unknown", None) or updated.get("stop_requested"):
                        updated["status"] = "stopped"
                    try:
                        if hasattr(transaction, "write"):
                            transaction.write(updated)
                    except Exception:
                        pass

            if unsettled:
                raise PluginResourceUnsettledError("plugin runtime previous generation resources remain unsettled: 插件原资源退出尚未确认")

    # LLM: 完整分页来自固定 transport；名称、说明和规范 schema 全部相符才构造代理，任一坏项拒绝整包而非部分发布。
    # 函数用途: 验证当前服务确实提供所安装的工具，并沿原 MCP 代理生成受控能力。
    def discover_tools(self, transport) -> tuple[PluginProxyTool, ...]:
        actual = self.list_tools(transport=transport)
        declared = {tool.name: tool for tool in installation_tool_declarations(self.installation)}
        code = "MCP_PACK_CATALOG_DRIFT" if self.lazy else "MCP_PROTOCOL_ERROR"
        pin = getattr(self.installation.activation, "pinned_catalog_json", None)
        if pin is not None:
            try:
                if pinned_catalog_json_from_tools(actual) != pin:
                    raise ValueError("固定目录已变化")
            except ValueError as exc:
                raise MCPError("能力包固定目录已变化", code=code, effect_outcome="not_started") from exc
        if len(actual) != len(declared) or {tool.name for tool in actual} != set(declared):
            raise MCPError("插件工具集合与安装声明不一致", code=code, effect_outcome="not_started")
        proxies = []
        for info in actual:
            expected = declared[info.name]
            try:
                schema = canonicalize_tool_input_schema(info.input_schema)
            except (TypeError, ValueError) as exc:
                raise MCPError("插件输入结构不可执行", code=code, effect_outcome="not_started") from exc
            if ((pin is None and info.description != expected.description)
                    or schema != expected.input_schema):
                raise MCPError("插件工具说明或输入结构与安装声明不一致", code=code, effect_outcome="not_started")
            proxies.append(self._proxy_for_info(info, transport))
        if len({tool.model_spec.name for tool in proxies}) != len(proxies):
            raise MCPError("插件工具名称冲突", code="MCP_PROTOCOL_ERROR")
        return tuple(proxies)

    def _proxy_for_info(self, info, transport, *, facade=False):
        """门面与连接代理共用 Schema/危险效果/内部参数，只有连接所有权不同。"""
        proxy = build_proxy_tool(self, self.config.name, info, transport=transport, catalog_category="plugins")
        name = plugin_tool_name(self.installation.manifest.plugin_id, info.name)
        policy = replace(proxy.runtime_policy, resource_scopes=ResourceScopePolicy(
            "declared", static_scopes=(f"logical:plugin:{self.activation_ref.scope.activation_id}:{info.name}",),
        ), input_policy=replace(proxy.runtime_policy.input_policy, internal_parameters=tuple(dict.fromkeys(
            (*proxy.runtime_policy.input_policy.internal_parameters, "__operation_id", "__run_scope")))))
        cls = PackMCPProxyTool if facade else PluginProxyTool
        return cls(self, info.name, self._plugin_model_spec(proxy.model_spec, name, info), policy, transport=transport)

    # LLM: 只改模型可见的说明与检索提示（软信息），不改权限、效果或实现身份；插件 ID 与简介来自已安装包描述。
    #   让模型、tool_search 与能力推荐都能按"插件 <ID>"找到它，而不是只看到通用 MCP 文案和哈希后的工具名。
    # 函数用途: 生成带插件身份的工具说明和关键词。
    def _plugin_model_spec(self, spec, name: str, info):
        manifest = self.installation.manifest
        plugin_id = manifest.plugin_id
        description = f"插件 {plugin_id}（{' '.join(manifest.summary.split())}）的 {info.name} 工具。{info.description}"
        keywords = tuple(dict.fromkeys((*spec.hints.keywords, plugin_id, plugin_id.replace("-", "_"),
                                        *plugin_id.replace("_", "-").split("-"), info.name)))
        hints = replace(spec.hints, keywords=keywords, provider_id="plugin:" + plugin_id)
        if getattr(self, "lazy", False):
            hints = replace(hints, provider_id="package:" + plugin_id, default_deferred=True,
                            deferred_summary=manifest.summary)
        return replace(spec, name=name, description=sanitize_credentials(description), hints=hints)

    # LLM: 缓存只在原连接/激活再次核验后整体替换；当前安装版本不能改变同连接代理的实现身份。
    # 函数用途: 为下一轮缓存完整工具贡献，调用方仍须在自己的权限视图中投影。
    def publish_discovered_tools(self, transport, tools: tuple[PluginProxyTool, ...]) -> None:
        # LLM: 回调只换内存元组，不取安装写锁、发请求或写持久状态。
        # 函数用途: 在原 MCP 发布临界区保存已构造的同连接工具。
        def publish():
            self.validated_tools = tools

        self.publish_tools(transport, publish)


    def stop(self):
        with self._credential_session_lock:
            self._credential_session_bindings.clear()
        return super().stop()

    def disconnect(self, *, transport):
        with self._state_lock:
            is_current = transport is self._transport
        if is_current:
            with self._credential_session_lock:
                self._credential_session_bindings.clear()
        return super().disconnect(transport=transport)

    # LLM: 原重连仍先核旧树清理；外层锁固定为来源→连接，避免set_source通知与基类重连反序死锁。
    # 函数用途: 清退失效来源连接，再由原MCP创建最新策略的固定transport。
    def reconnect(self):
        with source_process_boundary():
            if self._transport is not None and not self._source_configurations_current():
                self._retire_source_transport()
            return super().reconnect()


    # LLM: 只用固定激活owner、原未包装argv和原沙箱权限spec；来源从唯一repository现读，替换config不修改旧transport。
    # 函数用途: 为下一次实际启动刷新来源CAS和平台argv，禁止嵌套包装或沿用构造时的旧profile。
    def _refresh_source_launch(self):
        spec = self._source_sandbox_spec
        configurations = process_source_configurations(self.activation_ref.owner().home_dir,
                                                       full_access=bool(spec and spec.full_access))
        argv = list(self._source_raw_argv)
        if spec is not None:
            argv = sandboxed_plugin_argv(argv, spec)
        self.config = replace(self.config, command=argv[0], args=argv[1:])
        self._source_configurations = configurations


    # LLM: 固定激活owner不来自模型；沙箱FullAccess依赖集合每次鲜活复核，普通插件只比较本owner。
    # 函数用途: 判断当前连接来源版本及owner保护集合是否仍可用。
    def _source_configurations_current(self):
        spec = self._source_sandbox_spec
        return process_source_configurations_current(self._source_configurations,
                                                     owner_home=self.activation_ref.owner().home_dir,
                                                     full_access=bool(spec and spec.full_access))


    # LLM: 仅消费结构化完整CAS通知；资源仍是本客户端原transport/Store，未确认停止不放行来源提交。
    # 函数用途: 切源前临时撤销旧插件连接，不永久关闭客户端，使确认清理后能按新策略重连。
    def retire_agents_source(self, owner_home, proposed_cas):
        spec = self._source_sandbox_spec
        if process_source_change_affects(self._source_configurations, owner_home, proposed_cas,
                                         bound_scope=(self.activation_ref.owner().home_dir,
                                                      bool(spec and spec.full_access))):
            if str(self.activation_ref.owner().home_dir) != owner_home:
                raise AgentsContractError("AGENTS_SOURCE_RESOURCE_UNCONFIRMED", "cross_owner_plugin")
            self._retire_source_transport()


    # LLM: 撤销和清理沿原MCP/Managed Store；原句柄保留供reconnect核回执，不靠Popen.poll猜后代已退出。
    # 函数用途: 请求原插件资源退役，清理未知时抛错而不启动替代连接。
    def _retire_source_transport(self):
        with self._state_lock:
            transport = self._transport
            launch_unknown = self._starting or self._launch_failure is not None
        if transport is None:
            if launch_unknown:
                raise AgentsContractError("AGENTS_SOURCE_RESOURCE_UNCONFIRMED", "plugin_launch")
            return
        receipt = self.disconnect(transport=transport)
        if not receipt.confirmed:
            raise AgentsContractError("AGENTS_SOURCE_RESOURCE_UNCONFIRMED", "plugin_resource")


_BUILTIN_BROWSER_MANIFEST_SCHEMA = "my-agent.builtin-browser-lite-manifest.v1"
_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}\Z")
_SDK_CANONICAL_SOURCES = (
    ("my_agent_plugin_api/nofollow_fs.py", ("agent", "common", "nofollow_fs.py")),
    ("my_agent_plugin_api/path_access_policy.py", ("agent", "path_access_policy.py")),
    ("my_agent_plugin_api/workspace_read_context.py", ("agent", "workspace_read_context.py")),
    ("my_agent_plugin_api/workspace_write_context.py", ("agent", "workspace_write_context.py")),
)
_DISALLOWED_DIST_INFO_SUFFIXES = (
    ".pth", ".py", ".pyc", ".pyo", ".so", ".pyd", ".dylib", ".dll", ".exe", ".sh", ".whl", ".zip",
)


def _validated_digest_map(
    raw: object,
    *,
    prefix: str,
    required: tuple[str, ...],
) -> dict[str, str] | None:
    if not isinstance(raw, dict) or not raw:
        return None
    cleaned: dict[str, str] = {}
    for name, digest in raw.items():
        if (
            not isinstance(name, str)
            or not name.startswith(prefix)
            or name.endswith("/")
            or name.lower().endswith(".pth")
            or any(part in {"", ".", ".."} for part in name.split("/"))
            or any(ch in name for ch in ("\\", ":", "\x00"))
            or not isinstance(digest, str)
            or not _SHA256_HEX_RE.fullmatch(digest)
        ):
            return None
        cleaned[name] = digest
    if any(req not in cleaned for req in required):
        return None
    return cleaned


def _parse_builtin_browser_manifest_doc(
    raw_bytes: bytes,
) -> tuple[dict[str, str], dict[str, str]] | None:
    try:
        manifest_doc = load_strict_json(raw_bytes)
    except (UnicodeError, ValueError, TypeError, RecursionError):
        return None
    expected_keys = {
        "schema_version",
        "plugin_id",
        "entry_distribution",
        "sdk_distribution",
        "entry_members",
        "sdk_members",
    }
    if (
        not isinstance(manifest_doc, dict)
        or set(manifest_doc) != expected_keys
        or manifest_doc.get("schema_version") != _BUILTIN_BROWSER_MANIFEST_SCHEMA
        or manifest_doc.get("plugin_id") != "browser-lite"
        or manifest_doc.get("entry_distribution") != "my-agent-browser-lite"
        or manifest_doc.get("sdk_distribution") != "my-agent-plugin-api"
    ):
        return None
    entry_members = _validated_digest_map(
        manifest_doc.get("entry_members"),
        prefix="browser_lite/",
        required=("browser_lite/server.py", "browser_lite/declaration.json"),
    )
    sdk_members = _validated_digest_map(
        manifest_doc.get("sdk_members"),
        prefix="my_agent_plugin_api/",
        required=("my_agent_plugin_api/__init__.py",),
    )
    if entry_members is None or sdk_members is None:
        return None
    return entry_members, sdk_members


def _load_canonical_browser_lite_digests(
    *,
    package_root: Path | None = None,
) -> tuple[dict[str, str], dict[str, str]] | None:
    """从宿主自身权威来源加载 browser_lite 与 my_agent_plugin_api 的完整运行时成员摘要。"""
    pkg_root = package_root if package_root is not None else Path(__file__).resolve().parents[1]
    repo_root = pkg_root.parent
    browser_src_dir = repo_root / "plugins" / "browser-lite" / "src" / "browser_lite"
    manifest_path = pkg_root / "config" / "builtin_browser_lite_manifest.json"
    try:
        if browser_src_dir.exists() or browser_src_dir.is_symlink():
            if browser_src_dir.is_symlink() or not browser_src_dir.is_dir():
                return None
            repo_entry: dict[str, str] = {}
            for child in sorted(browser_src_dir.rglob("*")):
                if child.is_dir() and not child.is_symlink():
                    continue
                rel_parts = child.relative_to(browser_src_dir).parts
                if (
                    any(part == "__pycache__" or part.endswith(".egg-info") for part in rel_parts)
                    or child.name == ".DS_Store"
                    or child.name.startswith("._")
                    or child.suffix in {".pyc", ".pyo"}
                ):
                    continue
                if child.is_symlink() or not child.is_file():
                    return None
                rel_name = "browser_lite/" + child.relative_to(browser_src_dir).as_posix()
                if rel_name.lower().endswith(".pth"):
                    return None
                repo_entry[rel_name] = hashlib.sha256(child.read_bytes()).hexdigest()
            if "browser_lite/server.py" not in repo_entry or "browser_lite/declaration.json" not in repo_entry:
                return None
            repo_sdk: dict[str, str] = {
                "my_agent_plugin_api/__init__.py": hashlib.sha256(b"").hexdigest(),
            }
            for member_name, rel_parts in _SDK_CANONICAL_SOURCES:
                src = pkg_root.joinpath(*rel_parts)
                if src.is_symlink() or not src.is_file():
                    return None
                repo_sdk[member_name] = hashlib.sha256(src.read_bytes()).hexdigest()
            if manifest_path.exists() or manifest_path.is_symlink():
                if manifest_path.is_symlink() or not manifest_path.is_file():
                    return None
                parsed = _parse_builtin_browser_manifest_doc(manifest_path.read_bytes())
                if parsed is None or parsed != (repo_entry, repo_sdk):
                    return None
            return repo_entry, repo_sdk
        if manifest_path.is_symlink() or not manifest_path.is_file():
            return None
        return _parse_builtin_browser_manifest_doc(manifest_path.read_bytes())
    except (OSError, ValueError, TypeError):
        return None


def _is_benign_wheel_dist_info_member(member_name: str, dist_info: str) -> bool:
    if not member_name.startswith(f"{dist_info}/"):
        return False
    sub = member_name[len(dist_info) + 1:]
    if not sub or sub.lower().endswith(_DISALLOWED_DIST_INFO_SUFFIXES):
        return False
    if sub in {"METADATA", "WHEEL", "RECORD", "top_level.txt"}:
        return True
    leaf = ""
    if "/" not in sub:
        leaf = sub
    elif sub.startswith("licenses/") and sub.count("/") == 1:
        leaf = sub.removeprefix("licenses/")
    return bool(leaf and leaf.startswith(("LICENSE", "COPYING", "NOTICE")))


def _extract_verified_wheel_runtime_members(
    wheel_snapshot: object,
    *,
    allowed_package_prefix: str,
    expected_members: Mapping[str, str],
    seen_across_wheels: set[str],
    member_limit: int,
) -> dict[str, str] | None:
    if not getattr(wheel_snapshot, "purelib", False) or getattr(wheel_snapshot, "entry_points", b""):
        return None
    dist_info = str(getattr(wheel_snapshot, "dist_info", "") or "")
    raw_content = getattr(wheel_snapshot, "content", b"")
    if not dist_info or not isinstance(raw_content, bytes):
        return None
    allowed_dirs = {f"{dist_info}/", f"{dist_info}/licenses/", allowed_package_prefix}
    seen_in_wheel_raw: set[str] = set()
    seen_in_wheel_folded: set[str] = set()
    seen_dirs: set[str] = set()
    runtime_digests: dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(raw_content)) as zf:
        for info in zf.infolist():
            name = info.filename
            if (
                not name
                or name != info.orig_filename
                or name.startswith("/")
                or any(ch in name for ch in ("\\", ":", "\x00"))
                or any(part in {".", ".."} for part in name.rstrip("/").split("/"))
            ):
                return None
            if info.is_dir():
                dir_key = archive_path_key(name)
                if (
                    not name.endswith("/")
                    or info.file_size != 0
                    or dir_key in seen_dirs
                    or (name not in allowed_dirs and not any(exp.startswith(name) for exp in expected_members))
                ):
                    return None
                seen_dirs.add(dir_key)
                continue
            if name.endswith("/") or name.lower().endswith(".pth") or name.split("/", 1)[0].endswith(".data"):
                return None
            file_key = archive_path_key(name)
            if name in seen_in_wheel_raw or file_key in seen_in_wheel_folded or file_key in seen_across_wheels:
                return None
            seen_in_wheel_raw.add(name)
            seen_in_wheel_folded.add(file_key)
            seen_across_wheels.add(file_key)
            content = read_zip_member(zf, info, member_limit)
            if name.startswith(f"{dist_info}/"):
                if not _is_benign_wheel_dist_info_member(name, dist_info):
                    return None
                if name == f"{dist_info}/top_level.txt":
                    top_levels = [line.strip() for line in content.decode("utf-8").splitlines() if line.strip()]
                    if top_levels != [allowed_package_prefix.rstrip("/")]:
                        return None
                continue
            if not name.startswith(allowed_package_prefix):
                return None
            runtime_digests[name] = hashlib.sha256(content).hexdigest()
    return runtime_digests


def _verify_builtin_browser_lite_wheel(
    archive_bytes: bytes,
    entry_wheel: str,
    *,
    _package_root: Path | None = None,
) -> bool:
    """按宿主自身权威来源校验已安装 browser-lite 全量 wheel 集合与可执行成员，拦截伪装包或附加启动代码。"""
    canonical = _load_canonical_browser_lite_digests(package_root=_package_root)
    if canonical is None:
        return False
    expected_entry_digests, expected_sdk_digests = canonical
    member_limit = PackageReadLimits().member_bytes
    try:
        package = inspect_plugin_package(archive_bytes)
        manifest = package.manifest
        if (
            manifest.plugin_id != "browser-lite"
            or manifest.entry_module != "browser_lite"
            or manifest.entry is not None
            or manifest.is_capability_pack
            or manifest.entry_wheel != entry_wheel
            or not entry_wheel.startswith("wheels/my_agent_browser_lite-")
        ):
            return False
        wheels = inspect_plugin_wheels(package)
        if not (1 <= len(wheels) <= 2) or len(wheels) != len(manifest.wheels):
            return False
        entry_matches = [item for item in wheels if item.declaration.path == entry_wheel]
        if len(entry_matches) != 1 or entry_matches[0].name != "my-agent-browser-lite":
            return False
        other_wheels = [item for item in wheels if item.declaration.path != entry_wheel]
        if len(other_wheels) > 1:
            return False
        sdk_snapshot = other_wheels[0] if other_wheels else None
        if sdk_snapshot is not None and (
            sdk_snapshot.name != "my-agent-plugin-api"
            or not sdk_snapshot.declaration.path.startswith("wheels/my_agent_plugin_api-")
        ):
            return False
        seen_across_wheels: set[str] = set()
        actual_entry = _extract_verified_wheel_runtime_members(
            entry_matches[0],
            allowed_package_prefix="browser_lite/",
            expected_members=expected_entry_digests,
            seen_across_wheels=seen_across_wheels,
            member_limit=member_limit,
        )
        if actual_entry is None or actual_entry != expected_entry_digests:
            return False
        if sdk_snapshot is not None:
            actual_sdk = _extract_verified_wheel_runtime_members(
                sdk_snapshot,
                allowed_package_prefix="my_agent_plugin_api/",
                expected_members=expected_sdk_digests,
                seen_across_wheels=seen_across_wheels,
                member_limit=member_limit,
            )
            if actual_sdk is None or actual_sdk != expected_sdk_digests:
                return False
        return True
    except (PluginPackageError, OSError, KeyError, ValueError, TypeError, UnicodeError, zipfile.BadZipFile):
        return False


def _extract_session_scope(credential_use: object) -> tuple[dict[str, object], tuple[str, str, str]]:
    """从宿主 credential_use 读取已核验的 (owner_id, actor_id, thread_id) 会话隔离身份。"""
    reader = getattr(credential_use, "session_scope", None)
    raw = reader() if callable(reader) else {}
    if not isinstance(raw, Mapping):
        return {"active": False}, ("", "", "")
    owner_id = str(raw.get("owner_id") or "").strip()
    actor_id = str(raw.get("actor_id") or "").strip()
    thread_id = str(raw.get("thread_id") or "").strip()
    active = raw.get("active") is True
    payload = {
        "active": active,
        "owner_id": owner_id,
        "actor_id": actor_id,
        "thread_id": thread_id,
    }
    if not owner_id or not (actor_id or thread_id):
        return payload, ("", "", "")
    return payload, (owner_id, actor_id, thread_id)


def _parse_remote_json_result(output: str) -> dict[str, Any] | None:
    try:
        outer = json.loads(output)
        inner = outer.get("result") if isinstance(outer, dict) else None
        parsed = json.loads(inner) if isinstance(inner, str) else inner
        return parsed if isinstance(parsed, dict) else None
    except (TypeError, ValueError):
        return None


def _normalize_browser_session_remote_error(outcome: ToolHandlerOutcome) -> ToolHandlerOutcome:
    """将浏览器受保护会话跨 actor/无绑定隔离拒绝统一映射为 CREDENTIAL_NOT_AUTHORIZED 且 not_started。"""
    parsed = _parse_remote_json_result(outcome.output)
    remote_code = str((parsed or {}).get("code") or "")
    remote_message = str((parsed or {}).get("message") or "")
    if remote_code != "CREDENTIAL_SESSION_ISOLATED":
        return outcome
    envelope = dict(outcome.result_envelope)
    envelope["credential_error"] = {
        "stage": "browser_session_isolation",
        "error_code": "CREDENTIAL_NOT_AUTHORIZED",
        "reported_cause": remote_code,
        "side_effect_state": "not_started",
    }
    return replace(
        outcome,
        error_code="CREDENTIAL_NOT_AUTHORIZED",
        reported_error_code=remote_code,
        effect_outcome="not_started",
        result_envelope=envelope,
        next_action=(
            f"{remote_message or '当前浏览器会话与原授权用户及原会话隔离'}（effect_outcome=not_started）；"
            "请回到原发起人会话，或在本人获授权会话中先调用 open 另启独立浏览器会话。"
        ),
    )


def _normalize_credential_fill_remote_error(outcome: ToolHandlerOutcome, *, phase: str) -> ToolHandlerOutcome:
    """将两阶段填表中未产生表单写入的远端拒绝精确标为 effect_outcome=not_started，并给出具体下一步。"""
    parsed = _parse_remote_json_result(outcome.output)
    remote_code = str((parsed or {}).get("code") or "")
    remote_message = str((parsed or {}).get("message") or "")
    not_applied = phase == "inspect" or (parsed or {}).get("side_effect") == "not_applied"
    if remote_code in {"OBSERVATION_STALE", "OBSERVATION_NOT_FOUND"}:
        return outcome
    if not not_applied:
        return outcome
    error_code = outcome.error_code
    if remote_code in {"INVALID_ARGUMENTS", "INVALID_SELECTOR", "SELECTOR_NOT_FOUND", "SELECTOR_AMBIGUOUS", "NOT_FILLABLE", "OPTION_NOT_FOUND"}:
        error_code = "TOOL_INVALID_ARGUMENTS"
    elif remote_code == "CREDENTIAL_SESSION_ISOLATED":
        error_code = "CREDENTIAL_NOT_AUTHORIZED"
    elif remote_code in {"URL_NOT_ALLOWED", "ORIGIN_MISMATCH", "CROSS_ORIGIN_FORM_ACTION"}:
        error_code = "TOOL_PERMISSION_DENIED"
    elif remote_code in {"NO_PAGE", "DOCUMENT_UNVERIFIED", "DOCUMENT_CHANGED"}:
        error_code = "TOOL_EXECUTION_FAILED"
    envelope = dict(outcome.result_envelope)
    envelope["error_details"] = {
        "stage": f"credential_fill_{phase}",
        "reported_cause": remote_code or outcome.reported_error_code or "MCP_REMOTE_ERROR",
        "side_effect_state": "not_started",
    }
    hint = (
        f"凭据填表在 {phase} 阶段未写入页面（{remote_code or 'MCP_REMOTE_ERROR'}，effect_outcome=not_started）："
        f"{remote_message or '请先用 read/open 核对当前受控登录页面与控件选择器后再操作，不要盲目重试。'}"
    )
    return replace(
        outcome,
        error_code=error_code,
        reported_error_code=remote_code or outcome.reported_error_code,
        effect_outcome="not_started",
        result_envelope=envelope,
        next_action=hint,
    )


def _credential_variants_from_values(values: Iterable[str]) -> tuple[str, ...]:
    """优先复用 C5 共享 credential_echo_variants，单测独立运行期保留同构本地变体计算。"""
    from .tooling import host_output_redaction

    shared = getattr(host_output_redaction, "credential_echo_variants", None)
    if callable(shared):
        return tuple(shared(values))
    expanded: set[str] = set()
    for raw in values:
        if not isinstance(raw, str) or not raw:
            continue
        expanded.update((
            raw,
            json.dumps(raw, ensure_ascii=True)[1:-1],
            json.dumps(raw, ensure_ascii=False)[1:-1],
            quote(raw, safe=""),
            quote_plus(raw, safe=""),
        ))
        for encoder in (base64.b64encode, base64.urlsafe_b64encode):
            encoded = encoder(raw.encode("utf-8")).decode("ascii")
            expanded.update((encoded, encoded.rstrip("=")))
    return tuple(sorted((v for v in expanded if v), key=len, reverse=True))


def _scrub_plugin_outcome(
    outcome: ToolHandlerOutcome,
    *,
    context: ToolInvocationContext | None,
    extra_variants: tuple[str, ...] = (),
) -> ToolHandlerOutcome:
    """在观察候选铸造与公开结果返回前净化所有已知凭据变体（替换为 [REDACTED]）。"""
    credential_use = getattr(context, "credential_use", None)
    turn_redaction = getattr(credential_use, "turn_redaction", None)
    public_text = getattr(turn_redaction, "public_text", None)

    def clean(item: Any) -> Any:
        if isinstance(item, str):
            text = public_text(item) if callable(public_text) else item
            for variant in extra_variants:
                if variant:
                    text = text.replace(variant, "[REDACTED]")
            return text
        if isinstance(item, dict):
            return {clean(k): clean(v) for k, v in item.items()}
        if isinstance(item, list):
            return [clean(v) for v in item]
        if isinstance(item, tuple):
            return tuple(clean(v) for v in item)
        return item

    if not callable(public_text) and not extra_variants:
        return outcome
    return replace(
        outcome,
        output=clean(outcome.output),
        result_envelope=clean(outcome.result_envelope),
        next_action=clean(outcome.next_action),
    )


def _remember_credential_session(
    client: object,
    scope_key: tuple[str, str, str],
    *,
    credential_ref: str,
    generation: int,
    origin: str,
    consumer: dict[str, str],
    variants: tuple[str, ...] = (),
) -> None:
    _ = variants
    lock = getattr(client, "_credential_session_lock", None)
    bindings = getattr(client, "_credential_session_bindings", None)
    if lock is None or not isinstance(bindings, OrderedDict):
        return
    with lock:
        bindings[scope_key] = {
            "credential_ref": str(credential_ref),
            "generation": int(generation),
            "origin": str(origin),
            "consumer": dict(consumer),
        }
        bindings.move_to_end(scope_key)
        while len(bindings) > _MAX_CREDENTIAL_SESSION_BINDINGS_COUNT:
            bindings.popitem(last=False)


def _get_credential_session(client: object, scope_key: tuple[str, str, str]) -> dict[str, object] | None:
    lock = getattr(client, "_credential_session_lock", None)
    bindings = getattr(client, "_credential_session_bindings", None)
    if lock is None or not isinstance(bindings, OrderedDict):
        return None
    with lock:
        entry = bindings.get(scope_key)
        if entry is not None:
            bindings.move_to_end(scope_key)
        return dict(entry) if isinstance(entry, dict) else None


def _forget_credential_session(client: object, scope_key: tuple[str, str, str]) -> None:
    lock = getattr(client, "_credential_session_lock", None)
    bindings = getattr(client, "_credential_session_bindings", None)
    if lock is None or not isinstance(bindings, OrderedDict):
        return
    with lock:
        bindings.pop(scope_key, None)


def _has_credential_sessions(client: object) -> bool:
    lock = getattr(client, "_credential_session_lock", None)
    bindings = getattr(client, "_credential_session_bindings", None)
    if lock is None or not isinstance(bindings, OrderedDict):
        return False
    with lock:
        return bool(bindings)
