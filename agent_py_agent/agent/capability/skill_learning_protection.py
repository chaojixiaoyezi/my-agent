# LLM: V2-3 保护名单严禁扫全量线程历史；仅读取未结算任务、活跃子代理、未删除监控及待审提案索引，
#   且全流程共享 RetentionScanBudget (entries/bytes/ms) 上限，截断或不可读时返回 protection_complete=False。
# 模块用途: 为自学维护有界收集活跃引用保护名单，确保耗时与会话历史总量解耦。
from __future__ import annotations

import hashlib
import inspect
import json
import time
from pathlib import Path

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


def _consume_protection_source(agent, source, rows, protected, budget, include):
    """每个来源保留一份有界内容指纹与续点；CPU暂停不撤销已经读到的事实，也不让下一片从头重扫。"""
    if len(rows) > agent._skill_protection_scan_limit:
        return False
    encoded = json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()
    if len(encoded) > budget.max_bytes:
        return False
    signature = hashlib.sha256(encoded).hexdigest()
    scans = dict(getattr(agent, '_skill_protection_source_scans', {}))
    previous = scans.get(source, {})
    cursor = int(previous.get('cursor') or 0) if previous.get('signature') == signature else 0
    refs = set(previous.get('refs') or ()) if previous.get('signature') == signature else set()
    stepped = False
    for index in range(cursor, len(rows)):
        # 墙钟是下一个对象的让出点，单次不可中断读取超时后仍允许一个有界对象前进；数量/字节仍是硬上界。
        if (budget.entries >= budget.max_entries or budget.size >= budget.max_bytes
                or (stepped and time.monotonic() >= budget.deadline)):
            break
        row = rows[index]
        try:
            include(refs, row)
        except (ValueError, TypeError, KeyError):
            scans.pop(source, None)
            agent._skill_protection_source_scans = scans
            protected.update(refs)
            return False
        budget.charge(entries=1, size=_approx_bytes(row))
        cursor = index + 1
        stepped = True
    protected.update(refs)
    scans[source] = {'signature': signature, 'cursor': cursor, 'refs': tuple(sorted(refs))}
    agent._skill_protection_source_scans = scans
    return cursor == len(rows)


def _include_job_refs(protected, job):
    _include_refs(protected, job)
    _include_refs(protected, job.get('payload') or {})


def _include_proposal_refs(protected, proposal):
    if proposal.get('status') == 'pending' and proposal.get('expected_sha256'):
        protected.add('owner:' + proposal['decision']['name'])


def _include_child_refs(protected, child):
    if not task_status_in(child['status'], SUBAGENT_ENDED_STATUSES):
        _include_refs(protected, child['attributes'])


# LLM: 四个 canonical 活跃索引来源共享有界配额与墙钟预算；严禁调用 threads.list_report(limit=0)。
# 函数用途: 汇集未结算任务、监控、待审提案及未结束子代理的保护名单。
def learning_protection_for_agent(agent, *, scan_limit=1000):
    protected: set[str] = set()
    complete = True
    agent._skill_protection_scan_limit = scan_limit if type(scan_limit) is int and scan_limit > 0 else 1000
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
        jobs_complete = _consume_protection_source(agent, 'scheduler', jobs, protected, budget, _include_job_refs)
        complete = complete and jobs_complete
    except Exception:  # noqa: BLE001
        complete = False

    try:
        from .skill_learning_proposals import list_learning_proposals
        from .skill_learning_store import SkillLearningStore

        proposals = list_learning_proposals(
            SkillLearningStore.for_home(agent.home_paths),
            include_overflow=True,
        )
        proposals_complete = _consume_protection_source(
            agent, 'proposals', proposals, protected, budget, _include_proposal_refs,
        )
        complete = complete and proposals_complete
    except Exception:  # noqa: BLE001
        complete = False

    try:
        reader = getattr(agent.subagents, "list_recovery_runs_report", None)
        if callable(reader):
            if _supports_limit_kwarg(reader):
                # 读取上界属于整个有界保护视图，消费配额才属于本片；否则每片截到不同前缀会重置续点。
                report = reader(limit=agent._skill_protection_scan_limit + 1)
            else:
                report = reader()
        else:
            report = agent.subagents.list_runs_report()
        complete = complete and not report.load_errors
        children = [{'status': child.status, 'attributes': child.attributes or {}} for child in report.runs]
        children_complete = _consume_protection_source(
            agent, 'subagents', children, protected, budget, _include_child_refs,
        )
        complete = complete and children_complete
    except Exception:  # noqa: BLE001
        complete = False

    # 预算只决定是否开始下一对象；最后一个原子读取完成后越过墙钟不等于来源读取失败。
    if budget.size >= budget.max_bytes:
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

    from ..runtime_db.repository import RuntimeRepository

    if isinstance(repo, RuntimeRepository):
        return _collect_runtime_task_refs(agent, repo, tasks, protected, budget)

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



def _path_stamp(path):
    try:
        info = Path(path).lstat()
        return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    except FileNotFoundError:
        return None


def _runtime_task_ref_snapshot(repo, tasks, *, limit, max_bytes):
    """同一只读事务取有界当前树事实；旧TaskRun只作候选，不借其created投影推断执行存活。"""
    from ..runtime_db.operations import OP_CLAIMED, OP_EXECUTING, OP_UNKNOWN, exec_lock_scope
    from ..runtime_db.repository import _quiescent_nonterminal_agent_runs

    groups = {}
    total_bytes = 0
    complete = True
    with repo._runtime_connection() as conn:
        conn.execute("BEGIN")
        rows = conn.execute(
            "WITH candidates AS (SELECT * FROM task_runs WHERE closed_at=0 "
            "ORDER BY created_at,task_run_id LIMIT ?) "
            "SELECT tr.task_run_id,tr.task_id,tr.updated_at AS task_updated_at,"
            "substr(tr.metadata_json,1,?) AS task_metadata,t.thread_id,t.conversation_task_id,"
            "ar.agent_run_id,ar.parent_agent_run_id,ar.status,ar.updated_at,ar.current_attempt_id,ar.current_attempt_generation,"
            "aa.agent_run_id AS attempt_agent_run_id,aa.attempt_generation,aa.status AS attempt_status,aa.ended_at,"
            "substr(aa.metadata_json,1,?) AS attempt_metadata,"
            "EXISTS(SELECT 1 FROM resource_locks l WHERE l.canonical_scope=(? || ar.agent_run_id)) AS has_lock,"
            "EXISTS(SELECT 1 FROM tool_operations o WHERE o.attempt_id=ar.current_attempt_id "
            "AND (o.status IN (?,?) OR (o.status=? AND o.handler_started_at>0))) AS unsettled "
            "FROM candidates tr LEFT JOIN tasks t ON t.task_id=tr.task_id "
            "LEFT JOIN agent_runs ar ON ar.task_run_id=tr.task_run_id "
            "LEFT JOIN agent_attempts aa ON aa.attempt_id=ar.current_attempt_id "
            "ORDER BY tr.created_at,tr.task_run_id,ar.created_at,ar.agent_run_id LIMIT ?",
            (limit + 1, max_bytes + 1, max_bytes + 1, exec_lock_scope(""), OP_EXECUTING, OP_UNKNOWN, OP_CLAIMED, limit + 1),
        )
        for index, raw in enumerate(rows):
            row = dict(raw)
            size = _approx_bytes(row)
            total_bytes += size
            if index >= limit or total_bytes > max_bytes:
                complete = False
                break
            groups.setdefault(row['task_run_id'], []).append(row)
        for group in groups.values():
            # 复用运行库原树静止合同；额外要求exact current attempt的退出证明，unknown绝不算静止。
            group[0]['tree_quiescent'] = _quiescent_nonterminal_agent_runs(conn, group) is not None
    storage = getattr(tasks, 'storage', None)
    for group in groups.values():
        row = group[0]
        task_id = str(row.get('conversation_task_id') or '')
        thread_id = str(row.get('thread_id') or '')
        row['task_link_stamp'] = (_path_stamp(storage.task_path(task_id))
                                  if storage is not None and task_id else None)
        row['thread_stamp'] = (_path_stamp(storage.thread_path(thread_id))
                               if storage is not None and thread_id else None)
    signature = hashlib.sha256(json.dumps(groups, sort_keys=True, default=str).encode()).hexdigest()
    return groups, signature, complete


def _collect_runtime_task_refs(agent, repo, tasks, protected, budget):
    """只缓存有界保护投影续点；每次重核canonical树与准确文件指纹，新任务/新attempt立即废弃旧证明。"""
    from ..runtime_db.executor_liveness import executor_exit_reason
    from ..runtime_db.operations import ATTEMPT_STATUS_PENDING
    from ..runtime_db.repository import _QUIESCENT_ATTEMPT_STATUSES

    limit = max(1, min(int(getattr(agent, '_skill_protection_scan_limit', 1000)), 1000))
    groups, signature, source_complete = _runtime_task_ref_snapshot(
        repo, tasks, limit=limit, max_bytes=budget.max_bytes,
    )
    previous = getattr(agent, '_skill_task_protection_scan', {})
    storage = getattr(tasks, 'storage', None)
    active_stamps = dict(previous.get('active_stamps') or {})
    active_unchanged = all(storage is not None and _path_stamp(storage.task_path(task_id)) == stamp
                           for task_id, stamp in active_stamps.items())
    finished_failure = (not previous.get('complete', True)
                        and int(previous.get('cursor') or 0) >= len(groups))
    if previous.get('signature') == signature and active_unchanged and not finished_failure:
        cursor = int(previous.get('cursor') or 0)
        refs = set(previous.get('refs') or ())
        complete = bool(previous.get('complete', True))
    else:
        cursor, refs, complete = 0, set(), True
        active_stamps = {}
    items = list(groups.values())
    initial_cursor = cursor
    for index in range(cursor, len(items)):
        if (budget.entries >= budget.max_entries or budget.size >= budget.max_bytes
                or (index > initial_cursor and time.monotonic() >= budget.deadline)):
            break
        group = items[index]
        first = group[0]
        try:
            metadata = json.loads(first['task_metadata'] or '{}')
            if not isinstance(metadata, dict):
                raise ValueError('task_metadata_not_object')
            _include_refs(refs, metadata)
            ended = bool(first['tree_quiescent'])
            no_captured_execution = True
            for row in group:
                attempt_meta = json.loads(row['attempt_metadata'] or '{}')
                if not isinstance(attempt_meta, dict):
                    raise ValueError('attempt_metadata_not_object')
                _include_refs(refs, attempt_meta)
                exact = bool(row['agent_run_id'] and row['current_attempt_id']
                             and row['attempt_agent_run_id'] == row['agent_run_id']
                             and row['attempt_generation'] == row['current_attempt_generation'])
                no_execution = not row['has_lock'] and not row['unsettled']
                has_executor = any(attempt_meta.get(key) for key in ('executor', 'runner_pid', 'runner_start_time'))
                exited = bool(exact and no_execution and row['attempt_status'] in _QUIESCENT_ATTEMPT_STATUSES
                              and float(row['ended_at'] or 0) > 0
                              and (not has_executor or executor_exit_reason(attempt_meta) in {
                                  'executor_returned_without_result', 'executor_scope_exited', 'executor_process_died'}))
                ended = ended and exited
                pending = bool(exact and no_execution and row['status'] in {'', 'created'}
                               and row['attempt_status'] == ATTEMPT_STATUS_PENDING
                               and float(row['ended_at'] or 0) == 0
                               and attempt_meta.get('lifecycle') == ATTEMPT_STATUS_PENDING
                               and not has_executor)
                # 逻辑任务尚可恢复不等于本轮仍持快照；未启动或确证退出的attempt都不凭缺失投影伪造活跃引用。
                no_captured_execution = no_captured_execution and (exited or pending)
            if not ended:
                task_id = str(first.get('conversation_task_id') or '')
                thread_id = str(first.get('thread_id') or '')
                if task_id and callable(getattr(tasks, 'load_report', None)):
                    link, error = tasks.load_report(task_id)
                    if error or (link is None and not no_captured_execution):
                        complete = False
                    if link is not None:
                        if thread_id and link.thread_id != thread_id:
                            complete = False
                        _include_refs(refs, link.to_dict())
                elif thread_id and callable(getattr(tasks, 'active_report', None)):
                    try:
                        thread = tasks._require_thread(thread_id)
                        active_ids = tuple(thread.active_task_ids)
                        if len(active_ids) + len(active_stamps) > limit:
                            raise ValueError('active_task_link_budget_exceeded')
                        before_stamps = {task_id: _path_stamp(storage.task_path(task_id))
                                         for task_id in active_ids}
                        links, errors = tasks.active_report(thread_id)
                        after_stamps = {task_id: _path_stamp(storage.task_path(task_id))
                                        for task_id in active_ids}
                        if before_stamps != after_stamps or any(link.task_id not in before_stamps for link in links):
                            complete = False
                        else:
                            active_stamps.update(before_stamps)
                        if not _consume_task_links(links, errors, refs, budget, charge_entries=False):
                            complete = False
                    except KeyError:
                        if not no_captured_execution:
                            complete = False
                elif not no_captured_execution:
                    complete = False
        except (OSError, ValueError, TypeError, KeyError):
            complete = False
        budget.charge(entries=len(group), size=_approx_bytes(group))
        cursor = index + 1
    protected.update(refs)
    # 最后处理片只登记待复核；下一片开头的一次快照即可核验完整证明，避免同片双扫耗尽所有后续来源预算。
    verified_complete = cursor == len(items) and cursor == initial_cursor and source_complete and complete
    # 未完成只暴露partial；每次继续都从真实来源指纹重核，不把跨新attempt的旧complete缓存成授权。
    agent._skill_task_protection_scan = {
        'signature': signature, 'cursor': cursor, 'refs': tuple(sorted(refs)), 'complete': complete,
        'active_stamps': active_stamps,
    }
    return verified_complete

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
