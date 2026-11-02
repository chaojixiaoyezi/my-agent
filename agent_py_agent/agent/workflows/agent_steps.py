"""原引擎的非阻塞 Agent 节点端口；不拥有线程、模型槽或第二份任务状态。

submit 沿 W2 BackgroundAgentService 的耐久幂等入口；completion_reader 必须由
宿主沿准确 SessionTask→run/attempt 链读出结构化完成证据，不能接收客户端自报结果。
"""
from __future__ import annotations

from ..conversation.session_task_dispatch import (
    SessionTaskDispatchError,
    SessionTaskDispatchPlan,
    _task_dedupe_key,
    reconcile_session_task_dispatch,
)
from .models import payload


class WorkflowAgentSteps:
    def __init__(self, background_service, *, actor_provider, completion_reader, cancel_handler=None):
        self.background_service = background_service
        self.actor_provider = actor_provider
        self.completion_reader = completion_reader
        self.cancel_handler = cancel_handler

    def _actor(self, run):
        actor = self.actor_provider(run)
        if getattr(actor, 'owner_id', None) != run.owner_id:
            raise PermissionError('WORKFLOW_AGENT_OWNER_MISMATCH')
        return actor

    # LLM: 未知只对账；已有耐久absence须重验原binding与canonical，才允许沿原键/预留ID继续；不清除派发槽。
    # 函数用途: 提交或继续同一次未开始的原任务，缺证据时保留未知责任。
    def submit(self, run, spec, step):
        """保留原attempt/key与宿主结构化身份；typed未知结果只对账，不重派。"""
        result = step.escalation_result
        retry_absent = (result.get('submission_state') == 'not_started'
            and result.get('reconciliation_state') == 'absent' and not result.get('task_id'))
        if ('session_task_dispatch' in step.escalation_result or
                step.escalation_result.get('submission_state') == 'unknown' and
                getattr(self.background_service, 'store', None) is not None):
            checked = self.reconcile(run, step)
            if (not retry_absent or not checked or result.get('task_id')
                    or result.get('submission_state') != 'not_started' or result.get('reconciliation_state') != 'absent'):
                return
        step.escalation_result['submission_state'] = 'not_started'
        actor = self._actor(run)
        context = run.business_context
        if not context.get('space_id') or context.get('owner_id') != run.owner_id:
            raise PermissionError('WORKFLOW_AGENT_CONTEXT_REQUIRED')
        config = spec.get('config', {})
        if set(config) - {'instruction', 'controller_agent_id'} or not isinstance(config.get('instruction'), str) or not config['instruction'].strip():
            raise ValueError('WORKFLOW_AGENT_CONFIG_INVALID')
        request = {'space_id': context['space_id'], 'instruction': config['instruction']}
        if context.get('project_ref'):
            request['project_ref'] = context['project_ref']
        if config.get('controller_agent_id'):
            request['controller_agent_id'] = config['controller_agent_id']
        identity = step.escalation_result['attempt_id']
        key = f'workflow:{run.workflow_run_id}:{step.step_id}:{identity}'
        if step.escalation_result.setdefault('submission_key', key) != key:
            raise ValueError('WORKFLOW_AGENT_SUBMISSION_KEY_MISMATCH')
        if retry_absent:
            original = result.get('session_task_dispatch', {})
            operation_key = getattr(self.background_service, '_operation_key', None)
            if (not callable(operation_key) or getattr(actor, 'source_thread_id', None) != original.get('sender_thread_id')):
                raise PermissionError('WORKFLOW_AGENT_DISPATCH_IDENTITY_MISMATCH')
            plan = SessionTaskDispatchPlan(actor.source_thread_id, original['target_thread_id'], config['instruction'],
                idempotency_key=operation_key(actor, key))
            if (original['dedupe_key'] != _task_dedupe_key(plan)
                    or request.get('controller_agent_id', original['target_thread_id']) != original['target_thread_id']):
                raise PermissionError('WORKFLOW_AGENT_DISPATCH_IDENTITY_MISMATCH')
            request['controller_agent_id'] = original['target_thread_id']
            result['reconciliation_state'] = 'unknown'
        step.escalation_result['submission_state'] = 'unknown'
        try:
            receipt = self.background_service.submit(request, actor=actor, idempotency_key=key)
        except SessionTaskDispatchError as error:
            step.escalation_result['submission_outcome'] = {
                'error_code': error.error_code, 'effect_outcome': error.effect_outcome}
            if error.effect_outcome == 'not_started':
                step.escalation_result['submission_state'] = 'not_started'
                raise
            original = error.outcome.result_envelope.get('session_task_dispatch')
            original = payload(original) if isinstance(original, dict) else {}
            known = original.get('task_id')
            if known:
                if step.escalation_result.get('task_id') not in (None, known):
                    raise PermissionError('WORKFLOW_AGENT_DISPATCH_IDENTITY_MISMATCH') from error
                step.escalation_result['task_id'] = known
            step.escalation_result['session_task_dispatch'] = original
            return
        except PermissionError:
            step.escalation_result['submission_state'] = 'not_started'
            raise
        except OSError:
            # 未提供typed身份的既有端口仍保留原key；不分配新attempt，也不声称没有执行。
            step.escalation_result['submission_state'] = 'unknown'
            return
        if (not isinstance(receipt, dict) or not isinstance(receipt.get('task_id'), str)
                or not receipt['task_id'] or receipt.get('state') not in ('accepted', 'pending', 'completed')):
            raise ValueError('WORKFLOW_AGENT_SUBMISSION_INVALID')
        step.escalation_result.update(task_id=receipt['task_id'], assignment_ref=receipt.get('assignment_ref'),
            operation_id=receipt.get('operation_id'), submission_state='accepted')
        if retry_absent:
            result['reconciliation_state'] = 'confirmed'

    # LLM: 身份仅来自原typed槽/已知canonical，缺失不能从模型正文补齐；不发任何后台副作用。
    # 函数用途: 找回原任务ID，或保留尚未查明的责任。
    def reconcile(self, run, step):
        """只读原ConversationStore；调用方须先持久化恢复的同一步，再发取消副作用。"""
        self._actor(run)
        result = step.escalation_result
        store = getattr(self.background_service, 'store', None)
        if store is None:
            return 'session_task_dispatch' not in result and bool(result.get('task_id'))
        identity = result.get('session_task_dispatch')
        try:
            if 'session_task_dispatch' not in result and result.get('task_id'):
                task = store.session_tasks.require(result['task_id'])
                identity = {name: getattr(task, name) for name in ('sender_thread_id', 'target_thread_id', 'dedupe_key', 'task_id')}
            if (not isinstance(identity, dict) or not {'sender_thread_id', 'target_thread_id', 'dedupe_key'} <= set(identity)
                    or set(identity) - {'sender_thread_id', 'target_thread_id', 'dedupe_key', 'task_id'}):
                return False
            identity = dict(identity)
            if result.get('task_id'):
                if identity.get('task_id') not in (None, result['task_id']):
                    raise PermissionError('WORKFLOW_AGENT_DISPATCH_IDENTITY_MISMATCH')
                identity['task_id'] = result['task_id']
            task = reconcile_session_task_dispatch(store, owner_id=run.owner_id, **identity)
        except PermissionError:
            raise
        except (SessionTaskDispatchError, OSError, ValueError, TypeError, KeyError):
            result['reconciliation_state'] = 'unknown'
            return False
        if task is None:
            result.update(submission_state='not_started', reconciliation_state='absent')
            return True
        context = task.metadata.get('business_context', {})
        if (context.get('owner_id') != run.owner_id or context.get('task_id') != task.task_id
                or any(context.get(name) != run.business_context.get(name) for name in ('space_id', 'project_ref'))):
            raise PermissionError('WORKFLOW_AGENT_DISPATCH_CONTEXT_MISMATCH')
        result.update(task_id=task.task_id, session_task_dispatch={**identity, 'task_id': task.task_id},
            submission_state='accepted', reconciliation_state='confirmed')
        return True

    def completion(self, run, step):
        self._actor(run)
        task_id = step.escalation_result.get('task_id')
        if not task_id:
            return None
        identity = {'owner_id': run.owner_id, 'workflow_run_id': run.workflow_run_id,
                    'step_id': step.step_id, 'attempt_id': step.escalation_result['attempt_id']}
        result = self.completion_reader(task_id, **identity)
        if result is None:
            return None
        if (not isinstance(result, dict) or result.get('task_id') != task_id
                or any(result.get(key) != value for key, value in identity.items())):
            raise ValueError('WORKFLOW_AGENT_COMPLETION_IDENTITY_MISMATCH')
        if result.get('status') in ('pending', 'queued', 'running'):
            return None
        if result.get('status') not in ('completed', 'failed', 'cancelled') or not result.get('completion_ref'):
            raise ValueError('WORKFLOW_AGENT_COMPLETION_INVALID')
        if result['status'] == 'completed' and not isinstance(result.get('output'), dict):
            raise ValueError('WORKFLOW_AGENT_OUTPUT_INVALID')
        return payload(result)

    # LLM: 仅消费原W2 completed且显式空outstanding_refs；固定原attempt取消键，不创建替代工作。
    # 函数用途: 停止原任务，并如实判断取消是否已经确认。
    def cancel(self, run, step):
        """只认原W2完成且无未结引用的收据，不把accepted/pending当作取消完成。"""
        actor = self._actor(run)
        task = step.escalation_result.get('task_id')
        if not task:
            # 只有完整只读对账确认不存在，才没有需要停止的原任务。
            return step.escalation_result.get('reconciliation_state') == 'absent'
        if not callable(self.cancel_handler):
            return False
        receipt = self.cancel_handler(task, actor=actor,
            idempotency_key=f'workflow-cancel:{run.workflow_run_id}:{step.step_id}:{step.escalation_result["attempt_id"]}')
        step.escalation_result['cancellation_receipt'] = payload(receipt)
        return isinstance(receipt, dict) and receipt.get('state') == 'completed' and receipt.get('outstanding_refs') == []
