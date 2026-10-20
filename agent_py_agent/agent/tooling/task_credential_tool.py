# LLM: 原工具元数据前置安全新卡能力，仍只作模型软上下文；实际填写人/群范围/送达由原宿主裁决，find与ref冲突仍由原schema/execute拒绝。
# 模块用途: 发现可复用的群凭据引用或明确请求新卡，保留原状态/输入等待/运输确认；HTTP秘密只在原宿主注入。
from __future__ import annotations

import json
import logging

from ..capability.runtime_config_reload import capability_config_for_agent
from ..contracts.tool_input_schema import EXCLUSIVE_ARGUMENT_GROUPS_KEY
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


# LLM: 工具始终绑定当前 owner Agent；开关在 capability 配置，运输确认只读宿主记录，失败/取消不能变成完成或聊天凭据收集。
# 类用途: 返回原授权候选、共享本人旧群卡或明确发新卡；管理权交原宿主，不等待、不调用模型、不公开秘密。
class RequestCredentialsTool(BaseTool):
    # LLM: 新卡说明与检索词不改变注册/展开/Schema/运行策略；仅宿主confirmed可说送达，原status/find/ref和owner/actor权限保持。
    # 函数用途: 说明真实安全输入卡及凭据发现用法，避免用聊天表格代替宿主交互；参数与执行合同保持。
    def __init__(self,agent):
        self.agent = agent
        self.model_spec = ToolModelSpec(name='request_credentials', description='用户要新的账号密码或秘密输入界面时，用operation=request申请真实安全输入卡，常规账号密码用collection_mode=username_password。默认authentication=collected仅收集时可不传url，目标链接由用户在卡片里填写；发卡本身不访问目标网站，无需先搜索模板或历史。聊天表格、Markdown下划线或口头描述不能替代宿主安全表单，也不能要求用户在普通聊天中填写秘密。只有工具回执request_delivery.status=confirmed才可说卡片已发出；waiting_input只表示等待填写，不表示已提交或保存。已有credential_ref需确认填写或保存状态时，用operation=status和原引用；返回安全元信息，不读取秘密。find仅按真实URL的同origin发现，不能传credential_ref或编造占位URL；空匹配不表示某个引用未填写。群内需要认证的站点先 operation=find 传准确url，或用list查看本群已授权凭据；find的selected_credential_ref由宿主按实际填写人本人账号优先、否则本群固定顺序选择，已有可用引用直接交web_fetch。本人账号401/403或失败不能静默改用他人账号；用户明确撤销自己的引用后可重新find使用本群其他可用账号。旧task群卡不会自动共享；仅用户明确要求共享本人旧群卡时，可用operation=share把shareable_credentials中的本人旧卡原位共享，再find，无须重填。只有确实缺失或用户明确新增/换账号才用operation=request发新卡；request始终明确申请，不因别人已有凭据隐式复用。群新卡默认本群共享，个人来源仅本人任务；可显式access_scope=task。无需先选认证协议；collected只保存，使用前configure原引用与准确方案，再由web_fetch宿主注入秘密。可用job_id绑定已有本人的监控。',
            input_schema={'type':'object','properties':{'url':{'type':'string','description':'find须提供真实目标URL，按同origin发现；不得编造占位URL代查已有引用，已有ref用status。'}, 'authentication':{'type':'string','enum':['collected','basic','bearer','header','login','signature']},
            'collection_mode':{'type':'string','enum':['username_password','password'], 'description':'常规账号密码或仅密码使用本项；宿主自动生成相应秘密字段，以及缺失的目标链接、可选名称和备注。'},'header_name':{'type':'string'},'job_id':{'type':'string'},
            'fields':{'type':'array','description':'需要自定义秘密字段时才传；常规账号密码优先使用collection_mode。目标链接、可选名称和备注由宿主表单提供，不要在此重复声明。', 'items':{'type':'object','properties':{'name':{'type':'string'},'label':{'type':'string'}},'required':['name','label'],'additionalProperties':False}},
            'auth_config':{'type':'object'},'ttl_seconds':{'type':'integer','anyOf':[{'const':0},{'minimum':60,'maximum':31536000}], 'description':'保存后有效秒数；0持续复用直到认证拒绝或撤销，缺省使用能力配置（默认0）。不改变卡片填写期限。'},
            'operation':{'type':'string','enum':['request','status','update','configure','revoke','list','find','share']},'credential_ref':{'type':'string','description':'宿主返回的不透明引用，不是账号、密码或其它内容；查询原卡保存状态用status。find不能携带本字段。'},
            'credential_name':{'type':'string','description':'find可选名称筛选；名称只是查找元数据，不能扩大权限或绕过本人账号优先。'},
            'access_scope':{'type':'string','enum':['group','task'],'description':'新卡使用范围；省略由原宿主决定：群新卡默认group本群共享，个人仅task。个人来源不能选group。'}},
            'anyOf':[
                {'properties':{'operation':{'enum':['list']}},'required':['operation']},
                {'properties':{'operation':{'enum':['find']},'url':{'minLength':1},'credential_ref':{'type':'string'}},'required':['operation','url'],EXCLUSIVE_ARGUMENT_GROUPS_KEY:[['url','credential_ref']]},
                {'properties':{'operation':{'enum':['request']}},'required':[]},
                {'properties':{'operation':{'enum':['update']}},'required':['operation','credential_ref']},
                {'properties':{'operation':{'enum':['configure']}},'required':['operation','credential_ref','authentication']},
                {'properties':{'operation':{'enum':['status','revoke','share']}},'required':['operation','credential_ref']},
            ],'additionalProperties':False},
            hints=ToolModelHints(category='orchestration',
                use_cases=('明确要新的账号密码、输入卡或安全表单时，用request申请宿主安全输入卡；默认仅收集可不传URL，发卡不访问网站，无须先找模板或历史。',
                    '已有引用查保存状态用status；find仅用于真实URL同origin发现，空匹配不能推断原引用未填写。',
                    '群内需认证站点先用find查当前授权引用，有可用引用直接复用；list查看本群已保存候选。',
                    '旧task群卡不会自动共享；用户明确要求共享本人旧群卡时才share，再find，无须重新填写；新增或换账号仍明确request。',
                    '本人认证失败由用户决定，不静默换他人账号；明确撤销本人引用后再find本群其它账号。'),
                keywords=('凭据', '已保存账号', '群共享', '认证', 'credential', '复用账号', '账号密码', '输入卡', '安全表单', '凭据卡'),
                avoid_when=('不得索要或在工具参数中传密码、API Key明文，也不能从文件或记忆搜秘密。',
                    '不得用聊天表格或文本模板冒充安全输入卡；没有宿主confirmed回执不得声称已发卡，发卡成功也不等于用户已提交。',)))
        self.runtime_policy = ToolRuntimePolicy(effect_resolver=EffectResolverPolicy("mutating", by_parameter=(("operation", (("list", "read_only"), ("find", "read_only"))),)), promotes_task=True,
            idempotency_policy=IdempotencyPolicy(scope='operation'),timeout_policy=TimeoutPolicy(seconds=30),
            input_policy=ToolInputPolicy(internal_parameters=('__tool_call_id',), metadata_url_parameters=('url',)))

    # LLM: 查询只转交宿主选择结果，不重排/不换账号；find+ref字段冲突及条件必填在副作用前拒绝，不自动转status；发卡后读失败保持unknown，原输入依赖与取消不变。
    # 函数用途: 查找已授权凭据或明确管理/申请原卡；模型仅获安全摘要、引用及准确状态，不读取秘密。
    def execute(self, params):
        from ..user_space.task_credentials import manage_credential
        config = capability_config_for_agent(self.agent)
        if config is None or not config.im_task_credentials_enabled:
            return _credential_error('TOOL_UNAVAILABLE', '当前未启用私人凭据输入。')
        operation = params.get('operation', 'request')
        if operation == 'find' and 'credential_ref' in params:
            return _credential_error('TOOL_INVALID_ARGUMENTS',
                'find 按真实 URL 发现，不能同时携带 credential_ref；查询原引用保存状态请用 operation=status。')
        required = ('credential_ref',) if operation in {'status', 'revoke', 'configure', 'share'} else ()
        if operation == 'configure':
            required += ('authentication',)
        if operation == 'update':
            required += ('credential_ref',)
        if operation == 'find':
            required += ('url',)
        if any(not isinstance(params.get(name), str) or not params[name].strip() for name in required):
            return _credential_error('TOOL_INVALID_ARGUMENTS', '凭据参数缺失：' + '、'.join(required) + '；未发送卡片。')
        try:
            if operation in {'list', 'find'}:
                from ..user_space.task_credential_groups import discover_group_credentials
                result = discover_group_credentials(self.agent, url=params.get('url', ''),
                    name=params.get('credential_name', ''), operation=operation)
                return ToolHandlerOutcome('request_credentials', True, json.dumps(result, ensure_ascii=False),
                    result_envelope={'credential_discovery': result})
            if operation == 'share':
                from ..user_space.task_credential_forms import share_credential
                result = share_credential(self.agent, params['credential_ref'])
                return ToolHandlerOutcome('request_credentials', True, json.dumps(result, ensure_ascii=False),
                    result_envelope={'credential': result})
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
                    refresh_ref=params['credential_ref'] if operation == 'update' else '',
                    **({'access_scope': params['access_scope']} if 'access_scope' in params else {})))
            row = read_credential_row(credential_path(self.agent.home_paths.owner_home_dir, reference),
                stage='result_read', effect_outcome='unknown')
            if not all(key in row for key in ('origin', 'status', 'expires_at')):
                raise CredentialInputError('CREDENTIAL_RECORD_INVALID', '已发送卡片的原记录不可读取，请先核对原操作。',
                    stage='result_read', effect_outcome='unknown')
            result = {'credential_ref': reference, 'origin': row['origin'], 'status': row['status'], 'expires_at': row.get('ready_expires_at', row['expires_at']),
                'verification_status':row.get('verification_status','unverified'), 'replaces_ref':row.get('replaces_ref',''),
                'request_delivery': _request_delivery(row), 'access_scope': row.get('access_scope', 'task')}
            envelope = {'credential': result}
            if row['status'] == 'pending':
                result['status'] = 'waiting_input'
                envelope['input_dependency'] = {'input_id': row['input_id'], 'status': 'waiting_input', 'expires_at': row['expires_at'], 'verification_status':row.get('verification_status','unverified'), 'replaces_ref':row.get('replaces_ref','')}
                envelope['input_dependency']['request_delivery'] = result['request_delivery']
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


# LLM: 只读已由原凭据宿主验证的记录，不接收模型参数；missing/非法状态一律 unknown，pending/armed 不证明卡片送达。
# 函数用途: 把原运输状态压成无消息标识、地址或表单值的公开事实，只有 confirmed 才表示平台已确认。
def _request_delivery(row):
    status = row.get('delivery_state')
    return {'status': status if isinstance(status, str) and status in {'not_started', 'started', 'unknown', 'confirmed'} else 'unknown'}


# LLM: 所有错误沿原 ToolHandlerOutcome 保留 code/stage/副作用及安全诊断；不复制自由异常文字、密钥或私有路径。
# 函数用途: 生成模型可理解的凭据失败回执，避免缺参或前置拒绝被混报成发卡结果未知。
def _credential_error(code, message, *, stage='preflight', effect_outcome='not_started', diagnostic=None):
    logging.getLogger(__name__).warning('credential_tool_failure code=%s stage=%s effect_outcome=%s diagnostic=%s',
        code, stage, effect_outcome, diagnostic or {})
    return ToolHandlerOutcome('request_credentials', False, message, error_code=code,
        effect_outcome=effect_outcome, result_envelope={'credential_error': {'stage': stage, 'error_code': code, 'diagnostic': diagnostic or {}}})
