
# LLM: 访问身份读取 process_scope；后台记录与前台命令共用出生标识和终止回执，不用旧 PID 猜归属；联测 registry、后台 host 与 shell orphan kill。
# 模块用途: 记录受管进程并按精确执行归属停止，核对原进程树和独立组成员退出，不把发送信号当成清理完成。
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..common.json_io import read_json_object
from .cancellation import current_cancellation_token, raise_if_cancelled
from .process_scope import ProcessAccessScope
from .process_session_records import (
    LEGACY_PROCESS_SESSION_SCHEMA,
    PROCESS_SESSION_SCHEMA,
    PROCESS_TERMINAL_STATUSES,
)
from .process_session_store import (
    ProcessSessionStore,
    process_session_error_report,
)

HOST_STATE_SCHEMA = "background_process_host.v1"  # 仅供已发布 v1 记录读取旧终态文件。
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


# LLM: 结构化错误区别权威损坏和记录不存在，不展开其它会话的记录内容。
# 类用途: 阻止查询或停止在存储故障后继续使用旧缓存。
class ProcessSessionAuthorityError(RuntimeError):
    # LLM: 保留 Store 的脱敏分类供工具返回，异常消息不包含命令或身份正文。
    # 函数用途: 向调用方报告原权威不可读取。
    def __init__(self, report: dict[str, object]) -> None:
        super().__init__("managed process authority unavailable")
        self.report = report


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
    status: str = "running"
    exit_code: int | None = None
    finished_at: float | None = None
    completion_target: dict[str, str] = field(default_factory=dict)
    completion_notice_id: str = ""
    persisted_snapshot: dict[str, object] = field(default_factory=dict, repr=False)

    # LLM: Terminal is a typed host fact; model text and host-state prose never
    # participate in this transition.
    # 函数用途: 判断业务是否已退出、被终止或确定未启动；此标志不替代 host 资源清理回执。
    def is_terminal(self) -> bool:
        return self.status in PROCESS_TERMINAL_STATUSES

    # LLM: 摘要只暴露 session 管理句柄和控制标记；启动未确认时没有开始时间，日志错误不能改终态。
    # 函数用途: 生成后台进程的状态和日志观测，耗时与输出增长分开，宿主 PID 保持内部使用。
    def to_summary(self, *, include_output: bool = False, output_tail_chars: int = _OUTPUT_TAIL_CHARS) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "session_id": self.session_id,
            "command": self.command[:200],
            "status": self.status,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.started_at)) if self.started_at else None,
            "uptime_seconds": max(0, int((self.finished_at or time.time()) - self.started_at)) if self.started_at else 0,
            "output_file": self.output_file,
        }
        if self.persisted_snapshot.get("schema") == PROCESS_SESSION_SCHEMA:
            summary.update(stop_requested=self.persisted_snapshot["stop_requested"],
                           handoff_confirmed=self.persisted_snapshot["handoff_confirmed"])
        if self.exit_code is not None:
            summary["exit_code"] = self.exit_code
        if include_output:
            summary["output_tail"] = _read_log_tail(self.output_file, output_tail_chars)
            try:
                summary["output_bytes"] = Path(self.output_file).stat().st_size if self.output_file else None
            except OSError:
                summary["output_bytes"] = None
        return summary

    # LLM: 保留已验证记录的全部版本字段；旧版本不升级，新版本不丢执行身份、revision 或控制事实。
    # 函数用途: 返回当前缓存快照的持久字段，Popen 永不序列化。
    def to_record(self) -> dict[str, object]:
        return {
            **self.persisted_snapshot,
            "schema": self.persisted_snapshot["schema"],
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
            "completion_target": self.completion_target,
            "completion_notice_id": self.completion_notice_id,
        }

    # LLM: 只接受 Store 验证的当前记录，完整保留 v1/v2 字段；本地句柄只能在同目录同出生实例核对后附加。
    # 函数用途: 恢复另一进程写下的当前状态、执行归属和控制事实，不从访问范围猜任务身份。
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
            completion_target=dict(payload.get("completion_target") or {}),
            completion_notice_id=str(payload.get("completion_notice_id") or ""),
            persisted_snapshot=dict(payload),
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


# LLM: 此表只缓存原 Store 记录和本进程句柄；任何冷热查询都先刷新权威，锁顺序始终 registry 到 Store。
# 类用途: 按精确访问范围查询、等待和停止持久后台会话，不再在启动后创建另一份登记。
class ProcessRegistry:
    # LLM: key 同时包含规范目录和 session ID；不同 owner 的同名句柄不会复用进程对象。
    # 函数用途: 初始化本进程缓存和互斥。
    def __init__(self) -> None:
        self._processes: dict[tuple[str, str], BackgroundProcess] = {}
        self._lock = threading.Lock()

    # LLM: 原记录必须已经提交；附加 Popen 不新建 ID 或重写生命周期，实例不符明确拒绝。
    # 函数用途: 接管启动方已经交接的同一个会话，保留可回收的本地 host 句柄。
    def attach(self, payload: dict[str, object], process: subprocess.Popen, store_root: Path) -> BackgroundProcess:
        from .process_session_records import merge_process_record

        with self._lock:
            record = self._visible_record_locked(str(payload["session_id"]), None, store_root)
            if record is None:
                raise ProcessSessionAuthorityError({"error_type": "authority_missing"})
            merge_process_record(payload, record.to_record())
            if process.pid != record.pid:
                raise ValueError("managed process handle conflict")
            record.process = process
            self._prune_finished_locked(store_root)
            return record

    # LLM: 越界和缺失统一为空，损坏权威必须报错而不是拿缓存补成功；日志只能在范围校验后读取。
    # 函数用途: 获取当前有效会话，并检查托管进程是否失去控制。
    def get(self, session_id: str, access_scope: ProcessAccessScope | None = None,
            store_root: str | Path | None = None) -> BackgroundProcess | None:
        with self._lock:
            record = self._visible_record_locked(session_id, access_scope, store_root)
            if record is not None:
                self._refresh_locked(record)
            return record

    # LLM: v2 只信原 Store 的终态，host 丢失不是 child 退出；v1 明确保留已发布数据的旧终态读取。
    # 函数用途: 更新失去托管的未知状态，或读取旧版本已经退出的命令结果。
    def _refresh_locked(self, record: BackgroundProcess) -> None:
        if record.is_terminal():
            if record.process is not None:
                record.process.poll()
            return
        if record.persisted_snapshot.get("schema") == PROCESS_SESSION_SCHEMA:
            pid = record.pid or record.persisted_snapshot["launcher_pid"]
            birth = record.pid_birth_token or record.persisted_snapshot["launcher_birth_token"]
            if not _process_instance_terminated(pid, birth) or record.status == "unknown":
                return
            try:
                with ProcessSessionStore(record.store_root).transaction() as transaction:
                    current = transaction.load(record.session_id)
                    if current is None:
                        raise ProcessSessionAuthorityError({"error_type": "authority_missing"})
                    # 刚读取的较新终态或实例绑定优先，不能把旧观察写给不同的启动阶段。
                    if current == record.persisted_snapshot:
                        current = transaction.write({**current, "status": "unknown"})
                    self._cache_payload(current, Path(record.store_root))
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                self._processes.pop((record.store_root, record.session_id), None)
                raise ProcessSessionAuthorityError(process_session_error_report(exc, "process_registry.refresh")) from exc
            return
        return_code = record.process.poll() if record.process is not None else None
        if (record.process is not None and return_code is None) or (record.process is None and _same_process(record.pid, record.pid_birth_token)):
            return
        host_state = _host_terminal_state(record.host_state_file)
        record.status = "exited"
        record.exit_code = int(host_state["exit_code"]) if host_state.get("exit_code") is not None else return_code
        record.finished_at = float(host_state.get("finished_at") or time.time())
        self._persist_locked(record)

    # LLM: 状态与日志来自同一个范围内的当前记录；PID 不进入模型摘要。
    # 函数用途: 查询后台进度和最近输出。
    def status(self, session_id: str, access_scope: ProcessAccessScope | None = None,
               store_root: str | Path | None = None) -> dict[str, Any] | None:
        record = self.get(session_id, access_scope, store_root)
        return record.to_summary(include_output=True) if record is not None else None

    # LLM: 简单内部调用沿结构化报告入口，模型工具需直接消费 list_report 中的错误。
    # 函数用途: 返回当前可见的进程摘要列表。
    def list(self, access_scope: ProcessAccessScope | None = None, store_root: str | Path | None = None) -> list[dict[str, Any]]:
        rows, _errors = self.list_report(access_scope, store_root)
        return rows

    # LLM: 枚举先清除该目录旧缓存再装入有效记录；redo 故障不能回退到旧成功视图。
    # 函数用途: 列出当前范围的后台会话，并单列损坏或恢复错误。
    def list_report(self, access_scope: ProcessAccessScope | None = None,
                    store_root: str | Path | None = None) -> tuple[list[dict[str, Any]], list[dict[str, object]]]:
        with self._lock:
            roots = {str(Path(store_root).resolve())} if store_root else {rec.store_root for rec in self._processes.values()}
            errors = []
            for root in roots:
                errors.extend(self._hydrate_store_locked(root))
            records = [rec for rec in self._processes.values() if (access_scope is None or rec.access_scope == access_scope)
                       and _record_matches_store(rec, store_root)]
            for record in records:
                self._refresh_locked(record)
            records.sort(key=lambda item: (item.is_terminal(), -item.started_at))
            return [record.to_summary() for record in records], errors

    # LLM: v2 精确冻结一个 ID 再锁外清理；不得按其 task 扩大范围，launcher 绝不参与终止。
    # 函数用途: 停止显式指定的会话；只有可信树退出回执才报告已停止。
    def kill(self, session_id: str, access_scope: ProcessAccessScope | None = None,
             store_root: str | Path | None = None) -> dict[str, Any] | None:
        with self._lock:
            record = self._visible_record_locked(session_id, access_scope, store_root)
            if record is None:
                return None
            payload, process = record.to_record(), record.process
            root = Path(record.store_root)
        if payload["schema"] == PROCESS_SESSION_SCHEMA:
            from .process_session_cleanup import stop_process_session

            cleanup = stop_process_session(ProcessSessionStore(root), payload, host_process=process)
            with self._lock:
                current = self._cache_payload(cleanup.record, root)
                summary = current.to_summary()
            summary["termination"] = {"confirmed": cleanup.confirmed, "instances": [asdict(r) for r in cleanup.terminations]}
            return summary
        return self._kill_legacy(record, access_scope)

    # LLM: 已发布 v1 仅按原 session 管理，不推导 task 身份；原出生标识必须传入终止快照边界。
    # 函数用途: 保留旧记录的显式句柄停止，确认失败时不登记 killed。
    def _kill_legacy(self, record: BackgroundProcess, access_scope: ProcessAccessScope | None) -> dict[str, Any]:
        with self._lock:
            self._refresh_locked(record)
            if record.is_terminal():
                return {"session_id": record.session_id, "status": "already_exited" if record.status == "exited" else record.status,
                        "exit_code": record.exit_code, "message": "进程已经结束，无需终止。"}
        receipt = terminate_process_tree(record.pid, record.process, expected_birth_token=record.pid_birth_token)
        with self._lock:
            current = self._visible_record_locked(record.session_id, access_scope, record.store_root)
            if current is None:
                raise ProcessSessionAuthorityError({"error_type": "authority_missing"})
            if receipt.confirmed:
                current.status, current.finished_at, current.exit_code = "killed", time.time(), receipt.return_code
                self._persist_locked(current)
            summary = current.to_summary()
            summary.update(signal=receipt.method, termination=asdict(receipt))
            return summary

    # LLM: 每轮观察原 Store 的阶段变化，取消仅释放等待；超时仍 pending，不能因 host 不活推断 child 结束。
    # 函数用途: 在宿主取消和超时范围内等待同一个 session 的可信终态。
    def wait(self, session_id: str, timeout_seconds: float, access_scope: ProcessAccessScope | None = None,
             store_root: str | Path | None = None) -> dict[str, Any] | None:
        deadline = time.monotonic() + max(0.0, float(timeout_seconds or 0.0))
        token = current_cancellation_token()
        while True:
            raise_if_cancelled()
            record = self.get(session_id, access_scope, store_root)
            if record is None:
                return None
            remaining = deadline - time.monotonic()
            if record.is_terminal() or remaining <= 0:
                summary = record.to_summary(include_output=True)
                summary["wait_timed_out"] = not record.is_terminal()
                return summary
            interval = min(0.1, remaining)
            token.wait(interval) if token is not None else time.sleep(interval)

    # LLM: 没有显式 root 的内部查询只允许缓存中唯一匹配，再读它的原地址；不能跨 owner 猜同名句柄。
    # 函数用途: 从权威目录读当前记录，再做访问身份比较。
    def _visible_record_locked(self, session_id: str, access_scope: ProcessAccessScope | None,
                               store_root: str | Path | None) -> BackgroundProcess | None:
        if not store_root:
            matches = [rec for rec in self._processes.values() if rec.session_id == session_id]
            if len(matches) != 1:
                return None
            store_root = matches[0].store_root
        root = Path(store_root).expanduser().resolve(strict=False)
        report = ProcessSessionStore(root).load(session_id)
        if report.load_error:
            self._processes.pop((str(root), session_id), None)
            raise ProcessSessionAuthorityError(report.load_error)
        if not report.record:
            cached = self._processes.pop((str(root), session_id), None)
            if cached is not None and not cached.is_terminal():
                raise ProcessSessionAuthorityError({"error_type": "active_authority_missing"})
            return None
        record = self._cache_payload(report.record, root)
        return record if access_scope is None or record.access_scope == access_scope else None

    # LLM: 只在地址、PID 和出生标识完全相同时保留本地 Popen；所有持久字段每次整体刷新。
    # 函数用途: 将可信当前快照装入缓存，不丢 revision、执行归属和控制标记。
    def _cache_payload(self, payload: dict[str, object], root: Path) -> BackgroundProcess:
        key = (str(root), str(payload["session_id"]))
        cached = self._processes.get(key)
        fresh = BackgroundProcess.from_record(payload, store_root=root)
        if cached is not None:
            if cached.pid == fresh.pid and cached.pid_birth_token == fresh.pid_birth_token:
                fresh.process = cached.process
            cached.__dict__.update(fresh.__dict__)
            return cached
        self._processes[key] = fresh
        return fresh

    # LLM: 读取错误的记录从缓存撤下，健康子集来自同一次枚举；不可拿原缓存绕过恢复错误。
    # 函数用途: 刷新一个规范目录的完整可读记录，并返回独立错误。
    def _hydrate_store_locked(self, store_root: str | Path) -> list[dict[str, object]]:
        root = Path(store_root).expanduser().resolve(strict=False)
        records, errors = ProcessSessionStore(root).list_records()
        present = {str(payload["session_id"]) for payload in records}
        for key in list(self._processes):
            if key[0] == str(root) and key[1] not in present:
                del self._processes[key]
        for payload in records:
            self._cache_payload(payload, root)
        return errors

    # LLM: 此入口只写已发布 v1 的观察更新；v2 必须在调用处同一事务重读，不能强换 revision 写旧缓存。
    # 函数用途: 保存旧会话终态并吸收实际提交结果。
    def _persist_locked(self, record: BackgroundProcess) -> None:
        if record.persisted_snapshot.get("schema") != LEGACY_PROCESS_SESSION_SCHEMA:
            raise ValueError("v2 updates require a current store transaction")
        effective = ProcessSessionStore(record.store_root).write(record.to_record())
        self._cache_payload(effective, Path(record.store_root))

    # LLM: 只裁剪确认终态；磁盘仅操作当前调用目录，不能因其它 owner 的损坏影响本次交接。
    # 函数用途: 限制已完成缓存数量，并在本次启动所属的原目录裁剪历史。
    def _prune_finished_locked(self, store_root: Path) -> None:
        finished = [(key, rec) for key, rec in self._processes.items() if rec.is_terminal()]
        finished.sort(key=lambda item: item[1].finished_at or item[1].started_at)
        for key, _record in finished[:max(0, len(finished) - _MAX_FINISHED)]:
            self._processes.pop(key, None)
        ProcessSessionStore(store_root).prune_finished(_MAX_FINISHED)

    # LLM: 只丢本进程缓存，不修改持久记录或杀资源；测试负责精确回收自己创建的资源。
    # 函数用途: 清除缓存以验证跨进程恢复或隔离测试。
    def clear(self) -> None:
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


# LLM: 前台超时、后台停止和 runner 清理共用唯一终止入口；冻结资源可要求指定出生标识，快照不匹配时绝不发信号。
# 函数用途: 有界发送 TERM/KILL 并回收直接子进程，返回核对回执；无法读取进程信息时不冒充确认。
def terminate_process_tree(
    pid: int,
    proc: subprocess.Popen | None,
    *,
    grace_seconds: float | None = None,
    expected_birth_token: str | None = None,
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
    if expected_birth_token is not None and not expected_birth_token:
        return ProcessTerminationReceipt("identity_unavailable", False, None, 0, (pid,))
    if _IS_WINDOWS:
        if expected_birth_token is not None and not _same_process(pid, expected_birth_token):
            return ProcessTerminationReceipt("identity_changed", False, None, 0, (pid,))
        return _terminate_windows_tree(pid, proc)
    snapshot, complete = _process_tree_snapshot(pid)
    if expected_birth_token is not None and snapshot.get(pid) != expected_birth_token:
        return ProcessTerminationReceipt("identity_changed", False, None, len(snapshot), (pid,))
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


# LLM: 快照合并父子关系与仍可核对的独立进程组；只有活的或未回收组长能授予组范围，旧 PID 不能；枚举失败保留不完整，联测 shell orphan kill。
# 函数用途: 只读采集原进程树及已换父进程的同组后代出生标识，供本次终止和退出核对共用。
def _process_tree_snapshot(root_pid: int) -> tuple[dict[int, str], bool]:
    if root_pid <= 0:
        return {}, False
    children: dict[int, list[int]] = {}
    parents, complete = _process_parent_map()
    for child, parent in parents.items():
        children.setdefault(parent, []).append(child)
    group_members, group_complete = _exclusive_group_members(root_pid)
    complete = complete and group_complete
    ordered: list[int] = []
    pending = [root_pid, *group_members]
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


# LLM: 仅独立组长且出生身份在枚举前后相同才拥有整组；不恢复已回收 root 的旧 PGID，不读取命令文字或扩大到宿主组。
# 函数用途: 找出仍由本次组长证明归属的进程组成员，避免外层 Shell 退出后漏掉被系统接管的子进程。
def _exclusive_group_members(root_pid: int) -> tuple[list[int], bool]:
    birth = capture_process_birth_token(root_pid)
    if not birth:
        return [], _process_instance_terminated(root_pid, "")
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,pgid="], capture_output=True, text=True,
            timeout=1, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return [], False
    members: list[int] = []
    root_group: int | None = None
    complete = result.returncode == 0
    for line in result.stdout.splitlines():
        try:
            pid, pgid = map(int, line.split())
        except (TypeError, ValueError):
            complete = False
            continue
        if pgid == root_pid:
            members.append(pid)
        if pid == root_pid:
            root_group = pgid
    if not _same_process(root_pid, birth):
        return [], False
    if root_group is None:
        return [], False
    if root_group != root_pid or root_group == os.getpgrp():
        return [], complete
    return members, complete


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


# LLM: 信号失败、身份读取失败或权限不足不能证明死亡；僵尸以宿主内核状态确认，
#   不调用 waitpid 冒领 Popen 的退出码。同步检查 shell 和无句柄的子代理停止回执。
# 函数用途: 只读核对原进程实例已消失、已变成僵尸或 PID 已被明确复用，用于终止回执而非任务验收。
def _process_instance_terminated(pid: int, birth_token: str) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    if _process_is_zombie(pid):
        return True
    current = capture_process_birth_token(pid) if birth_token else ""
    return bool(birth_token and current and current != birth_token)


# LLM: Linux 从 proc、其它 POSIX 从 ps 读取内核进程状态；读取失败不等于已死。
#   只读探测不回收进程，保留持有 Popen 的调用方获取真实退出码的权利。
# 函数用途: 确认尚未被父进程回收、但已不能继续写文件的进程，包括 macOS 的僵尸。
def _process_is_zombie(pid: int) -> bool:
    if _IS_WINDOWS:
        return False
    if Path("/proc").is_dir():
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
            return stat.rsplit(")", 1)[1].split()[0] in {"Z", "X"}
        except (OSError, IndexError):
            return False
    try:
        result = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True, text=True, timeout=1, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip().startswith(("Z", "X"))


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
    "ProcessRegistry",
    "ProcessTerminationReceipt",
    "capture_process_birth_token",
    "process_registry",
    "terminate_process_tree",
]
