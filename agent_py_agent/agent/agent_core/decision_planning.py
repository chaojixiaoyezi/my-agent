# LLM: Jev only suggests priority among exact open IDs from the current canonical Todo ledger.
# 模块用途: 在主代理读取现有计划时给出可选优先项；不改账本、不创建目标或子代理。
from __future__ import annotations

import hashlib
import time
from pathlib import Path

from ..backends.decision_protocol import decision_json
from ..concurrency.interrupt import is_interrupted
from ..conversation.decision_service import (
    begin_decision_stage,
    decide,
    decision_outcome_is_current,
)
from ..runtime_context import current_subagent_run_id
from ..settings.decision_settings_schema import POINT_RUNTIME_SCOPES
from ..task_progress import (
    progress_path,
    read_task_progress,
    task_progress_display_identity,
    task_progress_display_items,
    task_progress_status_is_closed,
)
from ..tooling.cancellation import ToolCancelled, raise_if_cancelled
from .orchestration.dispatch_progress_seed import _known_child_run_ids
from .runtime.task_identity import progress_ledger_id

_POINT = "planning"
_MAX_CANDIDATES = 24
_NON_SELECTIONS = {
    "not_needed": "无需额外优先级建议，继续原计划",
    "no_match": "现有候选均不适合优先推荐",
    "abstain": "无法可靠选择",
    "need_data": "缺少判断材料，由主代理按原工具补读，不向用户逐次询问",
}


# LLM: Read-only enhancement after the original task_progress read/reconciliation.
# Only the current main run may receive a model-visible hint; observe/errors do
# not alter the canonical result. Propagate user cancellation, not optional failures.
# 函数用途: 对当前已存在的多项未完成 Todo 提议一个优先 ID，失败时原样交还回执。
def todo_priority_hint(agent: object, root: Path, run_id: str, payload: dict) -> dict | None:
    if _POINT not in POINT_RUNTIME_SCOPES:
        return None
    try:
        params = getattr(agent, "_current_run_params", None)
        if not _eligible(agent, params, root, run_id, payload):
            return None
        stage = begin_decision_stage(agent, params, operation_id=_operation_id(params, run_id, payload))
        if stage.error_code or _POINT not in stage.enabled_points:
            return None
        _check_interrupted()
        state, questions, revision = _material(agent, payload)
        outcome = decide(
            agent, params, stage, point=_POINT, state=state, questions=questions,
            candidates_revision=revision,
            source_refs=(f"task_progress:{run_id}:{state['generation_id']}:{state['plan_revision']}",),
        )
        _check_interrupted()
        if not outcome.may_apply or outcome.response is None:
            return None
        deadline = min(stage.deadline, outcome.deadline)
        if time.monotonic() >= deadline or outcome.response.binding.candidates_revision != revision:
            return None
        selected = _selected_id(outcome.response, state)
        if not selected:
            return None
        latest = read_task_progress(root, run_id)
        if not _eligible(agent, params, root, run_id, latest) or _material(agent, latest)[2] != revision:
            return None
        if not decision_outcome_is_current(agent, params, stage, outcome):
            return None
        _check_interrupted()
        if time.monotonic() >= deadline or _material(agent, read_task_progress(root, run_id))[2] != revision:
            return None
        return {
            "schema_version": "decision-planning-priority.v1",
            "item_id": selected,
            "ledger_run_id": run_id,
            "generation_id": state["generation_id"],
            "plan_revision": state["plan_revision"],
            "message": "可优先评估该现有待办；这只是建议，仍由主代理按原计划、权限和工具合同决定是否执行或派工。",
        }
    except (InterruptedError, ToolCancelled):
        raise
    except Exception:
        _check_interrupted()
        return None


# LLM: A read of a historical ledger or a child run cannot acquire planning
# authority. The display generation/revision must come from the same ledger.
# 函数用途: 只允许当前主代理的真实、有多项 open 项的计划进入可选分析。
def _eligible(agent: object, params: object, root: Path, run_id: str, payload: dict) -> bool:
    if params is None or current_subagent_run_id(agent) or not isinstance(payload, dict) or payload.get("load_error"):
        return False
    if str(getattr(params, "context_scope", "default") or "default").lower() not in {"", "default", "conversation"}:
        return False
    current_id = progress_ledger_id(agent, params)
    if not current_id or current_id != run_id or not progress_path(root, run_id).is_file():
        return False
    generation, revision = task_progress_display_identity(payload)
    if not generation or revision <= 0:
        return False
    rows = _candidate_rows(agent, payload)
    return 2 <= len(rows) <= _MAX_CANDIDATES and bool(_request_text(agent))


# LLM: Reuse the dispatch contract's exact child IDs to omit generated roster
# rows; do not recognize them by titles or infer work bindings from prose.
# 函数用途: 读取有限原待办候选，跳过已关闭项和真实子代理展示行，保持账本顺序与精确 ID。
def _candidate_rows(agent: object, payload: dict) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    child_ids = _known_child_run_ids(agent)
    for item in task_progress_display_items(payload):
        if not isinstance(item, dict) or task_progress_status_is_closed(item.get("status")):
            continue
        item_id, title = item.get("id"), item.get("title")
        if item_id in child_ids:
            continue
        if (type(item_id) is not str or not item_id or len(item_id) > 128 or item_id in seen
                or type(title) is not str or not title or len(title) > 256):
            return []
        seen.add(item_id)
        rows.append({"id": item_id, "title": title, "status": str(item.get("status") or "pending")})
    return rows


# LLM: Never send a partial current request; if it is too long, skip the
# optional decision rather than giving Jev a misleading fragment.
# 函数用途: 取出当前请求的有界原文，超长或缺失时保留原计划。
def _request_text(agent: object) -> str:
    value = getattr(agent, "_current_user_prompt", "")
    return value if type(value) is str and 0 < len(value) <= 1024 else ""


# LLM: Candidate keys are host-generated, not inferred from the model. The
# digest includes exact IDs, order, titles, statuses and display revision.
# 函数用途: 将现有 Todo 编成有限选择题，并给等待后的原账本复核提供稳定摘要。
def _material(agent: object, payload: dict) -> tuple[dict, dict, str]:
    rows = _candidate_rows(agent, payload)
    generation, plan_revision = task_progress_display_identity(payload)
    state = {
        "current_request": _request_text(agent),
        "generation_id": generation,
        "plan_revision": plan_revision,
        "todos": [{"candidate": f"todo_{i}", **row} for i, row in enumerate(rows, 1)],
        "notice": "只建议现有未完成 Todo 的一个优先 ID；不改变计划、Goal、权限或派工。",
    }
    criteria = {f"todo_{i}": {"item_id": row["id"], "title": row["title"], "status": row["status"]}
                for i, row in enumerate(rows, 1)}
    questions = {"priority": {"type": "choice", "instructions": "选一个当前最值得优先评估的已有 Todo；不能创造新项。",
                              "criteria": {**criteria, **_NON_SELECTIONS}}}
    revision = hashlib.sha256(decision_json({"state": state, "questions": questions})).hexdigest()
    return state, questions, revision


# LLM: One malformed, missing or non-selection answer yields no hint; the
# response cannot inject an arbitrary item ID or alter the original order.
# 函数用途: 把单题选择映射回宿主给出的精确 Todo ID，其他结果一律保留原计划。
def _selected_id(response: object, state: dict) -> str:
    answers = getattr(response, "answers", ())
    if len(answers) != 1:
        return ""
    answer = answers[0]
    if (getattr(answer, "question_id", "") != "priority" or getattr(answer, "kind", "") != "choice"
            or getattr(answer, "error_code", "")):
        return ""
    choices = {row["candidate"]: row["id"] for row in state["todos"]}
    return choices.get(getattr(answer, "value", ""), "")


# LLM: Operation identity is derived from the current run/request and exact
# plan revision, never the prose of a suggested next step.
# 函数用途: 生成本轮 Todo 分析的稳定操作编号，供原用量与请求绑定链使用。
def _operation_id(params: object, run_id: str, payload: dict) -> str:
    generation, revision = task_progress_display_identity(payload)
    data = [getattr(params, "request_id", ""), getattr(params, "run_id", ""), run_id, generation, revision]
    return _POINT + ":" + hashlib.sha256(decision_json(data)).hexdigest()


# LLM: A true user cancellation must propagate through the normal tool path;
# only optional provider/configuration failures may fall back to the old result.
# 函数用途: 在准备、发送和采用前复查本轮是否被用户停止。
def _check_interrupted() -> None:
    raise_if_cancelled()
    if is_interrupted():
        raise InterruptedError("当前规划建议随用户任务停止。")
