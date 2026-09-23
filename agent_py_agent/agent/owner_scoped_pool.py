# LLM: owner 池只管理进程内实例，唯一身份及持久工作沿既有 owner/thread 合同；空闲回收不删除用户资料。
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

_DEFAULT_MAX_AGENTS = 64


def _resolved_max_agents(value: Any) -> int:
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        resolved = _DEFAULT_MAX_AGENTS
    return max(1, resolved if resolved > 0 else _DEFAULT_MAX_AGENTS)


def _config_with_owner(base_config: Any, owner: Any) -> Any:
    """克隆基础 config,把 owner 三字段覆盖成该用户(其余配置原样继承)。"""
    return dataclasses.replace(
        base_config,
        my_agent_owner_provider=owner.provider,
        my_agent_owner_kind=owner.owner_kind,
        my_agent_owner_id=owner.owner_id,
    )


def build_owner_scoped_agent(
    base_config: Any,
    root: Any,
    owner: Any,
    workspace_roots: Any,
    *,
    channel_runtime_health_provider: RuntimeHealthProvider | None = None,
) -> Any:
    """按 owner 建一个作用域 SimpleAgent(独立 home/记忆/local_store)。延迟导入防循环。"""
    from .core import SimpleAgent

    agent = SimpleAgent(
        _config_with_owner(base_config, owner),
        root,
        workspace_roots=workspace_roots,
        channel_runtime_health_provider=channel_runtime_health_provider,
    )
    _maybe_seed_feishu_call_name(base_config, owner, agent)  # 飞书首聊自动称呼,best-effort 永不抛
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


# LLM: 同 owner 并发构建合流；缓存、活动租用和持久任务是不同寿命，调用者必须完成租用。
# 类用途: 复用用户智能体，减少并发首次访问重复初始化和历史用户常驻。
class OwnerScopedAgentPool:
    """按 OwnerIdentity get-or-create 作用域 agent;有界 LRU、线程安全、锁外构建。

    硬/软两级缓存(真机实锤:215 个 feishu 测试号的 curator 软活把 64 容量 LRU 灌满,
    真活 user-a 的 wake 硬事实被逐出饿死,父任务永不收口)。硬事实(待消费 wake/到期
    policy/未完成子代理/盯守路/调度活)必进池,满时优先逐软;软事实(curator 记忆策展)
    池满不进,且被后来的硬 owner 逐出——软活可以等,硬活不能饿死。请求路/盯守路默认
    硬(用户正在交互),后台 seed 路按发现的事实类别显式传 hard。"""

    # LLM: 初始化只创建进程内容器，不加载用户；容量与空闲策略由 Gateway 配置和调用者统一应用。
    # 函数用途: 绑定构建依赖并准备有界缓存、首次构建协调和活动租用计数。
    def __init__(
        self,
        base_config: Any,
        root: Any,
        *,
        workspace_roots: Any = None,
        max_agents: int | None = None,
        channel_runtime_health_provider: RuntimeHealthProvider | None = None,
    ) -> None:
        self._base_config = base_config
        self._root = root
        self._workspace_roots = workspace_roots
        # 显式入参 > config owner_agent_pool_max_agents > 兜底常量(千并发调参入口)。
        if max_agents is None:
            max_agents = getattr(base_config, "owner_agent_pool_max_agents", _DEFAULT_MAX_AGENTS)
        self._max_agents = _resolved_max_agents(max_agents)
        # Runtime health is process-scoped and read-only.  Keep the builder's four positional
        # arguments stable for test doubles while injecting that one shared provider explicitly.
        self._builder = partial(
            build_owner_scoped_agent,
            channel_runtime_health_provider=channel_runtime_health_provider,
        )
        self._hard_agents: OrderedDict[tuple, Any] = OrderedDict()
        self._soft_agents: OrderedDict[tuple, Any] = OrderedDict()
        self._lock = threading.Lock()
        self._last_used: dict[tuple, float] = {}
        self._pins: dict[int, int] = {}
        self._building: dict[tuple, Future] = {}

    # LLM: 同一 owner 首次构建合流；真实入口刷新空闲时刻，后台扫描 touch=False 不伪造使用。
    # 函数用途: 复用或创建唯一用户实例，等待同身份初始化时不重复加载工具和记忆。
    def get(self, owner: Any, *, hard: bool = True, touch: bool = True) -> Any | None:
        key = (owner.provider, owner.owner_kind, owner.owner_id)
        hit = self._get_cached(key, hard=hard)
        if hit is None:
            if not hard and not self._room_for_soft():
                return None
            hit = self._build_once(key, owner, hard=hard)
        with self._lock:
            if hit is not None and touch:
                self._last_used[key] = time.monotonic()
        return hit

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

    # LLM: 执行租用只固定对象身份，不授予任务权；finally 释放并从完成时重新计空闲时间。
    # 函数用途: 请求执行和后台扫描持有实例期间禁止空闲回收，不取消用户任务。
    @contextmanager
    def pin(self, agent: Any, *, touch: bool = True):
        identity = id(agent)
        with self._lock:
            self._pins[identity] = self._pins.get(identity, 0) + 1
        try:
            yield agent
        finally:
            with self._lock:
                count = self._pins[identity] - 1
                if count:
                    self._pins[identity] = count
                else:
                    self._pins.pop(identity, None)
                for key, value in (*self._hard_agents.items(), *self._soft_agents.items()):
                    if value is agent and touch:
                        self._last_used[key] = time.monotonic()

    # LLM: 候选只是缓存投影，调用者必须再验证持久事实及在途线程；过期不等于任务完成。
    # 函数用途: 返回超过空闲期限且无人租用的硬实例，不做磁盘读取。
    def idle_candidates(self, seconds: float) -> list[tuple[tuple, Any, float]]:
        cutoff = time.monotonic() - seconds
        with self._lock:
            return [(key, agent, self._last_used.get(key, 0.0))
                    for key, agent in self._hard_agents.items()
                    if self._last_used.get(key, 0.0) <= cutoff and not self._pins.get(id(agent))]

    # LLM: 使用时刻或精确实例变化时不能释放；删除仅为缓存引用，不关闭仍被读取方持有的资源。
    # 函数用途: 完成事实检查后回收同一空闲版本，防止与新消息争用。
    def evict_idle(self, key: tuple, agent: Any, touched_at: float) -> bool:
        with self._lock:
            if (self._hard_agents.get(key) is not agent or self._pins.get(id(agent))
                    or self._last_used.get(key, 0.0) != touched_at):
                return False
            self._hard_agents.pop(key)
            self._last_used.pop(key, None)
            return True

    # LLM: Read-only polling may inspect an already-loaded owner, but it must not
    # create, promote, or LRU-touch an Agent. Real ingress/control paths continue
    # to use get(); otherwise idle clients can keep cold owners resident forever.
    # 函数用途: 只查看池里已经存在的用户 Agent；空闲状态刷新不会因此加载整套工具和记忆。
    def peek(self, owner: Any) -> Any | None:
        key = (owner.provider, owner.owner_kind, owner.owner_id)
        with self._lock:
            return self._hard_agents.get(key) or self._soft_agents.get(key)

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

    # LLM: 注册对象与空闲时刻在同一锁内，LRU 逐出同步清除寿命元数据；不取消持久工作。
    # 函数用途: 存放初始化结果并保持缓存与记账容量一致。
    def _store(self, key: tuple, agent: Any, *, hard: bool) -> Any | None:
        with self._lock:
            # 双检:构建期间别人已建,丢弃多建的(罕见竞态,无害);升级场景两表都查。
            existing = self._hard_agents.get(key)
            if existing is None:
                existing = self._soft_agents.get(key)
            if existing is not None:
                return existing
            total = len(self._hard_agents) + len(self._soft_agents)
            if hard:
                if total >= self._max_agents:
                    if self._soft_agents:
                        self._last_used.pop(self._soft_agents.popitem(last=False)[0], None)  # 逐出软 owner
                    else:
                        self._last_used.pop(self._hard_agents.popitem(last=False)[0], None)  # 逐出旧 owner
                self._last_used[key] = time.monotonic()
                self._hard_agents[key] = agent
                self._hard_agents.move_to_end(key)
                return agent
            if total >= self._max_agents:
                if self._soft_agents:
                    self._last_used.pop(self._soft_agents.popitem(last=False)[0], None)  # 软内互挤
                else:
                    return None  # 总容量被硬占满 → 软活等下一轮(绝不逐硬腾位)
            self._last_used[key] = time.monotonic()
            self._soft_agents[key] = agent
            self._soft_agents.move_to_end(key)
            return agent

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

    # LLM: A completed low-priority curator releases only the exact soft instance it used;
    # a concurrent hard promotion moves the instance out of this table and makes eviction a no-op.
    # 函数用途: 记忆策展完成后释放临时软 agent；若期间用户重新活跃则保留其已升级实例。
    def evict_soft_agent_id(self, agent_id: int) -> bool:
        """按对象身份逐出软 agent；返回是否实际释放。"""
        with self._lock:
            matched_key = None
            for key, agent in self._soft_agents.items():
                if id(agent) == agent_id:
                    matched_key = key
                    break
            if matched_key is not None:
                self._soft_agents.pop(matched_key, None)
                self._last_used.pop(matched_key, None)
                return True
        return False


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
    def __init__(self, *, max_owners: int = _DEFAULT_MAX_AGENTS) -> None:
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
            max_owners = getattr(config, "owner_agent_pool_max_agents", _DEFAULT_MAX_AGENTS)
            existing = ActiveOwnerRegistry(max_owners=max_owners)
            agent._active_owner_registry = existing
        return existing
