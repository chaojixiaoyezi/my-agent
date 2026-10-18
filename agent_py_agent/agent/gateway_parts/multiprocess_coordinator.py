# LLM: 仅内部多进程 C 持有生命周期锁，默认单进程无任何新增 I/O；锁不继承给 worker/接班进程。
# 模块用途: 在恢复和派发前争抢协调者单例，撤权/关FD后才等仅只读的后台收尾。
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..common.executor_registration import bind_coordinator_requirement
from ..common.nofollow_fs import open_private_lock_beneath_tightened, split_existing_anchor

try:
    import fcntl
except ImportError:  # Windows 保持单进程，不能无锁启用内部模式。
    fcntl = None

# 仅在成功flock后收录，finally先移除再关FD；同根的线程可读取，不跨进程/重启/文件运输。
_HELD_COORDINATORS = {}


# LLM: 纯内存角色存在只禁止上下文丢失的裸工具执行；不检查FD或从JSON恢复权限，真正启动仍须原scope/T/P3/P5。
# 函数用途: 给共用工具接口提供C登记必需信号，默认count0无查询和I/O，fork副本也只拒绝绕行。
def coordinator_executor_required():
    return any(type(lease) is CoordinatorLease and type(lease.count) is int and lease.count > 0
               for lease in _HELD_COORDINATORS.values())


# LLM: 只有coordinator_guard创建并收录的对象有权威；对象字段不能替代注册身份和原FD。
# 类用途: 在进程内记录实际生命周期锁及一次冻结的代次。
class CoordinatorLease:
    # LLM: 根/分区数来自争锁组合根；保存原inode；control_plane只捕获本lease同一真实宿主，pin/Condition不签发owner或W权限。
    # 函数用途: 保存锁上下文与在途责任，不签发代次或读取用户状态。
    def __init__(self, root, count, descriptor):
        self.root, self.count, self.descriptor = root, count, descriptor
        self.pid = os.getpid()
        info = os.fstat(descriptor)
        self.signature = (info.st_dev, info.st_ino)
        self._generation = ""
        self.controller = None
        self.control_plane = None
        self._lifecycle_gate = threading.RLock()
        self._closing = False
        self._commit_pins = set()
        self._commit_changed = threading.Condition(self._lifecycle_gate)

    # LLM: 代次只能由bind_coordinator_generation第一次冻结，不用状态文件回填。
    # 函数用途: 返回当前锁生命周期的代次。
    @property
    def c_run_id(self):
        return self._generation

# LLM: 失败返回结构化错误，不恢复、不派发，也不拿 TTL 删除旧锁；内核锁才是单例权威。
# 类用途: 表示内部多进程协调者无法取得单例锁。
class CoordinatorUnavailable(RuntimeError):
    error_code = "COORDINATOR_ALREADY_RUNNING"


# LLM: count仅内部启动，flock后收录lease；finally短门内撤权/关FD，后台只读观察不占着flock收尾。
# 函数用途: 包裹完整C生命周期，默认模式不创建目录/锁/复核线程。
@contextmanager
def coordinator_guard(root: Path, count: int):
    if count == 0:
        yield
        return
    if type(count) is not int or count < 0 or fcntl is None:
        raise CoordinatorUnavailable("COORDINATOR_PLATFORM_OR_MODE_UNSUPPORTED")
    root = Path(os.path.abspath(root))
    anchor, missing = split_existing_anchor(root)
    descriptor = open_private_lock_beneath_tightened(anchor, (*missing, "coordinator.lock"))
    os.set_inheritable(descriptor, False)
    lease = None
    try:
        _acquire_coordinator_lock(descriptor)
        lease = CoordinatorLease(root, count, descriptor)
        _HELD_COORDINATORS[root] = lease
        yield lease
    finally:
        _close_coordinator_lease(root, lease, descriptor)


# LLM: 争锁失败不能撤外层C；真实FD先关闭，后台慢调用不挡替代C；异常也通知自己的后台。
# 函数用途: 先撤权/关锁，再收尾原C只读线程。
def _close_coordinator_lease(root, lease, descriptor):
    try:
        _withdraw_coordinator_lease(root, lease, descriptor)
    finally:
        if lease is not None and lease.controller is not None:
            lease.controller.close()


# LLM: 与最终副作用共门；closing拒新入，原flock保持到原pin逐项结清；门外收集本C真实内核域，轮询间隔不是完成/TTL凭据，同步原撤权/C域 tests。
# 函数用途: 等真实写与本地主体物理域责任结清再释放原flock，不在生命周期门内等待进程退出。
def _withdraw_coordinator_lease(root, lease, descriptor):
    if lease is None:
        os.close(descriptor)
        return
    with lease._commit_changed:
        lease._closing = True
        lease._commit_changed.notify_all()
    while True:
        if lease.controller is not None:
            from .multiprocess_takeover_runtime import collect_coordinator_executor_domains

            collect_coordinator_executor_domains(lease.controller)
        with lease._commit_changed:
            if not lease._commit_pins:
                break
            lease._commit_changed.wait(timeout=0.05)
    with lease._commit_changed:
        if _HELD_COORDINATORS.get(root) is lease:
            _HELD_COORDINATORS.pop(root)
        os.close(descriptor)


# LLM: 非阻塞 flock 防止第二个 C 等锁时偷偷恢复；不从 pid/心跳推断锁已经空闲。
# 函数用途: 取得内核独占锁，竞争失败抛出结构化错误。
def _acquire_coordinator_lock(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise CoordinatorUnavailable("协调者已在运行") from exc


# LLM: 注册对象、PID、原FD和根/分区数须一致；closing不可新授权，pin结清另核原登记。
# 函数用途: 只读返回当前进程未撤权的实际持锁C；默认模式不做I/O。
def current_coordinator(root: Path, count: int):
    if type(count) is not int or count <= 0:
        return None
    lease = _HELD_COORDINATORS.get(Path(os.path.abspath(root)))
    if not _is_registered_coordinator_lease(lease) or lease.count != count or lease._closing:
        return None
    return lease


# LLM: 只冻结成功争锁的本进程启动值；文件或请求不能换代，当前锁生命周期也不能静默重绑。
# 函数用途: 给当前持锁C绑定一次非空启动代次；缺锁/错代立即拒绝。
def bind_coordinator_generation(root: Path, count: int, c_run_id: str):
    lease = current_coordinator(root, count)
    if lease is None or type(c_run_id) is not str or not c_run_id.strip():
        raise CoordinatorUnavailable("COORDINATOR_CONTEXT_REQUIRED")
    if lease.c_run_id and lease.c_run_id != c_run_id:
        raise CoordinatorUnavailable("COORDINATOR_GENERATION_CONFLICT")
    lease._generation = c_run_id
    return lease


# LLM: 只读原登记/原FD；context中的原lease只增加同对象限制，闭合或复制的引用不能复活许可。
# 函数用途: 为C控制入口取得实际持锁对象，旧调用不能跟随替代C。
def coordinator_for_root(root: Path, *, context=None):
    lease = _HELD_COORDINATORS.get(Path(os.path.abspath(root)))
    current = current_coordinator(root, lease.count) if lease is not None else None
    original = getattr(context, '_coordinator_role_lease', None)
    return current if original is None or original is current else None


# LLM: 原登记或可信组合根留下的原lease引用只用于拒回退；摘登记/关FD/复制均不能把角色标记变成许可。
# 函数用途: 已进入的C调用在撤权后仍保留限制性角色，绝不据此允许新提交。
def coordinator_role_present(root: Path, *, context=None) -> bool:
    root = Path(os.path.abspath(root))
    original = getattr(context, '_coordinator_role_lease', None)
    if type(original) is CoordinatorLease and original.root == root:
        return True
    return _is_registered_coordinator_lease(_HELD_COORDINATORS.get(root))


# LLM: 仅原C进程内责任标记；身份用对象同一性，不可序列化作为W权限或完成凭据。
# 类用途: 保存不可变责任标签，不创建独立授权账。
@dataclass(frozen=True, eq=False)
class _CoordinatorCommitPin:
    scope_key: tuple[str, ...]


# LLM: 原登记对象/PID/FD仍须真实一致；closing期间只供原pin结清，不意味着可新提交。
# 函数用途: 核对真实C租约，不读取JSON代次或用户许可。
def _is_registered_coordinator_lease(lease):
    if not isinstance(lease, CoordinatorLease) or lease.pid != os.getpid():
        return False
    if _HELD_COORDINATORS.get(lease.root) is not lease:
        return False
    try:
        info = os.fstat(lease.descriptor)
    except OSError:
        return False
    return (info.st_dev, info.st_ino) == lease.signature


# LLM: 只记录责任，调用方须在原controller/owner/出生核验短门内先获许可；标签不授权。不得长持gate。
# 函数用途: 给当前原lease增加一项精确在途责任，正常撤权须等它结清。
def pin_coordinator_commit(lease, scope_key):
    if not _is_registered_coordinator_lease(lease):
        raise CoordinatorUnavailable("COORDINATOR_CONTEXT_REQUIRED")
    if type(scope_key) is not tuple or not scope_key or not all(
        type(item) is str and item.strip() for item in scope_key
    ):
        raise CoordinatorUnavailable("COORDINATOR_COMMIT_SCOPE_INVALID")
    with lease._lifecycle_gate:
        if not _is_registered_coordinator_lease(lease) or lease._closing:
            raise CoordinatorUnavailable("COORDINATOR_CONTEXT_REQUIRED")
        pin = _CoordinatorCommitPin(scope_key)
        lease._commit_pins.add(pin)
        return pin


# LLM: 仅当前原C中原登记对象可结清；closing允许结束既有责任，不按EOF/timeout删除或复活许可。
# 函数用途: 精确移除一项责任并唤醒撤权等待者；复制、重放和错C均拒绝。
def retire_coordinator_commit(lease, pin):
    if not _is_registered_coordinator_lease(lease):
        raise CoordinatorUnavailable("COORDINATOR_CONTEXT_REQUIRED")
    with lease._lifecycle_gate:
        if not _is_registered_coordinator_lease(lease):
            raise CoordinatorUnavailable("COORDINATOR_CONTEXT_REQUIRED")
        if type(pin) is not _CoordinatorCommitPin or pin not in lease._commit_pins:
            raise CoordinatorUnavailable("COORDINATOR_COMMIT_PIN_INVALID")
        lease._commit_pins.remove(pin)
        lease._commit_changed.notify_all()


bind_coordinator_requirement(coordinator_executor_required)
