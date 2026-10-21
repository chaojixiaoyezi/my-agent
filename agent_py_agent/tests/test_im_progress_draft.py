from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

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
from agent_py_agent.tests.test_committed_model_messages import _commit_writer_fixture


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


def test_timestamp_is_visible_bounded_and_updated_in_same_message(tmp_path, monkeypatch):
    from agent_py_agent.agent.adapter.progress_draft import send_gateway_draft

    clock = [1000.0]
    monkeypatch.setattr("agent_py_agent.agent.adapter.progress_draft.time.time", lambda: clock[0])
    store = GatewayReplyDeliveryStore(tmp_path)
    provider = Adapter()
    registry = ChannelAdapterRegistry()
    registry.register_adapter("feishu", provider, capabilities=ChannelCapabilities(edit_progress=True))
    service = DeliveryService(registry)
    update = ["2026-10-13T17:38:20+08:00"]
    final = [None]
    finals = []
    worker = GatewayReplyDeliveryWorker(store, poll_response=lambda _: final[0],
        deliver_response=lambda _, text: finals.append(text) or True,
        poll_progress=lambda row: GatewayProgressBatch(("执行中：run_command",), row.progress_cursor + 1,
            "stream", True, 100, update[0]),
        deliver_progress=lambda row, text: send_gateway_draft(service, row, text))
    worker.enqueue(PendingGatewayReply(request_id="gw-minimal", channel="feishu", user_id="ou_user", message_id="om_user"))
    worker.run_once()
    assert len(provider.created) == 1
    assert "更新于 17:38:20" in store.pending()[0].progress_draft["content"]
    clock[0] += 120
    update[0] = "2026-10-13T17:40:20+08:00"
    worker.run_once()
    assert len(provider.created) == 1 and provider.edits[0][0] == "om_preview"
    assert provider.edits[0][1] == "执行中：run_command\n更新于 17:40:20"
    assert len(provider.edits[0][1]) <= 100
    final[0] = "完整结果" * 100
    worker.run_once()
    assert finals == [final[0]] and not store.pending()
    clock[0] += 120
    worker.run_once()
    assert len(provider.edits) == 1  # 最终回复后不再编辑旧窗口。


def test_progress_redacts_paths_without_note_and_final_keeps_note():
    from agent_py_agent.agent.conversation.channels import HOST_PATH_REDACTION_NOTE

    provider = Adapter()
    registry = ChannelAdapterRegistry()
    registry.register_adapter("feishu", provider, capabilities=ChannelCapabilities(edit_progress=True))
    service = DeliveryService(registry)
    context = DeliveryContext(channel="feishu", target="ou_user", mode="reply", idempotency_key="time-preview")
    text = "执行中 /private/synthetic-home/long-command.sh\n更新于 17:40:20"
    assert service.deliver_progress(context, ReplyEnvelope(content=text)).ok
    progress = next(iter(provider.created.values()))[2]
    assert "/private/" not in progress and HOST_PATH_REDACTION_NOTE not in progress
    receipt = service.deliver(context, ReplyEnvelope(content=text))
    assert HOST_PATH_REDACTION_NOTE in receipt.content


@pytest.mark.parametrize("limit,size", [(100,99), (100,100), (100,101), (17,80), (100,200)])
def test_stream_whole_preview_is_bounded_and_final_remains_full(tmp_path, monkeypatch, limit, size):
    clock = [1000.0]
    monkeypatch.setattr("agent_py_agent.agent.adapter.progress_draft.time.time", lambda: clock[0])
    store = GatewayReplyDeliveryStore(tmp_path)
    provider = Adapter()
    registry = ChannelAdapterRegistry()
    registry.register_adapter("feishu", provider, capabilities=ChannelCapabilities(proactive=True, edit_progress=True))
    service = DeliveryService(registry)
    current = [("中🙂" * size)[:size]]
    final = [None]
    finals = []
    from agent_py_agent.agent.adapter.progress_draft import send_gateway_draft

    worker = GatewayReplyDeliveryWorker(store, poll_response=lambda row: final[0],
        deliver_response=lambda row, text: finals.append(text) or True,
        poll_progress=lambda row: GatewayProgressBatch((current[0],), row.progress_cursor + 1, "stream", True),
        deliver_progress=lambda row, text: send_gateway_draft(service, row, text))
    worker.stream_preview_max_chars = limit
    worker.enqueue(PendingGatewayReply(request_id="gw-bounded", channel="feishu", user_id="ou_user",
        message_id="om_user", progress_draft={"mode": "stream"}))
    worker.run_once()
    assert store.pending()[0].progress_draft["content"] == current[0][-limit:]
    clock[0] += 2
    current[0] = ("更新🙂" * size)[:size]
    worker.run_once()
    assert provider.edits[-1][1] == current[0][-limit:]
    final[0] = "完整结果" * 80
    clock[0] += 2
    worker.run_once()
    assert finals == [final[0]] and len(finals[0]) > limit


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
        assert store.pending()[0].progress_draft['disabled'] is False
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
    worker.stream_preview_max_chars=100
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


def test_long_model_draft_keeps_latest_window_after_complete_redaction(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import stream_events
    from agent_py_agent.agent.gateway_parts.stream_writer import BufferedChunkStreamWriter

    content = '旧内容' * 3000 + '最新结果'
    observed = []

    def public(text, *, max_chars, **kwargs):
        observed.append((len(text), max_chars))
        return text.replace('旧内容', '已脱敏')

    monkeypatch.setattr(stream_events, 'public_model_text', public)
    writer = BufferedChunkStreamWriter(tmp_path / 'committed.jsonl')
    writer.set_verbose_level('stream', preview_max_chars=8000)
    _commit_writer_fixture(writer, content, call_id='long-committed')
    import json

    events = [json.loads(line) for line in writer.chunk_path.read_text().splitlines()]
    assert observed == [(len(content), 0), (len(content), 0)]
    # 原raw草稿8K裁剪退休；已确认全文完整留在原chunk，展示上限仍由原投递配置消费。
    assert events[0]['text'] == content.replace('旧内容', '已脱敏')
    assert events[0]['preview_max_chars'] == 8000
    assert events[0]['text'].endswith('最新结果')
    from agent_py_agent.agent.adapter.progress_draft import send_gateway_draft

    store = GatewayReplyDeliveryStore(tmp_path / 'delivery')
    provider = Adapter()
    registry = ChannelAdapterRegistry()
    registry.register_adapter('feishu', provider, capabilities=ChannelCapabilities(edit_progress=True))
    service = DeliveryService(registry)
    worker = GatewayReplyDeliveryWorker(store, poll_response=lambda _: None,
        deliver_response=lambda *_: False,
        poll_progress=lambda _: GatewayProgressBatch((events[0]['text'],), 1, 'stream', True, 8000),
        deliver_progress=lambda row, text: send_gateway_draft(service, row, text))
    worker.enqueue(PendingGatewayReply(request_id='long-committed', channel='feishu', user_id='user', message_id='message'))
    worker.run_once()
    delivered = store.pending()[0].progress_draft['content']
    assert len(delivered) == 8000
    assert delivered.endswith('最新结果')


def test_failed_edit_recovers_same_id_and_uses_latest_content(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("agent_py_agent.agent.adapter.progress_draft.time.time", lambda: clock[0])
    monkeypatch.setattr("agent_py_agent.agent.adapter.delivery.time.time", lambda: clock[0])
    store = GatewayReplyDeliveryStore(tmp_path)
    calls = []
    state = ["初始"]
    def send(row, text):
        calls.append((row.progress_draft.get("message_id"), text))
        return ProgressDeliveryReceipt(len(calls) != 2, "same-id", "" if len(calls) != 2 else "IO")
    worker = GatewayReplyDeliveryWorker(store, poll_response=lambda _: None,
        deliver_response=lambda *_: True, deliver_progress=send,
        poll_progress=lambda row: GatewayProgressBatch((state[0],), row.progress_cursor + 1, "stream", True))
    worker.enqueue(PendingGatewayReply(request_id="retry", channel="feishu", user_id="ou_test", message_id="in"))
    worker.run_once()
    clock[0] += 2
    state[0] = "失败候选"
    worker.run_once()
    assert not store.pending()[0].progress_draft["disabled"]
    state[0] = "最新候选"
    worker.run_once()
    assert len(calls) == 2
    clock[0] += 3
    worker.run_once()
    assert calls[-1] == ("same-id", "最新候选")
    assert store.pending()[0].progress_draft["failures"] == 0


def test_unknown_first_send_is_not_repeated_and_final_has_priority(tmp_path):
    calls, final = [], [None]
    store = GatewayReplyDeliveryStore(tmp_path)
    worker = GatewayReplyDeliveryWorker(store, poll_response=lambda _: final[0],
        deliver_response=lambda *_: True,
        poll_progress=lambda row: GatewayProgressBatch(("等待",), row.progress_cursor + 1, "stream"),
        deliver_progress=lambda *_: calls.append(1) or ProgressDeliveryReceipt(False))
    worker.enqueue(PendingGatewayReply(request_id="unknown", channel="feishu", user_id="ou_test", message_id="in"))
    worker.run_once()
    worker.run_once()
    assert calls == [1]
    final[0] = "完成"
    assert worker.run_once() == 1 and calls == [1]


@pytest.mark.parametrize("kind,expected", [("permanent", 1), ("unknown", 3), ("transient", 3)])
def test_edit_failure_budget_is_bounded(tmp_path, monkeypatch, kind, expected):
    clock = [1000.0]
    monkeypatch.setattr("agent_py_agent.agent.adapter.delivery.time.time", lambda: clock[0])
    monkeypatch.setattr("agent_py_agent.agent.adapter.progress_draft.time.time", lambda: clock[0])
    calls = []
    store = GatewayReplyDeliveryStore(tmp_path)
    worker = GatewayReplyDeliveryWorker(store, poll_response=lambda _: None,
        deliver_response=lambda *_: True,
        poll_progress=lambda row: GatewayProgressBatch(("当前",), row.progress_cursor + 1, "stream", True),
        deliver_progress=lambda *_: calls.append(1) or ProgressDeliveryReceipt(False, "known-id", "IO", kind))
    worker.enqueue(PendingGatewayReply(request_id="bounded", channel="feishu", user_id="ou_test", message_id="in",
        progress_draft={"mode": "stream", "message_id": "known-id"}))
    for _ in range(8):
        worker.run_once()
        clock[0] += 31
    assert len(calls) == expected and store.pending()[0].progress_draft["disabled"]


def test_status_uses_latest_typed_state_not_stale_start(monkeypatch):
    from agent_py_agent.agent.gateway_parts import control_service
    monkeypatch.setattr(control_service, "gateway_chunk_path", lambda *_: "unused")
    rows = [{"kind": "tool_progress", "progress": {"phase": "started", "call_id": "a"}}]
    monkeypatch.setattr(control_service, "_tail_json_rows", lambda _: rows)
    assert "等待当前状态更新" in control_service._recent_progress(None, "request")
    rows.append({"kind": "tool_progress", "updated_at": "2026-10-14T01:02:03Z",
        "progress": {"phase": "running", "call_id": "a", "elapsed_seconds": 120}})
    assert "120 秒" in control_service._recent_progress(None, "request")
    rows.append({"kind": "tool_progress", "progress": {"phase": "finished", "call_id": "a", "ok": True}})
    assert "刚完成" in control_service._recent_progress(None, "request")
    rows.append({"kind": "model_liveness", "updated_at": "2026-10-14T01:04:03Z", "progress": {"wait_state": "model_response"}})
    assert "等待模型响应" in control_service._recent_progress(None, "request")
    rows.append({"kind": "model_delta", "text": "secret not read"})
    assert "模型已返回内容" in control_service._recent_progress(None, "request")


# LLM: 使用真实pending/dispatch和DeliveryService，仅provider及时间为替身，不向飞书外发。
# 函数用途: 为增量页脚与裁剪回归提供同一条可编辑进度消息。
@pytest.fixture
def c4_stream_case(tmp_path, monkeypatch):
    from agent_py_agent.agent.adapter.progress_draft import send_gateway_draft

    case = SimpleNamespace(clock=1000.0, batch=GatewayProgressBatch((), 0), final=None, finals=[])
    monkeypatch.setattr('agent_py_agent.agent.adapter.progress_draft.time.time', lambda: case.clock)
    case.store, case.provider = GatewayReplyDeliveryStore(tmp_path), Adapter()
    registry = ChannelAdapterRegistry()
    registry.register_adapter('feishu', case.provider, capabilities=ChannelCapabilities(edit_progress=True))
    service = DeliveryService(registry)
    case.worker = GatewayReplyDeliveryWorker(case.store, poll_response=lambda _: case.final,
        deliver_response=lambda _, text: case.finals.append(text) or True,
        poll_progress=lambda _: case.batch,
        deliver_progress=lambda row, text: send_gateway_draft(service, row, text))
    case.worker.enqueue(PendingGatewayReply(request_id='c4-preview', channel='feishu', user_id='ou_fixture', message_id='om_in'))
    return case


def test_c4_append_progress_keeps_only_latest_timestamp_in_same_message(c4_stream_case):
    case = c4_stream_case
    for cursor in range(1, 4):
        case.batch = GatewayProgressBatch((f'完整步骤 {cursor}',), cursor, 'stream', False, 1000,
            f'2026-10-21T10:20:0{cursor}+08:00')
        case.worker.run_once()
        content = case.store.pending()[0].progress_draft['content']
        assert content == '\n'.join(f'完整步骤 {step}' for step in range(1, cursor + 1)) + f'\n更新于 10:20:0{cursor}'
        assert content.count('更新于 ') == 1
        assert case.store.pending()[0].progress_cursor == cursor
        case.clock += 2
    assert len(case.provider.created) == 1
    assert [message for message, _ in case.provider.edits] == ['om_preview', 'om_preview']


@pytest.mark.parametrize('timestamp', ['', '2026-10-21T10:20:30+08:00'])
@pytest.mark.parametrize('content,budget,expected', [
    ('discarded partial line\n完整进度甲\n完整进度乙', len('partial line\n完整进度甲\n完整进度乙'), '完整进度甲\n完整进度乙'),
    ('单行很长但仍应保留末尾', 5, '应保留末尾'),
    ('完整第一行\n完整第二行', 100, '完整第一行\n完整第二行'),
    ('完整步骤\n  ', 4, '完整步骤'),
])
def test_c4_stream_trims_partial_lines_not_single_line_or_full_content(c4_stream_case, timestamp, content, budget, expected):
    case = c4_stream_case
    footer = '\n更新于 10:20:30' if timestamp else ''
    limit = budget + len(footer)
    case.batch = GatewayProgressBatch((content,), 1, 'stream', True, limit, timestamp)
    case.worker.run_once()
    shown = case.store.pending()[0].progress_draft['content']
    assert shown == expected + footer and len(shown) <= limit
    assert next(iter(case.provider.created.values()))[2] == shown
    case.final = '未经预览裁剪的完整结果' * 100
    case.clock += 2
    case.worker.run_once()
    assert case.finals == [case.final] and not case.store.pending()


@pytest.mark.parametrize('limit', [1, 12, 13, 14])
def test_c4_timestamp_budget_never_leaks_unbounded_body(c4_stream_case, limit):
    case = c4_stream_case
    footer = '\n更新于 10:20:30'
    content = '很长的单行正文' * 100
    case.batch = GatewayProgressBatch((content,), 1, 'stream', True, limit, '2026-10-21T10:20:30+08:00')
    case.worker.run_once()
    shown = case.store.pending()[0].progress_draft['content']
    budget = max(0, limit - len(footer))
    assert shown == (content[-budget:] if budget else '') + footer[-limit:]
    assert len(shown) == limit
