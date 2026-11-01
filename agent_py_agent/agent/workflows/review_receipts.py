"""审核回执的只读值对象；构造成功从不表示已经授权。"""
from __future__ import annotations

import math
from dataclasses import dataclass


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
