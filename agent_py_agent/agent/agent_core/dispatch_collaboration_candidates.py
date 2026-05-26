# LLM: Collaboration responder selection extends runner dispatch without broadening normal scan scope.
# 模块用途: 为待响应协作请求挑选可唤醒 runner，并按配置放宽协作 fan-out。

from __future__ import annotations

from dataclasses import dataclass


# LLM: CollaborationCandidateLimitInput keeps fan-out policy arguments explicit and compact.
# 类用途: 汇总协作候选限流输入，避免 helper 参数持续膨胀。
@dataclass(frozen=True)
class CollaborationCandidateLimitInput:
    config: object | None
    requested_limit: int
    candidates: list
    execute_runners: bool
    has_explicit_run_ids: bool


# LLM: collaboration_request_runner_candidates treats a targeted request as new work for an idle responder.
# 函数用途: 已完成的子代理如果后来被协作 request 点名，可以再跑一轮读取 targeted_requests 并提交证据。
def collaboration_request_runner_candidates(agent, tasks: list) -> list:
    store = getattr(agent, "collaboration_store", None)
    if store is None or not hasattr(store, "pending_requests_for_agent"):
        return []
    candidates = []
    for task in tasks:
        if _has_pending_request_for_task(store, task):
            candidates.append(task)
    return candidates


def _has_pending_request_for_task(store: object, task: object) -> bool:
    if not _can_run_for_collaboration_request(task):
        return False
    try:
        requests = store.pending_requests_for_agent(
            agent_id=str(getattr(task, "id", "") or ""),
            agent_name=str(getattr(task, "agent_name", "") or ""),
            agent_role=str(getattr(task, "role", "") or ""),
            limit=1,
        )
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    return bool(requests)


# LLM: _can_run_for_collaboration_request excludes unsafe live states without blocking completed responders.
# 函数用途: 新协作请求可以唤醒 DONE/VERIFIED 代理，但不会打断 RUNNING、BROKEN 或已接管/放弃的任务。
def _can_run_for_collaboration_request(task: object) -> bool:
    status = str(getattr(task, "status", "") or "").upper()
    if status in {"RUNNING", "TAKEN_OVER", "ABANDONED", "CHANNEL_ERROR", "TIMEOUT"}:
        return False
    if str(getattr(task, "channel_status", "") or "").upper() == "BROKEN":
        return False
    if any(getattr(item, "status", "") == "OPEN" for item in getattr(task, "capability_requests", []) or []):
        return False
    if any(getattr(item, "status", "") == "OPEN" for item in getattr(task, "capability_gaps", []) or []):
        return False
    return True


# LLM: collaboration_candidate_limit keeps responder fan-out fast without changing normal dispatch breadth.
# 函数用途: 有待响应协作请求时，隐式 dispatch 可按配置一次唤醒多名 responder，避免协作证据串行排队。
def collaboration_candidate_limit(request: CollaborationCandidateLimitInput) -> int:
    limit = max(0, int(request.requested_limit or 0))
    if not _should_expand_collaboration_limit(request, limit):
        return limit
    cap = _collaboration_auto_dispatch_limit(request.config)
    if cap <= 0:
        return limit
    return max(limit, min(len(request.candidates), cap))


def _should_expand_collaboration_limit(request: CollaborationCandidateLimitInput, limit: int) -> bool:
    return bool(
        request.execute_runners
        and not request.has_explicit_run_ids
        and request.candidates
        and limit > 0
    )


def _collaboration_auto_dispatch_limit(config: object | None) -> int:
    raw = getattr(config, "collaboration_auto_dispatch_max_runners", 8)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 8
