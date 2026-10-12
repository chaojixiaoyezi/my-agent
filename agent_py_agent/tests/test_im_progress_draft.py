from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch

from agent_py_agent.agent.adapter.delivery import (
    GatewayReplyDeliveryStore,
    GatewayReplyDeliveryWorker,
    PendingGatewayReply,
)
from agent_py_agent.agent.adapter.progress_draft import GatewayProgressBatch
from agent_py_agent.agent.delivery import (
    ChannelAdapterRegistry,
    ChannelCapabilities,
    DeliveryContext,
    DeliveryService,
    ReplyEnvelope,
)
from agent_py_agent.agent.delivery.progress import ProgressDeliveryReceipt


class Adapter:
    provider_idempotent_delivery = True
    provider_idempotency_window_seconds = 3600
    supports_edit_progress = True

    def __init__(self):
        self.created = {}
        self.edits = []
        self.finals = []
        self.edit_ok = True

    def start_progress_message(self, target, text, *, reply_to='', idempotency_key=''):
        self.created.setdefault(idempotency_key, ('om_preview', target, text, reply_to))
        return 'om_preview'

    def edit_progress_message(self, message_id, text):
        self.edits.append((message_id,text))
        return self.edit_ok

    def finalize_response(self, target, handle, message):
        self.finals.append((target, message.content))
        return True

    def send_message(self, target, message):
        self.finals.append((target, message.content))
        return True


def test_one_progress_message_survives_worker_restart_and_final_is_separate(tmp_path):
    adapter=Adapter()
    registry=ChannelAdapterRegistry()
    registry.register_adapter('feishu',adapter,capabilities=ChannelCapabilities(proactive=True, edit_progress=True))
    service=DeliveryService(registry)
    clock=[1000.0]
    final=[None]
    texts=['第一步','第二步','第三步']
    store=GatewayReplyDeliveryStore(tmp_path)

    class Callbacks:
        _delivery_registry=registry

        def progress(self,record,text):
            return service.deliver_progress(DeliveryContext(channel=record.channel,target=record.user_id,
                mode='reply',reply_to=record.message_id,idempotency_key=record.dispatch['message_key']),
                ReplyEnvelope(content=text),message_id=str(record.progress_draft.get('message_id') or ''))

        def response(self,record,text):
            return service.deliver(DeliveryContext(channel=record.channel,target=record.user_id),ReplyEnvelope(content=text)).delivery_status=='sent'

    callbacks=Callbacks()
    def make_worker():
        return GatewayReplyDeliveryWorker(store,poll_response=lambda _:final[0],deliver_response=callbacks.response,
            poll_progress=lambda record:GatewayProgressBatch(tuple(texts[record.progress_cursor:record.progress_cursor+1]),
                min(len(texts),record.progress_cursor+1),'stream'),deliver_progress=callbacks.progress)
    worker=make_worker()
    worker.enqueue(PendingGatewayReply(request_id='gw-test',channel='feishu',user_id='ou_admin',message_id='om_request'))
    with patch('agent_py_agent.agent.adapter.progress_draft.time.time',side_effect=lambda:clock[0]):
        assert worker.run_once()==0
        record=store.pending()[0]
        assert record.progress_draft['message_id']=='om_preview'
        clock[0]+=1.1
        worker=make_worker()  # 消息 ID 从原 pending 恢复，不依赖上一个进程缓存。
        assert worker.run_once()==0
        assert len(adapter.created)==1 and len(adapter.edits)==1
        assert adapter.edits[0][0]=='om_preview'
        clock[0]+=1.1
        adapter.edit_ok=False
        assert worker.run_once()==0
        assert store.pending()[0].progress_draft['disabled'] is True
        final[0]='最终结果'
        clock[0]+=1.1
        assert worker.run_once()==1
    assert len(adapter.created)==1 and len(adapter.edits)==2
    assert adapter.finals==[('ou_admin','最终结果')]
    assert store.pending()==[]


def test_failed_progress_does_not_block_final(tmp_path):
    store=GatewayReplyDeliveryStore(tmp_path)
    final=[]
    worker=GatewayReplyDeliveryWorker(store,poll_response=lambda _:'result',deliver_response=lambda _,text:final.append(text) or True,
        poll_progress=lambda _:GatewayProgressBatch(('draft',),1,'stream'),
        deliver_progress=lambda _,text:ProgressDeliveryReceipt(False,error_code='CHANNEL_PROGRESS_SEND_FAILED'))
    worker.enqueue(PendingGatewayReply(request_id='gw-failed',channel='feishu',user_id='ou_admin',message_id='om_request'))
    assert worker.run_once()==1 and final==['result']


def test_snapshot_replaces_previous_candidate_without_flooding(tmp_path):
    from agent_py_agent.agent.adapter.progress_draft import deliver_stream_progress

    class Worker:
        _claim_owner='owner'
        store=None

        def _dispatch_message(self,record,text,message):
            assert text=='新候选内容'
            return record,True

        def _commit_progress(self,record,cursor):
            return replace(record,progress_cursor=cursor)

    worker=Worker()
    class Store:
        def commit_claim(self,old,new,**kwargs):
            return new
    worker.store=Store()
    record=PendingGatewayReply(request_id='gw-snapshot',channel='feishu',user_id='ou_user',message_id='om_request',
        progress_draft={'mode':'stream','content':'旧候选内容','message_id':'om_preview'})
    updated=deliver_stream_progress(worker,record,GatewayProgressBatch(('新候选内容',),2,'stream',True))
    assert updated.progress_draft['content']=='新候选内容'


def test_stream_crash_after_accept_replays_same_key_and_frozen_draft(tmp_path, monkeypatch):
    from agent_py_agent.agent.adapter import delivery

    class Crash(BaseException):
        pass

    class Provider(Adapter):
        def __init__(self):
            super().__init__()
            self.crash=True

        def start_progress_message(self, target, text, *, reply_to='', idempotency_key=''):
            result=super().start_progress_message(target,text,reply_to=reply_to,idempotency_key=idempotency_key)
            if self.crash:
                raise Crash()
            return result

    provider=Provider()
    registry=ChannelAdapterRegistry()
    registry.register_adapter('feishu',provider,capabilities=ChannelCapabilities(proactive=True,edit_progress=True))
    service=DeliveryService(registry)
    store=GatewayReplyDeliveryStore(tmp_path)
    class Callbacks:
        _delivery_registry=registry

        def progress(self,record,text):
            return service.deliver_progress(DeliveryContext(channel='feishu',target=record.user_id,
                idempotency_key=record.dispatch['message_key']),ReplyEnvelope(content=text),
                message_id=str(record.progress_draft.get('message_id') or ''))
    callback=Callbacks()
    def worker(text, result=None):
        return GatewayReplyDeliveryWorker(store,poll_response=lambda _:result,deliver_response=lambda _,text:True,
            poll_progress=lambda _:GatewayProgressBatch((text,),1,'stream'),deliver_progress=callback.progress)
    first=worker('已冻结旧草稿')
    first.enqueue(PendingGatewayReply(request_id='gw-crash',channel='feishu',user_id='ou_fixture',message_id='om_request'))
    import pytest
    with pytest.raises(Crash):
        first.run_once()
    assert len(provider.created)==1 and not store.pending()[0].progress_draft.get('message_id')
    from types import SimpleNamespace

    now=delivery.time.time()+125
    monkeypatch.setattr(delivery,'time',SimpleNamespace(time=lambda:now))
    monkeypatch.setattr(delivery,'_dispatch_owner_live',lambda _:False)
    provider.crash=False
    second=worker('新的候选不能覆盖旧外发意图','最终结果')
    assert second.run_once()==1
    assert len(provider.created)==1
    assert next(iter(provider.created.values()))[2]=='已冻结旧草稿'


def test_feishu_draft_keeps_text_type_when_content_becomes_markdown(monkeypatch):
    import json

    from agent_py_agent.agent.adapter import feishu

    calls=[]
    def api(method,path,body,token):
        calls.append((method,path,body))
        return {"code":0,"data":{"message_id":"om_draft"}}
    monkeypatch.setattr(feishu,"_feishu_msg_api",api)
    adapter=feishu.FeishuAdapter({"feishu_session_lock_enabled":False})
    adapter._get_tenant_access_token=lambda:"fake-token"
    identity=adapter.start_progress_message("ou_fixture","正在检查",idempotency_key="draft-stable")
    assert identity=="om_draft"
    assert adapter.edit_progress_message(identity,"**检查完成**\n|列|值|\n|---|---|\n|a|b|")
    assert [item[2]["msg_type"] for item in calls]==["text","text"]
    assert calls[1][1].endswith("/om_draft")
    assert json.loads(calls[1][2]["content"])["text"].startswith("**检查完成**")


def test_long_model_draft_keeps_latest_window_after_complete_redaction(monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts import stream_writer

    events=[]
    content='旧内容'*3000+'最新结果'
    observed=[]
    def public(text, *, max_chars, **kwargs):
        observed.append((len(text),max_chars))
        return text.replace('旧内容','已脱敏')
    monkeypatch.setattr(stream_writer,'public_model_text',public)
    writer=SimpleNamespace(_last_im_draft_at=0,_model_segment=[content],delivery_channel='feishu',
        _identifier_redactions=(),_write_event=events.append)
    stream_writer._write_im_draft(writer)
    assert observed==[(len(content),0)]
    assert len(events[0]['text'])==8000
    assert events[0]['text'].endswith('最新结果')
