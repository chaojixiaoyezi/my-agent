"""R3 Acceptance 权威操作（3.txt I 节）：冻结契约 CAS + ValidatorOperation 账。

- freeze_contract：Contract Compiler 编译、校验后由框架单事务写入
  acceptance_contracts 并 CAS 更新 task_runs.current_contract_id（I.6，
  防分叉；契约不可变，任务不得改用第二份契约）。
- validator 执行记录（A.8）：每次执行追到具体 attempt_id，含 code
  digest/argv/env/artifact digests/stdout/stderr（§6 统一要求）。

所有终态 fail-closed：CAS rowcount≠1 一律 RuntimeConflictError。
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from ..common.id_generator import new_id
from .operations import CONTRACT_DIVERGED, RuntimeConflictError


class RuntimeAcceptanceMixin:
    """R3 冻结契约与 validator 账本（挂到 RuntimeRepository）。"""

    def freeze_contract(
        self,
        *,
        contract_id: str,
        task_run_id: str,
        attempt_id: str,
        compiled: dict[str, Any],
        digest: str,
        inert_legacy: dict[str, Any] | None = None,
    ) -> None:
        """冻结契约（I.2/I.6）：单事务 INSERT + current_contract_id CAS。

        已冻结同一 contract_id → 幂等通过；已冻结不同契约 → RuntimeConflictError
        （CONTRACT_DIVERGED：契约不可变，任务不得改用第二份契约）。
        """
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO acceptance_contracts(
                    contract_id, task_run_id, attempt_id, status,
                    compiled_json, digest, inert_legacy_json, frozen_at)
                VALUES(?, ?, ?, 'FROZEN', ?, ?, ?, ?)
                """,
                (
                    contract_id,
                    task_run_id,
                    attempt_id,
                    json.dumps(compiled, ensure_ascii=False, sort_keys=True),
                    digest,
                    json.dumps(inert_legacy or {}, ensure_ascii=False, sort_keys=True),
                    time.time(),
                ),
            )
            row = conn.execute(
                "SELECT current_contract_id FROM task_runs WHERE task_run_id = ?",
                (task_run_id,),
            ).fetchone()
            if row is None:
                raise RuntimeConflictError(
                    f"{CONTRACT_DIVERGED}: task_run 不存在: {task_run_id}"
                )
            current = str(row["current_contract_id"] or "")
            if current and current != contract_id:
                raise RuntimeConflictError(
                    f"{CONTRACT_DIVERGED}: task_run {task_run_id} 已冻结 {current},"
                    f" 拒绝改用 {contract_id}"
                )
            conn.execute(
                "UPDATE task_runs SET current_contract_id = ?, updated_at = ? WHERE task_run_id = ?",
                (contract_id, time.time(), task_run_id),
            )

    def contract_by_id(self, contract_id: str) -> dict[str, Any] | None:
        with self._runtime_connection() as conn:
            row = conn.execute(
                "SELECT * FROM acceptance_contracts WHERE contract_id = ?", (contract_id,)
            ).fetchone()
        if row is None:
            return None
        return {
            "contract_id": row["contract_id"],
            "task_run_id": row["task_run_id"],
            "attempt_id": row["attempt_id"],
            "status": row["status"],
            "compiled": json.loads(row["compiled_json"] or "{}"),
            "digest": row["digest"],
            "inert_legacy": json.loads(row["inert_legacy_json"] or "{}"),
            "frozen_at": row["frozen_at"],
        }

    def current_contract(self, task_run_id: str) -> dict[str, Any] | None:
        with self._runtime_connection() as conn:
            row = conn.execute(
                "SELECT current_contract_id FROM task_runs WHERE task_run_id = ?",
                (task_run_id,),
            ).fetchone()
        if row is None or not row["current_contract_id"]:
            return None
        return self.contract_by_id(str(row["current_contract_id"]))

    def create_validator_operation(
        self,
        *,
        attempt_id: str,
        agent_run_id: str,
        contract_id: str = "",
        validator_ref: str = "",
        validator_kind: str = "",
        code_digest: str = "",
        argv: list[str] | None = None,
        env: dict[str, str] | None = None,
        artifact_digests: list[str] | None = None,
    ) -> str:
        """登记一次 validator 执行（A.8：追到具体 attempt_id）。"""
        operation_id = new_id("validator_operation_id")
        now = time.time()
        with self._runtime_connection() as conn:
            conn.execute(
                """
                INSERT INTO validator_operations(
                    operation_id, attempt_id, agent_run_id, contract_id,
                    validator_ref, validator_kind, status, code_digest,
                    argv_json, env_json, artifact_digests_json,
                    started_at, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    attempt_id,
                    agent_run_id,
                    contract_id,
                    validator_ref,
                    validator_kind,
                    code_digest,
                    json.dumps(list(argv or []), ensure_ascii=False),
                    json.dumps(dict(env or {}), ensure_ascii=False, sort_keys=True),
                    json.dumps(list(artifact_digests or []), ensure_ascii=False),
                    now,
                    now,
                    now,
                ),
            )
            conn.commit()
        return operation_id

    def settle_validator_operation(
        self,
        operation_id: str,
        *,
        status: str,
        stdout_text: str = "",
        stderr_text: str = "",
        exit_code: int = -1,
        argv: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        """落终态（VERIFIED/FAILED/UNAVAILABLE/BLOCKED）。已终态二次 settle 拒绝。

        argv/env 可选回填（§6：process validator 实际 argv 在 run 后才可知，
        pure 执行前已记 argv=[]；此处保留真实执行面）。
        """
        with self.transaction() as conn:  # CAS 失败 raise 时回滚
            row = conn.execute(
                "SELECT status FROM validator_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise RuntimeConflictError(f"validator_operation 不存在: {operation_id}")
            if row["status"] != "PENDING":
                raise RuntimeConflictError(
                    f"validator_operation 已终态({row['status']}): {operation_id}"
                )
            if argv is not None:
                conn.execute(
                    "UPDATE validator_operations SET argv_json = ? WHERE operation_id = ?",
                    (json.dumps(list(argv), ensure_ascii=False), operation_id),
                )
            if env is not None:
                conn.execute(
                    "UPDATE validator_operations SET env_json = ? WHERE operation_id = ?",
                    (json.dumps(dict(env), ensure_ascii=False, sort_keys=True), operation_id),
                )
            conn.execute(
                """
                UPDATE validator_operations
                SET status = ?, stdout_text = ?, stderr_text = ?, exit_code = ?,
                    settled_at = ?, updated_at = ?
                WHERE operation_id = ?
                """,
                (
                    status,
                    stdout_text,
                    stderr_text,
                    exit_code,
                    time.time(),
                    time.time(),
                    operation_id,
                ),
            )

    def artifact_records_for_attempt(self, attempt_id: str) -> list[dict[str, Any]]:
        """本 attempt 已发布 artifact 的内容寻址记录（H.9 validator 输入源）。

        G2 补：主键独立 artifact_record_id，digest 是内容 hash；content_path
        指向内容寻址 store（validator 只读该对象）。旧记录（迁移前）无
        content_path → 回退 live 共享区读取（load_artifact_snapshot 兼容）。
        """
        with self._runtime_connection() as conn:
            rows = conn.execute(
                "SELECT artifact_record_id, rel_path, content_digest, content_path, "
                "size FROM artifact_records WHERE attempt_id = ? ORDER BY rel_path",
                (attempt_id,),
            ).fetchall()
        return [
            {
                "artifact_record_id": str(row["artifact_record_id"]),
                "rel_path": str(row["rel_path"]),
                "digest": str(row["content_digest"]),
                "content_path": str(row["content_path"]),
                "size": int(row["size"]),
            }
            for row in rows
        ]

    def validator_operation(self, operation_id: str) -> dict[str, Any] | None:
        with self._runtime_connection() as conn:
            row = conn.execute(
                "SELECT * FROM validator_operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
        if row is None:
            return None
        return {
            "operation_id": row["operation_id"],
            "attempt_id": row["attempt_id"],
            "agent_run_id": row["agent_run_id"],
            "contract_id": row["contract_id"],
            "validator_ref": row["validator_ref"],
            "validator_kind": row["validator_kind"],
            "status": row["status"],
            "code_digest": row["code_digest"],
            "argv": json.loads(row["argv_json"] or "[]"),
            "env": json.loads(row["env_json"] or "{}"),
            "artifact_digests": json.loads(row["artifact_digests_json"] or "[]"),
            "stdout_text": row["stdout_text"],
            "stderr_text": row["stderr_text"],
            "exit_code": row["exit_code"],
        }
