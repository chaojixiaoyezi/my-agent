# LLM: This module mirrors 会话运行时 stop-hook continuation for the model's own
# canonical task_progress ledger. It must never inspect prose, files, tests, LOC,
# artifacts, or business quality, and it must not schedule another task turn.
# 模块用途: 模型准备收尾但自己还有未结清单时，在同一回合提醒一次；仍未核对就诚实标为阻塞。

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, ClassVar

from ...backends import ModelResponse
from ...task_progress import (
    progress_path,
    read_task_progress_report,
    task_progress_status_is_closed,
)
from .._runtime_params import ToolLoopExecuteParams
from ..runtime.owner_roots import runtime_owner_root
from ..runtime.task_identity import progress_ledger_id

_STATE_KEY = "task_progress_closeout_reconciliation"
_SCHEMA_VERSION = "task_progress_closeout.v1"
_MAX_VISIBLE_OPEN_ENTRIES = 24
_MAX_DRAFT_CHARS = 1200
_SKIPPED_SCOPES = frozenset({"control_plane", "isolated"})


# LLM: The caller consumes only these three host-owned actions. `continue` keeps
# the same tool loop; `block` carries a typed non-completed ModelResponse.
# 类用途: 告诉工具循环应忽略、同轮继续，还是以真实未完成状态停下。
@dataclass(frozen=True)
class OpenPlanCloseoutDecision:
    __test__: ClassVar[bool] = False

    action: str
    response: ModelResponse | None = None


# LLM: Read only the canonical progress ledger selected by progress_ledger_id.
# One bounded continuation is allowed by config; exhaustion changes lifecycle
# truth to blocked but preserves the model-authored text verbatim.
# 函数用途: 在自然最终回复交付前核对一次开放清单，不另起后台任务或生成固定用户文案。
def decide_open_plan_closeout(
    agent: object,
    params: ToolLoopExecuteParams,
    response: ModelResponse,
) -> OpenPlanCloseoutDecision:
    if not _eligible_closeout(agent, params, response):
        return OpenPlanCloseoutDecision("ignore")
    max_attempts = _configured_attempts(agent)
    if max_attempts <= 0:
        return OpenPlanCloseoutDecision("ignore")
    facts = _open_plan_facts(agent, params)
    if not facts:
        return OpenPlanCloseoutDecision("ignore")
    state = _closeout_state(params)
    if state is None:
        return OpenPlanCloseoutDecision("ignore")
    return _decide_with_open_facts(
        params,
        response,
        facts=facts,
        state=state,
        max_attempts=max_attempts,
    )


# LLM: This helper owns the bounded state transition after canonical facts are
# loaded. It may change only active-turn repair state and response lifecycle.
# 函数用途: 根据开放项和已用核对次数决定同轮继续或诚实阻塞。
def _decide_with_open_facts(
    params: ToolLoopExecuteParams,
    response: ModelResponse,
    *,
    facts: dict[str, object],
    state: dict[str, object],
    max_attempts: int,
) -> OpenPlanCloseoutDecision:
    attempts = _nonnegative_int(state.get("attempts"))
    actionable = _nonnegative_int(facts.get("actionable_open_count"))
    if actionable > 0 and attempts < max_attempts:
        attempt = attempts + 1
        _record_repair_attempt(state, facts, attempt, max_attempts)
        params.tool_context.append(
            _reconciliation_context(
                facts,
                response=response,
                attempt=attempt,
                max_attempts=max_attempts,
            )
        )
        return OpenPlanCloseoutDecision("continue")
    return _blocked_plan_decision(
        response,
        facts,
        state,
        max_attempts=max_attempts,
    )


# LLM: Repair accounting is active-turn diagnostic state only; it cannot grant
# another turn or mutate the progress ledger.
# 函数用途: 记录这次清单核对，供下一次自然收尾判断预算是否耗尽。
def _record_repair_attempt(
    state: dict[str, object],
    facts: dict[str, object],
    attempt: int,
    max_attempts: int,
) -> None:
    state.update(
        {
            "attempts": attempt,
            "max_attempts": max_attempts,
            "ledger_id": str(facts.get("ledger_id") or ""),
            "last_open_count": _nonnegative_int(facts.get("open_count")),
            "exhausted": False,
        }
    )


# LLM: Exhaustion and an explicitly blocked-only plan both preserve the draft
# while replacing only host-owned lifecycle fields with typed blocked truth.
# 函数用途: 生成不能再按完成落账的阻塞响应，并记录明确原因。
def _blocked_plan_decision(
    response: ModelResponse,
    facts: dict[str, object],
    state: dict[str, object],
    *,
    max_attempts: int,
) -> OpenPlanCloseoutDecision:
    actionable = _nonnegative_int(facts.get("actionable_open_count"))
    reason = (
        "TASK_PROGRESS_RECONCILIATION_EXHAUSTED"
        if actionable > 0
        else "TASK_PROGRESS_BLOCKED"
    )
    attempts = _nonnegative_int(state.get("attempts"))
    state.update(
        {
            "attempts": attempts,
            "max_attempts": max_attempts,
            "ledger_id": str(facts.get("ledger_id") or ""),
            "last_open_count": _nonnegative_int(facts.get("open_count")),
            "exhausted": actionable > 0,
            "terminal_reason": reason,
        }
    )
    return OpenPlanCloseoutDecision(
        "block",
        replace(
            response,
            runtime_status="blocked",
            runtime_reason=reason,
            runtime_source="task_progress",
            turn_end_reason="blocked",
        ),
    )


# LLM: Auxiliary expression/control turns and already-typed non-ok responses do
# not own ordinary task closeout. `save` controls optional archive persistence,
# not active-turn lifecycle: Gateway/TUI and background-main turns legitimately
# use save=False and must still reconcile their canonical task_progress ledger.
# 函数用途: 过滤不属于普通主/子代理工作回合的响应，同时让不存档的真实任务轮照常核对清单。
def _eligible_closeout(
    agent: object,
    params: ToolLoopExecuteParams,
    response: ModelResponse,
) -> bool:
    scope = str(getattr(params, "context_scope", "") or "default").strip().lower()
    status = str(getattr(response, "runtime_status", "") or "ok").strip().lower()
    attrs = params.task_attributes if isinstance(params.task_attributes, dict) else {}
    return bool(
        status == "ok"
        and scope not in _SKIPPED_SCOPES
        and not str(attrs.get("thread_goal_id") or "").strip()
        and str(attrs.get("conversation_work_kind") or "").strip().lower() != "audit"
        and bool(getattr(getattr(agent, "config", None), "enable_tools", True))
    )


# LLM: Zero is the explicit off switch; malformed values fall back to one
# bounded same-turn reconciliation instead of expanding the call budget.
# 函数用途: 读取结束核对次数配置，保证只能是有限非负整数。
def _configured_attempts(agent: object) -> int:
    raw = getattr(
        getattr(agent, "config", None),
        "task_progress_closeout_repair_attempts",
        1,
    )
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 1


# LLM: Ledger load errors fail open because progress is advisory, not a safety
# authority. The exact path and key are host-derived and never accepted from prose.
# 函数用途: 读取当前任务唯一进度账本，并整理模型核对所需的开放项事实。
def _open_plan_facts(
    agent: object,
    params: ToolLoopExecuteParams,
) -> dict[str, object]:
    ledger_id = str(progress_ledger_id(agent, params) or "").strip()
    if not ledger_id:
        return {}
    root = runtime_owner_root(agent)
    if not progress_path(root, ledger_id).exists():
        return {}
    progress, load_error = read_task_progress_report(root, ledger_id)
    if load_error:
        return {}
    entries = _open_entries(progress)
    if not entries:
        return {}
    actionable = sum(1 for item in entries if item["status"] != "blocked")
    blocked = len(entries) - actionable
    visible = entries[:_MAX_VISIBLE_OPEN_ENTRIES]
    return {
        "schema": _SCHEMA_VERSION,
        "ledger_id": ledger_id,
        "open_count": len(entries),
        "actionable_open_count": actionable,
        "blocked_open_count": blocked,
        "open_entries": visible,
        "omitted_open_count": max(0, len(entries) - len(visible)),
        "summary": str(progress.get("summary") or "").strip()[:400],
        "next_action": str(progress.get("next_action") or "").strip()[:400],
    }


# LLM: Items and coverage targets share an exact-id namespace. Closed statuses
# and all-closed coverage checks are omitted; no title similarity is performed.
# 函数用途: 把普通 Todo 与 coverage 目标合成去重后的开放项列表。
def _open_entries(progress: dict[str, Any]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in progress.get("items", []) if isinstance(progress.get("items"), list) else []:
        if not isinstance(item, dict) or task_progress_status_is_closed(item.get("status")):
            continue
        _append_open_entry(entries, seen, item, surface="item")
    coverage = progress.get("coverage")
    targets = coverage.get("targets") if isinstance(coverage, dict) else []
    for target in targets if isinstance(targets, list) else []:
        if not isinstance(target, dict) or _coverage_target_closed(target):
            continue
        _append_open_entry(
            entries,
            seen,
            target,
            surface="coverage_target",
            fallback_status=_coverage_open_status(target),
        )
    return entries


# LLM: Only exact non-empty ids enter the stop-hook packet. Human labels are
# bounded display context and never gain identity authority.
# 函数用途: 追加一条安全裁剪的开放项，重复 id 只保留第一次权威投影。
def _append_open_entry(
    entries: list[dict[str, str]],
    seen: set[str],
    item: dict[str, Any],
    *,
    surface: str,
    fallback_status: str = "",
) -> None:
    item_id = str(item.get("id") or "").strip()
    if not item_id or item_id in seen:
        return
    seen.add(item_id)
    status = str(item.get("status") or fallback_status or "unknown").strip().lower()
    entries.append(
        {
            "id": item_id[:160],
            "title": " ".join(str(item.get("title") or "").split())[:240],
            "status": status if status else "unknown",
            "surface": surface,
        }
    )


# LLM: Coverage completion follows the ledger's public status semantics plus
# explicit check statuses; it does not inspect evidence text or requirement prose.
# 函数用途: 判断 coverage 目标是否已由结构化状态或全部检查关闭。
def _coverage_target_closed(target: dict[str, Any]) -> bool:
    if task_progress_status_is_closed(target.get("status")):
        return True
    checks = target.get("checks")
    return bool(
        isinstance(checks, dict)
        and checks
        and all(task_progress_status_is_closed(status) for status in checks.values())
    )


# LLM: A target is blocked only when its own status is blocked or every still-open
# check is explicitly blocked. Mixed pending/blocked checks remain actionable.
# 函数用途: 给未关闭 coverage 目标归一化可执行或阻塞状态。
def _coverage_open_status(target: dict[str, Any]) -> str:
    status = str(target.get("status") or "").strip().lower()
    if status == "blocked":
        return status
    checks = target.get("checks")
    open_checks = [
        str(value or "").strip().lower()
        for value in checks.values()
        if not task_progress_status_is_closed(value)
    ] if isinstance(checks, dict) else []
    return "blocked" if open_checks and all(value == "blocked" for value in open_checks) else (status or "unknown")


# LLM: The repair state lives only in this active turn's host-owned archive
# state. It is not durable authorization and cannot trigger a later model run.
# 函数用途: 取得或创建本回合的核对计数，防止模型重复收尾造成无限循环。
def _closeout_state(params: ToolLoopExecuteParams) -> dict[str, object] | None:
    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict):
        return None
    current = state.get(_STATE_KEY)
    if not isinstance(current, dict):
        current = {}
        state[_STATE_KEY] = current
    return current


# LLM: This packet contains structured ledger facts and the rejected draft only
# as non-authoritative context. The model must reconcile by exact task_progress id.
# 函数用途: 生成一次同轮停止核对提示，让模型继续、关项或如实标记真实阻塞。
def _reconciliation_context(
    facts: dict[str, object],
    *,
    response: ModelResponse,
    attempt: int,
    max_attempts: int,
) -> str:
    envelope = {
        **facts,
        "repair_attempt": attempt,
        "max_repair_attempts": max_attempts,
        "rejected_final_draft": _bounded_text(
            getattr(response, "text", ""),
            _MAX_DRAFT_CHARS,
        ),
        "allowed_resolution": [
            "continue_with_existing_tools",
            "close_exact_items_as_done_or_skipped",
            "mark_exact_items_blocked_with_real_reason",
        ],
    }
    return (
        "[tool-system:task-progress-closeout-reconciliation]\n"
        + json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n上一份最终草稿已暂缓交付：你自己维护的 canonical 清单仍有开放项。"
        "你仍在同一个 active turn，并保留原工具。能继续就直接推进；完成或明确不做的项请用 task_progress "
        "按原 exact id 更新为 done/skipped；确实受当前条件阻塞的项更新为 blocked 并写清真实原因。"
        "不要只在正文里宣称完成，也不要重建同义清单。清单核对后再给用户如实的最终说明。"
    )


# LLM: Draft truncation preserves both ends without interpreting its language.
# 函数用途: 限制被退回草稿的上下文开销，同时保留开头和结尾供模型自查。
def _bounded_text(value: object, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    marker = "\n…\n"
    head = (limit - len(marker)) // 2
    tail = limit - len(marker) - head
    return text[:head] + marker + text[-tail:]


# LLM: Malformed counters must not expand the stop-hook retry budget.
# 函数用途: 安全读取非负计数，损坏值按零处理。
def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


__all__ = ["OpenPlanCloseoutDecision", "decide_open_plan_closeout"]
