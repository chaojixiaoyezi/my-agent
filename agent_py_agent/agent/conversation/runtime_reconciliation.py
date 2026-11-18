"""显式管理员对账：只组合原执行账、索引及投影服务，不启动模型或推断未知副作用。"""
from __future__ import annotations

import base64
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


class ReconciliationBudgetExhausted(ValueError):
    error_code = 'CAPABILITY_PAGE_BYTE_BUDGET_EXHAUSTED'


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
                        from ..scheduler.active_run_closeout import _claim_may_still_execute
                        from .run_claim import detached_task_claim_scope_id
                        if link.cancellation_scope == 'foreground':
                            scope_id = ''
                        elif link.cancellation_scope == 'detached':
                            scope_id = detached_task_claim_scope_id(link.thread_id, row['task_id'])
                        else:
                            return {**result, 'reason_code': 'PROOF_INCOMPLETE'}
                        claim, claim_error = agent.conversation_store.claims.load_report(link.thread_id, claim_scope_id=scope_id)
                        if claim_error or (claim and _claim_may_still_execute(claim, link.thread_id, row['task_id'], scope_id)):
                            return {**result, 'reason_code': 'CURRENT_ATTEMPT_ACTIVE'}
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


def _capability_loader(agent, budget):
    """在原 owner 下限量读取 locator/canonical，再复用原模型 normalizer。"""
    import os

    from ..common.nofollow_fs import open_readonly_file_beneath
    from ..subagents.services.agent_run_state import (
        canonical_state_path_from_payload,
        is_agent_state_locator,
    )
    from ..subagents.services.persistence.service import task_from_payload
    root = Path(agent.home_paths.owner_home_dir)
    consumed = 0
    ceiling = budget.max_bytes if budget is not None else None
    ceiling = ceiling or getattr(agent.config, 'retention_scan_slice_bytes', 8 * 1024 * 1024) or 8 * 1024 * 1024
    def read(path):
        nonlocal consumed
        relative = Path(path).relative_to(root)
        descriptor = open_readonly_file_beneath(root, relative.parts)
        with os.fdopen(descriptor, 'rb') as stream:
            size = os.fstat(stream.fileno()).st_size
            if size > ceiling - consumed or (budget is not None and budget.exhausted(size=consumed)):
                raise ReconciliationBudgetExhausted('CAPABILITY_PAGE_BYTE_BUDGET_EXHAUSTED')
            raw = stream.read(ceiling - consumed + 1)
            consumed += len(raw)
            if consumed > ceiling:
                raise ReconciliationBudgetExhausted('CAPABILITY_PAGE_BYTE_BUDGET_EXHAUSTED')
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError('CAPABILITY_RECORD_INVALID')
        return data
    def load(run_id):
        if not _segment(run_id):
            raise ValueError('CAPABILITY_RUN_ID_INVALID')
        path = Path(agent.subagents.workspace) / run_id / 'task.json'
        data = read(path)
        canonical = canonical_state_path_from_payload(data, workspace=root)
        if canonical and canonical != path:
            data = read(canonical)
        elif is_agent_state_locator(data):
            raise ValueError('CAPABILITY_CANONICAL_STATE_UNAVAILABLE')
        return task_from_payload(data)
    return load


def reconcile_capability_page(agent, *, apply=False, after='', limit=100, run_ids=(), commit_transition=None, budget=None, older_than_seconds=None):
    """一页 canonical 申请，冻结准确申请集合，在原创建锁内重核全部父级证明。"""
    from ..subagents.capability_request_expiry import (
        close_existing_capability_requests,
        existing_capability_closeout_confirmed,
    )
    from ..subagents.model_capabilities import capability_request_counts_as_open
    from ..subagents.models import task_has_ended_status
    from ..subagents.services.persistence.run_index import SubagentRunIndex
    subagents = getattr(agent, 'subagents', None)
    if subagents is None:
        return {'items': [], 'next_after': '', 'errors': [{'reason_code': 'AUTHORITY_MISSING'}]}
    items, errors = [], []
    load_run = _capability_loader(agent, budget)
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
        # 至少核对一条再看预算：首条前耗尽会在 after 为空时把整个能力相静默跳过，非空时原样返回同一游标。
        if visited and budget is not None and budget.exhausted(visited):
            next_after = ids[visited - 1]
            break
        item = {'kind': 'capability', 'run_id': rid, 'repaired': False, 'eligible': False, 'reason_code': 'PARENT_TERMINAL_UNPROVEN'}
        try:
            run = load_run(rid)
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
                    parent = load_run(current.parent_id)
                    if parent.owner != current.owner or parent.root_id != current.root_id or not task_has_ended_status(parent):
                        return False
                return inspect_parent_terminal(agent, current.root_id)['eligible'] is True
            if eligible(run):
                item.update(eligible=True, reason_code='PARENT_TERMINAL_PROVEN')
                if apply:
                    def commit():
                        task_id = proof['observed_identity'].get('task_id') or run.root_id
                        with subagents.creation_guard(), agent.conversation_store.tasks.transition_guard(task_id):
                            current = load_run(rid)
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
            item.update(reason_code=getattr(exc, 'error_code', 'PROOF_INCOMPLETE'), error_type=type(exc).__name__,
                next_step='保留原申请与执行账；核对当前权威源。字节预算不足时缩小页或单独选定 run_ids，未知外部操作不得重放。')
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


# 每类保护原因对应的现有处置入口；只给方向，不新增强制结案路径。
_REASON_GUIDANCE = {
    'durable_task_active': '会话任务仍为活动状态；先用 /tasks 核对或结束该任务，结束后再预览。本工具不强制结案。',
    'execution_effect_unknown': '存在未确认副作用的托管操作；按 /recover 记录处置（confirmed_noop/recorded/abandoned）后再预览。',
    'attempt_unknown': '运行轮状态未知；先按 /recover 核对该运行，再预览。',
    'agent_tree_active': '代理树仍有活动或挂起的运行；先核对这些运行是否仍在执行。',
    'invalid_agent_tree': '代理树结构不完整；保留原记录并人工核对。',
}


# LLM: 只读补充管理员决定所需事实（任务状态、最近活动、未确认操作编号），不改判定；读失败只省略补充字段。
# 函数用途: 为不可收口的执行账条目附上处置所需的事实。
def _task_run_decision_facts(item, row, reader_path, store):
    facts = {'last_activity_at': row['updated_at']}
    reason = item.get('reason_code')
    try:
        if reason == 'durable_task_active':
            task = store.tasks.load(str(item.get('task_id') or row['task_id']))
            facts['task_status'] = str(getattr(task, 'status', '') or (task.get('status') if isinstance(task, dict) else '') or '')
        elif reason in {'execution_effect_unknown', 'attempt_unknown'}:
            with _snapshot(reader_path) as conn:
                facts['unknown_operation_ids'] = [str(op[0]) for op in conn.execute(
                    "SELECT op.operation_id FROM agent_runs ar JOIN tool_operations op ON op.agent_run_id=ar.agent_run_id "
                    "WHERE ar.task_run_id=? AND op.status IN ('EXECUTING','UNKNOWN','CLAIMED') ORDER BY op.created_at LIMIT 5",
                    (row['task_run_id'],))]
    except Exception:  # noqa: BLE001 - 补充事实尽力读取，不影响判定
        pass
    return facts


def _readonly_repository(path):
    from ..runtime_db.repository import RuntimeRepository
    return RuntimeRepository(path, read_only=True)


# LLM: 续页游标对模型是不透明串；明文 JSON 里的 "signature" 字段会被模型侧凭据遮蔽改写成 [REDACTED]，
#   第二页因此永远验不过绑定摘要（live 2026-11-17）。输出用 base64url 包装，输入仍接受旧的明文 JSON。
# 函数用途: 把续页状态编码成不会被文本脱敏改写的游标。
def _encode_reconcile_cursor(state: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(state, separators=(',', ':')).encode()).decode()


# 函数用途: 还原上一页游标；格式不对时抛 ValueError，由调用方提示从空 cursor 重新预览。
def decode_reconcile_cursor(cursor: str) -> object:
    text = cursor if cursor.lstrip().startswith('{') else base64.urlsafe_b64decode(cursor.encode()).decode()
    return json.loads(text)


def reconcile_owner_runtime(agent, *, apply: bool = False, cursor: str = '', limit: int | None = None,
                            older_than_seconds: float | None = None, run_ids: tuple[str, ...] = (), commit_transition=None,
                            task_runs_only: bool = False) -> dict[str, object]:
    """每次只处理一个领域页；续点绑定 owner、过滤和源身份，不写第二份进度账。

    task_runs_only 用于管理员对其他 owner 的只读/收口视图：只走执行账一相，结束后不进入能力、索引与投影相。
    """
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
    signature = hashlib.sha256(json.dumps([str(agent.home_paths.owner_home_dir), owner, generation, sorted(set(run_ids)), older_than_seconds]
                                          + (['task_runs_only'] if task_runs_only else [])).encode()).hexdigest()
    if task_runs_only and run_ids:
        raise ValueError('跨 owner 视图只处理执行账，不接受 run_ids')
    state = {'phase': 'capabilities' if run_ids else 'task_runs', 'after': ''}
    if cursor:
        if len(cursor) > 8192:
            raise ValueError('cursor 超出上限，请从空 cursor 重新预览')
        token = decode_reconcile_cursor(cursor)
        if not isinstance(token, dict):
            raise ValueError('cursor 必须是上一页原样返回的 JSON 对象；请从空 cursor 重新预览')
        allowed = {'task_runs'} if task_runs_only else {'capabilities', 'task_runs', 'indexes', 'projections'}
        if token.get('owner') != owner or token.get('signature') != signature or token.get('phase') not in allowed:
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
            # 让出时间只计逐行核对；打开权威库和取页本身已受 LIMIT 约束，不能在第一行之前就把本页耗光。
            budget = StorageLaneBudget(budget.max_items, budget.max_bytes,
                                       time.monotonic() + slice_ms / 1000 if slice_ms else None)
            visited = []
            for row in rows:
                # 每页至少核对一行再看预算，保证游标单调前进（live 2026-11-18：三页同一游标）。
                if visited and budget.exhausted(len(visited)):
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
                if item.get('decision') == 'protected':
                    item = {**item, **_task_run_decision_facts(item, row, repo.db_path, agent.conversation_store)}
                result['items'].append({'kind': 'task_run', **item})
                visited.append(row)
            if visited and (len(rows) >= count or len(visited) < len(rows)):
                next_state = {'phase': phase, 'after': visited[-1]['task_run_id'], 'created_at': visited[-1]['created_at']}
            else:
                next_state = None if task_runs_only else {'phase': 'capabilities', 'after': ''}
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
            result['next_cursor'] = _encode_reconcile_cursor(dict(next_state, owner=owner, signature=signature))
    except (OSError, ValueError, RuntimeError, sqlite3.Error, AttributeError, TypeError, KeyError) as exc:
        result['errors'].append({'reason_code': 'RECONCILIATION_SOURCE_UNAVAILABLE', 'error_type': type(exc).__name__,
            'next_step': '保留原记录；核对当前 owner 权威源后重新预览，未知外部操作不得原样重放。'})
        following = None if task_runs_only else {'task_runs': 'capabilities', 'capabilities': 'indexes', 'indexes': 'projections'}.get(state['phase'])
        result['next_cursor'] = _encode_reconcile_cursor(dict(phase=following, after='', owner=owner, signature=signature)) if following else ''
        result['effect_outcome'] = 'unknown' if apply else 'not_started'
    result['repaired'] = sum(int(row.get('repaired') or row.get('applied') or row.get('closed') or row.get('archived_dirs') or 0) for row in result['items'])
    result['unknown'] = len(result['errors']) + sum(int(row.get('unknown', 0)) + int(row.get('decision') == 'unknown' or row.get('reason_code') in {'PROOF_INCOMPLETE', 'AUTHORITY_MISSING', 'ATTEMPT_UNKNOWN'}) for row in result['items'])
    result['protected'] = sum(int(row.get('protected', row.get('protected_dirs', 0))) + int(row.get('eligible') is False or row.get('decision') == 'protected') for row in result['items'])
    result['partial'] = bool(result['next_cursor'] or result['errors'] or result['unknown'])
    counts: dict[str, int] = {}
    for row in result['items']:
        code = row.get('reason_code')
        if isinstance(code, str) and code:
            counts[code] = counts.get(code, 0) + 1
    result['reason_counts'] = counts
    result['reason_guidance'] = {code: _REASON_GUIDANCE[code] for code in counts if code in _REASON_GUIDANCE}
    if any(row.get('effect_outcome') == 'unknown' for row in result['items']):
        result['effect_outcome'] = 'unknown'
    if apply and result['effect_outcome'] != 'unknown' and result['repaired']:
        result['effect_outcome'] = 'succeeded'
    return result
