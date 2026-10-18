"""原guidance观察器只读结构合同；此处不伪造W控制分派或宣称交付完成。"""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts import control_operation_service as ops
from agent_py_agent.agent.gateway_parts.control_service import active_turn_guidance_dedupe_key
from agent_py_agent.agent.gateway_parts.execution_target import GatewayExecutionTarget
from agent_py_agent.agent.gateway_parts.multiprocess_control_reconcile import (
    _read_guidance_observation,
    read_original_steer_delivery,
)
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.tests.test_gateway_multiprocess_control_worker import prepared_case


# LLM: 正例由主线唯一guidance producer写入临时目录，不手造consumed/rejected或第二账本。
# 函数用途: 准备一条真实pending回执供纯观察器读回。
def pending_receipt(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    store.guidance.append_once({"target_type": "request", "target_id": "original-turn",
                                "sender": "actor", "message": "合成补充输入"},
                               dedupe_key="original-input")
    return store.guidance.storage.guidance_dedupe_path("original-input")


def test_actual_pending_guidance_is_observed_without_body_change(tmp_path):
    path = pending_receipt(tmp_path)
    original = path.read_bytes()
    result = _read_guidance_observation(path)
    assert result.status == "pending" and result.dedupe_key == "original-input"
    assert path.read_bytes() == original


def test_legacy_guidance_is_not_implicitly_migrated_by_observer(tmp_path):
    path = pending_receipt(tmp_path)
    data = json.loads(path.read_bytes())
    data.update(schema_version="conversation_guidance_once.v1", status="accepted")
    path.write_text(json.dumps(data), encoding="utf-8")
    original = path.read_bytes()
    assert _read_guidance_observation(path) is None
    assert path.read_bytes() == original


@pytest.mark.parametrize("content", [b"{", b"\xff", b"[]"])
def test_unreadable_guidance_remains_domain_corruption_without_repair(tmp_path, content):
    path = tmp_path / "guidance.json"
    path.write_bytes(content)
    with pytest.raises(DataCorruptionError):
        _read_guidance_observation(path)
    assert path.read_bytes() == content


def test_missing_guidance_stays_unknown_without_creating_body(tmp_path):
    path = tmp_path / "missing.json"
    assert _read_guidance_observation(path) is None
    assert not path.exists()


# LLM: 控制回执来自真实C producer，guidance只沿原append/claim/mark_status合成领域消费，不称真实provider已接收，也不修投影。
# 函数用途: 为原W只读对账准备唯一账内终态，允许变更一项事实验证不能错认另一条消息。
def original_terminal_guidance(tmp_path, monkeypatch, terminal, mismatch=None):
    _paths, receipt, _scope, _frozen, _pool, _owners = prepared_case(tmp_path, monkeypatch)
    scope = ops._receipt_scope(receipt, dispatched_execution=True)
    target = GatewayExecutionTarget.from_dict(receipt.dispatch['execution'])
    key = active_turn_guidance_dedupe_key(scope)
    metadata = {'thread_id': target.thread_id, 'channel_message_id': receipt.client_message_id,
                'expected_turn_id': target.turn_id, 'channel': scope.channel,
                'conversation_id': scope.conversation_id, 'kind': 'active_turn_user_input',
                'record_in_transcript': True,
                'gateway_input_request_id': str(scope.metadata.get('gateway_input_request_id') or '').strip()}
    values = {'target_type': target.kind, 'target_id': target.task_id, 'sender': scope.user_id,
              'message': '合成控制', 'priority': 'high', 'delivery': 'current_task', 'metadata': metadata}
    if mismatch == 'sender':
        values['sender'] = 'different-actor'
    elif mismatch == 'message':
        values['message'] = 'different-body'
    elif mismatch is not None:
        metadata[mismatch] = 'different-original-input'
    store = ConversationStore(tmp_path / 'actual-guidance')
    entry = store.guidance.append_once(values, dedupe_key=key)
    if terminal == 'consumed':
        assert store.guidance.claim_for_turn(entry, expected_turn_id=target.turn_id,
                                             attempt_id='synthetic-attempt', owning_task_id=target.task_id)
        assert store.guidance.submissions.mark_submitted(
            target.turn_id, [entry], attempt_id='synthetic-attempt', provider_call_id='synthetic-call') == (entry.guidance_id,)
    assert store.guidance.mark_status(key, terminal).status == terminal
    receipt = replace(receipt, conversation_thread_id=target.thread_id,
                      result={'delivery_status': 'unknown', 'guidance_dedupe_key': key})
    path = store.guidance.storage.guidance_dedupe_path(key)
    return SimpleNamespace(conversation_store=store), receipt, scope, path


@pytest.mark.parametrize('terminal', ['consumed', 'rejected'])
def test_original_terminal_guidance_is_only_projected_without_rewrite(tmp_path, monkeypatch, terminal):
    agent, receipt, scope, path = original_terminal_guidance(tmp_path, monkeypatch, terminal)
    original = path.read_bytes()
    result = read_original_steer_delivery(agent, receipt, scope)
    assert result is not None and result.delivery_status == ('accepted' if terminal == 'consumed' else 'rejected')
    assert path.read_bytes() == original and receipt.result['delivery_status'] == 'unknown'


@pytest.mark.parametrize('mismatch', ['sender', 'message', 'thread_id', 'channel_message_id',
                                     'channel', 'conversation_id', 'gateway_input_request_id'])
def test_other_original_guidance_facts_never_confirm_this_control(tmp_path, monkeypatch, mismatch):
    agent, receipt, scope, path = original_terminal_guidance(tmp_path, monkeypatch, 'rejected', mismatch)
    original = path.read_bytes()
    assert read_original_steer_delivery(agent, receipt, scope) is None
    assert path.read_bytes() == original and receipt.result['delivery_status'] == 'unknown'
