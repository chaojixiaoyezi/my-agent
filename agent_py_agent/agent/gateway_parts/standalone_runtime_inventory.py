# LLM: 同一原ro事务读取规范执行责任行，不以SQLite/WAL文件hash作业务CAS，不恢复或结算任何状态。
# 模块用途: 迁移时区分实际未结执行与可保留的历史任务、未来输入和工作区预留。
from __future__ import annotations

import hashlib
import json
import os

from ..common.nofollow_fs import open_readonly_file_beneath
from ..runtime_db.operations import (
    AGENT_RUN_TERMINAL_STATUSES,
    ATTEMPT_STATUS_PENDING,
    OP_CANCELLED,
    OP_FAILED,
    OP_SUCCEEDED,
    PUB_COMMITTED,
    RUN_STATUS_LEGACY_CREATED,
)
from ..runtime_db.repository import _QUIESCENT_ATTEMPT_STATUSES, RuntimeRepository
from ..runtime_db.schema import runtime_db_path
from .standalone_migration_errors import StandaloneMigrationRejected

_COLUMNS = {
    "tasks": "task_id,owner_id,thread_id,conversation_task_id",
    "task_runs": "task_run_id,task_id,status",
    "agent_runs": "agent_run_id,task_run_id,parent_agent_run_id,run_id,current_attempt_id,current_attempt_generation,status",
    "agent_attempts": "attempt_id,agent_run_id,attempt_generation,status,ended_at",
    "tool_operations": "operation_id,agent_run_id,attempt_id,attempt_generation,status,handler_started_at,settled_at",
    "resource_locks": "lock_id,canonical_scope,attempt_id,attempt_generation,pid,start_token,lease_expires_at",
    "workspace_bindings": "binding_id,owner_id,agent_run_id,attempt_id,root_path,status,workspace_epoch,roots_digest",
    "root_claims": "claim_id,binding_id,agent_run_id,attempt_id,root_path",
    "resource_mutations": "mutation_id,canonical_scope,state,attempt_id,version",
    "publish_operations": "publish_id,binding_id,agent_run_id,attempt_id,status",
    "wake_queue": "wake_id,root_task_id,root_run_id,root_thread_id,status,next_due_at,lease_until",
}


# LLM: 序列只包含规范行结构；不返回goal/outcome/prompt，不按文件页序、WAL或mtime推断业务。
# 函数用途: 生成可首末比较的内容摘要。
def responsibility_digest(value) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


# LLM: 所有拒绝都保留原账，不自动清锁、recover或把unknown变终态。
# 函数用途: 为准确owner/ref提供恢复动作。
def _reject(reason, ref):
    raise StandaloneMigrationRejected(reason, "保持数量0，用原恢复入口收口该执行责任，再正常停止后迁移。", ref)


# LLM: canonical根先nofollow打开并保原FD，原existing/ro连接不初始化或迁移；同一BEGIN事务读全行且锁忙即拒。
# 函数用途: 读取一个owner的原运行库，验证身份/refs并返回结构摘要与native引用索引。
def runtime_responsibility_snapshot(owner, *, prepare_read_facilities=False):
    path = runtime_db_path(owner.home_dir)
    try:
        descriptor = open_readonly_file_beneath(owner.home_dir, (path.name,))
    except FileNotFoundError:
        return {"digest": None, "tasks": {}, "executions": {}}
    try:
        origin = os.fstat(descriptor)
        facilities = _sqlite_facilities(owner.home_dir, path.name, descriptor, prepare_read_facilities)
        repo = RuntimeRepository(path, read_only=True, existing_only=True)
        with repo._runtime_connection() as conn:
            conn.execute("BEGIN")
            rows = {table: [dict(row) for row in conn.execute("SELECT " + columns + " FROM " + table)]
                    for table, columns in _COLUMNS.items()}
            # 设施阶段只准备原SQLite读口，不签责任；随后两次正式快照各自完整校验。
            if not prepare_read_facilities:
                _validate_runtime_rows(rows, owner.owner_id)
            _same_database(owner.home_dir, path.name, origin)
        _same_database(owner.home_dir, path.name, origin)
        latest = _sqlite_facilities(owner.home_dir, path.name, descriptor, False)
        if not prepare_read_facilities and latest != facilities:
            _reject("runtime_read_facilities_changed", path.name)
    finally:
        os.close(descriptor)
    for table in rows:
        rows[table].sort(key=responsibility_digest)
    tasks = {row["task_id"]: row for row in rows["tasks"]}
    runs = {row["agent_run_id"]: row for row in rows["agent_runs"]}
    task_runs = {row["task_run_id"]: row for row in rows["task_runs"]}
    executions = {}
    for attempt in rows["agent_attempts"]:
        run = runs[attempt["agent_run_id"]]
        task = tasks[task_runs[run["task_run_id"]]["task_id"]]
        executions[attempt["attempt_id"]] = (run["run_id"], task["task_id"], task["thread_id"])
    return {"digest": responsibility_digest(rows), "tasks": tasks, "executions": executions}


# LLM: WAL不可忽略或immutable读取；后继不创建缺失设施，只有真实Count0停止writer先沿原ro API准备，设施不是业务证据。
# 函数用途: 在SQLite连接前核准确辅助叶类型，缺sidecar时给正常0桥接动作，不跟链接或恢复journal。
def _sqlite_facilities(root, name, descriptor, prepare):
    found = {}
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            current = open_readonly_file_beneath(root, (name + suffix,))
        except FileNotFoundError:
            found[suffix] = None
        else:
            info = os.fstat(current)
            found[suffix] = (info.st_dev, info.st_ino)
            os.close(current)
    if found["-journal"]:
        _reject("runtime_journal_requires_original_recovery", name + "-journal")
    os.lseek(descriptor, 0, os.SEEK_SET)
    header = os.read(descriptor, 100)
    if header[:16] != b"SQLite format 3\x00":
        _reject("runtime_database_header_invalid", name)
    if header[18:20] == b"\x02\x02" and not prepare and not (found["-wal"] and found["-shm"]):
        _reject("runtime_read_facilities_missing", name)
    return found


# LLM: 同一原库路径首末须仍是最初FD的实例；不只看SQLite逻辑内容而忽略路径被换。
# 函数用途: 拒绝数据库inode、父链或叶类型在只读期间变化。
def _same_database(root, name, origin):
    current = open_readonly_file_beneath(root, (name,))
    try:
        info = os.fstat(current)
        if (info.st_dev, info.st_ino) != (origin.st_dev, origin.st_ino):
            _reject("runtime_source_changed", name)
    finally:
        os.close(current)


# LLM: task/run只收原created/legacy空串及原终态常量，未知协议拒；已知非终态不等于物理busy，仍核实际attempt/op/lock/refs。
# 函数用途: 在同一事务内拒绝未结、UNKNOWN与断ref，不修改历史或未来意图。
def _validate_runtime_rows(rows, owner_id):
    tasks = {row["task_id"]: row for row in rows["tasks"]}
    task_runs = {row["task_run_id"]: row for row in rows["task_runs"]}
    runs = {row["agent_run_id"]: row for row in rows["agent_runs"]}
    attempts = {row["attempt_id"]: row for row in rows["agent_attempts"]}
    bindings = {row["binding_id"]: row for row in rows["workspace_bindings"]}
    for task in tasks.values():
        if task["owner_id"] != owner_id:
            _reject("runtime_owner_conflict", task["task_id"])
    for run in task_runs.values():
        if run["status"] not in RUN_STATUS_LEGACY_CREATED | AGENT_RUN_TERMINAL_STATUSES:
            _reject("runtime_task_run_status_unverifiable", run["task_run_id"])
        if run["task_id"] not in tasks:
            _reject("runtime_task_ref_missing", run["task_run_id"])
    for run in runs.values():
        if run["status"] not in RUN_STATUS_LEGACY_CREATED | AGENT_RUN_TERMINAL_STATUSES:
            _reject("runtime_agent_run_status_unverifiable", run["agent_run_id"])
        current = attempts.get(run["current_attempt_id"])
        if (run["task_run_id"] not in task_runs or current is None
                or current["agent_run_id"] != run["agent_run_id"]
                or current["attempt_generation"] != run["current_attempt_generation"]
                or run["parent_agent_run_id"] and run["parent_agent_run_id"] not in runs):
            _reject("runtime_run_ref_missing", run["agent_run_id"])
    for attempt in attempts.values():
        if (attempt["agent_run_id"] not in runs
                or attempt["status"] not in _QUIESCENT_ATTEMPT_STATUSES | {ATTEMPT_STATUS_PENDING}
                or attempt["status"] != ATTEMPT_STATUS_PENDING and not attempt["ended_at"]):
            _reject("runtime_attempt_unsettled", attempt["attempt_id"])
    for op in rows["tool_operations"]:
        attempt = attempts.get(op["attempt_id"])
        if (attempt is None or op["agent_run_id"] != attempt["agent_run_id"]
                or op["attempt_generation"] != attempt["attempt_generation"]
                or op["status"] not in {OP_SUCCEEDED, OP_FAILED, OP_CANCELLED}):
            _reject("runtime_operation_unsettled", op["operation_id"])
    for lock in rows["resource_locks"]:
        _reject("runtime_execution_lock_present", lock["lock_id"])
    for binding in bindings.values():
        attempt = attempts.get(binding["attempt_id"])
        if (binding["owner_id"] != owner_id or attempt is None
                or binding["agent_run_id"] != attempt["agent_run_id"]):
            _reject("runtime_binding_ref_missing", binding["binding_id"])
    for claim in rows["root_claims"]:
        binding = bindings.get(claim["binding_id"])
        if binding is None or any(claim[key] != binding[key] for key in ("agent_run_id", "attempt_id", "root_path")):
            _reject("runtime_claim_ref_missing", claim["claim_id"])
    for mutation in rows["resource_mutations"]:
        if mutation["state"] != "STABLE" or mutation["attempt_id"] and mutation["attempt_id"] not in attempts:
            _reject("runtime_mutation_unsettled", mutation["mutation_id"])
    for publish in rows["publish_operations"]:
        if publish["status"] != PUB_COMMITTED or publish["attempt_id"] not in attempts or publish["binding_id"] not in bindings:
            _reject("runtime_publish_unsettled", publish["publish_id"])
    for wake in rows["wake_queue"]:
        if wake["root_task_id"] not in tasks:
            _reject("runtime_wake_ref_missing", wake["wake_id"])
