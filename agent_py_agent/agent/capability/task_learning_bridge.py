"""宿主canonical完成证明到原学习FIFO的消费端；DTO构造和聊天JSON均不认证。

session_tasks.load_task_learning_evidence从原claim证明和准确完成记录读回，
失配/未完成/取消返回None。W5只能准备context；此处不调用学习模型、不保存回复。
"""
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field

from ..conversation.business_contracts import BusinessExecutionContext, TrustedTaskCompletion
from .skill_learning_store import SkillLearningStore, request_key


@dataclass(frozen=True)
class TaskLearningEvidence:
    completion: TrustedTaskCompletion
    context: object
    business_context: BusinessExecutionContext | None = None
    request_material: dict = field(kw_only=True)


# LLM: 消费原 canonical reader 的身份和材料，复制后才补队列出处；不依赖活 finalize context。
# 函数用途: 将精确已完成任务的有界材料放入原学习 FIFO，不同步调用学习模型。
def enqueue_completed_task_learning(*, completion: TrustedTaskCompletion, host: object) -> dict:
    """只消费宿主重新读回的精确完成事实；保存回复和学习准入各守原开关。"""
    from ..conversation.session_tasks import load_task_learning_evidence
    from .skill_learning import SkillLearningService

    if not isinstance(completion, TrustedTaskCompletion):
        return {'status': 'ineligible', 'reason': 'canonical_completion_required'}
    service = getattr(host, 'skill_learning', None)
    if (getattr(getattr(host, 'config', None), 'enable_self_learning', False) is not True
            or not isinstance(service, SkillLearningService) or not service.settings.include_background):
        return {'status': 'ineligible', 'reason': 'background_learning_disabled'}
    home = getattr(host, 'home_paths', None)
    owner = getattr(home, 'owner_id', '')
    if (not owner or owner != completion.owner_id or service.runtime.owner_id != owner
            or service.store.directory != SkillLearningStore.for_home(home).directory):
        return {'status': 'conflict', 'reason': 'owner_binding_mismatch'}
    evidence = load_task_learning_evidence(host, completion)
    if not isinstance(evidence, TaskLearningEvidence) or evidence.completion != completion:
        return {'status': 'ineligible', 'reason': 'canonical_completion_unconfirmed'}
    ctx, business = evidence.context, evidence.business_context
    attrs = getattr(ctx, 'task_attributes', None) or {}
    task_ref = attrs.get('session_task_ref') or (business.task_id if business else getattr(ctx, 'task_id', ''))
    if (getattr(ctx, 'run_id', '') != completion.run_id or getattr(ctx, 'attempt_id', '') != completion.attempt_id
            or attrs.get('conversation_thread_id') != completion.thread_id or task_ref != completion.task_id
            or getattr(ctx, 'trajectory_digest', '') != completion.trajectory_digest):
        return {'status': 'conflict', 'reason': 'execution_binding_mismatch'}
    if business is not None and (business.owner_id != owner or business.task_id != completion.task_id
            or business.space_id != completion.space_id or business.project_ref != completion.project_ref):
        return {'status': 'conflict', 'reason': 'business_binding_mismatch'}
    if business is None and (completion.space_id is not None or completion.project_ref is not None):
        return {'status': 'conflict', 'reason': 'business_context_required'}
    if (completion.source_kind != 'background_main_agent' or getattr(ctx, 'source', '') != completion.source_kind
            or getattr(ctx, 'context_scope', '') == 'task_local'
            or getattr(ctx, 'tool_rounds', None) != completion.tool_rounds
            or completion.tool_rounds < max(1, service.settings.min_tool_rounds)):
        return {'status': 'ineligible', 'reason': 'background_source_or_rounds_ineligible'}
    request = deepcopy(evidence.request_material)
    identity = [owner, completion.task_id, completion.run_id, completion.attempt_id, completion.completion_ref]
    request.update(request_key=request_key(json.dumps(identity, ensure_ascii=False, separators=(',', ':'))),
                   owner_id=owner, task_id=completion.task_id, root_task_id=completion.task_id,
                   runtime_task_id=str(getattr(ctx, 'task_id', '') or ''), attempt_id=completion.attempt_id,
                   completion_ref=completion.completion_ref, claim_ref=completion.claim_ref,
                   result_refs=list(completion.result_refs), completion_identity=identity,
                   learning_scope={'kind': 'business_method', 'space_id': completion.space_id,
                                   'project_ref': completion.project_ref},
                   business_context=business.to_dict() if business else None,
                   asset_version_refs=list(business.asset_version_refs) if business else [])
    outcome = service._enqueue(request)
    return {'status': {'queued': 'enqueued', 'duplicate': 'duplicate', 'conflict': 'conflict',
                       'queue_full': 'ineligible'}[outcome], 'reason': outcome}
