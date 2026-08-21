from __future__ import annotations

from types import SimpleNamespace

# 防回归(T4·并发公平层): _BACKGROUND_OWNER_WORKERS=8 与 OwnerScopedAgentPool
# max_agents=64 曾是硬编码,千并发调参无入口。钉子:两值可由 config 覆盖,
# 非法/缺省值回落原默认,显式入参优先。
from agent_py_agent.agent.owner_scoped_pool import OwnerScopedAgentPool
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.cli.gateway_loops import (
    _background_owner_workers,
    _background_threads_per_owner,
)


def test_background_owner_workers_from_config():
    agent = SimpleNamespace(config=SimpleNamespace(background_owner_workers=24))
    assert _background_owner_workers(agent) == 24


def test_background_owner_workers_fallback_on_invalid():
    assert _background_owner_workers(SimpleNamespace(config=SimpleNamespace(background_owner_workers="x"))) == 8
    assert _background_owner_workers(SimpleNamespace(config=SimpleNamespace(background_owner_workers=0))) == 8
    assert _background_owner_workers(SimpleNamespace()) == 8


def test_background_threads_per_owner_from_config():
    agent = SimpleNamespace(config=SimpleNamespace(background_threads_per_owner=6))
    assert _background_threads_per_owner(agent) == 6


def test_background_threads_per_owner_fallback_on_invalid():
    assert _background_threads_per_owner(
        SimpleNamespace(config=SimpleNamespace(background_threads_per_owner="x"))
    ) == 4
    assert _background_threads_per_owner(
        SimpleNamespace(config=SimpleNamespace(background_threads_per_owner=0))
    ) == 4
    assert _background_threads_per_owner(SimpleNamespace()) == 4


def test_owner_pool_max_agents_from_config():
    config = SimpleNamespace(owner_agent_pool_max_agents=128)
    pool = OwnerScopedAgentPool(config, root=".")
    assert pool._max_agents == 128


def test_owner_pool_explicit_param_beats_config():
    config = SimpleNamespace(owner_agent_pool_max_agents=128)
    pool = OwnerScopedAgentPool(config, root=".", max_agents=4)
    assert pool._max_agents == 4


def test_owner_pool_fallback_on_missing_or_invalid():
    assert OwnerScopedAgentPool(SimpleNamespace(), root=".")._max_agents == 64
    assert OwnerScopedAgentPool(SimpleNamespace(owner_agent_pool_max_agents="junk"), root=".")._max_agents == 64
    assert OwnerScopedAgentPool(SimpleNamespace(owner_agent_pool_max_agents=-1), root=".")._max_agents == 64


def test_agent_config_declares_concurrency_fields():
    config = AgentConfig()
    assert config.background_owner_workers == 8
    assert config.background_threads_per_owner == 4
    assert config.owner_agent_pool_max_agents == 64
