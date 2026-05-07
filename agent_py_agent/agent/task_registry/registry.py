# LLM: Task registry module; keep task lookup and metadata contracts stable.
# 模块用途: 注册、查询和更新任务元数据，给 CLI 和调度逻辑使用。

from __future__ import annotations

"""task identity decoupling module.

给人看的解释：
让 task_id 全局唯一、独立于会话，任何终端/会话都能查询。
"""

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..local_store import LocalStore


# LLM: RegisterTaskParams is a 任务注册表 boundary object; coordinate field or method changes with callers, docs, and focused tests.
# 类用途: 保存 RegisterTaskParams 的输入字段，调用方先构造这个对象再进入 任务注册表，避免继续散传参数。
@dataclass(frozen=True)
class RegisterTaskParams:
    task_id: str
    status: str
    goal: str
    session_id: str | None = None
    user_id: str | None = None


# LLM: TaskRegistry is a 任务注册表 boundary object; coordinate field or method changes with callers, docs, and focused tests.
# 类用途: 集中封装 任务注册表 中和 TaskRegistry 相关的状态与行为，新增职责前先确认是否该拆到相邻服务。
class TaskRegistry:

    # LLM: TaskRegistry.__init__ belongs to 任务注册表; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 初始化实例依赖和字段，不应在构造阶段做难以回滚的重副作用；它是 TaskRegistry 的方法，通常依赖实例字段。
    def __init__(self, store: LocalStore) -> None:
        self._store = store

    # LLM: TaskRegistry.register_task belongs to 任务注册表; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 完成 任务注册表 里的 register_task 步骤，保持现有返回值、异常和副作用语义；会读取实例字段。
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
                (values.task_id, values.session_id or "", values.user_id or "", values.status, values.goal, now, now),
            )
            conn.commit()

    # LLM: TaskRegistry.lookup_task belongs to 任务注册表; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 完成 任务注册表 里的 lookup_task 步骤，保持现有返回值、异常和副作用语义；会读取实例字段。
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

    # LLM: TaskRegistry.query_tasks belongs to 任务注册表; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 查询已有记录、索引或配置并返回给上层调用方，返回结构需要保持稳定；它是 TaskRegistry 的方法，通常依赖实例字段。
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

    # LLM: TaskRegistry.remove_task belongs to 任务注册表; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 完成 任务注册表 里的 remove_task 步骤，保持现有返回值、异常和副作用语义；会读取实例字段。
    def remove_task(self, task_id: str) -> None:

        with self._store._connection() as conn:
            conn.execute("DELETE FROM task_registry WHERE task_id = ?", (task_id,))
            conn.commit()

    # LLM: TaskRegistry.update_task_status belongs to 任务注册表; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 完成 任务注册表 里的 update_task_status 步骤，保持现有返回值、异常和副作用语义；会读取实例字段。
    def update_task_status(self, task_id: str, status: str) -> bool:

        now = time.time()
        with self._store._connection() as conn:
            cursor = conn.execute(
                "UPDATE task_registry SET status = ?, updated_at = ? WHERE task_id = ?",
                (status, now, task_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    # LLM: TaskRegistry.update_task_description belongs to 任务注册表; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 完成 任务注册表 里的 update_task_description 步骤，保持现有返回值、异常和副作用语义；会读取实例字段。
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

    # LLM: TaskRegistry.get_task_timestamps belongs to 任务注册表; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 获取任务的时间戳信息。。
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
