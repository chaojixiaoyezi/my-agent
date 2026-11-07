"""缓存容量的配置推导、保守下限和池构造接线合同。"""

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common import cache_capacity
from agent_py_agent.agent.owner_scoped_pool import OwnerScopedAgentPool
from agent_py_agent.agent.settings.config import AgentConfig


@pytest.fixture(autouse=True)
def _reset_capacity():
    cache_capacity.reset_for_tests()
    try:
        yield
    finally:
        cache_capacity.reset_for_tests()


@pytest.fixture
def config():
    return SimpleNamespace(
        owner_agent_pool_max_agents=128,
        gateway_owner_inflight_limit=0,
        gateway_user_inflight_limit=128,
        background_threads_per_owner=2,
        background_admin_threads_per_owner=64,
        max_subagents=8,
        gateway_global_inflight_limit=500,
    )


def _capacities(floor=0):
    return (
        cache_capacity.resident_owners(floor),
        cache_capacity.owner_sessions(floor),
        cache_capacity.owner_agents(floor),
        cache_capacity.global_inflight(floor),
    )


def test_unconfigured_capacity_preserves_each_floor():
    assert _capacities(7) == (7, 7, 7, 7)
    assert cache_capacity.owners_scaled(7) == 7


def test_default_configuration_derives_capacity_without_mutating_config(config):
    original = vars(config).copy()
    cache_capacity.configure_from_config(config)

    assert _capacities(1) == (128, 192, 1728, 500)
    assert cache_capacity.owners_scaled(2) == 256
    assert vars(config) == original


@pytest.mark.parametrize("limit", [3, 300])
def test_explicit_owner_limit_takes_precedence_over_pool_capacity(config, limit):
    config.gateway_owner_inflight_limit = limit
    cache_capacity.configure_from_config(config)

    assert cache_capacity.resident_owners(1) == limit
    assert cache_capacity.owners_scaled(2) == limit * 2


def test_configured_capacity_still_respects_larger_floor(config):
    cache_capacity.configure_from_config(config)

    assert _capacities(2000) == (2000, 2000, 2000, 2000)


@pytest.mark.parametrize("bad_value", [None, "x", -5, object()])
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("owner_agent_pool_max_agents", (0, 192, 1728, 500)),
        ("gateway_owner_inflight_limit", (128, 192, 1728, 500)),
        ("gateway_user_inflight_limit", (128, 64, 576, 500)),
        ("background_threads_per_owner", (128, 192, 1728, 500)),
        ("background_admin_threads_per_owner", (128, 130, 1170, 500)),
        ("max_subagents", (128, 192, 192, 500)),
        ("gateway_global_inflight_limit", (128, 192, 1728, 0)),
    ],
)
def test_bad_config_values_are_treated_as_zero(config, name, expected, bad_value):
    setattr(config, name, bad_value)
    cache_capacity.configure_from_config(config)

    assert _capacities() == expected


def test_missing_config_fields_preserve_floors():
    cache_capacity.configure_from_config(SimpleNamespace())

    assert _capacities(7) == (7, 7, 7, 7)
    assert cache_capacity.owners_scaled(7) == 7


def test_numeric_strings_are_accepted(config):
    config.gateway_owner_inflight_limit = "300"
    config.gateway_user_inflight_limit = "50"
    cache_capacity.configure_from_config(config)

    assert _capacities() == (300, 114, 1026, 500)


def test_background_sessions_use_the_larger_configured_lane(config):
    config.background_threads_per_owner = 80
    cache_capacity.configure_from_config(config)

    assert cache_capacity.owner_sessions(1) == 208
    assert cache_capacity.owner_agents(1) == 1872


def test_zero_subagents_keeps_only_main_agent_slots(config):
    config.max_subagents = 0
    cache_capacity.configure_from_config(config)

    assert cache_capacity.owner_agents(1) == 192


def test_reconfiguration_replaces_all_previous_slots(config):
    cache_capacity.configure_from_config(config)
    cache_capacity.configure_from_config(SimpleNamespace(owner_agent_pool_max_agents=4))

    assert _capacities(1) == (4, 1, 1, 1)
    assert cache_capacity.owners_scaled(2) == 8


def test_reset_restores_unconfigured_floors(config):
    cache_capacity.configure_from_config(config)
    cache_capacity.reset_for_tests()

    assert _capacities(7) == (7, 7, 7, 7)
    assert cache_capacity.owners_scaled(7) == 7


@pytest.mark.parametrize(("owner_limit", "expected"), [(0, 17), (300, 300)])
def test_pool_construction_registers_config_capacity(tmp_path, owner_limit, expected):
    config = AgentConfig(
        model_backend="echo",
        my_agent_home=str(tmp_path / "home"),
        owner_agent_pool_max_agents=17,
        gateway_owner_inflight_limit=owner_limit,
    )
    OwnerScopedAgentPool(config, tmp_path, max_agents=2)

    assert cache_capacity.resident_owners(1) == expected
