# LLM: 原Agent与显式owner领域服务共用同一卡片RMW/加密/回放，输入生成ID只有原ref+generation算法。
# 模块用途: 共用原凭据提交/取消和无预填表单，账号控件只按结构name展示，不扩大群、actor、期限或工具权限。
# LLM: 凭据唯一存于原owner私密目录；显式group按当前成员和实际填写人择取，status/revoke安全公开authentication与authentication_configured，秘密仅宿主注入。
# 模块用途: 收集并加密接口凭据，群默认复用和个人任务隔离共用原卡/存储/网络门，不向聊天或模型交明文。
from __future__ import annotations

import base64
import hmac
import json
import os
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit, urlunsplit

from ..capability.runtime_config_reload import capability_config_for_agent
from ..common.json_io import (
    locked_json_path,
    read_json_object_report,
    write_private_text_file_atomic_unlocked,
)
from ..conversation.im_tool_approval import approval_recipient
from ..conversation.tool_approval_scope import tool_approval_scope, tool_approval_scope_is_current
from ..delivery import DeliveryContext
from ..path_access_policy import PRIVATE_INTERACTIONS_OWNER_PARTS
from .interaction_refs import interaction_key, sign_interaction_reference
from .owner_quota import OwnerQuotaChange
from .owner_resolver import owner_identity_from_config
from .task_credential_groups import (
    discover_group_credentials,
    discover_owner_credentials,
    new_credential_access_scope,
)
from .task_credential_schema import (
    collection_fields,
    credential_auth_config,
    credential_fields,
    credential_header_name,
    credential_ready_expired,
    credential_seconds,
)
from .task_credential_storage import CredentialInputError, credential_stage, read_credential_row


# LLM: 只使用已声明 cryptography 实现，缺依赖明确不可用，不自行实现加密或退回明文。
# LLM: Count0仍按原create=True读取/初始化同key；C已验原MAC只读既存key，缺失/损坏必须拒绝且不重建，派生公式不变。
# 函数用途: 由原owner主密钥派生同一加密密钥，C不为已签旧卡新建密钥。
def _cipher(home, *, existing_only=False):
    from cryptography.fernet import Fernet
    key = hmac.digest(interaction_key(home, create=not existing_only), b'task-credentials.v1', 'sha256')
    return Fernet(base64.urlsafe_b64encode(key))


# LLM: origin 来自结构 URL，禁止内嵌密码和片段；实际出站许可继续由原 web_fetch 网络门逐跳裁决。
# 函数用途: 将接口 URL 规范为凭据允许发送的准确 origin。
def credential_origin(url):
    parts = urlsplit(url)
    if parts.scheme not in {'http','https'} or not parts.hostname or parts.username or parts.password or parts.fragment:
        raise ValueError('接口 URL 无效')
    return f'{parts.scheme}://{parts.hostname.lower()}:{parts.port or (443 if parts.scheme=="https" else 80)}'


# LLM: 引用只能是不可穿越目录的宿主编号；这不是新工作区，所有读写仍在 H2 私密目录。
# 函数用途: 定位一条待输入或已加密凭据记录。
def credential_path(home, reference):
    if not isinstance(reference,str) or not reference.startswith('cred_') or len(reference)!=37:
        raise ValueError('凭据引用无效')
    if any(c not in '0123456789abcdef' for c in reference[5:]):
        raise ValueError('凭据引用无效')
    return Path(home).joinpath(*PRIVATE_INTERACTIONS_OWNER_PARTS, reference+'.json')


@dataclass(frozen=True)
class CredentialMaterial:
    """宿主单次授权后向受控处理器移交的不可变凭据材料；repr/str 排除所有敏感字段。"""

    credential_ref: str
    generation: int
    origin: str = field(repr=False)
    target_url: str = field(repr=False)
    values: Mapping[str, str] = field(repr=False)
    requested_credential_ref: str = field(default='', repr=False)
    access_scope: str = field(default='task', repr=False)
    owner_id: str = field(default='', repr=False)
    actor_id: str = field(default='', repr=False)
    thread_id: str = field(default='', repr=False)
    browser_consumer: Mapping[str, str] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.credential_ref, str)
            or not self.credential_ref.startswith('cred_')
            or len(self.credential_ref) != 37
            or any(ch not in '0123456789abcdef' for ch in self.credential_ref[5:])
        ):
            raise ValueError('凭据材料引用无效')
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError('凭据材料代次无效')
        if not isinstance(self.origin, str) or not self.origin:
            raise ValueError('凭据材料 origin 无效')
        if not isinstance(self.target_url, str) or not self.target_url:
            raise ValueError('凭据材料 target_url 无效')
        if not isinstance(self.values, Mapping) or any(
            not isinstance(k, str) or not isinstance(v, str)
            for k, v in self.values.items()
        ):
            raise ValueError('凭据材料字段值无效')
        object.__setattr__(self, 'values', MappingProxyType(dict(self.values)))
        if self.browser_consumer is not None:
            if not isinstance(self.browser_consumer, Mapping):
                raise ValueError('凭据浏览器消费者绑定无效')
            object.__setattr__(
                self,
                'browser_consumer',
                MappingProxyType({str(k): str(v) for k, v in self.browser_consumer.items()}),
            )


def _public_credential_target_url(url: str, origin: str) -> str:
    """将原任务已公开声明的请求 URL 去掉 query/fragment 后保留路径；不可判定时退回规范 origin。"""
    if not isinstance(url, str) or not url.strip():
        return ''
    parts = urlsplit(url.strip())
    if parts.username or parts.password or not parts.hostname or parts.scheme not in {'http', 'https'}:
        return origin
    if credential_origin(urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))) != origin:
        return origin
    if not parts.path or parts.path == '/':
        return origin
    netloc = f'{parts.hostname.lower()}:{parts.port}' if parts.port else parts.hostname.lower()
    return urlunsplit((parts.scheme, netloc, parts.path, '', ''))


def _approved_browser_consumer(agent) -> dict[str, str] | None:
    """读取宿主已批准且激活的 browser-lite fill 绑定，未安装或未激活返回 None，不创建目录。"""
    config = getattr(agent, 'config', None)
    if config is not None and (
        not getattr(config, 'enable_plugins', True)
        or not getattr(config, 'enable_tools', True)
    ):
        return None
    home_paths = getattr(agent, 'home_paths', None)
    root = getattr(home_paths, 'root', None)
    if home_paths is None or root is None:
        return None
    try:
        from ..plugin_activation_ref import PluginActivationRef
        from ..plugin_install_store import PluginInstallStore
        from ..plugin_runtime import plugin_tool_name
        from .owner_policy import resolve_effective_owner_policy
        from .owner_resolver import OwnerIdentity, resolve_owner_home

        fill_tool_name = plugin_tool_name('browser-lite', 'fill')
        disabled = set(getattr(resolve_effective_owner_policy(home_paths), 'disabled_tools', ()) or ())
        if fill_tool_name in disabled or 'browser-lite' in disabled:
            return None
        candidate_owners = []
        reg_owner = getattr(getattr(agent, 'tools', None), '_plugin_owner', None)
        if reg_owner is not None:
            candidate_owners.append(reg_owner)
        main_owner = resolve_owner_home(root, OwnerIdentity.local_main())
        if all(item.home_dir != main_owner.home_dir for item in candidate_owners):
            candidate_owners.append(main_owner)
        if getattr(home_paths, 'owner_provider', '') == 'local' and getattr(home_paths, 'owner_kind', '') == 'user':
            priv_owner = resolve_owner_home(
                root,
                OwnerIdentity(home_paths.owner_provider, home_paths.owner_kind, home_paths.owner_id),
            )
            if all(item.home_dir != priv_owner.home_dir for item in candidate_owners):
                candidate_owners.append(priv_owner)
        for plugin_owner in candidate_owners:
            table_path = plugin_owner.plugins_dir / 'installations.json'
            if not table_path.is_file():
                continue
            for entry in PluginInstallStore(plugin_owner).snapshot():
                if (
                    entry.manifest.plugin_id != 'browser-lite'
                    or not entry.enabled
                    or entry.manifest.is_content_only
                    or not entry.activation_id
                ):
                    continue
                if not any(tool.name == 'fill' for tool in getattr(entry.manifest, 'tools', ())):
                    continue
                ref = PluginActivationRef.from_owner(
                    plugin_owner, 'browser-lite', entry.activation_id, config=config,
                )
                verified = ref.require()
                return {
                    'installation_ref': verified.installation_ref,
                    'package_sha256': verified.package_sha256,
                    'activation_id': verified.activation_id,
                    'tool': 'fill',
                }
    except (OSError, PermissionError, RuntimeError, TypeError, ValueError):
        return None
    return None


# LLM: 认证方案只是受控配置和输入字段描述，不承载秘密；变化时同步卡片、工具 schema 与生命周期测试。
# 类用途: 汇总凭据表单、认证、期限和更新引用，避免创建接口不断堆积参数。
@dataclass(frozen=True)
class CredentialCardOptions:
    fields: object = None
    auth_config: object = None
    ttl_seconds: int | None = None
    refresh_ref: str = ''
    collection_mode: str = 'username_password'
    access_scope: str = ''


# LLM: 新群授权显式落原row并签名；原actor/输入/监控仍为出处，group只改变使用范围，个人与旧task不推断升级。
# 函数用途: 为活动任务发布有界卡片，群默认共享、个人限定任务；发送意图仍先落原记录。
def request_credential_card(agent, *, url='', authentication='collected', header_name='Authorization', job_id='', call_id='', options=None):
    options = options or CredentialCardOptions()
    fields, auth_config, ttl_seconds, refresh_ref = options.fields, options.auth_config, options.ttl_seconds, options.refresh_ref
    current = getattr(agent, '_current_run_params', None)
    if current is None:
        raise CredentialInputError('CREDENTIAL_TASK_NOT_ACTIVE', '当前没有可绑定的活动任务，未发送凭据卡。')
    scope = tool_approval_scope(agent, str(current.run_id or current.task_id))
    if not scope.active:
        raise CredentialInputError('CREDENTIAL_TASK_NOT_ACTIVE', '原任务已失效，未发送凭据卡。')
    channel, actor = approval_recipient(agent, scope)
    if channel != 'feishu':
        raise CredentialInputError('CREDENTIAL_CHANNEL_UNAVAILABLE', '当前来源不支持私人凭据卡，不能改在群里发送密码。')
    header_name = credential_header_name(header_name)
    fields = collection_fields(authentication, fields, options.collection_mode)
    auth_config = credential_auth_config(authentication, auth_config, fields)
    capability = capability_config_for_agent(agent)
    if capability is None:
        raise CredentialInputError('TOOL_UNAVAILABLE', '凭据有效期配置不可用，未发送卡片。', stage='configuration', effect_outcome='not_started')
    if options.access_scope not in {'', 'owner', 'task', 'group'}:
        raise ValueError('凭据使用范围无效')
    origin = credential_origin(url) if url else ''
    if options.access_scope == 'owner':
        if owner_identity_from_config(agent.config).owner_kind == 'group':
            raise ValueError('群来源不能登记个人站点凭据')
        if not origin:
            raise CredentialInputError('CREDENTIAL_BINDING_INVALID', '个人站点凭据必须绑定目标 URL。')
        access_scope = 'owner'
    elif options.access_scope == 'task':
        access_scope = 'task'
    else:
        access_scope = new_credential_access_scope(agent, scope, url=url)
        if options.access_scope == 'group' and access_scope != 'group':
            raise ValueError('当前来源不能登记群共享凭据')
    if access_scope == 'owner':
        ttl_seconds = credential_seconds(options.ttl_seconds, default=0, allow_unlimited=True)
    else:
        ttl_seconds = credential_seconds(options.ttl_seconds, default=capability.im_task_credentials_ttl_seconds, allow_unlimited=True)
    if job_id:
        job = agent.scheduler_repository.get_job(job_id)
        creator = job.get('creator') or {}
        if owner_identity_from_config(agent.config).owner_kind == 'group' and creator != {'channel':channel,'actor_id':actor}:
            raise CredentialInputError('CREDENTIAL_NOT_AUTHORIZED', '这项监控不属于当前发起人，未发送凭据卡。')
    home = agent.home_paths.owner_home_dir
    with credential_stage("key_initialization", key_validation=True):
        _cipher(home)
    from ..conversation.im_tool_approval import _scope_identity
    if authentication == 'login' and origin and credential_origin(auth_config['url']) != origin:
        raise ValueError('登录地址必须属于同一接口 origin')
    browser_consumer = _approved_browser_consumer(agent)
    public_target_url = _public_credential_target_url(url, origin) if origin else ''
    with credential_stage("key_reference", key_validation=True):
        identity = json.dumps({'scope':_scope_identity(scope),'call_id':call_id},sort_keys=True).encode()
        reference = refresh_ref or 'cred_'+(hmac.digest(interaction_key(home,create=True),identity,'sha256').hex()[:32] if call_id else uuid.uuid4().hex)
        schema_version = 'task_credential.v2' if access_scope == 'owner' else 'task_credential.v1'
        payload = {'kind':'task_credential','owner':asdict(owner_identity_from_config(agent.config)), 'channel':channel,
                   'actor_id':actor,'expires_at':time.time()+1800, 'credential_ref':reference,'scope':_scope_identity(scope),
                   'access_scope':access_scope}
        if schema_version == 'task_credential.v2':
            payload.update(schema_version=schema_version, origin=origin, collection_mode=options.collection_mode)
        if browser_consumer is not None:
            payload['browser_consumer'] = browser_consumer
        owner = owner_identity_from_config(agent.config)
        if owner.owner_kind == 'group':
            payload.update(presentation={'kind':'group','chat_id':owner.owner_id}, submitter_policy='current_group_member')
        signed = sign_interaction_reference(home,payload)
    row = {**payload,'schema_version':schema_version,'reference':signed,'status':'pending',
           'origin':origin,'target_url':url,'collection_mode':options.collection_mode,'authentication':authentication,
           'header_name':header_name,'job_id':job_id,'fields':fields,'auth_config':auth_config,
           'ttl_seconds':ttl_seconds,'generation':1,'delivery_state':'not_started'}
    if public_target_url:
        row['public_target_url'] = public_target_url
    path, previous, reused = _persist_credential_request(agent, row, scope, refresh_ref)
    if reused:
        return row['credential_ref'], scope
    _register_credential_input(agent, scope, row, path, None if row.get('replaces_ref') else previous)
    if previous and previous.get('status') == 'pending':
        from .task_credential_forms import retire_credential_input
        retire_credential_input(agent, previous)
    context = DeliveryContext(channel=channel,target=payload.get('presentation', {}).get('chat_id', actor),
        thread_id=scope.thread_id,task_id=scope.run_id,mode='reply',idempotency_key=row['input_id'])
    _prepare_credential_delivery(agent, path, row)
    with credential_stage('delivery', effect_outcome='unknown'):
        receipt = agent.delivery_service.deliver_control_card(context,credential_card(row),summary='请在凭据卡填写接口信息。')
    _commit_credential_receipt(agent, path, row, receipt)
    return row['credential_ref'], scope


# LLM: 重发身份/实时成员在原锁外核验，锁内CAS同次观察；成功替换沿原replaces_ref/generation留出处，不另建目录或alias。
# 函数用途: 保存或复用卡片请求，旧ready保留到新输入完成，待填取消不会使旧授权退休。
def _persist_credential_request(agent, row, scope, refresh_ref):
    home = agent.home_paths.owner_home_dir
    path = credential_path(home, row['credential_ref'])
    observed = None
    if refresh_ref:
        from .task_credential_forms import assert_credential_reissue_scope
        observed = read_credential_row(path, stage='reissue_preflight')
        assert_credential_reissue_scope(agent, observed, scope)
    with credential_stage('record_prepare'):
        with _credential_mutation(agent,path) as admission:
            previous = read_credential_row(path, stage='record_read')
            if observed is not None:
                _assert_credential_unchanged(agent, observed, previous, scope)
            if refresh_ref and not previous:
                raise ValueError('待更新的凭据不存在')
            if _reuse_credential_request(agent, previous, row, scope, row['origin'], refresh_ref):
                row.update(previous)
                return path, previous, True
            if refresh_ref and previous.get('status') == 'ready':
                row.update(credential_ref='cred_'+uuid.uuid4().hex, replaces_ref=refresh_ref,
                    previous_submitter_actor_id=previous.get('submitter_actor_id', previous['actor_id']),
                    replaces_generation=previous.get('generation', 1))
                path = credential_path(home, row['credential_ref'])
            from .task_credential_replay import credential_input_id
            row['input_id'] = credential_input_id(row['credential_ref'],row['generation'])
            signed_fields = {key:row[key] for key in ('kind','owner','channel','actor_id','expires_at','credential_ref','scope','generation')}
            signed_fields.update({key:row[key] for key in ('presentation','submitter_policy','access_scope',
                'replaces_ref','replaces_generation','previous_submitter_actor_id','browser_consumer') if key in row})
            if row.get('schema_version') == 'task_credential.v2':
                signed_fields.update({key: row[key] for key in ('schema_version', 'origin', 'collection_mode') if key in row})
            row['reference'] = sign_interaction_reference(home, signed_fields)
            if row.get('replaces_ref'):
                with locked_json_path(path):
                    _save_credential_row(admission,path,row)
            else:
                _save_credential_row(admission,path,row)
    return path, previous, False


# LLM: 发网络前锁内复读输入/签名，发送意图先落原账；前置失败只结算本次输入，不掩盖运输未知。
# 函数用途: 原子登记即将发送的代次，阻止并发重发或存储失败后盲重放。
def _prepare_credential_delivery(agent, path, row):
    try:
        with credential_stage('delivery_prepare'):
            with _credential_mutation(agent,path) as admission:
                latest = read_credential_row(path, stage='delivery_prepare_read')
                if latest.get('input_id') != row['input_id'] or latest.get('reference') != row['reference']:
                    raise CredentialInputError('CREDENTIAL_INPUT_SUPERSEDED', '本次输入已被新代次替代，未发送卡片。', stage='delivery_prepare')
                row['delivery_state'] = 'started'
                _save_credential_row(admission,path,row)
    except CredentialInputError:
        from ..conversation.input_dependencies import resolve_input_dependency
        with credential_stage('input_cleanup'):
            resolve_input_dependency(agent, row['input_id'], status='failed')
        raise


# LLM: 明确拒绝先结算原输入；回执写入失败按真实运输事实分类，保存前比较代次和签名，不覆盖并发新卡或已提交状态。
# 函数用途: 保存原卡投递结果，已发送或运输未知不得盲重发，不记录供应商正文。
def _commit_credential_receipt(agent, path, row, receipt):
    unknown = receipt.error_code in {'CHANNEL_CONTROL_IO_FAILED', 'CHANNEL_CONTROL_RECEIPT_MISSING'}
    effect = 'unknown' if receipt.ok else (getattr(receipt, 'effect_outcome', '') or ('unknown' if unknown else 'not_started'))
    if not receipt.ok:
        from ..conversation.input_dependencies import resolve_input_dependency
        with credential_stage('input_settlement', effect_outcome=effect):
            resolve_input_dependency(agent, row['input_id'], status='failed')
    provider_code = getattr(receipt, 'provider_code', None)
    diagnostic = {'credential_ref': row['credential_ref']}
    if isinstance(provider_code, int) and not isinstance(provider_code, bool):
        diagnostic['provider_code'] = provider_code
    with credential_stage('receipt_commit', effect_outcome=effect, diagnostic=diagnostic):
        with _credential_mutation(agent,path) as admission:
            latest = read_credential_row(path, stage='receipt_read', effect_outcome=effect)
            _merge_credential_receipt(latest, row, receipt, effect)
            _save_credential_row(admission,path,latest)
    if not receipt.ok:
        raise CredentialInputError(receipt.error_code or 'CREDENTIAL_CARD_NOT_DELIVERED',
            '凭据卡没有可验证送达回执，请核对原操作；不要在聊天中发送密码。',
            stage='delivery', effect_outcome=effect, diagnostic=diagnostic)


# LLM: 回执只合并相同签名/input/generation，保护已提交或终止状态；异代不覆盖，仍报告旧发送事实。
# 函数用途: 在原记录锁内合并运输回执，避免慢发送回写新卡或清掉已加密凭据。
def _merge_credential_receipt(latest, row, receipt, effect):
    if any(latest.get(key) != row.get(key) for key in ('reference', 'input_id', 'generation')):
        raise CredentialInputError('CREDENTIAL_RECEIPT_SUPERSEDED', '原卡已发送处理，但输入已换代，请核对原操作。',
            stage='receipt_commit', effect_outcome=effect, diagnostic={'credential_ref':row['credential_ref']})
    status = latest.get('status')
    if status == 'pending':
        status = 'pending' if receipt.ok else 'failed'
    latest.update(message_id=receipt.message_id, chat_id=receipt.chat_id, status=status,
        delivery_state='confirmed' if receipt.ok else ('unknown' if effect != 'not_started' else 'not_started'))
    provider_code = getattr(receipt, 'provider_code', None)
    if isinstance(provider_code, int) and not isinstance(provider_code, bool):
        latest['provider_code'] = provider_code


# LLM: 调用方已锁外核重发管理权并锁内CAS；本函数仅判发送/参数幂等，不发网络、不推断权限或旧卡结果。
# 函数用途: 复用准确原请求或递增未发送代次，发送未知时拒绝盲重放。
def _reuse_credential_request(agent, previous, row, scope, url, refresh_ref):
    if not previous:
        return False
    keys = ('channel','access_scope') if refresh_ref and previous.get('access_scope') == 'group' else ('channel','job_id') if refresh_ref else ('actor_id','channel','origin','authentication','header_name','job_id','fields','auth_config','access_scope')
    if any(previous.get(key)!=row.get(key) for key in keys) or (not refresh_ref and previous.get('scope') != row['scope']):
        raise ValueError('同一调用凭据参数冲突')
    if previous.get('delivery_state') == 'started' and not previous.get('message_id'):
        raise CredentialInputError('CREDENTIAL_DELIVERY_RECONCILIATION_REQUIRED',
            '原卡发送已启动但回执未提交，请核对原操作。', stage='delivery_recovery', effect_outcome='unknown')
    if not refresh_ref and previous.get('message_id') and previous.get('status') in {'pending','ready'}:
        return True
    elif not refresh_ref and previous.get('delivery_state') != 'not_started':
        raise CredentialInputError('CREDENTIAL_DELIVERY_RECONCILIATION_REQUIRED',
            '原卡结果需要核对，不能重复发卡。', stage='delivery_recovery', effect_outcome='unknown')
    row['generation'] = int(previous.get('generation', 1)) + 1
    return False


# LLM: 只有本次未发卡generation可撤回；登记失败在原记录锁内撤回后报告 typed input_registration，不能误判成 origin/actor 拒绝。
# 函数用途: 登记凭据通用输入，失败时删除新行或恢复先前授权；不发送卡片、泄露异常正文或留永久等待。
def _register_credential_input(agent, scope, row, path, previous):
    from ..conversation.input_dependencies import create_input_dependency, input_dependency_path
    try:
        replaces_input_id = ''
        if previous and previous.get('status') == 'pending' and previous.get('scope', {}).get('root_task_id') == row['scope']['root_task_id']:
            dependency = read_json_object_report(input_dependency_path(agent, previous['input_id']),
                context='credentials.replace_input').payload
            if dependency.get('status') == 'pending':
                replaces_input_id = previous['input_id']
        create_input_dependency(agent, scope, input_id=row['input_id'], provider='credential',
            expires_at=row['expires_at'], reference=row['credential_ref'],
            metadata={'channel': row['channel'], 'actor_id': row['actor_id']},
            replaces_input_id=replaces_input_id)
    except (OSError, ValueError, RuntimeError, TypeError, KeyError) as exc:
        with credential_stage('input_rollback'):
            _settle_partial_credential_input(agent, row['input_id'])
            _rollback_credential_row(agent, path, row, previous)
        raise CredentialInputError('CREDENTIAL_INPUT_UNAVAILABLE',
            '原任务私人输入未能登记：请检查任务输入容量、状态与存储；未发送卡片。',
            stage='input_registration', diagnostic={'exception_type': type(exc).__name__,
                **({'errno': exc.errno} if isinstance(exc, OSError) and isinstance(exc.errno, int) else {})}) from None


# LLM: 登记可能在写入后、安排期限前失败；只结算已存在的原输入，不补造输入或移除别的代次。
# 函数用途: 清理部分登记留下的等待槽，沿原输入账发布终态和释放引用。
def _settle_partial_credential_input(agent, input_id):
    from ..conversation.input_dependencies import input_dependency_path, resolve_input_dependency
    if input_dependency_path(agent, input_id).exists():
        resolve_input_dependency(agent, input_id, status='failed')


# LLM: 只撤回本次相同输入和签名，不改变并发新代次；写入沿原配额和锁，不重建记录。
# 函数用途: 输入登记失败时恢复旧凭据或删除本次尚未发送的行。
def _rollback_credential_row(agent, path, row, previous):
    with _credential_mutation(agent,path) as admission:
        current = read_credential_row(path, stage='rollback_read')
        if current and current.get('input_id') == row['input_id'] and current.get('reference') == row['reference']:
            if previous:
                _save_credential_row(admission,path,previous)
            else:
                path.unlink(missing_ok=True)


# LLM: 原 owner 配额锁先于记录锁，密文写入不能绕过其它文件工具的同一磁盘上限。
# 函数用途: 取得一次凭据状态修改事务，不持锁发送网络请求。
@contextmanager
def _credential_mutation(agent,path):
    with agent.owner_quota.admission() as admission:
        with locked_json_path(path):
            yield admission


# LLM: 检查最终 UTF-8 字节而非字符，原子 0600 写入只在同一配额和记录锁内调用。
# 函数用途: 安全保存待输入或加密状态，不产生普通聊天和秘密日志。
def _save_credential_row(admission,path,row):
    text=json.dumps(row,ensure_ascii=False)
    admission.check([OwnerQuotaChange(path,len(text.encode()))])
    write_private_text_file_atomic_unlocked(path,text)


# LLM: 展示只读原access_scope和结构字段；username为text，其它凭据password且无default，show_icon不控制明文眼睛；提交仍经原MAC/成员门。
# 函数用途: 在原表单说明使用范围；账号为普通文本、其它凭据遮蔽且从不预填，内部控件名不与业务字段冲突。
def credential_card(row):
    browser_hint = '\n已绑定供此浏览器填表及当前获准工具使用。' if row.get('browser_consumer') else ''
    is_owner = row.get('access_scope') == 'owner'
    if is_owner:
        inputs = [{
            'tag': 'input',
            'name': field['name'],
            'required': True,
            **({'input_type': 'text'} if field['name'] == 'username' else {'input_type': 'password', 'show_icon': True}),
            'label': {'tag': 'plain_text', 'content': field['label']},
        } for field in row.get('fields', credential_fields(row['authentication']))]
        submit_text = '更新并保存凭据' if row.get('replaces_ref') else '保存凭据'
        submit = {
            'tag': 'button',
            'name': '_credential_submit',
            'action_type': 'form_submit',
            'type': 'primary',
            'text': {'tag': 'plain_text', 'content': submit_text},
            'value': {'kind': 'my_agent_task_credential', 'reference': row['reference'], 'decision': 'submit'},
        }
        cancel = {
            'tag': 'button',
            'text': {'tag': 'plain_text', 'content': '取消'},
            'value': {'kind': 'my_agent_task_credential', 'reference': row['reference'], 'decision': 'cancel'},
        }
        site_target = row.get('origin') or row.get('target_url') or ''
        div_text = (
            f"站点凭据安全填写：{site_target}\n"
            f"保存后仅绑定到你的个人账号与该站点，跨任务可自动复用；输入不会进入聊天、模型或记忆。{browser_hint}"
        )
        return {
            'config': {'wide_screen_mode': True, 'update_multi': True},
            'elements': [
                {'tag': 'div', 'text': {'tag': 'plain_text', 'content': div_text}},
                {'tag': 'form', 'name': '_credential_form', 'elements': [*inputs, submit]},
                {'tag': 'action', 'actions': [cancel]},
            ],
        }
    inputs = [{
        'tag': 'input',
        'name': field['name'],
        'required': True,
        **({'input_type': 'text'} if field['name'] == 'username' else {'input_type': 'password', 'show_icon': True}),
        'label': {'tag': 'plain_text', 'content': field['label']},
    } for field in row.get('fields', credential_fields(row['authentication']))]
    if not row.get('target_url', row.get('origin')):
        inputs.insert(0, {'tag': 'input', 'name': 'target_url', 'required': True, 'label': {'tag': 'plain_text', 'content': '目标链接'}})
    inputs.extend({
        'tag': 'input',
        'name': name,
        'required': False,
        'label': {'tag': 'plain_text', 'content': label},
    } for name, label in [('credential_name', '名称（可选）'), ('credential_note', '备注（可选）')])
    submit = {
        'tag': 'button',
        'name': '_credential_submit',
        'action_type': 'form_submit',
        'type': 'primary',
        'text': {'tag': 'plain_text', 'content': '保存并供本群助手使用' if row.get('access_scope') == 'group' else '提交'},
        'value': {'kind': 'my_agent_task_credential', 'reference': row['reference'], 'decision': 'submit'},
    }
    cancel = {
        'tag': 'button',
        'text': {'tag': 'plain_text', 'content': '取消'},
        'value': {'kind': 'my_agent_task_credential', 'reference': row['reference'], 'decision': 'cancel'},
    }
    target = '监控 ' + row['job_id'] if row['job_id'] else '当前任务'
    usage = ('本群成员可让助手复用；优先使用自己填写的账号，主动取消后可使用本群其它账号。'
             if row.get('access_scope') == 'group' else '仅限原发起人的当前任务或已绑定监控。')
    return {
        'config': {'wide_screen_mode': True, 'update_multi': True},
        'elements': [
            {'tag': 'div', 'text': {'tag': 'plain_text', 'content': f"{target}需要接口凭据：{row.get('target_url') or row['origin'] or '请填写目标链接'}\n{usage}\n输入不进入聊天、模型或记忆。{browser_hint}"}},
            {'tag': 'form', 'name': '_credential_form', 'elements': [*inputs, submit]},
            {'tag': 'action', 'actions': [cancel]},
        ],
    }


# LLM: 上游已MAC/actor/解锁；群共享关闭时不收旧group卡新值但允许取消；同卡同代终态和原期限不变。
# 函数用途: 接受输入或取消并加密保存；填写与保存后的期限分开，明文仅在首次提交进程内存在。
# LLM: 原Agent只提供原capability/输入/审计和动态发布hook；算法与C共用，不改变Count0初始化或回放故障接缝。
# 函数用途: 接受原卡提交，仍沿原库和原Wake恢复。
def submit_credential_card(agent, payload, body):
    from ..conversation.input_dependencies import (
        input_dependency_access,
        publish_credential_resolution,
    )
    from .interaction_actor_audit import record_interaction_actor
    from .task_credential_forms import retire_credential_input
    return submit_credential_card_for_owner(agent.home_paths.owner_home_dir, capability_config_for_agent(agent),
        input_dependency_access(agent), payload, body,
        audit=lambda actor: record_interaction_actor(agent,payload,decision='cancel',actor=actor),
        publish=lambda row: publish_credential_resolution(agent,row),
        retire=lambda row: retire_credential_input(agent,row))


def _runner_scope_is_current(agent, scope) -> bool:
    """核对当前 runner 的真实活动执行身份与代次；子代理或过期 attempt 不继承主回合扩展。"""
    current = getattr(agent, '_current_run_params', None)
    if current is None or not scope.active or not scope.thread_id:
        return False
    if str(getattr(current, 'task_id', '') or '') != scope.root_task_id:
        return False
    if scope.agent_kind == 'subagent':
        if (
            str(getattr(current, 'run_id', '') or '') != scope.run_id
            or not scope.execution_attempt_id
            or str(getattr(current, 'attempt_id', '') or '') != scope.execution_attempt_id
        ):
            return False
    else:
        if str(getattr(current, 'run_id', '') or '') != scope.run_id:
            return False
        if (scope.execution_attempt_id or getattr(current, 'attempt_id', '')) and (
            str(getattr(current, 'attempt_id', '') or '') != scope.execution_attempt_id
        ):
            return False
    return bool(tool_approval_scope_is_current(agent, scope))


def current_credential_actor_scope(agent) -> tuple[str, str, str]:
    """从当前活动 runner 解析 (owner_key, actor_id, canonical_thread_id)，不读取或解密凭据。"""
    from ..conversation.host_actor import HostActorError
    from ..conversation.tool_approval_scope import ToolApprovalScopeUnavailable

    current = getattr(agent, '_current_run_params', None)
    if current is None:
        raise CredentialInputError(
            'CREDENTIAL_NOT_AUTHORIZED',
            '当前调用缺少活动任务上下文，未使用凭据。',
            stage='execution_scope',
            effect_outcome='not_started',
        )
    try:
        scope = tool_approval_scope(agent, str(current.run_id or current.task_id))
    except ToolApprovalScopeUnavailable:
        raise CredentialInputError(
            'CREDENTIAL_TASK_NOT_ACTIVE',
            '当前任务执行范围不可用，未使用凭据。',
            stage='execution_scope',
            effect_outcome='not_started',
        ) from None
    if not _runner_scope_is_current(agent, scope):
        raise CredentialInputError(
            'CREDENTIAL_NOT_AUTHORIZED',
            '原任务执行已失效或代次已变化，未使用凭据。',
            stage='execution_scope',
            effect_outcome='not_started',
        )
    try:
        _channel, actor = approval_recipient(agent, scope)
    except HostActorError as exc:
        raise CredentialInputError(
            exc.error_code,
            str(exc),
            stage='authorization',
            effect_outcome='not_started',
        ) from None
    except ValueError:
        raise CredentialInputError(
            'CREDENTIAL_NOT_AUTHORIZED',
            '当前调用缺少可核验的通道发起人，未使用凭据。',
            stage='authorization',
            effect_outcome='not_started',
        ) from None
    owner = owner_identity_from_config(agent.config)
    canonical_thread = scope.thread_id
    if scope.agent_kind == 'subagent':
        link, err = agent.conversation_store.tasks.load_report(scope.root_task_id)
        if err or link is None or not link.thread_id:
            raise CredentialInputError(
                'CREDENTIAL_NOT_AUTHORIZED',
                '当前子执行缺少原主会话归属，未使用凭据。',
                stage='execution_scope',
                effect_outcome='not_started',
            )
        canonical_thread = link.thread_id
    return f'{owner.provider}/{owner.owner_kind}/{owner.owner_id}', actor, canonical_thread


def _same_actor_feishu_thread_continuation(agent, row, scope, *, channel: str, actor: str) -> bool:
    """核验普通非 job 的 task 卡在同 Feishu 主对话内由原本人跨 root 延续使用的全部条件。"""
    from ..conversation.host_actor import background_host_actor, resolve_host_actor
    from .group_authorization import group_chat_id
    from .group_authorization_context import current_group_authority, group_operation_actor
    from .task_credential_forms import assert_credential_record_signature

    if scope.agent_kind != 'main' or not _runner_scope_is_current(agent, scope):
        return False
    if channel != 'feishu' or row.get('channel') != 'feishu' or not actor or row.get('actor_id') != actor:
        return False
    submitter = row.get('submitter_actor_id')
    if submitter is not None:
        if submitter != actor:
            return False
    elif row.get('status') == 'ready':
        return False
    original_scope = row.get('scope')
    if not isinstance(original_scope, dict) or original_scope.get('agent_kind') != 'main':
        return False
    original_thread_id = str(original_scope.get('thread_id') or '').strip()
    original_root_id = str(original_scope.get('root_task_id') or '').strip()
    if not original_thread_id or not original_root_id or scope.thread_id != original_thread_id:
        return False
    try:
        assert_credential_record_signature(agent, row)
    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
        return False
    store = getattr(agent, 'conversation_store', None)
    if store is None:
        return False
    try:
        current_link, link_err = store.tasks.load_report(scope.root_task_id)
        thread, thread_err = store.threads.load_report(scope.thread_id)
        if (
            link_err
            or thread_err
            or current_link is None
            or thread is None
            or current_link.status != 'active'
            or current_link.thread_id != original_thread_id
        ):
            return False
        host_actor = resolve_host_actor(agent, scope.thread_id)
        owner_id = str(getattr(getattr(agent, 'home_paths', None), 'owner_id', '') or '')
        if (
            host_actor is None
            or host_actor.channel != 'feishu'
            or host_actor.actor_id != actor
            or host_actor.source_thread_id != original_thread_id
            or (owner_id and host_actor.owner_id != owner_id)
        ):
            return False
        orig_link, orig_err = store.tasks.load_report(original_root_id)
        if orig_err:
            return False
        if orig_link is not None:
            if orig_link.thread_id != original_thread_id:
                return False
            orig_actor = background_host_actor(agent, orig_link.thread_id, original_root_id)
            if (
                orig_actor is None
                or orig_actor.channel != 'feishu'
                or orig_actor.actor_id != actor
                or orig_actor.source_thread_id != original_thread_id
                or orig_actor.conversation_id != host_actor.conversation_id
            ):
                return False
        elif not row.get('chat_id'):
            return False
        owner = owner_identity_from_config(agent.config)
        presentation = row.get('presentation')
        is_group = owner.owner_kind == 'group' or (
            isinstance(presentation, dict) and presentation.get('kind') == 'group'
        )
        if is_group:
            if owner.owner_kind != 'group' or not isinstance(presentation, dict) or presentation.get('kind') != 'group':
                return False
            chat = group_chat_id(agent.home_paths)
            if presentation.get('chat_id') != chat or (row.get('chat_id') and row.get('chat_id') != chat):
                return False
            group_actor = group_operation_actor(agent, scope.thread_id)
            if (group_actor.channel, group_actor.actor_id) != ('feishu', actor):
                return False
            snapshot = current_group_authority(agent)
            if not _runner_scope_is_current(agent, scope) or actor not in snapshot.member_ids:
                return False
    except (AttributeError, KeyError, OSError, RuntimeError, TypeError, ValueError):
        return False
    return True


class _CredentialUnavailableError(ValueError):
    """凭据未就绪、已过期、已被拒绝或无可用候选的结构化异常（保持 ValueError 调用兼容）。"""

    error_code = 'CREDENTIAL_INPUT_UNAVAILABLE'


def _map_credential_use_error(exc: ValueError) -> CredentialInputError:
    """按结构化异常类型与显式 error_code 归一凭据使用错误，不匹配自然语言文案。"""
    if isinstance(exc, CredentialInputError):
        return exc
    from ..conversation.host_actor import HostActorError

    if isinstance(exc, HostActorError):
        return CredentialInputError(exc.error_code, str(exc), stage='authorization', effect_outcome='not_started')
    if isinstance(exc, _CredentialUnavailableError) or getattr(exc, 'error_code', '') == 'CREDENTIAL_INPUT_UNAVAILABLE':
        return CredentialInputError(
            'CREDENTIAL_INPUT_UNAVAILABLE',
            '凭据当前不可用（未就绪、已过期、已被拒绝或已撤销）；请用 request_credentials(operation="status") 核实并由本人更新，勿在聊天发送密码。',
            stage='credential_use',
            effect_outcome='not_started',
        )
    return CredentialInputError(
        'CREDENTIAL_NOT_AUTHORIZED',
        '当前调用者、会话或目标站点无权使用该凭据；请回到原发起会话或改用已获授权的引用，勿在聊天发送密码。',
        stage='authorization',
        effect_outcome='not_started',
    )


@contextmanager
def authorized_credential_material(
    agent,
    reference: str,
    *,
    consumer: str,
    target_url: str | None = None,
    expected_generation: int | None = None,
    browser_consumer: Mapping[str, str] | None = None,
) -> Iterator[CredentialMaterial]:
    """逐次验证当前 runner 身份、范围、签名、代次与有效期后解密，不在锁内或跨调用缓存明文。"""
    from .task_credential_forms import (
        assert_credential_record_signature,
        valid_browser_consumer_binding,
    )
    from .task_credential_groups import group_credential_access

    if not isinstance(consumer, str) or consumer not in {'shell', 'browser', 'http'}:
        raise CredentialInputError(
            'TOOL_INVALID_ARGUMENTS',
            '凭据消费方参数无效：consumer 仅支持受控的 shell、browser 或 http。',
            stage='preflight',
            effect_outcome='not_started',
            diagnostic={'field': 'consumer'},
        )
    home_paths = getattr(agent, 'home_paths', None)
    if home_paths is None:
        raise CredentialInputError(
            'CREDENTIAL_NOT_AUTHORIZED',
            '当前执行缺少受管用户目录上下文，未使用凭据。',
            stage='execution_scope',
            effect_outcome='not_started',
        )
    home = home_paths.owner_home_dir
    try:
        requested_path = credential_path(home, reference)
    except ValueError:
        raise CredentialInputError(
            'TOOL_INVALID_ARGUMENTS',
            'credential_ref 格式无效，请传入宿主返回的 cred_<32位十六进制> 引用。',
            stage='preflight',
            effect_outcome='not_started',
            diagnostic={'field': 'credential_ref'},
        ) from None
    if expected_generation is not None and (type(expected_generation) is not int or expected_generation < 1):
        raise CredentialInputError(
            'TOOL_INVALID_ARGUMENTS',
            'expected_generation 必须是正整数。',
            stage='preflight',
            effect_outcome='not_started',
            diagnostic={'field': 'expected_generation'},
        )
    if consumer == 'shell':
        if target_url is not None:
            raise CredentialInputError(
                'TOOL_INVALID_ARGUMENTS',
                'shell 凭据授权不接受外部 target_url 参数；站点绑定直接取自已认证凭据记录。',
                stage='preflight',
                effect_outcome='not_started',
                diagnostic={'field': 'target_url'},
            )
    else:
        if not isinstance(target_url, str) or not target_url.strip():
            raise CredentialInputError(
                'TOOL_INVALID_ARGUMENTS',
                f'{consumer} 凭据授权必须提供真实目标 URL 以核对同源范围。',
                stage='preflight',
                effect_outcome='not_started',
                diagnostic={'field': 'target_url'},
            )
        try:
            credential_origin(target_url)
        except ValueError:
            raise CredentialInputError(
                'TOOL_INVALID_ARGUMENTS',
                '目标 URL 格式无效，无法核验凭据同源范围。',
                stage='preflight',
                effect_outcome='not_started',
                diagnostic={'field': 'target_url'},
            ) from None
    if browser_consumer is not None and (
        consumer != 'browser' or not valid_browser_consumer_binding(dict(browser_consumer))
    ):
        raise CredentialInputError(
            'CREDENTIAL_NOT_AUTHORIZED',
            '浏览器消费者绑定无效或与消费方类型不匹配，未使用凭据。',
            stage='authorization',
            effect_outcome='not_started',
        )
    capability = capability_config_for_agent(agent)
    if capability is None or not capability.im_task_credentials_enabled:
        raise CredentialInputError(
            'TOOL_UNAVAILABLE',
            '当前未启用私人凭据输入。',
            stage='configuration',
            effect_outcome='not_started',
        )
    owner_key, actor_id, canonical_thread = current_credential_actor_scope(agent)
    current = agent._current_run_params
    scope = tool_approval_scope(agent, str(current.run_id or current.task_id))
    initial_row = read_credential_row(requested_path, stage='credential_use')
    if not initial_row:
        raise CredentialInputError(
            'CREDENTIAL_INPUT_UNAVAILABLE',
            '未找到对应的凭据记录；请用 request_credentials 查询可用引用，勿在聊天发送密码。',
            stage='credential_use',
            effect_outcome='not_started',
        )
    try:
        assert_credential_record_signature(agent, initial_row)
    except ValueError:
        raise CredentialInputError(
            'CREDENTIAL_NOT_AUTHORIZED',
            '原凭据卡签名无法核验，未使用凭据。',
            stage='authorization',
            effect_outcome='not_started',
        ) from None
    lookup_url = target_url if consumer != 'shell' else (initial_row.get('origin') or '')
    try:
        if initial_row.get('access_scope') != 'group':
            _assert_credential_scope(agent, initial_row, scope, lookup_url, require_ready=False, operation='use')
        elif not lookup_url:
            group_credential_access(agent, initial_row, scope, operation='status')
            raise CredentialInputError(
                'CREDENTIAL_INPUT_UNAVAILABLE',
                '群凭据尚未填写完成，不能使用。',
                stage='credential_use',
                effect_outcome='not_started',
            )
        if not lookup_url:
            raise CredentialInputError(
                'CREDENTIAL_INPUT_UNAVAILABLE',
                '凭据尚未填写完成或缺少目标站点，不能使用。',
                stage='credential_use',
                effect_outcome='not_started',
            )
        observed = _credential_for_use(agent, reference, scope, lookup_url)
        assert_credential_record_signature(agent, observed)
    except ValueError as exc:
        raise _map_credential_use_error(exc) from None
    if expected_generation is not None and observed.get('generation', 1) != expected_generation:
        raise CredentialInputError(
            'CREDENTIAL_INPUT_UNAVAILABLE',
            '凭据代次已变化，请用 request_credentials(operation="status") 重新核对后再执行。',
            stage='credential_use',
            effect_outcome='not_started',
        )
    signed_browser = observed.get('browser_consumer')
    if consumer == 'browser' and signed_browser is not None:
        if not valid_browser_consumer_binding(signed_browser):
            raise CredentialInputError(
                'CREDENTIAL_NOT_AUTHORIZED',
                '原凭据卡的浏览器绑定无效，未使用凭据。',
                stage='authorization',
                effect_outcome='not_started',
            )
        actual_browser = dict(browser_consumer) if browser_consumer is not None else _approved_browser_consumer(agent)
        if actual_browser != signed_browser:
            raise CredentialInputError(
                'CREDENTIAL_NOT_AUTHORIZED',
                '当前浏览器插件安装或激活代次与原凭据卡绑定不符，未使用凭据。',
                stage='authorization',
                effect_outcome='not_started',
            )
    effective_path = credential_path(home, observed['credential_ref'])
    raw_values: dict[str, str] | None = None
    with locked_json_path(effective_path):
        locked_row = read_credential_row(effective_path, stage='credential_use')
        try:
            _assert_credential_unchanged(agent, observed, locked_row, scope)
        except ValueError:
            raise CredentialInputError(
                'CREDENTIAL_INPUT_UNAVAILABLE',
                '凭据记录或当前任务执行在核验期间已变化，未使用凭据。',
                stage='credential_use',
                effect_outcome='not_started',
            ) from None
        if (
            locked_row.get('status') != 'ready'
            or locked_row.get('verification_status') == 'rejected'
            or credential_ready_expired(locked_row, now=time.time())
            or not isinstance(locked_row.get('ciphertext'), str)
            or not locked_row['ciphertext']
        ):
            raise CredentialInputError(
                'CREDENTIAL_INPUT_UNAVAILABLE',
                '凭据未就绪、已过期、已被拒绝或已撤销，未使用凭据。',
                stage='credential_use',
                effect_outcome='not_started',
            )
        with credential_stage('decryption', key_validation=True):
            decrypted = _cipher(home, existing_only=True).decrypt(locked_row['ciphertext'].encode())
            decoded = json.loads(decrypted.decode('utf-8'))
        declared_names = {
            field_spec['name']
            for field_spec in locked_row.get('fields', ())
            if isinstance(field_spec, dict) and isinstance(field_spec.get('name'), str)
        }
        if (
            not isinstance(decoded, dict)
            or not declared_names
            or set(decoded) != declared_names
            or any(not isinstance(k, str) or not isinstance(v, str) for k, v in decoded.items())
        ):
            raise CredentialInputError(
                'CREDENTIAL_RECORD_INVALID',
                '凭据密文字段与声明结构不一致，未使用凭据。',
                stage='decryption',
                effect_outcome='not_started',
            )
        raw_values = dict(decoded)
        normalized_origin = credential_origin(locked_row['origin'])
        private_target = str(locked_row.get('target_url') or normalized_origin)
        effective_generation = int(locked_row.get('generation', 1))
        effective_ref = str(locked_row['credential_ref'])
        effective_scope = str(locked_row.get('access_scope', 'task'))
        effective_browser = (
            dict(signed_browser)
            if signed_browser is not None
            else (dict(browser_consumer) if browser_consumer is not None else None)
        )
    try:
        if locked_row.get('access_scope') == 'group':
            try:
                group_credential_access(agent, locked_row, scope, operation='use')
            except ValueError as exc:
                raise _map_credential_use_error(exc) from None
        if not _runner_scope_is_current(agent, scope):
            raise CredentialInputError(
                'CREDENTIAL_NOT_AUTHORIZED',
                '原任务执行在凭据移交前已结束，未使用凭据。',
                stage='execution_scope',
                effect_outcome='not_started',
            )
        material = CredentialMaterial(
            credential_ref=effective_ref,
            generation=effective_generation,
            origin=normalized_origin,
            target_url=private_target,
            values=raw_values,
            requested_credential_ref=reference,
            access_scope=effective_scope,
            owner_id=owner_key,
            actor_id=actor_id,
            thread_id=canonical_thread,
            browser_consumer=effective_browser,
        )
        yield material
    finally:
        if raw_values is not None:
            raw_values.clear()


# LLM: 私密凭据守原根，group由同群当前成员按实际提供者择取；仅此入口解密，不能把引用作为跨群授权。
# 函数用途: 为本次请求取得允许使用的认证头和净化值，不向模型或普通shell交秘密。
def credential_headers(agent,reference,url):
    current = agent._current_run_params
    scope = tool_approval_scope(agent,str(current.run_id or current.task_id))
    if not scope.active or not tool_approval_scope_is_current(agent,scope):
        raise ValueError('原任务执行已失效')
    row = _credential_for_use(agent, reference, scope, url)
    value = json.loads(_cipher(agent.home_paths.owner_home_dir).decrypt(row['ciphertext'].encode()))
    return _credential_headers_from_values(row, value)


# LLM: 调用方已核active scope；共享选择只在原group+准确origin内，返回当前实际row，不写alias、任务或凭据。
# 函数用途: 让普通HTTP和来源请求复用同一凭据选择，原引用失效后仍遵循用户的本群择取规则。
def _credential_for_use(agent, reference, scope, url):
    row = read_credential_row(credential_path(agent.home_paths.owner_home_dir, reference), stage='credential_use')
    if row.get('access_scope') == 'group':
        from .task_credential_groups import resolve_group_credential
        row = resolve_group_credential(agent, row, scope, origin=credential_origin(url))
        if (row.get('owner') != asdict(owner_identity_from_config(agent.config))
                or row.get('access_scope') != 'group' or row.get('origin') != credential_origin(url)
                or row.get('status') != 'ready' or row.get('verification_status') == 'rejected'
                or credential_ready_expired(row, now=time.time())):
            raise _CredentialUnavailableError('本群没有本次可使用的站点凭据')
    else:
        _assert_credential_scope(agent, row, scope, url)
    import logging
    logging.getLogger(__name__).info('credential_selection requested_ref=%s effective_ref=%s access_scope=%s',
        reference, row['credential_ref'], row.get('access_scope', 'task'))
    return row


# LLM: 纯格式化只接收已授权且解密的原row/value，不读文件、发网络或自行选择凭据；调用方必须提供净化返回值。
# 函数用途: 共用基本认证头的组装，避免HTTP入口重复查群成员和重复解密。
def _credential_headers_from_values(row, value):
    if row['authentication'] not in {'basic','bearer','header'}:
        raise ValueError('此认证需要受控 HTTP 请求入口')
    secret = value['secret']
    if row['authentication']=='basic':
        token = base64.b64encode(f"{value['username']}:{secret}".encode()).decode()
        headers = {'Authorization':'Basic '+token}
    elif row['authentication']=='bearer':
        token = secret
        headers = {'Authorization':'Bearer '+secret}
    else:
        token = secret
        headers = {row['header_name']:secret}
    return headers, tuple({*value.values(),token,*headers.values()})


# LLM: 准确owner/channel/origin先核，显式group交同一动作门，task继续原actor/root/job；管理不能继承共享使用权。
# 函数用途: 核对具体凭据授权；HTTP需自动择取时先走_credential_for_use，状态/配置/撤销只裁决原引用。
def _assert_credential_scope(agent, row, scope, url, *, require_ready=True, operation='use'):
    channel, actor = approval_recipient(agent,scope)
    # 查询/撤销是管理动作：本人或核验过的本机管理员前台可在任何后续任务里执行，不要求原任务仍在运行；
    # 解密使用（use）仍严格限定在原任务内。
    manage = operation in ('status', 'revoke')
    admin = manage and _manages_as_foreground_admin(agent)
    if (not row or row.get('owner') != asdict(owner_identity_from_config(agent.config))
            or (row['channel'] != channel and not admin) or row['origin'] != (credential_origin(url) if url else '')):
        raise ValueError('凭据不属于此调用或接口')
    if row.get('schema_version', 'task_credential.v1') not in {'task_credential.v1', 'task_credential.v2'}:
        raise ValueError('凭据不属于此调用或接口')
    if require_ready and (row.get('verification_status') == 'rejected' or row.get('status')!='ready' or credential_ready_expired(row, now=time.time())):
        raise _CredentialUnavailableError('凭据未就绪、已过期或已撤销，请重新申请私人输入')
    from .task_credential_groups import group_credential_access
    if group_credential_access(agent, row, scope, operation=operation):
        return
    if row.get('access_scope') == 'owner':
        if admin:
            return
        if (
            row.get('schema_version') != 'task_credential.v2'
            or owner_identity_from_config(agent.config).owner_kind == 'group'
            or row.get('actor_id') != actor
            or (row.get('submitter_actor_id') and row.get('submitter_actor_id') != actor)
        ):
            raise ValueError('凭据不属于此调用或接口')
        return
    if row.get('access_scope', 'task') != 'task':
        raise ValueError('凭据不属于此调用或接口')
    if manage and (admin or actor in (row.get('actor_id'), row.get('submitter_actor_id'))):
        return
    if row['actor_id'] != actor:
        raise ValueError('凭据不属于此调用或接口')
    if row['job_id']:
        run = agent.scheduler_repository.get_active_run(scope.root_task_id)
        if (run or {}).get('job_id')!=row['job_id'] and scope.root_task_id!=row['scope']['root_task_id']:
            raise ValueError('凭据不属于此监控')
    elif scope.root_task_id!=row['scope']['root_task_id']:
        if not _same_actor_feishu_thread_continuation(agent, row, scope, channel=channel, actor=actor):
            raise ValueError('凭据不属于此任务')


# LLM: 只认宿主前台事实与当前绑定的本机管理员身份（每次调用重新核验）；任何读取异常都按非管理员处理。
# 函数用途: 判断本次凭据管理是否由核验过的本机管理员前台发起。
def _manages_as_foreground_admin(agent):
    from .owner_access import is_foreground_admin_interactive

    try:
        return is_foreground_admin_interactive(agent)
    except (AttributeError, OSError, TypeError, ValueError):
        return False


# LLM: 通知资格读取原runner和当前凭据投影；已成功替换的群旧ref、旧代/撤销/跨owner不得再发提醒，不解密。
# 函数用途: 在原任务上下文核对这代凭据仍被明确拒绝，供后台通知发送和未发送重投复核。
def credential_rejection_current(agent, reference, generation):
    try:
        current = agent._current_run_params
        scope = tool_approval_scope(agent, str(current.run_id or current.task_id))
        if not scope.active or not tool_approval_scope_is_current(agent, scope):
            return False
        row = read_credential_row(credential_path(agent.home_paths.owner_home_dir, reference),
                                  stage='notification_read', effect_outcome='not_started')
        _assert_credential_scope(agent, row, scope, row.get('origin', ''), require_ready=False, operation='notification')
        return (type(generation) is int and generation > 0 and type(row.get('generation')) is int and row['generation'] == generation
                and row.get('owner') == asdict(owner_identity_from_config(agent.config))
                and row.get('status') == 'ready' and row.get('verification_status') == 'rejected')
    except (AttributeError, KeyError, OSError, RuntimeError, ValueError):
        return False


# LLM: 群成员/管理资格在原凭据锁外核验，锁内比较原授权事实和current scope；撤销只清准确引用，不替他人取消；回执投影当前认证方式与是否已配置，不暴露秘密。
# 函数用途: 查询或撤销凭据，群提供者可跨自己的新任务管理，返回值不含秘密但可判断是否还需configure。
def manage_credential(agent, reference, *, operation):
    current = agent._current_run_params
    scope = tool_approval_scope(agent,str(current.run_id or current.task_id))
    if not scope.active or not tool_approval_scope_is_current(agent, scope):
        raise ValueError('当前任务无效')
    path = credential_path(agent.home_paths.owner_home_dir, reference)
    observed = read_credential_row(path, stage='management_preflight')
    _assert_credential_scope(agent, observed, scope, observed.get('origin', ''), require_ready=False, operation=operation)
    with _credential_mutation(agent,path) as admission:
        row = read_json_object_report(path,context='credentials.manage').payload
        _assert_credential_unchanged(agent, observed, row, scope)
        if operation == 'revoke':
            _shred_credential_row(admission, path, row)
        elif operation != 'status':
            raise ValueError('凭据管理动作无效')
    usable = operation == 'status' and credential_usable_in_task(agent, row, scope)
    return credential_public_view(row, reference, usable=usable, revoked_now=operation == 'revoke')


# LLM: 撤销在调用方持有的配额与记录锁内删除全部密文（含登录会话密文）并标 revoked；记录本身留作审计与查询。
# 函数用途: 原子撤销一条凭据记录的秘密材料。
def _shred_credential_row(admission, path, row):
    row.update(status='revoked', revoked_at=time.time())
    row.pop('ciphertext', None)
    row.pop('session_ciphertext', None)
    _save_credential_row(admission, path, row)


# LLM: 复用原解密使用门（operation='use'）只求布尔结论；不解密、不写记录，任何拒绝都算不可用。
# 函数用途: 判断一条凭据在当前任务里能否被使用，供查询与列表如实说明。
def credential_usable_in_task(agent, row, scope):
    try:
        _assert_credential_scope(agent, row, scope, row.get('origin') or None, operation='use')
    except (KeyError, TypeError, ValueError):
        return False
    return True


# LLM: 只投影非秘密标量；origin_task_id 来自原卡绑定；usable 由调用方用原使用门算出（无任务上下文时不给），从不含账号或密文。
# 函数用途: 生成凭据查询/撤销/列表共用的公开元数据。
def credential_public_view(row, reference, *, usable=None, revoked_now=False):
    status = row.get('status', 'unknown')
    if status == 'ready' and credential_ready_expired(row, now=time.time()):
        status = 'expired'
    bound = row.get('scope') if isinstance(row.get('scope'), dict) else {}
    view = {'credential_ref': reference, 'status': status, 'origin': row.get('origin', ''),
            'expires_at': row.get('ready_expires_at', row.get('expires_at')),
            'authentication': row.get('authentication', 'collected'),
            'authentication_configured': row.get('authentication') in ('basic', 'bearer', 'header', 'signature', 'login'),
            'verification_status': row.get('verification_status', 'unverified'), 'replaces_ref': row.get('replaces_ref', ''),
            'access_scope': row.get('access_scope', 'task'), 'origin_task_id': str(bound.get('root_task_id') or ''),
            'secret_present': bool(row.get('ciphertext') or row.get('session_ciphertext'))}
    if usable is not None:
        view['usable_in_current_task'] = bool(usable)
    if revoked_now:
        view.update(revoked=True, deleted=True)
    return view


# LLM: 宿主 CLI 管理入口：不需要任务上下文，调用方（本机 owner 的 CLI）已决定目标 owner；在该 owner 的配额与记录锁内执行，
#   复用同一撤销与公开投影；不解密、不打印秘密。
# 函数用途: 无任务上下文地查询或撤销指定 owner 的一条凭据。
def manage_credential_for_owner(home_paths, reference, *, operation):
    from contextlib import nullcontext

    from ..memory_store.owner_resources import build_owner_memory_quota
    from .owner_policy import resolve_effective_owner_policy

    if operation not in ('status', 'revoke'):
        raise ValueError('凭据管理动作无效')
    path = credential_path(home_paths.owner_home_dir, reference)
    if not read_credential_row(path, stage='management_preflight'):
        raise CredentialInputError('CREDENTIAL_NOT_FOUND', '没有这条凭据记录。', stage='management_preflight')
    quota = build_owner_memory_quota(home_paths, resolve_effective_owner_policy(home_paths)) if operation == 'revoke' else None
    with (quota.admission() if quota is not None else nullcontext()) as admission:
        with locked_json_path(path):
            row = read_json_object_report(path, context='credentials.manage').payload
            if not isinstance(row, dict) or row.get('kind') != 'task_credential' or row.get('credential_ref') != reference:
                raise ValueError('凭据记录无效')
            if operation == 'revoke':
                if admission is None:
                    raise ValueError('目标 owner 配额不可用，未撤销')
                _shred_credential_row(admission, path, row)
    return credential_public_view(row, reference, revoked_now=operation == 'revoke')


# LLM: 只列目标 owner 私密交互目录里的凭据记录元数据；坏记录计数不展开，不解密、不输出密文或账号。
# 函数用途: 为宿主 CLI 列出一个 owner 的全部凭据公开元数据。
def list_credentials_for_owner(home_paths):
    from ..common.nofollow_fs import open_directory_beneath, read_text_beneath

    owner_home = home_paths.owner_home_dir
    try:
        descriptor = open_directory_beneath(owner_home, PRIVATE_INTERACTIONS_OWNER_PARTS)
    except FileNotFoundError:
        return {'credentials': [], 'unreadable': 0}
    try:
        names = sorted(os.listdir(descriptor))
    finally:
        os.close(descriptor)
    rows, unreadable = [], 0
    for name in names:
        if len(name) != 42 or not name.startswith('cred_') or not name.endswith('.json'):
            continue
        try:
            row = json.loads(read_text_beneath(owner_home, (*PRIVATE_INTERACTIONS_OWNER_PARTS, name)) or 'null')
        except (OSError, UnicodeError, ValueError):
            unreadable += 1
            continue
        if not isinstance(row, dict) or row.get('kind') != 'task_credential' or row.get('credential_ref') != name[:-5]:
            unreadable += 1
            continue
        view = credential_public_view(row, name[:-5])
        view.update(channel=str(row.get('channel') or ''), submitted_at=row.get('submitted_at'),
                    name=str(row.get('credential_name') or '')[:128])
        rows.append(view)
    return {'credentials': rows, 'unreadable': unreadable}


# LLM: 仅比较同次锁外观察的权限/密文事实，session缓存变化不授予权限；原scope必须仍current，不查网络或解密。
# 函数用途: 在凭据管理锁内防止检查后被换代、撤销或换提供者，避免持文件锁访问飞书。
def _assert_credential_unchanged(agent, observed, current, scope):
    keys = ('schema_version', 'owner', 'channel', 'actor_id', 'submitter_actor_id', 'scope', 'job_id',
            'origin', 'access_scope', 'generation', 'status', 'verification_status', 'ready_expires_at',
            'expires_at', 'reference', 'ciphertext', 'authentication', 'auth_config', 'header_name',
            'browser_consumer')
    if (not tool_approval_scope_is_current(agent, scope)
            or any(observed.get(key) != current.get(key) for key in keys)):
        raise ValueError('凭据或执行已变化，请重新查询后操作')


# LLM: 显式资源叶子保原卡RMW/加密/代次/TTL/重放；inputs.perform只运输原C末门，不接受JSON角色或假Agent。
# 函数用途: 在原卡锁内可选末核插件版本，保存后补原输入投影，Count0与C共用一份实现。
def submit_credential_card_for_owner(home, capability, inputs, payload, body, *, audit, publish=None, retire=None):
    if body.get('decision') != 'cancel' and (capability is None or not capability.im_task_credentials_enabled):
        raise ValueError('私人凭据输入未启用')
    path = credential_path(home,payload['credential_ref'])
    # LLM: 原quota→卡锁/原身分比较保持，源码/权限变化须在调用本RMW的原C末门拒绝。
    # 函数用途: 只提交一次原卡状态，不跨门发布Wake。
    def transition():
        with inputs.quota.admission(blocking=inputs.blocking) as admission, locked_json_path(path,blocking=inputs.blocking):
            row = read_credential_row(path,stage='submission_read')
            from .task_credential_forms import credential_submitter
            from .task_credential_replay import (
                assert_credential_callback_identity,
                credential_callback_replay_for_input,
            )
            assert_credential_callback_identity(row,payload,body)
            from ..gateway_parts.private_interaction_service import check_plugin_input_record
            check_plugin_input_record(row)
            if (body['decision'] != 'cancel' and row.get('access_scope') == 'group'
                    and getattr(capability,'im_group_shared_credentials_enabled',False) is not True):
                raise ValueError('本群共享凭据已关闭，请取消原卡')
            from ..conversation.input_dependencies import credential_submission_current_from_access
            actor = credential_submitter(row,body)
            if body['decision'] == 'cancel' and row['status'] not in {'pending','cancelled'}:
                from .interaction_cancellation import already_handled_result
                return row, False, already_handled_result()
            if row['status'] != 'pending':
                should_publish = False if row['status']=='cancelled' and body['decision']=='cancel' else credential_callback_replay_for_input(inputs,row,body,actor)
            else:
                expired = time.time() >= row['expires_at']
                task_current = credential_submission_current_from_access(inputs, row)
                if body['decision'] != 'cancel':
                    if expired:
                        raise ValueError('原任务或卡片已失效')
                    if not task_current and row.get('access_scope', 'task') != 'owner':
                        raise ValueError('原任务或卡片已失效')
                row['submitter_actor_id'] = actor
                if body['decision'] == 'cancel':
                    row.update(status='cancelled',cancelled_by=audit(actor))
                elif body['decision'] == 'submit':
                    from .task_credential_forms import credential_submission_values
                    value, metadata = credential_submission_values(row,body.get('fields'),credential_origin)
                    row.update(metadata)
                    ttl = credential_seconds(row.get('ttl_seconds'),default=86400,allow_unlimited=True)
                    # C资源模式仅缩小建钥资格；真实许可已由inputs.perform原末门核准，callback存在不签权。
                    cipher = _cipher(home) if inputs.commit is None else _cipher(home,existing_only=True)
                    row.update(status='ready',ciphertext=cipher.encrypt(json.dumps(value).encode()).decode(),
                        verification_status='unverified',submitted_at=time.time(),ready_expires_at=(time.time()+ttl if ttl else 0))
                else:
                    raise ValueError('提交动作无效')
                _save_credential_row(admission,path,row)
                if body['decision'] == 'submit' and row.get('access_scope') == 'owner' and not task_current:
                    return row, 'retire_inactive', None
                should_publish = True
        return row, should_publish, None
    row, action, done = inputs.perform(transition)
    if done is not None:
        return done
    if row['status'] == 'cancelled':
        if retire is None:
            from .task_credential_forms import retire_credential_input_from_access
            retire_credential_input_from_access(inputs,row)
        else:
            retire(row)
    elif action == 'retire_inactive':
        if retire is None:
            from .task_credential_forms import retire_credential_input_from_access
            retire_credential_input_from_access(inputs, row)
        else:
            retire(row)
        return {
            'ok': True,
            'decision': row['status'],
            'saved': True,
            'resume_status': 'task_inactive',
            'message': '凭据已安全保存（原任务已结束，后续访问该站点可直接复用）。',
        }
    elif action is True or (isinstance(action, bool) and action):
        if publish is None:
            from ..conversation.input_dependencies import credential_wake_projection

            projection = credential_wake_projection(row) if row.get('status') == 'ready' else None
            inputs.resolve(
                row.get('input_id', row['credential_ref']),
                status=row['status'],
                result_refs=[row['credential_ref']] if row.get('status') == 'ready' else [],
                credential_result=projection or None,
            )
        else:
            publish(row)
    from .interaction_cancellation import credential_cancel_result
    return credential_cancel_result(row) if row['status']=='cancelled' else {'ok':True,'decision':row['status'],'message':'凭据已安全保存，尚未验证。'}


# LLM: current只核原输入的准确owner/scope/ref/provider，调用方仍守MAC与原C每次提交。
# 函数用途: 判断原凭据行是否仍属于当前输入。
