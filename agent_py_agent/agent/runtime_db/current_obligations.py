"""按主键和当前义务partial索引证明终态，不枚举同实体全部历史运行。"""
from __future__ import annotations

from .operations import AGENT_RUN_TERMINAL_STATUSES, ATTEMPT_STATUS_RECOVERED

_RUN_OPEN = ' AND '.join("status IS NOT '" + value + "'" for value in sorted(AGENT_RUN_TERMINAL_STATUSES))
_WAKE_OPEN = "status IS NOT 'consumed' AND status IS NOT 'cancelled' AND status IS NOT 'done'"

# 声明由RuntimeSchemaMixin统一安装并进入原schema指纹；读侧INDEXED BY缺失时只能未知保留。
CURRENT_OBLIGATION_INDEX_SQL = (
    f'CREATE INDEX IF NOT EXISTS idx_task_runs_current_task ON task_runs(task_id,task_run_id) WHERE {_RUN_OPEN}',
    f'CREATE INDEX IF NOT EXISTS idx_agent_runs_current_run ON agent_runs(run_id,task_run_id) WHERE {_RUN_OPEN}',
    f'CREATE INDEX IF NOT EXISTS idx_wake_queue_current_task ON wake_queue(root_task_id) WHERE {_WAKE_OPEN}',
    f'CREATE INDEX IF NOT EXISTS idx_wake_queue_current_run ON wake_queue(root_run_id) WHERE {_WAKE_OPEN}',
)


def _identities(values):
    values = tuple(dict.fromkeys(values))
    if len(values) > 512 or any(type(value) is not str or not value for value in values):
        raise ValueError('current_obligation_identity_batch_invalid')
    return values


def _requested(ids):
    return 'WITH requested(id) AS (VALUES ' + ','.join('(?)' for _ in ids) + ') '


def pending_wake_ids(conn, ids, *, kind):
    ids = _identities(ids)
    if not ids:
        return set()
    column, index = (('root_task_id', 'idx_wake_queue_current_task') if kind == 'task' else
                     ('root_run_id', 'idx_wake_queue_current_run') if kind == 'run' else (None, None))
    if column is None:
        raise ValueError('current_obligation_scope_invalid')
    query = (_requested(ids) + f'SELECT id FROM requested r WHERE EXISTS('
             f'SELECT 1 FROM wake_queue INDEXED BY {index} WHERE {column}=r.id AND {_WAKE_OPEN} LIMIT 1)')
    return {row[0] for row in conn.execute(query, ids)}


def closed_task_ids(conn, ids):
    """结果最多与请求key数相同；历史库存只做存在性seek，非终态只读当前集合。"""
    ids = _identities(ids)
    if not ids:
        return set()
    marks = ','.join('?' for _ in ids)
    known = {row[0] for row in conn.execute(_requested(ids) + 'SELECT id FROM requested r WHERE EXISTS('
        'SELECT 1 FROM task_runs INDEXED BY idx_task_runs_task WHERE task_id=r.id LIMIT 1)', ids)}
    active = {row[0] for row in conn.execute(f'SELECT DISTINCT task_id FROM task_runs INDEXED BY idx_task_runs_current_task '
        f'WHERE {_RUN_OPEN} AND task_id IN ({marks})', ids)}
    # CROSS JOIN固定从当前AgentRun集合开始，避免优化器对每个旧TaskRun再探子执行体。
    agent_open = _RUN_OPEN.replace('status', 'a.status')
    active.update(row[0] for row in conn.execute('SELECT DISTINCT t.task_id FROM agent_runs a '
        'INDEXED BY idx_agent_runs_current_run CROSS JOIN task_runs t '
        f'WHERE {agent_open} AND t.task_run_id=a.task_run_id AND t.task_id IN ({marks})', ids))
    return known - active - pending_wake_ids(conn, ids, kind='task')


def recorded_run_ids(conn, ids):
    """精确业务run存在性seek；旧副本没有本批身份时不要求它拥有新维护索引。"""
    ids = _identities(ids)
    if not ids:
        return set()
    return {row[0] for row in conn.execute(_requested(ids) + 'SELECT id FROM requested r WHERE EXISTS('
        'SELECT 1 FROM agent_runs INDEXED BY idx_agent_runs_run_id WHERE run_id=r.id LIMIT 1)', ids)}


def closed_attempt_bindings(conn, run_attempts):
    """原launch必须携准确attempt主键；只有旧run_id而无代次的日志保持未知。"""
    runs = _identities(run_attempts)
    attempts = _identities(run_attempts.values())
    if not runs or len(attempts) != len(runs):
        return False, set()
    marks = ','.join('?' for _ in attempts)
    rows = conn.execute('SELECT aa.attempt_id,aa.status,a.run_id,a.status,t.status,t.task_id '
        'FROM agent_attempts aa JOIN agent_runs a ON a.agent_run_id=aa.agent_run_id '
        f'JOIN task_runs t ON t.task_run_id=a.task_run_id WHERE aa.attempt_id IN ({marks})', attempts).fetchall()
    if len(rows) != len(attempts):
        return False, set()
    task_ids = set()
    attempt_terminal = AGENT_RUN_TERMINAL_STATUSES | {ATTEMPT_STATUS_RECOVERED}
    for attempt, status, run, agent_status, task_status, task in rows:
        if (run_attempts.get(run) != attempt or status not in attempt_terminal
                or agent_status not in AGENT_RUN_TERMINAL_STATUSES or task_status not in AGENT_RUN_TERMINAL_STATUSES):
            return False, set()
        task_ids.add(task)
    if pending_wake_ids(conn, runs, kind='run') or pending_wake_ids(conn, task_ids, kind='task'):
        return False, set()
    return True, task_ids
