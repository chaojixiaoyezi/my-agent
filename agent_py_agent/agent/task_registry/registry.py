from __future__ import annotations

"""LLM: task identity decoupling module.

给人看的解释：
让 task_id 全局唯一、独立于会话，任何终端/会话都能查询。
"""

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..local_store import LocalStore


class TaskRegistry:

    def __init__(self, store: LocalStore) -> None:
        self._store = store

    def register_task(
        self,
        task_id: str,
        status: str,
        goal: str,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> None:

        now = time.time()

        with self._store._connection() as conn:
            conn.execute(
                """
                INSERT INTO task_registry (task_id, session_id, user_id, status, goal, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    session_id=excluded.session_id,
                    user_id=excluded.user_id,
                    status=excluded.status,
                    goal=excluded.goal,
                    updated_at=excluded.updated_at
                """,
                (task_id, session_id or "", user_id or "", status, goal, now, now),
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

    def update_task_status(self, task_id: str, status: str) -> bool:

        now = time.time()
        with self._store._connection() as conn:
            cursor = conn.execute(
                "UPDATE task_registry SET status = ?, updated_at = ? WHERE task_id = ?",
                (status, now, task_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def update_task_description(self, task_id: str, description: str) -> bool:

        now = time.time()
        # description 存到 goal 字段的前 100 字符，或者新建专门的 description 字段
        # 为兼容现有结构，把 description 截断后存到 goal 后面
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
