"""Stage 2 配置接线：真实加载、权限写入口、范围与终态热切换；不替代未合入切片的验收。"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.settings import parameter_changes as changes
from agent_py_agent.agent.settings.config import AgentConfig, load_config, normalize_agent_config
from agent_py_agent.agent.settings.live_switches import (
    DEPLOY_LIVE_SWITCH_KEYS,
    LIVE_SWITCH_KEYS,
    refresh_live_switches,
    register_live_config,
)
from agent_py_agent.agent.settings.parameter_metadata import ranges
from agent_py_agent.agent.settings.parameter_registry import (
    DEPLOYMENT_ONLY_KEYS,
    applied_value,
    parameter_registry,
)
from agent_py_agent.agent.settings.user_config_capability import (
    EFFECT_IMMEDIATE,
    TUNABLE_KEYS,
    packaged_config_path,
)
from agent_py_agent.agent.user_space import owner_epoch as epoch


@pytest.mark.parametrize("key,default,range_text", [
    ("gateway_event_hub_ring_events", 0, "0-65536"),
    ("gateway_event_fallback_poll_seconds", 0.0, "0-60"),
    ("conversation_actor_workers", 0, "0-64"),
    ("owner_epoch_fence_enabled", False, "true|false"),
    ("background_owner_wake_rescan_seconds", 120, "30-86400"),
])
def test_stage2_defaults_registry_and_deployment_boundary(key, default, range_text):
    registry = parameter_registry()
    assert key in registry
    spec = registry[key]
    assert getattr(AgentConfig(), key) == getattr(load_config(packaged_config_path()), key) == spec.default == default
    assert spec.tier == "deployment" and key in DEPLOYMENT_ONLY_KEYS
    assert key not in TUNABLE_KEYS and not spec.writable and not spec.user_editable
    assert any("\u4e00" <= char <= "\u9fff" for char in spec.description)
    assert spec.range == range_text
    if spec.value_type in {"int", "float"}:
        assert ranges()[key] == range_text
    else:
        assert spec.value_type == "bool" and spec.range == "true|false"
    assert spec.effect == (EFFECT_IMMEDIATE if key == "owner_epoch_fence_enabled" else "restart_gateway")


@pytest.mark.parametrize("key,raw,expected", [
    ("gateway_event_hub_ring_events", -1, 0),
    ("gateway_event_hub_ring_events", 65537, 65536),
    ("gateway_event_hub_ring_events", "65537", 65536),
    ("gateway_event_fallback_poll_seconds", -0.5, 0.0),
    ("gateway_event_fallback_poll_seconds", 61.0, 60.0),
    ("conversation_actor_workers", -1, 0),
    ("conversation_actor_workers", 65, 64),
    ("background_owner_wake_rescan_seconds", 0, 30),
    ("background_owner_wake_rescan_seconds", 86401, 86400),
])
def test_stage2_numeric_values_clamp_with_warning(key, raw, expected):
    out, warnings = normalize_agent_config({key: raw})
    assert out.get(key) == expected
    assert any(key in warning for warning in warnings)


@pytest.mark.parametrize("key,default", [
    ("gateway_event_hub_ring_events", 0), ("gateway_event_fallback_poll_seconds", 0.0),
    ("conversation_actor_workers", 0), ("background_owner_wake_rescan_seconds", 120),
])
@pytest.mark.parametrize("raw", [True, "invalid", float("nan"), float("inf")])
def test_stage2_numeric_invalid_types_use_default_with_warning(key, default, raw):
    out, warnings = normalize_agent_config({key: raw})
    assert out.get(key) == default
    assert any(key in warning for warning in warnings)


@pytest.mark.parametrize("raw,expected", [("true", True), ("false", False), ("off", False)])
def test_epoch_boolean_strings_are_not_truthy_strings(raw, expected):
    out, warnings = normalize_agent_config({"owner_epoch_fence_enabled": raw})
    assert out.get("owner_epoch_fence_enabled") is expected
    assert not any("owner_epoch_fence_enabled" in warning for warning in warnings)


def test_cell_template_loads_all_stage2_values_without_unknown_keys():
    cell = Path(__file__).resolve().parents[2] / "deploy/cell-template/agent_config.cell.yaml"
    config = load_config(cell)
    for key, expected in {
        "gateway_event_hub_ring_events": 1024, "gateway_event_fallback_poll_seconds": 2.0,
        "conversation_actor_workers": 4, "owner_epoch_fence_enabled": True,
        "background_owner_wake_rescan_seconds": 1800,
    }.items():
        assert getattr(config, key, None) == expected
    assert not config.config_warnings


@pytest.mark.parametrize("key", [
    "gateway_event_hub_ring_events", "gateway_event_fallback_poll_seconds", "conversation_actor_workers",
    "owner_epoch_fence_enabled", "background_owner_wake_rescan_seconds",
])
def test_only_epoch_fence_can_be_written_in_cli_deployment_scope(tmp_path, key):
    assert key in parameter_registry()
    path = tmp_path / "config.yaml"
    path.write_text("agent_name: test\n", encoding="utf-8")
    paths, origin = changes.WritePaths(user_path=path), changes.ChangeOrigin("test")
    before = path.read_bytes()
    raw = "true" if key == "owner_epoch_fence_enabled" else "32"
    denied = changes.set_parameter(key, raw, paths=paths, origin=origin)
    assert not denied["ok"] and path.read_bytes() == before
    with changes.user_settings_write_scope():
        denied_chat = changes.set_parameter(key, raw, paths=paths, origin=origin)
    assert not denied_chat["ok"] and path.read_bytes() == before
    with changes.cli_deployment_write_scope():
        result = changes.set_parameter(key, raw, paths=paths, origin=origin)
    if key == "owner_epoch_fence_enabled":
        assert key in DEPLOY_LIVE_SWITCH_KEYS and key in LIVE_SWITCH_KEYS
        assert result["ok"] and result["effect_when"] == EFFECT_IMMEDIATE
        assert load_config(path).owner_epoch_fence_enabled is True
    else:
        assert not result["ok"] and path.read_bytes() == before
        assert changes.parameter_history(user_path=path) == []


def test_applied_rules_report_disabled_hub_and_retired_worker_topology():
    assert applied_value("gateway_event_fallback_poll_seconds", SimpleNamespace()) == (
        0, "只在事件中心开启时生效")
    for ring, expected in ((0, 0), (1024, 2.0)):
        config = SimpleNamespace(gateway_event_hub_ring_events=ring, gateway_event_fallback_poll_seconds=2.0)
        assert applied_value("gateway_event_fallback_poll_seconds", config)[0] == expected
    assert applied_value("gateway_worker_process_count", AgentConfig(gateway_worker_process_count=2))[0] == 0


def test_yaml_epoch_switch_reaches_real_claim_finish_without_restart(tmp_path):
    assert "owner_epoch_fence_enabled" in parameter_registry()
    path = tmp_path / "agent.yaml"
    path.write_text("owner_epoch_fence_enabled: false\n", encoding="utf-8")
    config = load_config(path)
    register_live_config(config)
    repo = RuntimeRepository(tmp_path / "runtime.db")
    store = ConversationStore(tmp_path / "conversations")
    store.storage.owner_epoch_repository = repo
    store.storage.owner_epoch_config = config
    threads = [store.threads.get_or_create({
        "canonical_user_id": "owner", "owner_id": "local/main", "owner_home": str(tmp_path),
        "channel": "test", "channel_user_id": "owner", "channel_conversation_id": f"chat-{index}",
    }) for index in range(2)]
    old = epoch.activate_owner_epoch(repo)
    claims = [store.claims.acquire({"thread_id": thread.thread_id, "task_id": f"task-{index}"})
              for index, thread in enumerate(threads)]
    epoch.activate_owner_epoch(repo)
    with epoch.request_epoch_scope(repo, old, config):
        assert store.claims.finish({"thread_id": threads[0].thread_id, "claim_id": claims[0]["claim_id"],
                                    "status": "cancelled"})["status"] == "cancelled"
        path.write_text("owner_epoch_fence_enabled: true\n", encoding="utf-8")
        assert "owner_epoch_fence_enabled" in refresh_live_switches(config, force=True)
        before = store.storage.background_claim_path(threads[1].thread_id).read_bytes()
        with pytest.raises(epoch.StaleOwnerEpoch):
            store.claims.finish({"thread_id": threads[1].thread_id, "claim_id": claims[1]["claim_id"],
                                 "status": "cancelled"})
        assert store.storage.background_claim_path(threads[1].thread_id).read_bytes() == before
        path.write_text("owner_epoch_fence_enabled: false\n", encoding="utf-8")
        assert "owner_epoch_fence_enabled" in refresh_live_switches(config, force=True)
        assert store.claims.finish({"thread_id": threads[1].thread_id, "claim_id": claims[1]["claim_id"],
                                    "status": "cancelled"})["status"] == "cancelled"
