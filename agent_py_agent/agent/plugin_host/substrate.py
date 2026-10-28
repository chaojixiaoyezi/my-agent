# LLM: 仅真实AuthorizedCall；debug DTO原样透传，摘要别名全部验证且统一400，不扩本机管理员的caller边界。
# 模块用途: 接通包管理、安全调试及认证，复用安装、制品、会话和领域响应权威。
from __future__ import annotations

import base64
import binascii
import hashlib
import re
from pathlib import Path

from ..common.nofollow_fs import read_bytes_beneath
from ..plugin_installation import PluginInstallationError
from ..plugin_manifest import PluginPackageError
from ..plugin_package import (
    HOST_PACKAGE_LIMITS,
    inspect_plugin_package,
    normalize_plugin_archive_bytes,
)
from ..user_space.owner_resolver import OwnerIdentity
from ..user_space.totp_sso_auth import TotpSSOAuthService
from ..workflows.debug_nodes import analysis_preview
from .action_specs import action_spec, required_capabilities
from .action_specs_substrate import SUBSTRATE_ACTIONS
from .authorization import revalidate_call
from .contracts import DomainResult
from .conversations import digest, read_context
from .errors import HostAPIError
from .schemas import PAGE_MAX_BYTES, canonical_json, thaw


# LLM: 不返回安装私有配置、路径或权限票据；状态只根据原激活phase，不把未知清理说成停用完成。
# 函数用途: 投影通用安装结果所需的插件版本、摘要、静态清单和当前状态。
def installation_view(entry):
    return {'plugin_id': entry.manifest.plugin_id, 'version': entry.manifest.version,
        'sha256': entry.package_sha256, 'manifest': entry.manifest.to_payload(), 'revision': entry.revision,
        'status': 'enabled' if entry.enabled else 'cleanup_pending' if entry.activation is not None else 'disabled'}


# LLM: payload路径仅限当前Owner的uploads或workspace/staging；逐段nofollow，禁止resolve清洗点段、链接和硬链接。
# 函数用途: 读取已核管理员的有界暂存包，不从任意宿主或运行目录安装代码。
def staged_package_bytes(context, raw_path):
    if type(raw_path) is not str or any(part in ('', '.', '..') for part in raw_path.replace('\\', '/').split('/') if part != '' ) or '\\' in raw_path:
        raise HostAPIError('PACKAGE_SOURCE_DENIED', 403)
    home = context.owner_home.home_dir
    path = Path(raw_path)
    if not path.is_absolute():
        path = home / path
    roots = (home / 'uploads', context.owner_home.workspace_dir / 'staging')
    if '..' in path.parts or not any(path.is_relative_to(root) for root in roots):
        raise HostAPIError('PACKAGE_SOURCE_DENIED', 403)
    try:
        raw = read_bytes_beneath(home, path.relative_to(home).parts,
            max_bytes=HOST_PACKAGE_LIMITS.archive_bytes, require_dir_fd=True)
        if raw is None:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        return raw
    except (OSError, ValueError):
        raise HostAPIError('PACKAGE_SOURCE_DENIED', 403) from None


# LLM: 只由可信Gateway构造；分析/节点执行器取宿主注入，缺live分析器仅给mock，SSO仍需真实验签器。
# 类用途: 为通用包、预览调试及认证提供Host适配，不在JSON中注册执行回调或新增业务权威。
class GatewaySubstrateDomain:
    # LLM: 构造零IO，仅保留同一Gateway的真实依赖；不构造主Agent或启动后台服务。
    # 函数用途: 保存Owner路径、原会话与制品读口。
    def __init__(self, resources, conversation, observations):
        self.resources, self.conversation, self.observations = resources, conversation, observations

    # LLM: 登记不等于IdP已可验；callback缺可信协议端口必须不可用，不接受客户端声明已验证。
    # 函数用途: 返回指定通用动作当前是否具备真实后端。
    def ready(self, action):
        if action not in SUBSTRATE_ACTIONS:
            return False
        return action != 'users.auth.sso.callback' or callable(getattr(self.resources.server, 'sso_assertion_verifier', None))

    # LLM: 每次核当前caller而非仅安装actor；普通SSO登录不授Host管理权，admin_only是当前第一版安全上界。
    # 函数用途: 让通用管理底座仅在明确本机管理员上下文执行。
    def _context(self, authorized):
        revalidate_call(authorized)
        binding = authorized.binding
        caller = binding.source.caller_owner or binding.actor
        if (caller != OwnerIdentity.local_main() or binding.actor != OwnerIdentity.local_main()
                or authorized.effective_owner != OwnerIdentity.local_main()):
            raise HostAPIError('CAPABILITY_DENIED', 403)
        return read_context(authorized)

    # LLM: 每次读取当前部署配置和Owner策略；冷测试宿主无Agent时采用原dataclass默认，不读取别人的配置或凭据。
    # 函数用途: 构造原PluginManagement，让启停/审批和工具禁用规则保持生效。
    def _management(self, authorized, context):
        from ..plugin_management import PluginManagement, plugin_management_context
        from ..settings.config import AgentConfig
        from ..user_space.home_layout import home_paths
        server = self.resources.server
        agent = getattr(server, 'agent', None) or getattr(server, 'control_plane', None)
        config = getattr(agent, 'config', None) or AgentConfig()
        home = home_paths(self.resources.root)
        store = self.resources.store_for(context)
        management = plugin_management_context(context.owner_home, home, config, store.threads,
            actor_id='local/main', channel='plugin', conversation_id=authorized.binding.installation_ref, is_admin=True)
        return PluginManagement(management)

    # LLM: schema/能力/身份先于IO；debug布尔缺省False，原样保留DTO新旧键和persisted=False，认证不缓存秘密。
    # 函数用途: 分派已授权动作，调试只透传引擎事实，不在HTTP层拼第二份节点状态。
    def call(self, authorized):
        context = self._context(authorized)
        action = authorized.call.action
        if action not in SUBSTRATE_ACTIONS:
            raise HostAPIError('UNSUPPORTED_ACTION')
        data = action_spec(action).validate_payload(thaw(authorized.call.payload))
        if not required_capabilities(action, data) <= authorized.binding.capabilities:
            raise HostAPIError('CAPABILITY_DENIED', 403)
        if action.startswith('plugins.packages.'):
            return self._package(authorized, context, data)
        if authorized.call.idempotency_key is not None:
            raise HostAPIError('INVALID_SCHEMA')
        if action == 'workflows.steps.debug':
            from ..workflows.engine import WorkflowEngine
            server = self.resources.server
            analyzer = getattr(server, 'workflow_debug_analyzer', None)
            result = WorkflowEngine(context.owner_home.home_dir,
                analysis_handler=analyzer if callable(analyzer) else analysis_preview,
                debug_node_handler=getattr(server, 'workflow_debug_node_handler', None)).debug_step(
                    **{**data, 'allow_side_effects': data.get('allow_side_effects', False)})
            return DomainResult(200, result)
        return DomainResult(200, self._auth(authorized, data))

    # LLM: 参数不能选择host_root或verifier；TOTP管理限原manageable_user范围，bool管理员绕过不暴露在schema。
    # 函数用途: 接到标准库认证底座，认证票据只返回当前安全交互，不进入控制回执。
    def _auth(self, authorized, data):
        action = authorized.call.action
        allowed = authorized.binding.grant.get('manageable_user_ids', ())
        if 'user_id' in data and data['user_id'] not in allowed and '*' not in allowed:
            raise HostAPIError('TARGET_DENIED', 403)
        verifier = getattr(self.resources.server, 'sso_assertion_verifier', None)
        service = TotpSSOAuthService(self.resources.root, assertion_verifier=verifier)
        if action == 'users.auth.totp.setup':
            return service.setup_totp(data['user_id'])
        if action == 'users.auth.totp.verify':
            return service.verify_totp(data['user_id'], data['code'])
        if action == 'users.auth.totp.disable':
            return service.disable_totp(data['user_id'], data['code'])
        if action == 'users.auth.sso.config.get':
            return service.get_sso_config()
        if action == 'users.auth.sso.config.update':
            return service.update_sso_config(data['config'])
        if action == 'users.auth.sso.initiate':
            return service.initiate_sso_login(**data)
        if action == 'users.auth.sso.callback':
            return service.complete_sso_callback(**data)
        raise HostAPIError('UNSUPPORTED_ACTION')

    # LLM: 三来源互斥；两个摘要别名均须有效且匹配原字节，坏格式/不匹配统一400，先于安装写入且不能相互覆盖。
    # 函数用途: 校验SHA256格式、内容及manifest后规范化授权输入，十六进制大小写不改变摘要含义。
    def _candidate(self, authorized, context, data):
        if sum(key in data for key in ('package_base64', 'package_path', 'artifact_ref')) != 1:
            raise HostAPIError('INVALID_SCHEMA')
        expected = [data[key] for key in ('sha256', 'expected_sha256') if key in data]
        if any(not isinstance(value, str) or re.fullmatch(r'[a-fA-F0-9]{64}', value) is None for value in expected):
            raise HostAPIError('PACKAGE_SHA256_MISMATCH', 400)
        try:
            if 'package_base64' in data:
                raw = base64.b64decode(data['package_base64'], validate=True)
            elif 'package_path' in data:
                raw = staged_package_bytes(context, data['package_path'])
            else:
                reader = self.observations.routes['workspace']
                raw = reader.read_registered_bytes(authorized, context, data['artifact_ref'], max_bytes=HOST_PACKAGE_LIMITS.archive_bytes)
            actual = hashlib.sha256(raw).hexdigest()
            if any(actual != value.lower() for value in expected):
                raise HostAPIError('PACKAGE_SHA256_MISMATCH', 400)
            package = inspect_plugin_package(normalize_plugin_archive_bytes(raw), limits=HOST_PACKAGE_LIMITS)
            if data.get('plugin_id', package.manifest.plugin_id) != package.manifest.plugin_id:
                raise HostAPIError('PACKAGE_ID_MISMATCH', 409)
            if len(canonical_json(package.manifest.to_payload()).encode()) > PAGE_MAX_BYTES - 2048:
                raise HostAPIError('PAYLOAD_TOO_LARGE', 413)
            return package
        except (binascii.Error, PluginPackageError):
            raise HostAPIError('PACKAGE_INVALID', 400) from None

    # LLM: 唯一幂等门只存非秘密结果及输入摘要；新写/重放重核当前来源，包来源在门内只读一次。
    # 函数用途: 沿原PluginManagement与控制回执安装包或执行生命周期，未知/待审批结果不伪装成功。
    def _package(self, authorized, context, data):
        from .capabilities_v2_handlers import _once
        service = self._management(authorized, context)
        operation = 'host-' + digest([authorized.binding.installation_ref, authorized.call.action, authorized.call.idempotency_key])
        install = authorized.call.action == 'plugins.packages.install'
        if not install and data['action'] == 'status':
            entry, _ = service.package_lifecycle(data['plugin_id'], 'status', operation_id=operation)
            return DomainResult(200, installation_view(entry))
        if authorized.call.idempotency_key is None:
            raise HostAPIError('INVALID_SCHEMA')
        candidate = []
        # LLM: 回执重放仍核当前原安装；原来源消失不重读、不重复安装，换包或卸载不借旧回执绕过范围。
        # 函数用途: 在原幂等锁内固定一次候选快照并复核当前权限。
        def preflight(saved):
            self._context(authorized)
            if install and saved is None:
                candidate.append(self._candidate(authorized, context, data))
            if saved is not None:
                entry = next((row for row in service.installations.snapshot() if row.manifest.plugin_id == saved['plugin_id']), None)
                if saved['status'] == 'uninstalled':
                    if entry is not None:
                        raise HostAPIError('PLUGIN_CHANGED', 409)
                elif entry is None or saved.get('sha256') != entry.package_sha256:
                    raise HostAPIError('PLUGIN_CHANGED', 409)
        # LLM: 只消费预检固定字节；管理错误保留明确提交事实，不因未知而重试另一路。
        # 函数用途: 执行原安装或生命周期领域并投影真实当前状态。
        def effect():
            try:
                if install:
                    result = service.install_package_bytes(candidate[0].archive_bytes,
                        operation_id=operation, expected_revision=data.get('expected_revision', 0))
                    return DomainResult(200, installation_view(result.installation))
                entry, result = service.package_lifecycle(data['plugin_id'], data['action'], operation_id=operation)
                if entry is None:
                    return DomainResult(200, {'plugin_id': data['plugin_id'], 'status': 'uninstalled', 'result': result})
                return DomainResult(200, {**installation_view(entry), 'result': result})
            except PluginInstallationError as error:
                outcome = 'unknown' if error.commit_state == 'unknown' else 'committed' if error.commit_state == 'committed' else 'not_started'
                raise HostAPIError('PLUGIN_MANAGEMENT_FAILED', 409, outcome) from None
        return _once(authorized, context, self.observations.services, effect, preflight)
