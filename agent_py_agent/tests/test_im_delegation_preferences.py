"""当天偏好只改变新任务策略；只运行本地配置与控制合同，不触发实际网络。"""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from agent_py_agent.agent.conversation.control_commands import parse_conversation_command
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.delegation_control import execute_delegation_control
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.agent.settings.im_task_preferences import (
    delegation_preference,
    preference_path,
    set_delegation_preference,
)
from agent_py_agent.tests.test_im_execution_threads import fixture_agent


def test_zero_midnight_expires_without_timer_and_actor_settings_are_independent(tmp_path):
    agent, _ = fixture_agent(tmp_path)
    before = datetime(2026, 10, 12, 23, 59, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    enabled = set_delegation_preference(agent, "feishu", "ou_a", True, now=before)
    assert enabled["enabled"]
    assert not delegation_preference(agent, "feishu", "ou_b", now=before)["enabled"]
    assert not delegation_preference(agent, "feishu", "ou_a", now=enabled["expires_at"])["enabled"]
    reopened = SimpleNamespace(home_paths=agent.home_paths, config=agent.config)
    assert not delegation_preference(reopened, "feishu", "ou_a", now=enabled["expires_at"] + 60)["enabled"]
    assert not delegation_preference(agent, "feishu", "ou_a", now=before - 86400)["enabled"]
    assert enabled["enabled"]  # 已受理任务持有的原快照没有被下一天读取改变。


def test_disable_advances_only_own_setting_and_does_not_mutate_frozen_mode(tmp_path):
    agent, _ = fixture_agent(tmp_path)
    snapshot = set_delegation_preference(agent, "feishu", "ou_a", True, now=1000)
    other = set_delegation_preference(agent, "feishu", "ou_b", True, now=1000)
    disabled = set_delegation_preference(agent, "feishu", "ou_a", False, now=1010)
    assert not disabled["enabled"]
    assert disabled["revision"] == snapshot["revision"] + 1
    assert snapshot["enabled"]
    assert delegation_preference(agent, "feishu", "ou_b", now=1010) == other


def test_unconfigured_status_uses_same_deployment_timezone_as_enable(tmp_path, monkeypatch):
    agent, _ = fixture_agent(tmp_path)
    agent.config.timezone = ""
    monkeypatch.setattr("agent_py_agent.agent.scheduler.schedule._local_zone_name", lambda: "Asia/Tokyo")
    assert delegation_preference(agent, "feishu", "ou_a")["timezone"] == "Asia/Tokyo"
    assert set_delegation_preference(agent, "feishu", "ou_a", True)["timezone"] == "Asia/Tokyo"


def test_dst_uses_next_business_midnight_instead_of_24_hour_ttl(tmp_path):
    agent, _ = fixture_agent(tmp_path)
    agent.config.timezone = "America/New_York"
    instant = datetime(2026, 3, 8, tzinfo=ZoneInfo(agent.config.timezone)).timestamp()
    enabled = set_delegation_preference(agent, "feishu", "ou_a", True, now=instant)
    assert enabled["expires_at"] - instant == 23 * 3600


def test_disabled_child_creation_rejects_enable_and_keeps_new_input_original(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.im_dispatch_policy import capture_im_dispatch

    agent, _ = fixture_agent(tmp_path)
    agent.config.enable_subagents = True
    monkeypatch.setattr("agent_py_agent.agent.gateway_parts.control_service._request_agent_for_scope", lambda *_: agent)
    scope = GatewayControlScope(user_id="ou_a", channel="feishu", conversation_id="oc_a")
    set_delegation_preference(agent, "feishu", "ou_a", True)
    agent.owner_policy = SimpleNamespace(disabled_tools=("create_subagents",))
    assert not execute_delegation_control(agent, parse_conversation_command("/delegate enable"), scope).ok
    assert capture_im_dispatch(agent, scope)["mode"] == "original"


def test_tui_and_foreign_or_broken_preferences_are_rejected(tmp_path):
    agent, _ = fixture_agent(tmp_path)
    with pytest.raises(ValueError):
        set_delegation_preference(agent, "tui", "ou_a", True)
    set_delegation_preference(agent, "feishu", "ou_a", True)
    payload = json.loads(preference_path(agent).read_text())
    payload["owner_id"] = "foreign"
    preference_path(agent).write_text(json.dumps(payload))
    with pytest.raises(DataCorruptionError):
        delegation_preference(agent, "feishu", "ou_a")


def test_delegate_command_changes_only_authenticated_actor(tmp_path, monkeypatch):
    agent, _ = fixture_agent(tmp_path)
    agent.config.enable_subagents = True
    monkeypatch.setattr("agent_py_agent.agent.gateway_parts.control_service._request_agent_for_scope", lambda *_: agent)
    scope = GatewayControlScope("ou_a", "feishu", "oc_chat")
    enabled = execute_delegation_control(agent, parse_conversation_command("/delegate enable"), scope)
    assert enabled.ok and delegation_preference(agent, "feishu", "ou_a")["enabled"]
    assert not delegation_preference(agent, "feishu", "ou_b")["enabled"]
    assert execute_delegation_control(agent, parse_conversation_command("/delegate disable"), scope).ok
    assert not delegation_preference(agent, "feishu", "ou_a")["enabled"]
    invalid = parse_conversation_command("/delegate enable ou_b")
    assert invalid is None or not invalid.valid
