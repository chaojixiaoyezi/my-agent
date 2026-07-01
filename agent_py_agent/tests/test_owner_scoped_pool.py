"""Phase 1 真测:OwnerScopedAgentPool —— 每个 owner 一个隔离 agent + 有界 LRU。

多用户飞书 per-用户隔离的地基。真建两个飞书用户的作用域 agent,断言 home/记忆/local_store 路径
互相隔离(不串户);同一 owner 命中缓存(不重建);有界 LRU 逐出最久未用;config owner 字段被正确
覆盖、其余配置继承。
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.owner_scoped_pool import (
    ActiveOwnerRegistry,
    OwnerScopedAgentPool,
    _config_with_owner,
    shared_active_owner_registry,
)
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity


def _base_config(tmp_path) -> AgentConfig:
    return AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"))


# ---------- 真隔离(真建 SimpleAgent) ----------

def test_two_feishu_users_get_isolated_agents(tmp_path) -> None:
    pool = OwnerScopedAgentPool(_base_config(tmp_path), tmp_path)
    user_a = OwnerIdentity.provider_user("feishu", "user-a")
    user_b = OwnerIdentity.provider_user("feishu", "user-b")
    agent_a = pool.get(user_a)
    agent_b = pool.get(user_b)
    assert agent_a is not agent_b
    # owner 身份隔离(home_paths.owner_id 是作用域路径 providers/feishu/users/<id>)
    assert agent_a.home_paths.owner_id != agent_b.home_paths.owner_id
    assert "user-a" in agent_a.home_paths.owner_id and "feishu" in agent_a.home_paths.owner_id
    assert "user-b" in agent_b.home_paths.owner_id
    # local_store 数据库路径隔离(不同 owner 目录 → 数据不串户)
    assert str(agent_a.local_store.db_path) != str(agent_b.local_store.db_path)
    assert "user-a" in str(agent_a.local_store.db_path) and "user-b" in str(agent_b.local_store.db_path)


def test_same_owner_is_cached_not_rebuilt(tmp_path) -> None:
    pool = OwnerScopedAgentPool(_base_config(tmp_path), tmp_path)
    user = OwnerIdentity.provider_user("feishu", "u1")
    first = pool.get(user)
    assert pool.get(user) is first  # 命中缓存,同一实例(不重建)
    assert pool.active_count() == 1


# ---------- 池逻辑(替身 builder,免建真 agent) ----------

def _counting_pool(tmp_path, *, max_agents: int):
    pool = OwnerScopedAgentPool(_base_config(tmp_path), tmp_path, max_agents=max_agents)
    builds: list[tuple] = []

    def _fake_builder(base_config, root, owner, workspace_roots):
        builds.append((owner.provider, owner.owner_kind, owner.owner_id))
        return SimpleNamespace(owner_id=owner.owner_id)

    pool._builder = _fake_builder
    return pool, builds


def test_distinct_owners_build_distinct_agents(tmp_path) -> None:
    pool, builds = _counting_pool(tmp_path, max_agents=64)
    a = pool.get(OwnerIdentity.provider_user("feishu", "a"))
    b = pool.get(OwnerIdentity.provider_group("feishu", "g1"))
    assert a is not b and len(builds) == 2  # user 与 group 不同 owner → 各建一个


def test_lru_bound_evicts_oldest(tmp_path) -> None:
    pool, builds = _counting_pool(tmp_path, max_agents=3)
    for i in range(5):
        pool.get(OwnerIdentity.provider_user("feishu", f"u{i}"))
    assert pool.active_count() == 3  # 有界:不随用户数无限涨
    # 最久未用(u0/u1)被逐出,再取 u0 会重建(builds 增加)
    before = len(builds)
    pool.get(OwnerIdentity.provider_user("feishu", "u0"))
    assert len(builds) == before + 1


def test_lru_touch_keeps_recently_used(tmp_path) -> None:
    pool, builds = _counting_pool(tmp_path, max_agents=2)
    pool.get(OwnerIdentity.provider_user("feishu", "x"))
    pool.get(OwnerIdentity.provider_user("feishu", "y"))
    pool.get(OwnerIdentity.provider_user("feishu", "x"))  # touch x → x 变最近用
    pool.get(OwnerIdentity.provider_user("feishu", "z"))  # 触顶逐出最久未用(y)
    before = len(builds)
    pool.get(OwnerIdentity.provider_user("feishu", "x"))  # x 还在(被 touch 过)
    assert len(builds) == before  # 无重建 → x 命中缓存


# ---------- config owner 覆盖 ----------

# ---------- active_agents 快照(供后台循环逐 owner tick) ----------

def test_active_agents_snapshots_cached_agents(tmp_path) -> None:
    pool, _ = _counting_pool(tmp_path, max_agents=64)
    pool.get(OwnerIdentity.provider_user("feishu", "a"))
    pool.get(OwnerIdentity.provider_user("feishu", "b"))
    agents = pool.active_agents()
    assert len(agents) == 2
    assert {a.owner_id for a in agents} == {"a", "b"}
    # 返回副本:改返回列表不影响池内部(下次取仍是 2 个)
    agents.clear()
    assert len(pool.active_agents()) == 2


# ---------- ActiveOwnerRegistry(活跃 owner 身份登记表) ----------

def test_active_owner_registry_records_and_snapshots() -> None:
    registry = ActiveOwnerRegistry()
    a = OwnerIdentity.provider_user("feishu", "a")
    b = OwnerIdentity.provider_user("feishu", "b")
    registry.record(a)
    registry.record(b)
    registry.record(a)  # 重复登记不产生重复项
    keys = {(o.provider, o.owner_kind, o.owner_id) for o in registry.snapshot()}
    assert keys == {("feishu", "user", "a"), ("feishu", "user", "b")}


def test_active_owner_registry_is_bounded_lru() -> None:
    registry = ActiveOwnerRegistry(max_owners=2)
    for name in ("u0", "u1", "u2"):
        registry.record(OwnerIdentity.provider_user("feishu", name))
    ids = [o.owner_id for o in registry.snapshot()]
    assert len(ids) == 2  # 有界,不随用户数无限涨
    assert "u0" not in ids and "u2" in ids  # 逐出最久未活跃(u0),保留最近(u2)


def test_shared_active_owner_registry_is_same_instance_per_agent() -> None:
    agent = SimpleNamespace()
    first = shared_active_owner_registry(agent)
    second = shared_active_owner_registry(agent)
    assert first is second  # 同一 agent → 同一登记表(请求路 record 与后台路 snapshot 共享)


# ---------- config owner 覆盖 ----------

def test_config_with_owner_overrides_owner_keeps_rest(tmp_path) -> None:
    base = AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), tool_protocol="native")
    scoped = _config_with_owner(base, OwnerIdentity.provider_user("feishu", "alice"))
    assert scoped.my_agent_owner_provider == "feishu"
    assert scoped.my_agent_owner_kind == "user"
    assert scoped.my_agent_owner_id == "alice"
    assert scoped.model_backend == "echo" and scoped.tool_protocol == "native"  # 其余配置继承
    assert base.my_agent_owner_id == "main"  # 原 config 不被改(replace 返回新实例)
