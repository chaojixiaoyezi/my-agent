# LLM: 已登记普通账号只由本工厂冷读启用账号、规范home与有限宿主运行白名单装配；不克隆管理员凭据、完全访问、MCP/扩展或路径覆盖。
#   原普通池对已登记账号的拒绝保持不变；本池独立准入，模型由owner自己的选择在每片解析，W仍沿原冻结bootstrap/home/分区。
#   池按精确代次键缓存，租用在池锁内原子登记，容量经原普通池公开pin共享；构造与关闭都在池锁外，旧代次待最后租用释放后关闭。
# 模块用途: 为Count0/CountN受管派单与业务控制提供独立准入、有界租用的受限owner执行宿主。
from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from concurrent.futures import Future
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from ..owner_scoped_pool import (
    PoolCapacityExceededError,
    _close_agent_mcp_clients,
    _resolved_max_agents,
)
from ..plugin_host.errors import HostAPIError

# 有限宿主运行白名单：只含时限、预算与产品开关。凭据、访问模式/写根/私网、MCP/扩展、路径覆盖、
# 提示文件与模型投影一律不复制；模型由受管用户自己的选择（或管理员显式共享的初始模型）逐片解析。
_HOST_OPERATIONAL_FIELDS = (
    "business_spaces_enabled", "timezone", "week_start", "stream_enabled", "request_timeout",
    "provider_max_concurrent_requests", "max_tool_rounds", "max_parallel_tool_calls",
    "max_parallel_tool_calls_per_owner", "tool_http_timeout", "tool_shell_timeout", "tool_shell_output_max_chars",
    "tool_read_max_chars", "tool_output_preview_chars", "tool_output_externalize_min_chars",
    "input_media_max_bytes", "input_media_max_files", "input_image_max_pixels", "gateway_request_timeout",
    "gateway_processing_timeout_seconds", "gateway_request_max_attempts", "background_claim_ttl_seconds",
    "owner_agent_pool_max_agents", "owner_agent_idle_seconds", "background_threads_per_owner",
    "owner_maintenance_scan_interval_seconds", "memory_curator_enabled", "log_level",
)
_OWNER_FIELDS = ("my_agent_home", "my_agent_owner_provider", "my_agent_owner_kind", "my_agent_owner_id")
_MISSING = object()
_ACCESSOR_LOCK = threading.Lock()


# LLM: 与原普通池拒绝条件同一集合：local/user、非main、合法UID且已在账号库登记；冷读不创建账号或目录，坏账号库原样报错。
# 函数用途: 返回受管普通用户UID，非受管owner返回None。
def managed_local_user_id(home_root, owner) -> str | None:
    if (getattr(owner, "provider", None) != "local" or getattr(owner, "owner_kind", None) != "user"
            or getattr(owner, "owner_id", None) == "main"):
        return None
    from ..plugin_host.owner_context import validate_local_user_id
    from ..user_space.local_user_store import LocalUserStore

    try:
        uid = validate_local_user_id(owner.owner_id)
    except HostAPIError:
        return None
    return uid if LocalUserStore(home_root).get(uid) is not None else None


# LLM: 全新AgentConfig只接收白名单运行字段与owner三字段；不经dataclasses.replace继承管理员配置。
# 函数用途: 构造受管owner的受限运行配置。
def managed_owner_config(home_root, owner, host_options):
    from ..settings.config import AgentConfig

    values = {}
    for name in _HOST_OPERATIONAL_FIELDS:
        value = host_options.get(name, _MISSING) if isinstance(host_options, Mapping) else getattr(host_options, name, _MISSING)
        if value is not _MISSING:
            values[name] = copy.deepcopy(value)
    return AgentConfig(**values, my_agent_home=str(home_root), my_agent_owner_provider="local",
                       my_agent_owner_kind="user", my_agent_owner_id=owner.owner_id)


# LLM: 摘要只覆盖白名单与owner字段，用于精确代次键；不是授权凭据。
# 函数用途: 计算受限运行配置的稳定摘要。
def managed_config_digest(config) -> str:
    values = {name: getattr(config, name) for name in (*_HOST_OPERATIONAL_FIELDS, *_OWNER_FIELDS)}
    return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode("utf-8")).hexdigest()


# LLM: 独立准入：先签发原代次再构造全新受限Agent；构造后核owner/规范home/规范会话库及W原bootstrap，再复核代次，失败即关闭。
# 函数用途: 为一个已启用的受管普通用户构造受限owner执行Agent。
def prepare_managed_owner_runtime(home_root, owner, *, host_options, worker_bootstrap=None):
    from ..common.managed_owner_generation import (
        issue_managed_owner_generation,
        require_agent_managed_owner_generation,
    )
    from ..core import SimpleAgent
    from ..user_space.local_user_store import LocalUserStore

    uid = managed_local_user_id(home_root, owner)
    if uid is None:
        raise HostAPIError("TARGET_DENIED", 403)
    store = LocalUserStore(home_root)
    config = managed_owner_config(home_root, owner, host_options)
    generation = issue_managed_owner_generation(store.root, uid, config_digest=managed_config_digest(config))
    home = store.owner_home(uid)
    if worker_bootstrap is None:
        root, roots, options = home.home_dir, [home.home_dir], {}
    else:
        roots = list(worker_bootstrap.launch.workspace_roots)
        root, options = roots[0], {"worker_bootstrap": worker_bootstrap}
    agent = SimpleAgent(config, root, workspace_roots=roots, defer_mcp_connect=True, **options)
    try:
        _require_managed_agent(agent, home, owner, worker_bootstrap)
        agent._managed_owner_generation = generation
        require_agent_managed_owner_generation(agent, "managed_runtime_prepare")
    except BaseException:
        _close_agent_mcp_clients(agent)
        raise
    return agent


# LLM: 实际Agent必须落在同一受管owner规范home及唯一规范会话库；W另核原冻结home/分区与工作根。
# 函数用途: 拒绝归属或存储漂移的受管Agent。
def _require_managed_agent(agent, home, owner, worker_bootstrap):
    from ..user_space.managed_dispatch_recovery import canonical_conversation_root

    paths = agent.home_paths
    if ((paths.owner_provider, paths.owner_kind) != ("local", "user")
            or Path(paths.owner_home_dir).resolve() != Path(home.home_dir).resolve()
            or Path(agent.conversation_store.storage.root).resolve() != canonical_conversation_root(home).resolve()):
        raise HostAPIError("HOST_NOT_READY", 503)
    if worker_bootstrap is not None:
        from .multiprocess_agent_contract import require_agent_home
        from .multiprocess_worker_agent import require_worker_workspace

        require_agent_home(worker_bootstrap, owner, paths)
        require_worker_workspace(worker_bootstrap, agent.root, agent.workspace_roots)


# LLM: 共享容量凭证只在原普通池的实例预算内占一个名额；不含工具、MCP或凭据，最后释放时原池关闭为空操作。
# 类用途: 让受管实例与普通实例共用同一实例上限。
class _SharedSlot:
    __slots__ = ("__weakref__",)


# LLM: 条目只在池锁内修改；retired条目离开缓存但保留租用计数，直到最后释放才关闭。
# 类用途: 记录一个受管代次实例、共享容量凭证与租用。
@dataclass(eq=False)
class _Entry:
    key: tuple
    agent: object
    slot: ExitStack
    pins: int = 0
    retired: bool = False
    used_at: float = field(default_factory=time.monotonic)


# LLM: 有界进程内受管池：精确代次键、池锁内原子租用、与原普通池共享容量；构造/等待/关闭均在池锁外，旧代次不被复用。
# 类用途: 为受管普通用户提供独立准入的受限Agent租用。
class ManagedOwnerPool:
    # LLM: shared_pool为原普通池，只经其公开pin占用名额；W时home根必须是原冻结根。
    # 函数用途: 绑定账号根、宿主运行白名单来源、共享容量与原W bootstrap。
    def __init__(self, home_root, *, host_options, shared_pool=None, worker_bootstrap=None, max_agents=None):
        if worker_bootstrap is not None and Path(home_root) != worker_bootstrap.launch.owner_root:
            raise HostAPIError("HOST_NOT_READY", 503)
        self._root = Path(home_root)
        self._host_options = host_options
        self._shared = shared_pool
        self._bootstrap = worker_bootstrap
        if max_agents is None:
            max_agents = (host_options.get("owner_agent_pool_max_agents") if isinstance(host_options, Mapping)
                          else getattr(host_options, "owner_agent_pool_max_agents", None))
        self._max = _resolved_max_agents(max_agents)
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple, _Entry] = OrderedDict()
        self._by_agent: dict[int, _Entry] = {}
        self._building: dict[tuple, Future] = {}

    # LLM: 只判断归属类别，不构造实例；已登记但停用的账号仍归本池并在准入时拒绝。
    # 函数用途: 判断owner是否由受管池服务。
    def handles(self, owner) -> bool:
        return managed_local_user_id(self._root, owner) is not None

    # LLM: 租用在命中或发布的同一池锁内登记，退出必归还；归属复核失败也先归还再抛出。
    # 函数用途: 原子取得并租用准确当前代次的受管Agent。
    @contextmanager
    def acquire_pinned(self, owner, *, force_reload: bool = False, hard: bool = True):
        if force_reload:
            self._retire_user(managed_local_user_id(self._root, owner))
        agent = self._acquire(owner)
        try:
            yield agent
        finally:
            self._release(agent)

    # LLM: 兼容原池查询口；返回前已归还本次租用，执行方必须另经acquire_pinned或pin。
    # 函数用途: 取得受管Agent并保持缓存，不持有租用。
    def get(self, owner, *, hard: bool = True, touch: bool = True, force_reload: bool = False):
        with self.acquire_pinned(owner, force_reload=force_reload) as agent:
            return agent

    # LLM: 只接受本池仍登记的同一对象；已关闭或外来对象不能借pin复活。
    # 函数用途: 为已取得的受管Agent追加一次租用。
    @contextmanager
    def pin(self, agent, *, touch: bool = True):
        with self._lock:
            entry = self._by_agent.get(id(agent))
            if entry is None or entry.agent is not agent:
                raise HostAPIError("HOST_NOT_READY", 503)
            entry.pins += 1
        try:
            yield agent
        finally:
            self._release(agent)

    # LLM: 只读快照，不续期、不构造。
    # 函数用途: 返回当前缓存的受管Agent，供原后台调度器同步。
    def hard_agents(self) -> list:
        with self._lock:
            return [entry.agent for entry in self._entries.values()]

    # LLM: 只读计数，不代表持久任务完成。
    # 函数用途: 返回缓存、构造中与租用中的受管实例数。
    def gauges(self) -> dict[str, int]:
        with self._lock:
            return {"cached": len(self._entries), "building": len(self._building),
                    "pinned": sum(1 for entry in self._by_agent.values() if entry.pins)}

    # LLM: 只回收超过空闲期且无人租用的实例；busy由调用方原在途事实判断，关闭在池锁外。
    # 函数用途: 释放空闲受管实例及其共享容量。
    def release_idle(self, seconds: float, *, busy=None) -> list:
        cutoff = time.monotonic() - seconds
        with self._lock:
            candidates = [(key, entry) for key, entry in self._entries.items()
                          if not entry.pins and entry.used_at <= cutoff]
        released = []
        for key, entry in candidates:
            if callable(busy) and busy(entry.agent):
                continue
            with self._lock:
                if self._entries.get(key) is entry and not entry.pins:
                    self._retire_locked(key)
                    released.append(entry)
        self._close(released)
        return [entry.agent for entry in released]

    # LLM: 交接只经调用方原提交门写入；本方法不构造Agent。
    # 函数用途: 为一个受管owner执行有界旧派单交接。
    def reconcile(self, owner, *, commit_transition):
        from ..user_space.local_user_store import LocalUserStore
        from ..user_space.managed_dispatch_recovery import reconcile_managed_dispatches

        uid = managed_local_user_id(self._root, owner)
        if uid is None:
            raise HostAPIError("TARGET_DENIED", 403)
        return reconcile_managed_dispatches(LocalUserStore(self._root).owner_home(uid), commit_transition=commit_transition)

    # LLM: 每次先冷读当前代次键；命中即锁内租用，同owner其它代次退役；同键构造合流等待在锁外，失败对等待者同样失败。
    # 函数用途: 取得并租用当前代次的受管Agent。
    def _acquire(self, owner):
        from ..common.managed_owner_generation import current_managed_owner_key

        uid = managed_local_user_id(self._root, owner)
        if uid is None:
            raise HostAPIError("TARGET_DENIED", 403)
        self._require_worker_owner(owner)
        digest = managed_config_digest(managed_owner_config(self._root, owner, self._host_options))
        while True:
            try:
                key = current_managed_owner_key(self._root, uid, config_digest=digest)
            except HostAPIError:
                # 停用、缺失或无法证明当前权威时，已缓存代次一律退役，不再交给调度器或新租用。
                self._retire_user(uid)
                raise
            agent, future, creator = None, None, False
            with self._lock:
                ready = self._retire_other_generations_locked(key)
                entry = self._entries.get(key)
                if entry is not None:
                    entry.pins += 1
                    entry.used_at = time.monotonic()
                    self._entries.move_to_end(key)
                    agent = entry.agent
                else:
                    future = self._building.get(key)
                    if future is None:
                        room, evicted = self._make_room_locked()
                        ready.extend(evicted)
                        if room:
                            future = self._building[key] = Future()
                            creator = True
            self._close(ready)
            if agent is not None:
                try:
                    self._require_worker_owner(owner, agent)
                except BaseException:
                    self._release(agent)
                    raise
                return agent
            if future is None:
                raise HostAPIError("HOST_NOT_READY", 503, message="Managed owner pool capacity exhausted.")
            if creator:
                return self._build(key, owner, future)
            try:
                future.result()
            except HostAPIError:
                raise
            except Exception:
                raise HostAPIError("HOST_NOT_READY", 503) from None

    # LLM: 先在共享池占名额再构造；名额或构造失败释放已占名额并清预留，发布时若同键已存在则关闭多余实例。
    # 函数用途: 构造并发布一个受管代次实例，返回已租用的Agent。
    def _build(self, key, owner, future):
        slot = ExitStack()
        try:
            self._reserve_shared(slot)
            agent = prepare_managed_owner_runtime(self._root, owner, host_options=self._host_options,
                                                  worker_bootstrap=self._bootstrap)
        except BaseException as error:
            slot.close()
            with self._lock:
                if self._building.get(key) is future:
                    self._building.pop(key)
            future.set_exception(error)
            if isinstance(error, HostAPIError) or not isinstance(error, Exception):
                raise
            raise HostAPIError("HOST_NOT_READY", 503) from None
        actual = (key[0], *agent._managed_owner_generation.key[1:])
        surplus = None
        with self._lock:
            if self._building.get(key) is future:
                self._building.pop(key)
            ready = self._retire_other_generations_locked(actual)
            entry = self._entries.get(actual)
            if entry is not None:
                surplus = _Entry(actual, agent, slot)
            else:
                entry = self._entries[actual] = _Entry(actual, agent, slot)
                self._by_agent[id(agent)] = entry
            entry.pins += 1
            result = entry.agent
        future.set_result(result)
        self._close([*ready, *([surplus] if surplus is not None else [])])
        return result

    # LLM: 共享池满时先退役一个本池空闲实例再重试一次；仍满则作为未开始背压抛出。
    # 函数用途: 在原普通池实例预算内为新受管实例占一个名额。
    def _reserve_shared(self, slot):
        if self._shared is None:
            return
        for attempt in (0, 1):
            try:
                slot.enter_context(self._shared.pin(_SharedSlot()))
                return
            except PoolCapacityExceededError:
                with self._lock:
                    victim = next((key for key, entry in self._entries.items() if not entry.pins), None)
                    ready = [self._retire_locked(victim)] if victim is not None and attempt == 0 else []
                if not ready:
                    raise HostAPIError("HOST_NOT_READY", 503, message="Owner pool capacity exhausted.") from None
                self._close(ready)

    # LLM: 归还一次租用；退役且无人租用后在池锁外关闭。
    # 函数用途: 释放受管Agent租用。
    def _release(self, agent):
        with self._lock:
            entry = self._by_agent.get(id(agent))
            if entry is None or entry.agent is not agent:
                return
            entry.pins -= 1
            entry.used_at = time.monotonic()
            ready = []
            if entry.retired and not entry.pins:
                self._by_agent.pop(id(agent), None)
                ready.append(entry)
        self._close(ready)

    # LLM: 强制刷新或权威失效只退役当前缓存，不复用旧代次；仍有租用者待最后释放后关闭。
    # 函数用途: 退役一个受管UID的全部缓存代次。
    def _retire_user(self, uid):
        with self._lock:
            ready = [self._retire_locked(key) for key in tuple(self._entries) if key[0] == uid]
        self._close([entry for entry in ready if not entry.pins])

    # LLM: 调用方持池锁；同UID其它代次一律退役，仍有租用者延迟关闭。
    # 函数用途: 让新代次键取代同owner旧代次。
    def _retire_other_generations_locked(self, key):
        retired = [self._retire_locked(other) for other in tuple(self._entries) if other[0] == key[0] and other != key]
        return [entry for entry in retired if not entry.pins]

    # LLM: 调用方持池锁；本池上限含构造预留，只逐出无人租用的实例。
    # 函数用途: 为新构造腾出本池名额。
    def _make_room_locked(self):
        ready = []
        while len(self._entries) + len(self._building) >= self._max:
            victim = next((key for key, entry in self._entries.items() if not entry.pins), None)
            if victim is None:
                return False, ready
            ready.append(self._retire_locked(victim))
        return True, ready

    # LLM: 调用方持池锁；无人租用时同时移出身份索引，关闭留给锁外。
    # 函数用途: 将一个缓存条目标记为退役。
    def _retire_locked(self, key):
        entry = self._entries.pop(key)
        entry.retired = True
        if not entry.pins:
            self._by_agent.pop(id(entry.agent), None)
        return entry

    # LLM: 只在池锁外调用；先关闭实例MCP，再释放共享容量名额。
    # 函数用途: 关闭退役受管实例并归还共享名额。
    def _close(self, entries):
        for entry in entries:
            try:
                _close_agent_mcp_clients(entry.agent)
            finally:
                entry.slot.close()

    # LLM: W构造前核原冻结分区与构造身份，命中后核实际home及工作根；Count0无bootstrap时无额外I/O。
    # 函数用途: 阻止跨分区或已漂移的受管实例进入W执行。
    def _require_worker_owner(self, owner, agent=None):
        if self._bootstrap is None:
            return
        from .multiprocess_agent_contract import (
            AgentConstruction,
            require_agent_constructor,
            require_agent_home,
        )
        from .multiprocess_worker_agent import require_worker_workspace

        frozen = self._bootstrap
        require_agent_constructor(frozen, owner, AgentConstruction(frozen.launch.owner_root, owner))
        if agent is not None:
            require_agent_home(frozen, owner, agent.home_paths)
            require_worker_workspace(frozen, agent.root, agent.workspace_roots)


# LLM: 每个基础Gateway Agent一个受管池，容量与其原普通池共享；无原普通池的被动组合只受本池上限约束。
# 函数用途: 取得或创建Count0/业务控制使用的受管池。
def managed_owner_pool_for_agent(base_agent) -> ManagedOwnerPool:
    pool = getattr(base_agent, "_managed_owner_pool", None)
    if pool is not None:
        return pool
    from ..settings.model_scope import deployment_model_config

    with _ACCESSOR_LOCK:
        pool = getattr(base_agent, "_managed_owner_pool", None)
        if pool is None:
            shared = None
            if getattr(base_agent, "root", None) is not None:
                from .request_worker import _owner_pool

                shared = _owner_pool(base_agent)
            pool = ManagedOwnerPool(base_agent.home_paths.root, host_options=deployment_model_config(base_agent),
                                    shared_pool=shared)
            base_agent._managed_owner_pool = pool
        return pool
