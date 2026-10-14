"""用原模型账、完成合同和调度替身验证内部结果/公开回执边界，不接触真实账户或模型。"""
from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._tool_loop_service import _natural_user_reply_step
from agent_py_agent.agent.agent_core.model.response_provenance import (
    response_provenance_error,
    response_provenance_projection,
    stamp_response_provenance,
)
from agent_py_agent.agent.agent_core.tool_loop import background_public_receipt as receipt
from agent_py_agent.agent.agent_core.tool_loop.natural_user_reply import pending_natural_user_reply
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallFinishParams,
    ModelCallLedger,
    ModelCallStartedParams,
)
from agent_py_agent.agent.conversation.background_receipt_contract import (
    configure_background_public_receipt,
    public_receipt_delivery_block,
)
from agent_py_agent.agent.scheduler.delivery_policy import normalize_delivery
from agent_py_agent.agent.scheduler.schedule import ScheduleValidationError
from agent_py_agent.tests.test_tool_model_generation import _tool_loop_params


# LLM: 只用原循环DTO，合同字段代表宿主已经按冻结run装配；不通过模型attrs制造权限或其它身份。
# 函数用途: 建立没有生产文件和网络的完成合同测试参数。
def _params(*, explicit=True):
    return replace(_tool_loop_params(), user_prompt='检查合成平台并仅在有对外事项时通知',
        request_id='request-1', run_id='run-1', task_id='task-1', attempt_id='attempt-1',
        delivery_contract=({receipt.CONTRACT_KEY: {'schema': receipt.RESULT_SCHEMA, 'reply_policy': 'explicit'}} if explicit else None))


# LLM: 测试签发走同一个finished调用账，不构造私有seal；其它purpose只能通过原账表示。
# 函数用途: 模拟一次实际完成的模型生成并由正式入口绑定来源。
def _response(params, text, *, purpose='main', call_id='call-1', finished=True):
    ledger = ModelCallLedger()
    ledger.started(ModelCallStartedParams(call_id=call_id, backend='fake', model='fake', input_tokens=1,
        request_id=params.request_id, run_id=params.run_id, metadata={'logical_call_id': call_id,
            'task_id': params.task_id, 'attempt_id': params.attempt_id, 'purpose': purpose}))
    if finished:
        ledger.finished(ModelCallFinishParams(call_id=call_id, output_tokens=1))
    response = ModelResponse(text=text, backend='fake')
    stamp_response_provenance(params, ledger, call_id, response)
    return response, ledger


def _body(intent='publish', public='合成检查发现一项事项。', internal='合成检查完成，证据仍需按任务判断。'):
    return json.dumps({'schema': receipt.RESULT_SCHEMA, 'internal_result': internal,
        'notification_intent': intent, 'public_reply': public}, ensure_ascii=False)


def test_explicit_internal_only_is_success_with_internal_result_preserved():
    params = _params()
    response, _ = _response(params, _body('internal_only', ''))
    action, final = receipt.prepare_background_public_receipt(params, response)
    assert action == 'finish' and final.runtime_status == 'ok' and final.text == ''
    assert final.internal_response and final.public_receipt['notification_intent'] == 'internal_only'
    assert final.public_receipt['execution_source']['call_id'] == 'call-1'
    assert pending_natural_user_reply(params) is None


def test_real_provider_complete_json_fence_retains_internal_success_and_provenance():
    params = _params()
    internal = '合成监控静默检查执行完毕。样本数 3，全部探测成功，未发现异常，无需对外通知。'
    text = '```json\n' + _body('internal_only', '', internal) + '\n```'
    response, _ = _response(params, text)
    action, final = receipt.prepare_background_public_receipt(params, response)
    assert action == 'finish' and final.runtime_status == 'ok' and final.text == ''
    assert final.internal_response == internal
    assert final.public_receipt['notification_intent'] == 'internal_only'
    assert final.public_receipt['execution_source']['call_id'] == 'call-1'
    assert pending_natural_user_reply(params) is None
    assert receipt.prepare_background_public_receipt(_params(explicit=False), response) == ('normal', response)
    other = replace(params, run_id='other-run')
    action, failed = receipt.prepare_background_public_receipt(other, response)
    assert action == 'finish' and failed.runtime_reason == 'MODEL_RESPONSE_PROVENANCE_MISMATCH'


@pytest.mark.parametrize('text', [
    '说明\n```json\n' + _body('internal_only', '') + '\n```',
    '```json\n' + _body('internal_only', '') + '\n```\n后记',
    '```json\n' + _body('internal_only', '') + '\n```\n```json\n' + _body() + '\n```',
    '```python\n' + _body('internal_only', '') + '\n```',
    '```json\n' + _body('internal_only', '不得靠围栏隐藏矛盾正文') + '\n```',
    '```json\n' + json.dumps({'nested': json.loads(_body('internal_only', ''))}) + '\n```',
])
def test_json_wrapping_does_not_relax_whole_response_or_completion_schema(text):
    params = _params()
    response, _ = _response(params, text)
    action, final = receipt.prepare_background_public_receipt(params, response)
    assert action == 'finish' and final.runtime_reason == 'BACKGROUND_RECEIPT_CONTRACT_INVALID'
    assert final.runtime_status == 'failed' and final.text == '' and final.public_receipt is None


def test_normal_user_json_remains_public_json_and_no_extra_phase():
    params = _params()
    json_reply = '{"classification":"合成合法结果","count":2}'
    response, _ = _response(params, _body(public=json_reply))
    action, final = receipt.prepare_background_public_receipt(params, response)
    assert action == 'finish' and final.text == json_reply
    assert pending_natural_user_reply(params) is None
    ordinary = _params(explicit=False)
    response, _ = _response(ordinary, json_reply)
    assert receipt.prepare_background_public_receipt(ordinary, response) == ('normal', response)
    assert receipt.public_receipt_contract_prompt(ordinary) == ''


@pytest.mark.parametrize('text', ['（空）', '（本轮没有达到门槛的事项）', '{"classification":"other"}',
    _body('internal_only', '矛盾的公开正文'), _body('unknown'), _body(internal='')])
def test_explicit_protocol_errors_are_not_silence_or_content_blacklists(text):
    params = _params()
    response, _ = _response(params, text)
    action, final = receipt.prepare_background_public_receipt(params, response)
    assert action == 'finish' and final.runtime_status == 'failed' and final.text == ''
    assert final.runtime_reason == 'BACKGROUND_RECEIPT_CONTRACT_INVALID'
    ordinary = _params(explicit=False)
    assert receipt.prepare_background_public_receipt(ordinary, response)[0] == 'normal'


def test_physical_empty_response_cannot_declare_internal_success():
    params = _params()
    response, _ = _response(params, '')
    assert receipt.prepare_background_public_receipt(params, response) == ('normal', response)
    assert response.public_receipt is None and not response.internal_response


def test_attachment_obligation_cannot_be_cancelled_and_internal_result_survives(monkeypatch):
    params = _params()
    monkeypatch.setattr(receipt, '_has_pending_delivery_artifacts', lambda _params: True)
    response, _ = _response(params, _body('internal_only', ''))
    action, final = receipt.prepare_background_public_receipt(params, response)
    assert action == 'finish' and final.runtime_reason == 'BACKGROUND_RECEIPT_PUBLIC_OBLIGATION'
    assert final.internal_response and final.text == ''
    response, _ = _response(params, _body(public=''))
    action, final = receipt.prepare_background_public_receipt(params, response)
    assert action == 'finish' and final.runtime_status == 'ok' and final.text == ''
    assert final.public_receipt['notification_intent'] == 'publish'


def test_publish_missing_body_uses_exactly_one_existing_zero_tool_repair():
    params = _params()
    response, _ = _response(params, _body(public=''))
    assert receipt.prepare_background_public_receipt(params, response)[0] == 'repair'
    phase = pending_natural_user_reply(params)
    assert phase['kind'] == 'background_final_receipt'
    assert phase['facts']['internal_result_is_model_authored'] is True
    repair, _ = _response(params, '这是根据本轮执行情况补充的公开回复。', purpose='background_receipt_repair', call_id='call-2')
    verdict, final = _natural_user_reply_step(params, repair)
    assert verdict == 'finish' and final.runtime_status == 'ok'
    assert final.internal_response and final.public_receipt['public_source']['call_id'] == 'call-2'
    assert final.public_receipt['execution_source']['call_id'] == 'call-1'
    assert pending_natural_user_reply(params) is None


def test_failed_public_repair_preserves_internal_result_without_second_queue():
    params = _params()
    response, _ = _response(params, _body(public=''))
    assert receipt.prepare_background_public_receipt(params, response)[0] == 'repair'
    repair, _ = _response(params, '', purpose='background_receipt_repair', call_id='call-2')
    verdict, final = _natural_user_reply_step(params, repair)
    assert verdict == 'finish' and final.runtime_reason == 'BACKGROUND_RECEIPT_PUBLIC_UNAVAILABLE'
    assert final.internal_response and final.text == '' and final.public_receipt is None
    assert pending_natural_user_reply(params) is None


def test_consumed_guidance_reply_debt_prevents_internal_only(monkeypatch):
    params = _params()
    monkeypatch.setattr(receipt, 'active_turn_user_reply_required', lambda _params: True)
    response, _ = _response(params, _body('internal_only', ''))
    assert receipt.prepare_background_public_receipt(params, response)[0] == 'repair'
    assert pending_natural_user_reply(params)['facts']['public_reply_required'] is True


def test_existing_public_reply_satisfies_guidance_without_extra_request(monkeypatch):
    params = _params()
    satisfied = []
    monkeypatch.setattr(receipt, 'active_turn_user_reply_required', lambda _params: True)
    monkeypatch.setattr(receipt, 'satisfy_active_turn_user_reply', lambda _params: satisfied.append(True))
    response, _ = _response(params, _body())
    assert receipt.prepare_background_public_receipt(params, response)[0] == 'finish'
    assert satisfied == [True] and pending_natural_user_reply(params) is None


@pytest.mark.parametrize('purpose', ['decision', 'compact', 'probe:tool_capability', 'natural_user_reply'])
def test_other_call_purpose_cannot_become_business_final(purpose):
    params = _params()
    response, _ = _response(params, _body(), purpose=purpose)
    action, final = receipt.prepare_background_public_receipt(params, response)
    assert action == 'finish' and final.runtime_reason == 'MODEL_RESPONSE_PURPOSE_MISMATCH'
    assert not final.text


@pytest.mark.parametrize('key', ['request_id', 'run_id', 'task_id', 'attempt_id'])
def test_current_host_identity_must_match_final_call(key):
    params = _params()
    response, _ = _response(params, _body())
    other = replace(params, **{key: 'other'})
    assert response_provenance_error(other, response) == 'MODEL_RESPONSE_PROVENANCE_MISMATCH'


def test_old_response_and_persisted_metadata_cannot_restore_host_signature():
    params = _params()
    response, _ = _response(params, _body())
    projection = response_provenance_projection(response)
    forged = replace(response, response_provenance=projection)
    assert response_provenance_error(params, forged) == 'MODEL_RESPONSE_PROVENANCE_MISSING'
    _response(params, 'new', call_id='call-2')
    assert response_provenance_error(params, response) == 'MODEL_RESPONSE_PROVENANCE_MISMATCH'


def test_unfinished_or_missing_call_clears_prior_provider_fields():
    params = _params()
    old, _ = _response(params, _body())
    old.public_receipt = {'notification_intent': 'internal_only'}
    old.internal_response = 'provider cannot declare this'
    ledger = ModelCallLedger()
    stamp_response_provenance(params, ledger, 'missing', old)
    assert old.response_provenance is None and old.public_receipt is None and old.internal_response == ''
    response, _ = _response(params, _body(), finished=False)
    assert response_provenance_error(params, response) == 'MODEL_RESPONSE_PROVENANCE_MISSING'


def test_legacy_origin_keeps_always_and_explicit_policy_is_frozen_data():
    assert normalize_delivery(None) == {'mode': 'origin'}
    assert normalize_delivery({'mode': 'origin'}) == {'mode': 'origin'}
    assert normalize_delivery({'mode': 'origin', 'reply_policy': 'explicit'})['reply_policy'] == 'explicit'
    with pytest.raises(ScheduleValidationError):
        normalize_delivery({'mode': 'origin', 'reply_policy': [], 'target': 'other'})


def test_contract_is_prepared_only_from_exact_frozen_run_and_disabled_does_not_fallback():
    run = {'thread_id': 'thread-1', 'delivery': {'mode': 'origin', 'reply_policy': 'explicit'}}
    agent = SimpleNamespace(config=SimpleNamespace(background_public_receipt_contract_enabled=True),
        scheduler_repository=SimpleNamespace(get_active_run=lambda _task: run))
    request = SimpleNamespace(task_id='scheduled-run-1', thread_id='thread-1')
    params = SimpleNamespace(delivery_contract=None)
    configure_background_public_receipt(agent, request, params)
    assert receipt.explicit_public_receipt_active(params)
    agent.config.background_public_receipt_contract_enabled = False
    with pytest.raises(RuntimeError, match='BACKGROUND_RECEIPT_CONTRACT_DISABLED'):
        configure_background_public_receipt(agent, request, params)
    run['delivery'] = {'mode': 'local', 'reply_policy': 'explicit'}
    configure_background_public_receipt(agent, request, params)
    assert params.delivery_contract is None


def test_frozen_owner_retry_consumes_envelope_without_recreating_private_seal():
    run = {'thread_id': 'thread-1', 'delivery': {'mode': 'origin', 'reply_policy': 'explicit'}}
    agent = SimpleNamespace(scheduler_repository=SimpleNamespace(get_active_run=lambda _task: run))
    request = SimpleNamespace(task_id='scheduled-run-1', thread_id='thread-1')
    assert public_receipt_delivery_block(agent, request, None, 'raw final', has_attachments=False, frozen_retry=None) == 'failed'
    frozen = SimpleNamespace(message_metadata={'public_receipt': {'schema': receipt.RESULT_SCHEMA,
        'notification_intent': 'publish', 'public_source': {'call_id': 'archived'}}})
    assert public_receipt_delivery_block(agent, request, None, 'frozen public', has_attachments=True, frozen_retry=frozen) == ''
