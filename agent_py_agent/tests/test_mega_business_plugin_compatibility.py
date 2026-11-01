"""W13生产入口合同；不把静态schema/替身当成跨片验收。"""
import pytest

from agent_py_agent.agent.plugin_host import composition
from agent_py_agent.agent.plugin_host.authorization import _requires_execution
from agent_py_agent.agent.plugin_host.business_action_specs import business_action_specs
from agent_py_agent.agent.plugin_host.contracts import Call, HostDomainPorts
from agent_py_agent.tests.test_plugin_host_gateway_composition import production_call
from agent_py_agent.tests.test_plugin_host_gateway_composition import (
    production_host as production_host,
)


@pytest.mark.parametrize("spec", list(business_action_specs().values()), ids=lambda s: s.action)
def test_all_business_actions_select_explicit_business_domain(spec):
    assert composition.domain_name(spec.plugin_action) == "business"
    assert not composition.domain_ready(spec.plugin_action, HostDomainPorts())


@pytest.mark.parametrize("spec", list(business_action_specs().values()), ids=lambda s: s.action)
def test_business_execute_uses_original_restricted_execution_gate(spec):
    call = Call("plugin_host_call.v2", spec.plugin_action, {})
    assert _requires_execution(call) is (spec.metadata["effect"] == "execute")


def test_business_stream_is_transport_only_and_legacy_tasks_unchanged():
    from agent_py_agent.agent.plugin_host.action_specs import action_spec
    assert composition.domain_name("business.events.stream") == "business"
    assert action_spec("business.events.stream").mode == "stream"
    assert composition.domain_name("tasks.list") == "observations"
    assert action_spec("tasks.list").capabilities == {"tasks.read"}


def test_new_business_caps_do_not_gift_old_execution_or_human_gate():
    from agent_py_agent.agent.plugin_host.action_specs import required_capabilities
    for spec in business_action_specs().values():
        required = required_capabilities(spec.plugin_action, {})
        if spec.metadata['effect'] == 'execute':
            assert 'execute' in required
    assert 'interactions.respond' in required_capabilities('business.approvals.decide', {})


def business_heartbeat():
    return {'stream_item': 'heartbeat', 'event_id': 'event-one', 'cursor': 'opaque-cursor',
        'event_type': 'heartbeat', 'occurred_at': '2026-11-01T12:00:00Z', 'node': None,
        'space_id': None, 'task_id': None, 'run_id': None, 'attempt_id': None, 'operation_id': None,
        'payload': {'server_time': '2026-11-01T12:00:00Z'}, 'coverage': 'unknown'}


def test_business_typed_event_uses_original_bounded_sse_pump(monkeypatch):
    """运输叶子替身；W14真人scope/续传联测仍须独立验收。"""
    from types import SimpleNamespace

    from agent_py_agent.agent.plugin_host import http_transport as transport
    event = business_heartbeat()
    monkeypatch.setattr(transport, 'stream_events', lambda *args: (row for row in [event]))
    authorized = SimpleNamespace(call=SimpleNamespace(action='business.events.stream'))
    session = transport.StreamSession(authorized, HostDomainPorts())
    session.ready.set()
    session.produce()
    assert session.events.get_nowait() == ('business', event)
    assert session.events.get_nowait() is None
    assert session.events.maxsize == 8
    assert (transport.STREAM_MAX_COUNT, transport.INSTALL_STREAM_MAX_COUNT, transport.HEARTBEAT_SECONDS) == (16, 2, 15)


def test_business_approval_normalization_is_reissued_by_original_issuer(production_host):
    import json
    from dataclasses import replace

    from agent_py_agent.agent.plugin_host import authorization
    from agent_py_agent.agent.plugin_host.contracts import decode_call
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    server, token, *_ = production_host
    issuer = server.plugin_host.composition.issuer
    original = issuer.authorize(issuer.ingress(token), decode_call(json.dumps({'schema': 'plugin_host_call.v2',
        'action': 'business.approvals.decide', 'payload': {'approval_id': 'approval-one', 'decision': 'approved',
            'expected_revision': 1, 'response_ticket': 'opaque-ticket'}, 'idempotency_key': 'business-approval-key'}).encode()))
    normalize = getattr(authorization, 'canonical_business_approval', None)
    assert callable(normalize)
    canonical = normalize(original)
    authorization.revalidate_call(canonical)
    assert canonical is not original and canonical.call.action == 'interactions.approvals.respond'
    assert canonical.call.payload['interaction_id'] == 'approval-one'
    assert canonical.effective_owner == original.effective_owner
    with pytest.raises(HostAPIError):
        normalize(replace(original))


def test_actual_business_plugin_uses_shared_space_service(production_host):
    from types import SimpleNamespace

    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_resolver import home_paths_with_owner
    server, _, owner, *_ = production_host
    server.agent = SimpleNamespace(config=AgentConfig(business_spaces_enabled=True), home_paths=home_paths_with_owner(home_paths(owner.root), owner))
    created = production_call(production_host, 'business.spaces.create', {'name': '插件空间'}, key='plugin-business-create')
    fetched = production_call(production_host, 'business.spaces.get', {'space_id': created['space_id']})
    assert fetched['space']['name'] == '插件空间'
    assert 'spaces.create' in production_call(production_host, 'business.system.capabilities')['supported_actions']


@pytest.mark.parametrize('enabled', [None, True, False], ids=['cold-gateway', 'bound-enabled', 'bound-disabled'])
def test_business_host_wiring_does_not_require_a_warm_agent(production_host, enabled):
    from types import SimpleNamespace

    from agent_py_agent.agent.plugin_host.business_action_specs import business_plugin_actions
    server, _, owner, *_ = production_host
    if enabled is not None:
        server.agent = SimpleNamespace(config=SimpleNamespace(business_spaces_enabled=enabled))
    backend = composition.business_port(server.plugin_host.composition.ports)
    before = sorted(path.relative_to(owner.root) for path in owner.root.rglob('*'))
    readiness = {action: backend.ready(action) for action in business_plugin_actions()}
    assert readiness == dict.fromkeys(business_plugin_actions(), enabled is not False)
    assert not backend.ready('business.unregistered.action')
    assert backend._services == {}
    assert sorted(path.relative_to(owner.root) for path in owner.root.rglob('*')) == before
    if enabled is None:
        assert server.agent is None


def test_business_host_wiring_requires_original_gateway_root(production_host, monkeypatch):
    from agent_py_agent.agent.plugin_host.business_action_specs import business_plugin_actions
    backend = composition.business_port(production_host[0].plugin_host.composition.ports)
    monkeypatch.setattr(backend.resources, 'root', None)
    assert not any(backend.ready(action) for action in business_plugin_actions())
