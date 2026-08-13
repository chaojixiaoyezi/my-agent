
from __future__ import annotations

"""task identity decoupling module.

让 task_id 全局唯一、独立于会话，任何终端/会话都能查询。
"""

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..local_storage import LocalStore


@dataclass(frozen=True)
class RegisterTaskParams:
    task_id: str
    status: str
    goal: str
    session_id: str | None = None
    user_id: str | None = None


# 终态(不可复活):一旦进入,不允许被改回非终态(防"终态被并发/迟到写复活"=双花/重复对外动作)。
# 注意 failed/timeout/blocked 不在此列——它们可重试(failed→running 是合法重跑),只锁真正的"完结"态。
_FINAL_TASK_STATUSES = frozenset(
    {"done", "completed", "succeeded", "cancelled", "canceled", "abandoned", "closed", "resolved", "finished"}
)


def _status_write_guard(new_status: str, expected_status: str | None) -> tuple[str, list[str]]:
    """组装防丢更新/防终态复活的 WHERE 子句 + 参数(审计 #6)。"""
    clauses: list[str] = []
    params: list[str] = []
    if new_status not in _FINAL_TASK_STATUSES:
        marks = ",".join("?" for _ in _FINAL_TASK_STATUSES)
        clauses.append(f"status NOT IN ({marks})")  # 当前若是终态、且新状态非终态 → 不改(终态不复活)
        params.extend(sorted(_FINAL_TASK_STATUSES))
    if expected_status is not None:
        clauses.append("status = ?")  # CAS:仅当前状态匹配预期才改(防并发/迟到写覆盖)
        params.append(expected_status)
    return ("".join(f" AND {c}" for c in clauses), params)


class TaskRegistry:

    def __init__(self, store: LocalStore) -> None:
        self._store = store

    def register_task(
        self,
        task_id: str = "",
        *,
        status: str = "",
        goal: str = "",
        session_id: str | None = None,
        user_id: str | None = None,
        params: RegisterTaskParams | None = None,
    ) -> None:

        values = params or RegisterTaskParams(task_id, status, goal, session_id, user_id)
        now = time.time()

        with self._store._connection() as conn:
            # P0-1(HANDOFF 文档线, seq1562 收口): 终态任务不可被 register 复活。
            # 用同一写事务内的原子条件 UPSERT 替代「先 SELECT 再 UPDATE」(防
            # TOCTOU——两条连接交错时迟到写不得覆盖已提交终态): ON CONFLICT
            # DO UPDATE 的 WHERE 只放行「新值同为终态(幂等确认)」或「当前非
            # 终态」; 其余(当前终态+新值非终态)被 WHERE 拒绝, rowcount=0,
            # 再以 SELECT 确认存在终态行后抛错 fail-closed, 与
            # update_task_status 的终态守卫同语义, 对齐终态权威铁律。
            marks = ",".join("?" for _ in sorted(_FINAL_TASK_STATUSES))
            terminal = sorted(_FINAL_TASK_STATUSES)
            cursor = conn.execute(
                f"""
                INSERT INTO task_registry (task_id, session_id, user_id, status, goal, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    session_id=excluded.session_id,
                    user_id=excluded.user_id,
                    status=excluded.status,
                    goal=excluded.goal,
                    updated_at=excluded.updated_at
                WHERE excluded.status IN ({marks}) OR status NOT IN ({marks})
                """,
                (
                    values.task_id,
                    values.session_id or "",
                    values.user_id or "",
                    values.status,
                    values.goal,
                    now,
                    now,
                    *terminal,
                    *terminal,
                ),
            )
            if cursor.rowcount == 0:
                row = conn.execute(
                    "SELECT status FROM task_registry WHERE task_id = ?",
                    (values.task_id,),
                ).fetchone()
                if row is not None:
                    raise ValueError(
                        f"终态任务不可复活: task_id={values.task_id} "
                        f"status={row[0]} -> {values.status}"
                    )
            conn.commit()

    def lookup_task(self, task_id: str) -> dict | None:

        with self._store._connection() as conn:
            cursor = conn.execute(
                "SELECT task_id, session_id, user_id, status, goal, created_at, updated_at FROM task_registry WHERE task_id = ?",
                (task_id,),
            )
            row = cursor.fetchone()

        if row:
            return {
                "task_id": row[0],
                "session_id": row[1],
                "user_id": row[2],
                "status": row[3],
                "goal": row[4],
                "created_at": row[5],
                "updated_at": row[6],
            }
        return None

    def query_tasks(
        self,
        user_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict]:

        conditions = []
        params = []

        if user_id:
            conditions.append("user_id = ?")
            params.append(user_id)

        if status:
            conditions.append("status = ?")
            params.append(status)

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        with self._store._connection() as conn:
            query = f"""
                SELECT task_id, session_id, user_id, status, goal, created_at, updated_at
                FROM task_registry
                WHERE {where_clause}
                ORDER BY updated_at DESC
                LIMIT ?
            """
            cursor = conn.execute(query, params + [limit])
            rows = cursor.fetchall()

            return [
                {
                    "task_id": row[0],
                    "session_id": row[1],
                    "user_id": row[2],
                    "status": row[3],
                    "goal": row[4],
                    "created_at": row[5],
                    "updated_at": row[6],
                }
                for row in rows
            ]

    def remove_task(self, task_id: str) -> None:

        with self._store._connection() as conn:
            conn.execute("DELETE FROM task_registry WHERE task_id = ?", (task_id,))
            conn.commit()

    def update_task_status(self, task_id: str, status: str, *, expected_status: str | None = None) -> bool:
        """原子改状态。expected_status 给定时做 CAS(仅当前状态匹配才改,防丢更新/迟到写覆盖);
        并默认拒绝把终态(done/cancelled/abandoned…)改回非终态(防终态被复活)。返回是否真改了。"""
        now = time.time()
        guard_sql, guard_params = _status_write_guard(status, expected_status)
        with self._store._connection() as conn:
            cursor = conn.execute(
                f"UPDATE task_registry SET status = ?, updated_at = ? WHERE task_id = ?{guard_sql}",
                (status, now, task_id, *guard_params),
            )
            conn.commit()
            return cursor.rowcount > 0

    def update_task_description(self, task_id: str, description: str) -> bool:

        now = time.time()
        # 当前 registry schema 只有 goal 摘要字段，description 截断后写入 goal。
        truncated = description[:100] if description else ""
        with self._store._connection() as conn:
            # 尝试更新 goal 字段（存 description）
            cursor = conn.execute(
                "UPDATE task_registry SET goal = ?, updated_at = ? WHERE task_id = ?",
                (truncated, now, task_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_task_timestamps(self, task_id: str) -> dict | None:
        """获取任务的时间戳信息。"""

        with self._store._connection() as conn:
            cursor = conn.execute(
                "SELECT created_at, updated_at FROM task_registry WHERE task_id = ?",
                (task_id,),
            )
            row = cursor.fetchone()

        if row:
            return {
                "created_at": row[0],
                "updated_at": row[1],
            }
        return None
