"""Phase 1 真测:OwnerScopedAgentPool —— 每个 owner 一个隔离 agent + 有界 LRU。

多用户飞书 per-用户隔离的地基。真建两个飞书用户的作用域 agent,断言 home/记忆/local_store 路径
互相隔离(不串户);同一 owner 命中缓存(不重建);有界 LRU 逐出最久未用;config owner 字段被正确
覆盖、其余配置继承。
"""

from __future__ import annotations

import json
from collections import OrderedDict
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.owner_scoped_pool import (
    ActiveOwnerRegistry,
    OwnerScopedAgentPool,
    PoolCapacityExceededError,
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


def test_peek_never_builds_or_touches_lru(tmp_path) -> None:
    pool, builds = _counting_pool(tmp_path, max_agents=2)
    owner_a = OwnerIdentity.provider_user("feishu", "a")
    owner_b = OwnerIdentity.provider_user("feishu", "b")
    missing = OwnerIdentity.provider_user("feishu", "missing")
    agent_a = pool.get(owner_a)
    pool.get(owner_b)

    assert pool.peek(missing) is None
    assert pool.peek(owner_a) is agent_a
    assert len(builds) == 2

    pool.get(OwnerIdentity.provider_user("feishu", "c"))
    assert pool.peek(owner_a) is None


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


def test_shared_active_owner_registry_uses_agent_pool_limit() -> None:
    agent = SimpleNamespace(config=SimpleNamespace(owner_agent_pool_max_agents=128))

    registry = shared_active_owner_registry(agent)

    assert registry._max_owners == 128


# ---------- 硬/软分层准入(F 批:215 个 curator 软活测试号灌满池,真活 wake 硬 owner 被逐出饿死) ----------


def test_registry_soft_rejected_when_full_of_hard(tmp_path) -> None:
    registry = ActiveOwnerRegistry(max_owners=2)
    registry.record(OwnerIdentity.provider_user("feishu", "h1"))
    registry.record(OwnerIdentity.provider_user("feishu", "h2"))

    accepted = registry.record(OwnerIdentity.provider_user("feishu", "s1"), hard=False)

    assert accepted is False  # 软活等下一轮,绝不挤硬
    assert {o.owner_id for o in registry.snapshot()} == {"h1", "h2"}


def test_registry_hard_evicts_soft_not_hard(tmp_path) -> None:
    registry = ActiveOwnerRegistry(max_owners=2)
    registry.record(OwnerIdentity.provider_user("feishu", "s1"), hard=False)
    registry.record(OwnerIdentity.provider_user("feishu", "h1"))
    registry.record(OwnerIdentity.provider_user("feishu", "h2"))  # 满:应逐软 s1 而非硬 h1

    ids = {o.owner_id for o in registry.snapshot()}
    assert ids == {"h1", "h2"}


def test_registry_soft_upgrades_to_hard(tmp_path) -> None:
    registry = ActiveOwnerRegistry(max_owners=2)
    owner = OwnerIdentity.provider_user("feishu", "s1")
    registry.record(owner, hard=False)

    registry.record(owner, hard=True)  # 软 owner 出现硬事实(如新入站请求)→ 升级

    assert {o.owner_id for o in registry.hard_snapshot()} == {"s1"}
    assert registry.soft_snapshot() == []


def test_registry_soft_hit_does_not_downgrade_hard(tmp_path) -> None:
    registry = ActiveOwnerRegistry(max_owners=2)
    owner = OwnerIdentity.provider_user("feishu", "h1")
    registry.record(owner)

    registry.record(owner, hard=False)  # 已有硬事实 → 软登记不降级

    assert {o.owner_id for o in registry.hard_snapshot()} == {"h1"}
    assert registry.soft_snapshot() == []


def test_registry_snapshots_hard_before_soft(tmp_path) -> None:
    registry = ActiveOwnerRegistry(max_owners=8)
    registry.record(OwnerIdentity.provider_user("feishu", "s1"), hard=False)
    registry.record(OwnerIdentity.provider_user("feishu", "h1"))
    registry.record(OwnerIdentity.provider_user("feishu", "s2"), hard=False)
    registry.record(OwnerIdentity.provider_user("feishu", "h2"))

    ids = [o.owner_id for o in registry.snapshot()]
    assert ids == ["h1", "h2", "s1", "s2"]  # 硬先软后(后台循环硬 owner 先建 scheduler)


def test_pool_hard_get_evicts_soft_not_hard(tmp_path) -> None:
    pool, builds = _counting_pool(tmp_path, max_agents=2)
    pool.get(OwnerIdentity.provider_user("feishu", "s1"), hard=False)
    pool.get(OwnerIdentity.provider_user("feishu", "h1"), hard=True)
    pool.get(OwnerIdentity.provider_user("feishu", "h2"), hard=True)  # 满:逐软 s1

    ids = {a.owner_id for a in pool.active_agents()}
    assert ids == {"h1", "h2"}
    assert len(builds) == 3


def test_pool_soft_get_rejected_when_full_of_hard(tmp_path) -> None:
    pool, builds = _counting_pool(tmp_path, max_agents=2)
    pool.get(OwnerIdentity.provider_user("feishu", "h1"))
    pool.get(OwnerIdentity.provider_user("feishu", "h2"))

    result = pool.get(OwnerIdentity.provider_user("feishu", "s1"), hard=False)

    assert result is None  # 软活等下一轮,不挤硬
    assert pool.active_count() == 2
    assert len(builds) == 2


def test_pool_soft_evicts_soft_among_soft(tmp_path) -> None:
    pool, builds = _counting_pool(tmp_path, max_agents=2)
    pool.get(OwnerIdentity.provider_user("feishu", "s1"), hard=False)
    pool.get(OwnerIdentity.provider_user("feishu", "s2"), hard=False)
    pool.get(OwnerIdentity.provider_user("feishu", "s3"), hard=False)  # 软表满:软内互挤最久(s1)

    ids = {a.owner_id for a in pool.active_agents()}
    assert ids == {"s2", "s3"}


def test_pool_soft_hit_upgrades_to_hard_same_instance(tmp_path) -> None:
    pool, builds = _counting_pool(tmp_path, max_agents=2)
    owner = OwnerIdentity.provider_user("feishu", "s1")
    soft = pool.get(owner, hard=False)

    hard = pool.get(owner, hard=True)  # 升级:同一 agent 实例,不重建

    assert hard is soft
    assert len(builds) == 1
    assert {a.owner_id for a in pool.active_agents()} == {"s1"}


def test_pool_active_agents_lists_hard_before_soft(tmp_path) -> None:
    pool, _ = _counting_pool(tmp_path, max_agents=8)
    pool.get(OwnerIdentity.provider_user("feishu", "s1"), hard=False)
    pool.get(OwnerIdentity.provider_user("feishu", "h1"))
    pool.get(OwnerIdentity.provider_user("feishu", "s2"), hard=False)
    pool.get(OwnerIdentity.provider_user("feishu", "h2"))

    assert [a.owner_id for a in pool.active_agents()] == ["h1", "h2", "s1", "s2"]


def test_pool_hard_agents_excludes_curator_only_soft_instances(tmp_path) -> None:
    pool, _ = _counting_pool(tmp_path, max_agents=8)
    pool.get(OwnerIdentity.provider_user("feishu", "soft"), hard=False)
    hard = pool.get(OwnerIdentity.provider_user("feishu", "hard"), hard=True)

    assert pool.hard_agents() == [hard]
    assert pool.active_count() == 2


def test_pool_soft_eviction_preserves_concurrent_hard_promotion(tmp_path) -> None:
    pool, _ = _counting_pool(tmp_path, max_agents=8)
    owner = OwnerIdentity.provider_user("feishu", "curator-owner")
    soft = pool.get(owner, hard=False)

    assert pool.evict_soft_agent_id(id(soft)) is True
    assert pool.active_count() == 0

    promoted = pool.get(owner, hard=False)
    assert pool.get(owner, hard=True) is promoted
    assert pool.evict_soft_agent_id(id(promoted)) is False
    assert pool.hard_agents() == [promoted]


# ---------- config owner 覆盖 ----------

def test_config_with_owner_overrides_owner_keeps_rest(tmp_path) -> None:
    base = AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"))
    scoped = _config_with_owner(base, OwnerIdentity.provider_user("feishu", "alice"))
    assert scoped.my_agent_owner_provider == "feishu"
    assert scoped.my_agent_owner_kind == "user"
    assert scoped.my_agent_owner_id == "alice"
    assert scoped.model_backend == "echo"  # 其余配置继承
    assert base.my_agent_owner_id == "main"  # 原 config 不被改(replace 返回新实例)


# LLM: 真实JSONL读取/写后刷新共用缓存，缩小预算只为命中边界，不修改正文或放宽返回断言。
# 函数用途: 验证超大历史文件仍可解析但不留在进程缓存，包括清掉旧的小版本快照。
@pytest.mark.parametrize('entry', ['read', 'write'])
def test_opt7_history_oversize_is_returned_without_retention(tmp_path, monkeypatch, entry):
    from agent_py_agent.agent.gateway_parts import io

    monkeypatch.setattr(io, '_GATEWAY_HISTORY_CACHE', {})
    monkeypatch.setattr(io, '_MAX_GATEWAY_HISTORY_CACHE_FILE_BYTES', 80, raising=False)
    path = tmp_path / 'history.jsonl'
    row = {'id': 'one', 'content': 'small'}
    path.write_text(json.dumps(row) + '\n')
    io._gateway_history_snapshot(path)
    assert str(path.resolve()) in io._GATEWAY_HISTORY_CACHE
    row['content'] = '正文' * 100
    path.write_text(json.dumps(row, ensure_ascii=False) + '\n', encoding='utf-8')
    lines, index = io._gateway_history_snapshot(path)
    if entry == 'write':
        io._store_gateway_history_cache(path, lines, index)
    assert index['one'].payload == row and json.loads(lines[0]) == row
    assert str(path.resolve()) not in io._GATEWAY_HISTORY_CACHE


# LLM: 每个文件单独不超限，总预算取实际stat字节，两条边界可保留，第三条淘汰最早项。
# 函数用途: 验证历史冷读和写后刷新均受总字节预算约束而非只有条数上限。
@pytest.mark.parametrize('entry', ['read', 'write'])
def test_opt7_history_total_budget_evicts_oldest(tmp_path, monkeypatch, entry):
    from agent_py_agent.agent.gateway_parts import io

    monkeypatch.setattr(io, '_GATEWAY_HISTORY_CACHE', {})
    paths = [tmp_path / f'{number}.jsonl' for number in range(3)]
    body = json.dumps({'id': 'one', 'content': 'x' * 20}) + '\n'
    for path in paths:
        path.write_text(body)
    size = paths[0].stat().st_size
    monkeypatch.setattr(io, '_MAX_GATEWAY_HISTORY_CACHE_TOTAL_BYTES', size * 2, raising=False)
    for path in paths:
        if entry == 'read':
            io._gateway_history_snapshot(path)
        else:
            io._store_gateway_history_cache(path, [body.rstrip('\n')], {})
    assert list(io._GATEWAY_HISTORY_CACHE) == [str(path.resolve()) for path in paths[1:]]
    assert sum(row[0][1] for row in io._GATEWAY_HISTORY_CACHE.values()) == size * 2


# LLM: 使用真实stat和JSON解析，只替代领域对象构造；固定本地缓存时钟避免粗mtime窗口隐藏大小断言。
# 函数用途: 验证run超限仍每次返回解析副本，小版本缓存不残留且256KiB上限看size而非inode。
def test_opt7_run_large_item_bypasses_cache_and_drops_old_entry(tmp_path, monkeypatch):
    from agent_py_agent.agent.subagents.services.persistence import service as module

    service = module.SubAgentPersistenceService(SimpleNamespace(workspace=tmp_path))
    path = tmp_path / 'task.json'
    path.write_text(json.dumps({'id': 'one', 'result': 'small'}))
    monkeypatch.setattr(module, '_now', lambda: path.stat().st_mtime + 10)
    monkeypatch.setattr(service, '_MAX_RUN_CACHE_ITEM_BYTES', 80, raising=False)
    reads = []

    # LLM: 每次实际读JSON，计数仅观察缓存是否避开读取，不伪造stat或cache命中。
    # 函数用途: 解析本测试的权威文件并返回独立对象。
    def load(_run_id):
        reads.append(True)
        return SimpleNamespace(**json.loads(path.read_text()))

    monkeypatch.setattr(service, 'load', load)
    assert service._cached_run_copy('one', path).result == 'small'
    assert 'one' in service._run_cache
    path.write_text(json.dumps({'id': 'one', 'result': '大' * 100}, ensure_ascii=False), encoding='utf-8')
    first = service._cached_run_copy('one', path)
    second = service._cached_run_copy('one', path)
    assert first.result == second.result == '大' * 100 and first is not second
    assert len(reads) == 3 and 'one' not in service._run_cache


# LLM: 负缓存现实现已有条数限制，本测试改变独立上限确保新增常量真正被使用，不降低正缓存容量。
# 函数用途: 验证缺失canonical缓存单独限制为512条的合同及FIFO淘汰。
def test_opt7_negative_run_cache_has_independent_entry_budget(tmp_path, monkeypatch):
    from agent_py_agent.agent.common.cache_freshness import cache_stat_signature
    from agent_py_agent.agent.subagents.services.persistence import service as module

    service = module.SubAgentPersistenceService(SimpleNamespace(workspace=tmp_path))
    monkeypatch.setattr(service, '_MAX_NEGATIVE_RUN_CACHE_ENTRIES', 2, raising=False)
    monkeypatch.setattr(service, '_MAX_RUN_CACHE_ENTRIES', 100)
    for number in range(3):
        path = tmp_path / f'locator-{number}.json'
        path.write_text(json.dumps({'task_workspace_dir': str(tmp_path),
            'agent_run_workspace_dir': str(tmp_path / 'work' / 'agents' / f'run-{number}')}))
        service._cache_missing_canonical(str(number), path, cache_stat_signature(path.stat()))
    assert list(service._missing_canonical_cache) == ['1', '2']


# LLM: 真实线程解析及LRU读取，字节数取UTF8文件stat；不以缓存项数或对象存在冒充字节预算。
# 函数用途: 验证线程总量淘汰最久未用项，重复替换与深拷贝仍正确。
def test_opt7_thread_cache_total_budget_preserves_lru_and_copy(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import store_threads as module
    from agent_py_agent.agent.conversation.models import ConversationThread

    monkeypatch.setattr(module, '_THREAD_RECORD_CACHE', OrderedDict())
    paths = [tmp_path / f'{number}.json' for number in range(3)]
    for number, path in enumerate(paths):
        row = ConversationThread(thread_id=str(number), canonical_user_id='owner', metadata={'text': '字' * 10})
        path.write_text(json.dumps(row.to_dict(), ensure_ascii=False), encoding='utf-8')
    size = paths[0].stat().st_size
    monkeypatch.setattr(module, '_THREAD_CACHE_MAX_TOTAL_BYTES', size * 2, raising=False)
    for path in paths[:2]:
        row, error = module._read_thread_record_report(path)
        assert error is None and row.thread_id == path.stem
    module._read_thread_record_report(paths[0])
    row, error = module._read_thread_record_report(paths[2])
    assert error is None and row.thread_id == '2'
    assert list(module._THREAD_RECORD_CACHE) == [str(paths[0]), str(paths[2])]
    row.metadata['text'] = 'caller mutation'
    again, error = module._read_thread_record_report(paths[2])
    assert error is None and again.metadata['text'] == '字' * 10
    cached = module._THREAD_RECORD_CACHE[str(paths[2])]
    assert cached.size_bytes == paths[2].stat().st_size


_OPT7_MCP_EVICTIONS = (
    ('idle', True), ('hard_lru', True), ('soft_lru', False),
    ('hard_replaces_soft', False), ('soft_id', False),
)


# LLM: 真实池入口与锁，不起进程；回调记录锁可获取性而不在会被产品吞错的回调里断言。
# 函数用途: 构造可观察MCP关闭次数、身份和锁外调用事实的测试池。
def _opt7_mcp_pool(tmp_path, *, close_raises=False):
    pool, _ = _counting_pool(tmp_path, max_agents=1)
    closed = []

    # LLM: 仅替代重量级Agent初始化，每次独立返回对象，保留原get/_store及pin生命周期。
    # 函数用途: 给新owner附加可观察的MCP关闭回调。
    def build(_base, _root, owner, _workspace_roots):
        agent = SimpleNamespace(owner_id=owner.owner_id)

        # LLM: 不阻塞拿锁，不抛断言；失败事实交测试主体检查，模拟异常仍必须记录调用。
        # 函数用途: 记录关闭目标及调用时池锁已释放，并可模拟清理失败。
        def close():
            unlocked = pool._lock.acquire(blocking=False)
            if unlocked:
                pool._lock.release()
            closed.append((agent, unlocked))
            if close_raises:
                raise RuntimeError('MCP close failed')

        agent.tools = SimpleNamespace(close_mcp_clients=close)
        return agent

    pool._builder = build
    return pool, closed


# LLM: 每条路线使用原公开回收入口；LRU仍由真实get触发，不手改池内部容量或缓存引用。
# 函数用途: 统一触发空闲、硬软LRU和软实例ID淘汰，并核对原对象确实离池。
def _opt7_evict_mcp_agent(pool, agent, route):
    owner = OwnerIdentity.provider_user('feishu', agent.owner_id)
    key = (owner.provider, owner.owner_kind, owner.owner_id)
    if route == 'idle':
        assert pool.evict_idle(key, agent, pool._last_used[key]) is True
    elif route == 'soft_id':
        assert pool.evict_soft_agent_id(id(agent)) is True
    else:
        pool.get(OwnerIdentity.provider_user('feishu', 'replacement'), hard=route != 'soft_lru')
    assert pool.peek(owner) is None


# LLM: 原淘汰和新MCP关闭一起观察；清理异常不能改变淘汰结果，锁外事实在主体断言。
# 函数用途: 验证所有无pin淘汰分支仅关闭被释放实例一次且不持池锁。
@pytest.mark.parametrize('route,hard', _OPT7_MCP_EVICTIONS)
@pytest.mark.parametrize('close_raises', [False, True])
def test_opt7_pool_mcp_unpinned_eviction_closes_outside_lock(tmp_path, route, hard, close_raises):
    pool, closed = _opt7_mcp_pool(tmp_path, close_raises=close_raises)
    agent = pool.get(OwnerIdentity.provider_user('feishu', 'victim'), hard=hard)
    _opt7_evict_mcp_agent(pool, agent, route)
    assert len(closed) == 1 and closed[0][0] is agent and closed[0][1] is True
    assert not getattr(pool, '_retired_agents', {})


# LLM: Full pinned pools backpressure LRU replacement; explicit soft retirement still waits for the last nested lease before closing.
# 函数用途: 验证满池不以在途淘汰制造空位，显式退役仍在最后释放后锁外清理。
@pytest.mark.parametrize('route,hard', _OPT7_MCP_EVICTIONS[1:])
def test_opt7_pool_mcp_pinned_eviction_waits_for_last_release(tmp_path, route, hard):
    pool, closed = _opt7_mcp_pool(tmp_path)
    agent = pool.get(OwnerIdentity.provider_user('feishu', 'victim'), hard=hard)
    with pool.pin(agent):
        with pool.pin(agent, touch=False):
            if route == 'soft_id':
                _opt7_evict_mcp_agent(pool, agent, route)
                assert pool._retired_agents.get(id(agent)) is agent
            elif route == 'soft_lru':
                assert pool.get(OwnerIdentity.provider_user('feishu', 'replacement'), hard=False) is None
            else:
                with pytest.raises(PoolCapacityExceededError):
                    _opt7_evict_mcp_agent(pool, agent, route)
            assert closed == []
            assert pool.gauges()['total_live_instances'] == 1
        assert closed == [] and pool._pins[id(agent)] == 1
    if route != 'soft_id':
        assert closed == []
        _opt7_evict_mcp_agent(pool, agent, 'idle' if hard else 'soft_id')
    assert len(closed) == 1 and closed[0][0] is agent and closed[0][1] is True
    assert not pool._pins and not pool._retired_agents


# LLM: 保留原空闲CAS和pin保护，不为了覆盖关闭而放宽evict_idle的既有拒绝条件。
# 函数用途: 验证活跃租用不能空闲回收，释放后按新触摸版本才能回收并关闭。
def test_opt7_pool_mcp_idle_keeps_pinned_agent(tmp_path):
    pool, closed = _opt7_mcp_pool(tmp_path)
    owner = OwnerIdentity.provider_user('feishu', 'victim')
    agent = pool.get(owner)
    key = (owner.provider, owner.owner_kind, owner.owner_id)
    touched = pool._last_used[key]
    with pool.pin(agent):
        assert pool.evict_idle(key, agent, touched) is False
        assert pool.peek(owner) is agent and closed == []
    assert closed == []
    _opt7_evict_mcp_agent(pool, agent, 'idle')
    assert len(closed) == 1 and closed[0] == (agent, True)


# LLM: 同对象仍在另一张表有引用时不能关闭；按身份而非owner键或相等性决定退役。
# 函数用途: 验证软引用移除不会关闭仍被硬表持有的实例。
def test_opt7_pool_mcp_shared_pool_reference_prevents_early_close(tmp_path):
    pool, closed = _opt7_mcp_pool(tmp_path)
    pool._max_agents = 2
    agent = pool.get(OwnerIdentity.provider_user('feishu', 'victim'), hard=False)
    alias = ('feishu', 'user', 'alias')
    assert pool._store(alias, agent, hard=True) is agent
    assert pool.evict_soft_agent_id(id(agent)) is True
    assert pool.active_count() == 1 and closed == []
    assert pool.evict_idle(alias, agent, pool._last_used[alias]) is True
    assert len(closed) == 1 and closed[0] == (agent, True)


# LLM: _store双检和准入拒绝返回原结果，但新建且未采用的Agent同样不可遗留MCP资源。
# 函数用途: 验证重复构建或软准入失败只关闭未采用实例，不关闭已缓存实例。
@pytest.mark.parametrize('duplicate', [True, False])
def test_opt7_pool_mcp_store_closes_unused_candidate(tmp_path, duplicate):
    pool, closed = _opt7_mcp_pool(tmp_path)
    owner = OwnerIdentity.provider_user('feishu', 'cached')
    cached = pool.get(owner)
    candidate = pool._builder(None, None, OwnerIdentity.provider_user('feishu', 'candidate'), None)
    key = ('feishu', 'user', 'cached' if duplicate else 'rejected')
    result = pool._store(key, candidate, hard=duplicate)
    assert result is (cached if duplicate else None)
    assert pool.peek(owner) is cached
    assert len(closed) == 1 and closed[0][0] is candidate and closed[0][1] is True
    assert pool._store(('feishu', 'user', 'cached'), cached, hard=True) is cached
    assert len(closed) == 1


# LLM: 先完成替换再决定旧对象是否仍被引用；同对象换键不等于退役。
# 函数用途: 验证容量淘汰后原对象又成为新条目时不关闭仍在池的MCP。
def test_opt7_pool_mcp_same_object_reinserted_by_store_stays_open(tmp_path):
    pool, closed = _opt7_mcp_pool(tmp_path)
    agent = pool.get(OwnerIdentity.provider_user('feishu', 'victim'))
    assert pool._store(('feishu', 'user', 'replacement'), agent, hard=True) is agent
    assert pool.active_agents() == [agent] and closed == []


# LLM: 业务异常传播不受清理异常影响，contextmanager finally仍必须释放最后pin及退休强引用。
# 函数用途: 验证异常退出时关闭失败不会吞业务错误或遗留退休对象。
def test_opt7_pool_mcp_pin_exception_still_retires(tmp_path):
    pool, closed = _opt7_mcp_pool(tmp_path, close_raises=True)
    agent = pool.get(OwnerIdentity.provider_user('feishu', 'victim'), hard=False)
    with pytest.raises(ValueError, match='business error'):
        with pool.pin(agent):
            _opt7_evict_mcp_agent(pool, agent, 'soft_id')
            assert closed == []
            raise ValueError('business error')
    assert len(closed) == 1 and closed[0] == (agent, True)
    assert not pool._pins and not getattr(pool, '_retired_agents', {})


# LLM: Count all live objects before invoking the builder, including explicit soft retirement and reservations.
# 函数用途: 验证退役但仍在途的实例占满容量，新owner不能先构造后拒绝。
def test_retired_pinned_instance_blocks_new_build_until_release(tmp_path):
    pool, builds = _counting_pool(tmp_path, max_agents=1)
    first = pool.get(OwnerIdentity.provider_user('test', 'a'), hard=False)
    with pool.pin(first):
        assert pool.evict_soft_agent_id(id(first))
        with pytest.raises(RuntimeError, match='capacity'):
            pool.get(OwnerIdentity.provider_user('test', 'b'))
        assert len(builds) == 1
        assert pool.gauges()['total_live_instances'] == 1
    assert pool.gauges()['total_live_instances'] == 0
    assert pool.get(OwnerIdentity.provider_user('test', 'b')) is not first


# LLM: A concurrent build holds a real budget slot even before its result is published to the cache.
# 函数用途: 验证并发不同owner的首次构造不能突破单实例上限。
def test_building_reservation_counts_toward_live_capacity(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    pool, _ = _counting_pool(tmp_path, max_agents=1)
    building, proceed = threading.Event(), threading.Event()

    # LLM: Delay the actual construction result without holding any pool mutex.
    # 函数用途: 固定第一个owner正在构造的交错。
    def build(_base, _root, owner, _roots):
        if owner.owner_id == 'a':
            building.set()
            assert proceed.wait(10)
        return SimpleNamespace(owner_id=owner.owner_id)

    pool._builder = build
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(pool.get, OwnerIdentity.provider_user('test', 'a'))
        try:
            assert building.wait(10)
            with pytest.raises(RuntimeError, match='capacity'):
                pool.get(OwnerIdentity.provider_user('test', 'b'))
            assert pool.gauges()['total_live_instances'] == 1
        finally:
            proceed.set()
        assert future.result(timeout=10).owner_id == 'a'


# LLM: Force reload may defer replacement by reusing the pinned owner; it must not accumulate retired generations.
# 函数用途: 验证单容量池反复强制刷新仍只保留一个在途实例。
def test_force_reload_reuses_pinned_generation_at_capacity(tmp_path):
    import inspect
    from contextlib import ExitStack

    pool, builds = _counting_pool(tmp_path, max_agents=1)
    assert 'force_reload' in inspect.signature(pool.get).parameters, 'force_reload capacity contract missing'
    owner = OwnerIdentity.provider_user('test', 'a')
    first = pool.get(owner)
    with ExitStack() as leases:
        leases.enter_context(pool.pin(first))
        for _ in range(4):
            current = pool.get(owner, force_reload=True)
            leases.enter_context(pool.pin(current))
            assert current is first
            assert pool.gauges()['total_live_instances'] <= pool._max_agents
        assert len(builds) == 1
    assert pool.get(owner, force_reload=True) is not first
