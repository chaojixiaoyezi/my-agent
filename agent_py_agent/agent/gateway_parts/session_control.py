# LLM: 会话选择只能取认证 owner 的原 ThreadStore；跨入口控制复用原 scope/claim/命令，不新建执行或更换 owner。
# 模块用途: 管理员从绑定 IM 查询和控制自己原 TUI 会话，原界面不在线也能使用。
from __future__ import annotations

from dataclasses import replace

from ..conversation.control_commands import ConversationControlResult, parse_conversation_control
from ..user_space.approval_mode import is_permission_admin
from .execution_target import read_gateway_execution_target

_OPERATIONS={'status','context','stop','steer','recover','compact','endtask','wakes','approve','deny'}


# LLM: 列表只包含当前 owner 的线程编号、类型和状态，不读取消息正文；控制资格必须在服务端复核管理员身份。
# 函数用途: 处理原 sessions 命令的本人查询及准确会话控制，错误不会回退成模型聊天。
def execute_session_control(base,paths,command,scope):
    from .control_service import _request_agent_for_scope, execute_gateway_conversation_control
    try:
        agent=_request_agent_for_scope(base,scope)
        if command.operation!='control':
            threads,errors=agent.conversation_store.threads.list_report(limit=30)
            rows=[f"{item.thread_id}  {','.join(sorted({b.channel for b in item.channel_bindings})) or '后台'}  {item.status}"
                for item in threads if item.owner_id==agent.home_paths.owner_id]
            return ConversationControlResult('sessions',True,'\n'.join(rows) or '暂无会话。',details={'load_errors':errors})
        if not is_permission_admin(agent.home_paths) or scope.metadata.get('channel_chat_type') in {'group','topic'}:
            return _failed('仅管理员本机或绑定私聊可跨入口管理本人会话。','COMMAND_ADMIN_REQUIRED')
        parts=command.value.split(maxsplit=2)
        if len(parts)!=3:
            return _failed(command.usage,'CONTROL_ARGUMENT_INVALID')
        inner=parse_conversation_control(parts[2])
        if inner is None or not inner.valid or inner.kind not in _OPERATIONS:
            return _failed('只能转交本人原会话的状态、停止、插话及恢复控制。','CONTROL_ARGUMENT_INVALID')
        thread=agent.conversation_store.threads.require(parts[1])
        selected=_selected_scope(agent,thread,scope)
        target=read_gateway_execution_target(paths,agent.conversation_store,thread.thread_id)
        if target is not None:
            selected=replace(selected,metadata={**selected.metadata,'expected_turn_id':target.turn_id,'expected_execution':target.to_dict()})
        if inner.kind in {'approve','deny'}:
            from .admin_control_service import execute_admin_channel_control
            result=execute_admin_channel_control(base,paths,inner,scope,approval_scope=selected)
        else:
            result=execute_gateway_conversation_control(base,paths,inner,selected)
        return replace(result,details={**(result.details or {}),'target_thread_id':thread.thread_id,'cross_entry':True})
    except (OSError,KeyError,ValueError,RuntimeError,AttributeError):
        return _failed('原会话暂时不可管理，请刷新线程列表。','CONTROL_TARGET_UNAVAILABLE')


# LLM: 只选该 owner 原来已注册的精确绑定，并复核索引仍指向原线程；不能由客户端构造替代渠道身份。
# 函数用途: 将合法选择转换为原控制服务已经支持的会话 scope，不写绑定或改变消息投递。
def _selected_scope(agent,thread,scope):
    if thread.owner_id!=agent.home_paths.owner_id or thread.owner_home!=str(agent.home_paths.owner_home_dir) or not thread.channel_bindings:
        raise ValueError('会话归属或绑定无效')
    binding=max(thread.channel_bindings,key=lambda row:row.last_active_at)
    resolved=agent.conversation_store.threads.resolve(channel=binding.channel,
        channel_conversation_id=binding.channel_conversation_id,channel_user_id=binding.channel_user_id)
    if resolved is None or resolved.thread_id!=thread.thread_id:
        raise ValueError('原会话绑定已变化')
    return replace(scope,channel=binding.channel,conversation_id=binding.channel_conversation_id,user_id=binding.channel_user_id,
        metadata={**scope.metadata,'channel_chat_type':'','channel_chat_id':''})


# LLM: 错误代码是控制事实，不把原会话不存在或越权伪装成已接管成功。
# 函数用途: 返回简短确定性控制错误，不发模型请求。
def _failed(message,code):
    return ConversationControlResult('sessions',False,message,error_code=code)
