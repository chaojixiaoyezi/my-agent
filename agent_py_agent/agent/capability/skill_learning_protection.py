# LLM: V2-3 保护名单严禁扫全量线程历史；仅读取未结算任务、活跃子代理、未删除监控及待审提案索引，
#   且全流程共享 RetentionScanBudget (entries/bytes/ms) 上限，截断或不可读时返回 protection_complete=False。
# 模块用途: 为自学维护有界收集活跃引用保护名单，确保耗时与会话历史总量解耦。
from __future__ import annotations

import inspect
import json
import time

from ..conversation.models import THREAD_TASK_LINK_INACTIVE_STATUSES
from ..memory_store.retention_scan_budget import RetentionScanBudget
from ..subagents.models import SUBAGENT_ENDED_STATUSES, task_status_in


def _approx_bytes(value: object) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
    except Exception:  # noqa: BLE001
        return len(str(value).encode("utf-8", errors="ignore"))


def _zero_arg_callable(fn: object) -> bool:
    if not callable(fn):
        return False
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    for param in sig.parameters.values():
        if (
            param.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            and param.default is inspect.Parameter.empty
        ):
            return False
    return True


def _supports_limit_kwarg(fn: object) -> bool:
    if not callable(fn):
        return False
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    return "limit" in sig.parameters or any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )


# LLM: 四个 canonical 活跃索引来源共享有界配额与墙钟预算；严禁调用 threads.list_report(limit=0)。
# 函数用途: 汇集未结算任务、监控、待审提案及未结束子代理的保护名单。
def learning_protection_for_agent(agent, *, scan_limit=1000):
    protected: set[str] = set()
    complete = True
    budget = RetentionScanBudget.default(getattr(agent, "config", None))
    if type(scan_limit) is int and scan_limit > 0:
        budget.max_entries = min(budget.max_entries, scan_limit)

    try:
        tasks_complete = _collect_unsettled_tasks(agent, protected, budget)
        complete = complete and tasks_complete
    except Exception:  # noqa: BLE001
        complete = False

    try:
        jobs, errors = agent.scheduler_repository.list_jobs(include_deleted=False)
        complete = complete and not errors
        for job in jobs:
            if budget.exhausted():
                complete = False
                break
            _include_refs(protected, job)
            _include_refs(protected, job.get("payload") or {})
            budget.charge(entries=1, size=_approx_bytes(job))
    except Exception:  # noqa: BLE001
        complete = False

    try:
        from .skill_learning_proposals import list_learning_proposals
        from .skill_learning_store import SkillLearningStore

        proposals = list_learning_proposals(
            SkillLearningStore.for_home(agent.home_paths),
            include_overflow=True,
        )
        for proposal in proposals:
            if budget.exhausted():
                complete = False
                break
            if proposal.get("status") == "pending" and proposal.get("expected_sha256"):
                protected.add("owner:" + proposal["decision"]["name"])
            budget.charge(entries=1, size=_approx_bytes(proposal))
    except Exception:  # noqa: BLE001
        complete = False

    try:
        reader = getattr(agent.subagents, "list_recovery_runs_report", None)
        if callable(reader):
            if _supports_limit_kwarg(reader):
                remaining = max(1, budget.max_entries - budget.entries)
                report = reader(limit=remaining + 1)
            else:
                report = reader()
        else:
            report = agent.subagents.list_runs_report()
        complete = complete and not report.load_errors
        for child in report.runs:
            if budget.exhausted():
                complete = False
                break
            if not task_status_in(child.status, SUBAGENT_ENDED_STATUSES):
                attrs = child.attributes or {}
                _include_refs(protected, attrs)
                budget.charge(entries=1, size=_approx_bytes(attrs))
    except Exception:  # noqa: BLE001
        complete = False

    if budget.size >= budget.max_bytes or (budget.entries > 0 and time.monotonic() >= budget.deadline):
        complete = False
    return {"protected_ids": tuple(sorted(protected)), "protection_complete": complete}


def _collect_unsettled_tasks(agent, protected: set[str], budget: RetentionScanBudget) -> bool:
    conversations = getattr(agent, "conversation_store", None)
    tasks = getattr(conversations, "tasks", None) if conversations is not None else None
    repo = getattr(agent, "runtime_db", None) or getattr(getattr(agent, "subagents", None), "runtime_db", None)

    if _zero_arg_callable(getattr(tasks, "unsettled_report", None)):
        rows, errors = tasks.unsettled_report()
        return _consume_task_links(rows, errors, protected, budget)

    if _zero_arg_callable(getattr(tasks, "active_report", None)):
        rows, errors = tasks.active_report()
        return _consume_task_links(rows, errors, protected, budget)

    if repo is not None and callable(getattr(repo, "open_task_runs", None)):
        remaining = max(1, budget.max_entries - budget.entries)
        open_runs = repo.open_task_runs(limit=remaining + 1)
        complete = len(open_runs) <= remaining
        seen_threads: set[str] = set()
        seen_task_links: set[str] = set()
        for run in open_runs[:remaining]:
            if budget.exhausted():
                complete = False
                break
            meta_raw = str(run["metadata_json"] if hasattr(run, "keys") else "{}")
            budget.charge(entries=1, size=len(meta_raw.encode("utf-8")))
            try:
                meta = json.loads(meta_raw) if meta_raw else {}
            except (TypeError, ValueError):
                complete = False
                meta = {}
            if isinstance(meta, dict):
                _include_refs(protected, meta)
            task_id = str(run["task_id"] if hasattr(run, "keys") else "")
            task_row = repo.get_task(task_id) if task_id and callable(getattr(repo, "get_task", None)) else None
            if task_row is None:
                continue
            conv_task_id = str(task_row["conversation_task_id"] or "")
            thread_id = str(task_row["thread_id"] or "")
            if conv_task_id and tasks is not None and callable(getattr(tasks, "load_report", None)):
                if conv_task_id in seen_task_links:
                    continue
                seen_task_links.add(conv_task_id)
                link, err = tasks.load_report(conv_task_id)
                if err:
                    complete = False
                if link is not None and link.status not in THREAD_TASK_LINK_INACTIVE_STATUSES:
                    payload_dict = link.to_dict()
                    _include_refs(protected, payload_dict)
                    budget.charge(entries=0, size=_approx_bytes(payload_dict))
            elif thread_id and tasks is not None and callable(getattr(tasks, "active_report", None)):
                if thread_id in seen_threads:
                    continue
                seen_threads.add(thread_id)
                links, errors = tasks.active_report(thread_id)
                if not _consume_task_links(links, errors, protected, budget, charge_entries=False):
                    complete = False
        return complete

    if conversations is None:
        return False

    directory = getattr(getattr(tasks, "storage", None), "tasks_dir", None)
    if directory is not None and not directory.is_symlink() and (not directory.exists() or not any(directory.iterdir())):
        return True
    return False


def _consume_task_links(
    rows,
    errors,
    protected: set[str],
    budget: RetentionScanBudget,
    *,
    charge_entries: bool = True,
) -> bool:
    complete = not errors
    for row in rows:
        if getattr(row, "status", None) in THREAD_TASK_LINK_INACTIVE_STATUSES:
            continue
        if budget.exhausted():
            complete = False
            break
        data = row.to_dict() if hasattr(row, "to_dict") else dict(row)
        _include_refs(protected, data)
        budget.charge(entries=1 if charge_entries else 0, size=_approx_bytes(data))
    return complete


# LLM: 仅取声明式 refs/allowed_skills；空数组完整、坏形状抛错使外层保护不完整，禁止将坏账当空集。
# 函数用途: 合并一个 canonical 任务或监控的明确引用。
def _include_refs(protected, data):
    for key in ("skill_snapshot_refs", "skill_refs", "scheduler_skill_refs"):
        refs = data.get(key, [])
        if not isinstance(refs, (list, tuple)) or any(
            not isinstance(ref, dict) or not isinstance(ref.get("stable_id"), str) for ref in refs
        ):
            raise ValueError("方法引用索引损坏")
        protected.update(ref["stable_id"] for ref in refs)
    allowed = data.get("allowed_skills", [])
    if not isinstance(allowed, (list, tuple)) or any(not isinstance(ref, str) for ref in allowed):
        raise ValueError("方法授权引用损坏")
    protected.update(allowed)
