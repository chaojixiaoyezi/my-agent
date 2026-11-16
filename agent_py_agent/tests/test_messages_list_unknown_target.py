# 模块用途: business_execution messages.list 的 target 指向不存在的节点ID时，返回确定的 OBSERVATION_NOT_FOUND
#   (404、未执行、不可原样重试)和取得真实ID的读口，而不是把 KeyError 包成 503 HOST_NOT_READY。只用 tmp 隔离 Store。
from __future__ import annotations

import pytest

from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.agent.gateway_parts.business_api import _error_code, _error_details
from agent_py_agent.tests.test_business_v12_f7_observation import fixture


@pytest.mark.parametrize('kind', ['background_main', 'controller'])
def test_unknown_message_target_is_not_found_not_host_not_ready(tmp_path, kind):
    case = fixture(tmp_path)
    with pytest.raises(BusinessError) as caught:
        case.service.query('messages.list', {'target': {'kind': kind, 'id': 'bg-main'}}, actor=case.actor)
    assert caught.value.error_code == 'OBSERVATION_NOT_FOUND'
    assert _error_code(caught.value) == ('OBSERVATION_NOT_FOUND', 404, 'not_started')
    details = {item['key']: item['value'] for item in _error_details(caught.value)}
    assert details['reason'] == 'MESSAGE_TARGET_NOT_FOUND' and 'background_agents.list' in details['next_action']


def test_known_background_target_still_lists_messages(tmp_path):
    case = fixture(tmp_path)
    row = case.store.messages.append({'thread_id': 'background', 'role': 'assistant', 'content': '可见回答'})
    page = case.service.query('messages.list', {'target': {'kind': 'background_main', 'id': 'background'}},
                              actor=case.actor)
    assert [item['message_id'] for item in page['items']] == [row.message_id]
