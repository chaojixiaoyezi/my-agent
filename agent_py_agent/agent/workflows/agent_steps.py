"""原引擎的非阻塞 Agent 节点端口；不拥有线程、模型槽或第二份任务状态。

submit 沿 W2 BackgroundAgentService 的耐久幂等入口；completion_reader 必须由
宿主沿准确 SessionTask→run/attempt 链读出结构化完成证据，不能接收客户端自报结果。
"""
from __future__ import annotations

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

    def submit(self, run, spec, step):
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
        try:
            receipt = self.background_service.submit(request, actor=actor, idempotency_key=key)
        except OSError:
            # ACK 未知也沿原 key 恢复，不能另分配 attempt 或宣称业务未开始。
            step.escalation_result['submission_state'] = 'unknown'
            return
        if (not isinstance(receipt, dict) or not isinstance(receipt.get('task_id'), str)
                or not receipt['task_id'] or receipt.get('state') not in ('accepted', 'pending', 'completed')):
            raise ValueError('WORKFLOW_AGENT_SUBMISSION_INVALID')
        step.escalation_result.update(task_id=receipt['task_id'], assignment_ref=receipt.get('assignment_ref'),
            operation_id=receipt.get('operation_id'), submission_state='accepted')

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

    def cancel(self, run, step):
        actor = self._actor(run)
        task = step.escalation_result.get('task_id')
        if not task:
            # 未知 submit 必须先沿同 key 对账，不能把可能已创建的任务当不存在。
            return False
        if not callable(self.cancel_handler):
            return False
        receipt = self.cancel_handler(task, actor=actor,
            idempotency_key=f'workflow-cancel:{run.workflow_run_id}:{step.step_id}:{step.escalation_result["attempt_id"]}')
        return isinstance(receipt, dict) and receipt.get('state') == 'cancelled'
