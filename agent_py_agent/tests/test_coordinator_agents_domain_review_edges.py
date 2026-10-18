# ruff: noqa: F811
# LLM: 独立实际HTTP边界回归，受控异常类型不代物理退出证明。
# 模块用途: 验证原资源引用、部分提交、单发观察器及原异常对象保持。
import json

import pytest

from agent_py_agent.agent.capability.agents_source_repository import AgentsSourceRepository
from agent_py_agent.agent.gateway_parts.control_operation_service import (
    read_gateway_control_operation,
)
from agent_py_agent.agent.gateway_parts.multiprocess_local_control import _LocalControlWriteSource
from agent_py_agent.tests.test_coordinator_agents_domain import cold, submit


@pytest.mark.parametrize("cold", ["feishu"], indirect=True)
@pytest.mark.parametrize("field", ["store", "default"])
def test_actual_factory_resource_reference_substitution_refuses_before_thread(
    cold, monkeypatch, field
):  # noqa: F811
    original = _LocalControlWriteSource.prepare_actor_control_thread
    observed = []

    def changed(source):
        host, home, config, store, threads, storage, default, workspace = (
            source._actor_control_resources
        )
        if field == "store":
            host.conversation_store = object()
        else:
            threads.model_default = lambda: pytest.fail("replaced default must never run")
        try:
            return original(source)
        finally:
            observed.append(source)
            host.conversation_store = store
            threads.model_default = default

    monkeypatch.setattr(_LocalControlWriteSource, "prepare_actor_control_thread", changed)
    status, payload = submit(cold, "/agents show", "ref-swap-" + field)
    assert status == 503, (status, payload)
    source = observed[0]
    assert source._native_source_failure is not None
    row = read_gateway_control_operation(
        cold.server.paths, source.receipt.operation_id, read_only=True
    )
    assert row.state == "executing" and not row.conversation_thread_id


@pytest.mark.parametrize("cold", ["feishu"], indirect=True)
def test_actual_confirm_partial_body_ordinary_error_consumed_without_replay(cold, monkeypatch):  # noqa: F811
    original = AgentsSourceRepository.write_text
    writes = []

    def partial(repo, *args, **kwargs):
        result = original(repo, *args, **kwargs)
        writes.append(repo.owner_home)
        raise OSError("ordinary after real committed body")

    monkeypatch.setattr(AgentsSourceRepository, "write_text", partial)
    status, preview = submit(cold, '/agents write "actual partial remains"', "partial-preview")
    assert status == 200 and preview["ok"]
    status, result = submit(cold, preview["details"]["confirm_command"], "partial-confirm")
    assert (
        status == 200 and not result["ok"] and result["details"]["state"] == "consumed_unknown"
    ), (status, result)
    assert len(writes) == 1 and (writes[0] / "AGENTS.md").read_text() == "actual partial remains"
    record = json.loads(
        (
            writes[0] / "persona" / ("agents-control-" + preview["details"]["nonce"] + ".json")
        ).read_text()
    )
    assert record["state"] == "consumed_unknown"
    row = read_gateway_control_operation(cold.server.paths, result["operation_id"], read_only=True)
    assert row.state == "completed" and row.result["details"]["state"] == "consumed_unknown"
    assert submit(cold, preview["details"]["confirm_command"], "partial-confirm") == (
        status,
        result,
    )
    status, replayed = submit(
        cold, preview["details"]["confirm_command"], "partial-confirm-new-message"
    )
    assert (
        status == 200 and not replayed["ok"] and replayed["error_code"] == "AGENTS_WRITE_REPLAYED"
    )
    assert len(writes) == 1


@pytest.mark.parametrize("cold", ["feishu"], indirect=True)
def test_actual_observer_failure_cannot_undo_completed_or_repeat(cold, monkeypatch):  # noqa: F811
    from agent_py_agent.agent.gateway_parts import plugin_panels_http

    cold.server.control_plane.config.plugin_events_enabled = True
    calls = []

    def observer(server, owner, event):
        calls.append((server, event))
        row = read_gateway_control_operation(
            server.paths, event.facts["operation_id"], read_only=True
        )
        assert row.state == "completed"
        raise RuntimeError("optional observer failed")

    monkeypatch.setattr(plugin_panels_http, "publish_plugin_event", observer)
    status, result = submit(cold, "/agents show", "observer-fail")
    assert status == 200 and result["ok"]
    assert len(calls) == 1 and calls[0][0] is cold.server
    assert submit(cold, "/agents show", "observer-fail") == (status, result)
    assert len(calls) == 1


@pytest.mark.parametrize("cold", ["feishu"], indirect=True)
def test_actual_domain_outer_identity_cause_context_not_source_slot(cold, monkeypatch):  # noqa: F811
    from agent_py_agent.agent.backends.bounded_call import BoundedCallExitUnconfirmedError
    from agent_py_agent.agent.capability import agents_write_service

    outer = BoundedCallExitUnconfirmedError("review-controlled-type", "worker_exit_unconfirmed")
    cause = OSError("original cause")
    context = LookupError("original context")
    seen = []

    def fail(repo, *args, **kwargs):
        try:
            raise context
        except LookupError:
            raise outer from cause

    original = agents_write_service._raise_agents_runtime_error

    def observe(error):
        seen.append(error)
        return original(error)

    monkeypatch.setattr(AgentsSourceRepository, "write_text", fail)
    monkeypatch.setattr(agents_write_service, "_raise_agents_runtime_error", observe)
    status, preview = submit(cold, '/agents write "outer must persist"', "outer-id-preview")
    assert status == 200 and preview["ok"]
    status, result = submit(cold, preview["details"]["confirm_command"], "outer-id-confirm")
    assert status == 503 and seen == [outer]
    assert outer.__cause__ is cause and outer.__context__ is context
