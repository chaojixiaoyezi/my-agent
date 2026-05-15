# LLM: Board payload helpers keep the model-facing subagent board compact and recovery-friendly.
# 模块用途: 整理 subagent_board 输出里的可执行 run id 和长文本，避免主 orchestration 工具文件继续膨胀。

from __future__ import annotations

from .orchestration_run_scope import remembered_orchestration_run_ids


# LLM: board_actionable_run_ids puts status buckets before verbose board rows for LLM recovery.
# 函数用途: 把看板条目按状态整理成可继续 dispatch、验收或排障的 run id 列表，方便模型先看到关键 id。
def board_actionable_run_ids(items) -> dict[str, list[str]]:
    buckets = {"planning": [], "running": [], "awaiting_acceptance": [], "blocked": [], "done": []}
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
# 函数用途: 根据看板条目生成模型可读的完成状态和下一步建议，避免父级把 AWAITING_ACCEPTANCE 误当完成。
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
        "suggested_tool_call": {
            "tool": "dispatch_subagents",
            "apply": True,
            "execute_runners": True,
            "execute_acceptance_tests": True,
            "auto_apply_acceptance_followup": True,
            "workflow_mode": "execute",
            "run_ids": blockers[:20],
            "max_runners": min(len(blockers), 8) or 1,
        },
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


# LLM: _verified_target_tokens lets board completion mirror final closeout's repair coverage semantics.
# 函数用途: 汇总 DONE/VERIFIED 行的产物 token，让旧失败/待验收 run 被后续已验收修复覆盖时不再误报阻塞。
def _verified_target_tokens(items) -> set[str]:
    tokens: set[str] = set()
    for item in items:
        status = str(getattr(item, "status", "") or "").upper()
        verification = str(getattr(item, "verification_status", "") or "").upper()
        if status == "DONE" and verification == "VERIFIED":
            tokens.update(str(token or "") for token in getattr(item, "target_tokens", []) or [])
    return {token for token in tokens if token}


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
