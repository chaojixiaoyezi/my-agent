"""界面控制器绑定读取原线程空间，真实客户端往返保持授权和幂等。"""
from agent_py_agent.agent.gateway_parts.business_host_ports import wire_business_host_ports
from agent_py_agent.tests.test_acceptance_business_error_transport import gateway as gateway


def test_existing_background_controller_can_bind_only_its_space(gateway):
    agent, client = gateway
    wire_business_host_ports(agent)
    headers = agent.gateway_network_headers
    agent.gateway_network_headers = lambda: {**headers(), 'X-User-Id': 'surface-owner', 'X-Channel': 'feishu'}

    def write(action, params, **options):
        result = client.send(client.prepare(action, params, **options))
        assert result['ok'], result
        return result['data']

    first = write('spaces.create', {'name': 'controller-space'})['space_id']
    second = write('spaces.create', {'name': 'different-space'})['space_id']
    worker = write('background_agents.create', {'space_id': first, 'name': 'dedicated-controller'})['agent']
    before = client.call('background_agents.get', {'agent_id': worker['agent_id']})
    created = write('interfaces.bind', {'surface_ref': 'web', 'name': 'panel', 'space_ids': [first],
                    'default_space_id': first, 'controller': {'mode': 'global'}})
    binding_id = created['binding_id']
    dedicated = client.prepare('interfaces.update', {'binding_id': binding_id,
                               'controller': {'mode': 'agent', 'agent_id': worker['agent_id']}},
                               expected_revision=created['revision'])
    bound = client.send(dedicated)
    assert bound['ok'], bound
    assert bound['effect_outcome'] == 'committed'
    replay = client.send(dedicated)
    assert replay['ok'] and replay['data'] == bound['data']
    resolved = client.call('interfaces.resolve', {'binding_id': binding_id})
    assert resolved['ok'] and resolved['data']['controller_ref'] == worker['agent_id']
    assert resolved['data']['effective_spaces'] == [first]

    wrong = client.send(client.prepare('interfaces.update', {'binding_id': binding_id,
                         'space_ids': [second], 'default_space_id': second},
                         expected_revision=bound['data']['revision']))
    assert not wrong['ok'] and wrong['error']['code'] == 'SPACE_SCOPE_MISMATCH'
    assert wrong['effect_outcome'] == 'not_started'
    assert client.call('interfaces.resolve', {'binding_id': binding_id})['data'] == resolved['data']

    write('interfaces.unbind', {'binding_id': binding_id}, expected_revision=bound['data']['revision'])
    assert client.call('background_agents.get', {'agent_id': worker['agent_id']})['data'] == before['data']
    assert client.call('spaces.get', {'space_id': first})['ok']
