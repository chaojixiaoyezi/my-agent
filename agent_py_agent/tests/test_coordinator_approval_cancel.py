"""审批取消复用原签名记录和实际冷C HTTP，不生成执行源或修改P3。"""
import json
from contextlib import contextmanager

import pytest

from agent_py_agent.agent.common.json_io import read_json_object_report
from agent_py_agent.agent.contracts.tool_approval import build_tool_approval_request
from agent_py_agent.agent.conversation.agent_tool_approval import publish_agent_tool_approval
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall
from agent_py_agent.tests import test_coordinator_private_credential_transactions as seed


# LLM: 只在C启动前沿原Agent/claim/审批发布者种一张真实签名卡；C期间原fixture禁止新Agent/foreign pool。
# 函数用途: 提供原审批与凭据peer并存的实际冷C环境，不从测试JSON制造当前执行。
@contextmanager
def approval_case(tmp_path, monkeypatch):
    original = seed._card
    prepared = {}

    def card(*args):
        agent, path, credential, body = original(*args)
        current = agent._current_run_params
        thread_id = credential['scope']['thread_id']
        request = build_tool_approval_request(ToolCall(call_id='approval-cancel-call', tool_name='controlled_exec',
            arguments={'apply': True}, source_protocol='native', schema_hash='sha256:approval-test',
            run_id=current.task_id, turn_id='approval-turn', attempt_id='approval-attempt'),
            request_id='approval-request', round_number=1, call_index=1, description='合成审批取消')
        handle = publish_agent_tool_approval(agent, run_id=current.task_id, thread_id=thread_id, request_value=request)
        row = read_json_object_report(handle.path, context='test.approval.cancel').payload
        assert row['im_control']['delivery_status'] == 'sent'
        transport = row['im_control']
        prepared.update(path=handle.path, row=row, body={key: transport[key] for key in ('reference', 'message_id', 'chat_id')})
        prepared['body']['decision'] = 'cancel'
        return agent, path, credential, body

    monkeypatch.setattr(seed, '_card', card)
    with seed.original_case(tmp_path, monkeypatch) as case:
        yield case, prepared


def test_cold_c_cancels_original_approval_without_changing_execution_or_peer(tmp_path, monkeypatch):
    with approval_case(tmp_path, monkeypatch) as (case, pending):
        agent, peer, credential, _body, server, _runtime = case
        before_peer = peer.read_bytes()
        claim = agent.conversation_store.claims.load_report(pending['row']['thread_id'])[0]
        status, result = seed.post(server, credential, pending['body'])
        assert status == 200 and result['ok'], (status, result)
        row = json.loads(pending['path'].read_text())
        assert row['status'] == 'decided' and row['decision']['decision'] == 'cancelled'
        assert row['decision_by']['actor_id'] == credential['actor_id']
        saved = pending['path'].read_bytes()
        assert seed.post(server, credential, pending['body']) == (status, result)
        assert pending['path'].read_bytes() == saved and peer.read_bytes() == before_peer
        assert agent.conversation_store.claims.load_report(row['thread_id'])[0] == claim
        assert server.agent is None


@pytest.mark.parametrize('decision', ['approved', 'denied'])
def test_cold_c_does_not_treat_cancel_source_as_approval_permission(tmp_path, monkeypatch, decision):
    with approval_case(tmp_path, monkeypatch) as (case, pending):
        _agent, _peer, credential, _body, server, _runtime = case
        before = pending['path'].read_bytes()
        body = {**pending['body'], 'decision': decision}
        status, result = seed.post(server, credential, body)
        assert status == 200 and result['status'] == 'queued' and result['decision'] == 'pending'
        assert pending['path'].read_bytes() == before and json.loads(before)['status'] == 'pending'
        assert seed.post(server, credential, body) == (status, result)
        assert len(list((server.paths.root / 'control_operations').glob('*.json'))) == 1
        assert pending['path'].read_bytes() == before and server.agent is None


@pytest.mark.parametrize('fault', ['credential', 'actor', 'message_id', 'chat_id'])
def test_cold_c_cancel_retains_original_authenticated_card_binding(tmp_path, monkeypatch, fault):
    with approval_case(tmp_path, monkeypatch) as (case, pending):
        _agent, _peer, credential, _body, server, _runtime = case
        before = pending['path'].read_bytes()
        body = dict(pending['body'])
        kwargs = {}
        if fault == 'credential':
            kwargs['credential'] = False
        elif fault == 'actor':
            kwargs['actor'] = 'ou_unrelated'
        else:
            body[fault] = 'not-original-transport'
        assert seed.post(server, credential, body, **kwargs)[0] == 403
        assert pending['path'].read_bytes() == before


def test_cold_c_cancel_can_close_original_inactive_wait_without_rebinding_claim(tmp_path, monkeypatch):
    with approval_case(tmp_path, monkeypatch) as (case, pending):
        agent, _peer, credential, _body, server, _runtime = case
        store = agent.conversation_store
        row = pending['row']
        claim = store.claims.load_report(row['thread_id'])[0]
        store.claims.finish({'thread_id': row['thread_id'], 'claim_id': claim['claim_id'], 'status': 'finished'})
        closed_claim = store.claims.load_report(row['thread_id'])[0]
        status, result = seed.post(server, credential, pending['body'])
        assert status == 200 and result['decision'] == 'cancelled'
        assert store.claims.load_report(row['thread_id'])[0] == closed_claim


@pytest.mark.parametrize('lock_kind', ['transition', 'row'])
def test_cold_c_cancel_original_lock_busy_is_unexecuted_then_original_replay_succeeds(tmp_path, monkeypatch, lock_kind):
    from agent_py_agent.agent.common.json_io import locked_json_path
    from agent_py_agent.agent.conversation.agent_tool_approval import _transition_path
    with approval_case(tmp_path, monkeypatch) as (case, pending):
        _agent, _peer, credential, _body, server, _runtime = case
        before = pending['path'].read_bytes()
        lock = _transition_path(pending['path']) if lock_kind == 'transition' else pending['path']
        with locked_json_path(lock):
            status, result = seed.post(server, credential, pending['body'])
            assert status == 503 and result['error_code'] == 'IM_INTERACTION_HOST_UNAVAILABLE'
            assert pending['path'].read_bytes() == before
        assert seed.post(server, credential, pending['body'])[1]['ok']


def test_cold_c_cancel_closed_original_source_does_not_write_or_create_replacement(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.private_interaction_transaction import (
        _PrivateInteractionTransaction,
    )
    original = _PrivateInteractionTransaction.perform
    with approval_case(tmp_path, monkeypatch) as (case, pending):
        _agent, _peer, credential, _body, server, _runtime = case
        before = pending['path'].read_bytes()
        seen = []

        def stopped(source, operation):
            seen.append(source)
            source.close()
            return original(source, operation)

        monkeypatch.setattr(_PrivateInteractionTransaction, 'perform', stopped)
        assert seed.post(server, credential, pending['body'])[0] == 503
        assert len(seen) == 1 and pending['path'].read_bytes() == before
