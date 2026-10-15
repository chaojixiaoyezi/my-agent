from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.adapter.feishu import FeishuAdapter
from agent_py_agent.agent.adapter.manager import ChannelManager
from agent_py_agent.agent.delivery import (
    ChannelAdapterRegistry,
    DeliveryContext,
    DeliveryService,
    ReplyEnvelope,
)
from agent_py_agent.agent.gateway_parts.channel_health import adapter_runtime_health


def _agent(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        root=tmp_path,
        config=SimpleNamespace(gateway_workspace="gateway"),
    )


def _write_pid(path: Path, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid}), encoding="utf-8")


def test_adapter_runtime_health_reads_fresh_per_channel_snapshot(tmp_path: Path) -> None:
    gateway = tmp_path / "gateway"
    _write_pid(gateway / "adapter.pid", os.getpid())
    (gateway / "adapter_state.json").write_text(
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

    result = adapter_runtime_health(_agent(tmp_path))

    assert result["feishu"]["state"] == "healthy"
    assert result["feishu"]["error_code"] == ""


def test_adapter_runtime_health_rejects_stale_heartbeat(tmp_path: Path) -> None:
    gateway = tmp_path / "gateway"
    _write_pid(gateway / "adapter.pid", os.getpid())
    (gateway / "adapter_state.json").write_text(
        json.dumps(
            {
                "state": "running",
                "updated_at": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    result = adapter_runtime_health(_agent(tmp_path))

    assert result["*"]["state"] == "unhealthy"
    assert result["*"]["error_code"] == "CHANNEL_ADAPTER_HEARTBEAT_STALE"


def test_adapter_runtime_health_fails_closed_when_process_is_dead(tmp_path: Path) -> None:
    gateway = tmp_path / "gateway"
    _write_pid(gateway / "adapter.pid", 999_999_999)
    (gateway / "adapter_state.json").write_text(
        json.dumps(
            {
                "state": "running",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    result = adapter_runtime_health(_agent(tmp_path))

    assert result["*"]["state"] == "unhealthy"
    assert result["*"]["error_code"] == "CHANNEL_ADAPTER_NOT_RUNNING"


# LLM: 保留真实FeishuAdapter与registry，只替换SDK运输事实；不开连接、不构造密码库或写入生产状态。
# 函数用途: 为健康投影测试建立无网络的运行中飞书适配器。
def _feishu_with_transport(transport):
    adapter = FeishuAdapter({"feishu_session_lock_enabled": False})
    adapter._running = True
    adapter._ws_client = SimpleNamespace(transport_health=lambda: transport)
    return adapter


@pytest.mark.parametrize(("transport", "state", "error"), [
    ({"status": "healthy", "reason_code": "FEISHU_WS_CONNECTED"}, "healthy", ""),
    ({"status": "degraded", "reason_code": "FEISHU_WS_CONNECTING"}, "starting", "FEISHU_WS_CONNECTING"),
    ({"status": "degraded", "reason_code": "FEISHU_WS_DISCONNECTED"}, "unhealthy", "FEISHU_WS_DISCONNECTED"),
    ({"status": "degraded", "reason_code": "FEISHU_WS_STATE_UNKNOWN"}, "unhealthy", "FEISHU_WS_STATE_UNKNOWN"),
    ({"status": "stopped", "reason_code": "FEISHU_WS_STOPPED"}, "unhealthy", "FEISHU_WS_STOPPED"),
    ({"status": "healthy", "reason_code": "unexpected"}, "unhealthy", "FEISHU_WS_STATE_UNKNOWN"),
    (None, "unhealthy", "FEISHU_WS_STATE_UNKNOWN"),
])
def test_feishu_health_uses_transport_instead_of_running(transport, state, error):
    adapter = _feishu_with_transport(transport)
    registry = ChannelAdapterRegistry()
    registry.register_adapter("feishu", adapter)
    registry.mark_health("feishu", "healthy")

    snapshot = registry.runtime_snapshot(DeliveryContext(channel="feishu", target="ou_test"))[0]

    assert adapter.running is True
    assert snapshot.health.state == state
    assert snapshot.health.error_code == error
    assert snapshot.ready is (state == "healthy")
    assert snapshot.state != "healthy_not_bound"


def test_feishu_transport_recovery_is_live_and_stop_remains_lifecycle():
    transport = {"status": "degraded", "reason_code": "FEISHU_WS_DISCONNECTED"}
    adapter = _feishu_with_transport(transport)
    registry = ChannelAdapterRegistry()
    registry.register_adapter("feishu", adapter)
    assert registry.runtime_snapshot()[0].health.state == "unhealthy"

    transport.update(status="healthy", reason_code="FEISHU_WS_CONNECTED")
    assert registry.runtime_snapshot()[0].health.state == "healthy"
    assert registry.runtime_snapshot()[0].state == "healthy_not_bound"
    adapter._running = False
    assert registry.runtime_snapshot()[0].health.state == "stopped"


def test_feishu_initial_client_and_transport_query_failure_are_not_healthy():
    adapter = _feishu_with_transport({})
    adapter._ws_client = None
    assert adapter.runtime_health()["state"] == "starting"
    adapter._ws_client = SimpleNamespace()
    assert adapter.runtime_health()["error_code"] == "FEISHU_WS_STATE_UNKNOWN"


def test_sdk_connection_state_reaches_original_registry_snapshot():
    from websockets.protocol import State

    from agent_py_agent.agent.adapter.feishu_ws import FeishuWsClient

    adapter = _feishu_with_transport({})
    client = FeishuWsClient(app_id="synthetic", app_secret="synthetic", on_payload=lambda _: None)
    adapter._ws_client = client
    registry = ChannelAdapterRegistry()
    registry.register_adapter("feishu", adapter)
    assert registry.runtime_snapshot()[0].health.state == "starting"
    connection = SimpleNamespace(state=State.OPEN)
    client._client = SimpleNamespace(_conn=connection)
    assert registry.runtime_snapshot()[0].health.state == "healthy"
    connection.state = State.CLOSED
    assert registry.runtime_snapshot()[0].health.error_code == "FEISHU_WS_DISCONNECTED"
    assert adapter.running is True


def test_manager_snapshot_and_cross_process_projection_keep_transport_failure(tmp_path):
    adapter = _feishu_with_transport({"status": "degraded", "reason_code": "FEISHU_WS_DISCONNECTED"})
    manager = ChannelManager()
    manager.register_adapter(adapter)
    manager.start_all()
    statuses = manager.runtime_channel_statuses()
    assert statuses[0]["health"]["state"] == "unhealthy"
    assert statuses[0]["ready"] is False
    assert adapter.running is True
    gateway = tmp_path / "gateway"
    _write_pid(gateway / "adapter.pid", os.getpid())
    (gateway / "adapter_state.json").write_text(json.dumps({
        "state": "running", "updated_at": datetime.now(timezone.utc).isoformat(), "channels": statuses,
    }))

    assert adapter_runtime_health(_agent(tmp_path))["feishu"]["error_code"] == "FEISHU_WS_DISCONNECTED"


def test_manager_start_failure_survives_stopped_provider_then_recovers(monkeypatch):
    adapter = FeishuAdapter({"feishu_connection_mode": "webhook", "feishu_session_lock_enabled": False})
    def fail_start():
        raise OSError("synthetic-bind-failure")
    monkeypatch.setattr(adapter, "start", fail_start)
    manager = ChannelManager()
    manager.register_adapter(adapter)
    manager.start_all()
    failed = manager.runtime_channel_statuses()[0]
    assert failed["health"]["state"] == "unhealthy"
    assert failed["health"]["error_code"] == "CHANNEL_ADAPTER_START_FAILED"

    adapter._running = True
    assert manager.runtime_channel_statuses()[0]["health"]["state"] == "healthy"
    manager.stop_all()
    stopped = manager.runtime_channel_statuses()[0]
    assert stopped["health"]["state"] == "stopped"
    assert stopped["health"]["error_code"] != "CHANNEL_ADAPTER_START_FAILED"


@pytest.mark.parametrize("result", [None, {"state": "unknown"}, {"state": "healthy", "error_code": "raw secret text"}])
def test_explicit_adapter_health_invalid_protocol_does_not_fall_back_to_running(result):
    registry = ChannelAdapterRegistry()
    registry.register_adapter("custom", SimpleNamespace(running=True, runtime_health=lambda: result))
    health = registry.runtime_snapshot()[0].health
    assert health.state == "unhealthy"
    assert health.error_code == "CHANNEL_HEALTH_PROTOCOL_INVALID"


def test_explicit_adapter_health_exception_does_not_leak_or_fall_back():
    def broken():
        raise RuntimeError("synthetic-private-value")
    registry = ChannelAdapterRegistry()
    registry.register_adapter("custom", SimpleNamespace(running=True, runtime_health=broken))
    status = registry.runtime_snapshot()[0].to_dict()
    assert status["health"]["state"] == "unhealthy"
    assert "synthetic-private-value" not in str(status)


def test_ws_unhealthy_does_not_block_independent_rest_delivery(monkeypatch):
    adapter = _feishu_with_transport({"status": "degraded", "reason_code": "FEISHU_WS_DISCONNECTED"})
    sent = []
    monkeypatch.setattr(adapter, "send_message", lambda target, message: sent.append(target) or True)
    registry = ChannelAdapterRegistry()
    registry.register_adapter("feishu", adapter)
    assert registry.runtime_snapshot()[0].health.state == "unhealthy"

    receipt = DeliveryService(registry).deliver(
        DeliveryContext(channel="feishu", target="ou_test", mode="reply"), ReplyEnvelope(content="合成回执"))

    assert receipt.delivery_status == "sent"
    assert sent == ["ou_test"]
