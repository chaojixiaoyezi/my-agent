# LLM: Board payload helpers keep the model-facing subagent board compact and recovery-friendly.
# 模块用途: 整理 subagent_board 输出里的可执行 run id 和长文本，避免主 orchestration 工具文件继续膨胀。

from __future__ import annotations

from ..subagents.kernel import SubagentKernelQuery
from .orchestration_run_scope import remembered_orchestration_run_ids


# LLM: board_actionable_run_ids puts status buckets before verbose board rows for LLM recovery.
# 函数用途: 把看板条目按状态整理成可继续 dispatch、验收或排障的 run id 列表，方便模型先看到关键 id。
def board_actionable_run_ids(items) -> dict[str, list[str]]:
    buckets = {"planning": [], "running": [], "blocked": [], "done": []}
    for item in items:
        status = str(item.status or "").lower()
        key = status if status in buckets else ""
        if key:
            buckets[key].append(item.id)
    return {key: value for key, value in buckets.items() if value}


# LLM: scoped_board_items narrows model-facing board rows to the active root-turn scope when available.
# 函数用途: 当前轮已经创建或调度子代理时，看板默认只展示同一轮相关 run，避免旧任务污染父级判断。
def scoped_board_items(agent: object, items) -> list:
    seen = remembered_orchestration_run_ids(agent)
    rows = list(items or [])
    if not seen:
        return rows
    root_ids = {
        str(getattr(item, "root_id", "") or getattr(item, "id", "") or "")
        for item in rows
        if str(getattr(item, "id", "") or "") in seen
    }
    scoped = [
        item for item in rows
        if _board_item_in_scope(item, seen, root_ids)
    ]
    return scoped


# LLM: board_completion_status puts the “do not report done yet” fact at the top of board payloads.
# 函数用途: 根据看板条目生成模型可读的完成状态和下一步建议，避免父级把未完成子任务误当完成。
def board_completion_status(items) -> dict[str, object]:
    verified_targets = _verified_target_tokens(items)
    blockers = [
        item.id
        for item in items
        if _item_blocks_completion(item, verified_targets)
    ]
    if not blockers:
        return {
            "status": "complete_or_no_blockers",
            "blocking_run_ids": [],
            "must_not_report_done": False,
            "recommended_next_action": "summarize_done_verified_refs",
        }
    return {
        "status": "not_complete",
        "blocking_run_ids": blockers[:20],
        "must_not_report_done": True,
        "recommended_next_action": "continue_dispatch_or_repair_blocking_run_ids",
        "suggested_tool_call": _suggested_dispatch_tool_call(items, blockers),
    }


def board_aggregation_readiness(items) -> dict[str, object]:
    rows = list(items or [])
    unfinished = [
        str(getattr(item, "id", "") or "")
        for item in rows
        if str(getattr(item, "status", "") or "").upper() not in {"DONE", "TAKEN_OVER"}
    ]
    artifact_ready_count = sum(
        1
        for item in rows
        for row in _artifact_registry_rows(item)
        if str(row.get("status") or "").lower() in {"", "ready", "valid"}
    )
    return {
        "total_child_count": len(rows),
        "completed_child_count": len(rows) - len(unfinished),
        "artifact_ready_count": artifact_ready_count,
        "missing_or_unfinished_run_ids": [value for value in unfinished if value][:20],
        "recommended_next_action": "read_ready_refs_then_aggregate_or_continue_unfinished_children",
    }


# LLM: board_kernel_snapshot_payload exposes the new kernel read model to parent agents without reading artifacts.
# 函数用途: 给 subagent_board 输出附加统一内核快照，父级后续按结构化状态桶和 refs 判断下一步。
def board_kernel_snapshot_payload(agent: object, items) -> dict[str, object]:
    manager = getattr(agent, "subagents", None)
    if manager is None or not _manager_has_kernel_snapshot(manager):
        return {}
    root_id = _single_board_root_id(items)
    if not root_id:
        return {}
    snapshot = manager.kernel_snapshot(SubagentKernelQuery(root_id=root_id))
    return {
        "schema_version": snapshot.schema_version,
        "root_id": snapshot.root_id,
        "scope": snapshot.scope,
        "running_run_ids": snapshot.running_run_ids,
        "blocked_run_ids": snapshot.blocked_run_ids,
        "completed_run_ids": snapshot.completed_run_ids,
        "failed_run_ids": snapshot.failed_run_ids,
        "takeover_candidate_run_ids": snapshot.takeover_candidate_run_ids,
        "rows": [_kernel_row_payload(row) for row in snapshot.runs[:20]],
        "warnings": list(snapshot.warnings),
    }


# LLM: _board_item_in_scope mirrors final closeout scoping for board rows.
# 函数用途: 精确 id 或同 root 子树属于当前轮；其它历史 run 不进入默认看板 payload。
def _board_item_in_scope(item, seen: set[str], root_ids: set[str]) -> bool:
    item_id = str(getattr(item, "id", "") or "")
    if item_id in seen:
        return True
    root_id = str(getattr(item, "root_id", "") or "")
    return bool(root_id and root_id in root_ids)


# LLM: _item_blocks_completion mirrors final closeout semantics at board-read time.
# 函数用途: 判断单个看板条目是否还会阻止最终汇报完成；状态或验收未过都算阻塞。
def _item_blocks_completion(item, verified_targets: set[str]) -> bool:
    status = str(getattr(item, "status", "") or "").upper()
    verification = str(getattr(item, "verification_status", "") or "").upper()
    if status in {"DONE", "TAKEN_OVER"} and verification == "VERIFIED":
        return False
    targets = set(getattr(item, "target_tokens", []) or [])
    if targets and targets.issubset(verified_targets):
        return False
    return status not in {"DONE"} or verification != "VERIFIED"


# LLM: Board advice suggests continuing unfinished direct children.
# 函数用途: 看板只给继续推进建议；不再生成最终收口-only 路径。
def _suggested_dispatch_tool_call(items, blockers: list[str]) -> dict[str, object]:
    payload: dict[str, object] = {
        "tool": "dispatch_subagents",
        "apply": True,
        "execute_runners": True,
        "workflow_mode": "execute",
        "run_ids": blockers[:20],
    }
    payload["max_runners"] = min(len(blockers), 8) or 1
    return payload


# LLM: _verified_target_tokens lets board completion mirror final closeout's repair coverage semantics.
# 函数用途: 汇总 DONE/VERIFIED 行的产物 token，让旧失败/待收口 run 被后续已验收修复覆盖时不再误报阻塞。
def _verified_target_tokens(items) -> set[str]:
    tokens: set[str] = set()
    for item in items:
        status = str(getattr(item, "status", "") or "").upper()
        verification = str(getattr(item, "verification_status", "") or "").upper()
        if status == "DONE" and verification == "VERIFIED":
            tokens.update(str(token or "") for token in getattr(item, "target_tokens", []) or [])
    return {token for token in tokens if token}


# LLM: _manager_has_kernel_snapshot avoids MagicMock auto-attributes and keeps board tests deterministic.
# 函数用途: 只有真实 manager 类或显式 fake 暴露 kernel_snapshot 时，才追加内核快照。
def _manager_has_kernel_snapshot(manager: object) -> bool:
    return callable(getattr(type(manager), "kernel_snapshot", None))


# LLM: _single_board_root_id keeps kernel snapshot selection scoped to one root tree.
# 函数用途: 从当前看板行推导唯一 root_id；多棵树混在一起时不猜，避免污染父级判断。
def _single_board_root_id(items) -> str:
    root_ids = {
        str(getattr(item, "root_id", "") or getattr(item, "id", "") or "")
        for item in items
        if str(getattr(item, "root_id", "") or getattr(item, "id", "") or "")
    }
    return next(iter(root_ids)) if len(root_ids) == 1 else ""


# LLM: _kernel_row_payload keeps the board payload compact and refs-first.
# 函数用途: 将 kernel run 行转成 JSON 友好字段，不展开 goal、artifact 正文或长日志。
def _kernel_row_payload(row: object) -> dict[str, object]:
    return {
        "task_id": row.task_id,
        "run_id": row.run_id,
        "parent_id": row.parent_id,
        "parent_run_id": row.parent_run_id,
        "root_run_id": row.root_run_id,
        "depth": row.depth,
        "agent_kind": row.agent_kind,
        "role": row.role,
        "agent_name": row.agent_name,
        "status": row.status,
        "verification_status": row.verification_status,
        "progress": row.progress,
        "current_tool": row.current_tool,
        "heartbeat_at": row.heartbeat_at,
        "last_progress_at": row.last_progress_at,
        "running_seconds": int(float(getattr(row, "running_seconds", 0.0) or 0.0)),
        "seconds_since_progress": int(float(getattr(row, "seconds_since_progress", 0.0) or 0.0)),
        "last_progress_summary": row.last_progress_summary,
        "child_ids": list(row.child_ids),
        "address": dict(row.address),
        "task_envelope": dict(row.task_envelope),
        "workspace_refs": dict(row.workspace_refs),
        "recovery_refs": dict(row.recovery_refs),
        "tool_contract": dict(row.tool_contract),
        "artifact_refs": list(row.artifact_refs),
        "artifact_registry_refs": list(row.artifact_registry_refs),
        "evidence_refs": list(row.evidence_refs),
        "blockers": list(row.blockers),
    }


def _artifact_registry_rows(item: object) -> list[dict[str, object]]:
    value = getattr(item, "artifact_registry_refs", None)
    if not isinstance(value, list | tuple):
        return []
    return [dict(row) for row in value if isinstance(row, dict)]


# LLM: board_status_filter normalizes model-friendly aliases before filtering board rows.
# 函数用途: 把空值、ALL、* 这类“查看全部”的自然写法归一成不过滤，避免模型误把看板过滤空。
def board_status_filter(value: object) -> str:
    text = str(value or "").strip().upper()
    if text in {"", "ALL", "*", "ANY"}:
        return ""
    return text


# LLM: clip_board_text keeps subagent_board useful without flooding later model prompts.
# 函数用途: 截断看板中的长 goal，完整内容仍保留在 task_dir 或 context bundle 里，需要时再读。
def clip_board_text(value: str, *, limit: int = 260) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "...[truncated]"
