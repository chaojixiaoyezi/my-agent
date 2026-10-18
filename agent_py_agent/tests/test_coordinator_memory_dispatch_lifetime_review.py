"""非作者只读生产复核：所有故障/旁观仅在本临时pytest夹具，原handler/source/领域仍实际执行。"""

from contextlib import contextmanager

import pytest

from agent_py_agent.agent.common import global_write_commit
from agent_py_agent.agent.common.owner_write_errors import OwnerManagementIngressUnavailable
from agent_py_agent.agent.gateway_parts import control_operation_service as ops
from agent_py_agent.tests.test_coordinator_agents_domain import submit
from agent_py_agent.tests.test_coordinator_control_forward import cold


@pytest.mark.parametrize("cold,group", [("local", False), ("feishu", True)], indirect=["cold"])
def test_exact_command_scope_global_borrow_and_enabled_event_until_complete(
    cold, monkeypatch, group  # noqa: F811 复用原模块导出的pytest夹具
):
    from agent_py_agent.agent.gateway_parts import coordinator_memory_candidates as domain
    from agent_py_agent.agent.gateway_parts import coordinator_owner_management as parent
    from agent_py_agent.agent.gateway_parts import event_points, plugin_panels_http

    cold.server.control_plane.config.plugin_events_enabled = True
    entered, completing, events, sinks = [], [], [], []
    run, complete, event = (
        domain.execute_coordinator_memory_candidates,
        parent.complete_owner_management_operation,
        event_points.command_completed_from_resources,
    )

    def live(source):
        binding = source._management_memory_binding
        current = global_write_commit._SCOPE.get()
        assert binding.evidence.active and current.active and current.commit.__self__ is source
        assert len(binding.holder._entries) == 1
        assert next(iter(binding.holder._entries.values())).borrowers == 1
        return current

    @contextmanager
    def observe(source, command, scope):
        assert command is source.http_origin[3] and scope is source.http_origin[5]
        with run(source, command, scope) as result:
            entered.append((source, live(source)))
            yield result

    def finish(source, result, **kwargs):
        assert entered[-1][0] is source and source._receipt_state == "executing"
        live(source)
        receipt = complete(source, result, **kwargs)
        assert receipt.state == "completed" and source._receipt_state == "completed"
        completing.append(receipt.operation_id)
        live(source)
        return receipt

    def completed(config, home, receipt, **kwargs):
        source = entered[-1][0]
        live(source)
        assert source._receipt_state == receipt.state == "completed"
        assert home.owner_id == source._management_memory_binding.home.owner_id
        events.append(receipt.operation_id)
        return event(config, home, receipt, **kwargs)

    monkeypatch.setattr(domain, "execute_coordinator_memory_candidates", observe)
    monkeypatch.setattr(parent, "complete_owner_management_operation", finish)
    monkeypatch.setattr(event_points, "command_completed_from_resources", completed)
    monkeypatch.setattr(
        plugin_panels_http,
        "publish_plugin_event",
        lambda server, owner, fact: sinks.append((server, owner, fact)),
    )
    response = submit(cold, "/memory candidates", "exact-domain", as_group=group)
    assert response[0] == 200 and response[1]["ok"], response
    assert len(entered) == len(completing) == len(events) == len(sinks) == 1
    source, scope = entered[0]
    assert not scope.active and not source._management_memory_binding.evidence.active
    assert (
        not source.lease._commit_pins
        and next(iter(source._management_memory_binding.holder._entries.values())).borrowers == 0
    )
    assert sinks[0][0] is cold.server and sinks[0][2].type == "command_executed"
    assert sinks[0][2].facts == {"command": "/memory", "operation_id": events[0], "state": "ok"}
    path = ops.gateway_control_operation_path(cold.server.paths, events[0])
    raw = path.read_bytes()
    assert submit(cold, "/memory candidates", "exact-domain", as_group=group) == response
    assert path.read_bytes() == raw and len(entered) == len(events) == len(sinks) == 1
    assert cold.server.agent is None and cold.constructions == []


@pytest.mark.parametrize("native", [False, True])
def test_actual_caller_error_keeps_unknown_or_native_and_never_reexecutes(
    cold, monkeypatch, native  # noqa: F811 复用原模块导出的pytest夹具
):
    from agent_py_agent.agent.gateway_parts import coordinator_memory_candidates as domain
    from agent_py_agent.agent.gateway_parts import coordinator_owner_management as parent
    from agent_py_agent.agent.gateway_parts import event_points

    sources, errors, unknown_writes, events, attempts = [], [], [], [], []
    original, dispatch, writer = (
        domain._run_candidate_action,
        parent.execute_coordinator_owner_management,
        ops._write_control_operation_json,
    )
    ordinary = OSError("synthetic ordinary domain failure")
    cause = ValueError("synthetic original cause")
    ordinary.__cause__ = cause

    def fail(source, *args):
        sources.append(source)
        attempts.append(True)
        if not native:
            raise ordinary
        ingress = source.http_origin[7]
        active, ingress.active = ingress.active, False
        try:
            source._require_current()
        finally:
            ingress.active = active
        pytest.fail("actual original ingress must reject")

    def observed(*args, **kwargs):
        try:
            return dispatch(*args, **kwargs)
        except BaseException as error:
            errors.append(error)
            raise

    def write(path, payload, **kwargs):
        if payload["state"] == "terminal_unknown":
            unknown_writes.append(True)
            assert sources[-1]._management_memory_binding.evidence.active
        return writer(path, payload, **kwargs)

    monkeypatch.setattr(domain, "_run_candidate_action", fail)
    monkeypatch.setattr(parent, "execute_coordinator_owner_management", observed)
    monkeypatch.setattr(ops, "_write_control_operation_json", write)
    monkeypatch.setattr(
        event_points, "command_completed_from_resources", lambda *_a, **_k: events.append(True)
    )
    response = submit(cold, "/memory candidates", "caller-failure")
    assert response[0] == 503 and len(sources) == 1 and len(errors) == 1, response
    source = sources[0]
    current = ops.read_gateway_control_operation(
        source.paths, source.receipt.operation_id, read_only=True
    )
    if native:
        assert isinstance(errors[0], OwnerManagementIngressUnavailable)
        assert source._native_source_failure is errors[0]
        assert current.state == "executing" and unknown_writes == []
    else:
        assert (
            errors[0] is ordinary and ordinary.__cause__ is cause and ordinary.__context__ is None
        )
        assert current.state == "terminal_unknown" and unknown_writes == [True]
    assert (
        not events
        and not source._management_memory_binding.evidence.active
        and not source.lease._commit_pins
    )
    assert global_write_commit._SCOPE.get() is None
    assert next(iter(source._management_memory_binding.holder._entries.values())).borrowers == 0
    path = ops.gateway_control_operation_path(source.paths, source.receipt.operation_id)
    raw = path.read_bytes()
    status, replay = submit(cold, "/memory candidates", "caller-failure")
    assert status == 202 and replay["control_state"] == current.state, replay
    assert path.read_bytes() == raw and attempts == [True] and len(errors) == 1 and events == []
