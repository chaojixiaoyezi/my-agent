# LLM: 只投影宿主提交结果，不复制候选正文；历史清理恢复不得计入本次正式提交数量。
# 模块用途: 为策展运行保留不含私密正文的晋升摘要。
"""Host-only Curator promotion receipts; never copy candidate content."""


# LLM: 只投影宿主晋升结果的白名单字段；历史恢复绝不计为本轮提交，删除待清理由显式提交标志判定。
# 函数用途: 保留每条候选的状态、原因与本轮正式变更事实。
def promotion_receipt(candidate_id: str, result: object) -> dict[str, object]:
    code = str(getattr(result, 'reason_code', '') or '')
    promoted = bool(getattr(result, 'promoted', False))
    changed = bool(getattr(result, 'formal_memory_changed', False))
    return dict(candidate_id=candidate_id, status=str(getattr(result, 'status', '') or ''),
                promoted=promoted, reason_code=code, formal_memory_changed=changed)


# LLM: 只统计本轮返回的结构化回执，不回查候选或按正文猜测副作用。
# 函数用途: 将本轮正式提交、历史恢复和清理欠账分开计数。
def promotion_counts(receipts: tuple[dict[str, object], ...]) -> dict[str, int]:
    return dict(formal_commits=sum(bool(row['formal_memory_changed']) for row in receipts),
                cleanup_recovered=sum(row['reason_code'] == 'REMOVE_CLEANUP_RECOVERED' for row in receipts),
                cleanup_pending=sum(row['reason_code'] == 'REMOVE_CLEANUP_PENDING' for row in receipts))
