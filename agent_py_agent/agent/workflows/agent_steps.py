"""原引擎的非阻塞 Agent 节点端口；不拥有线程、模型槽或第二份任务状态。

submit 沿 W2 BackgroundAgentService 的耐久幂等入口；completion_reader 必须由
宿主沿准确 SessionTask→run/attempt 链读出结构化完成证据，不能接收客户端自报结果。
"""
from __future__ import annotations

from typing import Any

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


# LLM: 只读 SessionTask 完成证据读取；验证所有者与派发身份，返回结构化状态与输出 (G5b)。
# 函数用途: 为 WorkflowAgentSteps 提供安全 SessionTask 状态读取与完成证据验证。
def read_session_task_completion(
    conversation_store,
    run,
    step=None,
    *,
    owner_id: str = "",
    workflow_run_id: str = "",
    step_id: str = "",
    attempt_id: str = "",
    session_task_dispatch: Any = None,
):
    if conversation_store is None or not hasattr(conversation_store, "session_tasks"):
        return None

    if isinstance(run, str):
        task_id = run
    else:
        result = getattr(step, "escalation_result", {}) if step is not None else {}
        task_id = result.get("task_id")
        if not task_id:
            return None
        owner_id = getattr(run, "owner_id", "") or owner_id
        workflow_run_id = getattr(run, "workflow_run_id", "") or workflow_run_id
        step_id = getattr(step, "step_id", "") or step_id
        attempt_id = result.get("attempt_id", "") or attempt_id
        session_task_dispatch = result.get("session_task_dispatch") or session_task_dispatch

    tasks_store = conversation_store.session_tasks
    try:
        if hasattr(tasks_store, "require"):
            task = tasks_store.require(task_id)
        elif hasattr(tasks_store, "get"):
            task = tasks_store.get(task_id)
        else:
            task = None
    except Exception:
        return None

    if task is None:
        return None

    # 校验 owner_id
    context = getattr(task, "metadata", {}).get("business_context", {}) if isinstance(getattr(task, "metadata", None), dict) else {}
    task_owner = context.get("owner_id") or getattr(task, "owner_id", None)
    if not task_owner and hasattr(conversation_store, "threads") and hasattr(task, "target_thread_id"):
        try:
            th_store = conversation_store.threads
            target_th = th_store.load(task.target_thread_id) if hasattr(th_store, "load") else th_store.get(task.target_thread_id)
            if target_th:
                task_owner = getattr(target_th, "owner_id", None) or (target_th.get("owner_id") if isinstance(target_th, dict) else None)
        except Exception:
            pass

    if task_owner and owner_id and task_owner != owner_id:
        raise PermissionError("WORKFLOW_AGENT_OWNER_MISMATCH")

    # 校验 dispatch identity
    if isinstance(session_task_dispatch, dict):
        target_thread = session_task_dispatch.get("target_thread_id")
        if target_thread and getattr(task, "target_thread_id", None) != target_thread:
            raise PermissionError("WORKFLOW_AGENT_DISPATCH_IDENTITY_MISMATCH")
        dedupe = session_task_dispatch.get("dedupe_key")
        if dedupe and getattr(task, "dedupe_key", None) != dedupe:
            raise PermissionError("WORKFLOW_AGENT_DISPATCH_IDENTITY_MISMATCH")

    status = getattr(task, "status", "queued")
    if status in ("queued", "running", "pending"):
        return {
            "task_id": task_id,
            "owner_id": owner_id,
            "workflow_run_id": workflow_run_id,
            "step_id": step_id,
            "attempt_id": attempt_id,
            "state": "running",
            "status": "running",
            "output": None,
        }

    if status in ("completed", "done", "succeeded"):
        meta = getattr(task, "metadata", {}) or {}
        out = meta.get("output")
        if not isinstance(out, dict):
            out = meta.get("result")
        if not isinstance(out, dict):
            out = {
                "summary": getattr(task, "summary", ""),
                "result_refs": list(getattr(task, "result_refs", ())),
            }
        completion_ref = meta.get("completion_ref") or f"session_task:{task_id}"
        return {
            "task_id": task_id,
            "owner_id": owner_id,
            "workflow_run_id": workflow_run_id,
            "step_id": step_id,
            "attempt_id": attempt_id,
            "state": "succeeded",
            "status": "completed",
            "completion_ref": completion_ref,
            "output": out,
            "error_code": "",
            "error_message": "",
        }

    if status in ("failed", "cancelled"):
        meta = getattr(task, "metadata", {}) or {}
        code = (
            getattr(task, "failure_code", "")
            or meta.get("error_code")
            or ("SESSION_TASK_CANCELLED" if status == "cancelled" else "SESSION_TASK_FAILED")
        )
        msg = getattr(task, "summary", "") or meta.get("error_message") or f"Session task {status}"
        completion_ref = meta.get("completion_ref") or f"session_task:{task_id}:{status}"
        return {
            "task_id": task_id,
            "owner_id": owner_id,
            "workflow_run_id": workflow_run_id,
            "step_id": step_id,
            "attempt_id": attempt_id,
            "state": "failed",
            "status": status,
            "completion_ref": completion_ref,
            "error_code": code,
            "error_message": msg,
            "output": None,
        }

    return None


# LLM: 幂等取消绑定的 SessionTask；通过 BackgroundAgentService 或直更 ConversationStore 停止 (G5b)。
# 函数用途: 在工作流运行被取消时幂等取消关联的 SessionTask。
def cancel_workflow_session_task(
    background_service,
    conversation_store,
    run,
    step=None,
    *,
    actor=None,
    idempotency_key=None,
):
    if isinstance(run, str):
        task_id = run
    else:
        result = getattr(step, "escalation_result", {}) if step is not None else {}
        task_id = result.get("task_id")

    if not task_id:
        return {"operation_id": "", "state": "completed", "outstanding_refs": []}

    # 优先使用 background_service.cancel_task
    if background_service is not None and hasattr(background_service, "cancel_task") and actor is not None:
        try:
            receipt = background_service.cancel_task(task_id, actor=actor)
            if isinstance(receipt, dict):
                return receipt
        except Exception:
            pass

    # 次选：若任务在 conversation_store 中，幂等更新其状态为 cancelled
    if conversation_store is not None and hasattr(conversation_store, "session_tasks"):
        try:
            task = conversation_store.session_tasks.require(task_id)
            if getattr(task, "status", "") == "cancelled":
                return {"operation_id": task_id, "state": "completed", "outstanding_refs": []}
            from ..conversation.session_tasks import SessionTaskUpdate
            if hasattr(conversation_store.session_tasks, "advance"):
                conversation_store.session_tasks.advance(
                    task_id,
                    SessionTaskUpdate(status="cancelled", summary="Workflow run cancelled"),
                )
            elif hasattr(conversation_store.session_tasks, "record_update"):
                conversation_store.session_tasks.record_update(
                    task_id,
                    SessionTaskUpdate(status="cancelled", summary="Workflow run cancelled"),
                )
            return {"operation_id": task_id, "state": "completed", "outstanding_refs": []}
        except Exception:
            pass

    return {"operation_id": str(task_id), "state": "completed", "outstanding_refs": []}


def make_session_task_completion_reader(conversation_store):
    """构造沿准确 SessionTask->run/attempt 链读取结构化完成证据的 reader。"""
    def completion_reader(
        target: Any = None,
        step: Any = None,
        *,
        task_id: str = "",
        owner_id: str = "",
        workflow_run_id: str = "",
        step_id: str = "",
        attempt_id: str = "",
        session_task_dispatch: Any = None,
    ):
        if target is not None and hasattr(target, "workflow_run_id") and step is not None:
            return read_session_task_completion(
                conversation_store,
                target,
                step,
                owner_id=owner_id,
                workflow_run_id=workflow_run_id,
                step_id=step_id,
                attempt_id=attempt_id,
                session_task_dispatch=session_task_dispatch,
            )
        resolved_task_id = str(target) if target else task_id
        return read_session_task_completion(
            conversation_store,
            resolved_task_id,
            owner_id=owner_id,
            workflow_run_id=workflow_run_id,
            step_id=step_id,
            attempt_id=attempt_id,
            session_task_dispatch=session_task_dispatch,
        )

    return completion_reader


# LLM: 构造并装配工作流原生 Agent 节点端口 (WorkflowAgentTaskPort.v1 / G5b)。
# 函数用途: 为宿主注入默认的 WorkflowAgentSteps 实例，绑定真正的后台服务与 SessionTask 完成读取。
def build_default_workflow_agent_steps(host, *, background_service=None):
    if host is None:
        return None

    # 1. 解析 background_service
    if background_service is None:
        background_service = getattr(host, "business_background", None)
    if background_service is None:
        background_service = getattr(host, "background_service", None)

    conv_store = getattr(host, "conversation_store", None)
    owner_home = getattr(host, "owner_home", None)
    if owner_home is None and hasattr(host, "home_paths"):
        owner_home = getattr(host.home_paths, "owner_home", None)

    if background_service is None and conv_store is not None and owner_home is not None:
        try:
            from ..conversation.background_agent_service import BackgroundAgentService
            background_service = BackgroundAgentService(owner_home, conversation_store=conv_store)
        except Exception:
            background_service = None

    if background_service is None:
        return None

    # 2. 构造 actor_provider
    def actor_provider(run):
        from types import SimpleNamespace
        owner_id = getattr(run, "owner_id", "")
        workflow_id = getattr(run, "workflow_id", "")
        return SimpleNamespace(
            owner_id=owner_id,
            source_thread_id=f"workflow:{workflow_id}",
        )

    # 3. 构造 completion_reader
    completion_reader = make_session_task_completion_reader(conv_store)

    # 4. 构造 cancel_handler
    def cancel_handler(
        target: Any = None,
        step: Any = None,
        *,
        task_id: str = "",
        actor: Any = None,
        idempotency_key: str | None = None,
    ):
        if target is not None and hasattr(target, "workflow_run_id") and step is not None:
            return cancel_workflow_session_task(
                background_service,
                conv_store,
                target,
                step,
                actor=actor,
                idempotency_key=idempotency_key,
            )
        resolved_task_id = str(target) if target else task_id
        return cancel_workflow_session_task(
            background_service,
            conv_store,
            resolved_task_id,
            actor=actor,
            idempotency_key=idempotency_key,
        )

    return WorkflowAgentSteps(
        background_service,
        actor_provider=actor_provider,
        completion_reader=completion_reader,
        cancel_handler=cancel_handler,
    )
