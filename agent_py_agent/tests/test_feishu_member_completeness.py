"""官方成员安全限制/总数矛盾不能形成完整名单或负面离群证明；不连接平台。"""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.adapter import feishu_provider_facts as facts


def pages():
    return [{'items': [{'member_id_type': 'open_id', 'member_id': 'ou_a', 'name': '甲'}],
        'has_more': True, 'page_token': 'next', 'member_total': 2, 'trigger_security_conf_limit': False,
        'security_conf_limit': 100},
        {'items': [{'member_id_type': 'open_id', 'member_id': 'ou_b', 'name': '乙'}],
        'has_more': False, 'member_total': 2, 'trigger_security_conf_limit': False, 'security_conf_limit': 100}]


@pytest.mark.parametrize('fault', ['security_triggered', 'security_string', 'security_int', 'total_string',
    'total_bool', 'total_negative', 'total_short', 'total_large', 'total_changed', 'duplicate_page', 'duplicate_cross_page', 'wrong_type'])
def test_restricted_inconsistent_or_duplicate_members_never_form_complete_snapshot(monkeypatch, fault):
    data = pages()
    if fault.startswith('security'):
        data[0]['trigger_security_conf_limit'] = {'security_triggered': True, 'security_string': 'false', 'security_int': 0}[fault]
    elif fault in {'total_string', 'total_bool', 'total_negative'}:
        data[0]['member_total'] = {'total_string': '2', 'total_bool': True, 'total_negative': -1}[fault]
    elif fault in {'total_short', 'total_large'}:
        for row in data:
            row['member_total'] = 1 if fault == 'total_short' else 3
    elif fault == 'total_changed':
        data[1]['member_total'] = 3
    elif fault == 'duplicate_page':
        data[0]['items'] *= 2
    elif fault == 'duplicate_cross_page':
        data[1]['items'][0]['member_id'] = 'ou_a'
    else:
        data[0]['items'][0]['member_id_type'] = 'union_id'
    iterator = iter(data)
    monkeypatch.setattr(facts, '_read', lambda *_args, **_kwargs: deepcopy(next(iterator)))
    with pytest.raises(facts.FeishuProviderFactError) as failed:
        facts._complete_group_members('synthetic-only', 'oc_synthetic')
    assert failed.value.reason_code == 'FEISHU_GROUP_MEMBERS_INCOMPLETE' and failed.value.stage == 'members'


@pytest.mark.parametrize('optional', ['absent', 'none', 'nonzero_limit', 'missing_later_total'])
def test_optional_fields_do_not_invent_a_limit_or_require_new_provider_fields(monkeypatch, optional):
    data = pages()
    if optional in {'absent', 'none'}:
        for row in data:
            for key in ('member_total', 'trigger_security_conf_limit', 'security_conf_limit'):
                if optional == 'absent':
                    row.pop(key)
                else:
                    row[key] = None
    elif optional == 'missing_later_total':
        data[1].pop('member_total')
    iterator = iter(data)
    monkeypatch.setattr(facts, '_read', lambda *_args, **_kwargs: next(iterator))
    assert facts._complete_group_members('synthetic-only', 'oc_synthetic') == ({'ou_a', 'ou_b'}, {'ou_a': '甲', 'ou_b': '乙'})


@pytest.mark.parametrize('action', ['pause', 'cancel', 'delete'])
@pytest.mark.parametrize('fault', ['restricted', 'count_mismatch', 'duplicate', 'wrong_type'])
def test_incomplete_members_cannot_authorize_departed_creator_cleanup(tmp_path, monkeypatch, action, fault):
    import json
    from dataclasses import replace

    from agent_py_agent.agent.scheduler import creator_access
    from agent_py_agent.tests.test_scheduler_host_actor import create, group_case

    agent, _, actor, tool = group_case(tmp_path)
    job = create(tool)
    agent._current_run_params.host_channel_actor = replace(actor, actor_id='ou_b')
    data = {'items': [{'member_id_type': 'open_id', 'member_id': 'ou_b'}], 'has_more': False, 'member_total': 1}
    if fault == 'restricted':
        data['trigger_security_conf_limit'] = True
    elif fault == 'count_mismatch':
        data['member_total'] = 2
    elif fault == 'duplicate':
        data['items'] *= 2
    else:
        data['items'][0]['member_id_type'] = 'union_id'

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, size):
            assert size == 1_048_577
            return json.dumps({'code': 0, 'data': data}).encode()

    calls = []

    def send(request, *, timeout):
        assert 0 < timeout <= 2
        assert request.full_url.startswith('https://open.feishu.cn/open-apis/im/v1/chats/oc_source/members?')
        calls.append(1)
        return Response()

    monkeypatch.setattr(creator_access, 'open_authenticated_request', send)
    # 当前fixture原授权adapter无token入口，只补同一宿主REST认证；不提供成员或绕过当前actor门。
    original = agent.delivery_service.registry.adapter_for('feishu')
    original._get_tenant_access_token = lambda: 'synthetic-only'
    before = agent.scheduler_repository.store_path.read_bytes()
    result = tool.execute({'action': action, 'job_id': job['job_id'], 'expected_version': job['version']})
    assert not result.ok and result.error_code == 'SCHEDULER_MEMBERSHIP_UNAVAILABLE'
    assert agent.scheduler_repository.store_path.read_bytes() == before and calls == [1]
    assert agent.scheduler_repository.get_job(job['job_id'])['creator'] == {'channel': 'feishu', 'actor_id': 'ou_a'}


@pytest.mark.parametrize('too_large', [False, True])
def test_cleanup_keeps_original_real_read_limit_and_budget(monkeypatch, too_large):
    import json

    from agent_py_agent.agent.conversation.host_actor import HostActorError
    from agent_py_agent.agent.scheduler import creator_access

    base = json.dumps({'code': 0, 'data': {'items': [{'member_id_type': 'open_id', 'member_id': 'ou_b'}],
        'has_more': False, 'member_total': 1}}).encode()
    content = base + b' ' * (1_048_576 + int(too_large) - len(base))

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, size):
            assert size == 1_048_577
            return content[:size]

    def send(_request, *, timeout):
        assert 0 < timeout <= 2
        return Response()

    monkeypatch.setattr(creator_access, 'open_authenticated_request', send)
    agent = SimpleNamespace(delivery_service=SimpleNamespace(registry=SimpleNamespace(adapter_for=lambda _: SimpleNamespace(
        _get_tenant_access_token=lambda: 'synthetic-only'))))
    if too_large:
        with pytest.raises(HostActorError) as failed:
            creator_access.current_group_members(agent, 'oc_synthetic')
        assert failed.value.error_code == 'SCHEDULER_MEMBERSHIP_UNAVAILABLE'
    else:
        assert creator_access.current_group_members(agent, 'oc_synthetic') == {'ou_b'}
