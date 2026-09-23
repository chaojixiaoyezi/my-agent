# LLM: 后台摘要范围来自宿主身份和已读任务事实；此模块只投影原检查点，不创建状态或授予权限。
# 模块用途: 把后台实际采用的摘要与消息覆盖绑定，避免全线程游标裁掉独立任务材料。
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

from .compact_scope import THREAD_COMPACT_SCOPE, CompactScope
from .compact_summary_view import AppliedCompactContext, resolve_compact_summary_view


# LLM: narrow 身份只能用宿主 conversation_turn_id；detached 必须保留原创建锚点和精确 lineage，不能猜最新任务。
# 函数用途: 从一次读取的后台范围构造 Compact 范围，缺失必要身份直接拒绝准备。
def background_compact_application(agent, thread, request, decision=None) -> AppliedCompactContext:
    from .background_context import is_narrow_audit_event

    if is_narrow_audit_event(request.reason):
        scope = CompactScope(kind="turn", task_id=request.task_id,
                             turn_id=getattr(request, "conversation_turn_id", ""))
    elif decision is not None and decision.detached:
        link = decision.exact_link or {}
        scope = CompactScope(
            kind="task", task_id=decision.task_id, task_ids=tuple(sorted(decision.task_ids)),
            anchor_message_id=str(link.get("context_anchor_message_id") or ""),
            created_at=link.get("created_at", 0.0),
        )
    else:
        scope = THREAD_COMPACT_SCOPE
    return AppliedCompactContext(thread.thread_id, scope, resolve_compact_summary_view(agent, thread, scope))


# LLM: 旧 v1 只有前缀终点，必须在完整 canonical 行内解析，不能在任务筛选后猜边界；新版本只认精确 ID。
# 函数用途: 计算本次摘要实际替代的消息，缺失旧边界视为读取不完整而不是清空历史。
def compact_covered_message_ids(view, rows) -> frozenset[str]:
    covered = set(view.source_message_ids)
    positions = {row.message_id: index for index, row in enumerate(rows)}
    for end in view.legacy_message_end_ids:
        if end not in positions:
            raise OSError("legacy compact source boundary is missing from canonical history")
        covered.update(row.message_id for row in rows[:positions[end] + 1])
    return frozenset(covered)


# LLM: operational 展示与 native seed 必须采用同一摘要；只改准备副本，不重读任务或更新进度。
# 函数用途: 将冻结的后台上下文摘要对齐本次实际视图，避免并发提交的全局摘要混入独立任务。
def apply_background_compact_context(prepared, application: AppliedCompactContext, generation: int):
    bundle = deepcopy(prepared.payload.bundle)
    thread = dict(bundle.get("thread") or {})
    thread.update(summary=application.view.summary, compact_generation=generation,
                  compact_operation_evidence=deepcopy(application.view.operation_evidence),
                  compact_checkpoint_id=application.view.checkpoint_id)
    if application.scope.kind != "thread":
        thread.update(compacted_through_message_id="", compacted_through_byte_offset=0)
    bundle["thread"] = thread
    return replace(prepared, payload=replace(prepared.payload, bundle=bundle))
