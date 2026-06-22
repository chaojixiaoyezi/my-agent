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

    return SimpleAgent(_config_with_owner(base_config, owner), root, workspace_roots=workspace_roots)


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
