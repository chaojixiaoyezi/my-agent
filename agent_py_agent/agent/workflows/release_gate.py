"""按准确 candidate/run/step/attempt 核验当前审核，不保存第二份授权账。"""
from __future__ import annotations

import time
from dataclasses import dataclass

from .review_receipts import WorkflowReviewReceipt


@dataclass(frozen=True)
class WorkflowEffectAdmission:
    review_refs: tuple[str, ...] = ()
    reconciled_step_id: str = ''
    reconciled_output: dict | None = None
    effect_not_started: bool = False


class WorkflowReleaseGate:
    def __init__(self, *, receipt_reader, authorization_check, clock=None):
        self.receipt_reader = receipt_reader
        self.authorization_check = authorization_check
        self.clock = clock or time.time

    def require(self, record, step, *, candidate_hash, policy):
        minimum = policy.get('minimum_reviews', 1)
        refs, author = policy.get('review_refs'), policy.get('author_id')
        if (type(minimum) is not int or not 1 <= minimum <= 32 or not isinstance(author, str) or not author
                or not isinstance(refs, (list, tuple)) or not 1 <= len(refs) <= 64
                or not all(isinstance(ref, str) and ref for ref in refs)):
            raise PermissionError('WORKFLOW_REVIEW_REQUIRED')
        reviewers, accepted = set(), []
        for reference in dict.fromkeys(refs):
            receipt = WorkflowReviewReceipt.from_host(self.receipt_reader(reference, record, step.step_id))
            expected = (reference, record.owner_id, record.business_context.get('task_id', ''),
                        record.workflow_run_id, step.step_id, step.escalation_result.get('attempt_id'),
                        candidate_hash, author)
            actual = (receipt.receipt_ref, receipt.owner_id, receipt.task_id, receipt.workflow_run_id,
                      receipt.step_id, receipt.attempt_id, receipt.candidate_hash, receipt.author_id)
            if (actual != expected or receipt.decision != 'approved' or receipt.reviewer_id == author
                    or receipt.expires_at <= self.clock()
                    or self.authorization_check(receipt, record) is not True):
                raise PermissionError('WORKFLOW_REVIEW_NOT_CURRENT_OR_INDEPENDENT')
            reviewers.add(receipt.reviewer_id)
            accepted.append(reference)
        if len(reviewers) < minimum:
            raise PermissionError('WORKFLOW_INDEPENDENT_REVIEWS_REQUIRED')
        return tuple(accepted)
