
from __future__ import annotations

"""后台进程注册表 —— 让模型能管住 run_command(run_in_background=true) 起的后台进程。

学标杆 长期助手 tools/process_registry.py 的 ProcessSession + ProcessRegistry 设计
(输出滚动缓冲、状态轮询 poll()、按 ID 杀进程组、列表),裁剪适配 my-agent:

适配差异(为什么不照搬 长期助手):
  - my-agent 的 run_command 后台路径已经把 stdout/stderr 落到 .background_jobs/*.log
    日志文件(handle 写盘),不像 长期助手 用 stdout=PIPE + 后台 reader 线程实时读。
    所以这里"滚动输出缓冲"= 查询时惰性读日志文件尾部,不另起常驻 reader 线程
    (零额外线程,CI 友好,不引入 reader 卡死/孤儿线程那一类坑)。
  - 状态更新惰性化:不起后台轮询线程,而是在 list/status/kill 被调用时用
    Popen.poll() 或持久 PID 出生标识收割状态。显式后台命令由独立 host 持有，
    所以 one-shot 子代理结束后沙箱进程不会被 bwrap --die-with-parent 一起收掉；
    主代理、子代理和 Gateway 通过同一受保护记录继续管理它。
  - 不做自动重启后台命令：host 和持久记录只保证跨 one-shot runner 的生命周期、
    查询与停止；主机崩溃后保持真实终态，不凭旧记录擅自重放有副作用的命令。

杀进程组(避免杀父留子):后台 Popen 用 start_new_session=True 建独立进程组
(POSIX setsid),kill 时对整个进程组发信号(os.killpg(os.getpgid(pid))),
SIGTERM 宽限后再 SIGKILL;Windows 用 taskkill /T /F 杀进程树。
"""

# LLM: 本模块是后台 shell 的唯一进程事实源；任何模型可见的查询或停止入口都必须
# 携带 host 注入的 ProcessAccessScope，不能仅凭可猜的 session_id 访问全局记录；
# 终止回执必须区分已发信号与已确认退出，供前台超时和后台停止共用。
# 模块用途: 登记后台命令、隔离查询并终止进程树，保留实际退出核对结果而不凭信号宣称成功。

import json
import os
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..common.json_io import read_json_object
from .background_process_host import HOST_STATE_SCHEMA
from .process_session_store import (
    PROCESS_SESSION_SCHEMA,
    ProcessSessionStore,
)

_IS_WINDOWS = os.name == "nt"

# 杀进程时 SIGTERM 到 SIGKILL 的宽限秒数。先礼(SIGTERM 让进程自己清理)后兵
# (还活着就 SIGKILL 硬杀整组),避免留下孤儿子进程。
_KILL_GRACE_SECONDS = 3.0


# LLM: 该回执只证明本次观察到的进程树是否已终止，不证明命令成功或外部系统已回滚。
# 类用途: 把终止方式、退出码和无法确认的进程带回 shell 与后台进程控制调用方。
@dataclass(frozen=True)
class ProcessTerminationReceipt:
    method: str
    confirmed: bool
    return_code: int | None
    observed_processes: int
    unresolved_pids: tuple[int, ...] = ()
# 滚动输出缓冲:查询时从日志文件尾部最多读这么多字符,够看进度/结论又不撑爆 prompt。
_OUTPUT_TAIL_CHARS = 4000
# 最多保留多少条已结束的进程记录,超了按启动时间淘汰最老的(防内存无界增长)。
_MAX_FINISHED = 128


# LLM: Scope equality is the authorization test; do not add fuzzy path ancestry or natural-language
# identity fallback here. Empty fields deliberately fail the model-facing bound check.
# 类用途: 保存一条后台进程可被哪个用户、哪个 TUI 会话访问的不可变身份。
@dataclass(frozen=True)
class ProcessAccessScope:
    """后台进程的可信访问边界；空 conversation_id 表示不能暴露给模型工具。"""

    owner_id: str = ""
    conversation_id: str = ""
    owner_home: str = ""

    # LLM: Both owner and conversation identity are required before model-visible access.
    # 函数用途: 判断这份范围是否足以安全地访问共享 Gateway 里的后台进程。
    def is_bound(self) -> bool:
        return bool(self.owner_id and self.conversation_id)


# LLM: Process registration is one immutable launch fact. Bundle it before the
# registry lock so call sites cannot shift positional fields or grow another
# parallel registration signature; access scope and store root remain typed.
# 类用途: 汇总刚启动后台 host 的登记参数，交给唯一 ProcessRegistry 入口原子落盘。
@dataclass(frozen=True)
class ProcessRegistration:
    command: str
    pid: int
    output_file: str
    process: subprocess.Popen | None = None
    cwd: str = ""
    session_id: str | None = None
    access_scope: ProcessAccessScope | None = None
    child_pid: int = 0
    pid_birth_token: str = ""
    host_state_file: str = ""
    store_root: str | Path | None = None


# LLM: Scope 只能从 executor 注入的 run_scope 和 registry 的 owner home 构造；
# 选择 session -> root task -> root run -> run 的稳定降级顺序，以便主子代理在同一
# 对话树内协作，同时隔离其他用户、其他 TUI 会话和无法证明归属的裸调用。
# 函数用途: 把本轮可信身份整理成后台进程查询和停止所用的精确访问范围。
def process_access_scope(
    run_scope: object,
    owner_scope_root: object = "",
) -> ProcessAccessScope:
    scope = run_scope if isinstance(run_scope, dict) else {}
    owner_home = ""
    if owner_scope_root:
        owner_home = str(Path(str(owner_scope_root)).expanduser().resolve(strict=False))
    owner_id = str(scope.get("owner_id") or "").strip()
    if not owner_id and owner_home:
        owner_id = f"path:{owner_home}"
    conversation_id = next(
        (
            str(scope.get(key) or "").strip()
            for key in ("session_id", "root_task_id", "root_run_id", "run_id")
            if str(scope.get(key) or "").strip()
        ),
        "",
    )
    return ProcessAccessScope(
        owner_id=owner_id,
        conversation_id=conversation_id,
        owner_home=owner_home,
    )


# LLM: 记录既保存进程生命周期事实，也保存启动时的不可变访问范围；后续查询不得
# 用当前工作目录猜归属。新增生命周期字段时同步 summary 投影和进程工具测试。
# 类用途: 保存一条后台命令的进程、日志、状态和所属用户会话。
@dataclass
class BackgroundProcess:
    """一个被登记的后台进程。对标 长期助手 ProcessSession,裁剪到 my-agent 够用的字段。"""

    session_id: str
    command: str
    pid: int
    started_at: float
    access_scope: ProcessAccessScope = field(default_factory=ProcessAccessScope)
    cwd: str = ""
    output_file: str = ""
    process: subprocess.Popen | None = None  # 父进程 Popen 句柄(同进程内才有)
    child_pid: int = 0
    pid_birth_token: str = ""
    host_state_file: str = ""
    store_root: str = ""
    status: str = "running"  # running / exited / killed
    exit_code: int | None = None
    finished_at: float | None = None

    # LLM: Terminal is a typed host fact; model text and host-state prose never
    # participate in this transition.
    # 函数用途: 判断受管后台会话是否已经结束或被明确停止。
    def is_terminal(self) -> bool:
        return self.status in {"exited", "killed"}

    # LLM: Summary intentionally exposes only the stable session handle. Host,
    # command-entry and birth-token PIDs are lifecycle internals: descendants may
    # fork again, so none of them identifies the process that owns a resource.
    # 函数用途: 生成 process_session 返回给模型的简短状态和可选日志尾部。
    def to_summary(self, *, include_output: bool = False, output_tail_chars: int = _OUTPUT_TAIL_CHARS) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "session_id": self.session_id,
            "command": self.command[:200],
            "status": self.status,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.started_at)),
            "uptime_seconds": int((self.finished_at or time.time()) - self.started_at),
            "output_file": self.output_file,
        }
        if self.exit_code is not None:
            summary["exit_code"] = self.exit_code
        if include_output:
            summary["output_tail"] = _read_log_tail(self.output_file, output_tail_chars)
        return summary

    # LLM: Durable serialization carries the exact immutable scope and birth
    # token. Popen handles are process-local and must never be serialized.
    # 函数用途: 把内存进程对象转成可由其他 agent 进程接管的权威 JSON 记录。
    def to_record(self) -> dict[str, object]:
        return {
            "schema": PROCESS_SESSION_SCHEMA,
            "session_id": self.session_id,
            "command": self.command,
            "pid": self.pid,
            "child_pid": self.child_pid,
            "pid_birth_token": self.pid_birth_token,
            "started_at": self.started_at,
            "access_scope": {
                "owner_id": self.access_scope.owner_id,
                "conversation_id": self.access_scope.conversation_id,
                "owner_home": self.access_scope.owner_home,
            },
            "cwd": self.cwd,
            "output_file": self.output_file,
            "host_state_file": self.host_state_file,
            "status": self.status,
            "exit_code": self.exit_code,
            "finished_at": self.finished_at,
        }

    # LLM: Hydration accepts only ProcessSessionStore-validated payloads and
    # creates a handle-free record whose lifecycle is checked by PID identity.
    # 函数用途: 从另一进程写下的后台会话记录恢复可查询、可停止的内存对象。
    @classmethod
    def from_record(
        cls,
        payload: dict[str, object],
        *,
        store_root: Path,
    ) -> BackgroundProcess:
        scope = dict(payload.get("access_scope") or {})
        exit_code = payload.get("exit_code")
        finished_at = payload.get("finished_at")
        return cls(
            session_id=str(payload["session_id"]),
            command=str(payload.get("command") or ""),
            pid=int(payload["pid"]),
            started_at=float(payload["started_at"]),
            access_scope=ProcessAccessScope(
                owner_id=str(scope.get("owner_id") or ""),
                conversation_id=str(scope.get("conversation_id") or ""),
                owner_home=str(scope.get("owner_home") or ""),
            ),
            cwd=str(payload.get("cwd") or ""),
            output_file=str(payload.get("output_file") or ""),
            child_pid=int(payload.get("child_pid") or 0),
            pid_birth_token=str(payload.get("pid_birth_token") or ""),
            host_state_file=str(payload.get("host_state_file") or ""),
            store_root=str(store_root),
            status=str(payload.get("status") or "running"),
            exit_code=int(exit_code) if exit_code is not None else None,
            finished_at=float(finished_at) if finished_at is not None else None,
        )


def _read_log_tail(output_file: str, max_chars: int) -> str:
    """惰性读日志文件尾部作为滚动输出缓冲。日志读不到不报错(返回空串),
    因为进程已启动是事实,日志缺失只影响观测不影响管控。"""
    if not output_file or max_chars <= 0:
        return ""
    try:
        path = Path(output_file)
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > max_chars:
                handle.seek(size - max_chars)
            raw = handle.read()
    except OSError:
        return ""
    text = raw.decode("utf-8", errors="replace")
    if size > max_chars:
        # 从中间截断的,丢掉第一行残片避免半个字符/半行误导。
        newline = text.find("\n")
        if newline != -1:
            text = text[newline + 1 :]
        text = f"...[日志已截断,只显示尾部 {len(text)} 字符]...\n{text}"
    return text


# LLM: Registry is a process-local cache over ProcessSessionStore. Every
# model-visible call must provide the exact access_scope and store_root; None is
# reserved for same-process compatibility and tests. Never reintroduce an
# in-memory-only authority because subagent runners are intentionally one-shot.
# 类用途: 跨进程登记、等待、列出和终止后台命令，并按用户会话精确隔离。
class ProcessRegistry:
    """后台进程会话注册表；内存做加速，受保护 JSON 是跨进程权威。"""

    # LLM: The lock protects this process's cache; cross-process serialization is
    # supplied by ProcessSessionStore's per-record file lock.
    # 函数用途: 初始化空的进程会话缓存。
    def __init__(self) -> None:
        self._processes: dict[str, BackgroundProcess] = {}
        self._lock = threading.Lock()

    # LLM: IDs must be collision-resistant across concurrently exiting subagent
    # processes; a process-local counter is insufficient.
    # 函数用途: 生成可读且跨进程不冲突的后台 session id。
    def _next_session_id(self) -> str:
        return f"bg-{int(time.time())}-{uuid.uuid4().hex[:16]}"

    # LLM: access_scope is frozen into the record at launch and cannot be replaced later by a
    # caller-supplied session id; callers without trusted scope remain internal-only records.
    # 函数用途: 登记一条刚启动的后台命令，并返回后续管理所需的 session id。
    def register(self, request: ProcessRegistration) -> BackgroundProcess:
        """登记一个刚启动的后台 host，并在可用时同步写入跨进程权威记录。"""
        with self._lock:
            sid = request.session_id or self._next_session_id()
            birth_token = str(request.pid_birth_token or "") or capture_process_birth_token(
                request.pid
            )
            if request.store_root and not birth_token:
                raise OSError("cannot establish managed process birth identity")
            record = BackgroundProcess(
                session_id=sid,
                command=request.command,
                pid=request.pid,
                started_at=time.time(),
                access_scope=request.access_scope or ProcessAccessScope(),
                cwd=request.cwd,
                output_file=request.output_file,
                process=request.process,
                child_pid=max(0, int(request.child_pid or 0)),
                pid_birth_token=birth_token,
                host_state_file=str(request.host_state_file or ""),
                store_root=(
                    str(Path(request.store_root).resolve(strict=False))
                    if request.store_root
                    else ""
                ),
            )
            self._persist_locked(record)
            self._processes[sid] = record
            self._prune_finished_locked()
            return record

    # LLM: access_scope 非空时必须精确匹配启动记录，错误 scope 与不存在统一返回 None，
    # 避免通过错误差异探测其他用户的 session_id。
    # 函数用途: 在允许的用户会话范围内取得一条后台进程记录。
    def get(
        self,
        session_id: str,
        access_scope: ProcessAccessScope | None = None,
        store_root: str | Path | None = None,
    ) -> BackgroundProcess | None:
        with self._lock:
            return self._visible_record_locked(session_id, access_scope, store_root)

    # LLM: Refresh trusts only the exact Popen handle or the persisted PID birth
    # token. Host state may supply the child exit code only after that host is dead.
    # 函数用途: 惰性核对 host 是否仍是同一进程，结束后补上真实命令退出码并持久化。
    def _refresh_locked(self, record: BackgroundProcess) -> None:
        if record.is_terminal():
            return
        proc = record.process
        return_code: int | None = None
        if proc is not None:
            return_code = proc.poll()
            if return_code is None:
                return
        elif _same_process(record.pid, record.pid_birth_token):
            return
        host_state = _host_terminal_state(record.host_state_file)
        record.status = "exited"
        record.exit_code = (
            int(host_state["exit_code"])
            if host_state.get("exit_code") is not None
            else return_code
        )
        record.finished_at = float(host_state.get("finished_at") or time.time())
        self._persist_locked(record)

    # LLM: status 的日志尾部可能包含外部数据，只能在 scope 匹配后读取。
    # 函数用途: 查询一个后台进程的状态和最近输出。
    def status(
        self,
        session_id: str,
        access_scope: ProcessAccessScope | None = None,
        store_root: str | Path | None = None,
    ) -> dict[str, Any] | None:
        """查单个进程状态 + 最近输出(日志尾部)。不存在返回 None。"""
        with self._lock:
            record = self._visible_record_locked(session_id, access_scope, store_root)
            if record is None:
                return None
            self._refresh_locked(record)
            return record.to_summary(include_output=True)

    # LLM: 模型工具必须传 scope；内部 None 调用仍可看全表用于 Gateway 清理和旧测试。
    # 函数用途: 列出当前用户会话可见的后台进程摘要。
    def list(
        self,
        access_scope: ProcessAccessScope | None = None,
        store_root: str | Path | None = None,
    ) -> list[dict[str, Any]]:
        """列出所有登记的后台进程；跨进程记录会先加载到当前缓存。"""
        rows, _errors = self.list_report(access_scope, store_root)
        return rows

    # LLM: Corrupt store records remain structured diagnostics rather than being
    # silently omitted from the model-facing list response.
    # 函数用途: 列出当前会话后台进程，并同时返回权威文件损坏信息。
    def list_report(
        self,
        access_scope: ProcessAccessScope | None = None,
        store_root: str | Path | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, object]]]:
        with self._lock:
            load_errors = self._hydrate_store_locked(store_root)
            records = [
                record
                for record in self._processes.values()
                if (access_scope is None or record.access_scope == access_scope)
                and _record_matches_store(record, store_root)
            ]
            for record in records:
                self._refresh_locked(record)
            # 运行中的排前面,其次按启动时间倒序(新的在前)。
            records.sort(key=lambda item: (item.is_terminal(), -item.started_at))
            return [record.to_summary(include_output=False) for record in records], load_errors

    # LLM: kill 在拿到 PID 前先做 scope 精确匹配，实际发信号放在锁外；二次回锁时
    # 仍核对同一记录，只有终止回执确认后才登记 killed，不能把已发信号写成退出事实。
    # 函数用途: 停止当前用户会话所属的后台进程及后代，持久化已确认的终态并返回核对信息。
    def kill(
        self,
        session_id: str,
        access_scope: ProcessAccessScope | None = None,
        store_root: str | Path | None = None,
    ) -> dict[str, Any] | None:
        """SIGTERM→(宽限超时)SIGKILL 杀进程组,更新状态。不存在返回 None;
        已结束返回 already_exited。"""
        with self._lock:
            record = self._visible_record_locked(session_id, access_scope, store_root)
            if record is None:
                return None
            self._refresh_locked(record)
            if record.is_terminal():
                return {
                    "session_id": record.session_id,
                    "status": "already_exited" if record.status == "exited" else record.status,
                    "exit_code": record.exit_code,
                    "message": "进程已经结束,无需终止。",
                }
            pid = record.pid
            proc = record.process

        # 真正杀进程在锁外做(killpg + 宽限 + wait 可能耗时,不长占锁阻塞 list/status)。
        termination = terminate_process_tree(pid, proc)

        with self._lock:
            record = self._visible_record_locked(session_id, access_scope, store_root)
            if record is not None:
                if termination.confirmed:
                    record.status = "killed"
                    record.finished_at = time.time()
                    record.exit_code = termination.return_code
                self._persist_locked(record)
                summary = record.to_summary(include_output=False)
                summary["signal"] = termination.method
                summary["termination"] = asdict(termination)
                summary["message"] = (
                    "已确认观察到的进程树终止。" if termination.confirmed
                    else "已尝试终止，但仍无法确认所有进程退出；未登记已停止。"
                )
                return summary
        return None

    # LLM: wait 是 会话运行时 write_stdin 空轮询的有界等价入口；它等待真实 Popen/PID，
    # 不启动 shell sleep、不循环调用模型，超时只返回 running 事实而不伪造失败。
    # 函数用途: 在限定秒数内等待后台命令结束，并返回结束状态或当前进度。
    def wait(
        self,
        session_id: str,
        timeout_seconds: float,
        access_scope: ProcessAccessScope | None = None,
        store_root: str | Path | None = None,
    ) -> dict[str, Any] | None:
        timeout = max(0.0, float(timeout_seconds or 0.0))
        with self._lock:
            record = self._visible_record_locked(session_id, access_scope, store_root)
            if record is None:
                return None
            self._refresh_locked(record)
            if record.is_terminal():
                summary = record.to_summary(include_output=True)
                summary["wait_timed_out"] = False
                return summary
            proc = record.process
            pid = record.pid
            birth_token = record.pid_birth_token

        timed_out = False
        if proc is not None:
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
        else:
            deadline = time.monotonic() + timeout
            while _same_process(pid, birth_token) and time.monotonic() < deadline:
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            timed_out = _same_process(pid, birth_token)

        summary = self.status(session_id, access_scope, store_root)
        if summary is not None:
            summary["wait_timed_out"] = timed_out and summary.get("status") == "running"
        return summary

    # LLM: This is the single scope predicate used before process state or logs are exposed.
    # 函数用途: 在持锁状态下取得当前范围可见的记录。
    def _visible_record_locked(
        self,
        session_id: str,
        access_scope: ProcessAccessScope | None,
        store_root: str | Path | None,
    ) -> BackgroundProcess | None:
        """返回 scope 可见记录；调用方必须持有 _lock。"""
        record = self._processes.get(session_id)
        if record is not None and not _record_matches_store(record, store_root):
            return None
        if record is None and store_root:
            store_path = Path(store_root).expanduser().resolve(strict=False)
            report = ProcessSessionStore(store_path).load(session_id)
            if report.record:
                record = BackgroundProcess.from_record(report.record, store_root=store_path)
                self._processes[record.session_id] = record
        if record is None:
            return None
        if access_scope is not None and record.access_scope != access_scope:
            return None
        return record

    # LLM: Hydration never overwrites a live same-process Popen handle. Persisted
    # rows only fill cache misses, and each row retains its exact store root.
    # 函数用途: 把同一权威目录里的其他 agent 进程记录加载进当前缓存。
    def _hydrate_store_locked(
        self,
        store_root: str | Path | None,
    ) -> list[dict[str, object]]:
        if not store_root:
            return []
        root = Path(store_root).expanduser().resolve(strict=False)
        records, errors = ProcessSessionStore(root).list_records()
        for payload in records:
            session_id = str(payload.get("session_id") or "")
            if session_id and session_id not in self._processes:
                self._processes[session_id] = BackgroundProcess.from_record(
                    payload,
                    store_root=root,
                )
        return errors

    # LLM: Durable writes contain the full record and happen on every terminal
    # transition. A missing store_root deliberately keeps legacy internal tests in-memory.
    # 函数用途: 把当前记录同步到跨进程权威目录。
    def _persist_locked(self, record: BackgroundProcess) -> None:
        if not record.store_root:
            return
        effective = ProcessSessionStore(record.store_root).write(record.to_record())
        effective_status = str(effective.get("status") or record.status)
        if effective_status != record.status:
            record.status = effective_status
            exit_code = effective.get("exit_code")
            finished_at = effective.get("finished_at")
            record.exit_code = int(exit_code) if exit_code is not None else None
            record.finished_at = float(finished_at) if finished_at is not None else None

    def _prune_finished_locked(self) -> None:
        """已结束记录超上限时淘汰最老的。必须持锁调用。"""
        finished = [(sid, rec) for sid, rec in self._processes.items() if rec.is_terminal()]
        overflow = len(finished) - _MAX_FINISHED
        if overflow <= 0:
            return
        finished.sort(key=lambda item: item[1].finished_at or item[1].started_at)
        for sid, _ in finished[:overflow]:
            self._processes.pop(sid, None)
        roots = {record.store_root for _sid, record in finished if record.store_root}
        for root in roots:
            ProcessSessionStore(root).prune_finished(_MAX_FINISHED)

    def clear(self) -> None:
        """清空注册表(测试隔离用;不杀进程,只丢记录)。"""
        with self._lock:
            self._processes.clear()


# LLM: Host state is never used while the recorded PID identity is alive. Once
# the host is gone it may contribute only exit_code/finished_at, never scope or PID.
# 函数用途: 读取托管进程留下的真实命令退出码；损坏或未结束时返回空事实。
def _host_terminal_state(state_file: str) -> dict[str, object]:
    if not str(state_file or "").strip():
        return {}
    payload = read_json_object(Path(state_file))
    if (
        str(payload.get("schema") or "") != HOST_STATE_SCHEMA
        or str(payload.get("status") or "") != "exited"
    ):
        return {}
    result: dict[str, object] = {}
    try:
        if payload.get("exit_code") is not None:
            result["exit_code"] = int(payload["exit_code"])
        if payload.get("finished_at") is not None:
            result["finished_at"] = float(payload["finished_at"])
    except (TypeError, ValueError):
        return {}
    return result


def _pid_alive(pid: int) -> bool:
    """跨平台进程存活探测。pid<=0 视为无效。"""
    if not pid or pid <= 0:
        return False
    if _IS_WINDOWS:
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return str(pid) in (out.stdout or "")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但无权限发信号,仍算存活
    except OSError:
        return False
    return True


# LLM: 前台超时、后台停止和 runner 清理共用唯一终止入口；PID 出生标识避免复用误杀。
# 函数用途: 有界发送 TERM/KILL 并回收直接子进程，返回核对回执；无法读取进程信息时不冒充确认。
def terminate_process_tree(
    pid: int,
    proc: subprocess.Popen | None,
    *,
    grace_seconds: float | None = None,
) -> ProcessTerminationReceipt:
    """终止观察到的后代进程树，并返回退出核对结果。

    POSIX:先快照 root 的全部后代,对每个进程组和 pid 发 SIGTERM,宽限后再对
      仍为同一进程实例的存活者发 SIGKILL。不能只 killpg(root):bwrap
      ``--new-session`` 会在里面再建 session/process-group,否则 npm/test 等后代
      能逃逸并继续持有 stdout/stderr pipe。
    Windows:taskkill /T /F 杀整棵进程树(/T 含子进程,/F 强制)。
    """
    if pid <= 0:
        return ProcessTerminationReceipt("noop", False, None, 0)
    if _IS_WINDOWS:
        return _terminate_windows_tree(pid, proc)
    snapshot, complete = _process_tree_snapshot(pid)
    method = "already_gone"
    signalled = _signal_process_snapshot(snapshot, signal.SIGTERM)
    grace = _KILL_GRACE_SECONDS if grace_seconds is None else max(0.0, grace_seconds)
    if signalled:
        method = "SIGTERM"
        if not _wait_process_snapshot_gone(snapshot, proc, grace):
            # 根进程已回收时 PID 可能复用；只能对仍是原实例的根补充后代，不能杀到另一棵新树。
            if snapshot.get(pid) and _same_process(pid, snapshot[pid]):
                current, current_complete = _process_tree_snapshot(pid)
                snapshot.update(current)
                complete = complete and current_complete
            _signal_process_snapshot(snapshot, getattr(signal, "SIGKILL", signal.SIGTERM))
            method = "SIGTERM->SIGKILL"
    if proc is not None:
        try:
            proc.wait(timeout=2)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
    _wait_process_snapshot_gone(snapshot, proc, 0.5)
    unresolved = tuple(pid for pid, token in snapshot.items() if not _process_instance_terminated(pid, token))
    return_code = proc.poll() if proc is not None else None
    confirmed = complete and not unresolved and (proc is None or return_code is not None)
    return ProcessTerminationReceipt(method, confirmed, return_code, len(snapshot), unresolved)


# LLM: Windows 仅在 taskkill /T /F 成功且直接子进程已回收时确认；单独 Popen.kill 不能证明后代退出。
# 函数用途: 调用系统进程树终止工具并核对返回码，失败时尽力终止直接子进程但保留未确认结果。
def _terminate_windows_tree(pid: int, proc: subprocess.Popen | None) -> ProcessTerminationReceipt:
    try:
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc is not None:
            proc.wait(timeout=2)
        code = proc.poll() if proc is not None else None
        confirmed = result.returncode == 0 and proc is not None and code is not None
        return ProcessTerminationReceipt("taskkill/T/F", confirmed, code, 1, () if confirmed else (pid,))
    except (OSError, subprocess.SubprocessError):
        _safe_popen_kill(proc)
        return ProcessTerminationReceipt("popen.kill", False, None, 1, (pid,))


def _safe_popen_kill(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        proc.kill()
    except OSError:
        pass


# LLM: 快照同时返回完整性；枚举失败不能等同于没有后代，也不能用于确认终止。
# 函数用途: 只读采集 root 及可观察后代的出生标识，供同一次终止与退出核对使用。
def _process_tree_snapshot(root_pid: int) -> tuple[dict[int, str], bool]:
    if root_pid <= 0:
        return {}, False
    children: dict[int, list[int]] = {}
    parents, complete = _process_parent_map()
    for child, parent in parents.items():
        children.setdefault(parent, []).append(child)
    ordered: list[int] = []
    pending = [root_pid]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current <= 0 or current in seen:
            continue
        seen.add(current)
        ordered.append(current)
        pending.extend(children.get(current, ()))
    snapshot = {current: capture_process_birth_token(current) for current in ordered}
    complete = complete and all(token or _process_instance_terminated(pid, "") for pid, token in snapshot.items())
    return snapshot, complete


# LLM: 进程枚举只使用系统结构化字段；无法访问或解析时返回不完整，禁止用空表证明退出。
# 函数用途: 读取宿主机父子进程关系；进程恰好消失不算错误，其余读取失败保留核对缺口。
def _process_parent_map() -> tuple[dict[int, int], bool]:
    proc_root = Path("/proc")
    if proc_root.is_dir():
        result: dict[int, int] = {}
        try:
            entries = tuple(proc_root.iterdir())
        except OSError:
            return {}, False
        complete = True
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                status = (entry / "status").read_text(encoding="utf-8", errors="replace")
                parent_line = next(line for line in status.splitlines() if line.startswith("PPid:"))
                result[int(entry.name)] = int(parent_line.split(":", 1)[1].strip())
            except (FileNotFoundError, ProcessLookupError):
                continue
            except (OSError, StopIteration, TypeError, ValueError):
                complete = False
        return result, complete and bool(result)
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,ppid="],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}, False
    result = {}
    complete = completed.returncode == 0
    for line in completed.stdout.splitlines():
        try:
            child_text, parent_text = line.split()
            result[int(child_text)] = int(parent_text)
        except (TypeError, ValueError):
            complete = False
    return result, complete and bool(result)


# LLM: 信号发送失败、出生标识读取失败或权限不足都不能证明死亡；僵尸已不能再执行但可能尚未被父进程收割。
# 函数用途: 只读核对原进程实例已消失、已变成僵尸或 PID 已被明确复用，用于终止回执而非任务验收。
def _process_instance_terminated(pid: int, birth_token: str) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        if stat.rsplit(")", 1)[1].split()[0] == "Z":
            return True
    except (OSError, IndexError):
        pass
    current = capture_process_birth_token(pid) if birth_token else ""
    return bool(birth_token and current and current != birth_token)


# LLM: Persisted process control must pair a PID with a stable birth identity.
# Linux uses proc start ticks; other hosts use their native start timestamp command.
# 函数用途: 读取一个进程的启动指纹，供跨进程状态核对和防 PID 复用误杀。
def capture_process_birth_token(pid: int) -> str:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        return f"proc:{stat.rsplit(')', 1)[1].split()[19]}"
    except (IndexError, OSError):
        pass
    if _IS_WINDOWS:
        command = (
            f"(Get-Process -Id {int(pid)} -ErrorAction Stop)."
            "StartTime.ToUniversalTime().Ticks"
        )
        argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
    else:
        argv = ["ps", "-o", "lstart=", "-p", str(int(pid))]
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return ""
    value = completed.stdout.strip()
    return f"host:{value}" if completed.returncode == 0 and value else ""


def _same_process(pid: int, birth_token: str) -> bool:
    if pid <= 0:
        return False
    if birth_token:
        return capture_process_birth_token(pid) == birth_token
    return _pid_alive(pid)


# LLM: A cached record may be reused only for its canonical authority directory;
# equal owner/thread scope never aliases two independently configured stores.
# 函数用途: 核对缓存会话是否属于本次工具绑定的同一个持久目录。
def _record_matches_store(
    record: BackgroundProcess,
    store_root: str | Path | None,
) -> bool:
    if not store_root:
        return True
    if not record.store_root:
        return False
    expected = Path(store_root).expanduser().resolve(strict=False)
    actual = Path(record.store_root).expanduser().resolve(strict=False)
    return actual == expected


def _signal_process_snapshot(snapshot: dict[int, str], signum: int) -> bool:
    """先信号各独立进程组，再逐个信号后代；绝不碰调用者自己的进程组。"""
    own_pid = os.getpid()
    try:
        own_pgid = os.getpgrp()
    except OSError:
        own_pgid = -1
    live = [(pid, token) for pid, token in snapshot.items() if pid != own_pid and _same_process(pid, token)]
    groups: set[int] = set()
    for pid, _token in live:
        try:
            pgid = os.getpgid(pid)
        except (ProcessLookupError, PermissionError, OSError):
            continue
        if pgid > 0 and pgid != own_pgid:
            groups.add(pgid)
    sent = False
    for pgid in groups:
        try:
            os.killpg(pgid, signum)
            sent = True
        except (ProcessLookupError, PermissionError, OSError):
            continue
    for pid, token in reversed(live):
        if not _same_process(pid, token):
            continue
        try:
            os.kill(pid, signum)
            sent = True
        except (ProcessLookupError, PermissionError, OSError):
            continue
    return sent


# LLM: 此等待仅核对已观察进程，权限或身份读取失败须视为未确认；调用方仍要检查快照完整性。
# 函数用途: 在有界时间内收割直接子进程并等待观察到的后代退出，不把信号已发送当成终止。
def _wait_process_snapshot_gone(
    snapshot: dict[int, str],
    proc: subprocess.Popen | None,
    grace_seconds: float,
) -> bool:
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if proc is not None:
            proc.poll()
        if all(_process_instance_terminated(pid, token) for pid, token in snapshot.items()):
            return True
        time.sleep(0.05)
    return all(_process_instance_terminated(pid, token) for pid, token in snapshot.items())


# 当前进程的共享缓存；跨主/子代理的权威事实由 ProcessSessionStore 提供。
process_registry = ProcessRegistry()


__all__ = [
    "BackgroundProcess",
    "ProcessAccessScope",
    "ProcessRegistration",
    "ProcessRegistry",
    "ProcessTerminationReceipt",
    "capture_process_birth_token",
    "process_access_scope",
    "process_registry",
    "terminate_process_tree",
]
