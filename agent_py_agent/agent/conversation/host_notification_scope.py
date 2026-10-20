# LLM: 通知只在原claim内新建真实core轮，MP先组合原BG运输；词法holder仅防重复绑定，不是权限/状态账。
# 模块用途: 让首次义务冻结、表达和末次投递共用仍开放的宿主身份，冷重投不重业务。
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace

from ..agent_core.host_turn_scope import host_turn_scope
from ..user_space.owner_resolver import owner_identity_from_config
from .background_execution import _BackgroundRunControl
from .claim_execution_binding import current_claim_execution
from .local_run_control import LocalRunControl

_CURRENT = ContextVar('host_notification_execution', default=None)


# LLM: 不序列化原对象；捕获同栈claim/owner/actor及token，Context副本不能重置原token而借用本轮。
# 类用途: 为同一次通知调用的嵌套函数保存原句柄引用，不根据字段相等重建执行权。
class _NotificationScope:
    # LLM: 只能在真实core/prompt scope内创建；新轮身份仍是原core决定的run/attempt，不来自通知metadata。
    # 函数用途: 捕获本次词法调用的原对象与主体，供嵌套复用校验。
    def __init__(self, agent, request, claim, turn, control):
        self.agent, self.claim, self.turn, self.control = agent, claim, turn, control
        self.identity = _request_identity(request)
        self.owner = owner_identity_from_config(agent.config)
        self.home = agent.home_paths
        self.actor = turn.params.host_channel_actor
        self.authority = control.runtime_authority()
        self.execution_identity = _execution_identity(turn.params)
        self.pid, self.thread = os.getpid(), threading.get_ident()
        self.origin, self.active, self.token = self, True, None

    # LLM: 仅验证原词法来源，真实active claim/权限由credential和delivery既有门再读；不按source标签授信。
    # 函数用途: 拒绝退出、复制、跨线程、换主体或字段替换后的旧通知句柄。
    def require(self, agent, request):
        if (self.origin is not self or not self.active or self.agent is not agent
                or self.pid != os.getpid() or self.thread != threading.get_ident()
                or self.identity != _request_identity(request) or self.claim is not current_claim_execution()
                or self.home is not agent.home_paths or self.owner != owner_identity_from_config(agent.config)
                or self.turn.params.host_channel_actor is not self.actor
                or self.turn.params.conversation_task_binding_callback is not self.control
                or self.control.runtime_authority() != self.authority
                or _execution_identity(self.turn.params) != self.execution_identity
                or not LocalRunControl.__call__(self.control, None)):
            raise ValueError('原通知执行来源已经改变')
        self.turn.require(agent, self.turn.params)
        # reset先验证token确实属于当前Context；失败不改原Context，也不能在复制Context中重新签发。
        _CURRENT.reset(self.token)
        self.token = _CURRENT.set(self)


# LLM: 只比较core原字段和规范线程绑定，不从自然文本或当前DB补身份。
# 函数用途: 检测当前轮参数被原地换代或替换的情况。
def _execution_identity(params):
    attrs = params.task_attributes if isinstance(params.task_attributes, dict) else {}
    return (params.request_id, params.task_id, params.run_id, params.attempt_id,
            attrs.get('conversation_thread_id'), attrs.get('conversation_task_id'))


# LLM: 定位只取原宿主request及精确wake，不读取正文或通知payload里的权限字段。
# 函数用途: 为嵌套调用比较同一原通知输入，不把now等展示字段当身份。
def _request_identity(request):
    wake = request.wake_signal if isinstance(request.wake_signal, dict) else {}
    return request.thread_id, request.task_id, request.reason, wake.get('wake_signal_id', '')


# LLM: 纯读取本栈holder并核原对象；不建attempt/claim，不从DB或通知metadata猜当前身份。
# 函数用途: 让freshness与表达取得已进入的宿主轮，缺范围时保持不可用。
def current_notification_turn(agent, request):
    current = _CURRENT.get()
    if current is None:
        return None
    current.require(agent, request)
    return current.turn


# LLM: 无通知能力/终态任务零新轮；当前claim必须原同对象，MP binder保previous和T，四actual只关闭句柄不假结账。
# 函数用途: 在原租约里为通知进入一次真实执行，嵌套复用当前轮，退出统一原settle及finish。
@contextmanager
def notification_scope(agent, request):
    from ..gateway_parts.multiprocess_background_execution import bind_background_run_params
    from .required_user_action import notice_enabled
    from .runtime import _run_params

    existing = current_notification_turn(agent, request)
    if existing is not None:
        yield existing
        return
    if not notice_enabled(agent):
        yield None
        return
    link, error = agent.conversation_store.tasks.load_report(request.task_id)
    if error is not None:
        raise ValueError('通知原任务不可读')
    if link is None or link.status != 'active':
        yield None
        return
    claim = current_claim_execution()
    if (claim is None or claim.claims is not agent.conversation_store.claims
            or claim.thread_id != request.thread_id or link.thread_id != request.thread_id):
        raise ValueError('通知缺少原后台领取来源')
    thread = agent.conversation_store.threads.require(request.thread_id)
    params = _run_params(request.thread_id, request, agent, thread=thread)
    resolved = bind_background_run_params(agent, replace(params,
        source='background_user_action_notice', allowed_tools=[]))
    control = _BackgroundRunControl(resolved.request_id, resolved.conversation_task_binding_callback)
    try:
        with host_turn_scope(agent, '', replace(resolved, conversation_task_binding_callback=control),
                reason='required_user_action_notice', tool_rounds=0) as turn:
            scope = _NotificationScope(agent, request, claim, turn, control)
            scope.token = _CURRENT.set(scope)
            try:
                turn.runtime_status = 'ok'
                yield turn
            finally:
                scope.active = False
                _CURRENT.reset(scope.token)
    finally:
        control.finish()
