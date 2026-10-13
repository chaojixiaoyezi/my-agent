# LLM: 此工具只申请原任务的私人凭据卡，返回 opaque ref；不接收密码参数，不给管理员身份或额外文件权限。
# 模块用途: 让 IM 主子代理请求接口认证信息并继续原任务，HTTP 明文注入只发生在 web_fetch 宿主侧。
from __future__ import annotations

import time

from ..capability.runtime_config_reload import capability_config_for_agent
from ..common.cancellation import cancellation_requested
from ..conversation.tool_approval_scope import tool_approval_scope_is_current
from ..user_space.task_credentials import credential_path, request_credential_card
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
# 类用途: 发布一张绑定原发起人的私聊输入卡并等待确认，不调用模型。
class RequestCredentialsTool(BaseTool):
    # LLM: 注册只声明 schema，不读取密钥、不发卡；原 Registry 决定可用工具和权限。
    # 函数用途: 保存当前 owner 并声明无明文参数的输入接口。
    def __init__(self,agent):
        self.agent = agent
        self.model_spec = ToolModelSpec(name='request_credentials', description='接口需要账号密码或 API Key 时，向原发起人发私人密文输入卡；群内不收集明文。返回 credential_ref，之后 web_fetch 传此引用；可给 job_id 绑定已有本人的监控。',
            input_schema={'type':'object','properties':{'url':{'type':'string'}, 'authentication':{'type':'string','enum':['basic','bearer','header']},
            'header_name':{'type':'string'},'job_id':{'type':'string'}},'required':['url','authentication'],'additionalProperties':False},
            hints=ToolModelHints(category='orchestration'))
        self.runtime_policy = ToolRuntimePolicy(effect_resolver=EffectResolverPolicy("mutating"),
            idempotency_policy=IdempotencyPolicy(scope='operation'),timeout_policy=TimeoutPolicy(seconds=1800),
            input_policy=ToolInputPolicy(internal_parameters=('__tool_call_id',)))

    # LLM: 当前真实 scope 与取消决定等待是否继续；加密正文从不进入结果或错误，等待不阻塞另一聊天线程。
    # 函数用途: 发送卡片，等待同一原任务的私人输入，返回引用或稳定取消/失效原因。
    def execute(self,params):
        from ..common.json_io import read_json_object_report
        from ..conversation.im_tool_approval import _scope_identity
        config = capability_config_for_agent(self.agent)
        if config is not None and not config.im_task_credentials_enabled:
            return ToolHandlerOutcome('request_credentials',False,'当前未启用私人凭据输入。',error_code='TOOL_UNAVAILABLE')
        try:
            reference, scope = request_credential_card(self.agent,url=params['url'],authentication=params['authentication'],
                header_name=params.get('header_name','Authorization'),job_id=params.get('job_id',''),
                call_id=str(params.get('__tool_call_id') or ''))
            path = credential_path(self.agent.home_paths.owner_home_dir,reference)
            while not cancellation_requested() and tool_approval_scope_is_current(self.agent,scope):
                row = read_json_object_report(path,context='credentials.wait').payload
                if row.get('scope')!=_scope_identity(scope) or row.get('status') in {'failed','cancelled'} or time.time()>row.get('expires_at',0):
                    break
                if row.get('status')=='ready':
                    import json
                    return ToolHandlerOutcome('request_credentials',True,json.dumps({'credential_ref':reference,'origin':row['origin'],'status':'ready'}))
                time.sleep(.25)
            return ToolHandlerOutcome('request_credentials',False,'凭据输入已取消、过期或原任务结束。',error_code='CREDENTIAL_INPUT_CANCELLED')
        except (ImportError,OSError,ValueError,KeyError,RuntimeError):
            return ToolHandlerOutcome('request_credentials',False,'私人凭据卡未完成；请确认原任务、私人通道和密钥加密依赖。',error_code='CREDENTIAL_INPUT_UNAVAILABLE')
