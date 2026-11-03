# LLM: 执行权只来自当前真实callback与O_EXCL claim；磁盘排队正文不能构造执行器或授权。
# 模块用途: 按Owner/资产提供跨进程skip与有界FIFO，短事务门统一回收、转移和安全释放。
from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import stat
import sys
import threading
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import copy_context
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..common.log_redaction import redact_sensitive_text
from ..common.nofollow_fs import NoFollowPathError, open_directory_beneath
from ..gateway_parts.daemon_metadata import process_host_id
from ..gateway_parts.multiprocess_executor_identity import process_birth

try:
    import fcntl
except ImportError:  # Windows不伪装成具有POSIX进程互斥能力。
    fcntl = None

_LOG = logging.getLogger(__name__)
# 仅保存本进程确已结束回调的准确清理证明；规范工作流证明另附在原 run.json。
_PENDING_RELEASES: dict[tuple, _Lease] = {}


# LLM: 默认值属于对象配置，不增加模块级数值常量；路径只包含固定段和结构化作用域摘要。
# 类用途: 保存可信Owner根及本次资产作用域，不持有执行器或运行账本。
@dataclass(frozen=True)
class _Scope:
    root: Path
    parts: tuple[str, ...]
    read_limit: int = 1_048_576
    poll_seconds: float = 0.05


# LLM: release绑定本次创建的完整记录与inode，不凭相同run_id删除后来者。
# 类用途: 保存当前调用取得的claim凭据，仅供本进程释放。
@dataclass(frozen=True)
class _Lease:
    record: dict[str, Any]
    stamp: tuple[int, int]


# LLM: 坏队列是未知状态，不能当作空队列继续执行。
# 类用途: 表示持久并发元数据不符合当前协议。
class _StateError(RuntimeError):
    pass


# LLM: 目录安全能力不足时不退回Path.open或仅线程锁。
# 函数用途: 判断当前平台是否有本协议所需的严格POSIX原语。
def _supported() -> bool:
    return (
        fcntl is not None
        and bool(getattr(os, "O_NOFOLLOW", 0))
        and bool(getattr(os, "O_DIRECTORY", 0))
        and all(
            function in os.supports_dir_fd
            for function in (os.open, os.stat, os.mkdir, os.rename, os.unlink)
        )
    )


# LLM: 标识只进入结构化摘要或有界元数据，不直接拼成文件路径。
# 函数用途: 校验API标识并生成Owner内唯一的资产作用域。
def _scope(owner_home, kind, asset_id, run_id) -> _Scope:
    if kind not in ("workflow", "script"):
        raise ValueError("kind必须为workflow或script")
    for value in (asset_id, run_id):
        if type(value) is not str or not value or len(value) > 256:
            raise ValueError("资产与运行标识必须为非空有界字符串")
    raw = json.dumps([kind, asset_id], ensure_ascii=True, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return _Scope(
        Path(os.path.abspath(os.fspath(owner_home))),
        ("data", "workflow_concurrency", digest),
    )


# LLM: PID必须与同源内核birth和进程域共同使用；缺少任何一项不建立可回收claim。
# 函数用途: 捕获当前进程的可靠出生身份。
def _self_identity() -> dict[str, Any] | None:
    pid = os.getpid()
    birth = process_birth(pid)
    if sys.platform == "linux":
        valid_birth = type(birth) is int and birth >= 0
    elif sys.platform == "darwin":
        valid_birth = type(birth) is str and bool(birth)
    else:
        valid_birth = False
    if not valid_birth:
        return None
    host = process_host_id()
    if not host:
        return None
    return {
        "host_id": host,
        "source": f"kernel:{sys.platform}",
        "pid": pid,
        "birth": birth,
    }


# LLM: 只有同域ESRCH或同源birth已变化才能判死；EPERM、异域、不可观察全部保持未知。
# 函数用途: 返回进程身份存活、已死或未知，不发实际控制信号。
def _identity_live(identity) -> bool | None:
    if not isinstance(identity, dict):
        return None
    if (
        identity.get("host_id") != process_host_id()
        or identity.get("source") != f"kernel:{sys.platform}"
    ):
        return None
    pid, birth = identity.get("pid"), identity.get("birth")
    if type(pid) is not int or pid <= 0:
        return None
    if sys.platform == "linux":
        if type(birth) is not int or birth < 0:
            return None
    elif sys.platform == "darwin":
        if type(birth) is not str or not birth:
            return None
    else:
        return None
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return False if exc.errno == errno.ESRCH else None
    current = process_birth(pid)
    if current is None or type(current) is not type(birth):
        return None
    return current == birth


# LLM: 普通单链接是读写前提；inode是本次文件凭据，不以文件正文代替文件身份。
# 函数用途: 校验叶文件并取得设备与inode标识。
def _stamp(info) -> tuple[int, int]:
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise NoFollowPathError("并发元数据不是单链接普通文件")
    return info.st_dev, info.st_ino


# LLM: 父FD已由通用安全FS逐段打开；最终叶拒绝链接、FIFO及打开窗口中的身份替换。
# 函数用途: 相对固定父FD安全打开普通文件，由调用方关闭返回FD。
def _open_leaf(parent: int, name: str, flags: int, *, _with_stamp=False):
    try:
        before = _stamp(os.stat(name, dir_fd=parent, follow_symlinks=False))
    except FileNotFoundError:
        before = None
    descriptor = os.open(
        name,
        flags | os.O_NOFOLLOW | os.O_NONBLOCK,
        0o600,
        dir_fd=parent,
    )
    try:
        opened = _stamp(os.fstat(descriptor))
        current = _stamp(os.stat(name, dir_fd=parent, follow_symlinks=False))
        if opened != current or (before is not None and before != opened):
            raise NoFollowPathError("并发元数据在打开期间变化")
        return (descriptor, opened) if _with_stamp else descriptor
    except BaseException:
        os.close(descriptor)
        raise


# LLM: 永久gate不删除不替换；每次独立open的flock同时约束线程与本机进程。
# 函数用途: 为一个作用域取得短事务门，callback绝不在门内执行。
@contextmanager
def _gate(scope: _Scope):
    if fcntl is None:
        raise _StateError("严格跨进程事务门不可用")
    parent = open_directory_beneath(
        scope.root, scope.parts, create=True, require_dir_fd=True,
    )
    descriptor = None
    try:
        try:
            descriptor = _open_leaf(
                parent, "gate", os.O_RDWR | os.O_CREAT | os.O_EXCL,
            )
        except FileExistsError:
            descriptor = _open_leaf(parent, "gate", os.O_RDWR)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        opened = _stamp(os.fstat(descriptor))
        current = _stamp(os.stat("gate", dir_fd=parent, follow_symlinks=False))
        if opened != current:
            raise NoFollowPathError("并发事务门已被替换")
        yield parent
    finally:
        # 关闭本次独立open的FD即释放flock，不删除永久gate。
        try:
            if descriptor is not None:
                os.close(descriptor)
        finally:
            os.close(parent)


# LLM: 文件缺失与内容损坏分开；实际读取有字节上界，不凭st_size分配无界内存。
# 函数用途: 在事务门内读取一份普通元数据，返回原始字节及inode。
def _read(scope: _Scope, parent: int, name: str):
    try:
        descriptor = _open_leaf(parent, name, os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
        identity = _stamp(os.fstat(descriptor))
        chunks, count = [], 0
        while True:
            chunk = os.read(descriptor, min(65_536, scope.read_limit - count + 1))
            if not chunk:
                return b"".join(chunks), identity
            chunks.append(chunk)
            count += len(chunk)
            if count > scope.read_limit:
                raise _StateError("并发元数据超过读取上限")
    finally:
        os.close(descriptor)


# LLM: 仅在同一事务门内按本次inode删除；不删除同名新文件，也不跟随链接。
# 函数用途: 清理仍属于当前凭据的叶文件。
def _unlink_same(parent: int, name: str, expected: tuple[int, int]) -> bool:
    try:
        current = _stamp(os.stat(name, dir_fd=parent, follow_symlinks=False))
    except FileNotFoundError:
        return False
    if current != expected:
        return False
    os.unlink(name, dir_fd=parent)
    os.fsync(parent)
    return True


# LLM: 处理短写并在返回前刷文件；不把外层调用成功当成内部写入完成。
# 函数用途: 将完整字节写入当前新建FD。
def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("并发元数据发生短写")
        view = view[written:]
    os.fsync(descriptor)


# LLM: 队列先完整写私有临时文件再同目录替换；不覆盖链接或多链接叶。
# 函数用途: 在既有事务门及父FD内原子保存FIFO元数据。
def _write_queue(parent: int, entries: list[dict[str, Any]]) -> None:
    content = json.dumps(
        {"schema": "workflow-concurrency-queue.v1", "entries": entries},
        ensure_ascii=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    name = f".queue-{uuid.uuid4().hex}.tmp"
    descriptor, identity = _open_leaf(
        parent, name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _with_stamp=True,
    )
    try:
        _write_all(descriptor, content)
        try:
            _stamp(os.stat("queue.json", dir_fd=parent, follow_symlinks=False))
        except FileNotFoundError:
            pass
        os.replace(
            name, "queue.json", src_dir_fd=parent, dst_dir_fd=parent,
        )
        os.fsync(parent)
    finally:
        try:
            os.close(descriptor)
        finally:
            _unlink_same(parent, name, identity)


# LLM: FIFO由gate内实际追加顺序决定；坏记录不被当空队列或擅自恢复为可执行命令。
# 函数用途: 读取持久排队元数据并拒绝歧义结构。
def _queue(scope: _Scope, parent: int) -> list[dict[str, Any]]:
    found = _read(scope, parent, "queue.json")
    if found is None:
        return []
    try:
        value = json.loads(found[0])
    except (ValueError, UnicodeError) as exc:
        raise _StateError("并发队列内容损坏") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema") != "workflow-concurrency-queue.v1"
        or not isinstance(value.get("entries"), list)
    ):
        raise _StateError("并发队列结构损坏")
    entries = value["entries"]
    nonces = []
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or entry.get("schema") != "workflow-concurrency-ticket.v1"
            or type(entry.get("nonce")) is not str
            or not entry["nonce"]
            or type(entry.get("run_id")) is not str
        ):
            raise _StateError("并发队列项结构损坏")
        nonces.append(entry["nonce"])
    if len(nonces) != len(set(nonces)):
        raise _StateError("并发队列包含重复票据")
    return entries


# LLM: 只有证明原请求进程已死才移除排队项；未知项保留原位，不能超时越过。
# 函数用途: 在同一gate内清理已死排队者并保持存活者FIFO。
def _prune_queue(scope: _Scope, parent: int) -> list[dict[str, Any]]:
    entries = _queue(scope, parent)
    kept = [
        entry for entry in entries
        if _identity_live(entry.get("identity")) is not False
    ]
    if len(kept) != len(entries):
        _write_queue(parent, kept)
    return kept


# LLM: 半写或坏claim表示未知持有者，绝不以解析失败、年龄或PID单独回收。
# 函数用途: 读取claim；保留存在但未知的记录与其inode凭据。
def _claim(scope: _Scope, parent: int):
    found = _read(scope, parent, "claim.json")
    if found is None:
        return None
    try:
        value = json.loads(found[0])
    except (ValueError, UnicodeError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    return _Lease(value, found[1])


# LLM: 回收和新建共用gate；仅同源明确死亡可删，未知或坏schema保持占用。
# 函数用途: 返回当前持有者，并在可证明死亡时原位回收旧claim。
def _claim_key(scope: _Scope, lease: _Lease):
    return str(scope.root), scope.parts, lease.record.get('nonce'), lease.stamp


def _lifecycle_store(scope: _Scope, lease: _Lease):
    if lease.record.get('lifecycle') != 'workflow':
        return None
    from .run_store import WorkflowRunStore
    expected = _scope(scope.root, 'workflow', lease.record.get('asset_id'), lease.record.get('run_id'))
    if expected.parts != scope.parts:
        raise _StateError('工作流占位作用域不匹配')
    return WorkflowRunStore(scope.root)


def _holder(scope: _Scope, parent: int):
    """先核确切回调退出证明；委派义务不随原进程死亡而被回收。"""
    lease = _claim(scope, parent)
    if lease is None:
        return None
    key = _claim_key(scope, lease)
    store = _lifecycle_store(scope, lease)
    if _PENDING_RELEASES.get(key) == lease:
        if store is not None:
            phase = store._finish_concurrency_claim(lease)
            if phase != 'release_pending':
                _PENDING_RELEASES.pop(key, None)
                return lease
        _release_locked(scope, parent, lease)
        return _claim(scope, parent)
    if store is not None:
        from .run_store import _RunBusy
        try:
            phase = store._concurrency_claim_phase(lease)
        except _RunBusy:
            return lease
        if phase == 'release_pending':
            _release_locked(scope, parent, lease)
            return _claim(scope, parent)
        return lease
    if (lease.record.get('schema') == 'workflow-concurrency-claim.v1'
            and _identity_live(lease.record.get('identity')) is False):
        if not _unlink_same(parent, 'claim.json', lease.stamp):
            raise _StateError('死亡claim在回收期间变化')
        return None
    return lease


# LLM: O_EXCL始终是执行claim的取得条件；完整写失败只清自己刚建的inode。
# 函数用途: 为当前真实callback创建含PID及birth的独占claim。
def _create_claim(parent: int, ticket: dict[str, Any]) -> _Lease:
    record = {**ticket, "schema": "workflow-concurrency-claim.v1"}
    content = json.dumps(
        record, ensure_ascii=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    descriptor = _open_leaf(
        parent, "claim.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL,
    )
    identity = _stamp(os.fstat(descriptor))
    try:
        _write_all(descriptor, content)
        os.fsync(parent)
    except BaseException:
        _unlink_same(parent, "claim.json", identity)
        raise
    finally:
        os.close(descriptor)
    return _Lease(record, identity)


# LLM: 同run_id不等于同租约；nonce、完整记录、inode及本进程身份全部匹配才释放。
# 函数用途: 在原gate内安全释放自己的claim，不误删后来者。
def _release_locked(scope: _Scope, parent: int, lease: _Lease) -> None:
    """删除与确认分开；一旦 I/O 失败，完整原租约仍在内存和规范账本中。"""
    store = _lifecycle_store(scope, lease)
    if store is None:
        if lease.record.get('identity') != _self_identity():
            return
    elif store._concurrency_claim_phase(lease) != 'release_pending':
        return
    key = _claim_key(scope, lease)
    _PENDING_RELEASES[key] = lease
    current = _claim(scope, parent)
    if current is not None and current.stamp == lease.stamp and current.record == lease.record:
        _unlink_same(parent, 'claim.json', lease.stamp)
    if store is not None:
        store._ack_concurrency_release(lease)
    _PENDING_RELEASES.pop(key, None)


# LLM: 回调结束只证明本栈退出；原工作流义务须由规范账本确认，清理不能覆盖业务返回或原异常。
# 函数用途: 在门外执行一次真实回调，退出后保留或归还准确占位。
def _execute(scope: _Scope, lease: _Lease, callback: Callable[[], Any]):
    try:
        return callback()
    finally:
        key = _claim_key(scope, lease)
        _PENDING_RELEASES[key] = lease
        try:
            with _gate(scope) as parent:
                store = _lifecycle_store(scope, lease)
                if store is not None and store._finish_concurrency_claim(lease) != 'release_pending':
                    _PENDING_RELEASES.pop(key, None)
                else:
                    _release_locked(scope, parent, lease)
        except Exception as exc:
            _log_failure('claim_cleanup', exc)


def _reconcile_workflow_claim(owner_home, asset_id, run_id):
    """控制操作已退出运行锁后，核对同一原运行的保留证明；不触碰其它运行或执行中的回调。"""
    scope = _scope(owner_home, 'workflow', asset_id, run_id)
    if not _supported():
        return
    try:
        with _gate(scope) as parent:
            lease = _holder(scope, parent)
            if (lease is None or lease.record.get('run_id') != run_id
                    or lease.record.get('lifecycle') != 'workflow'):
                return
            store = _lifecycle_store(scope, lease)
            if (store._concurrency_claim_phase(lease) == 'retained'
                    and store._finish_concurrency_claim(lease) == 'release_pending'):
                _release_locked(scope, parent, lease)
    except Exception as exc:
        _log_failure('control_cleanup', exc)


# LLM: 磁盘记录只做FIFO匹配，只有worker内存中原始ticket及callback能够竞争自己的claim。
# 函数用途: 尝试让排队首项获得执行权，返回票据是否仍存在及本次claim。
def _take_ticket(scope: _Scope, ticket: dict[str, Any]):
    with _gate(scope) as parent:
        entries = _prune_queue(scope, parent)
        matches = [
            index for index, entry in enumerate(entries)
            if entry["nonce"] == ticket["nonce"]
        ]
        if not matches:
            return False, None
        index = matches[0]
        if entries[index] != ticket:
            raise _StateError("当前排队票据已发生身份变化")
        holder = _holder(scope, parent)
        if index != 0 or holder is not None:
            return True, None
        lease = _create_claim(parent, ticket)
        store = _lifecycle_store(scope, lease)
        if store is not None:
            store._bind_concurrency_claim(lease)
        try:
            _write_queue(parent, entries[1:])
        except BaseException:
            # 原子发表后的 ACK 错误不等于票据仍在；已删除时继续消费本次准确占位。
            if not any(entry['nonce'] == ticket['nonce'] for entry in _queue(scope, parent)):
                return True, lease
            if store is not None:
                store._finish_concurrency_claim(lease)
            _release_locked(scope, parent, lease)
            raise
        return True, lease


# LLM: 只删除当前内存ticket对应项；不清空队列、不推断其它请求的执行结果。
# 函数用途: 在已持gate时移除本次排队票据。
def _remove_ticket_locked(scope: _Scope, parent: int, ticket: dict[str, Any]) -> None:
    entries = _queue(scope, parent)
    kept = [entry for entry in entries if entry["nonce"] != ticket["nonce"]]
    if len(kept) != len(entries):
        _write_queue(parent, kept)


# LLM: 每个worker失败出口最多记录一次该阶段；异常文字先脱敏再截断，不打印完整traceback。
# 函数用途: 保留异步失败的结构化诊断，业务结果仍由原run账本承载。
def _log_failure(phase: str, exc: BaseException) -> None:
    _LOG.warning(
        "WORKFLOW_CONCURRENCY_WORKER_FAILED phase=%s error_type=%s error=%s",
        phase,
        exc.__class__.__name__,
        redact_sensitive_text(str(exc))[:512],
    )


# LLM: worker只执行原callback，不从文件恢复函数；Context由创建线程显式复制，不冒充线程局部run_params。
# 函数用途: 等待FIFO执行权，执行后或失败时清理本次排队票据。
def _queue_worker(scope: _Scope, ticket: dict[str, Any], callback: Callable[[], Any]) -> None:
    """等待失败不消费已受理票据；取得占位之后绝不重试业务回调。"""
    pause = threading.Event()
    reported = False
    while True:
        try:
            present, lease = _take_ticket(scope, ticket)
        except OSError as exc:
            if not reported:
                _log_failure('wait', exc)
                reported = True
            pause.wait(scope.poll_seconds)
            continue
        except BaseException as exc:
            # 无法解释的队列状态仍保留原票据，不能假装已经取消或完成。
            _log_failure('wait_unconfirmed', exc)
            return
        if not present:
            return
        if lease is not None:
            try:
                _execute(scope, lease, callback)
            except BaseException as exc:
                _log_failure('execute', exc)
            return
        pause.wait(scope.poll_seconds)


# LLM: allow保持旧调用语义；其它策略只管理执行准入，冻结定义、权限和运行终态仍由原宿主/Engine负责。
# 函数用途: 执行真实callback，或返回明确的skipped/queued并发策略回执。
def dispatch(
    owner_home,
    kind,
    asset_id,
    run_id,
    policy,
    callback,
    *,
    queue_limit=8,
    _lifecycle=None,
):
    if type(policy) is not str or policy not in ("allow", "skip_if_running", "queue"):
        return {"status": "skipped", "error_code": "INVALID_CONCURRENCY_POLICY"}
    if policy == "allow":
        return callback()
    if not callable(callback):
        raise TypeError("callback必须可调用")
    if type(queue_limit) is not int or queue_limit < 0:
        raise ValueError("queue_limit必须为非负整数")
    scope = _scope(owner_home, kind, asset_id, run_id)
    if not _supported():
        return {
            "status": "skipped",
            "error_code": "WORKFLOW_CONCURRENCY_UNSUPPORTED",
        }
    identity = _self_identity()
    if identity is None:
        return {
            "status": "skipped",
            "error_code": "WORKFLOW_CONCURRENCY_IDENTITY_UNAVAILABLE",
        }
    ticket = {
        "schema": "workflow-concurrency-ticket.v1",
        "nonce": uuid.uuid4().hex,
        "run_id": run_id,
        "identity": identity,
    }
    if _lifecycle is not None:
        if kind != 'workflow' or Path(_lifecycle.owner_home).absolute() != scope.root:
            raise ValueError('工作流生命周期作用域不匹配')
        ticket.update(lifecycle='workflow', asset_id=asset_id, policy=policy)
    context = copy_context()
    with _gate(scope) as parent:
        entries = _prune_queue(scope, parent)
        holder = _holder(scope, parent)
        if (holder is not None and _lifecycle is not None and holder.record.get('run_id') == run_id
                and holder.record.get('lifecycle') == 'workflow'
                and _lifecycle._concurrency_claim_phase(holder) == 'retained'):
            _lifecycle._bind_concurrency_claim(holder)
            lease = holder
        elif holder is None and not entries:
            lease = _create_claim(parent, ticket)
            if _lifecycle is not None:
                _lifecycle._bind_concurrency_claim(lease)
        elif policy == "skip_if_running":
            return {
                "status": "skipped",
                "error_code": "WORKFLOW_RUN_SKIPPED_ALREADY_RUNNING",
                "running_run_id": holder.record.get("run_id", "") if holder else "",
                **({"reason_code": "queued_predecessor"} if holder is None else {}),
            }
        else:
            if len(entries) >= queue_limit:
                return {
                    "status": "skipped",
                    "error_code": "WORKFLOW_RUN_QUEUE_FULL",
                }
            entries.append(ticket)
            position = len(entries)
            try:
                _write_queue(parent, entries)
                worker = threading.Thread(
                    target=context.run,
                    args=(_queue_worker, scope, ticket, callback),
                    name=f"workflow-queue-{ticket['nonce']}",
                    daemon=True,
                )
                worker.start()
            except BaseException:
                # 即使原子写在替换后报错，也读回并清理本次票据，不能留下活进程孤儿项。
                _remove_ticket_locked(scope, parent, ticket)
                raise
            return {
                "status": "queued",
                "queue_position": position,
                "run_id": run_id,
            }
    return _execute(scope, lease, callback)


__all__ = ["dispatch"]
