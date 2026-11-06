"""Measure real HTTPS model transports through the public backend factory."""
from __future__ import annotations

import ssl
import time

import pytest

from agent_py_agent.agent.backends import keepalive_transport
from agent_py_agent.agent.backends.factory import get_backend
from agent_py_agent.agent.settings import load_config
from agent_py_agent.agent.settings.model_profiles import selected_model_config_for_resources
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    resolve_owner_home,
)

from .fake_provider import FakeProvider
from .load import GatewayProcess
from .report import require_target


# LLM: 每轮fixture信任自己的临时CA，不沿用MEM-W3首次创建的进程共享上下文；仍完整校验证书。
# 函数用途: Compare actual handshake counts and elapsed model-call time with pooling off and requested on.
@pytest.mark.slo
def test_connection_reuse(tmp_path, monkeypatch, slo_data, slo_report):
    scenario = next(row for row in slo_data["scenarios"] if row["id"] == "connection_reuse")
    profile = scenario["load_profile"]
    results = {}
    for mode in ("legacy", "pooled"):
        with FakeProvider(tmp_path / mode, https=True, handshake_delay=profile["handshake_delay"],
                          teardown_seconds=slo_data["fixture"]["teardown_seconds"]) as provider:
            monkeypatch.setenv("SSL_CERT_FILE", str(provider.cert_path))
            monkeypatch.setattr(keepalive_transport, "_tls_context", lambda: ssl.create_default_context(cafile=str(provider.cert_path)))
            capacity = scenario["config"]["model_http_max_idle_connections"] if mode == "pooled" else 0
            gateway = GatewayProcess(tmp_path / f"{mode}-home", provider, slo_data["fixture"],
                {"stream_enabled": False, "model_http_max_idle_connections": capacity})
            owner_home = home_paths_with_owner(home_paths(gateway.home), resolve_owner_home(gateway.home, OwnerIdentity.local_main()))
            config = selected_model_config_for_resources(load_config(gateway.config), owner_home)
            backend = get_backend(config.model_backend, config)
            measurements = []
            for index in range(profile["calls"]):
                before_count, before = provider.handshakes, time.monotonic()
                result = backend.generate(f"transport-fixture-{index}")
                assert result, "model transport returned an empty result"
                measurements.append({"elapsed_ms": (time.monotonic() - before) * 1000,
                                     "handshakes": provider.handshakes - before_count})
            results[mode] = {"calls": len(provider.records), "tcp_accepts": provider.accepts,
                             "tls_handshakes": provider.handshakes, "samples": measurements,
                             "elapsed_ms": sum(row["elapsed_ms"] for row in measurements)}
    saved = results["legacy"]["elapsed_ms"] - results["pooled"]["elapsed_ms"]
    report = slo_report("connection_reuse", profile, [], transport=results, time_saved_ms=saved,
        metric_scope="model_transport_not_gateway_acceptance", cold=results["pooled"]["samples"][0],
        warm=results["pooled"]["samples"][1:])
    assert all(result["calls"] == profile["calls"] for result in results.values()), report
    require_target(results["pooled"]["tls_handshakes"] <= scenario["targets"]["total_handshakes_max"], report)
    require_target(sum(row["handshakes"] for row in results["pooled"]["samples"][1:]) <= scenario["targets"]["warm_handshakes_max"], report)
    require_target(saved >= scenario["targets"]["minimum_saved_ms"], report)
