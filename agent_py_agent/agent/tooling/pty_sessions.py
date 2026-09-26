# LLM: PTY 使用 process_scope 的执行身份合同；注册表独占容量、启动预留与终止回执，身份不授予访问权限。
# 模块用途: 提供有界交互终端并按任务归属收回进程；Windows 暂无 ConPTY，不虚报支持。
from __future__ import annotations

"""Bounded interactive PTY sessions using the same shell policy and sandbox gate."""

import json
import os
import select
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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

_MAX_SESSIONS = 32
_MAX_BUFFER_BYTES = 1_000_000
_MAX_WRITE_BYTES = 64_000
_WRITE_TIMEOUT_SECONDS = 5.0
_DEFAULT_READ_BYTES = 32_000
_MAX_TERMINAL_COLUMNS = 1000
_MAX_TERMINAL_ROWS = 1000


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
    close_lock: threading.Lock = field(default_factory=threading.Lock)
    closed: bool = False
    termination: ProcessTerminationReceipt | None = None

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


# LLM: This value is the canonical equality key for PTY ownership and filesystem boundaries.
# 类用途: 冻结 PTY 启动时的 owner、可读、可写和受保护路径。
@dataclass(frozen=True)
class PtyAccessScope:
    owner_home: str
    conversation_id: str
    write_roots: tuple[str, ...] | None
    read_roots: tuple[str, ...] | None
    protected_write_paths: tuple[str, ...] | None


# LLM: 一次 PTY 启动的沙箱输入原样收拢，不做任何推断；字段与 tooling/shell._sandbox_exec 的同名参数一一对应，
#   private_root 是 my-agent 家目录根（owner 隔离时由沙箱拒绝读取其中本 owner 以外的部分）。
# 类用途: 把 owner、读写根、受保护路径和私有根作为一个整体交给 _spawn。
@dataclass(frozen=True)
class _PtySandboxInputs:
    owner_home: object = None
    write_roots: tuple[Path, ...] | None = None
    read_roots: tuple[Path, ...] | None = None
    protected_write_paths: tuple[Path, ...] | None = None
    private_root: object = None


# LLM: 预留和取消标记共用 registry 锁；取消只命中当时的启动，不阻止用户后续显式恢复。
# 类用途: 在 Popen 尚未返回时保留执行归属，让明确的资源停止请求覆盖尚未完成的启动。
@dataclass
class _PtyStart:
    execution_scope: ProcessExecutionScope
    cancelled: bool = False


# LLM: Normalize every PTY scope path before comparing session ownership; missing lists retain
# their distinct "not constrained by this field" meaning instead of becoming empty deny lists.
# 函数用途: 把工具边界规范成可稳定比较的 PTY 权限快照。
def _pty_access_scope(
    owner_home: object,
    write_roots: tuple[Any, ...] | None,
    read_roots: tuple[Any, ...] | None = None,
    protected_write_paths: tuple[Any, ...] | None = None,
    run_scope: object = None,
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
    normalized_protected = None
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

    # LLM: 容量和执行身份一起预留；所有 spawn 成败路径释放同一预留，不吞掉宿主取消。
    # 函数用途: 有界创建终端，让明确的资源停止请求可以命中尚未完成的启动。
    def start(
        self,
        command: str,
        target,
        owner_home: object = None,
        write_roots: tuple[Path, ...] | None = None,
        read_roots: tuple[Path, ...] | None = None,
        protected_write_paths: tuple[Path, ...] | None = None,
        run_scope: object = None,
        *,
        private_root: object = None,
    ) -> PtySession:
        if os.name == "nt":
            raise OSError("PTY_UNAVAILABLE: Windows requires a ConPTY backend")
        raise_if_cancelled()
        execution_scope = ProcessExecutionScope.from_run_scope(run_scope, owner_home)
        with self._lock:
            self._prune_finished()
            active = sum(session.process.poll() is None for session in self._sessions.values())
            if active + len(self._pending_starts) >= _MAX_SESSIONS:
                raise OSError(f"PTY_SESSION_LIMIT: active session limit is {_MAX_SESSIONS}")
            self._counter += 1
            session_id = f"pty-{self._counter}-{int(time.time())}"
            self._pending_starts[session_id] = _PtyStart(execution_scope)
        try:
            sandbox = _PtySandboxInputs(owner_home, write_roots, read_roots, protected_write_paths, private_root)
            return self._spawn(session_id, command, target, sandbox, run_scope)
        finally:
            with self._lock:
                self._pending_starts.pop(session_id, None)

    # LLM: Popen 前后都核对同一预留；取消后的迟到进程仍登记并终止，不能返回运行成功。
    # 函数用途: 启动 PTY 并排空输出，封住启动期间收到停止后留下孤儿进程的窗口。
    def _spawn(self, session_id, command, target, sandbox: _PtySandboxInputs, run_scope) -> PtySession:
        import pty

        owner_home = sandbox.owner_home
        access_scope = _pty_access_scope(
            owner_home,
            sandbox.write_roots,
            sandbox.read_roots,
            sandbox.protected_write_paths,
            run_scope,
        )
        exec_arg, use_shell = _sandbox_exec(
            command,
            target,
            owner_home,
            write_roots=sandbox.write_roots,
            read_roots=sandbox.read_roots,
            protected_write_paths=sandbox.protected_write_paths,
            private_root=sandbox.private_root,
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
                env=_subprocess_text_env(owner_home),
            )
        except Exception:
            os.close(master_fd)
            os.close(slave_fd)
            raise
        os.close(slave_fd)
        session = PtySession(
            session_id=session_id,
            command=command,
            process=process,
            master_fd=master_fd,
            started_at=time.time(),
            last_active_at=time.time(),
            access_scope=access_scope,
            execution_scope=launch.execution_scope,
        )
        with self._lock:
            self._sessions[session_id] = session
            cancelled = launch.cancelled
        if cancelled or cancellation_requested():
            self.close(session_id)
            raise ToolCancelled("PTY 启动已被取消")
        threading.Thread(target=self._drain, args=(session,), daemon=True).start()
        return session

    # LLM: 调用方持有 registry 锁；仅淘汰已结束且 fd 已关闭的历史，不中断任何活跃进程。
    # 函数用途: 保留最近 32 个已结束 PTY，避免常驻 Gateway 累积历史对象和缓冲。
    def _prune_finished(self) -> None:
        finished = [key for key, item in self._sessions.items() if item.closed and item.process.poll() is not None]
        for key in finished[:-_MAX_SESSIONS]:
            del self._sessions[key]

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

    def get(self, session_id: str, access_scope: PtyAccessScope | None = None) -> PtySession | None:
        with self._lock:
            session = self._sessions.get(session_id)
        if (
            session is not None
            and access_scope is not None
            and session.access_scope != access_scope
        ):
            return None
        return session

    # LLM: 写入前的退出和部分写入分开报告；成功返回证明全部字节已提交，不能再用事后 poll 抹掉此事实。
    # 函数用途: 分段写完一份输入，锁等待/背压共用五秒期限；未写或部分写入均带明确字节数。
    def write(
        self,
        session_id: str,
        data: bytes,
        access_scope: PtyAccessScope | None = None,
    ) -> PtySession | None:
        session = self.get(session_id, access_scope)
        if session is None:
            return None
        deadline, written = time.monotonic() + _WRITE_TIMEOUT_SECONDS, 0
        while not session.write_lock.acquire(timeout=0.02):
            _check_write_deadline(deadline, written)
        try:
            if session.closed or session.process.poll() is not None:
                raise PtyWriteError("COMMAND_FAILED", 0)
            while written < len(data):
                _check_write_deadline(deadline, written)
                try:
                    if select.select([], [session.master_fd], [], 0.02)[1]:
                        written += os.write(session.master_fd, data[written:])
                except BlockingIOError:
                    continue
                except OSError as exc:
                    raise PtyWriteError("PTY_WRITE_FAILED", written) from exc
        finally:
            session.write_lock.release()
        session.last_active_at = time.time()
        return session

    # LLM: PTY geometry changes must use the kernel TIOCSWINSZ control, matching 会话运行时 resizePty;
    # terminal escape bytes are ordinary application output and must never stand in for this fact.
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
        session = self.get(session_id, access_scope)
        if session is None:
            return None
        with session.close_lock:
            if session.termination is None or not session.termination.confirmed:
                code = session.process.poll()
                session.termination = (
                    ProcessTerminationReceipt("already_exited", True, code, 1)
                    if code is not None else terminate_process_tree(session.process.pid, session.process)
                )
            if session.termination.confirmed:
                self._close_fd(session)
        return session

    # LLM: 使用 process_scope 的精确选择合同，不以访问范围代替执行归属。
    # 在锁内冻结句柄和取消预留，锁外异步终止；晚到恢复轮不能被旧停止重新扫描命中。
    # 函数用途: 明确停止任务资源或取消子代理时圈定其终端，回收进程而不阻塞控制应答。
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
                        if session.execution_scope.matches(target) and session.process.poll() is None]
        for session in sessions:
            threading.Thread(target=self.close, args=(session.session_id,),
                             name=f"stop-{session.session_id}", daemon=True).start()
        return tuple(dict.fromkeys([*pending, *(session.session_id for session in sessions)]))

    def clear(self) -> None:
        with self._lock:
            ids = list(self._sessions)
        for session_id in ids:
            self.close(session_id)
        with self._lock:
            self._sessions.clear()

    # LLM: fd 为非阻塞，暂时无输出不等于退出；只有 EOF/真实错误才关闭。
    # 函数用途: 连续排空 PTY 输出，在终态清理有界历史。
    def _drain(self, session: PtySession) -> None:
        while not session.closed:
            try:
                if not select.select([session.master_fd], [], [], 0.1)[0]:
                    continue
                chunk = os.read(session.master_fd, 4096)
            except BlockingIOError:
                continue
            except OSError:
                break
            if not chunk:
                break
            session.append(chunk)
        self._close_fd(session)
        with self._lock:
            self._prune_finished()

    @staticmethod
    def _close_fd(session: PtySession) -> None:
        with session.lock:
            if session.closed:
                return
            session.closed = True
            try:
                os.close(session.master_fd)
            except OSError:
                pass


pty_session_registry = PtySessionRegistry()


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
                    "maximum": _MAX_TERMINAL_COLUMNS,
                    "description": "resize 后的终端列数。",
                },
                "rows": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_TERMINAL_ROWS,
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

    # LLM: PTY follow-up actions must reconstruct the exact host-only scope from internal params;
    # model-visible arguments alone can never select a session.
    # 函数用途: 为列出、启动、读、写、调尺寸和关闭生成同一份 PTY 边界键。
    def _access_scope(self, params: dict[str, Any]) -> PtyAccessScope:
        return _pty_access_scope(
            self.shell_tool.path_access_policy.owner_scope_root,
            _sandbox_write_roots(params),
            _sandbox_read_roots(params),
            _sandbox_protected_write_paths(params),
            params.get("__run_scope"),
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
    # 先于进程启动裁决。解析接口改动时同步 shell 和 PTY 回归，不能调用已删除的私有方法。
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
                protected_write_paths,
                params.get("__run_scope"),
                private_root=self.shell_tool.host_private_root,
            )
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
            or not 1 <= columns <= _MAX_TERMINAL_COLUMNS
            or not 1 <= rows <= _MAX_TERMINAL_ROWS
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
        if session is None:
            return self._error("PROCESS_NOT_FOUND", f"PTY session 不存在: {session_id}",
                               effect_outcome="not_started")
        if session.process.poll() is not None or session.closed:
            return self._error("COMMAND_FAILED", f"PTY session 已结束: {session_id}")
        actual = os.get_terminal_size(session.master_fd)
        return self._ok(
            {
                "status": "resized",
                "session_id": session_id,
                "columns": actual.columns,
                "rows": actual.lines,
            }
        )

    # LLM: close 只访问当前权限，确认进程树退出才返回 closed；无法确认沿用工具未知副作用合同。
    # 函数用途: 关闭终端并返回真实核对结果，缺失句柄与未确认终止分别处理。
    def _close(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        session_id = str(params.get("session_id") or "").strip()
        session = pty_session_registry.close(session_id, self._access_scope(params))
        if session is None:
            return self._error("PROCESS_NOT_FOUND", f"PTY session 不存在: {session_id}",
                               effect_outcome="not_started")
        if session.termination is None or not session.termination.confirmed:
            return self._error("TOOL_OPERATION_OUTCOME_UNKNOWN", "已请求关闭终端，但尚未确认进程树退出。",
                               effect_outcome="unknown")
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


__all__ = ["PtySessionRegistry", "TerminalSessionTool", "pty_session_registry"]
