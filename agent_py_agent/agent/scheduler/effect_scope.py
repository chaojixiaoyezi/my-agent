# LLM: 宿主副作用入口只消费原task/子代理root事实；普通任务不写scheduler，不从模型参数或文本推断定时身份。
# 模块用途: 在原模型与后台投递调用边界复用事件副作用lease，不改变原执行器和回执权威。
from __future__ import annotations

import logging
from contextlib import contextmanager


# LLM: 只有开始CAS明确未获准才抛此类型；运输已开始后的中断不能冒充not_started。
# 类用途: 让模型和工具出口保留同一结构化拒绝码。
class SchedulerEffectRejected(InterruptedError):
    # LLM: code只能由宿主选择，不含平台响应、任务正文或凭据。
    # 函数用途: 保存准确未开始错误码，不改变原运行或回执。
    def __init__(self, code):
        self.error_code = code
        super().__init__(code)


# LLM: 子代理根只取原SubAgentTask.root_id，当前runner上下文不允许以模型自报参数冒充其它run。
# 函数用途: 找到当前任务所属的原scheduler根，普通前台任务快速返回空。
def _scheduled_root(agent, task_id):
    value = str(task_id or '')
    from ..runtime_context import current_subagent_run_id

    child = current_subagent_run_id(agent)
    if not child:
        return value if value.startswith('srun_') else ''
    seen = set()
    while child and child not in seen:
        if child.startswith('srun_'):
            return child
        seen.add(child)
        try:
            task = agent.subagents.load(child)
        except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
            raise SchedulerEffectRejected('SCHEDULER_EFFECT_SOURCE_UNAVAILABLE') from exc
        if task is None:
            raise SchedulerEffectRejected('SCHEDULER_EFFECT_SOURCE_UNAVAILABLE')
        root = str(getattr(task, 'root_id', '') or '')
        child = root if root.startswith('srun_') else str(getattr(task, 'parent_id', '') or '')
    if value.startswith('srun_'):
        raise SchedulerEffectRejected('SCHEDULER_EFFECT_SOURCE_UNAVAILABLE')
    return ''


# LLM: lease只围住原调用生命周期而不持store锁；拒绝必须发生在backend/transport调用之前，finally不覆盖原异常。
# 函数用途: 为准确事件根取得开始资格，调用结束后同nonce释放；普通定时任务保持零写。
@contextmanager
def scheduler_effect_scope(agent, task_id, operation_key, *, kind, allow_terminal=False):
    root = _scheduled_root(agent, task_id)
    repository = getattr(agent, 'scheduler_repository', None)
    if not root:
        yield
        return
    if repository is None:
        raise SchedulerEffectRejected('SCHEDULER_EFFECT_SOURCE_UNAVAILABLE')
    try:
        candidate = repository.effect_run(root)
    except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
        raise SchedulerEffectRejected('SCHEDULER_EFFECT_SOURCE_UNAVAILABLE') from exc
    if candidate is None:
        raise SchedulerEffectRejected('SCHEDULER_EFFECT_SOURCE_UNAVAILABLE')
    if candidate.get('event_source') is None:
        yield
        return
    try:
        lease = repository.begin_event_effect(candidate, operation_key, kind=kind, allow_terminal=allow_terminal)
    except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
        raise SchedulerEffectRejected('SCHEDULER_EVENT_EFFECT_RETIRED') from exc
    try:
        yield
    finally:
        try:
            repository.finish_event_effect(lease)
        except (OSError, RuntimeError, TypeError, ValueError, KeyError):
            logging.getLogger(__name__).warning('scheduler_event_effect_cleanup_pending')
