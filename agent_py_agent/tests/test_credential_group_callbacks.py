"""合成群凭据回调：签名、当前成员和私人锁隔离。"""
import time
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.adapter import private_controls
from agent_py_agent.agent.gateway_parts import private_interaction_service as service
from agent_py_agent.agent.user_space.interaction_refs import (
    sign_interaction_reference,
    verify_interaction_reference,
)
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, ensure_owner_home


@pytest.fixture
def group_case(tmp_path):
    identity = OwnerIdentity.provider_group('feishu', 'oc_group')
    home = ensure_owner_home(tmp_path, identity)
    payload = dict(kind='task_credential', owner=asdict(identity), channel='feishu', actor_id='ou_creator',
                   expires_at=time.time()+60, presentation={'kind':'group','chat_id':'oc_group'},
                   submitter_policy='current_group_member')
    ref = sign_interaction_reference(home.home_dir, payload)
    return tmp_path, payload, ref


@pytest.mark.parametrize('actor', ['ou_creator', 'ou_member'])
def test_group_member_signature_keeps_original_actor(group_case, actor):
    root, payload, ref = group_case
    facts = dict(actor_id=actor,chat_id='oc_group',current_group_member=True)
    assert verify_interaction_reference(root,ref,channel='feishu',actor_id=actor,submitter=facts)==payload
    assert payload['actor_id']=='ou_creator'


@pytest.mark.parametrize('facts', [None, {}, {'actor_id':'ou_member','chat_id':'oc_other','current_group_member':True},
    {'actor_id':'ou_other','chat_id':'oc_group','current_group_member':True},
    {'actor_id':'ou_member','chat_id':'oc_group','current_group_member':False}])
def test_group_rejects_untrusted_wrong_group_departed_actor(group_case, facts):
    root, _, ref = group_case
    with pytest.raises(ValueError):
        verify_interaction_reference(root,ref,channel='feishu',actor_id='ou_member',submitter=facts)


@pytest.mark.parametrize('kind', ['tool_approval','model_configuration'])
def test_group_presentation_cannot_open_other_controls(group_case,kind):
    root,payload,_=group_case
    home=ensure_owner_home(root,OwnerIdentity(**payload['owner']))
    ref=sign_interaction_reference(home.home_dir,{**payload,'kind':kind})
    with pytest.raises(ValueError):
        verify_interaction_reference(root,ref,channel='feishu',actor_id='ou_creator',
            submitter=dict(actor_id='ou_creator',chat_id='oc_group',current_group_member=True))


def test_group_gateway_skips_personal_setup_only_for_valid_group(group_case,monkeypatch):
    root,payload,ref=group_case
    base=SimpleNamespace(home_paths=SimpleNamespace(root=root))
    monkeypatch.setattr(service,'private_interaction_lock_error',lambda *a: pytest.fail('群输入不得套个人锁'))
    monkeypatch.setattr(service,'_resolve_request_agent_for_owner',lambda *a:base)
    from agent_py_agent.agent.user_space import task_credentials
    monkeypatch.setattr(task_credentials,'submit_credential_card',lambda agent,payload,body:{'ok':True})
    body=dict(reference=ref,decision='submit',message_id='om_card',chat_id='oc_group',fields={'secret':'synthetic'},
        submitter=dict(actor_id='ou_member',chat_id='oc_group',current_group_member=True))
    assert service.resolve_private_interaction(base,body,channel='feishu',actor_id='ou_member')['ok']
    body['chat_id']='oc_forward'
    with pytest.raises(ValueError):
        service.resolve_private_interaction(base,body,channel='feishu',actor_id='ou_member')


def test_private_still_requires_setup(group_case,monkeypatch):
    root,payload,_=group_case
    home=ensure_owner_home(root,OwnerIdentity(**payload['owner']))
    payload.pop('presentation')
    payload.pop('submitter_policy')
    ref=sign_interaction_reference(home.home_dir,payload)
    base=SimpleNamespace(home_paths=SimpleNamespace(root=root))
    monkeypatch.setattr(service,'private_interaction_lock_error',lambda *a:'IM_PASSWORD_SETUP_REQUIRED')
    result=service.resolve_private_interaction(base,dict(reference=ref,decision='submit',message_id='om',chat_id='oc'),
        channel='feishu',actor_id='ou_creator')
    assert result['error_code']=='IM_PASSWORD_SETUP_REQUIRED'


def test_membership_uses_all_pages_and_current_open_id(monkeypatch):
    from agent_py_agent.agent.adapter import feishu
    calls=[]
    responses=iter([{'code':0,'data':{'items':[{'member_id':'ou_creator'}],'has_more':True,'page_token':'next'}},
                    {'code':0,'data':{'items':[{'member_id':'ou_member'}],'has_more':False}}])
    def api(method,path,body,token,**kwargs):
        calls.append((method,path,body,token))
        return next(responses)
    monkeypatch.setattr(feishu,'_feishu_msg_api',api)
    assert private_controls._current_group_member(SimpleNamespace(_get_tenant_access_token=lambda:'token'),'oc_group','ou_member')
    assert 'page_token=next' in calls[1][1]
    assert all(call[0]=='GET' and call[2]=={} for call in calls)


@pytest.mark.parametrize('transport', ['sdk','webhook'])
@pytest.mark.parametrize('actor', ['ou_creator','ou_member','ou_departed'])
def test_real_callback_shape_to_authenticated_loopback(group_case,monkeypatch,transport,actor):
    from agent_py_agent.agent.adapter.feishu_card import (
        extract_card_action,
        extract_webhook_card_action,
    )
    root,_,ref=group_case
    value=dict(kind='my_agent_task_credential',reference=ref,decision='submit',actor_id='forged')
    event=dict(action=dict(value=value,form_value={'secret':'synthetic-never-receipt'}),
               operator=dict(open_id=actor),context=dict(open_message_id='om_real',open_chat_id='oc_group'))
    if transport=='sdk':
        event=SimpleNamespace(**{key:SimpleNamespace(**item) for key,item in event.items()})
        norm=extract_card_action(SimpleNamespace(event=event))
    else:
        norm=extract_webhook_card_action({'event':event})
    monkeypatch.setattr(private_controls,'_current_group_member',lambda a,c,u:u!='ou_departed')
    monkeypatch.setattr(private_controls.GatewayClientCredentials,'headers',lambda self:{'Authorization':'Bearer synthetic'})
    bodies=[]
    class Response:
        def __enter__(self):
            return self
        def __exit__(self,*args):
            return False
        def read(self,*args):
            return b'{"ok":true,"message":"saved"}'
    def request(req,timeout):
        import json
        body=json.loads(req.data)
        bodies.append(body)
        assert req.headers['X-user-id']==actor
        try:
            verify_interaction_reference(root,body['reference'],channel='feishu',actor_id=actor,submitter=body['submitter'])
        except ValueError:
            raise ValueError('rejected') from None
        return Response()
    monkeypatch.setattr(private_controls,'open_authenticated_request',request)
    adapter=SimpleNamespace(my_agent_home=root,_gateway_port=8420)
    result=private_controls.tool_approval_card_action(adapter,norm)
    if actor != 'ou_departed':
        assert bodies[0]['submitter']['actor_id']==actor
    else:
        assert not bodies
    assert result['toast']['type']==('error' if actor=='ou_departed' else 'success')
    assert 'synthetic-never-receipt' not in str(result)


def test_group_signature_tamper_does_not_change_scope(group_case):
    import base64
    import json
    root,payload,ref=group_case
    prefix,_,signature=ref.split('.')
    payload['presentation']['chat_id']='oc_other'
    encoded=base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
    with pytest.raises(ValueError):
        verify_interaction_reference(root,f'{prefix}.{encoded}.{signature}',channel='feishu',actor_id='ou_member',
            submitter=dict(actor_id='ou_member',chat_id='oc_other',current_group_member=True))


@pytest.mark.parametrize('reference', ['bad.x.y', 'ir1.'+'x'*8192+'.'+'0'*64, 'ir1.e30.short', 'ir1.W10.'+'0'*64])
def test_group_hint_rejects_oversize_or_wrong_prefix(reference):
    assert not private_controls._group_reference_hint(reference)


@pytest.mark.parametrize('mode', ['pages','loop','timeout','permission','absent'])
def test_membership_lookup_budget_and_negative_facts(monkeypatch,mode):
    from agent_py_agent.agent.adapter import feishu
    calls=[]
    clock=[0.0]
    monkeypatch.setattr(private_controls.time,'monotonic',lambda:clock[0])
    def api(method,path,body,token,*,timeout):
        assert 0 < timeout <= 1.5
        calls.append(path)
        if mode=='timeout':
            clock[0]=2
        if mode=='permission':
            return {'code':99991672,'msg':'sensitive-denial'}
        return {'code':0,'data':{'items':[], 'has_more':mode!='absent',
            'page_token':'same' if mode=='loop' else str(len(calls))}}
    monkeypatch.setattr(feishu,'_feishu_msg_api',api)
    adapter=SimpleNamespace(_get_tenant_access_token=lambda:'token')
    if mode=='absent':
        assert private_controls._current_group_member(adapter,'oc_group','ou_member') is False
    else:
        with pytest.raises(private_controls.GroupMembershipError) as caught:
            private_controls._current_group_member(adapter,'oc_group','ou_member')
        expected='IM_GROUP_MEMBERSHIP_BUDGET_EXCEEDED' if mode in {'pages','timeout'} else 'IM_GROUP_MEMBERSHIP_UNAVAILABLE'
        assert caught.value.code==expected
    assert len(calls)<=10
