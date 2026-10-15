"""原凭据已保存后，输入发布或回调响应失败的同卡幂等恢复。"""
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import input_dependencies as dep
from agent_py_agent.agent.delivery import ChannelCapabilities
from agent_py_agent.agent.user_space import task_credentials as tc
from agent_py_agent.tests.test_ingestion_task_credential_scope import _active, _agent


# LLM: 真实Agent/活动任务/输入账与签名沿产品入口；只替换飞书卡片运输。
# 函数用途: 构造私人或群卡，不访问真实租户，不把合成秘密交给模型。
def _card(tmp_path, kind):
    agent = _agent(tmp_path, kind)
    _active(agent)
    target = agent.config.my_agent_owner_id if kind == 'group' else 'oc_private_synthetic'
    agent.channel_registry.register_adapter('feishu', SimpleNamespace(send_control_card=lambda *_a, **_k:
        {'message_id':'om_card', 'chat_id':target}), capabilities=ChannelCapabilities(control_cards=True, proactive=True))
    ref, _ = tc.request_credential_card(agent, url='https://example.com', authentication='bearer', call_id='replay')
    path = tc.credential_path(agent.home_paths.owner_home_dir, ref)
    row = json.loads(path.read_text())
    body = {'reference':row['reference'], 'decision':'submit', 'message_id':'om_card', 'chat_id':target,
        'fields':{'secret':'SYNTHETIC_FIRST'}}
    if kind == 'group':
        body['submitter'] = {'actor_id':row['actor_id'], 'chat_id':target, 'current_group_member':True}
    return agent, path, row, body


@pytest.mark.parametrize('kind', ['user', 'group'])
@pytest.mark.parametrize('decision', ['submit', 'cancel'])
@pytest.mark.parametrize('failure', ['before_publish', 'after_publish', 'lost_response'])
def test_same_card_replays_saved_decision_without_overwriting_secret(tmp_path, monkeypatch, kind, decision, failure):
    agent, path, payload, body = _card(tmp_path, kind)
    body['decision'] = decision
    original = dep.publish_credential_resolution
    def failed_publish(*args):
        if failure == 'after_publish':
            original(*args)
        raise OSError(28, 'SYNTHETIC_FAILURE')
    if failure != 'lost_response':
        monkeypatch.setattr(dep, 'publish_credential_resolution', failed_publish)
        with pytest.raises(OSError):
            tc.submit_credential_card(agent, payload, body)
        monkeypatch.setattr(dep, 'publish_credential_resolution', original)
    else:
        tc.submit_credential_card(agent, payload, body)
    saved = path.read_text()
    body['fields'] = {'secret':'SYNTHETIC_DIFFERENT_MUST_IGNORE'}
    assert tc.submit_credential_card(agent, payload, body)['ok']
    assert path.read_text() == saved
    assert not agent.conversation_store.tasks.load(agent._current_run_params.task_id).input_dependency_refs


@pytest.mark.parametrize('kind', ['user', 'group'])
@pytest.mark.parametrize('attack', ['opposite', 'generation', 'root', 'cancelled_task', 'expired_pending', 'wrong_actor'])
def test_replay_does_not_expand_scope_or_replace_decision(tmp_path, monkeypatch, kind, attack):
    agent, path, payload, body = _card(tmp_path, kind)
    def failed_publish(*_a):
        raise OSError(28, 'SYNTHETIC_FAILURE')
    monkeypatch.setattr(dep, 'publish_credential_resolution', failed_publish)
    with pytest.raises(OSError):
        tc.submit_credential_card(agent, payload, body)
    if attack == 'opposite':
        body['decision'] = 'cancel'
    elif attack == 'generation':
        payload = {**payload, 'generation':payload['generation'] + 1}
    elif attack == 'root':
        payload = {**payload, 'scope':{**payload['scope'], 'root_task_id':'different-root'}}
    elif attack == 'cancelled_task':
        dep.cancel_task_inputs(agent, payload['scope']['root_task_id'])
    elif attack == 'expired_pending':
        monkeypatch.setattr(tc.time, 'time', lambda:payload['expires_at'] + 1)
    else:
        if kind == 'user':
            # 私人错actor由上游MAC/actor合同拒绝；此处改原记录提交者以核验终态实际提供者门。
            row = json.loads(path.read_text())
            row['submitter_actor_id'] = 'other-actor'
            with tc._credential_mutation(agent, path) as admission:
                tc._save_credential_row(admission, path, row)
        else:
            body['submitter']['actor_id'] = 'other-actor'
    saved = path.read_text()
    with pytest.raises(ValueError):
        tc.submit_credential_card(agent, payload, body)
    assert path.read_text() == saved


@pytest.mark.parametrize('kind', ['user', 'group'])
def test_expired_settled_decision_only_confirms_without_republishing(tmp_path, monkeypatch, kind):
    agent, path, payload, body = _card(tmp_path, kind)
    tc.submit_credential_card(agent, payload, body)
    saved = path.read_text()
    monkeypatch.setattr(tc.time, 'time', lambda:payload['expires_at'] + 1)
    monkeypatch.setattr(dep, 'publish_credential_resolution', lambda *_a:pytest.fail('过期终态不能再发布工作'))
    assert tc.submit_credential_card(agent, payload, body)['ok']
    assert path.read_text() == saved
