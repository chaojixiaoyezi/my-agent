# LLM: owner 池只管理进程内实例，唯一身份及持久工作沿既有 owner/thread 合同；空闲回收不删除用户资料。
# W仅运输原bootstrap并在构造/命中前后核分区及实际home，不复制池/配置/执行权；普通主线LRU、Future和凭据来源保持。
# 模块用途: 按用户隔离资源、合并首次构建，并向 Gateway 提供带在途保护的空闲回收入口。
"""按 owner 隔离的 agent 池(多用户飞书 per-用户隔离 + 审计 #2/#13)。

本池按 OwnerIdentity 复用用户作用域实例；home、记忆和 local_store 继续按 owner 隔离。
HTTP、请求 worker 与后台调度共用同一池；只读状态使用 peek，不构建实例或续空闲期。
构建在锁外完成，同一身份的并发首次访问等待同一个 Future；持久任务不属于缓存生命周期。
"""

from __future__ import annotations

import dataclasses
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future
from contextlib import contextmanager
from functools import partial
from typing import Any

from .delivery import RuntimeHealthProvider

# 单 owner 默认最多并行的代理数；防止一个 owner 占满全部线程资源。
_DEFAULT_MAX_AGENT_COUNT = 64


# LLM: 仅由池退役路径在池锁外调用；异常软失败，只关闭该Agent的MCP客户端，不触碰用户后台进程会话。
# 函数用途: 安全释放离池实例的MCP资源，清理失败不影响原缓存和业务结果。
def _close_agent_mcp_clients(agent: Any) -> None:
    try:
        close_mcp = getattr(getattr(agent, "tools", None), "close_mcp_clients", None)
        if callable(close_mcp):
            close_mcp()
    except Exception:
        pass


# LLM: 仅在get与pin竞态使对象在进入pin前已被离池关闭时于池锁外调用；恢复其本次租用期内的MCP可用性。
# 函数用途: 清除已淘汰实例在进入租用前被提前置位的MCP关闭标记，最后一次pin释放时仍会再次退役关闭。
def _reopen_agent_mcp_clients(agent: Any) -> None:
    try:
        tools = getattr(agent, "tools", None)
        if tools is None:
            return
        reopen_mcp = getattr(tools, "reopen_mcp_clients", None)
        if callable(reopen_mcp):
            reopen_mcp()
            return
        closed_event = getattr(tools, "_mcp_closed", None)
        clear = getattr(closed_event, "clear", None)
        if callable(clear):
            lock = getattr(tools, "_mcp_prepare_lock", None)
            if lock is not None:
                with lock:
                    clear()
            else:
                clear()
    except Exception:
        pass


def _resolved_max_agents(value: Any) -> int:
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        resolved = _DEFAULT_MAX_AGENT_COUNT
    return max(1, resolved if resolved > 0 else _DEFAULT_MAX_AGENT_COUNT)


# LLM: owner 身份只由宿主池提供；克隆前保留凭据来源的擦洗上界，不从用户属性构造配置权威。
# 函数用途: 为用户实例覆盖 owner 三字段，同时保留宿主环境来源的内部安全上界。
def _config_with_owner(base_config: Any, owner: Any) -> Any:
    """克隆基础 config,把 owner 三字段覆盖成该用户(其余配置原样继承)。"""
    from .settings.secret_ref import freeze_managed_secret_environment

    return dataclasses.replace(
        freeze_managed_secret_environment(base_config),
        my_agent_owner_provider=owner.provider,
        my_agent_owner_kind=owner.owner_kind,
        my_agent_owner_id=owner.owner_id,
    )


# LLM: owner只由原池提供；W运输原bootstrap到最早构造门，构造后核真实home，保main secret克隆与默认自动称呼语义。
# 函数用途: 为原owner构造唯一Agent，W不能用热配置改变冻结根或返回另一owner实例。
def build_owner_scoped_agent(
    base_config: Any,
    root: Any,
    owner: Any,
    workspace_roots: Any,
    *,
    channel_runtime_health_provider: RuntimeHealthProvider | None = None,
    worker_bootstrap=None,
) -> Any:
    """按 owner 建一个作用域 SimpleAgent(独立 home/记忆/local_store)。延迟导入防循环。"""
    from .core import SimpleAgent

    worker_options = {} if worker_bootstrap is None else {"worker_bootstrap": worker_bootstrap}
    agent = SimpleAgent(
        _config_with_owner(base_config, owner),
        root,
        workspace_roots=workspace_roots,
        channel_runtime_health_provider=channel_runtime_health_provider,
        **worker_options,
    )
    if worker_bootstrap is not None:
        from .gateway_parts.multiprocess_agent_contract import require_agent_home

        require_agent_home(worker_bootstrap, owner, agent.home_paths)
    _maybe_seed_feishu_call_name(base_config, owner, agent)  # 飞书首聊自动称呼,best-effort 永不抛
    # J17：作用域 agent 的模型调用账本在构造时已登记进进程准入表，Gateway 停机时一并关门结清（弱引用，逐出回收后自动消失）。
    return agent


def _maybe_seed_feishu_call_name(base_config: Any, owner: Any, agent: Any) -> None:
    """飞书用户首次建作用域 agent 时,用其飞书姓名填 USER.md 的"称呼"(空才填,全程 fail-open)。
    无通讯录权限/网络失败→称呼留空由 agent 自然询问;绝不因此影响 agent 创建。"""
    try:
        if getattr(owner, "provider", "") != "feishu":
            return
        user_md = getattr(getattr(agent, "home_paths", None), "owner_user_md", None)
        from .adapter.feishu_profile import (
            call_name_is_empty,
            fetch_feishu_display_name,
            seed_call_name,
        )

        if not user_md or not call_name_is_empty(user_md):
            return  # 文件不在 / 称呼已填 → 跳过
        from .settings.secret_ref import resolve_secret_ref

        app_id = resolve_secret_ref(getattr(base_config, "feishu_app_id", "") or "")
        app_secret = resolve_secret_ref(getattr(base_config, "feishu_app_secret", "") or "")
        name = fetch_feishu_display_name(app_id, app_secret, str(getattr(owner, "owner_id", "")))
        if name:
            seed_call_name(
                user_md,
                name,
                repository=getattr(agent, "persona_repository", None),
            )
    except Exception:
        pass  # 绝不因 auto-name 失败影响 agent 创建


# LLM: 同owner合流、缓存/租用/持久任务寿命分离；W每次消费核原bootstrap与实际home而不从热配置换属，默认池不新增检查I/O。
# 类用途: 复用用户智能体，减少并发首次访问重复初始化和历史用户常驻。
class OwnerScopedAgentPool:
    """按 OwnerIdentity get-or-create 作用域 agent;有界 LRU、线程安全、锁外构建。

    硬/软两级缓存(真机实锤:215 个 feishu 测试号的 curator 软活把 64 容量 LRU 灌满,
    真活 user-a 的 wake 硬事实被逐出饿死,父任务永不收口)。硬事实(待消费 wake/到期
    policy/未完成子代理/盯守路/调度活)必进池,满时优先逐软;软事实(curator 记忆策展)
    池满不进,且被后来的硬 owner 逐出——软活可以等,硬活不能饿死。请求路/盯守路默认
    硬(用户正在交互),后台 seed 路按发现的事实类别显式传 hard。"""

    # LLM: 初始化原池及退休暂存；仅在途离池对象由退休表持有，可选W bootstrap及容量/惰性/合流合同不变。
    # 函数用途: 绑定构建依赖并准备有界缓存、首次构建协调和活动租用计数。
    def __init__(
        self,
        base_config: Any,
        root: Any,
        *,
        workspace_roots: Any = None,
        max_agents: int | None = None,
        channel_runtime_health_provider: RuntimeHealthProvider | None = None,
        worker_bootstrap=None,
    ) -> None:
        self._base_config = base_config
        self._root = root
        self._workspace_roots = workspace_roots
        self._worker_bootstrap = worker_bootstrap
        # 显式入参 > config owner_agent_pool_max_agents > 兜底常量(千并发调参入口)。
        if max_agents is None:
            max_agents = getattr(base_config, "owner_agent_pool_max_agents", _DEFAULT_MAX_AGENT_COUNT)
        self._max_agents = _resolved_max_agents(max_agents)
        # Runtime health is process-scoped and read-only.  Keep the builder's four positional
        # arguments stable for test doubles while injecting that one shared provider explicitly.
        self._builder = partial(
            build_owner_scoped_agent,
            channel_runtime_health_provider=channel_runtime_health_provider,
        )
        if worker_bootstrap is not None:
            self._builder = partial(self._builder, worker_bootstrap=worker_bootstrap)
        self._hard_agents: OrderedDict[tuple, Any] = OrderedDict()
        self._soft_agents: OrderedDict[tuple, Any] = OrderedDict()
        self._lock = threading.Lock()
        self._last_used: dict[tuple, float] = {}
        self._pins: dict[int, int] = {}
        self._retired_agents: dict[int, Any] = {}
        self._building: dict[tuple, Future] = {}

    # LLM: W命中缓存前后都核原分区/实际home；同owner合流/LRU/租用保main，touch=False不伪造活动事实。
    # 函数用途: 复用或创建唯一用户实例，等待同身份初始化时不重复加载工具和记忆。
    def get(self, owner: Any, *, hard: bool = True, touch: bool = True) -> Any | None:
        self._require_worker_owner(owner)
        key = (owner.provider, owner.owner_kind, owner.owner_id)
        hit = self._get_cached(key, hard=hard)
        if hit is None:
            if not hard and not self._room_for_soft():
                return None
            hit = self._build_once(key, owner, hard=hard)
        with self._lock:
            if hit is not None and touch:
                self._last_used[key] = time.monotonic()
        self._require_worker_owner(owner, hit)
        return hit

    # LLM: 每次消费核原C home/constructor roots，命中后核实际home与原始根而非热config或effective权限投影；不额外派生工作区权限。
    # 函数用途: 阻止跨分区/管理员或已漂移对象进入worker池消费路径。
    def _require_worker_owner(self, owner, agent=None):
        if self._worker_bootstrap is None:
            return
        from .gateway_parts.multiprocess_agent_contract import (
            AgentConstruction,
            require_agent_constructor,
            require_agent_home,
        )
        from .gateway_parts.multiprocess_worker_agent import (
            require_worker_config,
            require_worker_workspace,
        )

        frozen = self._worker_bootstrap
        home = require_worker_config(frozen, self._base_config)
        require_worker_workspace(frozen, self._root, self._workspace_roots)
        require_agent_constructor(frozen, owner, AgentConstruction(home, owner))
        if agent is not None:
            require_agent_home(frozen, owner, agent.home_paths)
            require_worker_workspace(frozen, agent.root, agent.workspace_roots)


    # LLM: Future 只表示同 owner 初始化，不持久化；失败传播给所有等待者，完成后删除临时槽。
    # 函数用途: 防止多个请求同时首次访问同一用户时重复构造重量级实例。
    def _build_once(self, key: tuple, owner: Any, *, hard: bool) -> Any | None:
        with self._lock:
            future = self._building.get(key)
            creator = future is None
            if creator:
                future = self._building[key] = Future()
        if not creator:
            result = future.result()
            if hard:
                cached = self._get_cached(key, hard=True)
                if cached is not None:
                    return cached
                if result is None:
                    result = self._builder(self._base_config, self._root, owner, self._workspace_roots)
                return self._store(key, result, hard=True)
            return result
        try:
            existing = self._get_cached(key, hard=hard)
            result = existing if existing is not None else self._store(
                key, self._builder(self._base_config, self._root, owner, self._workspace_roots), hard=hard,
            )
            future.set_result(result)
            return result
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self._lock:
                self._building.pop(key, None)

    # LLM: 租用只固定身份不授予任务权；若进入pin前已被逐出则恢复租用期MCP并在最后pin释放时退役，所有MCP操作移到池锁外。
    # 函数用途: 保护执行中的实例，并在最后一次释放后清理已淘汰实例的MCP资源。
    @contextmanager
    def pin(self, agent: Any, *, touch: bool = True):
        identity = id(agent)
        reopen = False
        with self._lock:
            self._pins[identity] = self._pins.get(identity, 0) + 1
            in_pool = (
                any(value is agent for value in self._hard_agents.values())
                or any(value is agent for value in self._soft_agents.values())
            )
            if not in_pool:
                self._retired_agents[identity] = agent
                reopen = True
        if reopen:
            _reopen_agent_mcp_clients(agent)
        try:
            yield agent
        finally:
            ready = []
            with self._lock:
                count = self._pins[identity] - 1
                if count:
                    self._pins[identity] = count
                else:
                    self._pins.pop(identity, None)
                    if identity in self._retired_agents:
                        ready = self._retire_agent_locked(agent)
                for key, value in (*self._hard_agents.items(), *self._soft_agents.items()):
                    if value is agent and touch:
                        self._last_used[key] = time.monotonic()
            for retired in ready:
                _close_agent_mcp_clients(retired)

    # LLM: 调用方必须持池锁；身份仍被硬/软表引用则不退役，有pin时暂存强引用，无pin仅返回锁外待关闭列表。
    # 函数用途: 区分仍在池、等待最后租用释放和可以立即清理的实例，不在锁内关闭资源。
    def _retire_agent_locked(self, agent: Any) -> list[Any]:
        if (any(value is agent for value in self._hard_agents.values())
                or any(value is agent for value in self._soft_agents.values())):
            return []
        identity = id(agent)
        if self._pins.get(identity, 0) > 0:
            self._retired_agents[identity] = agent
            return []
        self._retired_agents.pop(identity, None)
        return [agent]

    # LLM: 候选只是缓存投影，调用者必须再验证持久事实及在途线程；过期不等于任务完成。
    # 函数用途: 返回超过空闲期限且无人租用的硬实例，不做磁盘读取。
    def idle_candidates(self, seconds: float) -> list[tuple[tuple, Any, float]]:
        cutoff = time.monotonic() - seconds
        with self._lock:
            return [(key, agent, self._last_used.get(key, 0.0))
                    for key, agent in self._hard_agents.items()
                    if self._last_used.get(key, 0.0) <= cutoff and not self._pins.get(id(agent))]

    # LLM: 保留原时刻/身份CAS及pin拒绝条件；引用移除后统一判退役，MCP关闭不能持池锁。
    # 函数用途: 回收同一空闲版本并清理其MCP资源，防止与新消息争用。
    def evict_idle(self, key: tuple, agent: Any, touched_at: float) -> bool:
        with self._lock:
            if (self._hard_agents.get(key) is not agent or self._pins.get(id(agent))
                    or self._last_used.get(key, 0.0) != touched_at):
                return False
            self._hard_agents.pop(key)
            self._last_used.pop(key, None)
            ready = self._retire_agent_locked(agent)
        for retired in ready:
            _close_agent_mcp_clients(retired)
        return True

    # LLM: Read-only polling may inspect an already-loaded owner, but it must not
    # create, promote, or LRU-touch an Agent. Real ingress/control paths continue
    # to use get(); otherwise idle clients can keep cold owners resident forever.
    # 函数用途: 只查看池里已经存在的用户 Agent；空闲状态刷新不会因此加载整套工具和记忆。
    # LLM: 被动查询仍不物化或touch；W必须先核原冻结分区，再核命中对象实际home，缺失仍返回None。
    # 函数用途: 只查看原池缓存，拒绝旧W代次或归属漂移的实例。
    def peek(self, owner: Any) -> Any | None:
        self._require_worker_owner(owner)
        key = (owner.provider, owner.owner_kind, owner.owner_id)
        with self._lock:
            agent = self._hard_agents.get(key) or self._soft_agents.get(key)
        self._require_worker_owner(owner, agent)
        return agent

    def _room_for_soft(self) -> bool:
        """锁内判断软 agent 是否有落点:软表非空可逐软腾位,或总容量未满。"""
        with self._lock:
            if self._soft_agents:
                return True
            return len(self._hard_agents) + len(self._soft_agents) < self._max_agents

    def _get_cached(self, key: tuple, *, hard: bool) -> Any | None:
        with self._lock:
            if hard:
                existing = self._hard_agents.get(key)
                if existing is not None:
                    self._hard_agents.move_to_end(key)  # LRU touch
                    return existing
                existing = self._soft_agents.pop(key, None)
                if existing is not None:
                    # 软 owner 重新以硬身份被请求 → 升级(软表删除,硬表登记)
                    self._hard_agents[key] = existing
                    self._hard_agents.move_to_end(key)
                    return existing
                return None
            existing = self._soft_agents.get(key)
            if existing is not None:
                self._soft_agents.move_to_end(key)
                return existing
            existing = self._hard_agents.get(key)
            if existing is not None:
                self._hard_agents.move_to_end(key)  # 已硬不降级,只刷新活跃位置
                return existing
            return None

    # LLM: 原硬软准入/双检及记账保持；替换完成后按身份判退役，未采用候选同样退役，关闭全部在锁外且不取消持久工作。
    # 函数用途: 存放初始化结果并回收被逐出或未采用实例的MCP资源，仍保护在途租用。
    def _store(self, key: tuple, agent: Any, *, hard: bool) -> Any | None:
        ready = []
        with self._lock:
            # 双检期间多建的候选也须清理；若是同对象，退休检查会保留池内引用。
            existing = self._hard_agents.get(key)
            if existing is None:
                existing = self._soft_agents.get(key)
            total = len(self._hard_agents) + len(self._soft_agents)
            if existing is not None:
                ready = self._retire_agent_locked(agent)
                result = existing
            elif not hard and total >= self._max_agents and not self._soft_agents:
                ready = self._retire_agent_locked(agent)
                result = None
            else:
                evicted = []
                if total >= self._max_agents:
                    victims = self._soft_agents if self._soft_agents else self._hard_agents
                    old_key, old_agent = victims.popitem(last=False)
                    self._last_used.pop(old_key, None)
                    evicted.append(old_agent)
                self._last_used[key] = time.monotonic()
                target = self._hard_agents if hard else self._soft_agents
                target[key] = agent
                target.move_to_end(key)
                for old_agent in evicted:
                    ready.extend(self._retire_agent_locked(old_agent))
                result = agent
        for retired in ready:
            _close_agent_mcp_clients(retired)
        return result

    def active_count(self) -> int:
        with self._lock:
            return len(self._hard_agents) + len(self._soft_agents)

    def active_agents(self) -> list[Any]:
        """当前缓存的所有作用域 agent 的线程安全快照(供后台循环逐 owner tick 唤醒消费)。

        硬表在前(硬事实 owner 先被 tick);返回列表副本(非内部视图),调用方遍历时不受
        并发 get/逐出影响;快照瞬时,遍历期间被逐出的 agent 仍在列表里(无害:多 tick 一次)。"""
        with self._lock:
            return [*self._hard_agents.values(), *self._soft_agents.values()]

    # LLM: Background conversation/watch schedulers must only retain agents backed by
    # hard durable work; soft curator-only instances are admitted separately by worker slots.
    # 函数用途: 返回有前台请求、未完成任务或盯守事实的常驻 agent 快照。
    def hard_agents(self) -> list[Any]:
        """返回硬事实 agent 的线程安全快照，保持 LRU 顺序。"""
        with self._lock:
            return list(self._hard_agents.values())

    # LLM: 仅移除精确软实例，硬升级仍使逐出无操作；pin暂存退休，MCP清理只能在池锁外。
    # 函数用途: 释放临时软实例并按最后租用时刻清理MCP，保留已升级或仍被其它池条目引用的对象。
    def evict_soft_agent_id(self, agent_id: int) -> bool:
        """按对象身份逐出软 agent；返回是否实际释放。"""
        with self._lock:
            matched_key = None
            for key, agent in self._soft_agents.items():
                if id(agent) == agent_id:
                    matched_key = key
                    break
            if matched_key is None:
                return False
            agent = self._soft_agents.pop(matched_key)
            self._last_used.pop(matched_key, None)
            ready = self._retire_agent_locked(agent)
        for retired in ready:
            _close_agent_mcp_clients(retired)
        return True


# LLM: 登记本只投影近期身份，实例与执行租用由共享 owner 池管理，持久工作另有事实源。
# 类用途: 让真实入站和后台发现交换有界的活跃身份，不长期持有历史用户的完整实例。
class ActiveOwnerRegistry:
    """网关进程内"最近活跃 scoped owner"身份登记表(有界分层 LRU、线程安全)。

    只登记 OwnerIdentity(轻量,不持有 agent 实例),解决"请求 worker 与后台主代理循环各建自己的
    agent+owner 池、互不可见"的断裂:请求路解析出 scoped owner 时登记进来,后台循环据此快照逐 owner
    tick 唤醒(实例统一由共享 owner 池取得，可变回合字段沿线程局部合同隔离)。有界防无限涨(复用池同款
    "无界结构必有界"律);单 owner/未开 scoping 时永不登记 → 表空 → 后台只 tick base,行为不变。

    硬/软两层(与 OwnerScopedAgentPool 同款):硬事实(待消费 wake/到期 policy/未完成子代理/
    盯守路/调度活)必登记,满时优先逐软;软事实(curator 记忆策展)登记表满则跳过(返回 False),
    且被后来的硬 owner 逐出。请求路(入站交互)/到期任务默认硬;后台磁盘发现路按事实类别显式传。"""

    # LLM: 登记本只保存身份与最近写入时刻；它是唤醒发现的缓存，不持有 Agent 或任务状态。
    # 函数用途: 创建有界的最近活跃身份登记本。
    def __init__(self, *, max_owners: int = _DEFAULT_MAX_AGENT_COUNT) -> None:
        self._max_owners = _resolved_max_agents(max_owners)
        self._hard: OrderedDict[tuple, Any] = OrderedDict()
        self._soft: OrderedDict[tuple, Any] = OrderedDict()
        self._lock = threading.Lock()
        self._recorded_at: dict[tuple, float] = {}

    # LLM: 每次真实入站/发现更新单调时刻，退休扫描必须重检，不能抹掉并发新登记。
    # 函数用途: 记录用户的近期活动身份，按既有硬/软优先级维护容量。
    def record(self, owner: Any, *, hard: bool = True) -> bool:
        """登记 owner;返回是否在表内(软满拒入时 False)。硬:满时优先逐软再逐硬;软:满则拒。"""
        key = (owner.provider, owner.owner_kind, owner.owner_id)
        with self._lock:
            self._recorded_at = {key: value for key, value in self._recorded_at.items() if key in self._hard or key in self._soft}
            self._recorded_at[key] = time.monotonic()
            if hard:
                existing = self._hard.get(key)
                if existing is not None:
                    self._hard.move_to_end(key)  # LRU touch:最近活跃留到最后
                    return True
                self._soft.pop(key, None)  # 软 owner 升级为硬
                self._hard[key] = owner
                self._hard.move_to_end(key)
                if len(self._hard) + len(self._soft) <= self._max_owners:
                    return True
                if self._soft:
                    self._soft.popitem(last=False)  # 逐出最久未活跃的软 owner
                else:
                    self._hard.popitem(last=False)  # 全是硬 → 逐硬最久(有界)
                return True
            existing = self._soft.get(key)
            if existing is not None:
                self._soft.move_to_end(key)
                return True
            if key in self._hard:
                self._hard.move_to_end(key)  # 已硬不降级,只刷新活跃位置
                return True
            if len(self._hard) + len(self._soft) >= self._max_owners:
                return False  # 登记表满 → 软活等下一轮(绝不挤硬)
            self._soft[key] = owner
            self._soft.move_to_end(key)
            return True

    # LLM: 只移除检查开始前的身份版本；新入站/持久唤醒登记优先，不被旧空闲扫描抹掉。
    # 函数用途: 释放空闲用户的后台轮询登记，磁盘身份和历史保留。
    def discard_idle(self, key: tuple, touched_at: float) -> bool:
        with self._lock:
            if self._recorded_at.get(key, 0.0) > touched_at:
                return False
            self._hard.pop(key, None)
            self._soft.pop(key, None)
            self._recorded_at.pop(key, None)
            return True

    def snapshot(self) -> list[Any]:
        """全部在表 owner(硬先软后),按 LRU 活跃序。"""
        with self._lock:
            return [*self._hard.values(), *self._soft.values()]

    def hard_snapshot(self) -> list[Any]:
        """仅在表硬 owner 快照(供 _sync_owner_schedulers 按类别建池,硬优先)。"""
        with self._lock:
            return list(self._hard.values())

    def soft_snapshot(self) -> list[Any]:
        """仅在表软 owner 快照(供 _sync_owner_schedulers 按类别建池,软靠后)。"""
        with self._lock:
            return list(self._soft.values())


_SHARED_REGISTRY_LOCK = threading.Lock()


def shared_active_owner_registry(agent: Any) -> ActiveOwnerRegistry:
    """取或建挂在共享网关 agent 上的活跃 owner 登记表(双检锁,多循环并发首次只建一个)。

    请求 worker 循环与后台主代理循环都拿同一个 context.agent 调本函数 → 拿到同一个登记表实例:
    请求路 record、后台路 snapshot,跨线程共享的只是身份(轻量),不是 agent。"""
    existing = getattr(agent, "_active_owner_registry", None)
    if existing is not None:
        return existing
    with _SHARED_REGISTRY_LOCK:
        existing = getattr(agent, "_active_owner_registry", None)
        if existing is None:
            config = getattr(agent, "config", None)
            max_owners = getattr(config, "owner_agent_pool_max_agents", _DEFAULT_MAX_AGENT_COUNT)
            existing = ActiveOwnerRegistry(max_owners=max_owners)
            agent._active_owner_registry = existing
        return existing
