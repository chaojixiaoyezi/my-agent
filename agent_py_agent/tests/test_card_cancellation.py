"""凭据与模型取消：真实签名/owner/原卡CAS、无秘密与无任务唤醒。"""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import input_dependencies as inputs
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.gateway_parts import model_profile_service
from agent_py_agent.agent.gateway_parts.private_interaction_service import (
    resolve_private_interaction,
)
from agent_py_agent.agent.user_space.identity_store import (
    ProviderIdentityRecord,
    link_provider_identity,
)
from agent_py_agent.agent.user_space.interaction_refs import sign_interaction_reference
from agent_py_agent.agent.user_space.task_credentials import submit_credential_card
from agent_py_agent.tests.test_background_tool_approval import background
from agent_py_agent.tests.test_credential_callback_replay import _card
from agent_py_agent.tests.test_im_model_cards import callback, setup, submit

_FIXTURES=(background,)

def _cancel_body(body,actor=None):
    body = {**body,'decision':'cancel','fields':{}}
    if actor and 'submitter' in body:
        body['submitter']={**body['submitter'],'actor_id':actor}
    return body


@pytest.mark.parametrize('kind',['user','group'])
def test_credential_cancel_works_locked_audits_and_does_not_wake(tmp_path,kind,monkeypatch):
    agent,path,payload,body = _card(tmp_path,kind)
    actor = 'ou_member' if kind=='group' else payload['actor_id']
    link_provider_identity(agent.home_paths,ProviderIdentityRecord('feishu',actor,'user',actor,display_name='合成操作者'))
    agent.config.feishu_session_lock_enabled=True
    monkeypatch.setattr("agent_py_agent.agent.capability.runtime_config_reload.capability_config_for_agent",lambda *_:SimpleNamespace(im_task_credentials_enabled=False))
    monkeypatch.setattr('agent_py_agent.agent.gateway_parts.private_interaction_service._resolve_request_agent_for_owner',lambda *_:agent)
    monkeypatch.setattr(inputs,'publish_credential_resolution',lambda *_:pytest.fail('取消不得发恢复Wake'))
    result = resolve_private_interaction(agent,_cancel_body(body,actor),channel='feishu',actor_id=actor)
    row = json.loads(path.read_text())
    assert result['ok'] and row['status']=='cancelled'
    assert row['cancelled_by']['actor_id']==actor and row['cancelled_by']['actor_name']=='合成操作者'
    assert actor in result['message'] and row['cancelled_by']['clicked_at'] in result['message']
    assert 'ciphertext' not in row
    dependency = inputs.read_json_object_report(inputs.input_dependency_path(agent,row['input_id']),context='test').payload
    assert dependency['status']=='cancelled' and dependency['armed'] is False and dependency['resolved_at']>0
    assert dependency['resolution_delivery']=='retire'
    link=agent.conversation_store.tasks.load(agent._current_run_params.task_id)
    assert not link.input_dependency_refs and link.status=='active'
    assert not inputs.task_has_waiting_input(agent,agent._current_run_params.task_id)
    before = path.read_text()
    assert resolve_private_interaction(agent,_cancel_body(body,actor),channel='feishu',actor_id=actor)==result
    assert path.read_text()==before


def test_group_cancel_race_has_one_winner_and_never_overwrites_ready(tmp_path):
    agent,path,payload,body = _card(tmp_path,'group')
    def cancel(actor):
        return submit_credential_card(agent,payload,_cancel_body(body,actor))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(cancel,['ou_first','ou_second']))
    assert results[0]==results[1]
    winner=json.loads(path.read_text())['cancelled_by']['actor_id']
    assert winner in {'ou_first','ou_second'} and results[0]['cancelled_by']['actor_id']==winner
    agent2,path2,payload2,body2 = _card(tmp_path/'ready','group')
    assert submit_credential_card(agent2,payload2,body2)['ok']
    before=path2.read_text()
    assert submit_credential_card(agent2,payload2,_cancel_body(body2,'ou_other'))['error_code']=='IM_INTERACTION_ALREADY_HANDLED'
    assert path2.read_text()==before


@pytest.mark.parametrize('group',[False,True])
def test_model_selection_and_private_form_have_cancel_and_preserve_config(tmp_path,monkeypatch,group):
    base,owner,scope,host,thread=setup(tmp_path,monkeypatch,group=group)
    assert model_profile_service.execute_model_text_control(base,parse_conversation_control('/model'),scope).ok
    assert '取消' in json.dumps(base.delivery_service.sent[-1][1],ensure_ascii=False)
    body=callback(base,decision='cancel',fields={})
    if group:
        body['submitter']={'actor_id':'ou_member','chat_id':'oc_group','current_group_member':True}
    actor='ou_member' if group else 'ou_actor'
    base.config.feishu_session_lock_enabled=True
    monkeypatch.setattr("agent_py_agent.agent.capability.runtime_config_reload.capability_config_for_agent",lambda *_:SimpleNamespace(im_model_cards_enabled=False))
    result=submit(base,body,actor=actor)
    assert result['ok'] and result['cancelled_by']['actor_id']==actor
    assert submit(base,body,actor=actor)==result
    body['decision']='select'
    body['fields']={'profile_id':'default'}
    if group:
        with pytest.raises(ValueError):
            submit(base,body,actor=actor)
    else:
        assert not submit(base,body,actor=actor)['ok']


def test_model_private_form_cancel_cannot_save_secret_and_completed_cannot_rollback(tmp_path,monkeypatch):
    base,owner,scope,host,thread=setup(tmp_path,monkeypatch,group=True)
    model_profile_service.execute_model_text_control(base,parse_conversation_control('/model'),scope)
    assert submit(base,callback(base))['ok']
    assert '取消' in json.dumps(base.delivery_service.sent[-1][1],ensure_ascii=False)
    form=callback(base,decision='cancel',fields={'api_key':'SYNTHETIC-MUST-NOT-SAVE'})
    assert submit(base,form)['ok']
    assert 'SYNTHETIC-MUST-NOT-SAVE' not in ''.join(p.read_text() for p in host.home_paths.owner_home_dir.rglob('model-*.json'))
    first=callback(base,index=0,decision='cancel',fields={})
    first['submitter']={'actor_id':'ou_actor','chat_id':'oc_group','current_group_member':True}
    assert submit(base,first)['error_code']=='IM_INTERACTION_ALREADY_HANDLED'


def test_expired_card_can_only_close_not_submit_and_rejects_wrong_identity(tmp_path):
    agent,path,payload,body=_card(tmp_path,'user')
    payload['expires_at']=time.time()-5
    reference=sign_interaction_reference(agent.home_paths.owner_home_dir,payload)
    payload['reference']=reference
    path.write_text(json.dumps(payload))
    body={**body,'reference':reference}
    from agent_py_agent.agent.user_space.interaction_refs import verify_interaction_reference
    with pytest.raises(ValueError):
        verify_interaction_reference(agent.home_paths.root,reference,channel='feishu',actor_id=payload['actor_id'])
    assert verify_interaction_reference(agent.home_paths.root,reference,channel='feishu',actor_id=payload['actor_id'],decision='cancel')
    with pytest.raises(ValueError):
        verify_interaction_reference(agent.home_paths.root,reference,channel='feishu',actor_id='ou_other',decision='cancel')
    assert submit_credential_card(agent,payload,_cancel_body(body))['ok']


def test_cancel_releases_only_accurate_input_slot_and_allows_next_card(tmp_path,monkeypatch):
    from agent_py_agent.agent.conversation import input_dependency_index
    from agent_py_agent.agent.user_space.task_credentials import request_credential_card
    monkeypatch.setattr(input_dependency_index,'AGENT_TOOL_APPROVAL_MAX_PENDING_COUNT',1)
    agent,path,payload,body=_card(tmp_path,'user')
    assert submit_credential_card(agent,payload,_cancel_body(body))['ok']
    reference,_=request_credential_card(agent,url='https://example.com',authentication='bearer',call_id='after-cancel')
    assert reference!=payload['credential_ref']
    link=agent.conversation_store.tasks.load(agent._current_run_params.task_id)
    assert len(link.input_dependency_refs)==1 and payload['input_id'] not in link.input_dependency_refs


def test_cancel_new_candidate_preserves_previous_ready_authorization(background):
    from agent_py_agent.agent.user_space.task_credentials import (
        CredentialCardOptions,
        credential_headers,
        credential_path,
        request_credential_card,
    )
    from agent_py_agent.tests.test_task_credentials import _card as private_card
    old_ref,old_path,old_body,_ = private_card(background)
    agent=background.agent
    old_payload=json.loads(old_path.read_text())
    assert submit_credential_card(agent,old_payload,old_body)['ok']
    before=old_path.read_text()
    expected=credential_headers(agent,old_ref,'https://example.com/api')[0]
    new_ref,_=request_credential_card(agent,url='https://example.com/api',authentication='basic',options=CredentialCardOptions(refresh_ref=old_ref))
    path=credential_path(agent.home_paths.owner_home_dir,new_ref)
    row=json.loads(path.read_text())
    body={'reference':row['reference'],'message_id':row['message_id'],'chat_id':row['chat_id'],'decision':'cancel','fields':{}}
    assert submit_credential_card(agent,row,body)['ok']
    assert old_path.read_text()==before and credential_headers(agent,old_ref,'https://example.com/api')[0]==expected


def test_group_model_two_people_cancel_have_one_durable_actor(tmp_path,monkeypatch):
    base,owner,scope,host,thread=setup(tmp_path,monkeypatch,group=True)
    model_profile_service.execute_model_text_control(base,parse_conversation_control('/model'),scope)
    original=callback(base,decision='cancel',fields={})
    def cancel(actor):
        body={**original,'submitter':{'actor_id':actor,'chat_id':'oc_group','current_group_member':True}}
        return submit(base,body,actor=actor)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(cancel,['ou_first','ou_second']))
    assert results[0]==results[1] and results[0]['cancelled_by']['actor_id'] in {'ou_first','ou_second'}
    before=next(host.home_paths.owner_home_dir.rglob('model-*.json')).read_text()
    with pytest.raises(ValueError):
        submit(base,{**original,'message_id':'forwarded'},actor='ou_actor')
    assert next(host.home_paths.owner_home_dir.rglob('model-*.json')).read_text()==before


def test_edit_profile_cancel_preserves_previous_model_and_secret(tmp_path,monkeypatch):
    from agent_py_agent.agent.settings.model_profiles import model_profiles_path
    from agent_py_agent.tests.test_model_profiles import add
    base,owner,scope,host,thread=setup(tmp_path,monkeypatch)
    profile_id,_=add(host)
    before=model_profiles_path(host.home_paths).read_text()
    model_profile_service.execute_model_text_control(base,parse_conversation_control('/model'),scope)
    assert submit(base,callback(base,decision='edit',fields={'profile_id':profile_id}))['ok']
    assert '取消' in json.dumps(base.delivery_service.sent[-1][1],ensure_ascii=False)
    assert submit(base,callback(base,decision='cancel',fields={'api_key':'SYNTHETIC-REPLACEMENT'}))['ok']
    assert model_profiles_path(host.home_paths).read_text()==before


def test_cancel_buttons_are_outside_required_forms_and_empty_sdk_form_reaches_handler(tmp_path,monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.adapter import private_controls
    from agent_py_agent.agent.gateway_parts.model_control_cards import model_configuration_card
    from agent_py_agent.agent.user_space.task_credentials import credential_card
    agent,path,payload,body=_card(tmp_path/'credential','user')
    for card in (credential_card(payload),model_configuration_card('synthetic-reference','配置归属：本人')):
        forms=[item for item in card['elements'] if item.get('tag')=='form']
        assert forms and all('"decision": "cancel"' not in json.dumps(form) for form in forms)
        actions=[button for item in card['elements'] if item.get('tag')=='action' for button in item.get('actions',[]) if button.get('value',{}).get('decision')=='cancel']
        assert actions and all(button.get('action_type')!='form_submit' for button in actions)
    base,owner,scope,host,thread=setup(tmp_path/'model',monkeypatch,group=True)
    model_profile_service.execute_model_text_control(base,parse_conversation_control('/model'),scope)
    model_body=callback(base,decision='cancel',fields={})
    monkeypatch.setattr(private_controls,'_current_group_member',lambda *_:True)
    monkeypatch.setattr(private_controls,'_submit_control_request',lambda _adapter,actor,request:submit(base,request,actor=actor))
    norm={'operator_open_id':'ou_member','message_id':model_body['message_id'],'chat_id':model_body['chat_id'],
        'value':{'kind':'my_agent_model_configuration','reference':model_body['reference'],'decision':'cancel'}}
    result=private_controls.tool_approval_card_action(SimpleNamespace(),norm)
    assert result['toast']['type']=='success' and 'ou_member' in result['toast']['content']
    assert result['card']['data']['elements']


def test_cancel_before_tool_archive_is_not_rearmed_or_left_waiting(tmp_path,monkeypatch):
    from agent_py_agent.agent.conversation.input_tool_boundary import (
        input_wait_response,
        record_input_wait,
    )
    agent,path,payload,body=_card(tmp_path,'user')
    assert submit_credential_card(agent,payload,_cancel_body(body))['ok']
    monkeypatch.setattr(inputs,'_publish_resolution',lambda *_:pytest.fail('已取消输入不得再次armed或发布Wake'))
    params=SimpleNamespace(tool_context=[],live_archive_state={})
    result=SimpleNamespace(ok=True,metadata={'handler_details':{'input_dependency':{'status':'waiting_input','input_id':payload['input_id']}}})
    record_input_wait(agent,params,result)
    assert input_wait_response(agent,params) is None
    assert 'cancelled' in params.tool_context[0]
    dependency=inputs.read_json_object_report(inputs.input_dependency_path(agent,payload['input_id']),context='test').payload
    assert dependency['status']=='cancelled' and dependency['armed'] is False


def test_expired_model_cancel_closes_only_original_card(tmp_path,monkeypatch):
    from agent_py_agent.agent.user_space import interaction_refs
    base,owner,scope,host,thread=setup(tmp_path,monkeypatch)
    model_profile_service.execute_model_text_control(base,parse_conversation_control('/model'),scope)
    body=callback(base,decision='cancel',fields={})
    before=interaction_refs.time.time()
    monkeypatch.setattr(interaction_refs.time,'time',lambda:before+3600)
    assert submit(base,body)['ok']
    with pytest.raises(ValueError):
        submit(base,{**body,'decision':'select','fields':{'profile_id':'default'}})


def test_cancel_old_generation_never_retires_new_pending_card(background):
    from agent_py_agent.agent.user_space.task_credentials import (
        CredentialCardOptions,
        request_credential_card,
    )
    from agent_py_agent.tests.test_task_credentials import _card as private_card
    reference,path,body,_=private_card(background)
    old=json.loads(path.read_text())
    request_credential_card(background.agent,url='https://example.com/api',authentication='basic',options=CredentialCardOptions(refresh_ref=reference))
    current=path.read_text()
    with pytest.raises(ValueError):
        submit_credential_card(background.agent,old,_cancel_body(body))
    assert path.read_text()==current and json.loads(current)['status']=='pending'


def test_cancel_wins_while_private_model_form_is_in_network_and_late_child_cannot_save(tmp_path,monkeypatch):
    import threading

    from agent_py_agent.agent.settings.model_profiles import model_profiles_path
    base,owner,scope,host,thread=setup(tmp_path,monkeypatch,group=True)
    model_profile_service.execute_model_text_control(base,parse_conversation_control('/model'),scope)
    parent=callback(base,decision='add')
    entered,released=threading.Event(),threading.Event()
    original=base.delivery_service.deliver_control_card
    def delayed(context,card,*,summary):
        entered.set()
        assert released.wait(5)
        return original(context,card,summary=summary)
    monkeypatch.setattr(base.delivery_service,'deliver_control_card',delayed)
    results=[]
    worker=threading.Thread(target=lambda:results.append(submit(base,parent)))
    worker.start()
    assert entered.wait(5)
    cancel={**parent,'decision':'cancel','fields':{},'submitter':{'actor_id':'ou_member','chat_id':'oc_group','current_group_member':True}}
    first=submit(base,cancel,actor='ou_member')
    assert first['ok'] and first['decision']=='cancelled'
    released.set()
    worker.join(5)
    assert not worker.is_alive() and results==[first]
    child=callback(base,decision='save',fields={'model_backend':'openai_compatible','api_base':'https://synthetic.test/v1','model_name':'late','model_context_window_tokens':'32000','api_key':'SYNTHETIC-LATE'})
    before=model_profiles_path(host.home_paths).read_text() if model_profiles_path(host.home_paths).exists() else None
    assert submit(base,child)['error_code']=='IM_INTERACTION_ALREADY_HANDLED'
    after=model_profiles_path(host.home_paths).read_text() if model_profiles_path(host.home_paths).exists() else None
    assert before==after


def test_cancel_between_precheck_and_atomic_arm_never_rearms(tmp_path,monkeypatch):
    from agent_py_agent.agent.conversation import input_tool_boundary as boundary
    agent,path,payload,body=_card(tmp_path,'user')
    original=boundary.arm_input_dependency
    def after_precheck(host,input_id):
        assert submit_credential_card(agent,payload,_cancel_body(body))['ok']
        return original(host,input_id)
    monkeypatch.setattr(boundary,'arm_input_dependency',after_precheck)
    params=SimpleNamespace(tool_context=[],live_archive_state={})
    result=SimpleNamespace(ok=True,metadata={'handler_details':{'input_dependency':{'status':'waiting_input','input_id':payload['input_id']}}})
    boundary.record_input_wait(agent,params,result)
    row=inputs.read_json_object_report(inputs.input_dependency_path(agent,payload['input_id']),context='test').payload
    assert row['status']=='cancelled' and row['armed'] is False
    assert boundary.input_wait_response(agent,params) is None
    assert 'cancelled' in params.tool_context[0]
