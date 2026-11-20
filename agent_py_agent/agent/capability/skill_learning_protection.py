# LLM: V2-3 保护名单严禁扫全量线程历史；仅读取未结算任务、活跃子代理、未删除监控及待审提案索引，
#   且全流程共享 RetentionScanBudget (entries/bytes/ms) 上限，截断或不可读时返回 protection_complete=False。
# 模块用途: 为自学维护有界收集活跃引用保护名单，确保耗时与会话历史总量解耦。
from __future__ import annotations

import hashlib
import inspect
import json
import time
from dataclasses import replace
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


def _signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


class _ProtectionSnapshot(dict):
    """源代次复核供发布事务消费，字典键继续兼容维护入口。"""

    def __init__(self, refs, complete, generation, recheck):
        super().__init__(protected_ids=tuple(sorted(refs)), protection_complete=complete)
        self.generation = generation
        self.recheck = recheck


def _read_sources(agent, store):
    scheduler = getattr(agent, 'scheduler_repository', None)
    subagents = getattr(agent, 'subagents', None)
    persistence = getattr(subagents, 'persistence', None)
    index = getattr(persistence, 'run_index', None)
    tasks = getattr(getattr(agent, 'conversation_store', None), 'tasks', None)
    runtime = getattr(agent, 'runtime_db', None) or getattr(subagents, 'runtime_db', None)

    scheduler_path = getattr(scheduler, 'store_path', None)
    jobs = scheduler.list_jobs(include_deleted=False) if scheduler is not None and scheduler_path is None else None
    if index is not None:
        index_state = persistence.prepare_run_index(tolerate_pending=True)
    child_reader = getattr(subagents, 'list_recovery_runs_report', None) or getattr(subagents, 'list_runs_report', None)

    def children():
        if child_reader is None:
            return [], []
        report = child_reader()
        rows = [{'status': child.status, 'attributes': child.attributes or {}} for child in report.runs]
        return rows, report.load_errors

    child_rows = children() if index is None else None
    if index is not None and (not index_state['complete'] or index_state['pending']):
        child_rows = [], ['subagent_index_incomplete']
    paths = [scheduler_path, getattr(index, 'path', None), getattr(runtime, 'db_path', None)]
    storage = getattr(tasks, 'storage', None)
    paths.extend(getattr(storage, name, None) for name in ('tasks_dir', 'threads_dir'))
    if store is not None:
        paths.extend(store.directory / name for name in ('proposals', 'proposals_overflow', '.proposals-intent.json'))
    files = tuple(Path(path) for path in paths if path is not None)
    wal = tuple(Path(str(path) + '-wal') for path in files if path.suffix in ('.db', '.sqlite', '.sqlite3'))
    task_reader = next((getattr(tasks, name) for name in ('unsettled_report', 'active_report')
                        if _zero_arg_callable(getattr(tasks, name, None))), None)
    task_rows = task_reader() if task_reader is not None else None

    def generation(*, fresh=False):
        return _signature((
            [_path_stamp(path) for path in (*files, *wal)],
            scheduler.list_jobs(include_deleted=False) if fresh and jobs is not None else jobs,
            children() if fresh and index is None else child_rows,
            (_task_listing_signature(task_reader()) if fresh else _task_listing_signature(task_rows))
            if task_rows is not None else None,
        ))

    return generation, scheduler, jobs, persistence, child_rows, task_rows


def _task_listing_signature(report):
    rows, errors = report
    return [row.to_dict() if hasattr(row, 'to_dict') else row for row in rows], errors


def _page_rows(rows, errors, offset, budget, include, refs):
    if errors:
        raise ValueError('protection_source_unreadable')
    initial = offset
    while offset < len(rows):
        if budget.entries >= budget.max_entries or budget.size >= budget.max_bytes:
            break
        if offset != initial and budget.exhausted():
            break
        row = rows[offset]
        size = _approx_bytes(row)
        if budget.size + size > budget.max_bytes:
            break
        include(refs, row)
        budget.charge(entries=1, size=size)
        offset += 1
    return None if offset == len(rows) else offset


def _scheduler_page(agent, scheduler, jobs, after, budget, refs, generation):
    if jobs is not None:
        return _page_rows(*jobs, after, budget, _include_job_refs, refs)
    if scheduler is None:
        return None
    store = scheduler._store_snapshot(readonly=True)['jobs']
    memo = getattr(agent, '_skill_protection_scheduler_keys', None)
    if memo is None or memo[0] != generation:
        memo = (generation, tuple(store))
        agent._skill_protection_scheduler_keys = memo
    keys = memo[1]
    initial = after
    while after < len(keys) and budget.entries < budget.max_entries and budget.size < budget.max_bytes:
        if after != initial and budget.exhausted():
            break
        job, error = scheduler._parse_job(store[keys[after]])
        if error or job is None:
            raise ValueError('scheduler_protection_source_unreadable')
        size = _approx_bytes(job)
        if budget.size + size > budget.max_bytes:
            break
        if job['status'] != 'deleted':
            _include_job_refs(refs, job)
        budget.charge(entries=1, size=size)
        after += 1
    return None if after == len(keys) else after


def _include_task_refs(refs, task):
    if getattr(task, 'status', None) not in THREAD_TASK_LINK_INACTIVE_STATUSES:
        _include_refs(refs, task.to_dict() if hasattr(task, 'to_dict') else dict(task))


def _proposal_refs_page(store, after, budget, refs):
    from .skill_learning_proposals import learning_proposal_page

    if store is None:
        return None
    # 页游标仅在所有行完成核对以后提交。
    rows, errors, cursor = learning_proposal_page(store, after=after, limit=budget.max_entries - budget.entries)
    page_budget = replace(budget)
    page_refs = set()
    if _page_rows(rows, errors, 0, page_budget, _include_proposal_refs, page_refs) is not None:
        # 大页超过字节/墙钟预算时按逐行页推进，避免在同一续点重试同一大页。
        single, errors, cursor = learning_proposal_page(store, after=after, limit=1)
        if _page_rows(single, errors, 0, budget, _include_proposal_refs, refs) is not None:
            return after
    else:
        budget.charge(entries=page_budget.entries - budget.entries, size=page_budget.size - budget.size)
        refs.update(page_refs)
    return cursor


def _child_refs_page(persistence, child_rows, after, budget, refs):
    if persistence is None or child_rows is not None:
        return _page_rows(*(child_rows or ([], [])), after or 0, budget, _include_child_refs, refs)
    ids, cursor = persistence.run_index.recovery_ids(
        after=after or '', limit=budget.max_entries - budget.entries,
    )
    report = persistence.list_runs_by_ids_report(ids)
    children = {child.run_id: child for child in report.runs}
    rows = [{'status': children[run_id].status, 'attributes': children[run_id].attributes or {}}
            for run_id in ids if run_id in children]
    if report.load_errors or len(rows) != len(ids):
        raise ValueError('subagent_protection_source_unreadable')
    remaining = _page_rows(rows, [], 0, budget, _include_child_refs, refs)
    if remaining is not None:
        return ids[remaining - 1] if remaining else after
    return cursor or None


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
    from .skill_learning_store import SkillLearningStore, SkillLearningStoreError

    budget = RetentionScanBudget.default(getattr(agent, "config", None))
    if type(scan_limit) is int and scan_limit > 0:
        budget.max_entries = min(budget.max_entries, scan_limit)
    home = getattr(agent, 'home_paths', None)
    store = SkillLearningStore.for_home(home) if getattr(home, 'owner_skill_learning_dir', None) else None
    protected = set()
    try:
        generation_of, scheduler, jobs, persistence, children, tasks = _read_sources(agent, store)
        generation = generation_of()
        previous = _load_protection_scan(store) if store is not None else getattr(agent, '_skill_protection_scan', None)
        saved = previous if isinstance(previous, dict) and previous.get('generation') == generation else {}
        after = dict(saved.get('after') or {})
        task_progress = dict(after.get('runtime') or {})
        if not _active_stamps_current(agent, task_progress):
            after, task_progress = {}, {}
        # 引用来源共享配额，已结束页不再读其正文。无主机来源时只返回覆盖不完整。
        protected.update(after.get('protected_ids', ()))
        if tasks is None:
            complete_tasks = _collect_unsettled_tasks(agent, protected, budget, progress=task_progress)
            after['runtime'] = task_progress
        else:
            after['tasks'] = _page_rows(*tasks, after.get('tasks', 0), budget, _include_task_refs, protected) \
                if after.get('tasks', 0) is not None else None
            complete_tasks = after['tasks'] is None

        if after.get('scheduler', 0) is not None:
            after['scheduler'] = _scheduler_page(agent, scheduler, jobs, after.get('scheduler', 0), budget,
                                                 protected, generation)
        if budget.entries < budget.max_entries and budget.size < budget.max_bytes:
            if after.get('proposals', {}) is not None:
                after['proposals'] = _proposal_refs_page(store, after.get('proposals', {}), budget, protected)
            if budget.entries < budget.max_entries and budget.size < budget.max_bytes \
                    and after.get('subagents', '') is not None:
                after['subagents'] = _child_refs_page(persistence, children, after.get('subagents', ''), budget, protected)
        complete = (complete_tasks and scheduler is not None and getattr(agent, 'subagents', None) is not None
                    and store is not None and all(key in after and after[key] is None
                                                  for key in ('scheduler', 'proposals', 'subagents')))
        after['protected_ids'] = sorted(protected)
        current = {'generation': generation, 'after': after}

        def recheck():
            return generation_of(fresh=True) == generation and _active_stamps_current(agent, task_progress)

        if not recheck():
            complete = False
        elif current != previous:
            if store is None:
                agent._skill_protection_scan = current
            elif protected or _scan_has_progress(after) or store.registry_path.exists():
                complete = _save_protection_scan(store, previous, current) and complete
        return _ProtectionSnapshot(protected, complete, generation, recheck)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, SkillLearningStoreError):
        return _ProtectionSnapshot(protected, False, '', lambda: False)


def _load_protection_scan(store):
    return store.load_registry().import_operations.get('protection_scan')


def _scan_has_progress(after):
    runtime = after.get('runtime') or {}
    return any(after.get(source) for source in ('tasks', 'scheduler', 'proposals', 'subagents')) or bool(
        runtime.get('cursor') or runtime.get('prefix_refs') or runtime.get('after', [0, ''])[1],
    )


def _save_protection_scan(store, previous, current):
    # 复用旧版登记表能保留的操作元数据槽。仅在原状态锁内对扫描字段做 CAS。
    with store.state_lock():
        registry = store.load_registry()
        if registry.import_operations.get('protection_scan') != previous:
            return False
        store.save_registry(replace(
            registry, import_operations={**registry.import_operations, 'protection_scan': current},
        ))
    return True


def _collect_unsettled_tasks(agent, protected: set[str], budget: RetentionScanBudget, *, progress=None) -> bool:
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
        return _collect_runtime_task_refs(agent, repo, tasks, protected, budget, progress=progress)

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
        return [info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
    except FileNotFoundError:
        return None


def _active_stamps_current(agent, progress):
    tasks = getattr(getattr(agent, 'conversation_store', None), 'tasks', None)
    storage = getattr(tasks, 'storage', None)
    return (
        all(storage is not None and _path_stamp(storage.task_path(task_id)) == stamp
            for task_id, stamp in (progress.get('active_stamps') or {}).items())
        and all(storage is not None and _path_stamp(storage.thread_path(thread_id)) == stamp
                for thread_id, stamp in (progress.get('thread_stamps') or {}).items())
    )


def _runtime_task_ref_snapshot(repo, tasks, *, limit, max_bytes, after=(0, '')):
    """同一只读事务取有界当前树事实；旧TaskRun只作候选，不借其created投影推断执行存活。"""
    from ..runtime_db.operations import OP_CLAIMED, OP_EXECUTING, OP_UNKNOWN, exec_lock_scope
    from ..runtime_db.repository import _quiescent_nonterminal_agent_runs

    groups = {}
    total_bytes = 0
    complete = True
    with repo._runtime_connection() as conn:
        conn.execute("BEGIN")
        rows = conn.execute(
            "WITH candidates AS (SELECT * FROM task_runs WHERE closed_at=0 AND (created_at,task_run_id)>(?,?) "
            "ORDER BY created_at,task_run_id LIMIT ?) "
            "SELECT tr.task_run_id,tr.task_id,tr.created_at AS task_created_at,tr.updated_at AS task_updated_at,"
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
            "ORDER BY tr.created_at,tr.task_run_id,ar.created_at,ar.agent_run_id",
            (*after, limit + 1, max_bytes + 1, max_bytes + 1, exec_lock_scope(""),
             OP_EXECUTING, OP_UNKNOWN, OP_CLAIMED),
        )
        for raw in rows:
            row = dict(raw)
            key = row['task_run_id']
            total_bytes += _approx_bytes(row)
            if total_bytes > max_bytes:
                groups.pop(key, None)
                complete = False
                break
            if key not in groups and len(groups) >= limit:
                complete = False
                break
            groups.setdefault(key, []).append(row)
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


def _collect_runtime_task_refs(agent, repo, tasks, protected, budget, *, progress=None):
    """只缓存有界保护投影续点；每次重核canonical树与准确文件指纹，新任务/新attempt立即废弃旧证明。"""
    from ..runtime_db.executor_liveness import executor_exit_reason
    from ..runtime_db.operations import ATTEMPT_STATUS_PENDING
    from ..runtime_db.repository import _QUIESCENT_ATTEMPT_STATUSES

    previous = getattr(agent, '_skill_task_protection_scan', {}) if progress is None else progress
    db_path = Path(repo.db_path)
    db_generation = _signature((_path_stamp(db_path), _path_stamp(str(db_path) + '-wal')))
    if previous.get('database_generation') != db_generation or not _active_stamps_current(agent, previous):
        previous = {}
    after = previous.get('after') or [0, '']
    limit = budget.max_entries
    groups, signature, source_complete = _runtime_task_ref_snapshot(
        repo, tasks, limit=limit, max_bytes=budget.max_bytes, after=after,
    )
    storage = getattr(tasks, 'storage', None)
    active_stamps = dict(previous.get('active_stamps') or {})
    thread_stamps = dict(previous.get('thread_stamps') or {})
    finished_failure = (not previous.get('complete', True)
                        and int(previous.get('cursor') or 0) >= len(groups))
    if previous.get('signature') == signature and not finished_failure:
        cursor = int(previous.get('cursor') or 0)
        refs = set(previous.get('refs') or ())
        complete = bool(previous.get('complete', True))
    else:
        cursor, refs, complete = 0, set(previous.get('prefix_refs') or ()), True
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
                    if storage is not None:
                        active_stamps[task_id] = first['task_link_stamp']
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
                        before_stamps = {task_id: _path_stamp(storage.task_path(task_id))
                                         for task_id in active_ids}
                        links, errors = tasks.active_report(thread_id)
                        after_stamps = {task_id: _path_stamp(storage.task_path(task_id))
                                        for task_id in active_ids}
                        if before_stamps != after_stamps or any(link.task_id not in before_stamps for link in links):
                            complete = False
                        else:
                            active_stamps.update(before_stamps)
                            if storage is not None:
                                thread_stamps[thread_id] = first['thread_stamp']
                        if not _consume_task_links(links, errors, refs, budget, charge_entries=False):
                            complete = False
                    except KeyError:
                        if not no_captured_execution:
                            complete = False
                elif not no_captured_execution:
                    complete = False
        except (OSError, ValueError, TypeError, KeyError):
            complete = False
        budget.charge(entries=1, size=_approx_bytes(group))
        cursor = index + 1
    protected.update(refs)
    # 最后处理片只登记待复核；下一片开头的一次快照即可核验完整证明，避免同片双扫耗尽所有后续来源预算。
    verified_complete = cursor == len(items) and cursor == initial_cursor and source_complete and complete
    # 未完成只暴露partial；每次继续都从真实来源指纹重核，不把跨新attempt的旧complete缓存成授权。
    prefix = previous.get('prefix_refs') or []
    if groups and cursor == len(items) and complete and not source_complete:
        last = items[-1][0]
        after, cursor, signature = [last['task_created_at'], last['task_run_id']], 0, ''
        prefix = sorted(refs)
    updated = {
        'signature': signature, 'cursor': cursor, 'refs': sorted(refs), 'complete': complete,
        'active_stamps': active_stamps, 'thread_stamps': thread_stamps, 'after': after, 'prefix_refs': prefix,
        'database_generation': db_generation,
    }
    if progress is None:
        agent._skill_task_protection_scan = updated
    else:
        progress.update(updated)
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
