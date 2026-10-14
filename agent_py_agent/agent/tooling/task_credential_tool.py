# LLM: 此工具只申请原任务的私人凭据卡，返回 opaque ref；不接收密码参数，不给管理员身份或额外文件权限。
# 模块用途: 让 IM 主子代理请求接口认证信息并继续原任务，HTTP 明文注入只发生在 web_fetch 宿主侧。
from __future__ import annotations

import json

from ..capability.runtime_config_reload import capability_config_for_agent
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
    # LLM: 注册只声明 schema，不读取密钥、不发卡；原 Registry 决定可用工具和权限。
    # 函数用途: 保存当前 owner 并声明无明文参数的输入接口。
    def __init__(self,agent):
        self.agent = agent
        self.model_spec = ToolModelSpec(name='request_credentials', description='接口需要账号密码或 API Key 时，向原发起人发私人密文输入卡；群内不收集明文。返回 credential_ref，之后 web_fetch 传此引用；可给 job_id 绑定已有本人的监控。',
            input_schema={'type':'object','properties':{'url':{'type':'string'}, 'authentication':{'type':'string','enum':['basic','bearer','header','login','signature']},
            'header_name':{'type':'string'},'job_id':{'type':'string'},
            'fields':{'type':'array','items':{'type':'object','properties':{'name':{'type':'string'},'label':{'type':'string'}},'required':['name','label'],'additionalProperties':False}},
            'auth_config':{'type':'object'},'ttl_seconds':{'type':'integer','minimum':60},
            'operation':{'type':'string','enum':['request','status','update','revoke']},'credential_ref':{'type':'string'}},
            'anyOf':[
                {'properties':{'operation':{'enum':['request']}},'required':['url','authentication']},
                {'properties':{'operation':{'enum':['update']}},'required':['operation','url','authentication','credential_ref']},
                {'properties':{'operation':{'enum':['status','revoke']}},'required':['operation','credential_ref']},
            ],'additionalProperties':False},
            hints=ToolModelHints(category='orchestration'))
        self.runtime_policy = ToolRuntimePolicy(effect_resolver=EffectResolverPolicy("mutating"),
            idempotency_policy=IdempotencyPolicy(scope='operation'),timeout_policy=TimeoutPolicy(seconds=30),
            input_policy=ToolInputPolicy(internal_parameters=('__tool_call_id',), metadata_url_parameters=('url',)))

    # LLM: 条件必填在副作用前核验；只按固定类型/码报告原任务、加密或发送失败，不展示凭据和异常正文。
    # 函数用途: 申请、更新、查询或撤销原任务凭据，模型只获得引用、期限和状态。
    def execute(self, params):
        from ..common.json_io import read_json_object_report
        from ..user_space.task_credentials import manage_credential
        config = capability_config_for_agent(self.agent)
        if config is None or not config.im_task_credentials_enabled:
            return _credential_error('TOOL_UNAVAILABLE', '当前未启用私人凭据输入。')
        operation = params.get('operation', 'request')
        required = ('credential_ref',) if operation in {'status', 'revoke'} else ('url', 'authentication')
        if operation == 'update':
            required += ('credential_ref',)
        if any(not isinstance(params.get(name), str) or not params[name].strip() for name in required):
            return _credential_error('TOOL_INVALID_ARGUMENTS', '凭据参数缺失：' + '、'.join(required) + '；未发送卡片。')
        try:
            if operation in {'status', 'revoke'}:
                result = manage_credential(self.agent, params['credential_ref'], operation=operation)
                return ToolHandlerOutcome('request_credentials', True, json.dumps(result), result_envelope={'credential': result})
            if operation not in {'request', 'update'}:
                raise ValueError('凭据动作无效')
            reference, _scope = request_credential_card(self.agent, url=params['url'], authentication=params['authentication'],
                header_name=params.get('header_name','Authorization'), job_id=params.get('job_id',''),
                call_id=str(params.get('__tool_call_id') or ''), options=CredentialCardOptions(fields=params.get('fields'),
                    auth_config=params.get('auth_config'), ttl_seconds=params.get('ttl_seconds'),
                    refresh_ref=params['credential_ref'] if operation == 'update' else ''))
            row = read_json_object_report(credential_path(self.agent.home_paths.owner_home_dir, reference), context='credentials.result').payload
            result = {'credential_ref': reference, 'origin': row['origin'], 'status': row['status'], 'expires_at': row['expires_at']}
            envelope = {'credential': result}
            if row['status'] == 'pending':
                result['status'] = 'waiting_input'
                envelope['input_dependency'] = {'input_id': row['input_id'], 'status': 'waiting_input', 'expires_at': row['expires_at']}
            return ToolHandlerOutcome('request_credentials', True, json.dumps(result), result_envelope=envelope)
        except CredentialInputError as exc:
            return _credential_error(exc.code, str(exc), stage=exc.stage, effect_outcome=exc.effect_outcome)
        except ImportError:
            return _credential_error('CREDENTIAL_ENCRYPTION_UNAVAILABLE', '私人凭据加密依赖不可用，请管理员修复；勿在群里发送密码。')
        except (ValueError, KeyError) as exc:
            from ..conversation.host_actor import HostActorError
            code = exc.error_code if isinstance(exc, HostActorError) else 'CREDENTIAL_BINDING_INVALID'
            return _credential_error(code, '凭据参数或原发起人/任务绑定无法核验，未完成卡片操作。')
        except (OSError, RuntimeError):
            return _credential_error('CREDENTIAL_STORAGE_UNAVAILABLE', '私人凭据存储不可用，请管理员检查；勿在群里发送密码。',
                stage='storage', effect_outcome='unknown')


# LLM: 所有错误沿原 ToolHandlerOutcome 保留 code/stage/副作用事实；不复制自由异常文字、密钥或私有路径。
# 函数用途: 生成模型可理解的凭据失败回执，避免缺参或前置拒绝被混报成发卡结果未知。
def _credential_error(code, message, *, stage='preflight', effect_outcome='not_started'):
    return ToolHandlerOutcome('request_credentials', False, message, error_code=code,
        effect_outcome=effect_outcome, result_envelope={'credential_error': {'stage': stage, 'error_code': code}})
