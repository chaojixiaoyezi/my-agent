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
from typing import Any

_DEFAULT_MAX_AGENTS = 64


def _config_with_owner(base_config: Any, owner: Any) -> Any:
    """克隆基础 config,把 owner 三字段覆盖成该用户(其余配置原样继承)。"""
    return dataclasses.replace(
        base_config,
        my_agent_owner_provider=owner.provider,
        my_agent_owner_kind=owner.owner_kind,
        my_agent_owner_id=owner.owner_id,
    )


def build_owner_scoped_agent(base_config: Any, root: Any, owner: Any, workspace_roots: Any) -> Any:
    """按 owner 建一个作用域 SimpleAgent(独立 home/记忆/local_store)。延迟导入防循环。"""
    from .core import SimpleAgent

    agent = SimpleAgent(_config_with_owner(base_config, owner), root, workspace_roots=workspace_roots)
    _maybe_seed_feishu_call_name(base_config, owner, agent)  # 飞书首聊自动称呼,best-effort 永不抛
    return agent


def _maybe_seed_feishu_call_name(base_config: Any, owner: Any, agent: Any) -> None:
    """飞书用户首次建作用域 agent 时,用其飞书姓名填 USER.md 的"称呼"(空才填,全程 fail-open)。
    无通讯录权限/网络失败→称呼留空由 agent 自然询问;绝不因此影响 agent 创建。"""
    try:
        if getattr(owner, "provider", "") != "feishu":
            return
        user_md = getattr(getattr(agent, "home_paths", None), "owner_user_md", None)
        from .adapter.feishu_profile import call_name_is_empty, fetch_feishu_display_name, seed_call_name

        if not user_md or not call_name_is_empty(user_md):
            return  # 文件不在 / 称呼已填 → 跳过
        from .settings.secret_ref import resolve_secret_ref

        app_id = resolve_secret_ref(getattr(base_config, "feishu_app_id", "") or "")
        app_secret = resolve_secret_ref(getattr(base_config, "feishu_app_secret", "") or "")
        name = fetch_feishu_display_name(app_id, app_secret, str(getattr(owner, "owner_id", "")))
        if name:
            seed_call_name(user_md, name)
    except Exception:
        pass  # 绝不因 auto-name 失败影响 agent 创建


class OwnerScopedAgentPool:
    """按 OwnerIdentity get-or-create 作用域 agent;有界 LRU、线程安全、锁外构建。"""

    def __init__(
        self, base_config: Any, root: Any, *, workspace_roots: Any = None, max_agents: int = _DEFAULT_MAX_AGENTS
    ) -> None:
        self._base_config = base_config
        self._root = root
        self._workspace_roots = workspace_roots
        self._max_agents = max(1, int(max_agents))
        self._builder = build_owner_scoped_agent  # 测试可替身
        self._agents: OrderedDict[tuple, Any] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, owner: Any) -> Any:
        """取或建该 owner 的作用域 agent。命中走缓存(LRU touch);未命中锁外构建再双检登记。"""
        key = (owner.provider, owner.owner_kind, owner.owner_id)
        hit = self._get_cached(key)
        if hit is not None:
            return hit
        agent = self._builder(self._base_config, self._root, owner, self._workspace_roots)
        return self._store(key, agent)

    def _get_cached(self, key: tuple) -> Any:
        with self._lock:
            existing = self._agents.get(key)
            if existing is not None:
                self._agents.move_to_end(key)  # LRU touch
            return existing

    def _store(self, key: tuple, agent: Any) -> Any:
        with self._lock:
            existing = self._agents.get(key)
            if existing is not None:
                return existing  # 双检:构建期间别人已建,丢弃多建的(罕见竞态,无害)
            while len(self._agents) >= self._max_agents:
                self._agents.popitem(last=False)  # 逐出最久未用(有界,防无限涨)
            self._agents[key] = agent
            return agent

    def active_count(self) -> int:
        with self._lock:
            return len(self._agents)

    def active_agents(self) -> list[Any]:
        """当前缓存的所有作用域 agent 的线程安全快照(供后台循环逐 owner tick 唤醒消费)。

        返回列表副本(非内部 OrderedDict 视图),调用方遍历时不受并发 get/逐出影响;快照瞬时,
        遍历期间被逐出的 agent 仍在列表里(无害:多 tick 一次空 store)。"""
        with self._lock:
            return list(self._agents.values())


class ActiveOwnerRegistry:
    """网关进程内"最近活跃 scoped owner"身份登记表(有界 LRU、线程安全)。

    只登记 OwnerIdentity(轻量,不持有 agent 实例),解决"请求 worker 与后台主代理循环各建自己的
    agent+owner 池、互不可见"的断裂:请求路解析出 scoped owner 时登记进来,后台循环据此快照逐 owner
    tick 唤醒(各自建自己线程私有的 scoped agent,不跨线程共享 agent 实例)。有界防无限涨(复用池同款
    "无界结构必有界"律);单 owner/未开 scoping 时永不登记 → 表空 → 后台只 tick base,行为不变。"""

    def __init__(self, *, max_owners: int = _DEFAULT_MAX_AGENTS) -> None:
        self._max_owners = max(1, int(max_owners))
        self._owners: OrderedDict[tuple, Any] = OrderedDict()
        self._lock = threading.Lock()

    def record(self, owner: Any) -> None:
        key = (owner.provider, owner.owner_kind, owner.owner_id)
        with self._lock:
            self._owners[key] = owner
            self._owners.move_to_end(key)  # LRU touch:最近活跃留到最后
            while len(self._owners) > self._max_owners:
                self._owners.popitem(last=False)  # 逐出最久未活跃

    def snapshot(self) -> list[Any]:
        with self._lock:
            return list(self._owners.values())


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
            existing = ActiveOwnerRegistry()
            agent._active_owner_registry = existing
        return existing
