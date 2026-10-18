# LLM: 文件短事务改由common运输原平台端口，仍先原T/P3/P5再业务锁；未结typed事件不得吞，Count0不增I/O。
# LLM: 执行器身份属于exact attempt元数据；ASK未决及HCM缺来源typed原样透传，内存登记只证明原执行区间。
# 不按心跳、token 速度或 lease 年龄判死；状态投影和通知仍由子代理收口服务负责。
#   启动指纹统一取自 common.heartbeat.process_start_time（跨平台字符串），比较走 start_time_matches
#   （旧数字记录与字符串指纹双口径，不可核验不报替换）。
# 模块用途: 记录真实执行边界，识别进程退出和同宿主执行器退出，避免把活着的 Gateway 当成活着的子代理。
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from ..common.heartbeat import process_start_time, start_time_matches
from ..common.request_execution_ports import (
    RequestExecutionContextUnavailable,
    gateway_short_operation,
    raise_if_transport_uncertain,
)
from ..scheduler.repository import _process_state
from .operations import RuntimeConflictError

_EPOCH = uuid.uuid4().hex


# LLM: 本进程一个执行区间的登记：线程用来判断区间是否还活着，repo/run_id/attempt_id 只给停机打记号时找到它自己的 attempt。
# 类用途: 记住本进程里正在执行的一个子代理执行区间。
@dataclass(frozen=True)
class _ExecutorScope:
    thread: threading.Thread
    repo: Any
    run_id: str
    attempt_id: str


_ACTIVE: dict[str, _ExecutorScope] = {}
_LOCK = threading.RLock()
_SCHEMA = "attempt-executor.v1"
_LOGGER = logging.getLogger(__name__)


# LLM: 登记先于执行；finally退出事实仍消费同source，ASK/HCM来源拒绝须typed透传，不能用日志吞掉撤权。
# 函数用途: 记录原执行区间的出生和退出；来源丢失时保留责任并向上传播。
@contextmanager
def attempt_executor(repo: Any, run_id: str, attempt_id: str):
    if repo is None:
        yield
        return
    token = uuid.uuid4().hex
    executor = {
        "schema_version": _SCHEMA, "token": token, "process_epoch": _EPOCH,
        "pid": os.getpid(), "start_time": process_start_time(os.getpid()),
        "status": "running", "started_at": time.time(),
    }
    with _LOCK:
        _ACTIVE[token] = _ExecutorScope(threading.current_thread(), repo, run_id, attempt_id)
    try:
        _write_executor(repo, run_id, attempt_id, executor, starting=True)
        try:
            yield
        finally:
            with _LOCK:
                _ACTIVE.pop(token, None)
            try:
                _write_executor(repo, run_id, attempt_id,
                                {**executor, "status": "exited", "ended_at": time.time()}, starting=False)
            except RequestExecutionContextUnavailable:
                raise
            except Exception as error:
                raise_if_transport_uncertain(error)
                _LOGGER.exception("执行器退出事实写入失败: run_id=%s attempt_id=%s", run_id, attempt_id)
    finally:
        with _LOCK:
            _ACTIVE.pop(token, None)


# LLM: 整段元数据事务由原短 callback 覆盖；current attempt/token CAS 在同窗内保持，来源拒绝不可写普通失败。
# 函数用途: 把出生或退出事实交原平台短提交，不新建队列、不改 run/工具状态，Count0 仍直接执行。
def _write_executor(repo, run_id: str, attempt_id: str, executor: dict, *, starting: bool) -> None:
    return gateway_short_operation("child_executor_record",
        lambda: _write_executor_record(repo, run_id, attempt_id, executor, starting=starting))


# LLM: 此leaf完整在原callback短门里，原repo事务/current attempt/token CAS与退出元数据原样保持，不授任何业务权限。
# 函数用途: 在调用者原短提交内保存同一个执行器的出生或退出记录。
def _write_executor_record(repo, run_id, attempt_id, executor, *, starting):
    with repo.transaction() as conn:
        row = conn.execute(
            "SELECT a.metadata_json, a.status, a.agent_run_id FROM agent_attempts a "
            "JOIN agent_runs r ON r.agent_run_id=a.agent_run_id "
            "WHERE r.run_id=? AND r.current_attempt_id=a.attempt_id AND a.attempt_id=?",
            (run_id, attempt_id),
        ).fetchone()
        if row is None:
            if starting:
                raise RuntimeConflictError("执行器登记的 attempt 已不是 current")
            return
        metadata = json.loads(row["metadata_json"] or "{}")
        previous = metadata.get("executor") or {}
        if starting:
            if row["status"] != "running" or (previous and not executor_exit_reason(metadata)):
                raise RuntimeConflictError("执行器重复登记或 attempt 已结束")
        elif previous.get("token") != executor["token"]:
            return
        metadata["executor"] = executor
        conn.execute("UPDATE agent_attempts SET metadata_json=? WHERE attempt_id=?",
                     (json.dumps(metadata, ensure_ascii=False), attempt_id))


# LLM: 只接受持久退出、同进程注册区间结束、OS 死亡或 PID 复用；外部活进程内的未知线程保持保守。
# 函数用途: 返回可核验的执行器退出原因；空串表示仍活着或证据不足。
def executor_exit_reason(metadata: dict[str, Any]) -> str:
    executor = metadata.get("executor")
    if isinstance(executor, dict) and executor.get("schema_version") == _SCHEMA:
        if executor.get("status") == "exited" and float(executor.get("ended_at") or 0) > 0:
            return "executor_returned_without_result"
        pid = int(executor.get("pid") or 0)
        start_time = executor.get("start_time")
        if pid == os.getpid() and executor.get("process_epoch") == _EPOCH:
            token = str(executor.get("token") or "")
            if not token:
                return ""
            with _LOCK:
                scope = _ACTIVE.get(token)
                return "" if scope is not None and scope.thread.is_alive() else "executor_scope_exited"
    else:
        pid = int(metadata.get("runner_pid") or 0)
        start_time = metadata.get("runner_start_time")
    if pid <= 0:
        return ""
    state = _process_state(pid)
    if state == "dead":
        return "executor_process_died"
    if state == "alive" and start_time is not None:
        current = process_start_time(pid)
        if not start_time_matches(start_time, current):
            return "executor_process_replaced"
    return ""


# LLM: 扫描必须重复核对 current pointer；未登记、正常等待、旧 attempt 和状态未知都不是无结果退出。
# 函数用途: 读取退出证据与未决工具副作用；不会改状态，也不会读模型文本。
def exited_attempt_facts(repo: Any, run_id: str, attempt_id: str) -> dict[str, Any] | None:
    if repo is None or not attempt_id:
        return None
    with repo._runtime_connection() as conn:
        row = conn.execute(
            "SELECT a.metadata_json, a.status, a.agent_run_id, r.status AS run_status "
            "FROM agent_attempts a JOIN agent_runs r ON r.agent_run_id=a.agent_run_id "
            "WHERE r.run_id=? AND r.current_attempt_id=a.attempt_id AND a.attempt_id=?",
            (run_id, attempt_id),
        ).fetchone()
        if row is None or row["status"] not in {"running", "done", "unknown"}:
            return None
        if row["run_status"] not in {"", "created", "unknown"}:
            return None
        metadata = json.loads(row["metadata_json"] or "{}")
        reason = executor_exit_reason(metadata)
        if not reason:
            return None
        uncertain = conn.execute(
            "SELECT 1 FROM tool_operations WHERE attempt_id=? AND "
            "(status IN ('EXECUTING','UNKNOWN') OR (status='CLAIMED' AND handler_started_at>0)) LIMIT 1",
            (attempt_id,),
        ).fetchone() is not None
        return {"reason": reason, "run_id": run_id, "attempt_id": attempt_id,
                "agent_run_id": str(row["agent_run_id"]), "uncertain_effects": uncertain,
                "host_shutdown": _host_shutdown_marked(metadata)}


# LLM: 宿主优雅停机时调用（Gateway 收尾，关闭模型调用准入之后）：给本进程仍在执行区间里的每个执行器，在它自己的 attempt
#   元数据上按 token CAS 记 executor.host_shutdown（pid + 时间）。不改 status、不判死：线程若还来得及自己收尾，照常写退出与结果；
#   进程退出把它带走时，重启收尾（exited_attempt_facts → recover_exited_runner）凭这笔事实按“宿主停机中断”收口。
#   只覆盖本进程的执行器（子进程 runner 不在 _ACTIVE 里，不受影响）；kill -9 这类非正常退出不会走到这里，没有记号。
#   单个写入失败只记日志、继续下一个，不能卡住停机。改动同步 test_executor_exit_recovery。
# 函数用途: 停机时给本进程在跑的子代理执行器打上“被宿主停机中断”的记号，返回打上记号的个数（写库副作用）。
def mark_in_process_executors_host_shutdown() -> int:
    with _LOCK:
        scopes = list(_ACTIVE.items())
    marked = 0
    for token, scope in scopes:
        try:
            marked += _mark_host_shutdown(token, scope)
        except Exception:
            _LOGGER.exception("执行器停机记号写入失败: run_id=%s attempt_id=%s", scope.run_id, scope.attempt_id)
    return marked


# LLM: 只改仍是 current attempt、token 仍是这个执行区间、状态仍为 running 的记录；别的执行器或已退出的记录不动。
# 函数用途: 在一个执行区间自己的 attempt 元数据上写停机记号，写了返回 1，否则 0。
def _mark_host_shutdown(token: str, scope: _ExecutorScope) -> int:
    with scope.repo.transaction() as conn:
        row = conn.execute(
            "SELECT a.metadata_json FROM agent_attempts a JOIN agent_runs r ON r.agent_run_id=a.agent_run_id "
            "WHERE r.run_id=? AND r.current_attempt_id=a.attempt_id AND a.attempt_id=?",
            (scope.run_id, scope.attempt_id),
        ).fetchone()
        metadata = json.loads(row["metadata_json"] or "{}") if row is not None else {}
        executor = metadata.get("executor")
        if not isinstance(executor, dict) or executor.get("token") != token or executor.get("status") != "running":
            return 0
        metadata["executor"] = {**executor, "host_shutdown": {"pid": os.getpid(), "marked_at": time.time()}}
        conn.execute("UPDATE agent_attempts SET metadata_json=? WHERE attempt_id=?",
                     (json.dumps(metadata, ensure_ascii=False), scope.attempt_id))
    return 1


# LLM: 只认 mark_in_process_executors_host_shutdown 写下的结构化字段，不读任何文本。
# 函数用途: 判断一条 attempt 的执行器是不是在宿主优雅停机时被打过记号。
def _host_shutdown_marked(metadata: dict[str, Any]) -> bool:
    executor = metadata.get("executor")
    return isinstance(executor, dict) and isinstance(executor.get("host_shutdown"), dict)


# LLM: 执行器已退出也不能猜工具副作用；unknown 保留执行锁并阻止自动重跑，通知由正式结果链补齐。
# 函数用途: 精确封存有未决工具的退出轮，避免重新执行可能已生效的动作。
def mark_exited_attempt_unknown(repo: Any, facts: dict[str, Any]) -> None:
    with repo.transaction() as conn:
        changed = conn.execute(
            "UPDATE agent_attempts SET status='unknown', ended_at=? WHERE attempt_id=? "
            "AND status IN ('running','done') AND EXISTS (SELECT 1 FROM agent_runs "
            "WHERE agent_run_id=? AND current_attempt_id=?)",
            (time.time(), facts["attempt_id"], facts["agent_run_id"], facts["attempt_id"]),
        ).rowcount
        if changed:
            repo._append_event_conn(
                conn, event_type="attempt_unknown_terminal", attempt_id=facts["attempt_id"],
                agent_run_id=facts["agent_run_id"], payload={**facts, "status": "unknown"},
            )
        conn.execute(
            "UPDATE agent_runs SET status='unknown', updated_at=? WHERE agent_run_id=? "
            "AND current_attempt_id=? AND status IN ('','created')",
            (time.time(), facts["agent_run_id"], facts["attempt_id"]),
        )
