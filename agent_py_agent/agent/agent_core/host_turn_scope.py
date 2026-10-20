# LLM: 仅复用原core绑定/当前prompt/attempt结算；本栈状态不授予权限，actual未知保持原对象和未结责任。
# 模块用途: 给宿主工具与有界通知共用一次真实执行轮的生命周期，不创建队列、任务或第二账本。
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from ..backends.bounded_call import BoundedCallExitUnconfirmedError
from ..common.request_execution_ports import (
    RequestExecutionContextUnavailable,
    RequestExecutionOutcomeUncertainError,
)
from ..common.tool_write_context import ToolWriteContextUnavailable

_CURRENT_HOST_TURN = ContextVar("original_host_turn", default=None)

UNSETTLED_HOST_ERRORS = (RequestExecutionOutcomeUncertainError, BoundedCallExitUnconfirmedError,
                        RequestExecutionContextUnavailable, ToolWriteContextUnavailable)


# LLM: params只取本次core返回值；active/status仅供同栈清理，不是运行授权或可保存的任务状态。
# 类用途: 保存一次宿主执行轮的绑定和原收口事实，供可选表达复用同一轮而不二次绑定。
@dataclass
class HostTurn:
    agent: object
    params: object
    reason: str
    tool_rounds: int
    runtime_status: str = 'failed'
    active: bool = False
    _pid: int = field(default_factory=os.getpid)
    _thread: int = field(default_factory=threading.get_ident)
    _origin: object = field(init=False, repr=False)
    _token: object = field(default=None, init=False, repr=False)

    # LLM: 仅原context manager发布本对象，复制保旧origin；没有原token不能靠字段拼出执行轮。
    # 函数用途: 固定本栈句柄自身身份，不创建执行权。
    def __post_init__(self):
        self._origin = self

    # LLM: 仅本栈原参数可复用；真实权限仍由claim/core/工具和投递门逐次核对，不能把此检查当授权。
    # 函数用途: 拒绝退出、跨线程或被替换的宿主轮句柄。
    def require(self, agent, params):
        if (self._origin is not self or _CURRENT_HOST_TURN.get() is not self
                or not self.active or self.agent is not agent or self.params is not params
                or os.getpid() != self._pid or threading.get_ident() != self._thread
                or agent._current_run_params is not params):
            raise ValueError('原宿主执行轮已退出或被替换')
        _CURRENT_HOST_TURN.reset(self._token)
        self._token = _CURRENT_HOST_TURN.set(self)


# LLM: 原core先绑定，prompt scope覆盖全部副作用；四actual不写普通终态，正常失败/取消沿原精确attempt结算。
# 函数用途: 共用宿主工作轮的进入和退出，避免表达内部提前结束投递仍要使用的身份。
@contextmanager
def host_turn_scope(agent, prompt, params, *, reason, tool_rounds):
    from .runtime_mixin import (
        RunCloseoutFacts,
        _bind_main_agent_turn_params,
        _settle_main_agent_run_status,
        current_prompt_scope,
    )

    bound = _bind_main_agent_turn_params(agent, prompt, params)
    turn = HostTurn(agent, bound, reason, tool_rounds)
    unsettled = False
    try:
        with current_prompt_scope(agent, prompt, bound):
            turn.active = True
            turn._token = _CURRENT_HOST_TURN.set(turn)
            try:
                yield turn
            finally:
                _CURRENT_HOST_TURN.reset(turn._token)
    except UNSETTLED_HOST_ERRORS:
        unsettled = True
        raise
    except (InterruptedError, KeyboardInterrupt):
        turn.runtime_status = 'cancelled'
        raise
    except BaseException:
        turn.runtime_status = 'failed'
        raise
    finally:
        turn.active = False
        if not unsettled:
            _settle_main_agent_run_status(agent, run_id=bound.run_id, attempt_id=bound.attempt_id,
                facts=RunCloseoutFacts(runtime_status=turn.runtime_status,
                    runtime_reason=reason, tool_rounds=tool_rounds))
