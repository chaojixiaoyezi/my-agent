# LLM: 此工具只申请原任务的私人凭据卡，返回 opaque ref；不接收密码参数，不给管理员身份或额外文件权限。
# 模块用途: 让 IM 主子代理请求接口认证信息并继续原任务，HTTP 明文注入只发生在 web_fetch 宿主侧。
from __future__ import annotations

import json
import logging

from ..capability.runtime_config_reload import capability_config_for_agent
from ..conversation.tool_approval_scope import ToolApprovalScopeUnavailable
from ..user_space.task_credential_storage import credential_stage, read_credential_row
from ..user_space.task_credentials import (
    CredentialCardOptions,
    CredentialInputError,
    credential_path,
    request_credential_card,
)
from .models import (
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    TimeoutPolicy,
    ToolHandlerOutcome,
    ToolInputPolicy,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)


# LLM: 工具始终绑定当前 owner Agent；开关在 capability 配置，卡片失败/取消不能变成完成或聊天凭据收集。
# 类用途: 发布绑定原发起人的私聊输入卡或管理授权，不等待、不调用模型。
class RequestCredentialsTool(BaseTool):
    # LLM: 所有生命周期都要求规范任务，原 Registry 在 handler 前沿 promotes_task 晋升；注册不读密钥或发卡。
    # 函数用途: 保存当前 owner 并声明原任务输入接口，首次普通请求也能持久等待和恢复。
    def __init__(self,agent):
        self.agent = agent
        self.model_spec = ToolModelSpec(name='request_credentials', description='接口需要账号密码或 API Key 时，向原发起人发私人密文输入卡；群来源在原群发卡，个人来源在本人私聊发卡；可仅收密码，缺链接在卡上填写。无需先选认证协议；返回 credential_ref；默认collected只保存，不能直接HTTP使用。需要使用时先operation=configure并传原引用与准确认证方案，配置后web_fetch再传此引用；可给 job_id 绑定已有本人的监控。',
            input_schema={'type':'object','properties':{'url':{'type':'string'}, 'authentication':{'type':'string','enum':['collected','basic','bearer','header','login','signature']},
            'collection_mode':{'type':'string','enum':['username_password','password']},'header_name':{'type':'string'},'job_id':{'type':'string'},
            'fields':{'type':'array','items':{'type':'object','properties':{'name':{'type':'string'},'label':{'type':'string'}},'required':['name','label'],'additionalProperties':False}},
            'auth_config':{'type':'object'},'ttl_seconds':{'type':'integer','minimum':60},
            'operation':{'type':'string','enum':['request','status','update','configure','revoke']},'credential_ref':{'type':'string'}},
            'anyOf':[
                {'properties':{'operation':{'enum':['request']}},'required':[]},
                {'properties':{'operation':{'enum':['update']}},'required':['operation','credential_ref']},
                {'properties':{'operation':{'enum':['configure']}},'required':['operation','credential_ref','authentication']},
                {'properties':{'operation':{'enum':['status','revoke']}},'required':['operation','credential_ref']},
            ],'additionalProperties':False},
            hints=ToolModelHints(category='orchestration'))
        self.runtime_policy = ToolRuntimePolicy(effect_resolver=EffectResolverPolicy("mutating"), promotes_task=True,
            idempotency_policy=IdempotencyPolicy(scope='operation'),timeout_policy=TimeoutPolicy(seconds=30),
            input_policy=ToolInputPolicy(internal_parameters=('__tool_call_id',), metadata_url_parameters=('url',)))

    # LLM: 条件必填在副作用前核验；scope 不可用与存储失败分开，发送后读取失败保持unknown且禁止盲重发，不展示异常正文。
    # 函数用途: 申请、更新、查询或撤销原任务凭据，模型只获得引用、期限和状态。
    def execute(self, params):
        from ..user_space.task_credentials import manage_credential
        config = capability_config_for_agent(self.agent)
        if config is None or not config.im_task_credentials_enabled:
            return _credential_error('TOOL_UNAVAILABLE', '当前未启用私人凭据输入。')
        operation = params.get('operation', 'request')
        required = ('credential_ref',) if operation in {'status', 'revoke', 'configure'} else ()
        if operation == 'configure':
            required += ('authentication',)
        if operation == 'update':
            required += ('credential_ref',)
        if any(not isinstance(params.get(name), str) or not params[name].strip() for name in required):
            return _credential_error('TOOL_INVALID_ARGUMENTS', '凭据参数缺失：' + '、'.join(required) + '；未发送卡片。')
        try:
            if operation == 'configure':
                from ..user_space.task_credential_forms import configure_credential
                result = configure_credential(self.agent, params['credential_ref'], authentication=params.get('authentication'), auth_config=params.get('auth_config'), header_name=params.get('header_name','Authorization'))
                return ToolHandlerOutcome('request_credentials', True, json.dumps(result), result_envelope={'credential':result})
            if operation in {'status', 'revoke'}:
                with credential_stage('record_management', effect_outcome='not_started' if operation == 'status' else 'unknown'):
                    result = manage_credential(self.agent, params['credential_ref'], operation=operation)
                return ToolHandlerOutcome('request_credentials', True, json.dumps(result), result_envelope={'credential': result})
            if operation not in {'request', 'update'}:
                raise ValueError('凭据动作无效')
            reference, _scope = request_credential_card(self.agent, url=params.get('url',''), authentication=params.get('authentication','collected'),
                header_name=params.get('header_name','Authorization'), job_id=params.get('job_id',''),
                call_id=str(params.get('__tool_call_id') or ''), options=CredentialCardOptions(fields=params.get('fields'),
                    auth_config=params.get('auth_config'), collection_mode=params.get('collection_mode','username_password'), ttl_seconds=params.get('ttl_seconds'),
                    refresh_ref=params['credential_ref'] if operation == 'update' else ''))
            row = read_credential_row(credential_path(self.agent.home_paths.owner_home_dir, reference),
                stage='result_read', effect_outcome='unknown')
            if not all(key in row for key in ('origin', 'status', 'expires_at')):
                raise CredentialInputError('CREDENTIAL_RECORD_INVALID', '已发送卡片的原记录不可读取，请先核对原操作。',
                    stage='result_read', effect_outcome='unknown')
            result = {'credential_ref': reference, 'origin': row['origin'], 'status': row['status'], 'expires_at': row['expires_at'],
                'verification_status':row.get('verification_status','unverified'), 'replaces_ref':row.get('replaces_ref','')}
            envelope = {'credential': result}
            if row['status'] == 'pending':
                result['status'] = 'waiting_input'
                envelope['input_dependency'] = {'input_id': row['input_id'], 'status': 'waiting_input', 'expires_at': row['expires_at'], 'verification_status':row.get('verification_status','unverified'), 'replaces_ref':row.get('replaces_ref','')}
            return ToolHandlerOutcome('request_credentials', True, json.dumps(result), result_envelope=envelope)
        except ToolApprovalScopeUnavailable:
            return _credential_error('CREDENTIAL_TASK_NOT_ACTIVE', '当前任务执行范围不可用，未发送凭据卡。',
                stage='execution_scope', effect_outcome='not_started')
        except CredentialInputError as exc:
            exc.diagnostic['operation_id'] = str(params.get('__tool_call_id') or '')
            return _credential_error(exc.code, str(exc), stage=exc.stage, effect_outcome=exc.effect_outcome, diagnostic=exc.diagnostic)
        except ImportError:
            return _credential_error('CREDENTIAL_ENCRYPTION_UNAVAILABLE', '私人凭据加密依赖不可用，请管理员修复；勿在群里发送密码。')
        except (ValueError, KeyError) as exc:
            from ..conversation.host_actor import HostActorError
            code = exc.error_code if isinstance(exc, HostActorError) else 'CREDENTIAL_BINDING_INVALID'
            return _credential_error(code, '凭据参数或原发起人/任务绑定无法核验，未完成卡片操作。')
        except (OSError, RuntimeError) as exc:
            return _credential_error('CREDENTIAL_STORAGE_UNAVAILABLE', '凭据前置检查不可用，未发送卡片。',
                stage='preflight', effect_outcome='not_started', diagnostic={'exception_type': type(exc).__name__,
                    **({'errno': exc.errno} if isinstance(exc, OSError) and isinstance(exc.errno, int) else {})})


# LLM: 所有错误沿原 ToolHandlerOutcome 保留 code/stage/副作用及安全诊断；不复制自由异常文字、密钥或私有路径。
# 函数用途: 生成模型可理解的凭据失败回执，避免缺参或前置拒绝被混报成发卡结果未知。
def _credential_error(code, message, *, stage='preflight', effect_outcome='not_started', diagnostic=None):
    logging.getLogger(__name__).warning('credential_tool_failure code=%s stage=%s effect_outcome=%s diagnostic=%s',
        code, stage, effect_outcome, diagnostic or {})
    return ToolHandlerOutcome('request_credentials', False, message, error_code=code,
        effect_outcome=effect_outcome, result_envelope={'credential_error': {'stage': stage, 'error_code': code, 'diagnostic': diagnostic or {}}})
