# LLM: V2-3 保护名单严禁扫全量线程历史；仅读取未结算任务、活跃子代理、未删除监控及待审提案索引，
#   且全流程共享 RetentionScanBudget (entries/bytes/ms) 上限，截断或不可读时返回 protection_complete=False。
# 模块用途: 为自学维护有界收集活跃引用保护名单，确保耗时与会话历史总量解耦。
from __future__ import annotations

import bisect
import hashlib
import inspect
import json
import math
import time
from collections.abc import Mapping
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


_PROGRESS_SCHEMA = 'skill-protection-progress.v2'
_SOURCE_NAMES = ('tasks', 'scheduler', 'proposals', 'subagents')
# 窗口签名只取保护判定用到的列：心跳类时间戳每次写库都会变，不能让它重置续点。
_VOLATILE_RUNTIME_COLUMNS = frozenset({'task_updated_at', 'updated_at'})


class _ProtectedIds(tuple):
    """保护名单；随映射展开或单独传参时仍携带原快照，置换前据此复核来源。"""

    snapshot = None


class _ProtectionSnapshot(dict):
    """按来源身份复核供置换事务消费，字典键继续兼容维护入口。"""

    def __init__(self, refs, complete, generation, recheck):
        protected = _ProtectedIds(sorted(refs))
        protected.snapshot = self
        super().__init__(protected_ids=protected, protection_complete=complete)
        self.generation = generation
        self.recheck = recheck


def _digest_rows(hasher, rows, project):
    """已读前缀的引用投影摘要：只看每行的引用视图，运行计数、时间戳等变化不改变来源身份。"""
    for row in rows:
        hasher.update(json.dumps(project(row), sort_keys=True, default=str).encode() + b'\n')
    return hasher


def _included_refs(include):
    def project(row):
        refs = set()
        include(refs, row)
        return sorted(refs)

    return project


def _plain(value):
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


_JOB_REFERENCE_FIELDS = ('skill_snapshot_refs', 'skill_refs', 'scheduler_skill_refs', 'allowed_skills')


def _job_reference_projection(raw):
    """账本只读快照里一条 job 的引用视图：状态与引用字段原样，开工计数、时间等运行字段不进入。"""
    payload = raw.get('payload')
    return [raw.get('status'), _plain({key: raw.get(key) for key in _JOB_REFERENCE_FIELDS}),
            _plain({key: payload.get(key) for key in _JOB_REFERENCE_FIELDS}) if isinstance(payload, Mapping) else None]


def _job_position(job_id, raw):
    """job 在键集中的位置：创建时间（缺失或非法按 0）再按 job_id。job_id 是随机值，新建 job 只按创建时间落在续点之后。"""
    created = raw.get('created_at') if isinstance(raw, Mapping) else None
    if type(created) not in (int, float) or not math.isfinite(created) or created < 0:
        created = 0.0
    return float(created), job_id


def _path_identity(path):
    try:
        info = Path(path).lstat()
    except FileNotFoundError:
        return None
    return [info.st_dev, info.st_ino]


def _rolling_refs(state):
    return set(state.get('done') or ()) | set(state.get('refs') or ())


class _ListingSource:
    """一次返回全量的清单（宿主替身）：按下标续读，已读前缀的引用投影变了才重来，末尾追加直接续读。"""

    def __init__(self, reader, include):
        self.reader, self.include, self.project = reader, include, _included_refs(include)

    def _rows(self):
        rows, errors = self.reader()
        if errors:
            raise ValueError('protection_source_unreadable')
        return list(rows)

    def _resume(self, state, rows):
        offset, hasher = state.get('after'), hashlib.sha256()
        if (type(offset) is int and 0 <= offset <= len(rows)
                and state.get('prefix') == _digest_rows(hasher, rows[:offset], self.project).hexdigest()):
            return offset, hasher
        return None, hashlib.sha256()

    def advance(self, state, budget):
        rows = self._rows()
        offset, hasher = self._resume(state, rows)
        refs = set(state.get('refs') or ()) if offset is not None else set()
        offset = offset or 0
        remaining = _page_rows(rows, [], offset, budget, self.include, refs)
        end = len(rows) if remaining is None else remaining
        _digest_rows(hasher, rows[offset:end], self.project)
        state.update(after=end, prefix=hasher.hexdigest(), refs=sorted(refs))
        return remaining is None

    def refs(self, state):
        return set(state.get('refs') or ())

    def recheck(self, state, protected, budget):
        rows = self._rows()
        offset, _ = self._resume(state, rows)
        if offset is None:
            return False
        appended = set()
        for row in rows[offset:]:
            self.include(appended, row)
        return appended <= protected


class _SchedulerStoreSource:
    """监控账本：按 (created_at, job_id) 键集续读，新建 job 落在续点之后直接续读；账本身份或已读前缀的引用投影变了才重来。"""

    def __init__(self, scheduler):
        self.scheduler = scheduler

    def _read(self):
        store = self.scheduler._store_snapshot(readonly=True)
        jobs = store['jobs']
        keys = sorted(_job_position(job_id, raw) for job_id, raw in jobs.items())
        return _plain([store.get('schema_version'), store.get('owner')]), jobs, keys

    def _resume(self, state, identity, jobs, keys):
        after, hasher = state.get('after'), hashlib.sha256()
        if (state.get('identity') != identity or type(after) is not list or len(after) != 2
                or type(after[0]) not in (int, float) or not math.isfinite(after[0]) or type(after[1]) is not str):
            return None, hasher
        position = bisect.bisect_right(keys, (float(after[0]), after[1]))
        _digest_rows(hasher, (jobs[key[1]] for key in keys[:position]), _job_reference_projection)
        return (position, hasher) if state.get('prefix') == hasher.hexdigest() else (None, hashlib.sha256())

    def _refs(self, raw, refs):
        job, error = self.scheduler._parse_job(raw)
        if error or job is None:
            raise ValueError('scheduler_protection_source_unreadable')
        if job['status'] != 'deleted':
            _include_job_refs(refs, job)
        return job

    def advance(self, state, budget):
        identity, jobs, keys = self._read()
        position, hasher = self._resume(state, identity, jobs, keys)
        refs = set(state.get('refs') or ()) if position is not None else set()
        after = state.get('after') if position is not None else [0, '']
        position = position or 0
        initial = position
        while position < len(keys) and budget.entries < budget.max_entries and budget.size < budget.max_bytes:
            if position != initial and budget.exhausted():
                break
            row_refs = set()
            job = self._refs(jobs[keys[position][1]], row_refs)
            size = _approx_bytes(job)
            if budget.size + size > budget.max_bytes:
                break
            refs.update(row_refs)
            budget.charge(entries=1, size=size)
            _digest_rows(hasher, (jobs[keys[position][1]],), _job_reference_projection)
            after, position = list(keys[position]), position + 1
        state.update(identity=identity, after=after, prefix=hasher.hexdigest(), refs=sorted(refs))
        return position == len(keys)

    def refs(self, state):
        return set(state.get('refs') or ())

    def recheck(self, state, protected, budget):
        identity, jobs, keys = self._read()
        position, _ = self._resume(state, identity, jobs, keys)
        if position is None:
            return False
        appended = set()
        for key in keys[position:]:
            self._refs(jobs[key[1]], appended)
        return appended <= protected


class _RollingSource:
    """目录与恢复索引不保证新条目落在续点之后：整轮读完后从头再滚一轮，上一整轮的引用在新一轮完成前继续有效。

    复核时在新预算内从头完整读一轮，新出现的引用必须已在保护名单内。
    """

    def __init__(self, identity, page, start, prepare=None):
        self.identity, self.page, self.start, self.prepare = identity, page, start, prepare

    def advance(self, state, budget):
        if self.prepare is not None:
            self.prepare()
        identity = self.identity()
        if state.get('identity') != identity:
            state.clear()
        refs = set(state.get('refs') or ())
        cursor = self.page(state.get('after') or self.start, budget, refs)
        if cursor is None:
            state.update(done=sorted(refs), refs=[], after=self.start)
        else:
            state.update(after=cursor, refs=sorted(refs))
        state['identity'] = identity
        return state.get('done') is not None

    def refs(self, state):
        return _rolling_refs(state)

    def recheck(self, state, protected, budget):
        if self.prepare is not None:
            self.prepare()
        if state.get('identity') != self.identity():
            return False
        refs, cursor = set(), self.start
        while True:
            before = cursor
            cursor = self.page(cursor, budget, refs)
            if cursor is None:
                return refs <= protected
            if cursor == before or budget.exhausted():
                return False


class _RuntimeSource:
    """运行库：库文件身份与 schema 代次决定重来；任务运行按 (created_at, task_run_id) 键集续读。"""

    def __init__(self, agent, repo, tasks):
        self.agent, self.repo, self.tasks = agent, repo, tasks

    def advance(self, state, budget):
        return _collect_runtime_task_refs(self.agent, self.repo, self.tasks, set(), budget, progress=state)

    def refs(self, state):
        return set(state.get('refs') or ())

    def recheck(self, state, protected, budget):
        # 证明跨了多个窗口时，更早窗口里已读任务运行的变化（新 attempt、元数据新增引用）不在最后窗口的逐组核对里，
        # 而一片预算读不完全部窗口：此时按证明过期处理，只会少置换，不会多置换。
        if state.get('after') not in (None, [0, '']):
            return False
        return _runtime_window_current(self.agent, self.repo, self.tasks, state)


class _UnpagedTaskSource:
    """无运行库与可分页清单的旧宿主：每片在预算内重读，复核时同样重读。"""

    def __init__(self, agent):
        self.agent = agent

    def advance(self, state, budget):
        refs = set()
        complete = _collect_unsettled_tasks(self.agent, refs, budget)
        state.clear()
        state['refs'] = sorted(refs)
        return complete

    def refs(self, state):
        return set(state.get('refs') or ())

    def recheck(self, state, protected, budget):
        refs = set()
        return _collect_unsettled_tasks(self.agent, refs, budget) and refs <= protected


def _protection_sources(agent, store):
    """每个来源绑定自己的身份与续点；缺失的来源为 None，使保护证明不完整。"""
    from ..runtime_db.repository import RuntimeRepository

    subagents = getattr(agent, 'subagents', None)
    persistence = getattr(subagents, 'persistence', None)
    index = getattr(persistence, 'run_index', None)
    tasks = getattr(getattr(agent, 'conversation_store', None), 'tasks', None)
    runtime = getattr(agent, 'runtime_db', None) or getattr(subagents, 'runtime_db', None)
    scheduler = getattr(agent, 'scheduler_repository', None)

    task_reader = next((getattr(tasks, name) for name in ('unsettled_report', 'active_report')
                        if _zero_arg_callable(getattr(tasks, name, None))), None)
    if task_reader is not None:
        task_source = _ListingSource(task_reader, _include_task_refs)
    elif isinstance(runtime, RuntimeRepository):
        task_source = _RuntimeSource(agent, runtime, tasks)
    else:
        task_source = _UnpagedTaskSource(agent)

    if scheduler is None:
        scheduler_source = None
    elif getattr(scheduler, 'store_path', None) is None:
        scheduler_source = _ListingSource(lambda: scheduler.list_jobs(include_deleted=False), _include_job_refs)
    else:
        scheduler_source = _SchedulerStoreSource(scheduler)

    proposal_source = None if store is None else _RollingSource(
        lambda: [_path_identity(store.directory / name) for name in ('proposals', 'proposals_overflow')],
        lambda after, budget, refs: _proposal_refs_page(store, after, budget, refs), {},
    )

    if subagents is None:
        child_source = None
    elif index is not None:
        def prepare():
            state = persistence.prepare_run_index(tolerate_pending=True)
            if not state['complete'] or state['pending']:
                raise ValueError('subagent_index_incomplete')

        child_source = _RollingSource(
            lambda: _path_identity(index.path),
            lambda after, budget, refs: _child_refs_page(persistence, None, after, budget, refs), '', prepare,
        )
    else:
        reader = getattr(subagents, 'list_recovery_runs_report', None) or getattr(subagents, 'list_runs_report', None)

        def children():
            if reader is None:
                return [], []
            report = reader()
            return [{'status': child.status, 'attributes': child.attributes or {}} for child in report.runs], \
                report.load_errors

        child_source = _ListingSource(children, _include_child_refs)
    return {'tasks': task_source, 'scheduler': scheduler_source, 'proposals': proposal_source,
            'subagents': child_source}


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
#   每个来源各自绑定库身份/代次与键集续点：普通写入（WAL、运行计数）不重置进度，只有身份变了的来源重来。
# 函数用途: 汇集未结算任务、监控、待审提案及未结束子代理的保护名单。
def learning_protection_for_agent(agent, *, scan_limit=1000):
    from .skill_learning_store import SkillLearningStore, SkillLearningStoreError

    def slice_budget():
        budget = RetentionScanBudget.default(getattr(agent, "config", None))
        if type(scan_limit) is int and scan_limit > 0:
            budget.max_entries = min(budget.max_entries, scan_limit)
        return budget

    failures = (OSError, ValueError, TypeError, KeyError, AttributeError, SkillLearningStoreError)
    budget = slice_budget()
    home = getattr(agent, 'home_paths', None)
    try:
        store = SkillLearningStore.for_home(home) if getattr(home, 'owner_skill_learning_dir', None) else None
        sources = _protection_sources(agent, store)
        previous = _load_protection_scan(store) if store is not None else getattr(agent, '_skill_protection_scan', None)
    except failures:
        return _ProtectionSnapshot((), False, '', lambda: False)
    saved = previous.get('sources') if isinstance(previous, dict) and previous.get('schema') == _PROGRESS_SCHEMA else None
    states = {name: dict(saved[name]) if isinstance(saved, dict) and isinstance(saved.get(name), dict) else {}
              for name in _SOURCE_NAMES}
    protected, complete = set(), True
    # 引用来源共享配额，已结束页不再读其正文。无主机来源时只返回覆盖不完整。
    for name in _SOURCE_NAMES:
        source, state = sources[name], states[name]
        if source is None:
            complete = False
            continue
        if budget.entries >= budget.max_entries or budget.size >= budget.max_bytes:
            complete = False  # 本片配额已用尽：该来源保留原续点，下一片接着读。
        else:
            kept = dict(state)
            try:
                complete = source.advance(state, budget) and complete
            except failures:
                # 单个来源不可读只让本片不完整，不丢它已提交的进度，也不影响其它来源。
                state.clear()
                state.update(kept)
                complete = False
        protected.update(source.refs(state))
    current = {'schema': _PROGRESS_SCHEMA, 'sources': states}
    generation = _signature({name: states[name].get('identity') for name in _SOURCE_NAMES})
    proven = frozenset(protected)

    def recheck():
        # 置换前复核：各来源身份未变，且自快照以来新出现的引用都已在保护名单内；任何读取失败都按证明过期处理。
        fresh = slice_budget()
        try:
            return all(sources[name] is not None and sources[name].recheck(states[name], proven, fresh)
                       for name in _SOURCE_NAMES)
        except Exception:  # noqa: BLE001 - 复核不可完成即不可置换
            return False

    try:
        if current != previous:
            if store is None:
                agent._skill_protection_scan = current
            elif protected or _scan_has_progress(states) or store.registry_path.exists():
                complete = _save_protection_scan(store, previous, current) and complete
    except failures:
        complete = False
    return _ProtectionSnapshot(protected, complete, generation, recheck)


def _load_protection_scan(store):
    return store.load_registry().protection_scan


def _scan_has_progress(states):
    return any(state.get('after') not in (None, 0, '', {}, [0, '']) or state.get('cursor') or state.get('prefix_refs')
               or state.get('refs') for state in states.values())


def _save_protection_scan(store, previous, current):
    # 仅在原状态锁内对保护游标做 CAS。
    with store.state_lock():
        registry = store.load_registry()
        if registry.protection_scan != previous:
            return False
        store.save_registry(replace(registry, protection_scan=current))
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
    signatures = [_signature([{key: value for key, value in row.items() if key not in _VOLATILE_RUNTIME_COLUMNS}
                              for row in group]) for group in groups.values()]
    return groups, signatures, complete


def _runtime_identity(repo):
    """库文件身份加 schema 代次：重建或换库才变；WAL 大小、心跳与新行都不改变它。"""
    from ..runtime_db.schema import runtime_db_identity

    identity = runtime_db_identity(Path(repo.db_path))
    with repo._runtime_connection() as conn:
        present = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'").fetchone()
        row = conn.execute("SELECT value FROM metadata WHERE key='schema_generation'").fetchone() if present else None
    return None if identity is None else [*identity, None if row is None else row[0]]


def _runtime_window_current(agent, repo, tasks, state):
    """复核已完成的运行库证明：身份、在途链接文件与最后窗口逐组一致，窗口之后也没有新任务运行。"""
    if (state.get('identity') != _runtime_identity(repo) or not _active_stamps_current(agent, state)
            or type(state.get('limit')) is not int or type(state.get('max_bytes')) is not int):
        return False
    _, signatures, complete = _runtime_task_ref_snapshot(
        repo, tasks, limit=state['limit'], max_bytes=state['max_bytes'], after=state.get('after') or [0, ''],
    )
    return complete and state.get('complete') and signatures == state.get('signatures') \
        and state.get('cursor') == len(signatures)


def _collect_runtime_task_refs(agent, repo, tasks, protected, budget, *, progress=None):
    """只缓存有界保护投影续点；每次重核canonical树与准确文件指纹，新任务/新attempt立即废弃旧证明。"""
    from ..runtime_db.executor_liveness import executor_exit_reason
    from ..runtime_db.operations import ATTEMPT_STATUS_PENDING
    from ..runtime_db.repository import _QUIESCENT_ATTEMPT_STATUSES

    previous = getattr(agent, '_skill_task_protection_scan', {}) if progress is None else progress
    identity = _runtime_identity(repo)
    if previous.get('identity') != identity or not _active_stamps_current(agent, previous):
        previous = {}
    after = previous.get('after') or [0, '']
    limit = budget.max_entries
    groups, signatures, source_complete = _runtime_task_ref_snapshot(
        repo, tasks, limit=limit, max_bytes=budget.max_bytes, after=after,
    )
    storage = getattr(tasks, 'storage', None)
    active_stamps = dict(previous.get('active_stamps') or {})
    thread_stamps = dict(previous.get('thread_stamps') or {})
    cursor = previous.get('cursor') if type(previous.get('cursor')) is int else 0
    processed = previous.get('signatures')
    finished_failure = not previous.get('complete', True) and cursor >= len(groups)
    if (not finished_failure and isinstance(processed, list) and cursor <= min(len(processed), len(signatures))
            and processed[:cursor] == signatures[:cursor]):
        # 已处理的组逐组未变：窗口内新追加的任务运行从续点接着处理，不重读整窗。
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
    # 最后窗口处理完后再取一次快照核验：本片处理过的组逐组未变才算完整证明，期间的追加或改动留给下一片续处理。
    verified_complete = cursor == len(items) and source_complete and complete
    if verified_complete and cursor != initial_cursor:
        _, confirmed, confirmed_complete = _runtime_task_ref_snapshot(
            repo, tasks, limit=limit, max_bytes=budget.max_bytes, after=after,
        )
        verified_complete = confirmed_complete and confirmed == signatures
    # 未完成只暴露partial；每次继续都从真实来源指纹重核，不把跨新attempt的旧complete缓存成授权。
    prefix = previous.get('prefix_refs') or []
    if groups and cursor == len(items) and complete and not source_complete:
        last = items[-1][0]
        after, cursor, signatures = [last['task_created_at'], last['task_run_id']], 0, []
        prefix = sorted(refs)
    updated = {
        'signatures': signatures, 'cursor': cursor, 'refs': sorted(refs), 'complete': complete,
        'active_stamps': active_stamps, 'thread_stamps': thread_stamps, 'after': after, 'prefix_refs': prefix,
        'identity': identity, 'limit': limit, 'max_bytes': budget.max_bytes,
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
