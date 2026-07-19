from __future__ import annotations

"""list_capabilities 只投影同一运行时 registry 的安装、配置、健康与绑定事实。"""

import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.delivery import (
    ChannelAdapterRegistry,
    ChannelCapabilities,
    ChannelHealth,
    DeliveryContext,
    build_default_channel_registry,
)
from agent_py_agent.agent.gateway_parts.request_worker import _resolve_request_agent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.capabilities_tool import (
    ListCapabilitiesTool,
    build_capability_inventory,
)


def test_channels_come_only_from_runtime_registry() -> None:
    registry = ChannelAdapterRegistry()
    registry.register_factory(
        "second-im",
        lambda: None,
        capabilities=ChannelCapabilities(text=True, reply=True),
        configured=False,
    )

    inventory = build_capability_inventory(channel_registry=registry)

    assert inventory["channels"] == ["second-im"]
    assert inventory["channel_catalog"][0]["installed"] is True
    assert inventory["channel_catalog"][0]["state"] == "setup_required"


def test_capability_inventory_covers_product_areas() -> None:
    config = SimpleNamespace(
        feishu_app_id="cli_test",
        feishu_app_secret="secret",
        qq_app_id="",
        qq_app_secret="",
        gateway_per_user_owner_scoping=True,
    )
    tools = {
        "remember",
        "session_search",
        "update_persona",
        "create_subagents",
        "dispatch_subagents",
        "wait",
        "skill_search",
        "browser",
        "analyze_image",
        "send_message",
    }
    registry = build_default_channel_registry(
        config,
        runtime_health_provider=lambda: {
            "feishu": ChannelHealth(
                state="healthy",
                checked_at="2026-07-18T00:00:00+00:00",
            )
        },
    )

    inventory = build_capability_inventory(
        config=config,
        tool_names_provider=lambda: tools,
        channel_registry=registry,
        channel_binding_provider=lambda: DeliveryContext(
            channel="feishu",
            target="ou_current",
            mode="proactive",
        ),
    )

    areas = {item["area"] for item in inventory["capabilities"]}
    assert {"多通道网关", "多用户隔离", "持久记忆", "当前任务非阻塞等待"} <= areas
    assert inventory["configured_channels"] == ["feishu"]
    states = {str(row["name"]): row["state"] for row in inventory["channel_catalog"]}
    assert states == {"feishu": "ready", "qq": "setup_required"}
    feishu = next(row for row in inventory["channel_catalog"] if row["name"] == "feishu")
    assert feishu["health"]["state"] == "healthy"
    assert feishu["current_bound"] is True
    assert feishu["binding_target_kind"] == "open_id"
    assert "ou_current" not in str(feishu)
    assert any(item["area"] == "持久定时/日历提醒" for item in inventory["not_available"])
    assert "current_bound=false" in inventory["principle"]


def test_skill_capability_projects_snapshot_without_private_paths() -> None:
    snapshot = SimpleNamespace(
        fingerprint="snapshot-abc",
        enabled_entries=lambda: (
            SimpleNamespace(source="builtin", path="/private/builtin/SKILL.md"),
            SimpleNamespace(source="shared", path="/private/shared/SKILL.md"),
            SimpleNamespace(source="owner", path="/private/owner/SKILL.md"),
        ),
        errors=(
            SimpleNamespace(
                code="SKILL_PARSE_FAILED",
                path="/private/broken/SKILL.md",
                message="secret parse detail",
            ),
        ),
    )

    inventory = build_capability_inventory(
        tool_names_provider=lambda: {"skill_search"},
        skill_snapshot_provider=lambda: snapshot,
    )

    assert inventory["skill_catalog"] == {
        "state": "available",
        "health": "degraded",
        "enabled_total": 3,
        "enabled_by_source": {"builtin": 1, "shared": 1, "owner": 1},
        "load_error_count": 1,
        "load_error_codes": ["SKILL_PARSE_FAILED"],
        "snapshot_fingerprint": "snapshot-abc",
    }
    skill = next(item for item in inventory["capabilities"] if item["area"] == "Skill 检索")
    assert skill["state"] == "available"
    assert "builtin=1" in skill["what"]
    assert "/private/" not in str(inventory)
    assert "secret parse detail" not in str(inventory)


def test_skill_capability_fails_closed_when_snapshot_is_unavailable() -> None:
    def unavailable_snapshot() -> object:
        raise RuntimeError("private failure detail")

    inventory = build_capability_inventory(
        tool_names_provider=lambda: {"skill_search"},
        skill_snapshot_provider=unavailable_snapshot,
    )

    assert inventory["skill_catalog"]["state"] == "unavailable"
    assert inventory["skill_catalog"]["health"] == "unavailable"
    skill = next(item for item in inventory["capabilities"] if item["area"] == "Skill 检索")
    assert skill["state"] == "unavailable"
    assert "private failure detail" not in str(inventory)


def test_memory_and_persona_catalogs_project_health_without_private_content() -> None:
    inventory = build_capability_inventory(
        tool_names_provider=lambda: {"remember", "session_search", "update_persona"},
        memory_snapshot_provider=lambda: {
            "state": "available",
            "health": "degraded",
            "active_total": 3,
            "active_by_kind": {"fact": 2, "lesson": 1},
            "event_total": 5,
            "load_error_count": 1,
            "load_error_codes": ["JSONDecodeError"],
            "text_index": "configured",
            "semantic_index": "unconfigured",
            "path": "/private/memory.jsonl",
            "content": "private memory content",
        },
        persona_snapshot_provider=lambda: {
            "state": "available",
            "health": "degraded",
            "targets": [
                {
                    "target": "user",
                    "state": "blocked",
                    "truncated": False,
                    "blocked_lines": 1,
                    "version": 4,
                    "path": "/private/USER.md",
                    "content": "private persona content",
                }
            ],
            "confirmation_required": ["soul", "agents"],
            "user_autonomous": True,
            "versioned": True,
        },
    )

    assert inventory["schema_version"] == 5
    assert inventory["memory_catalog"]["active_total"] == 3
    assert inventory["memory_catalog"]["active_by_kind"] == {"fact": 2, "lesson": 1}
    assert inventory["persona_catalog"]["targets"] == [
        {
            "target": "user",
            "state": "blocked",
            "truncated": False,
            "blocked_lines": 1,
            "version": 4,
        }
    ]
    assert "/private/" not in str(inventory)
    assert "private memory content" not in str(inventory)
    assert "private persona content" not in str(inventory)
    states = {row["area"]: row["state"] for row in inventory["capabilities"]}
    assert states["持久记忆"] == "available"
    assert states["人格定制"] == "available"


def test_scheduler_capability_uses_owner_runtime_snapshot() -> None:
    inventory = build_capability_inventory(
        tool_names_provider=lambda: {"schedule"},
        scheduler_snapshot_provider=lambda: {
            "state": "available",
            "health": "healthy",
            "active_jobs": 2,
            "paused_jobs": 1,
            "active_runs": 1,
            "schedule_kinds": ["cron", "every"],
            "private_path": "/private/scheduler/store.json",
        },
    )

    assert inventory["scheduler_catalog"] == {
        "state": "available",
        "health": "healthy",
        "active_jobs": 2,
        "paused_jobs": 1,
        "active_runs": 1,
        "schedule_kinds": ["cron", "every"],
        "load_error_codes": [],
    }
    assert inventory["not_available"] == []
    scheduled = next(item for item in inventory["capabilities"] if item["area"] == "持久定时与提醒")
    assert scheduled["state"] == "available"
    assert "/private/" not in str(inventory)


def test_list_capabilities_tool_executes() -> None:
    config = SimpleNamespace(
        feishu_app_id="",
        feishu_app_secret="",
        qq_app_id="",
        qq_app_secret="",
        gateway_per_user_owner_scoping=False,
    )
    result = ListCapabilitiesTool(
        config=config,
        tool_names_provider=lambda: {"wait"},
        channel_registry=build_default_channel_registry(config),
    ).execute({})

    assert result.ok
    assert result.result_envelope.get("channels") == ["feishu", "qq"]
    assert result.result_envelope.get("capabilities")
    assert result.result_envelope["configured_channels"] == []


def test_simple_agent_capability_binding_comes_from_current_thread(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            feishu_app_id="configured",
            feishu_app_secret="configured",
        ),
        tmp_path,
    )
    thread = agent.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": "canonical-a",
            "owner_id": agent.home_paths.owner_id,
            "owner_home": str(agent.home_paths.owner_home_dir),
            "channel": "feishu",
            "channel_conversation_id": "oc_private",
            "channel_user_id": "ou_private",
            "title": "binding test",
        }
    )
    agent._current_run_params = RunParams(
        task_attributes={"conversation_thread_id": thread.thread_id}
    )

    result = agent.tools.tools["list_capabilities"].execute({})

    assert "schedule" in agent.tools.tools
    feishu = next(row for row in result.result_envelope["channel_catalog"] if row["name"] == "feishu")
    assert feishu["current_bound"] is True
    assert feishu["binding_target_kind"] == "open_id"
    assert "ou_private" not in result.output
    assert "oc_private" not in result.output
    assert result.result_envelope["memory_catalog"]["state"] == "available"
    assert result.result_envelope["persona_catalog"]["state"] == "available"
    assert result.result_envelope["scheduler_catalog"]["state"] == "available"


def test_owner_scoped_agent_uses_gateway_process_health_and_its_own_binding(tmp_path) -> None:
    base = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            feishu_app_id="configured",
            feishu_app_secret="configured",
            gateway_per_user_owner_scoping=True,
        ),
        tmp_path / "service",
    )
    gateway_root = base.root / base.config.gateway_workspace
    gateway_root.mkdir(parents=True, exist_ok=True)
    (gateway_root / "adapter.pid").write_text(
        json.dumps({"pid": os.getpid()}),
        encoding="utf-8",
    )
    (gateway_root / "adapter_state.json").write_text(
        json.dumps(
            {
                "state": "running",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "channels": [
                    {
                        "name": "feishu",
                        "health": {"state": "healthy", "error_code": ""},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    scoped = _resolve_request_agent(
        base,
        {
            "user_id": "ou_owner_a",
            "metadata": {
                "channel": "feishu",
                "channel_chat_type": "p2p",
                "channel_chat_id": "oc_owner_a",
            },
        },
    )
    assert scoped is not base
    assert scoped.config.gateway_workspace != base.config.gateway_workspace

    thread = scoped.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": "canonical-owner-a",
            "owner_id": scoped.home_paths.owner_id,
            "owner_home": str(scoped.home_paths.owner_home_dir),
            "channel": "feishu",
            "channel_conversation_id": "oc_owner_a",
            "channel_user_id": "ou_owner_a",
            "title": "owner runtime health binding test",
        }
    )
    scoped._current_run_params = RunParams(
        task_attributes={"conversation_thread_id": thread.thread_id}
    )

    result = scoped.tools.tools["list_capabilities"].execute({})

    feishu = next(
        row for row in result.result_envelope["channel_catalog"] if row["name"] == "feishu"
    )
    assert feishu["health"]["state"] == "healthy"
    assert feishu["current_bound"] is True
    assert feishu["state"] == "ready"
    assert "ou_owner_a" not in result.output
    assert "oc_owner_a" not in result.output
