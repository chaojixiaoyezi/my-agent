"""显式管理员对账：只组合原执行账、索引及投影服务，不启动模型或推断未知副作用。"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import stat
import time
from contextlib import contextmanager
from pathlib import Path

from ..runtime_db.operations import AGENT_RUN_TERMINAL_STATUSES
from ..settings.defaults import default_config_int
from ..user_space.storage_lanes import StorageLaneBudget


@contextmanager
def _snapshot(db):
    """复用原生只读 SQLite；只容 WAL/SHM 协调，固定原业务文件代次。"""
    db = Path(db)
    paths = [Path(str(db) + suffix) for suffix in ('', '-wal', '-shm', '-journal')]
    def inventory():
        result = {}
        for path in paths:
            try:
                info = path.lstat()
            except FileNotFoundError:
                if path == db:
                    raise
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('runtime authority is not a single regular file')
            result[path.name] = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        return result
    before = inventory()
    from ..io.readonly_sqlite import readonly_sqlite_connection
    with readonly_sqlite_connection(db, blocking=False, require_private_directory=False) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        yield conn
        # SHM 的原生读标记不是业务代次；DB/WAL 内容变化需要新预览。
        after = inventory()
        if any(after.get(key) != value for key, value in before.items() if not key.endswith('-shm')):
            raise ValueError('runtime authority changed during preview')


def canonical_gateway_terminal_reason(paths, root_id):
    """保留原 Gateway receipt 与分桶终态查询；不从文件名独自认定终态。"""
    from ..gateway_parts.io import read_json_file_report
    from ..gateway_parts.owner_retention import gateway_terminal_exists
    if not _segment(root_id):
        return None
    path = paths.terminal / (root_id + '.json')
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            return None
        report = read_json_file_report(path, context='runtime_reconciliation.terminal')
        row = next((v for k, v in vars(report).items() if k not in {'load_error', 'lock_busy'}), None) if report.load_error is None else None
        if (not isinstance(row, dict) or row.get('schema_version') != 'gateway_terminal_request.v1'
                or row.get('id') != root_id or row.get('status') not in {'done', 'completed', 'interrupted', 'stopped', 'cancelled', 'failed'}):
            return None
        return 'parent_turn_interrupted' if row['status'] in {'interrupted', 'stopped'} else 'parent_turn_ended'
    except FileNotFoundError:
        return 'parent_turn_ended' if gateway_terminal_exists(paths, root_id + '.json') else None


def _segment(value):
    return isinstance(value, str) and bool(value) and value not in {'.', '..'} and not any(c in value for c in ('/', '\\', '\0'))


def _scheduler_effect_readonly(repository, run_id):
    """复用 effect_run 的解析/选择，不调用原 history.read 的建锁路径。"""
    from copy import copy

    from ..scheduler.history_index import _snapshot as history_snapshot
    index = repository._history_index
    errors = []
    class ReadOnlyHistory:
        def read(self, *, run_id):
            if not index.source.exists():
                return [], []
            with _snapshot(index.path) as conn:
                before = history_snapshot(index.source)
                state = index._state(conn)
                if (state.get('owner') != index.owner or any(state.get(key) != before[key] for key in ('stamp', 'head', 'tail'))
                        or state.get('pending') or state.get('skipping') or state.get('errors')
                        or state.get('cursor', 0) < (before['stamp'][2] if before['stamp'] else 0)):
                    errors.append('SCHEDULER_HISTORY_INDEX_INCOMPLETE')
                    return [], errors
                rows = conn.execute('SELECT payload FROM history_effects WHERE generation=? AND run_id=?',
                    (state['generation'], run_id)).fetchall()
                if history_snapshot(index.source) != before:
                    raise ValueError('scheduler history changed during proof')
                return [json.loads(row[0]) for row in rows], []
    reader = copy(repository)
    reader._history_index = ReadOnlyHistory()
    result = reader.effect_run(run_id)
    if errors:
        raise ValueError('scheduler history proof incomplete')
    return result


def inspect_parent_terminal(agent, root_id: str) -> dict[str, object]:
    """精确 root、owner、持久任务和定时执行证明；缺失不是安全终态。"""
    owner = str(getattr(getattr(agent, 'home_paths', None), 'owner_id', '') or '')
    result = dict(owner_id=owner, root_id=root_id, eligible=False, terminal_reason=None,
                  reason_code='AUTHORITY_MISSING', proof_source='', observed_identity={})
    repo = getattr(getattr(agent, 'subagents', None), 'runtime_db', None)
    if not owner or not _segment(root_id):
        return result
    try:
        if repo is not None and Path(repo.db_path).is_file():
            with _snapshot(repo.db_path) as conn:
                row = conn.execute('SELECT ar.*,tr.task_id,t.owner_id,t.thread_id FROM agent_runs ar '
                    'JOIN task_runs tr USING(task_run_id) JOIN tasks t ON t.task_id=tr.task_id '
                    'WHERE ar.run_id=? LIMIT 2', (root_id,)).fetchall()
                if len(row) > 1:
                    return {**result, 'reason_code': 'PROOF_INCOMPLETE'}
                row = row[0] if row else None
                if row is not None:
                    result['observed_identity'] = {key: row[key] for key in ('agent_run_id', 'task_run_id', 'task_id', 'current_attempt_id', 'current_attempt_generation')}
                    result['proof_source'] = 'runtime_db'
                    if row['owner_id'] != owner:
                        return {**result, 'reason_code': 'OWNER_MISMATCH'}
                    if row['parent_agent_run_id']:
                        return {**result, 'reason_code': 'PARENT_MISMATCH'}
                    if row['status'] == 'unknown':
                        return {**result, 'reason_code': 'ATTEMPT_UNKNOWN'}
                    if row['status'] not in AGENT_RUN_TERMINAL_STATUSES:
                        return {**result, 'reason_code': 'ROOT_ACTIVE'}
                    unknown = conn.execute("SELECT 1 FROM agent_attempts WHERE agent_run_id=? AND status='unknown' LIMIT 1", (row['agent_run_id'],)).fetchone()
                    if unknown is not None or conn.execute("SELECT 1 FROM tool_operations WHERE agent_run_id=? AND status='UNKNOWN' LIMIT 1", (row['agent_run_id'],)).fetchone() is not None:
                        return {**result, 'reason_code': 'ATTEMPT_UNKNOWN'}
                    current = conn.execute('SELECT * FROM agent_attempts WHERE attempt_id=?', (row['current_attempt_id'],)).fetchone()
                    if current is not None and (current['agent_run_id'] != row['agent_run_id'] or current['attempt_generation'] != row['current_attempt_generation']):
                        return {**result, 'reason_code': 'PROOF_INCOMPLETE'}
                    if current is not None and current['status'] == 'unknown':
                        return {**result, 'reason_code': 'ATTEMPT_UNKNOWN'}
                    if current is not None and current['status'] not in {'done', 'failed', 'cancelled', 'recovered'}:
                        return {**result, 'reason_code': 'CURRENT_ATTEMPT_ACTIVE'}
                    if row['current_attempt_id'] and current is None:
                        return {**result, 'reason_code': 'PROOF_INCOMPLETE'}
                    link, error = agent.conversation_store.tasks.load_report(row['task_id'])
                    if error and error.get('error_type') != 'FileNotFoundError':
                        return {**result, 'reason_code': 'PROOF_INCOMPLETE'}
                    if link is not None:
                        from .task_state import conversation_task_link_is_terminal
                        if link.thread_id != row['thread_id']:
                            return {**result, 'reason_code': 'PARENT_MISMATCH'}
                        if not conversation_task_link_is_terminal(link.status):
                            return {**result, 'reason_code': 'DURABLE_TASK_ACTIVE'}
        # 定时运行必须精确查 effect_run；无记录不能借 srun 前缀猜已结束。
        scheduler = getattr(agent, 'scheduler_repository', None)
        scheduled = _scheduler_effect_readonly(scheduler, root_id) if scheduler is not None else None
        if scheduled is not None:
            expected = {'provider': agent.home_paths.owner_provider, 'kind': agent.home_paths.owner_kind, 'id': owner}
            if scheduled.get('owner') != expected:
                return {**result, 'reason_code': 'OWNER_MISMATCH'}
            if scheduled.get('status') not in {'done', 'failed', 'cancelled', 'skipped'}:
                return {**result, 'reason_code': 'SCHEDULER_LEASE_ACTIVE', 'proof_source': 'scheduler.effect_run'}
            if not result['observed_identity']:
                return {**result, 'reason_code': 'PROOF_INCOMPLETE', 'proof_source': 'scheduler.effect_run'}
            result['proof_source'] = 'runtime_db+scheduler.effect_run'
        if result['observed_identity']:
            return {**result, 'eligible': True, 'terminal_reason': 'parent_turn_ended', 'reason_code': 'PARENT_TERMINAL_PROVEN'}
        from ..gateway_parts.paths import gateway_paths
        reason = canonical_gateway_terminal_reason(gateway_paths(agent), root_id)
        if reason:
            # 兼容 Gateway 原终态回执；当前持久 task 仍不得被回合结束覆盖。
            link, error = agent.conversation_store.tasks.load_report(root_id)
            if error and error.get('error_type') != 'FileNotFoundError':
                return {**result, 'reason_code': 'PROOF_INCOMPLETE'}
            if link is not None:
                from .task_state import conversation_task_link_is_terminal
                if not conversation_task_link_is_terminal(link.status):
                    return {**result, 'reason_code': 'DURABLE_TASK_ACTIVE'}
            return {**result, 'eligible': True, 'terminal_reason': reason, 'reason_code': 'PARENT_TERMINAL_PROVEN', 'proof_source': 'gateway_receipt'}
    except (OSError, ValueError, sqlite3.Error, AttributeError, KeyError, TypeError) as exc:
        return {**result, 'reason_code': 'PROOF_INCOMPLETE', 'error_type': type(exc).__name__}
    return result


def canonical_parent_terminal_reason(agent, root_id: str) -> str | None:
    proof = inspect_parent_terminal(agent, root_id)
    return proof['terminal_reason'] if proof['eligible'] else None


def reconcile_capability_page(agent, *, apply=False, after='', limit=100, run_ids=(), commit_transition=None, budget=None, older_than_seconds=None):
    """一页 canonical 申请，冻结准确申请集合，在原创建锁内重核全部父级证明。"""
    from ..subagents.capability_request_expiry import (
        close_existing_capability_requests,
        existing_capability_closeout_confirmed,
    )
    from ..subagents.model_capabilities import capability_request_counts_as_open
    from ..subagents.models import task_has_ended_status
    from ..subagents.services.persistence.run_index import SubagentRunIndex
    from ..subagents.services.persistence.service import load_subagent_task
    subagents = getattr(agent, 'subagents', None)
    if subagents is None:
        return {'items': [], 'next_after': '', 'errors': [{'reason_code': 'AUTHORITY_MISSING'}]}
    items, errors = [], []
    if run_ids:
        ids = sorted(set(run_ids))
        ids = [rid for rid in ids if rid > after]
        more = len(ids) > limit
        ids = ids[:limit]
        next_after = ids[-1] if more else ''
    else:
        try:
            ids, next_after = SubagentRunIndex(subagents.workspace, read_only=True).candidate_ids(
                statuses=('BLOCKED',), include_open_capabilities=True, after=after, limit=limit)
        except (OSError, ValueError, sqlite3.Error, RuntimeError) as exc:
            return {'items': [], 'next_after': after, 'errors': [{'reason_code': 'CAPABILITY_INDEX_UNAVAILABLE', 'error_type': type(exc).__name__}]}
    for visited, rid in enumerate(ids):
        if budget is not None and budget.exhausted(visited):
            next_after = ids[visited - 1] if visited else after
            break
        item = {'kind': 'capability', 'run_id': rid, 'repaired': False, 'eligible': False, 'reason_code': 'PARENT_TERMINAL_UNPROVEN'}
        try:
            run = load_subagent_task(subagents.workspace, rid)
            if older_than_seconds is not None and time.time() - max(run.created_at, run.updated_at) < older_than_seconds:
                items.append({**item, 'reason_code': 'OLDER_THAN_FILTER'})
                continue
            proof = inspect_parent_terminal(agent, run.root_id)
            item.update(parent_proof=proof, root_id=run.root_id, parent_id=run.parent_id)
            selected = tuple(req.id for req in run.capability_requests if capability_request_counts_as_open(req.status))
            def eligible(current):
                if current.owner != agent.home_paths.owner_id or current.status != 'BLOCKED' or current.failure_type != 'capability_request' or current.runner_active_attempt_id or not selected:
                    return False
                if current.parent_id != current.root_id:
                    parent = load_subagent_task(subagents.workspace, current.parent_id)
                    if parent.owner != current.owner or parent.root_id != current.root_id or not task_has_ended_status(parent):
                        return False
                return inspect_parent_terminal(agent, current.root_id)['eligible'] is True
            if eligible(run):
                item.update(eligible=True, reason_code='PARENT_TERMINAL_PROVEN')
                if apply:
                    def commit():
                        task_id = proof['observed_identity'].get('task_id') or run.root_id
                        with subagents.creation_guard(), agent.conversation_store.tasks.transition_guard(task_id):
                            current = load_subagent_task(subagents.workspace, rid)
                            # 精确 revision/attempt/申请代次变化一律留待新预览。
                            if current != run or not eligible(current):
                                return False
                            close_existing_capability_requests(subagents.workspace, run.root_id, proof['terminal_reason'], {rid: selected})
                            return existing_capability_closeout_confirmed(subagents.workspace, run.root_id, {rid: selected})
                    item['repaired'] = bool(commit() if commit_transition is None else commit_transition(commit))
                    if item['repaired']:
                        _settle_capability_runtime(agent, rid, item, commit_transition)
                    if not item['repaired']:
                        item['reason_code'] = 'SOURCE_CHANGED'
            else:
                item['reason_code'] = 'OWNER_MISMATCH' if run.owner != agent.home_paths.owner_id else proof['reason_code'] if not proof['eligible'] else 'PARENT_MISMATCH'
        except (OSError, ValueError, RuntimeError, AttributeError, TypeError) as exc:
            item.update(reason_code='PROOF_INCOMPLETE', error_type=type(exc).__name__)
            if apply:
                item['effect_outcome'] = 'unknown'
        items.append(item)
    return {'items': items, 'next_after': next_after, 'errors': errors}


def _settle_capability_runtime(agent, run_id, item, commit_transition):
    """元数据关闭后仅结算已终结的精确 attempt；created/running/UNKNOWN 不等于未执行。"""
    repo = getattr(agent.subagents, 'runtime_db', None)
    if repo is None:
        item['runtime_reason_code'] = 'AUTHORITY_MISSING'
        return
    with _snapshot(repo.db_path) as conn:
        row = conn.execute('SELECT ar.*,aa.status AS attempt_status FROM agent_runs ar '
            'LEFT JOIN agent_attempts aa ON aa.attempt_id=ar.current_attempt_id WHERE ar.run_id=?', (run_id,)).fetchone()
    if row is None or row['attempt_status'] not in {'done', 'failed', 'cancelled', 'recovered'}:
        item['runtime_reason_code'] = 'ATTEMPT_UNKNOWN_OR_ACTIVE'
        return
    def settle():
        return repo.settle_agent_run(agent_run_id=row['agent_run_id'], status='cancelled',
            attempt_id=row['current_attempt_id'], expected_attempt_status=row['attempt_status'],
            require_no_tool_operations=True, payload={'runtime_source': 'capability_parent_reconciliation'})
    outcome = settle() if commit_transition is None else commit_transition(settle)
    item['runtime_closeout'] = outcome
    if outcome.get('settled'):
        from .task_run_closeout import reconcile_task_run_closeout
        item['task_run_closeout'] = reconcile_task_run_closeout(repo, agent.conversation_store,
            task_run_id=row['task_run_id'], apply=True, commit_transition=commit_transition)


def _readonly_repository(path):
    from ..runtime_db.repository import RuntimeRepository
    return RuntimeRepository(path, read_only=True)


def reconcile_owner_runtime(agent, *, apply: bool = False, cursor: str = '', limit: int | None = None,
                            older_than_seconds: float | None = None, run_ids: tuple[str, ...] = (), commit_transition=None) -> dict[str, object]:
    """每次只处理一个领域页；续点绑定 owner、过滤和源身份，不写第二份进度账。"""
    config = getattr(agent, 'config', None)
    owner = str(agent.home_paths.owner_id)
    count = getattr(config, 'retention_scan_slice_entries', default_config_int('retention_scan_slice_entries')) if limit is None else limit
    if type(count) is not int or count <= 0:
        raise ValueError('limit 必须为正整数；请使用现有 retention_scan_slice_entries 的有限页预算')
    if older_than_seconds is not None and (type(older_than_seconds) not in {int, float} or not math.isfinite(older_than_seconds) or older_than_seconds < 0):
        raise ValueError('older_than_seconds 必须是非负有限数；年龄不构成终态证明')
    configured_count = getattr(config, 'retention_scan_slice_entries', default_config_int('retention_scan_slice_entries'))
    if type(configured_count) is int and configured_count > 0:
        count = min(count, configured_count)
    if len(run_ids) > count:
        raise ValueError('run_ids 超出本页 limit；请缩小准确编号集合')
    if any(not _segment(rid) for rid in run_ids):
        raise ValueError('run_ids 必须为同 owner 的不含路径分隔符编号')
    repo_path = getattr(getattr(getattr(agent, 'subagents', None), 'runtime_db', None), 'db_path', None)
    try:
        generation = (Path(repo_path).stat().st_dev, Path(repo_path).stat().st_ino) if repo_path else None
    except FileNotFoundError:
        generation = None
    signature = hashlib.sha256(json.dumps([str(agent.home_paths.owner_home_dir), owner, generation, sorted(set(run_ids)), older_than_seconds]).encode()).hexdigest()
    state = {'phase': 'capabilities' if run_ids else 'task_runs', 'after': ''}
    if cursor:
        if len(cursor) > 8192:
            raise ValueError('cursor 超出上限，请从空 cursor 重新预览')
        token = json.loads(cursor)
        if not isinstance(token, dict):
            raise ValueError('cursor 必须是上一页原样返回的 JSON 对象；请从空 cursor 重新预览')
        if token.get('owner') != owner or token.get('signature') != signature or token.get('phase') not in {'capabilities', 'task_runs', 'indexes', 'projections'}:
            raise ValueError('cursor owner/过滤不匹配，请对当前 owner 使用空 cursor 重新预览')
        state = token
    slice_ms = float(getattr(config, 'retention_scan_slice_ms', 50))
    budget = StorageLaneBudget(count, getattr(config, 'retention_scan_slice_bytes', 8 * 1024 * 1024) or None,
        time.monotonic() + slice_ms / 1000 if slice_ms else None)
    result = dict(owner_id=owner, apply=apply, items=[], repaired=0, protected=0, unknown=0, errors=[], next_cursor='', partial=False, effect_outcome='not_started')
    try:
        phase, after = state['phase'], state.get('after', '')
        if phase == 'capabilities':
            page = reconcile_capability_page(agent, apply=apply, after=after, limit=count, run_ids=run_ids, commit_transition=commit_transition, budget=budget, older_than_seconds=older_than_seconds)
            result.update(items=page['items'], errors=page['errors'])
            next_state = {'phase': phase, 'after': page['next_after']} if page['next_after'] else ({'phase': 'projections', 'after': ''} if run_ids else {'phase': 'indexes', 'after': ''})
        elif phase == 'task_runs':
            from .task_run_closeout import reconcile_task_run_closeout
            repo = getattr(getattr(agent, 'subagents', None), 'runtime_db', None)
            if repo is None:
                raise ValueError('runtime authority unavailable')
            reader = _readonly_repository(repo.db_path)
            rows = reader.open_task_runs_page(after_created_at=float(state.get('created_at', 0)), after_task_run_id=after, limit=count)
            # W7 的页合同同时支持带 next_cursor 的结构化回执。
            if isinstance(rows, dict):
                rows = rows.get('items', rows.get('rows', []))
            visited = []
            for row in rows:
                if budget.exhausted(len(visited)):
                    break
                if older_than_seconds is not None and time.time() - max(row['created_at'], row['updated_at']) < older_than_seconds:
                    result['items'].append({'kind': 'task_run', 'task_run_id': row['task_run_id'], 'eligible': False, 'reason_code': 'OLDER_THAN_FILTER'})
                    visited.append(row)
                    continue
                task = reader.get_task(row['task_id'])
                if task is None or task['owner_id'] != owner:
                    result['items'].append({'kind': 'task_run', 'task_run_id': row['task_run_id'], 'eligible': False, 'reason_code': 'OWNER_MISMATCH'})
                    visited.append(row)
                    continue
                item = reconcile_task_run_closeout(repo if apply else reader, agent.conversation_store,
                    task_run_id=row['task_run_id'], apply=apply, commit_transition=commit_transition)
                result['items'].append({'kind': 'task_run', **item})
                visited.append(row)
            if visited and (len(rows) >= count or len(visited) < len(rows)):
                next_state = {'phase': phase, 'after': visited[-1]['task_run_id'], 'created_at': visited[-1]['created_at']}
            elif rows and not visited:
                next_state = state
            else:
                next_state = {'phase': 'capabilities', 'after': ''}
        elif phase == 'indexes':
            from ..user_space.home_index_rebuild import reconcile_home_index_page
            page = reconcile_home_index_page(agent.home_paths, owner_id=owner, apply=apply, cursor=after,
                limit=count, config=config, budget=budget, commit_transition=commit_transition)
            result['errors'].extend(page['errors'])
            result['items'] = [{'kind': 'indexes', **page}]
            next_state = {'phase': phase, 'after': page.get('next_cursor', '')} if page.get('next_cursor') else {'phase': 'projections', 'after': ''}
        else:
            from ..subagents.services.persistence.locator_hygiene import reconcile_projection_page
            page = reconcile_projection_page(agent, apply=apply, after=after, limit=count, budget=budget, commit_transition=commit_transition, run_ids=run_ids)
            result['errors'].extend(page['errors'])
            result['items'] = [{'kind': 'projections', **page}]
            next_state = {'phase': phase, 'after': page['next_after']} if not page.get('page_complete') else None
        if next_state:
            result['next_cursor'] = json.dumps(dict(next_state, owner=owner, signature=signature), separators=(',', ':'))
    except (OSError, ValueError, RuntimeError, sqlite3.Error, AttributeError, TypeError, KeyError) as exc:
        result['errors'].append({'reason_code': 'RECONCILIATION_SOURCE_UNAVAILABLE', 'error_type': type(exc).__name__,
            'next_step': '保留原记录；核对当前 owner 权威源后重新预览，未知外部操作不得原样重放。'})
        following = {'task_runs': 'capabilities', 'capabilities': 'indexes', 'indexes': 'projections'}.get(state['phase'])
        result['next_cursor'] = json.dumps(dict(phase=following, after='', owner=owner, signature=signature), separators=(',', ':')) if following else ''
        result['effect_outcome'] = 'unknown' if apply else 'not_started'
    result['repaired'] = sum(int(row.get('repaired') or row.get('applied') or row.get('closed') or row.get('archived_dirs') or 0) for row in result['items'])
    result['unknown'] = len(result['errors']) + sum(int(row.get('unknown', 0)) + int(row.get('decision') == 'unknown' or row.get('reason_code') in {'PROOF_INCOMPLETE', 'AUTHORITY_MISSING', 'ATTEMPT_UNKNOWN'}) for row in result['items'])
    result['protected'] = sum(int(row.get('protected', row.get('protected_dirs', 0))) + int(row.get('eligible') is False or row.get('decision') == 'protected') for row in result['items'])
    result['partial'] = bool(result['next_cursor'] or result['errors'] or result['unknown'])
    if any(row.get('effect_outcome') == 'unknown' for row in result['items']):
        result['effect_outcome'] = 'unknown'
    if apply and result['effect_outcome'] != 'unknown' and result['repaired']:
        result['effect_outcome'] = 'succeeded'
    return result
