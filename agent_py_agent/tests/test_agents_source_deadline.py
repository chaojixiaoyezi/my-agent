"""HTTPS权限/DNS预检查不能绕过抓取已有总期限。"""
import threading
import time
from contextvars import ContextVar

import pytest

from agent_py_agent.agent.capability import agents_source_network as network
from agent_py_agent.agent.capability.agents_source_contract import AgentsContractError


def test_permission_dns_check_cannot_outlive_existing_request_deadline(monkeypatch):
    release = threading.Event()
    calls = []
    deadline = time.monotonic() + .07
    monkeypatch.setattr(network, "_request_deadline", ContextVar("test_deadline", default=deadline), raising=False)
    def resolver(host):
        calls.append(host)
        release.wait(.5)
        return ("8.8.8.8",)
    start = time.monotonic()
    try:
        with pytest.raises(AgentsContractError, match="AGENTS_SOURCE_TIMEOUT"):
            network.validate_agents_url("https://public.test/", url_allowed=lambda url: True, resolver=resolver)
        assert time.monotonic() - start < .3
        assert calls == ["public.test"]
    finally:
        release.set()
