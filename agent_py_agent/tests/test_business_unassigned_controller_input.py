"""未指定空间的输入经原任务主链交给唯一总控，绑定空间的输入保持原归属。"""
import json

from agent_py_agent.cli.space_commands import BusinessClient
from agent_py_agent.tests.test_mega_business_api_actions import (
    background_http as background_http,
)
from agent_py_agent.tests.test_mega_business_api_actions import (
    create_background,
    native_request,
)


def test_unassigned_input_reaches_controller_once_without_guessing_space(background_http):
    params, _ = create_background(background_http)

    def transport(path, request, **kwargs):
        status, _, raw = native_request(background_http, 'POST', path, request)
        return status, json.loads(raw)

    client = BusinessClient(transport)
    second = client.send(client.prepare('spaces.create', {'name': '第二个候选空间'},
        idempotency_key='second-unassigned-space'))
    assert second['ok'], second
    request = client.prepare('tasks.submit', {'space_id': None, 'instruction': '请判断这条输入归属哪个空间。'},
        idempotency_key='unassigned-controller-input')
    first, replay = client.send(request), client.send(request)
    assert first['ok'] and replay['ok'], first
    assert first['data']['task_id'] == replay['data']['task_id']
    controller = client.call('controllers.get', {})
    assert controller['ok'] and controller['data']['available']
    [services] = [item for item in background_http[0]._business_services.values()
                  if item.owner_id.endswith('/business-alice')]
    task = services.host.conversation_store.session_tasks.require(first['data']['task_id'])
    assert task.target_thread_id == controller['data']['thread_id']
    assert task.status == 'queued' and task.metadata['business_context']['space_id'] is None
    assert task.body_guidance_id
    explicit = client.send(client.prepare('tasks.submit',
        {'space_id': params['space_id'], 'instruction': '已绑定空间输入'},
        idempotency_key='explicit-controller-input'))
    assert explicit['ok'], explicit
    bound = services.host.conversation_store.session_tasks.require(explicit['data']['task_id'])
    assert bound.target_thread_id == task.target_thread_id
    assert bound.metadata['business_context']['space_id'] == params['space_id']
