# LLM: 容器只拥有原记忆实例和内存借用责任；不缓存HTTP/source/actor/callback，不签写权，不做关闭flush；联验同owner构造、脏pending逐出和stop等待者。
# 模块用途: 为同一个真实C服务复用owner记忆资源，构造与业务串行等待都在Root门外。
import threading
from collections import OrderedDict
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass, field

from ..owner_scoped_pool import _resolved_max_agents


# LLM: 忙和关闭只描述内存资源可用性，不代表业务已执行或允许重放，不含owner路径或正文。
# 类用途: 让调用方明确拒绝本次借用，避免丢弃未落盘的原访问信号。
class CoordinatorMemoryResourcesUnavailable(RuntimeError):
    pass


# LLM: future仅持实际构造结果/原异常，operation锁只串行本实例；不保存构造callback或请求来源。
# 类用途: 跟踪一个owner的原实例、等待者与在途借用数。
@dataclass
class _OwnerResources:
    future: Future = field(default_factory=Future)
    operation: object = field(default_factory=threading.Lock)
    borrowers: int = 0


# LLM: holder出生零IO且绑定原control/runtime/config/paths；权限必须由每次活HTTP源重新核验，资源不是权限缓存。
# 类用途: 管理当前C服务的有界owner记忆实例引用和关闭准入。
class CoordinatorMemoryResources:
    # LLM: 只保存原引用和同步原语；数量解析共用原owner池，不能加载profile/目录/SQLite。
    # 函数用途: 创建尚无owner资源的内存容器。
    def __init__(self, control, runtime, paths):
        self.control, self.runtime, self.paths = control, runtime, paths
        self.config = control.config
        self._origin = self
        self._references = (control, runtime, paths, self.config)
        self._capacity = _resolved_max_agents(self.config.owner_agent_pool_max_agents)
        self._condition = threading.Condition()
        self._entries = OrderedDict()
        self._closing = False

    # LLM: 只核同一holder和原引用，不加载权限或替换资源；关闭立刻拒绝后继写。
    # 函数用途: 为本次HTTP来源确认容器仍开放。
    def require_open(self):
        with self._condition:
            if (
                self._origin is not self
                or self._closing
                or any(
                    a is not b
                    for a, b in zip(
                        (self.control, self.runtime, self.paths, self.config), self._references
                    )
                )
                or self.control.config is not self.config
            ):
                raise CoordinatorMemoryResourcesUnavailable(
                    "coordinator memory resources are closed"
                )

    # LLM: key只消费调用方已认证规范home；临时config副本不参与，每次actor/source永不写入容器。
    # 函数用途: 以完整owner及canonical目录定位同实例。
    def _key(self, home):
        return (
            id(self.control),
            id(self.config),
            home.owner_provider,
            home.owner_kind,
            home.owner_id,
            str(home.root),
            str(home.owner_home_dir),
        )

    # LLM: 只释放空闲且原pending为空的引用；不flush、不清pending或替别人完成访问义务。
    # 函数用途: 容量满时逐出一个已干净归还的最早实例。
    def _evict_idle(self):
        for key, entry in self._entries.items():
            if (
                entry.borrowers == 0
                and entry.future.done()
                and entry.future.exception() is None
                and not entry.future.result().long_term._pending_access
            ):
                del self._entries[key]
                return
        raise CoordinatorMemoryResourcesUnavailable("coordinator memory resources are busy")

    # LLM: 构造callback仅当前栈临时调用，绝不存入holder；失败不发布，closing唤醒等待者且晚构造不再可借。
    # 函数用途: 在缓存锁外完成唯一首次构造，再交给同owner的等待请求。
    def _construct(self, key, entry, factory):
        try:
            resources = factory()
        except BaseException as error:
            with self._condition:
                entry.future.set_exception(error)
                if self._entries.get(key) is entry:
                    del self._entries[key]
                self._condition.notify_all()
            raise
        with self._condition:
            entry.future.set_result(resources)
            if self._closing and self._entries.get(key) is entry:
                del self._entries[key]
            self._condition.notify_all()

    # LLM: 原condition内选中entry即预留本次borrow，覆盖构造发布、Future/operation等待和业务；同一finally恰好归还，所有等待仍在Root外。
    # 函数用途: 防止资源发布后尚未进入业务就被逐出，让同owner始终借用同实例并保留原pending义务。
    @contextmanager
    def borrow_current_owner(self, home, factory):
        key = self._key(home)
        with self._condition:
            self.require_open()
            entry = self._entries.get(key)
            construct = entry is None
            if construct:
                if len(self._entries) >= self._capacity:
                    self._evict_idle()
                entry = _OwnerResources()
                self._entries[key] = entry
            self._entries.move_to_end(key)
            entry.borrowers += 1
        acquired = False
        try:
            if construct:
                self._construct(key, entry, factory)
            with self._condition:
                while not entry.future.done() and not self._closing:
                    self._condition.wait()
                self.require_open()
                resources = entry.future.result()
            # operation锁定时用原condition作可关闭等待；不让stop后的等待者永久阻塞。
            while not acquired:
                with self._condition:
                    self.require_open()
                    acquired = entry.operation.acquire(blocking=False)
                    if not acquired:
                        self._condition.wait()
            self.require_open()
            yield resources
        finally:
            with self._condition:
                if acquired:
                    entry.operation.release()
                entry.borrowers -= 1
                if self._closing and entry.borrowers == 0 and self._entries.get(key) is entry:
                    del self._entries[key]
                self._condition.notify_all()

    # LLM: 只关闭准入/唤醒等待者/退空闲引用；不等模型、不flush、不清另一线程责任，pending不承诺进程退出持久恢复。
    # 函数用途: 在原HTTP shutdown之前使所有新借用和晚写立即失效。
    def close_admission(self):
        with self._condition:
            self._closing = True
            self._entries = OrderedDict(
                (key, entry)
                for key, entry in self._entries.items()
                if not entry.future.done() or entry.borrowers
            )
            self._condition.notify_all()
