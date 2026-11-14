"""基础工具与当前有效临时grant共用的权威投影；不从角色文案或展示manifest反推权限。"""
from __future__ import annotations

import math
import time


def valid_task_grants(task, *, now=None):
    now = time.time() if now is None else now
    from .models import SUBAGENT_ENDED_STATUSES, task_status_in
    closed = bool(task.ended_at) or task_status_in(task.status, SUBAGENT_ENDED_STATUSES)
    return [g for g in task.capability_grants if g.grant_to_run_id == task.id
        and not (g.expires_after_task and closed)
        and type(g.expires_at) in (int, float) and math.isfinite(g.expires_at)
        and g.expires_at >= 0 and (not g.expires_at or g.expires_at > now)]


def task_tool_authority(task, *, grants=None):
    grants = valid_task_grants(task) if grants is None else grants
    granted = {name for g in grants for name in (*g.tools, *g.mcp_tools)}
    ambiguous = set()
    if task.base_allowed_tools is None:
        # 旧账只保留能证明不是grant专属的原工具；混合来源必须显式重新授权，不能猜成角色独立权限。
        historical = {name for g in task.capability_grants for name in (*g.tools, *g.mcp_tools)}
        historical.update(name for r in task.capability_requests
            if r.from_run_id == task.id and r.status == 'GRANTED'
            for name in (*r.requested_tools, *r.requested_mcp_tools))
        base = set(task.allowed_tools) - historical
        ambiguous = historical - granted
    else:
        base = set(task.base_allowed_tools)
    return [name for name in task.allowed_tools if name in base or name in granted], sorted(ambiguous)
