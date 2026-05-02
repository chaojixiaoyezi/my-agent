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
    """任务身份注册表。

    职责：
    - 注册任务到全局索引
    - 查询任务信息
    - 按条件过滤任务
    - 删除任务

    使用 LocalStore 的 task_registry 表存储。
    """

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
        """注册任务到全局索引。

        Args:
            task_id: 任务 ID
            status: 任务状态
            goal: 任务目标
            session_id: 会话 ID
            user_id: 用户 ID
        """

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
        """查询任务信息。

        Args:
            task_id: 任务 ID

        Returns:
            任务信息字典，或 None
        """

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
        """按条件查询任务。

        Args:
            user_id: 用户 ID 过滤
            status: 状态过滤
            limit: 最多返回多少条

        Returns:
            任务信息列表
        """

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
        """删除任务。

        Args:
            task_id: 任务 ID
        """

        with self._store._connection() as conn:
            conn.execute("DELETE FROM task_registry WHERE task_id = ?", (task_id,))
            conn.commit()
