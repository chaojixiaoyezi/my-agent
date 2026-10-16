# LLM: PTY 使用 process_scope 的执行身份合同；注册表独占容量、启动预留与终止回执，身份不授予访问权限。
#   回收三层网：回合/任务收口（reclaim_pty_sessions，按 owner/thread/task 与 run）、显式停止
#   （request_stop）、空闲兜底（set_idle_timeout_minutes + sweep_idle）。分页器等按键会让命令永不退出，
#   所以每层都必须真的杀进程树，不能只关 fd。
# 模块用途: 提供有界交互终端并按任务归属收回进程；Windows 暂无 ConPTY，不虚报支持。
from __future__ import annotations

"""Bounded interactive PTY sessions using the same shell policy and sandbox gate."""

import json
import logging
import os
import select
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..attempt.sandbox import SandboxHostPolicy
from ..capability._agents_contract_fields import AgentsContractError
from ..capability.agents_source_processes import (
    process_source_change_affects,
    process_source_configurations,
    process_source_configurations_current,
    register_source_registry,
    source_process_boundary,
)
from ..common.cancellation import ToolCancelled, cancellation_requested, raise_if_cancelled
from ..contracts.gates.command_policy import evaluate_command_policy
from .models import (
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    OutputPolicy,
    ResourceScopePolicy,
    SandboxPolicy,
    ToolAvailability,
    ToolHandlerOutcome,
    ToolInputPolicy,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
    TrustedParameterBinding,
)
from .process_registry import (
    ProcessTerminationReceipt,
    terminate_process_tree,
)
from .process_scope import ProcessExecutionScope, process_access_scope
from .pty_process_lifecycle import PtyOwnedProcess
from .sandbox import SandboxUnavailable
from .shell import (
    ShellTool,
    _sandbox_exec,
    _sandbox_protected_write_paths,
    _sandbox_read_roots,
    _sandbox_write_roots,
    _subprocess_text_env,
    parse_shell_command,
)
from .write_boundary import SandboxWriteProtection

# PTY 会话最多 32 个：防失控并发把资源占满。
_MAX_SESSION_COUNT = 32
# PTY 输出缓冲上限 1 MiB：防输出积压撑爆内存。
_MAX_BUFFER_BYTES = 1_000_000
# PTY 单次写入最多 64 KiB：控制单次写入体积。
_MAX_WRITE_BYTES = 64_000
# PTY 写入超时 5 秒：慢终端不无限等，防写操作卡死。
_WRITE_TIMEOUT_SECONDS = 5.0
# PTY 会话单次读取默认 32000 字节：平衡吞吐与响应延迟。
_DEFAULT_READ_BYTES = 32_000
# PTY 终端列数上限 1000：防畸形尺寸请求。
_MAX_TERMINAL_COLUMN_COUNT = 1000
# PTY 终端行数上限 1000：防畸形尺寸请求。
_MAX_TERMINAL_ROW_COUNT = 1000
# 空闲兜底巡检间隔上限 30 秒：默认 30 分钟的空闲阈值不需要更密的检查。
_IDLE_SWEEP_INTERVAL_SECONDS = 30.0
# 空闲巡检间隔下限 0.5 秒：测试/极小阈值时也要有界，不把巡检打成忙循环。
_MIN_IDLE_SWEEP_INTERVAL_SECONDS = 0.5


# LLM: 访问快照与执行归属分别冻结；close_lock 串行同一进程的终止，回执确认后才宣称已停止。
# 类用途: 保存终端进程、增量输出、权限与所属执行，允许模型回合结束后仍按任务收口。
@dataclass
class PtySession:
    session_id: str
    command: str
    process: subprocess.Popen
    master_fd: int
    started_at: float
    last_active_at: float
    access_scope: PtyAccessScope
    execution_scope: ProcessExecutionScope = field(default_factory=ProcessExecutionScope)
    output: bytearray = field(default_factory=bytearray)
    base_cursor: int = 0
    next_cursor: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)
    write_lock: threading.Lock = field(default_factory=threading.Lock)
    read_lock: threading.Lock = field(default_factory=threading.Lock)
    io_condition: threading.Condition = field(default_factory=threading.Condition)
    io_users: int = 0
    fd_closing: bool = False
    close_lock: threading.Lock = field(default_factory=threading.Lock)
    closed: bool = False
    termination: ProcessTerminationReceipt | None = None
    source_configurations: tuple = ()
    retirement_thread: threading.Thread | None = field(default=None, repr=False)
    # macOS 上父进程保留的一份 slave FD（-1 表示未保留），与 master 一起在尾部排空后关闭。
    tail_hold_fd: int = -1

    def append(self, chunk: bytes) -> None:
        with self.lock:
            self.output.extend(chunk)
            self.next_cursor += len(chunk)
            overflow = len(self.output) - _MAX_BUFFER_BYTES
            if overflow > 0:
                del self.output[:overflow]
                self.base_cursor += overflow
            self.last_active_at = time.time()

    def read(self, cursor: int, max_bytes: int) -> tuple[bytes, int, bool]:
        with self.lock:
            requested = max(0, int(cursor))
            truncated = requested < self.base_cursor
            start = max(requested, self.base_cursor) - self.base_cursor
            chunk = bytes(self.output[start : start + max_bytes])
            next_cursor = max(requested, self.base_cursor) + len(chunk)
            self.last_active_at = time.time()
            return chunk, next_cursor, truncated


# LLM: 此不可变值是PTY归属和文件系统边界的唯一比较键；人格根、拒写根和网络上界必须一起保留。
# 类用途: 冻结 PTY 启动时的 owner、可读、可写和受保护路径。
@dataclass(frozen=True)
class PtyAccessScope:
    owner_home: str
    conversation_id: str
    write_roots: tuple[str, ...] | None
    read_roots: tuple[str, ...] | None
    protected_write_paths: tuple[str, ...] | None
    write_scope_denied_roots: tuple[str, ...] = ()
    network_access: bool = True
    protected_persona_root: str = ""


# LLM: 一次 PTY 启动的沙箱输入原样收拢，不做任何推断；字段与 tooling/shell._sandbox_exec 的同名参数一一对应，
#   private_roots 是拒读的宿主根；凭据来源仅由 Shell 的宿主选项传入，不能读模型参数或削弱擦洗上界。
# 类用途: 把 owner、读写根、受保护路径和私有根作为一个整体交给 _spawn。
@dataclass(frozen=True)
class _PtySandboxInputs:
    owner_home: object = None
    write_roots: tuple[Path, ...] | None = None
    read_roots: tuple[Path, ...] | None = None
    protected_write_paths: tuple[Path, ...] | None = None
    private_roots: tuple[object, ...] = ()
    write_scope_denied_roots: tuple[Path, ...] = ()
    network_access: bool = True
    host_credential_env_names: frozenset[str] = field(default_factory=frozenset, repr=False)
    protected_persona_root: object = None


# LLM: 继承原宿主环境/网络上界，只增加独立人格根；由Shell宿主装配，不能从模型参数构造许可。
# 类用途: 在原host_policy参数内同时携带PTY所需的来源保护根，避免继续增长启动参数。
@dataclass(frozen=True)
class PtyHostPolicy(SandboxHostPolicy):
    protected_persona_root: object = None


# LLM: 预留和取消标记共用 registry 锁；取消只命中当时的启动，不阻止用户后续显式恢复。
# 类用途: 在 Popen 尚未返回时保留执行归属，让明确的资源停止请求覆盖尚未完成的启动。
@dataclass
class _PtyStart:
    execution_scope: ProcessExecutionScope
    cancelled: bool = False


# LLM: 先规范全部宿主路径再比会话权限；缺失列表保持未声明语义，不能误变成空拒绝列表或从模型参数补根。
# their distinct "not constrained by this field" meaning instead of becoming empty deny lists.
# 函数用途: 把工具边界规范成可稳定比较的 PTY 权限快照。
def _pty_access_scope(
    owner_home: object,
    write_roots: tuple[Any, ...] | None,
    read_roots: tuple[Any, ...] | None = None,
    protected_write_paths: tuple[Any, ...] | None = None,
    run_scope: object = None,
    *, write_scope_denied_roots: tuple[Path, ...] = (), network_access: bool = True,
    protected_persona_root: object = None,
) -> PtyAccessScope:
    owner = ""
    if owner_home:
        owner = str(Path(owner_home).expanduser().resolve(strict=False))
    normalized_roots = None
    if write_roots is not None:
        normalized_roots = tuple(
            sorted({str(Path(root).expanduser().resolve(strict=False)) for root in write_roots})
        )
    normalized_read_roots = None
    if read_roots is not None:
        normalized_read_roots = tuple(
            sorted({str(Path(root).expanduser().resolve(strict=False)) for root in read_roots})
        )
    normalized_protected = ()
    if protected_write_paths is not None:
        normalized_protected = tuple(
            sorted(
                {
                    str(Path(root).expanduser().resolve(strict=False))
                    for root in protected_write_paths
                }
            )
        )
    return PtyAccessScope(
        owner_home=owner,
        conversation_id=process_access_scope(run_scope, owner).conversation_id,
        write_roots=normalized_roots,
        read_roots=normalized_read_roots,
        protected_write_paths=normalized_protected,
        write_scope_denied_roots=tuple(sorted({str(Path(root).resolve(strict=False)) for root in write_scope_denied_roots})),
        network_access=network_access,
        protected_persona_root=str(Path(protected_persona_root).expanduser().resolve(strict=False)) if protected_persona_root else "",
    )


# LLM: 模型访问核对 PtyAccessScope；宿主停止按独立执行身份冻结当前句柄和预留，不扫目录猜归属。
# 类用途: 管理有界终端及其启动、停止竞态，防止跨用户、会话或任务误停。
class PtySessionRegistry:
    # LLM: 创建中也占容量；历史最多保留最近一批终态，计数 ID 不在 clear 后复用。
    # 函数用途: 初始化唯一会话表和并发创建预留。
    def __init__(self) -> None:
        self._sessions: dict[str, PtySession] = {}
        self._counter = 0
        self._lock = threading.Lock()
        self._pending_starts: dict[str, _PtyStart] = {}
        # 空闲兜底阈值（秒，0=不限制）与唯一巡检线程；由工具装配点按配置设置，
        # 未装配时保持 0，单元测试与嵌入式使用不会被后台线程打扰。
        self.idle_timeout_seconds = 0.0
        self._idle_thread: threading.Thread | None = None
        register_source_registry(self)

    # LLM: 容量和执行身份一起预留；宿主来源名随本次 spawn 冻结，不入模型参数或持久会话权限账。
    # 函数用途: 有界创建终端，让明确的资源停止请求可以命中尚未完成的启动。
    def start(
        self,
        command: str,
        target,
        owner_home: object = None,
        write_roots: tuple[Path, ...] | None = None,
        read_roots: tuple[Path, ...] | None = None,
        write_protection: SandboxWriteProtection | None = None,
        run_scope: object = None,
        *,
        host_policy: SandboxHostPolicy | None = None,
    ) -> PtySession:
        host_policy = host_policy or SandboxHostPolicy()
        if os.name == "nt":
            raise OSError("PTY_UNAVAILABLE: Windows requires a ConPTY backend")
        raise_if_cancelled()
        execution_scope = ProcessExecutionScope.from_run_scope(run_scope, owner_home)
        with self._lock:
            _prune_finished_sessions(self)
            active = sum(not _source_resource_retired(session) for session in self._sessions.values())
            if active + len(self._pending_starts) >= _MAX_SESSION_COUNT:
                raise OSError(f"PTY_SESSION_LIMIT: active session limit is {_MAX_SESSION_COUNT}")
            self._counter += 1
            session_id = f"pty-{self._counter}-{int(time.time())}"
            self._pending_starts[session_id] = _PtyStart(execution_scope)
        try:
            protection = write_protection or SandboxWriteProtection()
            sandbox = _PtySandboxInputs(owner_home, write_roots, read_roots, protection.absolute_paths,
                                        host_policy.private_roots, protection.denied_roots, host_policy.network_access,
                                        host_policy.host_credential_env_names, getattr(host_policy, "protected_persona_root", None))
            with source_process_boundary():
                return self._spawn(session_id, command, target, sandbox, run_scope)
        finally:
            with self._lock:
                self._pending_starts.pop(session_id, None)

    # LLM: Popen 前后核对同一预留；环境只经 Shell 唯一擦洗入口，取消后的迟到进程仍登记并终止。
    # 函数用途: 启动 PTY 并排空输出，封住启动期间收到停止后留下孤儿进程的窗口。
    def _spawn(self, session_id, command, target, sandbox: _PtySandboxInputs, run_scope) -> PtySession:
        import pty

        owner_home = sandbox.owner_home
        source_configurations = process_source_configurations(sandbox.protected_persona_root or owner_home, full_access=not bool(owner_home))
        access_scope = _pty_access_scope(
            owner_home,
            sandbox.write_roots,
            sandbox.read_roots,
            sandbox.protected_write_paths,
            run_scope,
            write_scope_denied_roots=sandbox.write_scope_denied_roots,
            network_access=sandbox.network_access,
            protected_persona_root=sandbox.protected_persona_root,
        )
        exec_arg, use_shell = _sandbox_exec(
            command,
            target,
            owner_home,
            sandbox.protected_persona_root,
            write_roots=sandbox.write_roots,
            read_roots=sandbox.read_roots,
            write_protection=SandboxWriteProtection(tuple(sandbox.protected_write_paths or ()), sandbox.write_scope_denied_roots),
            host_policy=SandboxHostPolicy(sandbox.private_roots, sandbox.network_access),
        )
        with self._lock:
            launch = self._pending_starts[session_id]
            if launch.cancelled:
                raise ToolCancelled("PTY 启动已被取消")
        raise_if_cancelled()
        master_fd, slave_fd = pty.openpty()
        try:
            os.set_blocking(master_fd, False)
            process = subprocess.Popen(
                exec_arg,
                shell=use_shell,
                cwd=str(target),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
                close_fds=True,
                env=_subprocess_text_env(owner_home, hidden_roots=tuple(str(root) for root in sandbox.private_roots),
                                         sandboxed=True, host_credential_env_names=sandbox.host_credential_env_names),
            )
        except BaseException:
            os.close(master_fd)
            os.close(slave_fd)
            raise
        tail_hold_fd = _hold_slave_for_tail(slave_fd)
        session = PtySession(
            session_id=session_id,
            command=command,
            process=PtyOwnedProcess(process),
            tail_hold_fd=tail_hold_fd,
            master_fd=master_fd,
            started_at=time.time(),
            last_active_at=time.time(),
            access_scope=access_scope,
            execution_scope=launch.execution_scope,
            source_configurations=source_configurations,
        )
        with self._lock:
            self._sessions[session_id] = session
            cancelled = launch.cancelled
        if cancelled or cancellation_requested():
            self.close(session_id)
            raise ToolCancelled("PTY 启动已被取消")
        threading.Thread(target=self._drain, args=(session,), daemon=True).start()
        return session

    # LLM: Listing is an exact-scope projection over the bounded in-memory registry; never expose
    # sessions from another owner/conversation or use session-id knowledge as authorization.
    # 函数用途: 列出当前可信用户会话能访问的交互终端，包括已结束终端的真实状态。
    def list(self, access_scope: PtyAccessScope) -> list[PtySession]:
        with self._lock:
            sessions = [
                session
                for session in self._sessions.values()
                if session.access_scope == access_scope
            ]
        return sorted(sessions, key=lambda item: (item.started_at, item.session_id))

    # LLM: 先核原权限键和完整CAS；有写期限时内层锁与清退等待均不得重置预算，退役继续由原session负责。
    # 函数用途: 取得仍有效的会话；失效句柄拒绝复用，并在调用预算内请求原资源退出。
    def get(self, session_id: str, access_scope: PtyAccessScope | None = None, *, deadline=None) -> PtySession | None:
        with source_process_boundary(deadline=deadline):
            session = _session_in_scope(self, session_id, access_scope, deadline=deadline)
            if session is None:
                return None
            if process_source_configurations_current(
                    session.source_configurations, owner_home=session.access_scope.protected_persona_root
                    or session.access_scope.owner_home, full_access=not bool(session.access_scope.owner_home)):
                return session
            if deadline is None:
                self.close(session_id, access_scope)
            else:
                _retire_stale_for_write(self, session, deadline)
            return None

    # LLM: 通知仅进入原注册表；相关依赖完整CAS不同才处理，跨owner不擅自停止，未确认抛错阻止提交。
    # 函数用途: 切源前清退本注册表持有的旧版本PTY。
    def retire_agents_source(self, owner_home, proposed_cas):
        _retire_source_sessions(self, owner_home, proposed_cas)

    # LLM: 来源准入、write_lock和实际输入共用原deadline；FD借用允许输出排空，累计字节不被事后poll抹掉。
    # 函数用途: 分段写完一份输入，锁等待/背压共用五秒期限；未写或部分写入均带明确字节数。
    def write(
        self,
        session_id: str,
        data: bytes,
        access_scope: PtyAccessScope | None = None,
    ) -> PtySession | None:
        deadline, written = time.monotonic() + _WRITE_TIMEOUT_SECONDS, 0
        session = _get_session_for_write(self, session_id, access_scope, deadline)
        if session is None:
            return None
        while not session.write_lock.acquire(timeout=0.02):
            _check_write_deadline(deadline, written)
        try:
            if session.closed or session.process.poll() is not None:
                raise PtyWriteError("COMMAND_FAILED", 0)
            with _borrow_session_fd(session, deadline=deadline):
                _write_session_bytes(session, data, deadline)
        except PtyWriteError:
            raise
        except OSError as exc:
            raise PtyWriteError("COMMAND_FAILED", 0) from exc
        finally:
            session.write_lock.release()
        session.last_active_at = time.time()
        return session

    # LLM: TIOCSWINSZ持FD借用；不持写串行锁阻止输出排空，ESC仍不是真实resize。
    # 函数用途: 调整当前可信会话里指定 PTY 的字符行列，并让交互程序收到真实窗口尺寸变化。
    def resize(
        self,
        session_id: str,
        columns: int,
        rows: int,
        access_scope: PtyAccessScope | None = None,
    ) -> PtySession | None:
        session = self.get(session_id, access_scope)
        if session is None or session.closed or session.process.poll() is not None:
            return session
        import fcntl
        import struct
        import termios

        packed = struct.pack("HHHH", rows, columns, 0, 0)
        with _borrow_session_fd(session):
            if session.closed or session.process.poll() is not None:
                return session
            fcntl.ioctl(session.master_fd, termios.TIOCSWINSZ, packed)
        session.last_active_at = time.time()
        return session

    # LLM: 终止复用进程树和 PID 实例核对；重复 close 串行，未确认退出保留句柄和真实状态。
    # 函数用途: 关闭当前权限允许的终端及其后代，保存退出核对回执。
    def close(
        self,
        session_id: str,
        access_scope: PtyAccessScope | None = None,
    ) -> PtySession | None:
        session = _session_in_scope(self, session_id, access_scope)
        if session is None:
            return None
        _close_known_session(session)
        return session

    # LLM: 使用process_scope精确选择；父进程退出不免除后代责任，全部未退役原资源均可被停止。
    # 函数用途: 冻结任务的终端与启动预留，锁外请求清退，不扫描后来恢复的新资源。
    def request_stop(self, *, owner_home: object, thread_id: str = "", task_id: str = "",
                     run_id: str = "", attempt_id: str = "") -> tuple[str, ...]:
        if not owner_home or not ((thread_id and task_id) or run_id):
            return ()
        target = ProcessExecutionScope(
            owner_home=str(Path(str(owner_home)).expanduser().resolve(strict=False)),
            thread_id=thread_id, root_task_id=task_id, run_id=run_id, attempt_id=attempt_id,
        )

        with self._lock:
            pending = [key for key, launch in self._pending_starts.items()
                       if launch.execution_scope.matches(target)]
            for key in pending:
                self._pending_starts[key].cancelled = True
            sessions = [session for session in self._sessions.values()
                        if session.execution_scope.matches(target) and not _source_resource_retired(session)]
        for session in sessions:
            threading.Thread(target=self.close, args=(session.session_id,),
                             name=f"stop-{session.session_id}", daemon=True).start()
        return tuple(dict.fromkeys([*pending, *(session.session_id for session in sessions)]))

    # LLM: 空闲兜底是回合/任务收口之外的第三道网：没等到收口的会话（崩溃回合、宿主停摆）按 last_active_at
    #   自动回收。阈值 0=不限制；实现放模块级保持类体量在软阈值内，本方法只保留注册表 API。
    # 函数用途: 设置空闲阈值（分钟）并在开启时启动唯一巡检线程；重复设置幂等。
    def set_idle_timeout_minutes(self, minutes: object) -> None:
        _configure_idle_timeout(self, minutes)

    # LLM: 同步关闭空闲会话：进程树终止有界，巡检线程可以承受；回执就是各会话自己的 termination。
    # 函数用途: 收掉空闲超时的活跃会话，返回被收回的 session_id 元组。
    def sweep_idle(self, now: float | None = None) -> tuple[str, ...]:
        return _sweep_idle_sessions(self, now)

    # LLM: clear是关闭请求而非退出证据；未知后代必须保留在唯一注册表，不能靠清表绕过切源。
    # 函数用途: 尝试关闭全部会话，只删除完整树已确认退役的记录。
    def clear(self) -> None:
        with self._lock:
            ids = list(self._sessions)
        for session_id in ids:
            self.close(session_id)
        with self._lock:
            self._sessions = {key: value for key, value in self._sessions.items()
                              if not _source_resource_retired(value)}

    # LLM: 委派同一原会话的排空；FD读写/resize/关闭共用生命周期锁，不将无输出当退出。
    # 函数用途: 连续排空 PTY 输出，在终态清理有界历史。
    def _drain(self, session: PtySession) -> None:
        _drain_session_io(self, session)


pty_session_registry = PtySessionRegistry()


# LLM: 空闲阈值归一化、空闲巡检、终态裁剪和 fd 关闭都在模块级实现（只操作注册表/会话已有状态），
#   注册表类保留同名薄方法维持 API，类体量回到软阈值内。改动须同步类方法与调用点。
#   线程在持锁期间登记并启动：若在锁外 start，并发调用者会在“已登记未启动”的窗口里看到线程不活而再开一个
#   （ds10 初审探针实测多开 3 条）。巡检循环先 sleep 再取锁，持锁启动不会自锁。
# 函数用途: 归一化分钟阈值并在开启时启动唯一巡检线程；重复设置、并发设置都只开一条。
def _configure_idle_timeout(registry: PtySessionRegistry, minutes: object) -> None:
    try:
        value = float(minutes)
    except (TypeError, ValueError):
        value = 0.0
    registry.idle_timeout_seconds = max(0.0, value) * 60.0
    if registry.idle_timeout_seconds <= 0:
        return
    with registry._lock:
        if registry._idle_thread is not None and registry._idle_thread.is_alive():
            return
        thread = threading.Thread(target=_idle_sweeper_loop, args=(registry,),
                                  name="pty-idle-sweeper", daemon=True)
        registry._idle_thread = thread
        thread.start()


# LLM: 空闲选择覆盖所有未退役资源，父进程poll结果不解除后代责任；仅返回已请求清理的句柄。
# 函数用途: 请求收回超时终端，未确认者仍保留原注册表责任。
def _sweep_idle_sessions(registry: PtySessionRegistry, now: float | None) -> tuple[str, ...]:
    timeout = float(registry.idle_timeout_seconds or 0.0)
    if timeout <= 0:
        return ()
    moment = time.time() if now is None else float(now)
    with registry._lock:
        expired = [
            session for session in registry._sessions.values()
            if not _source_resource_retired(session) and moment - session.last_active_at > timeout
        ]
    reclaimed = []
    for session in expired:
        registry.close(session.session_id)
        reclaimed.append(session.session_id)
    with registry._lock:
        _prune_finished_sessions(registry)
    return tuple(reclaimed)


# LLM: 调用方持registry锁；只有完整树退役记录可裁剪，poll/closed/历史上限均不免除未知资源责任。
# 函数用途: 保留最近32个已确认退役历史，同时保留全部未确认资源。
def _prune_finished_sessions(registry: PtySessionRegistry) -> None:
    finished = [
        key for key, item in registry._sessions.items()
        if _source_resource_retired(item) and item.process.poll() is not None
    ]
    for key in finished[:-_MAX_SESSION_COUNT]:
        del registry._sessions[key]


# LLM: 关闭挡住新写准入并等待全部FD借用；既有写等待输出时仍允许排空，避免双向背压互等。
# 函数用途: 等实际IO结束后幂等关闭master fd；不以FD关闭冒充进程树退出。
def _close_session_fd(session: PtySession) -> None:
    with session.io_condition:
        if session.closed:
            return
        session.fd_closing = True
        while session.io_users:
            session.io_condition.wait()
        if session.closed:
            return
        session.closed = True
        for descriptor in (session.master_fd, session.tail_hold_fd):
            if descriptor < 0:
                continue
            try:
                os.close(descriptor)
            except OSError:
                pass


# LLM: 巡检周期按阈值收敛（阈值/4，夹在 0.5~30 秒）：默认 30 分钟阈值下每 30 秒查一次，
#   小阈值测试里也能在几秒内触发；线程是 daemon，不阻塞进程退出。放模块级保持注册表类体量在阈值内。
# 函数用途: 后台循环按间隔调用注册表的 sweep_idle，自动回收空闲 PTY 会话。
def _idle_sweeper_loop(registry: PtySessionRegistry) -> None:
    while True:
        interval = min(_IDLE_SWEEP_INTERVAL_SECONDS,
                       max(_MIN_IDLE_SWEEP_INTERVAL_SECONDS, registry.idle_timeout_seconds / 4.0))
        time.sleep(interval)
        try:
            registry.sweep_idle()
        except Exception:
            logging.getLogger(__name__).warning("PTY 空闲巡检失败，保留原会话；下轮重试", exc_info=False)


# LLM: 回合/任务收口的统一入口：按 (owner, thread, root task) 收整个任务的会话，再叠一次按 run 精确收，
#   覆盖任务键对不上的边角（子代理树根差异），两次命中同一会话时去重。只消费宿主已校验的结构化身份，
#   不扫全表、不按访问范围代替执行归属。请求异步发出（request_stop 内部线程终止进程树），
#   回执里的 status=requested 不代表已确认退出，确认以各会话的 termination 为准。
# 函数用途: 回合或任务结束时收回该归属的 PTY 会话，返回结构化回执供调用方记账/展示。
def reclaim_pty_sessions(owner_home: object, *, thread_id: str = "", root_task_id: str = "",
                         run_id: str = "") -> dict:
    if not str(owner_home or "").strip():
        return {"scope": "", "status": "not_selected", "session_ids": [], "error_type": ""}
    calls: list[tuple[str, dict]] = []
    if str(thread_id or "").strip() and str(root_task_id or "").strip():
        calls.append(("task", {"thread_id": thread_id, "task_id": root_task_id}))
    if str(run_id or "").strip():
        calls.append(("run", {"run_id": run_id}))
    if not calls:
        return {"scope": "", "status": "not_selected", "session_ids": [], "error_type": ""}
    found_ids: list[str] = []
    scopes: list[str] = []
    error = ""
    for scope, kwargs in calls:
        try:
            found_ids.extend(pty_session_registry.request_stop(owner_home=owner_home, **kwargs))
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            error = error or type(exc).__name__
            continue
        scopes.append(scope)
    session_ids = list(dict.fromkeys(found_ids))
    status = "requested" if session_ids else ("error" if error else "not_selected")
    return {"scope": "+".join(scopes), "status": status, "session_ids": session_ids, "error_type": error}


# LLM: 写错误携带已接受字节数；既有会话不因输入失败被销毁或假装没有执行。
# 类用途: 表达 PTY 超时、取消和部分写入。
class PtyWriteError(OSError):
    # LLM: code 与 written 为结构化事实，不能从展示文本解析。
    # 函数用途: 保存写入中止原因和已写数量。
    def __init__(self, code: str, written: int) -> None:
        super().__init__(f"{code}: 已写入 {written} 字节，剩余输入未确认；不要整段盲重放")
        self.code, self.written = code, written


# LLM: 锁等待和系统写入调用同一取消/时间判断。
# 函数用途: 在输入写入安全点停止等待并保留部分进度。
def _check_write_deadline(deadline: float, written: int) -> None:
    if cancellation_requested():
        raise PtyWriteError("CANCELLED", written)
    if time.monotonic() >= deadline:
        raise PtyWriteError("TOOL_TIMEOUT", written)


# LLM: terminal_session 必须将 start 视为一次新的可持久命令授权；list/write/read/resize/close
# 只操作当前可信会话里已批准的 PTY，不暴露系统 PID。
# 类用途: 启动并操作真实 PTY，供必须有 TTY/stdin 的交互命令使用。
class TerminalSessionTool(BaseTool):
    """Start and interact with a real pseudoterminal session."""

    model_spec = ToolModelSpec(
        name="terminal_session",
        description=(
            "启动并操作真实 PTY 交互终端会话，支持 list/start/write/read/resize/close。"
            "start 立即返回 session_id，进程可继续运行；用 read 读取后续输出，用 write 输入。"
            "不接受 run_in_background；非交互后台命令使用 run_command 的该参数。"
            "resize 必须使用结构化 columns/rows；发送终端 ESC 文本不能代替真实尺寸调整。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "start", "write", "read", "resize", "close"],
                    "description": "要执行的终端会话动作。",
                },
                "session_id": {
                    "type": "string",
                    "description": "本工具 start/list 返回的 PTY 句柄；不能填 run_command 的后台进程编号。",
                },
                "command": {"type": "string", "description": "start 所需命令。"},
                "working_dir": {
                    "type": "string",
                    "description": "start 的工作目录；省略时使用本轮可信有效目录。",
                },
                "data": {"type": "string", "description": "write 写入的文本。"},
                "append_newline": {"type": "boolean", "description": "write 后是否追加换行。"},
                "cursor": {"type": "integer", "minimum": 0, "description": "read 的增量游标。"},
                "max_bytes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_BUFFER_BYTES,
                    "description": "read 最多返回的字节数。",
                },
                "columns": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_TERMINAL_COLUMN_COUNT,
                    "description": "resize 后的终端列数。",
                },
                "rows": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_TERMINAL_ROW_COUNT,
                    "description": "resize 后的终端行数。",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="shell",
            use_cases=(
                "CLI 必须检测 TTY、显示交互提示或接收 stdin 时",
                "需要向长驻 REPL、调试器或交互安装器持续写入输入并读取输出时",
            ),
            avoid_when=(
                "一次性非交互命令继续使用 run_command",
                "纯后台批处理使用 run_command(run_in_background=true)",
            ),
            keywords=("pty", "terminal", "interactive", "stdin", "repl", "交互终端"),
            examples=(
                '{"tool":"terminal_session","action":"start","command":"python -q"}',
                '{"tool":"terminal_session","action":"write","session_id":"pty-1-...","data":"print(42)","append_newline":true}',
                '{"tool":"terminal_session","action":"read","session_id":"pty-1-...","cursor":0}',
                '{"tool":"terminal_session","action":"resize","session_id":"pty-1-...","columns":100,"rows":30}',
            ),
        ),
    )
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy(
            "dangerous",
            by_parameter=(("action", (("list", "read_only"), ("read", "read_only"))),),
        ),
        sandbox_policy=SandboxPolicy(
            "required",
            contained_by_parameter=(("action", ("resize", "close")),),
        ),
        idempotency_policy=IdempotencyPolicy("operation"),
        resource_scopes=ResourceScopePolicy(
            parameter_names=("session_id", "working_dir"),
            parameter_kinds={"session_id": "logical"},
        ),
        output_policy=OutputPolicy(trust="external_data"),
        input_policy=ToolInputPolicy(
            internal_parameters=(
                "__sandbox_write_roots",
                "__sandbox_read_roots",
                "__sandbox_protected_write_paths",
                "__sandbox_task_records",
                "__access_mode",
                "__run_scope",
            ),
            trusted_parameter_bindings=(
                (
                    "working_dir",
                    TrustedParameterBinding(
                        source_refs=("registry.effective_cwd",),
                        when=(("action", "start"),),
                    ),
                ),
            ),
        ),
        promotes_task=True,
        mutates_workspace=True,
    )

    def __init__(self, shell_tool: ShellTool):
        self.shell_tool = shell_tool

    # LLM: 能力发现与最终执行都按平台事实，不把 PowerShell 可用当成 ConPTY 可用。
    # 函数用途: 未实现 Windows PTY 时从模型可用工具快照中移除，POSIX 复用 Shell 沙箱检查。
    def availability(self) -> ToolAvailability:
        if os.name == "nt":
            return ToolAvailability.unavailable("当前节点未提供 Windows ConPTY 后端", error_code="PTY_UNAVAILABLE")
        return self.shell_tool.availability()

    # seq 253 #5：与 ShellTool 同沙箱语义——bwrap 可写全部 allowed_write_roots，
    # 执行写根经协议结构化声明，operation lock 全量覆盖（不按 internal 参数名特判）。
    def effective_write_roots(
        self,
        arguments: dict[str, Any],
        write_boundary: dict[str, Any] | None,
        workspace_root: Path,
    ) -> tuple[str, ...]:
        return self.shell_tool.effective_write_roots(arguments, write_boundary, workspace_root)

    @property
    def workspace_roots(self) -> list[Path]:
        return self.shell_tool.workspace_roots

    @workspace_roots.setter
    def workspace_roots(self, roots: list[Path]) -> None:
        self.shell_tool.workspace_roots = roots

    # LLM: 后续PTY操作重建相同宿主范围；人格根只取Shell装配，模型知道session_id也不能越owner选择会话。
    # model-visible arguments alone can never select a session.
    # 函数用途: 为列出、启动、读、写、调尺寸和关闭生成同一份 PTY 边界键。
    def _access_scope(self, params: dict[str, Any]) -> PtyAccessScope:
        return _pty_access_scope(
            self.shell_tool.path_access_policy.owner_scope_root,
            _sandbox_write_roots(params),
            _sandbox_read_roots(params),
            _sandbox_protected_write_paths(params),
            params.get("__run_scope"),
            write_scope_denied_roots=getattr(self.shell_tool, "_sandbox_forbidden_write_roots", ()),
            network_access=getattr(self.shell_tool, "_sandbox_network_access", True),
            protected_persona_root=self.shell_tool.protected_persona_root,
        )

    # LLM: Action dispatch is the sole model-facing PTY control switch. Keep schema, effect
    # mapping and this dispatch synchronized so invalid actions fail before any host mutation.
    # 函数用途: 按结构化 action 分发交互终端的列出、启动、读写、调尺寸和关闭操作。
    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        action = str(params.get("action") or "").strip().lower()
        if action == "list":
            return self._list(params)
        if action == "start":
            return self._start(params)
        if action == "write":
            return self._write(params)
        if action == "read":
            return self._read(params)
        if action == "resize":
            return self._resize(params)
        if action == "close":
            return self._close(params)
        return self._error(
            "TOOL_INVALID_ARGUMENTS",
            "action 必须是 list/start/write/read/resize/close",
        )

    # LLM: List output is bounded by the registry session cap and contains stable logical handles,
    # not OS PIDs. Status is sampled from each exact-scope process at projection time.
    # 函数用途: 显示当前用户会话创建过的交互终端及其运行状态和真实终端尺寸。
    def _list(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        sessions = pty_session_registry.list(self._access_scope(params))
        return self._ok(
            {
                "sessions": [_pty_session_summary(session) for session in sessions],
                "count": len(sessions),
            }
        )

    # LLM: PTY 启动与普通 shell 复用唯一命令解析入口；命令策略、cwd、沙箱与保护路径必须
    # 先于启动裁决；凭据来源仅由 shell_tool 的宿主 options 传入，params 不能覆盖环境上界。
    # 函数用途: 校验命令和目录后启动受沙箱保护的交互进程，返回可按会话管理的句柄。
    def _start(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        command_result = parse_shell_command(self.model_spec.name, params.get("command", ""))
        if isinstance(command_result, ToolHandlerOutcome):
            return self._error(command_result.error_code, command_result.output)
        command = command_result
        command_policy = evaluate_command_policy(command, allow_shell_operators=True)
        if not command_policy.allowed:
            return self._error(
                "COMMAND_POLICY_BLOCKED",
                "危险命令被系统拒绝: " + ",".join(command_policy.finding_codes),
            )
        write_roots = _sandbox_write_roots(params)
        read_roots = _sandbox_read_roots(params)
        protected_write_paths = _sandbox_protected_write_paths(params)
        target = self.shell_tool._execution_target(params, command)
        if isinstance(target, ToolHandlerOutcome):
            return self._error(target.error_code, target.output)
        try:
            session = pty_session_registry.start(
                command,
                target,
                self.shell_tool.path_access_policy.owner_scope_root,
                write_roots,
                read_roots,
                SandboxWriteProtection(tuple(protected_write_paths or ()), getattr(self.shell_tool, "_sandbox_forbidden_write_roots", ())),
                params.get("__run_scope"),
                host_policy=PtyHostPolicy(self.shell_tool.host_private_roots, getattr(self.shell_tool, '_sandbox_network_access', True),
                                          self.shell_tool.host_credential_env_names, self.shell_tool.protected_persona_root),
            )
        except AgentsContractError as exc:
            return self._error("SANDBOX_UNAVAILABLE", exc.code)
        except SandboxUnavailable as exc:
            return self._error("SANDBOX_UNAVAILABLE", str(exc))
        except OSError as exc:
            return self._error("COMMAND_FAILED", str(exc))
        return self._ok(
            {
                "status": "running",
                "session_id": session.session_id,
                "cursor": 0,
            }
        )

    # LLM: 不存在的精确权限句柄为 not_started；写入成功仅证明字节提交，不宣称终端中的业务已完成。
    # 函数用途: 校验输入后写入终端，区分零写入、取消和部分提交，让模型纠正工具而非误停整轮。
    def _write(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        session_id = str(params.get("session_id") or "").strip()
        data = str(params.get("data") or "")
        if bool(params.get("append_newline")):
            data += "\n"
        encoded = data.encode("utf-8")
        if not session_id or not encoded:
            return self._error("TOOL_INVALID_ARGUMENTS", "write 需要 session_id 和非空 data",
                               effect_outcome="not_started")
        if len(encoded) > _MAX_WRITE_BYTES:
            return self._error("TOOL_INVALID_ARGUMENTS", f"PTY write 超过 {_MAX_WRITE_BYTES} 字节",
                               effect_outcome="not_started")
        try:
            session = pty_session_registry.write(session_id, encoded, self._access_scope(params))
        except PtyWriteError as exc:
            return ToolHandlerOutcome("terminal_session", False, str(exc), error_code=exc.code,
                                      effect_outcome="unknown" if exc.written else "not_started",
                                      result_envelope={"bytes_written": exc.written, "bytes_requested": len(encoded)})
        except OSError as exc:
            return self._error("COMMAND_FAILED", str(exc))
        if session is None:
            return self._error("PROCESS_NOT_FOUND", f"PTY session 不存在: {session_id}；请用本工具 list 核对句柄",
                               effect_outcome="not_started")
        return self._ok({"status": "written", "session_id": session_id, "bytes": len(encoded)})

    def _read(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        session_id = str(params.get("session_id") or "").strip()
        session = pty_session_registry.get(session_id, self._access_scope(params))
        if session is None:
            return self._error("PROCESS_NOT_FOUND", f"PTY session 不存在: {session_id}")
        try:
            cursor = int(params.get("cursor") or 0)
            max_bytes = min(
                _MAX_BUFFER_BYTES, max(1, int(params.get("max_bytes") or _DEFAULT_READ_BYTES))
            )
        except (TypeError, ValueError):
            return self._error("TOOL_INVALID_ARGUMENTS", "cursor/max_bytes 必须是整数")
        chunk, next_cursor, truncated = session.read(cursor, max_bytes)
        return self._ok(
            {
                "status": "running" if session.process.poll() is None else "exited",
                "session_id": session_id,
                "exit_code": session.process.poll(),
                "output": chunk.decode("utf-8", errors="replace"),
                "cursor": next_cursor,
                "truncated_before_cursor": truncated,
            }
        )

    # LLM: 参数和句柄拒绝发生在 ioctl 前，明确未执行；真实 ioctl 异常仍不猜测最终效果。
    # 函数用途: 按结构化行列数调整终端并回报内核尺寸，避免无效句柄误触发未知副作用停机。
    def _resize(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        session_id = str(params.get("session_id") or "").strip()
        try:
            columns = int(params.get("columns"))
            rows = int(params.get("rows"))
        except (TypeError, ValueError):
            return self._error(
                "TOOL_INVALID_ARGUMENTS",
                "resize 需要 session_id、columns 和 rows",
                effect_outcome="not_started",
            )
        if (
            not session_id
            or not 1 <= columns <= _MAX_TERMINAL_COLUMN_COUNT
            or not 1 <= rows <= _MAX_TERMINAL_ROW_COUNT
        ):
            return self._error(
                "TOOL_INVALID_ARGUMENTS",
                "resize 的 columns/rows 必须在 1-1000 之间",
                effect_outcome="not_started",
            )
        try:
            session = pty_session_registry.resize(
                session_id,
                columns,
                rows,
                self._access_scope(params),
            )
        except OSError as exc:
            return self._error("COMMAND_FAILED", str(exc))
        return _resize_response(self, session_id, session)

    # LLM: close 只访问当前权限，确认进程树退出才返回 closed；无法确认沿用工具未知副作用合同，
    #   reported_error_code=PTY_CLOSE_UNCONFIRMED 说明是关闭后进程树退出未确认，终止回执结构化放进 process.termination。
    # 函数用途: 关闭终端并返回真实核对结果，缺失句柄与未确认终止分别处理。
    def _close(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        session_id = str(params.get("session_id") or "").strip()
        session = pty_session_registry.close(session_id, self._access_scope(params))
        if session is None:
            return self._error("PROCESS_NOT_FOUND", f"PTY session 不存在: {session_id}",
                               effect_outcome="not_started")
        if session.termination is None or not session.termination.confirmed:
            facts: dict[str, object] = {"session_id": session_id}
            if session.termination is not None:
                facts["termination"] = asdict(session.termination)
            return ToolHandlerOutcome(self.model_spec.name, False, "已请求关闭终端，但尚未确认进程树退出。",
                                      error_code="TOOL_OPERATION_OUTCOME_UNKNOWN", effect_outcome="unknown",
                                      reported_error_code="PTY_CLOSE_UNCONFIRMED", result_envelope={"process": facts})
        return self._ok(
            {
                "status": "closed",
                "session_id": session_id,
                "exit_code": session.process.poll(),
            }
        )

    def _ok(self, payload: dict[str, Any]) -> ToolHandlerOutcome:
        return ToolHandlerOutcome(
            self.model_spec.name, True, json.dumps(payload, ensure_ascii=False)
        )

    # LLM: effect_outcome 只接受调用点掌握的执行事实，不能按错误文案猜测或统一放宽所有失败。
    # 函数用途: 构造工具失败结果，让执行器正确区分未执行与可能已发生的副作用。
    def _error(self, code: str, message: str, *, effect_outcome: str = "") -> ToolHandlerOutcome:
        return ToolHandlerOutcome(self.model_spec.name, False, message, error_code=code,
                                  effect_outcome=effect_outcome)


# LLM: fd 关闭不等于进程退出；以 poll 优先，PID 保持私有，不能把失去终端输出当作停止成功。
# 函数用途: 投影终端真实状态和尺寸，供精确权限下的列表展示。
def _pty_session_summary(session: PtySession) -> dict[str, object]:
    status = (
        "running" if session.process.poll() is None else ("closed" if session.closed else "exited")
    )
    try:
        size = os.get_terminal_size(session.master_fd)
        columns, rows = size.columns, size.lines
    except OSError:
        columns, rows = None, None
    return {
        "session_id": session.session_id,
        "status": status,
        "command": session.command,
        "columns": columns,
        "rows": rows,
        "started_at": session.started_at,
        "last_active_at": session.last_active_at,
    }


__all__ = ["PtySessionRegistry", "TerminalSessionTool", "pty_session_registry", "reclaim_pty_sessions"]


# LLM: 原session串行清退；新PTY先保留组长身份再拍树快照，旧已回收PID不得补发信号或冒充后代已退役。
# 函数用途: 安全关闭原进程树并保留真实回执，只有确认后才关闭FD。
def _close_known_session(session):
    with session.close_lock:
        if session.termination is None or not session.termination.confirmed:
            session.termination = _retire_session_process(session.process)
        if session.termination.confirmed:
            _finish_session_output(session)


# LLM: 只使用session持有的原句柄；WNOWAIT包装器负责后代清退，旧句柄沿用原终止协议。
# 函数用途: 为一种PTY进程句柄取得真实清退回执。
def _retire_session_process(process):
    if isinstance(process, PtyOwnedProcess):
        return process.retire(terminate_process_tree)
    code = process.poll()
    if code is not None:
        return ProcessTerminationReceipt("already_exited", True, code, 1)
    return terminate_process_tree(process.pid, process)


# LLM: 只在完整树清退后调用；读取与append共用read_lock，关FD前排尽内核已产生的尾字节。
# 函数用途: 串行保存退出前最后输出，不能拿之前select超时当作输出已排空。
def _finish_session_output(session):
    with session.read_lock:
        _drain_terminal_tail(session)
        _close_session_fd(session)


# LLM: 调用方持read_lock且完整进程树已退役；逐块读取直到无尾字节，再由调用方关闭FD。
# 函数用途: 把内核中剩余的终端输出全部保存。
def _drain_terminal_tail(session):
    while not session.closed:
        chunk = _read_terminal_tail(session)
        if not chunk:
            break
        session.append(chunk)


# LLM: 只在read_lock内且进程树已退役后借用master；EAGAIN/EOF维持原尾部结束语义。
# 函数用途: 读取退出后当前已缓冲的一块尾字节，不等待未来输出。
def _read_terminal_tail(session):
    try:
        with _borrow_session_fd(session, draining=True):
            return os.read(session.master_fd, 4096)
    except OSError:
        return b""


# LLM: deadline来自原write；无期限路径维持原锁语义，超时不能在未获得锁时访问注册表。
# 函数用途: 在输入的剩余预算内取得一个内部锁。
@contextmanager
def _session_lock(lock, deadline=None):
    if deadline is None:
        lock.acquire()
    else:
        _check_write_deadline(deadline, 0)
        if not lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise PtyWriteError("TOOL_TIMEOUT", 0)
    try:
        if deadline is not None:
            _check_write_deadline(deadline, 0)
        yield
    finally:
        lock.release()


# LLM: 每个session最多一条在途清退线程；写超时不删除句柄、不伪造退出，原线程继续负责释放。
# 函数用途: 对失效PTY请求原资源退出，只在剩余输入预算内等待。
def _retire_stale_for_write(registry, session, deadline):
    with _session_lock(registry._lock, deadline):
        worker = session.retirement_thread
        if worker is None or not worker.is_alive():
            worker = threading.Thread(target=_close_known_session, args=(session,),
                                      name=f"retire-{session.session_id}", daemon=True)
            session.retirement_thread = worker
            worker.start()
    while worker.is_alive():
        _check_write_deadline(deadline, 0)
        worker.join(timeout=min(.02, max(0.0, deadline - time.monotonic())))
    _check_write_deadline(deadline, 0)


# LLM: 只按原注册表权限键查找，不核来源；close须可清理失效版本，不能通过get递归进入自身。
# 函数用途: 在原锁下找权限允许的会话，写路径锁等待不能超出调用预算。
def _session_in_scope(registry, session_id, access_scope, *, deadline=None):
    with _session_lock(registry._lock, deadline):
        session = registry._sessions.get(session_id)
    if session is not None and access_scope is not None and session.access_scope != access_scope:
        return None
    return session


# LLM: 不复制资源账；只处理结构化来源CAS命中的会话，树回执或自然退出后代未知不能冒充确认。
# 函数用途: 逐个要求旧PTY停止；外owner相关资源或未确认停止使切源失败关闭。
def _retire_source_sessions(registry, owner_home, proposed_cas):
    with registry._lock:
        sessions = tuple(registry._sessions.values())
    for session in sessions:
        if _source_resource_retired(session):
            continue
        own_home = session.access_scope.protected_persona_root or session.access_scope.owner_home
        if not process_source_change_affects(session.source_configurations, owner_home, proposed_cas,
                                             bound_scope=(own_home, not bool(session.access_scope.owner_home))):
            continue
        if own_home != owner_home:
            raise AgentsContractError("AGENTS_SOURCE_RESOURCE_UNCONFIRMED", "cross_owner_resource")
        registry.close(session.session_id)
        receipt = session.termination
        if receipt is None or not receipt.confirmed or receipt.method == "already_exited":
            raise AgentsContractError("AGENTS_SOURCE_RESOURCE_UNCONFIRMED", "pty_resource")


# LLM: 仅已有确认的完整树退役回执可略过历史资源；closed、poll或already_exited都不能证明后代清理。
# 函数用途: 避免已确认退役历史会话阻断新owner，同时保留未知资源失败关闭。
def _source_resource_retired(session):
    receipt = session.termination
    return bool(session.closed and receipt is not None and receipt.confirmed and receipt.method != "already_exited")


# LLM: 复用同一可重入来源锁，get仍核原权限和CAS；等锁超时必须报零写入，不得删锁绕过准入。
# 函数用途: 把PTY来源检查的锁等待纳入输入的原始期限。
def _get_session_for_write(registry, session_id, access_scope, deadline):
    _check_write_deadline(deadline, 0)
    try:
        with source_process_boundary(deadline=deadline):
            session = registry.get(session_id, access_scope, deadline=deadline)
    except TimeoutError as exc:
        raise PtyWriteError("TOOL_TIMEOUT", 0) from exc
    _check_write_deadline(deadline, 0)
    return session


# LLM: macOS 在最后一个 slave FD 关闭时会丢弃 master 里尚未读走的输出；父进程留一份 slave 直到尾部排空，
#   这样退出前的最后输出仍可读到（读完返回 EAGAIN 而不是 EOF，终态由组长退出观察决定）。其它平台照旧立即关闭。
# 函数用途: 决定启动后父进程是否保留 slave FD，返回保留的 FD 或 -1。
def _hold_slave_for_tail(slave_fd: int) -> int:
    if sys.platform == "darwin":
        return slave_fd
    os.close(slave_fd)
    return -1


# LLM: 每个可读块和无数据分支均检查父终态；读取append串行，清退不持read锁，避免与显式close倒锁。
# 函数用途: 持续排空并及时清退自然退出的终端，清退时保存最后输出。
def _drain_session_io(registry, session):
    while not session.closed:
        try:
            chunk = _read_and_append_session(session)
        except OSError:
            break
        if isinstance(session.process, PtyOwnedProcess) and session.process.poll() is not None:
            _close_known_session(session)
        if chunk == b"":
            break
    _close_session_fd(session)
    if isinstance(session.process, PtyOwnedProcess):
        session.process.wait()
        _close_known_session(session)
    with registry._lock:
        _prune_finished_sessions(registry)


# LLM: 一个读者取得的字节必须在下一个读者前append；不持close锁，避免反向等待。
# 函数用途: 将终端读取和输出缓冲追加作为一个有序操作。
def _read_and_append_session(session):
    with session.read_lock:
        try:
            chunk = _poll_session_chunk(session)
        except BlockingIOError:
            return None
        if chunk:
            session.append(chunk)
        return chunk


# LLM: 等就绪不持写锁；实际read借用同一FD生命周期，允许与输入并行但不能读复用FD。
# 函数用途: 单次有界读取，None表示暂时无输出，空字节表示终态。
def _poll_session_chunk(session):
    if not select.select([session.master_fd], [], [], 0.1)[0]:
        return None
    with _borrow_session_fd(session, draining=True):
        return b"" if session.closed else os.read(session.master_fd, 4096)


# LLM: 借用入口锁等待遵守原write期限；最终计数释放必须完成，不能因超时丢失尚在使用的FD责任。
# 函数用途: 让双向读写共享活FD，closing时只排空既有IO，最后释放唤醒close。
@contextmanager
def _borrow_session_fd(session, *, draining=False, deadline=None):
    with _session_lock(session.io_condition, deadline):
        if session.closed or (session.fd_closing and (not draining or not session.io_users)):
            raise OSError("PTY_FD_CLOSED")
        session.io_users += 1
    try:
        yield
    finally:
        with session.io_condition:
            session.io_users -= 1
            session.io_condition.notify_all()


# LLM: 调用方持整份写串行锁及FD借用；累计真实字节和原deadline，部分错误不能改报零字节。
# 函数用途: 在有界期限内完成输入，同时允许排空线程持续消费输出。
def _write_session_bytes(session, data, deadline):
    written = 0
    while written < len(data):
        _check_write_deadline(deadline, written)
        written += _write_session_chunk(session, data[written:], written)


# LLM: 仅格式化已按原权限解析的resize会话；size读回须借用原FD，保留原错误码/副作用分类。
# 函数用途: 返回内核实际尺寸，不读取已经关闭或复用的终端FD。
def _resize_response(tool, session_id, session):
    if session is None:
        return tool._error("PROCESS_NOT_FOUND", f"PTY session 不存在: {session_id}", effect_outcome="not_started")
    if session.process.poll() is not None or session.closed:
        return tool._error("COMMAND_FAILED", f"PTY session 已结束: {session_id}")
    try:
        with _borrow_session_fd(session):
            actual = os.get_terminal_size(session.master_fd)
    except OSError as exc:
        return tool._error("COMMAND_FAILED", str(exc))
    return tool._ok({"status": "resized", "session_id": session_id, "columns": actual.columns, "rows": actual.lines})


# LLM: 不重新借用FD、不改计时；EAGAIN仅本轮无进展，真实错误保留此前累计字节。
# 函数用途: 单次等待可写并提交剩余输入，精确保留部分写状态。
def _write_session_chunk(session, data, written):
    try:
        return os.write(session.master_fd, data) if select.select([], [session.master_fd], [], .02)[1] else 0
    except BlockingIOError:
        return 0
    except OSError as exc:
        raise PtyWriteError("PTY_WRITE_FAILED", written) from exc
