"""watch_stream refusals raised before any watch exists must reach the operation ledger as not_started.

Live clone (2026-11-17): an Audit prepare opened file://<workspace> and the path policy refused it with
TOOL_PERMISSION_DENIED, but the ledger stored the call as TOOL_OPERATION_OUTCOME_UNKNOWN
(effect_outcome_unknown:TOOL_PERMISSION_DENIED) because ``_err`` only wrote effect_outcome into the
display body.  UNKNOWN blocks the model from retrying a different source.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent.common.audit_activation import AUDIT_ATTR
from agent.conversation.authority import CONVERSATION_REQUEST_ID_ATTR
from agent.ingestion import harvester as hv
from agent.ingestion import watch_state as ws
from agent.ingestion import watch_tool as wt
from agent.ingestion.watch_tool import WatchStreamTool
from agent.settings import AgentConfig
from agent.tooling.models import ToolHandlerOutcome
from agent.tooling.runtime_contracts import ProviderToolCapability, ToolProtocolSnapshot
from agent.tooling.tool_operation_coordinator import (
    TOOL_OPERATION_FAILED,
    _operation_status_for_result,
)


@pytest.fixture()
def owner_home(tmp_path, monkeypatch):
    fresh = ws.WatchRegistry()
    monkeypatch.setattr(ws, "registry", fresh)
    monkeypatch.setattr(wt, "registry", fresh)
    monkeypatch.setattr(hv, "harvesters", hv._HarvesterRegistry())
    home = tmp_path / "owner"
    home.mkdir()
    yield home
    for watch_id in fresh.ids():
        hv.stop_harvester(watch_id)


def _tool(owner_home: Path, task_attributes: dict | None = None) -> WatchStreamTool:
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=str(owner_home), owner_id="u-test"),
        _current_user_prompt="",
        _current_run_params=SimpleNamespace(
            task_attributes=task_attributes,
            tool_protocol_snapshot=ToolProtocolSnapshot(
                run_id="watch-refusal-run",
                source_protocol="text",
                capability=ProviderToolCapability(
                    provider="test", endpoint="local://watch-refusal", model="watch-refusal",
                    stream=False, native_supported=False, evidence="explicit-watch-refusal-contract",
                ),
            ),
        ),
        config=AgentConfig(enable_subagents=False),
        subagents=None,
    )
    return WatchStreamTool(agent)


def _assert_terminal_not_started(outcome: ToolHandlerOutcome) -> None:
    assert outcome.ok is False and outcome.effect_outcome == "not_started"
    # The registry marks every entered handler as executed; the structured fact must still win.
    assert _operation_status_for_result(replace(outcome, handler_executed=True)) == TOOL_OPERATION_FAILED


def test_file_source_outside_owner_scope_is_terminal_not_unknown(owner_home, tmp_path):
    outside = tmp_path / "workspace-a2"
    outside.mkdir()
    outcome = _tool(owner_home).execute({"action": "open", "url": outside.as_uri()})
    assert outcome.error_code == "TOOL_PERMISSION_DENIED"
    _assert_terminal_not_started(outcome)
    assert ws.registry.ids() == []


def test_network_gate_refusal_on_open_is_terminal_not_unknown(owner_home, monkeypatch):
    def refuse(tool, url, resolver, settings=None):
        return ToolHandlerOutcome(tool, False, "网络安全检查失败", error_code="NETWORK_SAFETY_DENIED",
                                  reported_error_code="NETWORK_SAFETY_DENIED")

    monkeypatch.setattr(wt, "_network_safety_error", refuse)
    outcome = _tool(owner_home).execute({"action": "open", "url": "https://example.com/feed?since=<next>"})
    assert outcome.reported_error_code == "NETWORK_SAFETY_DENIED"
    _assert_terminal_not_started(outcome)
    assert ws.registry.ids() == []


@pytest.mark.parametrize("probe_sent", [False, None])
def test_envelope_refusal_is_not_started_only_when_no_probe_request_was_sent(owner_home, monkeypatch, probe_sent):
    # Live: an adapter source opened outside a named Audit was refused as SOURCE_ENVELOPE_INVALID
    # ("动态来源适配器缺少准确的命名 Audit 归属") before any request, yet stored as UNKNOWN.  A probe that
    # did reach the source must keep the unknown contract (see the credential redirect test).
    envelope = {"mode": "adapter", "valid": False, "invalid_reason": "动态来源适配器缺少准确的命名 Audit 归属"}
    if probe_sent is not None:
        envelope["probe_sent"] = probe_sent
    monkeypatch.setattr(wt, "_probe_envelope_for", lambda *args, **kwargs: dict(envelope))
    outcome = _tool(owner_home).execute({"action": "open", "url": "https://example.com/feed?since=<next>"})
    assert outcome.ok is False and outcome.reported_error_code == "SOURCE_ENVELOPE_INVALID"
    if probe_sent is False:
        _assert_terminal_not_started(outcome)
    else:
        assert outcome.effect_outcome == ""
    assert ws.registry.ids() == []


def test_adapter_probe_without_audit_identity_sends_nothing():
    def never(*args, **kwargs):
        raise AssertionError("no source request may be sent")

    state = SimpleNamespace(audit_root_task_id="", prepare_root_task_id="")
    envelope = wt._probe_adapter_source_envelope(never, state, request_facts={}, adapter_facts={}, audit_id="")
    assert envelope["valid"] is False and envelope["probe_sent"] is False


def test_audit_coordinator_open_refusal_is_terminal_not_unknown(owner_home):
    tool = _tool(owner_home, {AUDIT_ATTR: True, CONVERSATION_REQUEST_ID_ATTR: "audit-refusal-test"})
    outcome = tool.execute({"action": "open", "url": "https://example.com/feed?since=<next>"})
    assert outcome.reported_error_code == "AUDIT_COORDINATOR_SOURCE_ACTION_FORBIDDEN"
    _assert_terminal_not_started(outcome)


def test_err_keeps_body_and_only_passes_known_effect_values():
    passed = wt._err("拒绝", "TOOL_PERMISSION_DENIED", effect_outcome="not_started")
    assert passed.effect_outcome == "not_started" and '"effect_outcome": "not_started"' in passed.output
    ignored = wt._err("拒绝", "TOOL_PERMISSION_DENIED", effect_outcome="committed")
    assert ignored.effect_outcome == "" and '"effect_outcome": "committed"' in ignored.output
