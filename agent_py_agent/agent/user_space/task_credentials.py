# LLM: 凭据唯一存于原owner私密目录；显式group按当前成员和实际填写人择取，task继续原根/监控约束，秘密仅宿主注入。
# 模块用途: 收集并加密接口凭据，群默认复用和个人任务隔离共用原卡/存储/网络门，不向聊天或模型交明文。
from __future__ import annotations

import base64
import hmac
import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

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
# 函数用途: 由原 owner 主密钥派生独立用途的加密密钥。
def _cipher(home):
    from cryptography.fernet import Fernet
    key = hmac.digest(interaction_key(home, create=True), b'task-credentials.v1', 'sha256')
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
    ttl_seconds = credential_seconds(ttl_seconds, default=capability.im_task_credentials_ttl_seconds, allow_unlimited=True)
    if options.access_scope not in {'', 'task', 'group'}:
        raise ValueError('凭据使用范围无效')
    from .task_credential_groups import new_credential_access_scope
    access_scope = 'task' if options.access_scope == 'task' else new_credential_access_scope(agent, scope)
    if options.access_scope == 'group' and access_scope != 'group':
        raise ValueError('当前来源不能登记群共享凭据')
    if job_id:
        job = agent.scheduler_repository.get_job(job_id)
        creator = job.get('creator') or {}
        if owner_identity_from_config(agent.config).owner_kind == 'group' and creator != {'channel':channel,'actor_id':actor}:
            raise CredentialInputError('CREDENTIAL_NOT_AUTHORIZED', '这项监控不属于当前发起人，未发送凭据卡。')
    home = agent.home_paths.owner_home_dir
    with credential_stage("key_initialization", key_validation=True):
        _cipher(home)
    from ..conversation.im_tool_approval import _scope_identity
    origin = credential_origin(url) if url else ''
    if authentication == 'login' and origin and credential_origin(auth_config['url']) != origin:
        raise ValueError('登录地址必须属于同一接口 origin')
    with credential_stage("key_reference", key_validation=True):
        identity = json.dumps({'scope':_scope_identity(scope),'call_id':call_id},sort_keys=True).encode()
        reference = refresh_ref or 'cred_'+(hmac.digest(interaction_key(home,create=True),identity,'sha256').hex()[:32] if call_id else uuid.uuid4().hex)
        payload = {'kind':'task_credential','owner':asdict(owner_identity_from_config(agent.config)), 'channel':channel,
                   'actor_id':actor,'expires_at':time.time()+1800, 'credential_ref':reference,'scope':_scope_identity(scope),
                   'access_scope':access_scope}
        owner = owner_identity_from_config(agent.config)
        if owner.owner_kind == 'group':
            payload.update(presentation={'kind':'group','chat_id':owner.owner_id}, submitter_policy='current_group_member')
        signed = sign_interaction_reference(home,payload)
    row = {**payload,'schema_version':'task_credential.v1','reference':signed,'status':'pending',
           'origin':origin,'target_url':url,'collection_mode':options.collection_mode,'authentication':authentication,
           'header_name':header_name,'job_id':job_id,'fields':fields,'auth_config':auth_config,
           'ttl_seconds':ttl_seconds,'generation':1,'delivery_state':'not_started'}
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
            row['input_id'] = row['credential_ref']+':'+str(row['generation'])
            signed_fields = {key:row[key] for key in ('kind','owner','channel','actor_id','expires_at','credential_ref','scope','generation')}
            signed_fields.update({key:row[key] for key in ('presentation','submitter_policy','access_scope',
                'replaces_ref','replaces_generation','previous_submitter_actor_id') if key in row})
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


# LLM: 展示只能读取已签名access_scope，group presentation不是授权；提交仍走原MAC/当前成员，不显示秘密。
# 函数用途: 在原表单说明群共享或原任务用途，保留取消与原输入控件。
def credential_card(row):
    inputs = [{'tag':'input','name':field['name'],'input_type':'password','show_icon':True,
        'label':{'tag':'plain_text','content':field['label']}} for field in row.get('fields', credential_fields(row['authentication']))]
    if not row.get('target_url', row.get('origin')):
        inputs.insert(0, {'tag':'input','name':'target_url','label':{'tag':'plain_text','content':'目标链接'}})
    inputs.extend({'tag':'input','name':name,'required':False,'label':{'tag':'plain_text','content':label}}
                  for name,label in [('credential_name','名称（可选）'),('credential_note','备注（可选）')])
    submit = {'tag':'button','name':'submit','action_type':'form_submit','type':'primary',
        'text':{'tag':'plain_text','content':'保存并供本群助手使用' if row.get('access_scope') == 'group' else '仅供此任务使用'}, 'value':{'kind':'my_agent_task_credential','reference':row['reference'],'decision':'submit'}}
    cancel = {'tag':'button','text':{'tag':'plain_text','content':'取消'},
        'value':{'kind':'my_agent_task_credential','reference':row['reference'],'decision':'cancel'}}
    target = '监控 '+row['job_id'] if row['job_id'] else '当前任务'
    usage = ('本群成员可让助手复用；优先使用自己填写的账号，主动取消后可使用本群其它账号。'
             if row.get('access_scope') == 'group' else '仅限原发起人的当前任务或已绑定监控。')
    return {'config':{'wide_screen_mode':True,'update_multi':True},'elements':[
        {'tag':'div','text':{'tag':'plain_text','content':f"{target}需要接口凭据：{row.get('target_url') or row['origin'] or '请填写目标链接'}\n{usage}\n输入不进入聊天、模型或记忆。"}},
        {'tag':'form','name':'credentials','elements':[*inputs,submit]}, {'tag':'action','actions':[cancel]}]}


# LLM: 上游已MAC/actor/解锁；群共享关闭时不收旧group卡新值但允许取消；同卡同代终态和原期限不变。
# 函数用途: 接受输入或取消并加密保存；填写与保存后的期限分开，明文仅在首次提交进程内存在。
def submit_credential_card(agent,payload,body):
    from ..capability.runtime_config_reload import capability_config_for_agent
    capability = capability_config_for_agent(agent)
    if body.get('decision')!='cancel' and (capability is None or not capability.im_task_credentials_enabled):
        raise ValueError('私人凭据输入未启用')
    home = agent.home_paths.owner_home_dir
    path = credential_path(home,payload['credential_ref'])
    with _credential_mutation(agent,path) as admission:
        row = read_credential_row(path, stage='submission_read')
        from .task_credential_forms import credential_submitter
        from .task_credential_replay import (
            assert_credential_callback_identity,
            credential_callback_replay,
        )
        assert_credential_callback_identity(row, payload, body)
        if (body['decision'] != 'cancel' and row.get('access_scope') == 'group'
                and getattr(capability, 'im_group_shared_credentials_enabled', False) is not True):
            raise ValueError('本群共享凭据已关闭，请取消原卡')
        actor = credential_submitter(row, body)
        if body['decision']=='cancel' and row['status'] not in {'pending','cancelled'}:
            from .interaction_cancellation import already_handled_result
            return already_handled_result()
        if row['status'] != 'pending':
            publish = False if row['status']=='cancelled' and body['decision']=='cancel' else credential_callback_replay(agent,row,body,actor)
        else:
            from ..conversation.input_dependencies import credential_submission_current
            if body['decision']!='cancel' and (time.time() >= row['expires_at'] or not credential_submission_current(agent, row)):
                raise ValueError('原任务或卡片已失效')
            row['submitter_actor_id'] = actor
            if body['decision']=='cancel':
                from .interaction_actor_audit import record_interaction_actor
                row.update(status='cancelled',cancelled_by=record_interaction_actor(agent,payload,decision='cancel',actor=actor))
            elif body['decision']=='submit':
                from .task_credential_forms import credential_submission_values
                value, metadata = credential_submission_values(row, body.get('fields'), credential_origin)
                row.update(metadata)
                ttl = credential_seconds(row.get('ttl_seconds'), default=86400, allow_unlimited=True)
                row.update(status='ready',ciphertext=_cipher(home).encrypt(json.dumps(value).encode()).decode(),
                           verification_status='unverified', submitted_at=time.time(),ready_expires_at=(time.time()+ttl if ttl else 0))
            else:
                raise ValueError('提交动作无效')
            _save_credential_row(admission,path,row)
            publish = True
    from ..conversation.input_dependencies import publish_credential_resolution
    if row['status']=='cancelled':
        from .task_credential_forms import retire_credential_input
        retire_credential_input(agent,row)
    elif publish:
        publish_credential_resolution(agent, row)
    from .interaction_cancellation import credential_cancel_result
    return credential_cancel_result(row) if row['status']=='cancelled' else {'ok':True,'decision':row['status'],'message':'凭据已安全保存，尚未验证。'}


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
            raise ValueError('本群没有本次可使用的站点凭据')
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
    if (not row or row.get('owner') != asdict(owner_identity_from_config(agent.config))
            or row['channel'] != channel or row['origin'] != (credential_origin(url) if url else '')):
        raise ValueError('凭据不属于此调用或接口')
    if require_ready and (row.get('verification_status') == 'rejected' or row.get('status')!='ready' or credential_ready_expired(row, now=time.time())):
        raise ValueError('凭据未就绪、已过期或已撤销，请重新申请私人输入')
    from .task_credential_groups import group_credential_access
    if group_credential_access(agent, row, scope, operation=operation):
        return
    if row.get('access_scope', 'task') != 'task' or row['actor_id'] != actor:
        raise ValueError('凭据不属于此调用或接口')
    if row['job_id']:
        run = agent.scheduler_repository.get_active_run(scope.root_task_id)
        if (run or {}).get('job_id')!=row['job_id'] and scope.root_task_id!=row['scope']['root_task_id']:
            raise ValueError('凭据不属于此监控')
    elif scope.root_task_id!=row['scope']['root_task_id']:
        raise ValueError('凭据不属于此任务')


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


# LLM: 群成员/管理资格在原凭据锁外核验，锁内比较原授权事实和current scope；撤销只清准确引用，不替他人取消。
# 函数用途: 查询或撤销凭据，群提供者可跨自己的新任务管理，返回值不含秘密。
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
            row.update(status='revoked',revoked_at=time.time())
            row.pop('ciphertext',None)
            row.pop('session_ciphertext',None)
            _save_credential_row(admission,path,row)
        elif operation != 'status':
            raise ValueError('凭据管理动作无效')
    status = row['status']
    if status == 'ready' and credential_ready_expired(row, now=time.time()):
        status = 'expired'
    return {'credential_ref':reference,'status':status,'origin':row['origin'],
            'expires_at':row.get('ready_expires_at',row['expires_at']),
            'verification_status':row.get('verification_status','unverified'), 'replaces_ref':row.get('replaces_ref','')}


# LLM: 仅比较同次锁外观察的权限/密文事实，session缓存变化不授予权限；原scope必须仍current，不查网络或解密。
# 函数用途: 在凭据管理锁内防止检查后被换代、撤销或换提供者，避免持文件锁访问飞书。
def _assert_credential_unchanged(agent, observed, current, scope):
    keys = ('schema_version', 'owner', 'channel', 'actor_id', 'submitter_actor_id', 'scope', 'job_id',
            'origin', 'access_scope', 'generation', 'status', 'verification_status', 'ready_expires_at',
            'expires_at', 'reference', 'ciphertext', 'authentication', 'auth_config', 'header_name')
    if (not tool_approval_scope_is_current(agent, scope)
            or any(observed.get(key) != current.get(key) for key in keys)):
        raise ValueError('凭据或执行已变化，请重新查询后操作')
