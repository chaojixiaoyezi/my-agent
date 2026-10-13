# LLM: 任务凭据只保存在原 owner 的受保护私密交互目录，Fernet 加密；模型只拿引用，原网络工具在确切作用域解密注入。
# 模块用途: 私人卡收集 API 凭据，绑定原发起人、任务或监控、接口 origin，避免群聊天及日志保存明文。
from __future__ import annotations

import base64
import hmac
import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

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


# LLM: 请求者采用原 scope/actor；监控引用必须已存在且属于该发起人。外发私人卡不发群、不把凭据放 prompt。
# 函数用途: 为当前任务或监控发布一张可取消的私人输入卡，写宿主待输入状态及原生回执。
def request_credential_card(agent, *, url, authentication, header_name='Authorization', job_id='', call_id=''):
    current = agent._current_run_params
    scope = tool_approval_scope(agent, str(current.run_id or current.task_id))
    if not scope.active:
        raise ValueError('原任务已失效')
    channel, actor = approval_recipient(agent, scope)
    if channel != 'feishu':
        raise ValueError('当前来源没有私人输入卡通道')
    if authentication not in {'bearer','basic','header'} or not header_name or not all(c.isalnum() or c=='-' for c in header_name):
        raise ValueError('认证方式或请求头无效')
    if job_id:
        job = agent.scheduler_repository.get_job(job_id)
        creator = job.get('creator') or {}
        if owner_identity_from_config(agent.config).owner_kind == 'group' and creator != {'channel':channel,'actor_id':actor}:
            raise ValueError('监控不属于请求人')
    home = agent.home_paths.owner_home_dir
    _cipher(home)
    from ..conversation.im_tool_approval import _scope_identity
    origin = credential_origin(url)
    identity = json.dumps({'scope':_scope_identity(scope),'call_id':call_id},sort_keys=True).encode()
    reference = 'cred_'+(hmac.digest(interaction_key(home,create=True),identity,'sha256').hex()[:32] if call_id else uuid.uuid4().hex)
    expires = time.time()+1800
    payload = {'kind':'task_credential','owner':asdict(owner_identity_from_config(agent.config)), 'channel':channel,
               'actor_id':actor,'expires_at':expires, 'credential_ref':reference,'scope':_scope_identity(scope)}
    signed = sign_interaction_reference(home,payload)
    row = {**payload,'schema_version':'task_credential.v1','reference':signed,'status':'pending',
           'origin':origin,'authentication':authentication,'header_name':header_name,'job_id':job_id}
    path = credential_path(home,reference)
    with _credential_mutation(agent,path) as admission:
        previous = read_json_object_report(path,context='credentials.request').payload
        if previous:
            if any(previous.get(key)!=row.get(key) for key in ('scope','actor_id','channel','origin','authentication','header_name','job_id')):
                raise ValueError('同一调用凭据参数冲突')
            if previous.get('status') not in {'pending','ready'} or not previous.get('message_id'):
                raise ValueError('原私人输入卡不可恢复')
            return reference,scope
        _save_credential_row(admission,path,row)
    context = DeliveryContext(channel=channel,target=actor,thread_id=scope.thread_id,task_id=scope.run_id,
                              mode='reply',idempotency_key=reference)
    receipt = agent.delivery_service.deliver_control_card(context,credential_card(row),summary='请在私人卡片填写接口凭据。')
    with _credential_mutation(agent,path) as admission:
        row.update(message_id=receipt.message_id,chat_id=receipt.chat_id,status='pending' if receipt.ok else 'failed')
        _save_credential_row(admission,path,row)
    if not receipt.ok:
        raise ValueError('私人输入卡未送达')
    return reference, scope


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


# LLM: 输入采用已有密码卡的原生密文 input；按钮只携带签名引用，接口用途及绑定范围先让真人可见。
# 函数用途: 创建私聊表单，提交后整卡替换，群消息不包含任何输入框或值。
def credential_card(row):
    inputs = [{'tag':'input','name':name,'input_type':'password','show_icon':True,
        'label':{'tag':'plain_text','content':label}} for name,label in
        ([('username','账号'),('secret','密码')] if row['authentication']=='basic' else [('secret','API Key / Token')])]
    submit = {'tag':'button','name':'submit','action_type':'form_submit','type':'primary',
        'text':{'tag':'plain_text','content':'仅供此任务使用'}, 'value':{'kind':'my_agent_task_credential','reference':row['reference'],'decision':'submit'}}
    cancel = {'tag':'button','text':{'tag':'plain_text','content':'取消'},
        'value':{'kind':'my_agent_task_credential','reference':row['reference'],'decision':'cancel'}}
    target = '监控 '+row['job_id'] if row['job_id'] else '当前任务'
    return {'config':{'wide_screen_mode':True,'update_multi':True},'elements':[
        {'tag':'div','text':{'tag':'plain_text','content':f"{target}需要接口凭据：{row['origin']}\n方式：{row['authentication']}。输入不进入聊天、模型或记忆。"}},
        {'tag':'form','name':'credentials','elements':[*inputs,submit]}, {'tag':'action','actions':[cancel]}]}


# LLM: 上游已 MAC/actor/解锁；这里复核原卡身份与任务代次。提交只加密写入，不自动解锁或执行其它任务。
# 函数用途: 接受原发起人的私人输入或取消，明文只在这次进程内存在。
def submit_credential_card(agent,payload,body):
    from ..conversation.im_tool_approval import _scope_identity
    home = agent.home_paths.owner_home_dir
    path = credential_path(home,payload['credential_ref'])
    with _credential_mutation(agent,path) as admission:
        row = read_json_object_report(path,context='credentials.submit').payload
        if not row or row.get('reference')!=body['reference'] or row.get('message_id')!=body['message_id'] or row.get('chat_id')!=body['chat_id']:
            raise ValueError('私人卡身份不匹配')
        current = tool_approval_scope(agent, row['scope']['run_id'])
        if row['status']!='pending' or not current.active or _scope_identity(current) != row['scope']:
            raise ValueError('原任务或卡片已失效')
        if body['decision']=='cancel':
            row['status']='cancelled'
        elif body['decision']=='submit':
            value = body.get('fields')
            names = {'username','secret'} if row['authentication']=='basic' else {'secret'}
            if not isinstance(value,dict) or set(value)!=names or any(not isinstance(v,str) or not v or len(v)>8192 or '\r' in v or '\n' in v for v in value.values()):
                raise ValueError('输入字段无效')
            row.update(status='ready',ciphertext=_cipher(home).encrypt(json.dumps(value).encode()).decode(),submitted_at=time.time())
        else:
            raise ValueError('提交动作无效')
        _save_credential_row(admission,path,row)
    return {'ok':True,'decision':row['status'],'message':'凭据已安全保存，原任务继续。' if row['status']=='ready' else '已取消凭据输入。'}


# LLM: 只为当前调用的原任务/监控和同发起人注入指定 origin；引用泄漏不等于可解密，重定向仍由原网络门处理。
# 函数用途: 给网络工具返回请求头及需净化的秘密值，不交给模型或普通 shell。
def credential_headers(agent,reference,url):
    current = agent._current_run_params
    scope = tool_approval_scope(agent,str(current.run_id or current.task_id))
    if not scope.active or not tool_approval_scope_is_current(agent,scope):
        raise ValueError('原任务执行已失效')
    channel, actor = approval_recipient(agent,scope)
    row = read_json_object_report(credential_path(agent.home_paths.owner_home_dir,reference),context='credentials.use').payload
    if row.get('status')!='ready' or (row['channel'],row['actor_id'])!=(channel,actor) or row['origin']!=credential_origin(url):
        raise ValueError('凭据不属于此调用或接口')
    if row['job_id']:
        run = agent.scheduler_repository.get_active_run(scope.root_task_id)
        if (run or {}).get('job_id')!=row['job_id'] and scope.root_task_id!=row['scope']['root_task_id']:
            raise ValueError('凭据不属于此监控')
    elif scope.root_task_id!=row['scope']['root_task_id']:
        raise ValueError('凭据不属于此任务')
    value = json.loads(_cipher(agent.home_paths.owner_home_dir).decrypt(row['ciphertext'].encode()))
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
