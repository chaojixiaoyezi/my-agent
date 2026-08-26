from __future__ import annotations

"""Bounded interactive PTY sessions using the same shell policy and sandbox gate."""

import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..contracts.gates.command_policy import evaluate_command_policy
from .models import (
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    OutputPolicy,
    ResourceScopePolicy,
    SandboxPolicy,
    ToolHandlerOutcome,
    ToolInputPolicy,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
    TrustedParameterBinding,
)
from .sandbox import SandboxUnavailable
from .shell import (
    ShellTool,
    _sandbox_exec,
    _sandbox_read_roots,
    _sandbox_write_roots,
    _subprocess_text_env,
)

_MAX_SESSIONS = 32
_MAX_BUFFER_BYTES = 1_000_000
_MAX_WRITE_BYTES = 64_000
_DEFAULT_READ_BYTES = 32_000


@dataclass
class PtySession:
    session_id: str
    command: str
    process: subprocess.Popen
    master_fd: int
    started_at: float
    last_active_at: float
    access_scope: PtyAccessScope
    output: bytearray = field(default_factory=bytearray)
    base_cursor: int = 0
    next_cursor: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)
    closed: bool = False

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


@dataclass(frozen=True)
class PtyAccessScope:
    owner_home: str
    write_roots: tuple[str, ...] | None
    read_roots: tuple[str, ...] | None


def _pty_access_scope(
    owner_home: object,
    write_roots: tuple[Any, ...] | None,
    read_roots: tuple[Any, ...] | None = None,
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
    return PtyAccessScope(
        owner_home=owner,
        write_roots=normalized_roots,
        read_roots=normalized_read_roots,
    )


class PtySessionRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, PtySession] = {}
        self._counter = 0
        self._lock = threading.Lock()

    def start(
        self,
        command: str,
        target,
        owner_home: object = None,
        write_roots: tuple[Path, ...] | None = None,
        read_roots: tuple[Path, ...] | None = None,
    ) -> PtySession:
        if os.name == "nt":
            raise OSError("PTY_UNAVAILABLE: Windows requires a ConPTY backend")
        import pty

        with self._lock:
            active = sum(session.process.poll() is None for session in self._sessions.values())
            if active >= _MAX_SESSIONS:
                raise OSError(f"PTY_SESSION_LIMIT: active session limit is {_MAX_SESSIONS}")
            self._counter += 1
            session_id = f"pty-{self._counter}-{int(time.time())}"
        access_scope = _pty_access_scope(owner_home, write_roots, read_roots)
        exec_arg, use_shell = _sandbox_exec(
            command,
            target,
            owner_home,
            write_roots=write_roots,
            read_roots=read_roots,
        )
        master_fd, slave_fd = pty.openpty()
        try:
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
        )
        with self._lock:
            self._sessions[session_id] = session
        threading.Thread(target=self._drain, args=(session,), daemon=True).start()
        return session

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

    def write(
        self,
        session_id: str,
        data: bytes,
        access_scope: PtyAccessScope | None = None,
    ) -> PtySession | None:
        session = self.get(session_id, access_scope)
        if session is None or session.closed or session.process.poll() is not None:
            return session
        os.write(session.master_fd, data)
        session.last_active_at = time.time()
        return session

    def close(
        self,
        session_id: str,
        access_scope: PtyAccessScope | None = None,
    ) -> PtySession | None:
        session = self.get(session_id, access_scope)
        if session is None:
            return None
        if session.process.poll() is None:
            try:
                os.killpg(os.getpgid(session.process.pid), signal.SIGTERM)
                session.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(session.process.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        self._close_fd(session)
        return session

    def clear(self) -> None:
        with self._lock:
            ids = list(self._sessions)
        for session_id in ids:
            self.close(session_id)
        with self._lock:
            self._sessions.clear()
            self._counter = 0

    def _drain(self, session: PtySession) -> None:
        while not session.closed:
            try:
                chunk = os.read(session.master_fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            session.append(chunk)
        self._close_fd(session)

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


# LLM: terminal_session 必须将 start 视为一次新的可持久命令授权；write/read/close 只操作已批准会话。
# 类用途: 启动并操作真实 PTY，供必须有 TTY/stdin 的交互命令使用。
class TerminalSessionTool(BaseTool):
    """Start and interact with a real pseudoterminal session."""

    model_spec = ToolModelSpec(
        name="terminal_session",
        description="启动并操作真实 PTY 交互终端会话，支持 start/write/read/close。",
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["start", "write", "read", "close"], "description": "要执行的终端会话动作。"},
                "session_id": {"type": "string", "description": "write/read/close 所需 PTY session id。"},
                "command": {"type": "string", "description": "start 所需命令。"},
                "working_dir": {"type": "string", "description": "start 的工作目录；省略时使用本轮可信有效目录。"},
                "data": {"type": "string", "description": "write 写入的文本。"},
                "append_newline": {"type": "boolean", "description": "write 后是否追加换行。"},
                "cursor": {"type": "integer", "minimum": 0, "description": "read 的增量游标。"},
                "max_bytes": {"type": "integer", "minimum": 1, "maximum": _MAX_BUFFER_BYTES, "description": "read 最多返回的字节数。"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="shell",
            use_cases=("CLI 必须检测 TTY、显示交互提示或接收 stdin 时", "需要向长驻 REPL、调试器或交互安装器持续写入输入并读取输出时"),
            avoid_when=("一次性非交互命令继续使用 run_command", "纯后台批处理使用 run_command(run_in_background=true)"),
            keywords=("pty", "terminal", "interactive", "stdin", "repl", "交互终端"),
            examples=(
                '{"tool":"terminal_session","action":"start","command":"python -q"}',
                '{"tool":"terminal_session","action":"write","session_id":"pty-1-...","data":"print(42)","append_newline":true}',
                '{"tool":"terminal_session","action":"read","session_id":"pty-1-...","cursor":0}',
            ),
        ),
    )
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy(
            "dangerous",
            by_parameter=(("action", (("read", "read_only"),)),),
        ),
        sandbox_policy=SandboxPolicy(
            "required",
            uncontained_by_parameter=(("action", ("start",)),),
        ),
        idempotency_policy=IdempotencyPolicy("operation"),
        resource_scopes=ResourceScopePolicy(
            parameter_names=("session_id", "working_dir"),
            parameter_kinds={"session_id": "logical"},
        ),
        output_policy=OutputPolicy(trust="external_data"),
        input_policy=ToolInputPolicy(
            internal_parameters=("__sandbox_write_roots", "__sandbox_read_roots", "__access_mode"),
            trusted_parameter_bindings=((
                "working_dir",
                TrustedParameterBinding(
                    source_refs=("registry.effective_cwd",),
                    when=(("action", "start"),),
                ),
            ),),
        ),
        promotes_task=True,
        mutates_workspace=True,
    )

    def __init__(self, shell_tool: ShellTool):
        self.shell_tool = shell_tool

    # seq 253 #5：与 ShellTool 同沙箱语义——bwrap 可写全部 allowed_write_roots，
    # 执行写根经协议结构化声明，operation lock 全量覆盖（不按 internal 参数名特判）。
    def effective_write_roots(
        self,
        arguments: dict[str, Any],
        write_boundary: dict[str, Any] | None,
        workspace_root: Path,
    ) -> tuple[str, ...]:
        return self.shell_tool.effective_write_roots(
            arguments, write_boundary, workspace_root
        )

    @property
    def workspace_roots(self) -> list[Path]:
        return self.shell_tool.workspace_roots

    @workspace_roots.setter
    def workspace_roots(self, roots: list[Path]) -> None:
        self.shell_tool.workspace_roots = roots

    def _access_scope(self, params: dict[str, Any]) -> PtyAccessScope:
        return _pty_access_scope(
            self.shell_tool.path_access_policy.owner_scope_root,
            _sandbox_write_roots(params),
            _sandbox_read_roots(params),
        )

    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        action = str(params.get("action") or "").strip().lower()
        if action == "start":
            return self._start(params)
        if action == "write":
            return self._write(params)
        if action == "read":
            return self._read(params)
        if action == "close":
            return self._close(params)
        return self._error("TOOL_INVALID_ARGUMENTS", "action 必须是 start/write/read/close")

    def _start(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        command_result = self.shell_tool._parse_command(params)
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
            )
        except SandboxUnavailable as exc:
            return self._error("SANDBOX_UNAVAILABLE", str(exc))
        except OSError as exc:
            return self._error("COMMAND_FAILED", str(exc))
        return self._ok(
            {
                "status": "running",
                "session_id": session.session_id,
                "pid": session.process.pid,
                "cursor": 0,
            }
        )

    def _write(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        session_id = str(params.get("session_id") or "").strip()
        data = str(params.get("data") or "")
        if bool(params.get("append_newline")):
            data += "\n"
        encoded = data.encode("utf-8")
        if not session_id or not encoded:
            return self._error("TOOL_INVALID_ARGUMENTS", "write 需要 session_id 和非空 data")
        if len(encoded) > _MAX_WRITE_BYTES:
            return self._error("TOOL_INVALID_ARGUMENTS", f"PTY write 超过 {_MAX_WRITE_BYTES} 字节")
        try:
            session = pty_session_registry.write(session_id, encoded, self._access_scope(params))
        except OSError as exc:
            return self._error("COMMAND_FAILED", str(exc))
        if session is None:
            return self._error("PROCESS_NOT_FOUND", f"PTY session 不存在: {session_id}")
        if session.process.poll() is not None or session.closed:
            return self._error("COMMAND_FAILED", f"PTY session 已结束: {session_id}")
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

    def _close(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        session_id = str(params.get("session_id") or "").strip()
        session = pty_session_registry.close(session_id, self._access_scope(params))
        if session is None:
            return self._error("PROCESS_NOT_FOUND", f"PTY session 不存在: {session_id}")
        return self._ok(
            {
                "status": "closed",
                "session_id": session_id,
                "exit_code": session.process.poll(),
            }
        )

    def _ok(self, payload: dict[str, Any]) -> ToolHandlerOutcome:
        return ToolHandlerOutcome(self.model_spec.name, True, json.dumps(payload, ensure_ascii=False))

    def _error(self, code: str, message: str) -> ToolHandlerOutcome:
        return ToolHandlerOutcome(self.model_spec.name, False, message, error_code=code)


__all__ = ["PtySessionRegistry", "TerminalSessionTool", "pty_session_registry"]
