"""按 owner 隔离的 agent 池(多用户飞书 per-用户隔离 + 审计 #2/#13)。

实证现状:网关只建一个 owner="main" 的 agent 复用,所有飞书用户的 home/记忆/数据/成本/审计全混在
main 名下——多用户多公司场景下串户(隐私/合规硬伤)、不可计费、审计不到人。本池按 OwnerIdentity 给
每个用户建一个作用域 agent(把 config 的 owner 三字段覆盖成该用户),其 home/记忆/local_store 天然
隔离,成本/审计自动按用户分。有界 LRU 逐出最久未用(复用审计 #16 的"无界结构必有界"律),线程安全。

默认不启用:网关无 per-请求 owner 时仍走单 agent main(不破现有);启用与解析见 Phase 2 网关接线。
构建在锁外做(SimpleAgent 初始化较重),不阻塞其他 owner 的并发请求;双检避免重复登记。
"""

from __future__ import annotations

import dataclasses
import threading
from collections import OrderedDict
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


class OwnerScopedAgentPool:
    """按 OwnerIdentity get-or-create 作用域 agent;有界 LRU、线程安全、锁外构建。

    硬/软两级缓存(真机实锤:215 个 feishu 测试号的 curator 软活把 64 容量 LRU 灌满,
    真活 user-a 的 wake 硬事实被逐出饿死,父任务永不收口)。硬事实(待消费 wake/到期
    policy/未完成子代理/盯守路/调度活)必进池,满时优先逐软;软事实(curator 记忆策展)
    池满不进,且被后来的硬 owner 逐出——软活可以等,硬活不能饿死。请求路/盯守路默认
    硬(用户正在交互),后台 seed 路按发现的事实类别显式传 hard。"""

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

    def get(self, owner: Any, *, hard: bool = True) -> Any | None:
        """取或建该 owner 的作用域 agent。硬:命中缓存(LRU touch)或锁外构建;满时优先逐软。
        软:缓存命中(命中硬表不降级)或构建;满时逐软表最久,软表空且总容量被硬占满则
        None(软活可以等,不挤硬活)——调用方须容忍 None。软拒发生在构建前,不白建 agent。"""
        key = (owner.provider, owner.owner_kind, owner.owner_id)
        hit = self._get_cached(key, hard=hard)
        if hit is not None:
            return hit
        if not hard and not self._room_for_soft():
            return None  # 总容量被硬占满 → 软活等下一轮(构建开销大,绝不白建)
        agent = self._builder(self._base_config, self._root, owner, self._workspace_roots)
        return self._store(key, agent, hard=hard)

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
                        self._soft_agents.popitem(last=False)  # 逐出最久未用软 owner
                    else:
                        self._hard_agents.popitem(last=False)  # 全是硬 → 逐硬最久(有界)
                self._hard_agents[key] = agent
                self._hard_agents.move_to_end(key)
                return agent
            if total >= self._max_agents:
                if self._soft_agents:
                    self._soft_agents.popitem(last=False)  # 软内互挤
                else:
                    return None  # 总容量被硬占满 → 软活等下一轮(绝不逐硬腾位)
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


class ActiveOwnerRegistry:
    """网关进程内"最近活跃 scoped owner"身份登记表(有界分层 LRU、线程安全)。

    只登记 OwnerIdentity(轻量,不持有 agent 实例),解决"请求 worker 与后台主代理循环各建自己的
    agent+owner 池、互不可见"的断裂:请求路解析出 scoped owner 时登记进来,后台循环据此快照逐 owner
    tick 唤醒(各自建自己线程私有的 scoped agent,不跨线程共享 agent 实例)。有界防无限涨(复用池同款
    "无界结构必有界"律);单 owner/未开 scoping 时永不登记 → 表空 → 后台只 tick base,行为不变。

    硬/软两层(与 OwnerScopedAgentPool 同款):硬事实(待消费 wake/到期 policy/未完成子代理/
    盯守路/调度活)必登记,满时优先逐软;软事实(curator 记忆策展)登记表满则跳过(返回 False),
    且被后来的硬 owner 逐出。请求路(入站交互)/到期任务默认硬;后台磁盘发现路按事实类别显式传。"""

    def __init__(self, *, max_owners: int = _DEFAULT_MAX_AGENTS) -> None:
        self._max_owners = _resolved_max_agents(max_owners)
        self._hard: OrderedDict[tuple, Any] = OrderedDict()
        self._soft: OrderedDict[tuple, Any] = OrderedDict()
        self._lock = threading.Lock()

    def record(self, owner: Any, *, hard: bool = True) -> bool:
        """登记 owner;返回是否在表内(软满拒入时 False)。硬:满时优先逐软再逐硬;软:满则拒。"""
        key = (owner.provider, owner.owner_kind, owner.owner_id)
        with self._lock:
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
