
# LLM: 运行归档使用 owner_runs_dir 与结构化身份；归档不选择业务 cwd，不改写模型文件或交付合同的路径。
# 模块用途: 保存可恢复的运行记录并注入真实工作位置，业务整理交给共享目录指南。

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import stat
from contextlib import ExitStack, suppress
from dataclasses import dataclass, replace
from pathlib import Path

from ..common.json_io import append_private_jsonl_records
from ..common.nofollow_fs import (
    NoFollowPathError,
    _openat_directory,
    _openat_file,
    _reject_unsafe_existing_leaf,
    open_directory_beneath,
)
from ..conversation.authority import (
    CONVERSATION_TASK_TURN_ACTIVE_ATTR,
    CONVERSATION_TRANSIENT_WORKSPACE_ATTR,
    conversation_execution_cwd,
)
from ..user_space.home_indexes import RunIndexRef, TaskIndexRef, register_run_ref, register_task_ref
from ..user_space.run_workspace import (
    EnsureRunWorkspaceRequest,
    FinishRunWorkspaceRequest,
    _now_iso,
    activate_run_workspace,
    ensure_run_workspace,
    finish_run_workspace,
)
from ..user_space.task_title import concise_task_title
from ._runtime_params import ArchiveRunParams
from .runtime.owner_roots import runtime_owner_root

TOOL_OUTPUT_ARCHIVE_ROOT_ATTR = "runtime_tool_output_archive_root"
from .runtime.task_identity import durable_task_id


def current_run_task_workspace_root(agent, params: object | None = None) -> Path | None:
    for text in _task_workspace_root_candidates(agent, params):
        if not text:
            continue
        try:
            return Path(text).expanduser().resolve(strict=False)
        except OSError:
            continue
    return None


def current_run_artifact_registry_root(agent: object, params: object) -> Path:
    """交付物登记与读取共用同一执行工作区；没有运行工作区时使用规范 owner 根。"""
    task_root = current_run_task_workspace_root(agent, params)
    return task_root if task_root is not None else runtime_owner_root(agent)


def current_run_task_work_dir(agent, params: object | None = None) -> Path | None:
    for text in _task_workspace_work_dir_candidates(agent, params):
        if not text:
            continue
        try:
            return Path(text).expanduser().resolve(strict=False)
        except OSError:
            continue
    root = current_run_task_workspace_root(agent, params)
    return root / "work" if root is not None else None


# LLM: One run must never switch tool-output indexes after task promotion. Cache the first
# owner-scoped archive root in host task attributes and reject any forged/out-of-owner value.
# 函数用途: 固定本轮工具大输出的唯一归档根，避免中途创建任务目录后旧 ref 立即失效。
def current_run_tool_output_archive_root(agent, params: object | None = None) -> Path:
    owner_root = runtime_owner_root(agent).expanduser().resolve(strict=False)
    attrs = getattr(params, "task_attributes", None) if params is not None else None
    stored = (
        str(attrs.get(TOOL_OUTPUT_ARCHIVE_ROOT_ATTR) or "").strip()
        if isinstance(attrs, dict)
        else ""
    )
    if stored:
        candidate = Path(stored).expanduser().resolve(strict=False)
        if _path_is_within(candidate, owner_root):
            return candidate
    task_work = current_run_task_work_dir(agent, params)
    candidate = task_work if task_work is not None else owner_root
    candidate = candidate.expanduser().resolve(strict=False)
    if not _path_is_within(candidate, owner_root):
        candidate = owner_root
    if isinstance(attrs, dict):
        attrs[TOOL_OUTPUT_ARCHIVE_ROOT_ATTR] = str(candidate)
    return candidate


# LLM: Archive-root validation is structural and must not rely on string prefixes.
# 函数用途: 判断候选归档根是否确实位于当前用户家目录内。
def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


# LLM: Archive a conversation turn under its task only after a task-promoting tool activated that
# task in this turn; a 会话运行时 sticky cwd alone must not turn plain chat into task execution.
# 函数用途: 需要时保存本轮任务目录和索引；纯聊天即使继承工作目录也不会写成任务运行。
def write_run_task_workspace_if_needed(agent, params: ArchiveRunParams) -> str:
    if not bool(getattr(agent.config, "run_task_workspace_enabled", True)):
        return ""
    home_paths = getattr(agent, "home_paths", None)
    if home_paths is None:
        return ""
    attrs = getattr(params, "task_attributes", None)
    if (
        isinstance(attrs, dict)
        and str(getattr(params, "source", "") or "").strip().lower() == "gateway"
        and str(attrs.get("conversation_thread_id") or "").strip()
        and attrs.get(CONVERSATION_TASK_TURN_ACTIVE_ATTR) is not True
    ):
        return ""
    existing = _existing_workspace_paths(getattr(params, "task_attributes", None))
    if existing is None and _unpromoted_conversation_turn(params):
        return ""
    if existing is not None:
        existing.root.mkdir(parents=True, exist_ok=True)
        existing.output_dir.mkdir(parents=True, exist_ok=True)
        existing.work_dir.mkdir(parents=True, exist_ok=True)
        register_saved_run_task_ref(agent, _SavedWorkspaceRef(existing.root, existing.work_dir), _root_task_params(params))
        return str(existing.root)
    result = ensure_run_workspace(_run_workspace_request(agent, params, params.user_prompt))
    register_saved_run_task_ref(agent, result, params)
    return str(result.root)


# LLM: 顶层 run 返回时只用 AgentRunResult 的 typed runtime_status/reason 结束工作区；
# conversation task 继续以 conversation store 为唯一生命周期权威，子代理走自己的 state。
# 函数用途: 把 standalone CLI/本地 run 的当前工作区从 RUNNING 投影到真实终态并刷新索引。
def finish_run_task_workspace_if_needed(agent, params: object, result: object) -> str:
    if not bool(getattr(agent.config, "run_task_workspace_enabled", True)):
        return ""
    if str(getattr(params, "context_scope", "") or "").strip().lower() in {
        "task_local",
        "control_plane",
    }:
        return ""
    attrs = getattr(params, "task_attributes", None)
    attrs = attrs if isinstance(attrs, dict) else {}
    # A cli_run now has a ConversationStore transcript for Memory evidence, but it remains a
    # standalone one-shot task whose workspace must be projected out of RUNNING here.  Gateway
    # conversations keep their separate task-link lifecycle and continue to skip this projection.
    if str(getattr(params, "source", "") or "").strip().lower() != "cli_run" and any(
        str(attrs.get(key) or "").strip()
        for key in ("conversation_thread_id", "conversation_task_id")
    ):
        return ""
    existing = _existing_workspace_paths(attrs)
    if existing is None or not _workspace_is_owner_scoped(agent, existing.root):
        return ""
    finish_request = _run_workspace_finish_request(params, result, existing.root)
    if finish_request is None:
        return ""
    try:
        finished = finish_run_workspace(finish_request)
        if finished is None:
            return ""
        _register_finished_run_task_ref(agent, params, finished, finish_request)
        # SANDBOX-01(2026-08-15 真机): 沙箱 /tmp 映射在任务 work/.sandbox-tmp,
        # 收口时必须发布到任务交付目录, 否则模型视角完成、用户视角产物消失。
        _publish_sandbox_tmp_outputs(finished.root, finish_request.run_id)
        return str(finished.root)
    except (OSError, RuntimeError, ValueError):
        logging.getLogger(__name__).warning(
            "run workspace terminal projection failed(task=%s, run=%s)",
            finish_request.task_id,
            finish_request.run_id,
            exc_info=True,
        )
        return ""


# LLM: SANDBOX-01 发布函数——沙箱 /tmp 在命令结束后仍可写，宿主收口只经锚定的 no-follow 目录/文件描述符读写：
#   源与目标的目录链接、叶子链接、非普通文件和多链接源一律不发布；主位置真实不存在才看任务根旧位置，
#   主位置不安全时整体拒绝。计数与 timeline 只算真正原子发布成功的文件，不掩盖不伪造。
#   缺少 dir_fd/no-follow 能力的平台不降级到路径复制，直接不发布。
# 函数用途: 任务收口时把沙箱临时区产物发布到交付目录, 返回发布的相对路径列表。
def _publish_sandbox_tmp_outputs(root: Path, run_id: str) -> list[str]:
    try:
        with ExitStack() as descriptors:
            root_fd = open_directory_beneath(root, (), require_dir_fd=True)
            descriptors.callback(os.close, root_fd)
            located = _open_sandbox_tmp_source(root_fd)
            if located is None:
                return []
            source_fd, source_parts = located
            descriptors.callback(os.close, source_fd)
            publisher = _SandboxTmpPublisher(root_fd)
            descriptors.callback(publisher.close)
            published = publisher.publish_directory(source_fd, ())
        if published:
            append_private_jsonl_records(
                root / "work" / "timeline.jsonl",
                [
                    {
                        "created_at": _now_iso(),
                        "event_type": "sandbox_tmp_published",
                        "run_id": run_id,
                        "count": len(published),
                        "source": str(root.joinpath(*source_parts)),
                        "target": str(root / "output" / _SANDBOX_TMP_DIR),
                    }
                ],
            )
        return published
    except OSError:
        return []


_SANDBOX_TMP_DIR = ".sandbox-tmp"
# 发布沙箱产物时按 1 MB 分块复制：单次读写有界，大文件不整块读入内存。
_PUBLISH_CHUNK_BYTES = 1024 * 1024
_PRIVATE_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)


# LLM: 主位置 work/.sandbox-tmp 任一段真实不存在才回落任务根 .sandbox-tmp（working_dir 未指定时的沙箱 tmp 根）；
#   任一段是链接或非目录属于不安全，NoFollowPathError 交调用方整体拒绝，不回落。
# 函数用途: 按原两处布局打开沙箱临时区目录描述符，返回描述符与相对任务根的段。
def _open_sandbox_tmp_source(root_fd: int) -> tuple[int, tuple[str, ...]] | None:
    for parts in (("work", _SANDBOX_TMP_DIR), (_SANDBOX_TMP_DIR,)):
        try:
            return _open_directory_chain(root_fd, parts, create=False), parts
        except FileNotFoundError:
            continue
    return None


# 函数用途: 从已打开目录逐段 no-follow 打开子目录（可选创建缺失段），返回新描述符；失败时关闭中间描述符。
def _open_directory_chain(parent_fd: int, parts: tuple[str, ...], *, create: bool) -> int:
    current = os.dup(parent_fd)
    try:
        for part in parts:
            if create:
                with suppress(FileExistsError):
                    os.mkdir(part, dir_fd=current)
            child = _openat_directory(current, part)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


# LLM: 源只按已准入目录描述符逐层列名；目标 output/.sandbox-tmp 在第一份准入产物出现时才创建并缓存描述符。
# 类用途: 持有任务根与目标基目录描述符，深度优先发布沙箱临时区产物。
class _SandboxTmpPublisher:
    def __init__(self, root_fd: int) -> None:
        self._root_fd = root_fd
        self._base_fd = -1

    # 函数用途: 关闭本次发布打开的目标基目录描述符。
    def close(self) -> None:
        if self._base_fd >= 0:
            os.close(self._base_fd)
            self._base_fd = -1

    # LLM: 子目录必须是普通目录且打开前后同一 inode；链接、非普通、多链接条目一律跳过，单条失败不影响其余条目。
    # 函数用途: 发布一个源目录子树，返回真正发布成功的相对路径。
    def publish_directory(self, directory_fd: int, prefix: tuple[str, ...]) -> list[str]:
        published: list[str] = []
        for name in sorted(os.listdir(directory_fd)):
            parts = (*prefix, name)
            try:
                admitted = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if stat.S_ISDIR(admitted.st_mode):
                    child = _openat_directory(directory_fd, name)
                    try:
                        published.extend(self.publish_directory(child, parts))
                    finally:
                        os.close(child)
                elif stat.S_ISREG(admitted.st_mode) and admitted.st_nlink == 1:
                    self._publish_file(directory_fd, name, admitted, parts)
                    published.append(str(Path(*parts)))
            except OSError:
                continue
        return published

    # LLM: 源经 no-follow 打开后必须仍是发现时那个单链接普通文件；之后只从该描述符读取，不按路径重开。
    # 函数用途: 发布一个已准入的产物文件，任何拒绝或错误都关闭本次打开的描述符。
    def _publish_file(self, directory_fd: int, name: str, admitted: os.stat_result, parts: tuple[str, ...]) -> None:
        source_fd = _openat_file(directory_fd, name, os.O_RDONLY, 0o600)
        try:
            opened = os.fstat(source_fd)
            if (opened.st_dev, opened.st_ino) != (admitted.st_dev, admitted.st_ino):
                raise NoFollowPathError("sandbox output was replaced before publication")
            if self._base_fd < 0:
                self._base_fd = _open_directory_chain(self._root_fd, ("output", _SANDBOX_TMP_DIR), create=True)
            parent_fd = _open_directory_chain(self._base_fd, parts[:-1], create=True)
            try:
                _stream_publish(source_fd, opened, parent_fd, parts[-1])
            finally:
                os.close(parent_fd)
        finally:
            os.close(source_fd)


# LLM: 只读准入时的字节数，分块写入目标目录描述符下的私有临时文件；源在复制期间变了就不发布。
#   权限位与时间戳只经已打开描述符设置（去掉 setuid/setgid/sticky），目标叶子若是链接或非普通文件则拒绝，
#   最后在同一目录描述符内原子改名；失败只清理本次创建的临时文件。
# 函数用途: 把源描述符正文发布为目标目录中的同名文件。
def _stream_publish(source_fd: int, opened: os.stat_result, parent_fd: int, leaf: str) -> None:
    temporary = f".{leaf}.publish-{secrets.token_hex(8)}"
    target_fd = os.open(temporary, _PRIVATE_CREATE_FLAGS, 0o600, dir_fd=parent_fd)
    try:
        try:
            copied = _copy_admitted_bytes(source_fd, target_fd, opened.st_size)
            current = os.fstat(source_fd)
            if copied != opened.st_size or (current.st_size, current.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns):
                raise NoFollowPathError("sandbox output changed during publication")
            os.fchmod(target_fd, stat.S_IMODE(opened.st_mode) & 0o777)
            os.utime(target_fd, ns=(opened.st_atime_ns, opened.st_mtime_ns))
            os.fsync(target_fd)
        finally:
            os.close(target_fd)
        _reject_unsafe_existing_leaf(parent_fd, leaf)
        os.rename(temporary, leaf, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    except BaseException:
        with suppress(OSError):
            os.unlink(temporary, dir_fd=parent_fd)
        raise


# 函数用途: 以固定大小分块把源描述符的前 size 个字节写入目标描述符，返回实际复制的字节数。
def _copy_admitted_bytes(source_fd: int, target_fd: int, size: int) -> int:
    copied = 0
    while copied < size:
        chunk = os.read(source_fd, min(_PUBLISH_CHUNK_BYTES, size - copied))
        if not chunk:
            break
        view = memoryview(chunk)
        while view:
            written = os.write(target_fd, view)
            if written <= 0:
                raise OSError("short write while publishing sandbox output")
            view = view[written:]
        copied += len(chunk)
    return copied


# LLM: 一个 standalone 终态只能构造一份 typed request，写 canonical state 和刷新索引都复用它。
# 函数用途: 从本次运行参数和真实结果整理出工作区收尾请求；仍在等待时返回 None。
def _run_workspace_finish_request(
    params: object,
    result: object,
    root: Path,
) -> FinishRunWorkspaceRequest | None:
    status = _terminal_workspace_status(params, result)
    if not status:
        return None
    workspace_task_id = str(durable_task_id(params) or "").strip()
    task_id = str(
        workspace_task_id
        or getattr(params, "run_id", "")
        or getattr(params, "request_id", "")
    ).strip()
    return FinishRunWorkspaceRequest(
        root=root,
        request_id=str(getattr(params, "request_id", "") or "").strip(),
        run_id=str(
            getattr(params, "run_id", "")
            or getattr(params, "request_id", "")
            or task_id
        ).strip(),
        task_id=workspace_task_id,
        status=status,
        verification_status="UNVERIFIED",
        runtime_status=str(getattr(result, "runtime_status", "") or ""),
        runtime_reason=str(getattr(result, "runtime_reason", "") or ""),
        runtime_source=str(getattr(result, "runtime_source", "") or ""),
    )


# LLM: 仅将 typed runtime result 映射为 workspace 投影；wait/background 和显式 goal 非成功结果保持活跃。
# 函数用途: 决定standalone顶层的工作区投影；原用户输入/审批等待与其它可恢复状态同记BLOCKED，不误标失败。
def _terminal_workspace_status(params: object, result: object) -> str:
    runtime_status = str(getattr(result, "runtime_status", "ok") or "ok").strip().lower()
    runtime_reason = str(getattr(result, "runtime_reason", "") or "").strip().lower()
    attrs = getattr(params, "task_attributes", None)
    attrs = attrs if isinstance(attrs, dict) else {}
    if runtime_reason in {"background_dispatch"}:
        return ""
    if str(attrs.get("thread_goal_id") or "").strip() and runtime_status != "ok":
        return ""
    if runtime_status == "ok":
        return "DONE"
    if runtime_status == "cancelled":
        return "CANCELLED"
    if runtime_status in {"blocked", "context_overflow", "unfinished", "needs_user_input", "approval_required"}:
        return "BLOCKED"
    return "FAILED"


# LLM: 终态写入以 canonical owner home 为边界，不用 tasks/runs 名称授权；逐级拒绝符号链接。
# 函数用途: 确认运行归档属于当前用户；具体 run/task 身份仍由 finish_run_workspace 在锁内核对。
def _workspace_is_owner_scoped(agent: object, root: Path) -> bool:
    home_paths = getattr(agent, "home_paths", None)
    owner_home = getattr(home_paths, "owner_home_dir", None)
    if owner_home is None:
        return False
    try:
        owner = Path(owner_home).expanduser()
        unresolved_root = Path(root).expanduser()
        relative = unresolved_root.relative_to(owner)
        if not relative.parts or ".." in relative.parts:
            return False
        unresolved_root.resolve(strict=False).relative_to(owner.resolve(strict=False))
        current = owner
        for part in (*relative.parts, "work", "state.json"):
            if current.is_symlink():
                return False
            current = current / part
        return not current.is_symlink()
    except (OSError, RuntimeError, ValueError):
        return False


# LLM: task/run index 只是 canonical state 的检索投影；必须使用已经写成功的终态和工作区路径。
# 函数用途: 工作区收尾成功后刷新当前用户的任务索引和运行索引。
def _register_finished_run_task_ref(
    agent: object,
    params: object,
    result: object,
    request: FinishRunWorkspaceRequest,
) -> None:
    home_paths = getattr(agent, "home_paths", None)
    task_id = str(request.task_id or request.run_id or request.request_id or "").strip()
    run_id = str(request.run_id or request.request_id or task_id).strip()
    if home_paths is None or not task_id or not run_id:
        return
    index_status = str(request.status or "").lower()
    register_task_ref(
        home_paths,
        TaskIndexRef(
            owner_id=str(getattr(home_paths, "owner_id", "") or ""),
            task_id=task_id,
            task_path=result.root,
            status=index_status,
            title=_authoritative_task_title(agent, params, task_id),
        ),
    )
    register_run_ref(
        home_paths,
        RunIndexRef(
            owner_id=str(getattr(home_paths, "owner_id", "") or ""),
            run_id=run_id,
            task_id=task_id,
            run_path=result.work_dir,
            status=index_status,
        ),
    )


def register_saved_run_task_ref(agent, result, params) -> None:
    home_paths = getattr(agent, "home_paths", None)
    if home_paths is None:
        return
    task_id = params.task_id or params.run_id or params.run_request_id
    if not task_id:
        return
    try:
        register_task_ref(
            home_paths,
            TaskIndexRef(
                owner_id=str(getattr(home_paths, "owner_id", "") or ""),
                task_id=task_id,
                task_path=result.root,
                status="active",
                title=_authoritative_task_title(agent, params, task_id),
            ),
        )
        register_run_ref(
            home_paths,
            RunIndexRef(
                owner_id=str(getattr(home_paths, "owner_id", "") or ""),
                run_id=str(params.run_id or params.run_request_id or task_id),
                task_id=str(task_id),
                run_path=result.work_dir,
                status="active",
            ),
        )
        _sync_conversation_task_workspace(agent, params, task_id, result.root)
    except OSError:
        return


def _sync_conversation_task_workspace(agent, run_params, task_id: str, task_root: Path) -> None:
    store = getattr(agent, "conversation_store", None)
    if store is None or not callable(getattr(getattr(store, 'tasks', None), 'bind', None)):
        return
    attrs = getattr(run_params, "task_attributes", None)
    attrs = attrs if isinstance(attrs, dict) else {}
    # Ingress has already bound transient named-work identity, path and lifecycle.
    # Rebinding it from a generic run archive would reinterpret this turn's user
    # prompt as durable configuration.  An Audit prepare draft remains pending
    # until publish_audit_update commits it.
    if attrs.get(CONVERSATION_TRANSIENT_WORKSPACE_ATTR) is True:
        return
    # 同一 thread 只有一份历史；只有结构化任务晋升已经给出 task_id 时才建立
    # thread↔task/workspace 运行关系。
    if str(attrs.get("conversation_task_id") or "").strip() != str(task_id):
        return
    thread_id = str(attrs.get("conversation_thread_id") or "").strip()
    if not thread_id:
        try:
            thread = store.tasks.thread_for(str(task_id))
        except Exception:
            # 绑定线索失败只降级(任务工作区不依赖会话线索),但要留观测——
            # 静默丢元数据会让跨通道接续悄悄断链(体检实锤)。
            logging.getLogger(__name__).debug("thread_for_task lookup failed", exc_info=True)
            thread = None
        thread_id = str(getattr(thread, "thread_id", "") or "").strip()
    if not thread_id:
        return
    existing = _conversation_task_link(store, thread_id, str(task_id))
    goal = str(getattr(existing, "goal", "") or getattr(run_params, "user_prompt", "") or task_id)
    existing_path = str(getattr(existing, "task_path", "") or "").strip()
    task_path = existing_path if existing_path and Path(existing_path).exists() else str(task_root)
    status = str(getattr(existing, "status", "") or "active")
    if (
        existing is not None
        and goal == str(getattr(existing, "goal", "") or "")
        and task_path == existing_path
        and status == str(getattr(existing, "status", "") or "")
    ):
        return
    try:
        store.tasks.bind(
            {
                "thread_id": thread_id,
                "task_id": str(task_id),
                "goal": goal,
                "status": status,
                "task_path": task_path,
                "now": float(getattr(existing, "created_at", 0.0) or 0.0) or None,
            }
        )
    except OSError:
        return


def _conversation_task_link(store: object, thread_id: str, task_id: str):
    try:
        link, error = store.tasks.link_report(thread_id, task_id)
    except Exception:
        return None
    return link if error is None else None


# LLM: 标题优先显式结构化值和 goal，普通运行回退 root prompt，不能用 compact 续接提示覆盖原任务名。
# 函数用途: 为任务索引选择稳定、可读的标题。
def _authoritative_task_title(agent: object, params: object, task_id: str) -> str:
    attrs = getattr(params, "task_attributes", None)
    attrs = attrs if isinstance(attrs, dict) else {}
    for key in ("task_title", "task_name"):
        value = str(attrs.get(key) or "").strip()
        if value:
            return value[:160]
    store = getattr(agent, "conversation_store", None)
    thread_id = str(attrs.get("conversation_thread_id") or "").strip()
    if store is not None and thread_id:
        link = _conversation_task_link(store, thread_id, task_id)
        goal = str(getattr(link, "goal", "") or "").strip()
        if goal:
            return goal[:160]
    return str(
        getattr(params, "root_user_prompt", "")
        or getattr(params, "user_prompt", "")
        or task_id
    )[:160]


def attach_run_task_workspace_context(agent, params, user_prompt: str):
    if not _should_create_workspace(agent, params):
        return params
    result = _ensure_workspace_for_run(agent, params, user_prompt)
    primary_workspace_root = _primary_workspace_root(agent, params)
    injection = _workspace_prompt_section(
        primary_workspace_root=primary_workspace_root,
        # R7c 实锤(单次 run"请示退出"形态):cli_run 是一次性非交互运行,中途请示
        # 无人应答——这个环境事实必须告知模型;gateway 有 guidance 补发渠道、chat
        # 可多轮,不注入。
        single_shot=str(getattr(params, "source", "") or "").strip() == "cli_run",
        delivery_output_dir=(result.output_dir if getattr(params, "business_context", None) is not None else None),
    )
    next_inject = _append_once(list(getattr(params, "inject", None) or []), injection)
    next_attrs = _task_attributes_with_workspace(getattr(params, "task_attributes", None), result)
    next_contract = _delivery_contract_with_workspace(
        getattr(params, "delivery_contract", None),
        result,
        primary_workspace_root=primary_workspace_root,
    )
    agent._current_run_task_workspace = str(result.root)
    return replace(params, inject=next_inject, task_attributes=next_attrs, delivery_contract=next_contract)


def _should_create_workspace(agent, params) -> bool:
    if not bool(getattr(agent.config, "run_task_workspace_enabled", True)):
        return False
    if str(getattr(params, "context_scope", "") or "").strip().lower() in {"task_local", "control_plane"}:
        return False
    if _unpromoted_conversation_turn(params):
        # 普通聊天是 RequestRun，不因“可能以后会做事”预建 RUNNING task。真正调用
        # promotes_task 工具/create_subagents/task_progress 时由任务晋升点懒建。
        agent._current_run_task_workspace = ""
        return False
    return bool(getattr(agent, "home_paths", None) is not None)


# LLM: A thread id alone demotes interactive chat to a no-workspace turn, except cli_run whose
# one-shot task identity is already explicit and whose thread exists only for transcript evidence.
# 函数用途: 判断带会话身份的本轮是不是尚未真正工作的普通聊天；CLI 一次性任务不属于此类。
def _unpromoted_conversation_turn(params: object) -> bool:
    # cli_run is already an explicit standalone task.  Its ConversationStore thread exists only
    # to preserve the authoritative transcript and Memory evidence, not to demote the task into a
    # Gateway-style plain chat turn.
    if str(getattr(params, "source", "") or "").strip().lower() == "cli_run":
        return False
    attrs = getattr(params, "task_attributes", None)
    return bool(
        isinstance(attrs, dict)
        and str(attrs.get("conversation_thread_id") or "").strip()
        and not str(attrs.get("conversation_task_id") or "").strip()
    )


def materialize_promoted_task_workspace(agent: object, params: object, goal: str = "") -> Path | None:
    """TaskRun 晋升后的唯一懒建入口；同步 attrs、delivery contract 与当前工具边界。"""
    if getattr(agent, "home_paths", None) is None:
        return None
    result = _ensure_workspace_for_run(agent, params, goal or str(getattr(params, "root_user_prompt", "") or ""))
    attrs = getattr(params, "task_attributes", None)
    if not isinstance(attrs, dict):
        return None
    attrs.update(_task_attributes_with_workspace(attrs, result))
    contract = _delivery_contract_with_workspace(
        getattr(params, "delivery_contract", None),
        result,
        primary_workspace_root=_primary_workspace_root(agent, params),
    )
    if contract is not None:
        params.delivery_contract = contract
    agent._current_run_task_workspace = str(result.root)
    return result.root


def _ensure_workspace_for_run(agent, params, user_prompt: str):
    request = _run_workspace_request(agent, params, user_prompt)
    existing = _existing_workspace_paths(getattr(params, "task_attributes", None))
    if existing is not None:
        return activate_run_workspace(existing.root, request)
    return ensure_run_workspace(request)


# LLM: 新 run 归档写入 canonical owner_runs_dir；task_id 只决定不透明存储键，不从自然语言选择用户目录。
# 函数用途: 生成唯一宿主运行目录请求；tasks 整理模板仅用于模型提示，已有运行仍按原链接恢复。
def _run_workspace_request(agent, params, user_prompt: str) -> EnsureRunWorkspaceRequest:
    home_paths = agent.home_paths
    owner_home = getattr(home_paths, "owner_home_dir", None)
    target_home = Path(owner_home) if owner_home else Path(home_paths.root)
    task_id = durable_task_id(params)
    run_id = str(getattr(params, "run_id", "") or "")
    request_id = str(
        getattr(params, "request_id", "") or getattr(params, "run_request_id", "") or ""
    )
    identity = task_id or run_id or request_id
    if not identity:
        raise ValueError("run workspace requires an explicit runtime identity")
    runs_root = Path(getattr(home_paths, "owner_runs_dir", target_home / "runs"))
    relative_root = runs_root.resolve(strict=False).relative_to(target_home.resolve(strict=False))
    storage_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return EnsureRunWorkspaceRequest(
        home=target_home,
        template=str(relative_root / "{date}" / storage_key),
        task_name=concise_task_title(user_prompt),
        user_prompt=user_prompt,
        request_id=request_id,
        run_id=run_id,
        task_id=task_id,
        owner_id=str(getattr(home_paths, "owner_id", "") or ""),
        owner_home=str(owner_home or ""),
        source=str(getattr(params, "source", "") or "run"),
    )


@dataclass(frozen=True)
class _ExistingWorkspacePaths:
    root: Path
    output_dir: Path
    work_dir: Path


@dataclass(frozen=True)
class _SavedWorkspaceRef:
    root: Path
    work_dir: Path


def _root_task_params(params: ArchiveRunParams) -> ArchiveRunParams:
    root_task_id = _conversation_task_id(getattr(params, "task_attributes", None))
    if not root_task_id:
        return params
    return replace(params, task_id=root_task_id)


def _conversation_task_id(attrs: object) -> str:
    if not isinstance(attrs, dict):
        return ""
    return str(attrs.get("conversation_task_id") or "").strip()


def _existing_workspace_paths(attrs: object) -> _ExistingWorkspacePaths | None:
    if not isinstance(attrs, dict):
        return None
    workspace = attrs.get("run_workspace")
    if not isinstance(workspace, dict):
        return None
    root_text = str(workspace.get("task_root") or "").strip()
    if not root_text:
        return None
    root = Path(root_text)
    output_dir = Path(str(workspace.get("output_dir") or root / "output"))
    work_dir = Path(str(workspace.get("work_dir") or root / "work"))
    return _ExistingWorkspacePaths(root=root, output_dir=output_dir, work_dir=work_dir)


def _task_workspace_root_candidates(agent, params: object | None) -> list[str]:
    attrs = getattr(params, "task_attributes", None) if params is not None else None
    contract = getattr(params, "delivery_contract", None) if params is not None else None
    subagent_root = _subagent_run_workspace(agent, params)
    internal_root = _internal_agent_run_workspace(params, attrs)
    return [
        subagent_root,
        internal_root,
        _workspace_root_from_mapping(contract, "task_workspace"),
        _workspace_root_from_mapping(attrs, "run_workspace"),
        str(getattr(agent, "_current_run_task_workspace", "") or "").strip(),
    ]


def _task_workspace_work_dir_candidates(agent, params: object | None) -> list[str]:
    attrs = getattr(params, "task_attributes", None) if params is not None else None
    contract = getattr(params, "delivery_contract", None) if params is not None else None
    subagent_root = _subagent_run_workspace(agent, params)
    internal_root = _internal_agent_run_workspace(params, attrs)
    return [
        subagent_root,
        internal_root,
        _workspace_work_dir_from_mapping(contract, "task_workspace"),
        _workspace_work_dir_from_mapping(attrs, "run_workspace"),
        str(Path(getattr(agent, "_current_run_task_workspace", "") or "") / "work")
        if str(getattr(agent, "_current_run_task_workspace", "") or "").strip()
        else "",
    ]


def _internal_agent_run_workspace(params: object | None, attrs: object) -> str:
    if str(getattr(params, "context_scope", "") or "").strip().lower() not in {"task_local", "control_plane"}:
        return ""
    if not isinstance(attrs, dict):
        return ""
    return str(attrs.get("agent_run_workspace_dir") or "").strip()


def _subagent_run_workspace(agent, params: object | None) -> str:
    run_id = str(getattr(params, "run_id", "") or "").strip()
    if not run_id:
        return ""
    manager = getattr(agent, "subagents", None)
    if manager is None or not callable(getattr(manager, "load", None)):
        return ""
    try:
        task = manager.load(run_id)
    except Exception:
        return ""
    return str(getattr(task, "agent_run_workspace_dir", "") or "").strip()


def _workspace_root_from_mapping(value: object, key: str) -> str:
    if not isinstance(value, dict):
        return ""
    workspace = value.get(key)
    if not isinstance(workspace, dict):
        return ""
    root = str(workspace.get("task_root") or "").strip()
    if root:
        return root
    for field in ("output_dir", "work_dir"):
        text = str(workspace.get(field) or "").strip()
        if not text:
            continue
        try:
            path = Path(text).expanduser()
        except OSError:
            continue
        if field == "output_dir":
            return str(path.parent)
        if field == "work_dir":
            return str(path.parent)
    return ""


def _workspace_work_dir_from_mapping(value: object, key: str) -> str:
    if not isinstance(value, dict):
        return ""
    workspace = value.get(key)
    if not isinstance(workspace, dict):
        return ""
    work_dir = str(workspace.get("work_dir") or "").strip()
    if work_dir:
        return work_dir
    root = str(workspace.get("task_root") or "").strip()
    return str(Path(root) / "work") if root else ""


# LLM: CLI/TUI/IM 共用真实 cwd 事实；output/work 都是普通目录，内部运行记录不能变成产物强制落点。
# 函数用途: 告诉模型从哪里解析相对路径；单次 CLI 另外说明中途不会有用户应答，不扫描历史目录。
def _workspace_prompt_section(
    *,
    primary_workspace_root: Path,
    single_shot: bool = False,
    delivery_output_dir: Path | None = None,
) -> str:
    lines = [
        "# Current Task Workspace",
        f"- cwd: {primary_workspace_root}（普通相对路径从这里解析）",
        "- output/、work/、tasks/ 等名称都是 cwd 内的普通目录，不是宿主运行目录的别名。",
        "- 文件按家目录整理约定或用户明确要求放置；续作保留原文件位置，内部运行归档由宿主管理。",
    ]
    if delivery_output_dir is not None:
        # 业务任务已有宿主交付目录；告知位置但不把运行归档改成 cwd，也不把工作文件自动当作交付。
        lines.extend([
            f"- 本任务已声明交付目录: {delivery_output_dir}（不改变 cwd 或写入权限）",
            "- 新的最终交付文件放在该目录，并使用能返回结构化文件回执的文件工具写入。"
            "仅由 shell 写出的普通工作文件、或在答复中提到的路径，不会自动成为已登记交付物。",
        ])
    if single_shot:
        lines.append("- 单次运行：中途提问不会收到应答；采用合理假设推进，最终如实报告完成情况或阻碍。")
    return "\n".join(lines)


def _append_once(items: list[str], injection: str) -> list[str]:
    marker = "# Current Task Workspace"
    return items if any(marker in str(item) for item in items) else [*items, injection]


def _task_attributes_with_workspace(attrs: object, paths) -> dict:
    result = dict(attrs) if isinstance(attrs, dict) else {}
    result["run_workspace"] = {
        "task_root": str(paths.root),
        "output_dir": str(paths.output_dir),
        "work_dir": str(paths.work_dir),
    }
    return result


# LLM: 真实会话 cwd 优先，运行归档不参与选择；非会话入口沿已初始化的工具 cwd。
# 函数用途: 返回实际执行位置，保证提示的相对路径解释与工具一致。
def _primary_workspace_root(agent, params: object | None = None) -> Path:
    current = params if params is not None else getattr(agent, "_current_run_params", None)
    root = conversation_execution_cwd(getattr(current, "task_attributes", None))
    if not root:
        root = getattr(getattr(agent, "tools", None), "workspace_root", None) or getattr(
            agent,
            "root",
            ".",
        )
    return Path(root).expanduser().resolve(strict=False)


# LLM: 运行材料仅附加归档引用，不从 artifacts 反推用户意图，也不重写路径或新造 allowed_output_roots。
# 函数用途: 给既有交付合同附上宿主恢复位置；模型原有文件声明完整保留，权限独立校验。
def _delivery_contract_with_workspace(contract: object, paths, *, primary_workspace_root: Path | None = None):
    if not isinstance(contract, dict):
        return contract
    result = dict(contract)
    task_workspace = {
        "task_root": str(paths.root),
        "output_dir": str(paths.output_dir),
        "work_dir": str(paths.work_dir),
    }
    if primary_workspace_root is not None:
        task_workspace["source_workspace_root"] = str(primary_workspace_root)
        task_workspace["relative_input_root"] = str(primary_workspace_root)
    result["task_workspace"] = task_workspace
    return result


__all__ = [
    "attach_run_task_workspace_context",
    "current_run_task_work_dir",
    "current_run_tool_output_archive_root",
    "current_run_task_workspace_root",
    "materialize_promoted_task_workspace",
    "write_run_task_workspace_if_needed",
]
