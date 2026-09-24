from types import SimpleNamespace

from agent_py_agent.agent.gateway_parts.owner_retention import release_idle_owner_agents
from agent_py_agent.agent.owner_scoped_pool import ActiveOwnerRegistry, OwnerScopedAgentPool
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity


# LLM: 只用临时目录和可控单调时间；池/登记本/释放路径用产品实现，持久事实单独替身。
# 函数用途: 构造一个已空闲的用户，检查恢复、新消息及在途保护。
def _idle_owner(tmp_path, monkeypatch):
    from agent_py_agent.agent import owner_scoped_pool as module
    from agent_py_agent.agent import owner_wake_discovery

    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(owner_wake_discovery, "_owner_has_hard_facts", lambda _home: False)
    config = AgentConfig(owner_agent_idle_seconds=60)
    pool = OwnerScopedAgentPool(config, tmp_path)
    pool._builder = lambda *_args: SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=tmp_path))
    owner = OwnerIdentity.provider_user("test", "user")
    registry = ActiveOwnerRegistry()
    registry.record(owner)
    agent = pool.get(owner)
    supervisor = SimpleNamespace(
        _base_agent=SimpleNamespace(config=config), _owner_pool=pool, _registry=registry,
        _active_threads_for_owner=lambda _identity: 0, _curator_inflight={},
        _owner_schedulers={id(agent): object()},
    )
    clock[0] = 161
    return supervisor, pool, owner, agent, clock


def test_idle_agent_releases_and_new_message_rebuilds(tmp_path, monkeypatch):
    supervisor, pool, owner, agent, _ = _idle_owner(tmp_path, monkeypatch)
    assert release_idle_owner_agents(supervisor) == 1
    assert pool.active_count() == 0 and supervisor._registry.snapshot() == []
    assert not supervisor._owner_schedulers
    assert pool.get(owner) is not agent


def test_inflight_lease_and_durable_work_protect_idle_agent(tmp_path, monkeypatch):
    from agent_py_agent.agent import owner_wake_discovery

    supervisor, pool, owner, agent, _ = _idle_owner(tmp_path, monkeypatch)
    with pool.pin(agent, touch=False):
        assert release_idle_owner_agents(supervisor) == 0
    monkeypatch.setattr(owner_wake_discovery, "_owner_has_hard_facts", lambda _home: True)
    assert release_idle_owner_agents(supervisor) == 0
    assert pool.peek(owner) is agent


def test_polling_does_not_extend_idle_but_new_message_does(tmp_path, monkeypatch):
    supervisor, pool, owner, agent, clock = _idle_owner(tmp_path, monkeypatch)
    assert pool.peek(owner) is agent
    assert pool.get(owner, touch=False) is agent
    assert len(pool.idle_candidates(60)) == 1
    pool.get(owner)
    assert release_idle_owner_agents(supervisor) == 0
    clock[0] += 61
    assert release_idle_owner_agents(supervisor) == 1


def test_concurrent_get_cannot_be_evicted_by_stale_observation(tmp_path, monkeypatch):
    from agent_py_agent.agent import owner_wake_discovery

    supervisor, pool, owner, agent, clock = _idle_owner(tmp_path, monkeypatch)

    def check(_home):
        clock[0] += 1
        supervisor._registry.record(owner)
        pool.get(owner)
        return False

    monkeypatch.setattr(owner_wake_discovery, "_owner_has_hard_facts", check)
    assert release_idle_owner_agents(supervisor) == 0
    assert pool.peek(owner) is agent and supervisor._registry.snapshot() == [owner]


def test_idle_lifetime_config_accepts_zero_and_rejects_invalid_values():
    from agent_py_agent.agent.settings.normalize import normalize_agent_config

    value, warnings = normalize_agent_config({'owner_agent_idle_seconds': '0'})
    assert value['owner_agent_idle_seconds'] == 0 and not warnings
    for invalid in (-1, 'bad'):
        value, warnings = normalize_agent_config({'owner_agent_idle_seconds': invalid})
        assert value['owner_agent_idle_seconds'] == AgentConfig().owner_agent_idle_seconds
        assert warnings


def test_disabled_curator_does_not_build_idle_owners_and_releases_finished_instances(monkeypatch):
    from agent_py_agent.cli import gateway_loops

    supervisor = object.__new__(gateway_loops._BackgroundMainSupervisor)
    supervisor._base_agent = SimpleNamespace(config=SimpleNamespace(memory_curator_enabled=False))
    released = []
    supervisor._owner_pool = SimpleNamespace(evict_soft_agent_id=lambda key: released.append(key))
    supervisor._curator_inflight = {123: SimpleNamespace(done=lambda: True)}
    supervisor._curator_soft_agent_ids = {123}
    supervisor._next_curator_run_at = 0
    monkeypatch.setattr(gateway_loops, '_curator_candidates', lambda *_: (_ for _ in ()).throw(AssertionError('disabled')))
    supervisor._run_due_curators()
    assert released == [123] and not supervisor._curator_inflight


def test_finished_curator_releases_before_next_dispatch_interval():
    from agent_py_agent.cli import gateway_loops

    supervisor = object.__new__(gateway_loops._BackgroundMainSupervisor)
    supervisor._base_agent = SimpleNamespace(config=SimpleNamespace(memory_curator_enabled=True))
    released = []
    supervisor._owner_pool = SimpleNamespace(evict_soft_agent_id=lambda key: released.append(key))
    supervisor._curator_inflight = {123: SimpleNamespace(done=lambda: True)}
    supervisor._curator_soft_agent_ids = {123}
    supervisor._next_curator_run_at = float('inf')
    supervisor._run_due_curators()
    assert released == [123] and not supervisor._curator_inflight
