"""宿主私密凭据子进程材料化（0700/0600 无名只读 FD）、单次授权消费句柄与 C6 结构化诊断。"""
from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import shutil
import stat
import tempfile
import time
from collections.abc import Iterable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..conversation.im_tool_approval import approval_recipient
from ..conversation.tool_approval_scope import tool_approval_scope, tool_approval_scope_is_current
from .owner_resolver import owner_identity_from_config
from .task_credential_schema import credential_ready_expired
from .task_credential_storage import CredentialInputError, read_credential_row
from .task_credentials import (
    credential_origin,
    credential_path,
)

CREDENTIAL_REF_PATTERN = r"^cred_[0-9a-f]{32}$"
CREDENTIAL_FILE_ENV = "MY_AGENT_CREDENTIAL_FILE"
LINUX_CHILD_CREDENTIAL_DIR = "/dev/shm/.my-agent-credential"
LINUX_CHILD_CREDENTIAL_MOUNT = f"{LINUX_CHILD_CREDENTIAL_DIR}/credential.json"
_CREDENTIAL_REF_RE = re.compile(CREDENTIAL_REF_PATTERN)


def is_valid_credential_ref(value: object) -> bool:
    return isinstance(value, str) and _CREDENTIAL_REF_RE.fullmatch(value) is not None


@dataclass(frozen=True)
class ChildCredentialFile:
    """传递给受控沙箱子进程的只读无名文件描述符句柄；repr 排除路径细节。"""

    env_path: str = field(repr=False)
    read_fd: int
    child_mount: str | None = field(default=None, repr=False)
    _state: dict[str, bool] = field(default_factory=lambda: {"fd_closed": False}, repr=False, compare=False)

    def __repr__(self) -> str:
        return f"ChildCredentialFile(read_fd={self.read_fd})"

    def __reduce__(self):
        raise TypeError("ChildCredentialFile is non-serializable")

    @property
    def fd_closed(self) -> bool:
        return bool(self._state.get("fd_closed", False))

    def rewind(self) -> None:
        os.lseek(self.read_fd, 0, os.SEEK_SET)


def _uid_tag() -> str:
    getuid = getattr(os, "getuid", None)
    return str(getuid()) if callable(getuid) else "0"


def credential_staging_root() -> Path:
    """返回宿主凭据无名化中转父目录路径（供沙箱 hidden_paths 遮蔽，不主动持久化空目录）。"""
    return Path(tempfile.gettempdir()).resolve(strict=False) / f".my-agent-cred-stage-{_uid_tag()}"


def credential_scratch_parent() -> Path:
    """返回携卡子进程私有临时写区的父目录路径（供沙箱 private_read_roots 隔离同 UID 邻居）。"""
    return Path(tempfile.gettempdir()).resolve(strict=False) / f".my-agent-cred-scratch-{_uid_tag()}"


def _ensure_private_host_dir(path: Path) -> Path:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    dir_fd = os.open(path, os.O_RDONLY | nofollow)
    try:
        info = os.fstat(dir_fd)
        if not stat.S_ISDIR(info.st_mode):
            raise OSError(errno.ENOTDIR, "credential private root is not a directory")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise PermissionError("credential private root uid mismatch")
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.chmod(dir_fd, 0o700)
    finally:
        os.close(dir_fd)
    return path


def _try_prune_empty_dir(path: Path) -> None:
    with contextlib.suppress(OSError):
        os.rmdir(path)


def _force_rmtree(path: Path) -> bool:
    """回收携卡子进程私有临时目录（处理子进程可能创建的只读子文件），返回是否已确认不存在。"""
    def _on_err(func, target, _exc_info):
        with contextlib.suppress(OSError):
            os.chmod(target, 0o700)
            func(target)

    if path.exists() or path.is_symlink():
        with contextlib.suppress(OSError):
            os.chmod(path, 0o700)
        shutil.rmtree(path, onexc=_on_err)
    return not path.exists() and not path.is_symlink()


@contextmanager
def credential_scratch_dir() -> Iterator[tuple[Path, dict[str, bool]]]:
    """为单次携卡子进程分配独立 0700 临时写区，退出后立即递归删除并回收空父目录。"""
    parent = _ensure_private_host_dir(credential_scratch_parent())
    scratch = Path(tempfile.mkdtemp(prefix="run-", dir=os.fspath(parent))).resolve(strict=False)
    os.chmod(scratch, 0o700)
    state = {"scratch_cleaned": False}
    try:
        yield scratch, state
    finally:
        try:
            state["scratch_cleaned"] = _force_rmtree(scratch)
        finally:
            _try_prune_empty_dir(parent)


def _resolve_child_backend(sandbox_backend: str) -> tuple[str, str | None]:
    from ..tooling.sandbox import SandboxUnavailable

    normalized = str(sandbox_backend or "").strip().lower()
    if normalized in {"darwin", "macos", "seatbelt"}:
        return "darwin", None
    if normalized in {"linux", "bwrap"}:
        return "linux", LINUX_CHILD_CREDENTIAL_MOUNT
    raise SandboxUnavailable(f"SANDBOX_UNAVAILABLE: CREDENTIAL_FD_UNSUPPORTED_BACKEND:{normalized or 'unknown'}")


@contextmanager
def credential_file_for_child(
    material: Any,
    *,
    sandbox_backend: str,
) -> Iterator[ChildCredentialFile]:
    """先创建 0700/0600 排他文件、核验 inode 后立即 unlink 文件与空目录，再写入秘密并仅向子进程暴露只读 FD。"""
    from ..tooling.sandbox import SandboxUnavailable

    if os.name != "posix":
        raise SandboxUnavailable("SANDBOX_UNAVAILABLE: CREDENTIAL_FD_POSIX_REQUIRED")
    backend_kind, child_mount = _resolve_child_backend(sandbox_backend)
    raw_values = getattr(material, "values", None)
    if not isinstance(raw_values, Mapping):
        raise CredentialInputError(
            "CREDENTIAL_INPUT_UNAVAILABLE",
            "凭据材料缺少有效字段映射，未启动子进程。",
            stage="materialization",
            effect_outcome="not_started",
        )
    payload_bytes = json.dumps(
        {
            "credential_ref": str(getattr(material, "credential_ref", "") or ""),
            "origin": str(getattr(material, "origin", "") or ""),
            "target_url": str(getattr(material, "target_url", "") or ""),
            "values": {str(k): str(v) for k, v in raw_values.items()},
        },
        ensure_ascii=False,
    ).encode("utf-8")

    stage_root = _ensure_private_host_dir(credential_staging_root())
    stage_dir = Path(tempfile.mkdtemp(prefix="fd-", dir=os.fspath(stage_root)))
    os.chmod(stage_dir, 0o700)
    write_fd: int | None = None
    read_fd: int | None = None
    file_path: str | None = None

    fd_state = {"fd_closed": False}

    with ExitStack() as stack:
        def _cleanup_fds_and_dirs() -> None:
            nonlocal write_fd, read_fd, file_path
            if write_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(write_fd)
                write_fd = None
            if read_fd is not None:
                try:
                    os.close(read_fd)
                    fd_state["fd_closed"] = True
                except OSError as close_err:
                    fd_state["fd_closed"] = close_err.errno == errno.EBADF
                read_fd = None
            else:
                fd_state["fd_closed"] = True
            if file_path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(file_path)
                file_path = None
            _try_prune_empty_dir(stage_dir)
            _try_prune_empty_dir(stage_root)

        stack.callback(_cleanup_fds_and_dirs)
        write_fd, file_path = tempfile.mkstemp(prefix="cred-", dir=os.fspath(stage_dir))
        os.fchmod(write_fd, 0o600)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        cloexec = getattr(os, "O_CLOEXEC", 0)
        read_fd = os.open(file_path, os.O_RDONLY | nofollow | cloexec)
        os.set_inheritable(read_fd, False)

        write_stat = os.fstat(write_fd)
        read_stat = os.fstat(read_fd)
        if (
            not stat.S_ISREG(write_stat.st_mode)
            or not stat.S_ISREG(read_stat.st_mode)
            or (write_stat.st_dev, write_stat.st_ino) != (read_stat.st_dev, read_stat.st_ino)
            or stat.S_IMODE(read_stat.st_mode) != 0o600
            or (hasattr(os, "getuid") and read_stat.st_uid != os.getuid())
        ):
            raise SandboxUnavailable("SANDBOX_UNAVAILABLE: CREDENTIAL_FD_INODE_VERIFICATION_FAILED")

        # 关键顺序：先 unlink 命名文件并移除临时目录，确保写秘密前磁盘目录树已零命名残留。
        os.unlink(file_path)
        file_path = None
        _try_prune_empty_dir(stage_dir)
        _try_prune_empty_dir(stage_root)

        if os.fstat(read_fd).st_nlink != 0:
            raise SandboxUnavailable("SANDBOX_UNAVAILABLE: CREDENTIAL_FD_UNLINK_INCOMPLETE")

        view = memoryview(payload_bytes)
        written = 0
        while written < len(view):
            written += os.write(write_fd, view[written:])
        os.close(write_fd)
        write_fd = None
        os.lseek(read_fd, 0, os.SEEK_SET)

        env_path = f"/dev/fd/{read_fd}" if backend_kind == "darwin" else str(child_mount)
        yield ChildCredentialFile(env_path=env_path, read_fd=read_fd, child_mount=child_mount, _state=fd_state)


class HostCredentialUse:
    """挂在 ToolInvocationContext.credential_use 上的不可序列化宿主授权凭据访问器。"""

    def __init__(
        self,
        agent: Any,
        params: Any,
        *,
        turn_redaction: Any,
        redaction: Any,
        armed: bool = False,
    ) -> None:
        self._agent = agent
        self._params = params
        self._turn_redaction = turn_redaction
        self._redaction = redaction
        self._armed = bool(armed)

    def __repr__(self) -> str:
        return "HostCredentialUse()"

    def __reduce__(self):
        raise TypeError("HostCredentialUse is non-serializable")

    @property
    def redaction(self) -> Any:
        return self._redaction

    @property
    def turn_redaction(self) -> Any:
        return self._turn_redaction

    def begin_invocation(self, invocation_redaction: Any) -> HostCredentialUse:
        return HostCredentialUse(
            self._agent,
            self._params,
            turn_redaction=self._turn_redaction,
            redaction=invocation_redaction,
            armed=True,
        )

    def disarm(self) -> None:
        self._armed = False

    def register_values(self, values: Iterable[str]) -> Any:
        self._turn_redaction.register_values(values)
        self._redaction = self._redaction.with_credential_values(values)
        return self._redaction

    @contextmanager
    def _bound_run_params(self) -> Iterator[Any]:
        existing = getattr(self._agent, "_current_run_params", None)
        if existing is not None or self._params is None:
            yield existing if existing is not None else self._params
            return
        self._agent._current_run_params = self._params
        try:
            yield self._params
        finally:
            self._agent._current_run_params = None

    def session_scope(self) -> dict[str, object]:
        inactive = {
            "active": False,
            "owner_id": "",
            "owner_home": "",
            "channel": "",
            "actor_id": "",
            "thread_id": "",
            "root_task_id": "",
            "run_id": "",
            "attempt_id": "",
        }
        with self._bound_run_params() as current:
            if current is None:
                return inactive
            run_or_task = str(getattr(current, "run_id", "") or getattr(current, "task_id", "") or "").strip()
            if not run_or_task:
                return inactive
            try:
                scope = tool_approval_scope(self._agent, run_or_task)
                is_current = bool(scope.active and tool_approval_scope_is_current(self._agent, scope))
                channel, actor = approval_recipient(self._agent, scope) if scope.active else ("", "")
                owner = owner_identity_from_config(self._agent.config)
                home = getattr(self._agent, "home_paths", None)
            except (OSError, ValueError):
                return inactive
            return {
                "active": is_current,
                "owner_id": str(owner.owner_id or ""),
                "owner_home": str(getattr(home, "owner_home_dir", "") or ""),
                "channel": str(channel or ""),
                "actor_id": str(actor or ""),
                "thread_id": str(getattr(scope, "thread_id", "") or ""),
                "root_task_id": str(getattr(scope, "root_task_id", "") or ""),
                "run_id": str(getattr(scope, "run_id", "") or run_or_task),
                "attempt_id": str(getattr(current, "execution_attempt_id", "") or ""),
            }

    def verify_execution_current(
        self,
        *,
        credential_ref: str = "",
        expected_generation: int | None = None,
    ) -> None:
        """在重试启动或脚本真实执行前复核当前调用权与凭据有效状态。"""
        if not self._armed:
            raise CredentialInputError(
                "CREDENTIAL_NOT_AUTHORIZED",
                "当前凭据调用句柄未处于已批准工具执行期内，拒绝继续使用。",
                stage="authorization",
                effect_outcome="not_started",
            )
        with self._bound_run_params() as current:
            if current is None:
                raise CredentialInputError(
                    "CREDENTIAL_TASK_NOT_ACTIVE",
                    "当前没有活动任务上下文，已停止执行。",
                    stage="authorization",
                    effect_outcome="not_started",
                )
            scope = tool_approval_scope(self._agent, str(current.run_id or current.task_id))
            if not scope.active or not tool_approval_scope_is_current(self._agent, scope):
                raise CredentialInputError(
                    "CREDENTIAL_TASK_NOT_ACTIVE",
                    "当前执行 attempt 已失效或被取消，拒绝重试携卡子进程。",
                    stage="authorization",
                    effect_outcome="not_started",
                )
            if credential_ref:
                row = read_credential_row(
                    credential_path(self._agent.home_paths.owner_home_dir, credential_ref),
                    stage="credential_recheck",
                    effect_outcome="not_started",
                )
                if (
                    not row
                    or row.get("status") != "ready"
                    or row.get("verification_status") == "rejected"
                    or credential_ready_expired(row, now=time.time())
                    or (expected_generation is not None and int(row.get("generation", 1)) != int(expected_generation))
                ):
                    raise CredentialInputError(
                        "CREDENTIAL_INPUT_UNAVAILABLE",
                        "凭据已过期、已撤销或已换代，拒绝继续启动子进程。",
                        stage="credential_state",
                        effect_outcome="not_started",
                    )

    @contextmanager
    def __call__(
        self,
        reference: str,
        *,
        consumer: str,
        target_url: str | None = None,
        expected_generation: int | None = None,
        **extra: Any,
    ) -> Iterator[Any]:
        if not self._armed:
            raise CredentialInputError(
                "CREDENTIAL_NOT_AUTHORIZED",
                "凭据材料只能在宿主已完成授权的工具执行期内读取，未执行解密。",
                stage="authorization",
                effect_outcome="not_started",
            )
        if not is_valid_credential_ref(reference):
            raise CredentialInputError(
                "TOOL_INVALID_ARGUMENTS",
                "credential_ref 必须为形如 cred_<32位小写十六进制> 的宿主凭据引用。",
                stage="validation",
                effect_outcome="not_started",
            )
        from .task_credentials import authorized_credential_material

        unexpected = set(extra) - {"browser_consumer"}
        if unexpected:
            raise CredentialInputError(
                "TOOL_INVALID_ARGUMENTS",
                f"不支持的凭据授权参数：{sorted(unexpected)!r}。",
                stage="validation",
                effect_outcome="not_started",
            )
        with self._bound_run_params():
            with authorized_credential_material(
                self._agent,
                reference,
                consumer=consumer,
                target_url=target_url,
                expected_generation=expected_generation,
                browser_consumer=extra.get("browser_consumer"),
            ) as material:
                # 在任何工具副作用与首个明文出口之前完成回合级与调用级净化绑定。
                updated_redaction = self._turn_redaction.register(material)
                self._redaction = self._redaction.with_credential_values(
                    getattr(updated_redaction.snapshot.secret_values, "credential_values", ())
                )
                yield material

    open = __call__


def _canonical_failure_stage(stage: str, code: str, effect_outcome: str) -> str:
    lowered = str(stage or "").strip().lower()
    if lowered in {"protocol", "authorization", "validation", "runtime_gate", "execution", "effect_reconciliation", "persistence"}:
        return lowered
    if code == "TOOL_INVALID_ARGUMENTS":
        return "validation"
    if code in {"CREDENTIAL_NOT_AUTHORIZED", "CREDENTIAL_BINDING_INVALID", "CREDENTIAL_TASK_NOT_ACTIVE", "CREDENTIAL_CHANNEL_UNAVAILABLE"}:
        return "authorization"
    if effect_outcome != "not_started":
        return "execution"
    return "runtime_gate"


def classify_credential_exception(
    exc: BaseException,
    *,
    tool_name: str = "run_command",
    credential_ref: str = "",
    default_stage: str = "authorization",
    effect_outcome: str = "not_started",
) -> dict[str, str]:
    """将凭据/沙箱前置或执行期异常映射为 C6 统一结构（error_code, failure_stage, effect_outcome, next_action, message）。"""
    from ..tooling.sandbox import SandboxUnavailable

    ref_hint = credential_ref if is_valid_credential_ref(credential_ref) else "cred_<32位十六进制>"
    if isinstance(exc, SandboxUnavailable):
        return {
            "error_code": "SANDBOX_UNAVAILABLE",
            "failure_stage": "runtime_gate",
            "detail_stage": "sandbox_prepare",
            "effect_outcome": effect_outcome,
            "message": f"{tool_name} 无法为携卡执行建立受保护 OS 沙箱隔离（{type(exc).__name__}），未启动子进程。",
            "next_action": (
                f'改用已授权的 HTTP 入口 web_fetch(url="...", credential_ref="{ref_hint}") '
                "或受保护浏览器入口，或恢复当前系统沙箱后端后再重试。"
            ),
        }
    if isinstance(exc, CredentialInputError):
        code = str(exc.code or "CREDENTIAL_INPUT_UNAVAILABLE")
        eff = str(exc.effect_outcome or effect_outcome)
        raw_stage = str(exc.stage or default_stage)
        if code == "TOOL_INVALID_ARGUMENTS":
            next_action = (
                f'请修正参数后重试，例如 {tool_name}(command="python3 query.py", credential_ref="{ref_hint}")；'
                "不要传入空串、明文密码或后台/PTY标志。"
            )
        elif code in {"CREDENTIAL_NOT_AUTHORIZED", "CREDENTIAL_BINDING_INVALID", "CREDENTIAL_TASK_NOT_ACTIVE", "CREDENTIAL_CHANNEL_UNAVAILABLE"}:
            next_action = (
                '请回到原授权聊天/会话由本人发起调用，或先调用 request_credentials(operation="find", url="...") '
                "核实当前会话可用的凭据引用；禁止盲目原样重试。"
            )
        else:
            code = "CREDENTIAL_INPUT_UNAVAILABLE" if code not in {"TOOL_UNAVAILABLE"} else code
            next_action = (
                f'请先调用 request_credentials(operation="status", credential_ref="{ref_hint}") 核实状态，'
                "若已过期或撤销，请向本人重新发送凭据卡更新后再执行。"
            )
        return {
            "error_code": code,
            "failure_stage": _canonical_failure_stage(raw_stage, code, eff),
            "detail_stage": raw_stage,
            "effect_outcome": eff,
            "message": str(exc),
            "next_action": next_action,
        }

    typed_code = str(getattr(exc, "error_code", "") or getattr(exc, "code", "") or "").strip()
    if typed_code:
        raw_stage = str(getattr(exc, "stage", "") or default_stage)
        eff = str(getattr(exc, "effect_outcome", "") or effect_outcome)
        next_action = (
            '请回到原授权聊天/会话由本人发起调用，或先调用 request_credentials(operation="find", url="...") '
            "核实当前会话可用的凭据引用；禁止盲目原样重试。"
            if typed_code in {"CREDENTIAL_NOT_AUTHORIZED", "CREDENTIAL_BINDING_INVALID", "CREDENTIAL_TASK_NOT_ACTIVE"}
            else f'请先调用 request_credentials(operation="status", credential_ref="{ref_hint}") 查询状态并由本人在飞书凭据卡中更新后再试。'
        )
        return {
            "error_code": typed_code,
            "failure_stage": _canonical_failure_stage(raw_stage, typed_code, eff),
            "detail_stage": raw_stage,
            "effect_outcome": eff,
            "message": str(exc) or f"凭据不可用或未获授权（{typed_code}），未启动子进程。",
            "next_action": next_action,
        }

    fallback_code = "CREDENTIAL_NOT_AUTHORIZED" if default_stage == "authorization" and isinstance(exc, ValueError) else "CREDENTIAL_INPUT_UNAVAILABLE"
    return {
        "error_code": fallback_code,
        "failure_stage": _canonical_failure_stage(default_stage, fallback_code, effect_outcome),
        "detail_stage": default_stage,
        "effect_outcome": effect_outcome,
        "message": str(exc) or f"凭据准备失败（{type(exc).__name__}），未启动子进程。",
        "next_action": (
            f'请通过 request_credentials(operation="status", credential_ref="{ref_hint}") 核实凭据状态，不要原样盲目重试。'
        ),
    }


def build_credential_telemetry(
    *,
    request_id: str = "",
    call_id: str = "",
    credential_ref: str = "",
    effective_credential_ref: str = "",
    generation: int | None = None,
    access_scope: str = "",
    origin: str = "",
    redaction_sites: tuple[str, ...] = ("stream", "outcome", "log", "turn"),
    status_code: str = "OK",
    failure_stage: str = "",
    effect_outcome: str = "succeeded",
    fd_closed: bool = True,
    scratch_cleaned: bool = True,
    process_tree_reaped: bool = True,
) -> dict[str, object]:
    """构造不含任何秘密值、宿主机文件路径或请求正文的 C6 结构化审计回执。"""
    safe_origin = ""
    if origin:
        with contextlib.suppress(ValueError):
            safe_origin = credential_origin(origin)
    cleanup_confirmed = bool(fd_closed and scratch_cleaned and process_tree_reaped)
    receipt: dict[str, object] = {
        "credential_ref": credential_ref if is_valid_credential_ref(credential_ref) else "",
        "effective_credential_ref": (
            effective_credential_ref
            if is_valid_credential_ref(effective_credential_ref)
            else (credential_ref if is_valid_credential_ref(credential_ref) else "")
        ),
        "generation": int(generation) if isinstance(generation, int) and not isinstance(generation, bool) else 0,
        "access_scope": str(access_scope or ""),
        "origin": safe_origin,
        "redaction_sites": list(redaction_sites),
        "status_code": str(status_code or ""),
        "effect_outcome": str(effect_outcome or ""),
        "fd_closed": bool(fd_closed),
        "scratch_cleaned": bool(scratch_cleaned),
        "process_tree_reaped": bool(process_tree_reaped),
        "cleanup_confirmed": cleanup_confirmed,
    }
    if request_id:
        receipt["request_id"] = str(request_id)
    if call_id:
        receipt["call_id"] = str(call_id)
    if failure_stage:
        receipt["failure_stage"] = str(failure_stage)
    return receipt


__all__ = [
    "CREDENTIAL_FILE_ENV",
    "CREDENTIAL_REF_PATTERN",
    "LINUX_CHILD_CREDENTIAL_DIR",
    "LINUX_CHILD_CREDENTIAL_MOUNT",
    "ChildCredentialFile",
    "HostCredentialUse",
    "build_credential_telemetry",
    "classify_credential_exception",
    "credential_file_for_child",
    "credential_scratch_dir",
    "credential_scratch_parent",
    "credential_staging_root",
    "is_valid_credential_ref",
]
