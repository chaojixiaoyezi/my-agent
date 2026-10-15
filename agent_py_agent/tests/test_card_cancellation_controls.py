"""原卡取消、真实点击审计与确认竞态；只替换运输/供应商，无真实飞书。"""
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_py_agent.agent.adapter.feishu import FeishuAdapter
from agent_py_agent.agent.adapter.feishu_card import apply_card_action, build_persona_confirm_card
from agent_py_agent.agent.capability import persona_pending
from agent_py_agent.agent.common.json_io import read_json_object_report
from agent_py_agent.agent.gateway_parts.private_interaction_service import (
    resolve_private_interaction,
)
from agent_py_agent.agent.session_lock.card_control import bind_password_card, prepare_password_card
from agent_py_agent.agent.session_lock.feishu_cards import build_password_card
from agent_py_agent.agent.user_space import feishu_oauth as oauth
from agent_py_agent.tests.test_background_tool_approval import background
from agent_py_agent.tests.test_feishu_oauth import _api, _authorize, ctx
from agent_py_agent.tests.test_im_tool_approval import _im_pending

_FIXTURES=(background,ctx)


# LLM: 真实adapter/原密码库及签名nonce保留，只提供假message/chat回执，不连接租户。
# 函数用途: 建立一张当前个人密码卡，返回真实回调与原行快照。
def password_case(tmp_path,mode='set'):
    adapter=FeishuAdapter(config={'my_agent_home':str(tmp_path)})
    if mode!='set':
        adapter._unlock.set_password('ou_owner','SyntheticPassword123')
        adapter._unlock.store.record_activity('ou_owner',now=1)
    reference,payload=prepare_password_card(adapter,'ou_owner',mode)
    bind_password_card(adapter,payload,{'message_id':'om_password','chat_id':'oc_private'})
    norm={'value':{'session_lock_action':'pwd_cancel','user_id':'ou_owner','reference':reference},
          'operator_open_id':'ou_owner','message_id':'om_password','chat_id':'oc_private','form_value':{}}
    return adapter,norm,payload


@pytest.mark.parametrize('mode',['set','unlock','change'])
def test_password_cancel_empty_form_preserves_lock_hash_activity_and_queue(tmp_path,mode):
    adapter,norm,payload=password_case(tmp_path,mode)
    store=adapter._unlock.store
    before=(store.password_hash('ou_owner'),store.last_activity('ou_owner'))
    from agent_py_agent.agent.adapter.protocol import IncomingMessage
    received=[]
    adapter.on_message(received.append)
    adapter._send_password_card=lambda *_:True
    assert adapter._session_locked_gate(IncomingMessage(channel='feishu',user_id='ou_owner',content='合成待处理消息',message_id='pending-input',metadata={'feishu_chat_type':'p2p'}))
    assert adapter._pending_unlock_messages.depth('ou_owner')==1
    card=build_password_card(mode=mode,user_id='ou_owner',reference=norm['value']['reference'])
    form=next(item for item in card['elements'] if item.get('tag')=='form')
    assert not any(item.get('value',{}).get('session_lock_action')=='pwd_cancel' for item in form['elements'])
    cancel=card['elements'][-1]['actions'][0]
    assert cancel['value']['session_lock_action']=='pwd_cancel' and cancel.get('action_type')!='form_submit'
    result=adapter._handle_card_action(norm)
    assert result['toast']['type']=='success'
    assert (store.password_hash('ou_owner'),store.last_activity('ou_owner'))==before
    assert received==[] and adapter._pending_unlock_messages.depth('ou_owner')==1
    repeated=adapter._handle_card_action(norm)
    assert repeated==result
    submit={**norm,'value':{**norm['value'],'session_lock_action':{'set':'pwd_set','unlock':'pwd_unlock','change':'pwd_change'}[mode]},
            'form_value':{'pwd':'SyntheticPassword123','old_pwd':'SyntheticPassword123','new_pwd':'OtherPassword123'}}
    assert adapter._handle_card_action(submit)['toast']['type']=='error'
    assert (store.password_hash('ou_owner'),store.last_activity('ou_owner'))==before
    with store._connect() as connection:
        row=dict(connection.execute('SELECT * FROM private_chat_locks WHERE user_id=?',('ou_owner',)).fetchone())
    assert row['card_status']=='cancelled' and 'SyntheticPassword' not in row['card_actor_json']


@pytest.mark.parametrize('change',[{'operator_open_id':'ou_other'},{'chat_id':'oc_other'},{'message_id':'forwarded'}])
def test_password_wrong_actor_or_forwarded_card_cannot_cancel(tmp_path,change):
    adapter,norm,_=password_case(tmp_path)
    assert adapter._handle_card_action({**norm,**change})['toast']['type']=='error'
    with adapter._unlock.store._connect() as connection:
        assert connection.execute('SELECT card_status FROM private_chat_locks WHERE user_id=?',('ou_owner',)).fetchone()[0]=='pending'


def test_password_nonce_replacement_and_cancel_submit_race(tmp_path):
    adapter,norm,_=password_case(tmp_path)
    reference,payload=prepare_password_card(adapter,'ou_owner','set')
    bind_password_card(adapter,payload,{'message_id':'om_new','chat_id':'oc_private'})
    assert adapter._handle_card_action(norm)['toast']['type']=='error'
    cancel={**norm,'message_id':'om_new','value':{**norm['value'],'reference':reference}}
    submit={**cancel,'value':{**cancel['value'],'session_lock_action':'pwd_set'},'form_value':{'pwd':'SyntheticPassword123'}}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(adapter._handle_card_action,[cancel,submit]))
    assert sum(result['toast']['type']=='success' for result in results)==1
    with adapter._unlock.store._connect() as connection:
        row=connection.execute('SELECT card_status,password_hash FROM private_chat_locks WHERE user_id=?',('ou_owner',)).fetchone()
    assert bool(row['password_hash'])==(row['card_status']=='completed')


def test_approval_cancel_is_distinct_from_denial_and_replays_without_unlock(background):
    body,cards=_im_pending(background)
    assert any(button['value']['decision']=='cancel' for button in cards[0][1]['elements'][-1]['actions'])
    background.agent.config.feishu_session_lock_enabled=True
    body={**body,'decision':'cancel'}
    first=resolve_private_interaction(background.agent,body,channel='feishu',actor_id='ou_admin')
    second=resolve_private_interaction(background.agent,body,channel='feishu',actor_id='ou_admin')
    background.waiter.join(2)
    assert first==second and first['decision']=='cancelled'
    assert background.response['decision']=='cancelled'
    assert first['clicked_by']['actor_id']=='ou_admin'
    assert background.agent.conversation_store.tasks.load_report(background.task_id)[0].status!='cancelled'


def test_oauth_cancel_retires_original_input_and_does_not_revoke_ready(ctx):
    dependency,state,payload=_authorize(ctx)
    row=read_json_object_report(oauth.oauth_path(ctx.agent,'ou_synthetic'),context='test').payload
    body={'reference':state,'decision':'cancel',**row['im_control']}
    ctx.agent.config.feishu_session_lock_enabled=True
    result=resolve_private_interaction(ctx.agent,body,channel='feishu',actor_id='ou_synthetic')
    assert result['ok'] and result['clicked_by']['actor_id']=='ou_synthetic'
    assert resolve_private_interaction(ctx.agent,body,channel='feishu',actor_id='ou_synthetic')==result
    current=read_json_object_report(oauth.oauth_path(ctx.agent,'ou_synthetic'),context='test').payload
    assert current['status']=='cancelled' and current['ciphertext']==''
    input_row=read_json_object_report(oauth.input_dependency_path(ctx.agent,dependency['input_id']),context='test').payload
    assert input_row['status']=='cancelled'
    with pytest.raises(ValueError):
        oauth.complete_oauth(ctx.agent,payload,state,'synthetic-unused-code')


def test_persona_expired_cancel_idempotent_and_confirm_race(tmp_path,monkeypatch):
    token=persona_pending.add(tmp_path,('feishu','user','ou_owner'),'soul','合成长期设定')
    card=build_persona_confirm_card(token,'soul','合成长期设定')
    assert any(button['value']['choice']=='cancel' for button in card['elements'][-1]['actions'])
    wrong=apply_card_action({'token':token,'choice':'cancel'},tmp_path,'ou_other')
    assert not wrong['wrote'] and persona_pending.load(tmp_path,token)
    writes=[]
    monkeypatch.setattr('agent_py_agent.agent.adapter.feishu_card._write_confirmed_persona',lambda record,home:writes.append(record.token) or True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda choice:apply_card_action({'token':token,'choice':choice},tmp_path,'ou_owner'),['cancel','confirm']))
    assert sum(result['wrote'] for result in results)==len(writes)<=1
    if not writes:
        again=apply_card_action({'token':token,'choice':'cancel'},tmp_path,'ou_owner')
        assert again['clicked_by']['actor_id']=='ou_owner'
    assert persona_pending.load(tmp_path,token) is None


def test_oauth_completed_cancel_preserves_existing_token(ctx,monkeypatch):
    _,state,payload=_authorize(ctx)
    _api(ctx,monkeypatch)
    assert oauth.complete_oauth(ctx.agent,payload,state,'synthetic-code')['ok']
    path=oauth.oauth_path(ctx.agent,'ou_synthetic')
    before=path.read_bytes()
    row=read_json_object_report(path,context='test').payload
    result=resolve_private_interaction(ctx.agent,{'reference':state,'decision':'cancel',**row['im_control']},channel='feishu',actor_id='ou_synthetic')
    assert result['error_code']=='IM_INTERACTION_ALREADY_HANDLED' and path.read_bytes()==before


def test_oauth_cancel_not_overwritten_by_late_exchange_failure(ctx):
    dependency,state,payload=_authorize(ctx)
    path=oauth.oauth_path(ctx.agent,'ou_synthetic')
    row=read_json_object_report(path,context='test').payload
    body={'reference':state,'decision':'cancel',**row['im_control']}
    assert resolve_private_interaction(ctx.agent,body,channel='feishu',actor_id='ou_synthetic')['ok']
    before=path.read_bytes()
    oauth._fail_oauth(ctx.agent,'ou_synthetic',dependency['input_id'])
    assert path.read_bytes()==before


def test_approval_confirm_cancel_race_has_one_decision(background):
    body,_=_im_pending(background)
    def click(decision):
        try:
            return resolve_private_interaction(background.agent,{**body,'decision':decision},channel='feishu',actor_id='ou_admin')
        except (ValueError,FileNotFoundError):
            return {'ok':False}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(click,['cancel','approved']))
    background.waiter.join(2)
    assert sum(result['ok'] for result in results)==1
    assert background.response['decision'] in {'approved','cancelled'}


def test_password_expired_cancel_and_tamper_remain_scoped(tmp_path,monkeypatch):
    adapter,norm,payload=password_case(tmp_path)
    import time
    now=time.time()
    monkeypatch.setattr(time,'time',lambda:now+3600)
    assert adapter._handle_card_action(norm)['toast']['type']=='success'
    changed={**norm,'value':{**norm['value'],'reference':norm['value']['reference'][:-1]+'x'}}
    assert adapter._handle_card_action(changed)['toast']['type']=='error'
    assert not adapter._unlock.store.has_password('ou_owner')


@pytest.mark.parametrize('change',[{'chat_id':'oc_other'},{'message_id':'forwarded'},{'reference':'ir1.invalid.invalid'}])
def test_approval_cancel_rejects_forwarded_or_tampered_source(background,change):
    body,_=_im_pending(background)
    with pytest.raises(ValueError):
        resolve_private_interaction(background.agent,{**body,'decision':'cancel',**change},channel='feishu',actor_id='ou_admin')
    assert not background.response


def test_expired_oauth_cancel_closes_only_original_request(ctx,monkeypatch):
    dependency,state,payload=_authorize(ctx)
    row=read_json_object_report(oauth.oauth_path(ctx.agent,'ou_synthetic'),context='test').payload
    import time
    now=time.time()
    monkeypatch.setattr(time,'time',lambda:now+3600)
    body={'reference':state,'decision':'cancel',**row['im_control']}
    first=resolve_private_interaction(ctx.agent,body,channel='feishu',actor_id='ou_synthetic')
    assert first['ok']
    assert resolve_private_interaction(ctx.agent,body,channel='feishu',actor_id='ou_synthetic')==first


def test_persona_expired_cancel_has_original_actor_and_single_audit(tmp_path,monkeypatch):
    token=persona_pending.add(tmp_path,('feishu','user','ou_owner'),'soul','合成过期设定')
    import time
    now=time.time()
    monkeypatch.setattr(time,'time',lambda:now+2*86400)
    first=apply_card_action({'token':token,'choice':'cancel'},tmp_path,'ou_owner')
    second=apply_card_action({'token':token,'choice':'cancel'},tmp_path,'ou_owner')
    assert first==second and first['clicked_by']['actor_id']=='ou_owner'
    assert persona_pending.load(tmp_path,token) is None


def test_oauth_cancel_during_exchange_does_not_persist_late_success(ctx,monkeypatch):
    from threading import Event
    _,state,payload=_authorize(ctx)
    _api(ctx,monkeypatch)
    started,release=Event(),Event()
    original=oauth._exchange_oauth
    def delayed(agent,row,code):
        ready=original(agent,row,code)
        assert ready['status']=='ready'
        started.set()
        assert release.wait(5)
        return ready
    monkeypatch.setattr(oauth,'_exchange_oauth',delayed)
    path=oauth.oauth_path(ctx.agent,'ou_synthetic')
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(oauth.complete_oauth,ctx.agent,payload,state,'synthetic-code')
        assert started.wait(5)
        row=read_json_object_report(path,context='test').payload
        assert row['status']=='exchanging'
        result=resolve_private_interaction(ctx.agent,{'reference':state,'decision':'cancel',**row['im_control']},channel='feishu',actor_id='ou_synthetic')
        assert result['ok']
        cancelled=path.read_bytes()
        release.set()
        with pytest.raises(ValueError):
            future.result(5)
    assert path.read_bytes()==cancelled
    row=read_json_object_report(path,context='test').payload
    assert row['status']=='cancelled' and row['ciphertext']==''
