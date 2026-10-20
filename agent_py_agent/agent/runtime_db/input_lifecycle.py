# LLM: 只扩展原RuntimeRepository的精确wake行CAS和输入退休执行账；原schema/队列/状态不变，C必须显式只读，所有写在原来源短门内。
# 模块用途: 防旧到期消费者删掉重排字条，并在输入静默取消后安全结束准确执行账。
from __future__ import annotations

import time

from ..common.strict_json import load_strict_json
from .operations import (
    AGENT_RUN_TERMINAL_STATUSES,
    RUN_STATUS_LEGACY_CREATED,
    attempt_status_is_terminal,
)


# LLM: 比较整行，不用DB文件mtime/字节；必须在同一SQL事务里读取，回放不能把旧claim当新许可。
# 函数用途: 验证某个wake仍与调用方刚冻结的结构完全一致。
def _same_wake(conn, expected):
    row = conn.execute('SELECT * FROM wake_queue WHERE wake_id=?', (expected['wake_id'],)).fetchone()
    return row is not None and dict(row) == expected


# LLM: 发现、认领、收口共享原wake_queue；构造和方法不新建调度器，外层RuntimeRepository仍控制ro/事务。
# 类用途: 为原运行库提供输入期限与静默退休的精确事务。
class RuntimeInputLifecycleMixin:
    # LLM: 只读索引页包括原lease到期claim，不因读取初始化库或改变状态；C的repo须read_only=True。
    # 函数用途: 找到有限的真实到期字条供原后台运输发现。
    def due_runtime_wakes(self, *, now, limit=64):
        with self._runtime_connection() as conn:
            rows = conn.execute("SELECT * FROM wake_queue WHERE (status='pending' AND next_due_at<=? AND retry_after<=?) "
                "OR (status='claimed' AND lease_until<?) ORDER BY next_due_at,wake_id LIMIT ?",
                (now, now, now, int(limit))).fetchall()
            return [dict(row) for row in rows]

    # LLM: 原wake_id仅作查询，未知返回None不创造字条；只能从实际owner运行库调用。
    # 函数用途: 重读一条准确来源行。
    def read_runtime_wake(self, wake_id):
        with self._runtime_connection() as conn:
            row = conn.execute('SELECT * FROM wake_queue WHERE wake_id=?', (wake_id,)).fetchone()
            return dict(row) if row is not None else None

    # LLM: 精确root/kind查询不受全owner1000行投影影响；不读取其它业务行或修改状态。
    # 函数用途: 为同一任务冻结现有输入期限的观察版本。
    def input_deadline_wakes(self, task_id):
        with self._runtime_connection() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM wake_queue WHERE root_task_id=? "
                "AND kind='task_input_deadline'", (task_id,)).fetchall()]

    # LLM: 原pending/lease回收判据与完整行CAS同事务；返回自己的新claim快照，旧观察不得领取重排后的行。
    # 函数用途: 为原P5消费认领唯一冻结字条，不领取owner其它工作。
    def claim_runtime_wake(self, expected, *, now):
        with self.transaction() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if not _same_wake(conn, expected):
                return None
            reclaim = expected['status'] == 'claimed' and expected['lease_until'] < now
            due = expected['status'] == 'pending' and expected['next_due_at'] <= now and expected['retry_after'] <= now
            if not (reclaim or due):
                return None
            conn.execute("UPDATE wake_queue SET status='claimed',woke_at=?,lease_until=?,updated_at=?,"
                "attempt_count=attempt_count+?,last_error=? WHERE wake_id=?",
                (now, now+300.0, now, int(reclaim), 'lease_expired' if reclaim else expected['last_error'], expected['wake_id']))
            return dict(conn.execute('SELECT * FROM wake_queue WHERE wake_id=?', (expected['wake_id'],)).fetchone())

    # LLM: quota先于此SQL事务由调用方持有；整行claim与实际文件效果同一事务窗口，重排必须等效果结束，旧消费者不能先副作用后拒complete。
    # 函数用途: 在仍持有准确claim时同步提交一段不含模型/网络/嵌套DB写的领域效果。
    def perform_runtime_wake(self, claim, operation):
        with self.transaction() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if claim['status'] != 'claimed' or not _same_wake(conn,claim):
                return False
            operation()
            return True

    # LLM: complete/release只能消费自己整行claim；并发upsert同id改期限/kind或新lease时零写，不能删除/回退新责任。
    # 函数用途: 完成精确字条，或按原字段退避重试。
    def settle_runtime_wake(self, claim, *, retry_after=None):
        with self.transaction() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if claim['status'] != 'claimed' or not _same_wake(conn, claim):
                return False
            if retry_after is None:
                conn.execute('DELETE FROM wake_queue WHERE wake_id=?', (claim['wake_id'],))
            else:
                conn.execute("UPDATE wake_queue SET status='pending',retry_after=?,attempt_count=attempt_count+1,"
                    "last_error='consume_failed',updated_at=? WHERE wake_id=?",
                    (retry_after, time.time(), claim['wake_id']))
            return True

    # LLM: 无pending输入才由领域调用；只删原观察的pending输入期限，活claim/其它kind/重排版本全部保留。
    # 函数用途: 及时回收已经没有输入责任的旧期限。
    def retire_input_deadline(self, expected):
        with self.transaction() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if expected['kind'] != 'task_input_deadline' or expected['status'] != 'pending' or not _same_wake(conn, expected):
                return False
            conn.execute('DELETE FROM wake_queue WHERE wake_id=?', (expected['wake_id'],))
            return True

    # LLM: 此例外只收准确已结束等待片；所有树/attempt/op UNKNOWN及活动锁在同一最终事务拒绝，不改变旧generic settle。
    # 函数用途: 输入已静默退休时，将本次运行账记cancelled，保留长期Task和外部TaskLink。
    def settle_retired_input_execution(self, *, task_id, run_id, attempt_id):
        with self.transaction() as conn:
            conn.execute('BEGIN IMMEDIATE')
            target = conn.execute('SELECT ar.*,aa.status AS attempt_status,aa.ended_at FROM agent_runs ar '
                'JOIN agent_attempts aa ON aa.attempt_id=ar.current_attempt_id WHERE ar.run_id=? '
                'AND aa.attempt_id=?', (run_id, attempt_id)).fetchone()
            if target is None or target['attempt_status'] == 'unknown' or not attempt_status_is_terminal(target['attempt_status']) or target['ended_at'] <= 0:
                return False
            event = conn.execute("SELECT payload_json FROM runtime_events WHERE agent_run_id=? AND attempt_id=? "
                "AND event_type IN ('agent_attempt.completed','agent_run.completed') ORDER BY seq DESC LIMIT 1",
                (target['agent_run_id'],attempt_id)).fetchone()
            if event is None or not _finished_input_wait(event['payload_json']):
                return False
            task_run = conn.execute('SELECT * FROM task_runs WHERE task_run_id=?', (target['task_run_id'],)).fetchone()
            if (task_run is None or task_run['task_id'] != task_id
                    or task_run['status'] not in RUN_STATUS_LEGACY_CREATED | {'cancelled'}
                    or target['status'] not in RUN_STATUS_LEGACY_CREATED | {'cancelled'}):
                return False
            agents = conn.execute('SELECT ar.*,aa.status AS attempt_status,aa.ended_at FROM agent_runs ar '
                'LEFT JOIN agent_attempts aa ON aa.attempt_id=ar.current_attempt_id WHERE ar.task_run_id=?',
                (target['task_run_id'],)).fetchall()
            if not _retired_tree_quiescent(conn, agents):
                return False
            now = time.time()
            if target['status'] != 'cancelled':
                conn.execute("UPDATE agent_runs SET status='cancelled',updated_at=? WHERE agent_run_id=?",
                    (now,target['agent_run_id']))
                self._append_event_conn(conn,event_type='agent_run.completed',attempt_id=attempt_id,
                    agent_run_id=target['agent_run_id'],task_run_id=target['task_run_id'],
                    payload={'status':'cancelled','runtime_status':'cancelled','runtime_reason':'TASK_INPUT_CANCELLED','runtime_source':'input_dependency'})
            if not target['parent_agent_run_id'] and task_run['closed_at'] == 0:
                conn.execute("UPDATE task_runs SET status='cancelled',closed_at=?,updated_at=? WHERE task_run_id=? AND closed_at=0",
                    (now,now,target['task_run_id']))
                self._append_event_conn(conn,event_type='task_run.closed',attempt_id='',agent_run_id='',task_run_id=target['task_run_id'],
                    payload={'status':'cancelled','task_id':task_id,'operator':'input-dependency','reason':'TASK_INPUT_CANCELLED'})
            return True


# LLM: active-Link例外用更严整树SQL事实，terminal run也不绕UNKNOWN操作检查；不修改旧收口的静止定义。
# 函数用途: 在持有最终SQL写事务时确认所有相关执行都已结束且无未知副作用。
def _retired_tree_quiescent(conn, agents):
    if len([row for row in agents if not row['parent_agent_run_id']]) != 1:
        return False
    for row in agents:
        if row['status'] not in RUN_STATUS_LEGACY_CREATED | AGENT_RUN_TERMINAL_STATUSES:
            return False
        if row['attempt_status'] == 'unknown' or not attempt_status_is_terminal(row['attempt_status']) or not row['ended_at'] or row['ended_at'] <= 0:
            return False
        if conn.execute('SELECT 1 FROM agent_attempts WHERE agent_run_id=? AND (status=? OR ended_at=0)',
                (row['agent_run_id'],'unknown')).fetchone() is not None:
            return False
        if conn.execute("SELECT 1 FROM tool_operations WHERE agent_run_id=? AND status NOT IN ('SUCCEEDED','FAILED','CANCELLED')",
                (row['agent_run_id'],)).fetchone() is not None:
            return False
        if conn.execute('SELECT 1 FROM resource_locks WHERE attempt_id IN '
                '(SELECT attempt_id FROM agent_attempts WHERE agent_run_id=?)', (row['agent_run_id'],)).fetchone() is not None:
            return False
    return True


# LLM: 只读原repo写下的结构化结束事件，字段必须精确匹配输入等待/退休，不把任意已done attempt当成等待片。
# 函数用途: 验证本次已结束的执行确实属于同一输入等待语义。
def _finished_input_wait(raw):
    try:
        value = load_strict_json(raw)
    except (ValueError,TypeError,RecursionError):
        return False
    if not isinstance(value,dict) or value.get('runtime_source') != 'input_dependency':
        return False
    return (value.get('runtime_status'),value.get('runtime_reason')) in {
        ('needs_user_input','TASK_INPUT_WAIT'),('unfinished','TASK_INPUT_RESUME_PENDING'),
        ('cancelled','TASK_INPUT_CANCELLED')}
