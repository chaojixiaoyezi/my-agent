"""审核回执的只读值对象；构造成功从不表示已经授权。"""
from __future__ import annotations

import math
from dataclasses import dataclass


def pending_review_error(run):
    """只公开失败步骤与本次运行、尝试相符的审核报码，历史回执与新回执共用。"""
    if not isinstance(run, dict) or run.get('status') != 'failed' or not run.get('failed_step_id'):
        return ''
    steps = run.get('steps')
    if not isinstance(steps, list):
        return ''
    for step in steps:
        if not isinstance(step, dict) or step.get('step_id') != run['failed_step_id']:
            continue
        escalation = step.get('escalation_result')
        pending = escalation.get('pending_review') if isinstance(escalation, dict) else None
        code = step.get('error_code')
        if (isinstance(pending, dict) and pending.get('schema') == 'workflow.pending-review.v1'
                and run.get('workflow_run_id') and pending.get('workflow_run_id') == run['workflow_run_id']
                and pending.get('step_id') == step['step_id'] and pending.get('attempt_id')
                and pending['attempt_id'] == escalation.get('attempt_id')
                and code in ('WORKFLOW_REVIEW_REQUIRED', 'WORKFLOW_REVIEW_STALE', 'WORKFLOW_REVIEW_CHANGES_REQUESTED')):
            return code
    return ''


@dataclass(frozen=True)
class WorkflowReviewReceipt:
    receipt_ref: str
    owner_id: str
    task_id: str
    workflow_run_id: str
    step_id: str
    attempt_id: str
    candidate_hash: str
    author_id: str
    reviewer_id: str
    decision: str
    expires_at: float

    @classmethod
    def from_host(cls, value):
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise PermissionError('WORKFLOW_REVIEW_RECEIPT_INVALID')
        fields = cls.__dataclass_fields__
        if set(fields) - value.keys():
            raise PermissionError('WORKFLOW_REVIEW_RECEIPT_INVALID')
        if any(not isinstance(value[key], str) or (not value[key] and key != 'task_id')
               for key in fields if key != 'expires_at'):
            raise PermissionError('WORKFLOW_REVIEW_RECEIPT_INVALID')
        if (type(value['expires_at']) not in (int, float) or not math.isfinite(value['expires_at'])
                or len(value['candidate_hash']) != 64):
            raise PermissionError('WORKFLOW_REVIEW_RECEIPT_INVALID')
        return cls(**{key: value[key] for key in fields})
