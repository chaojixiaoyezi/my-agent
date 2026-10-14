"""原调度/后台主模型/投递链的回执回归；模型与外发是替身，其余使用正式事务和原账。"""
from __future__ import annotations

import math

import pytest

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.conversation import BackgroundMainAgentScheduler
from agent_py_agent.agent.conversation.runtime import BackgroundMainAgentRuntime
from agent_py_agent.tests.test_background_public_receipt_contract import _body
from agent_py_agent.tests.test_scheduler_im_l_integration import im_case
from agent_py_agent.tests.test_scheduler_runtime import _RetryDelivery


# LLM: 夹具通过正式schedule create建立冻结策略，后端只回复测试数据；不代被测agent修改任务产物或私有来源。
# 函数用途: 给真实调度链装上无网络的脚本模型与渠道运输，并记录真实调用数。
def _case(tmp_path, replies, *, delivery=None):
    case = im_case(tmp_path, group=True, delivery=delivery or {'mode': 'origin', 'reply_policy': 'explicit'})
    prompts = []

    def generate(prompt, *_args, **_kwargs):
        prompts.append(prompt)
        assert len(prompts) <= len(replies), '额外业务/表达模型调用不符合声明预算'
        # 刻意提供假的宿主完成字段，真实模型生成出口必须覆盖它们。
        return ModelResponse(text=replies[len(prompts) - 1], backend='synthetic',
            public_receipt={'notification_intent': 'internal_only'}, internal_response='untrusted provider field')

    case.backend.generate = generate
    channels = _RetryDelivery()
    channels.status = 'sent'
    runtime = BackgroundMainAgentRuntime(agent=case.agent, store=case.store, channels=channels)
    scheduler = BackgroundMainAgentScheduler({'runtime': runtime, 'store': case.store,
        'scheduler_service': case.agent.scheduler_service})
    return case, prompts, channels, scheduler


def test_explicit_no_public_content_keeps_monitor_success_without_extra_model_call(tmp_path):
    case, prompts, channels, scheduler = _case(tmp_path, [_body('internal_only', '', '内部完成，无公开内容。')])
    reports = scheduler.tick(now=case.job['next_run_at'])
    history, errors = case.agent.scheduler_repository.history()
    assert not errors and history[0]['status'] == 'done', history
    assert history[0]['delivery_status'] == 'suppressed'
    assert len(prompts) == 1 and not channels.attempts and reports[0].wake_handled
    run_thread = history[0]['thread_id']
    rows = case.store.messages.recent(run_thread, limit=0)
    assert any(row.metadata.get('internal_execution_result') == '内部完成，无公开内容。' for row in rows)
    assert not any(row.content == '内部完成，无公开内容。' for row in rows)


def test_real_provider_json_fence_completes_original_scheduler_without_public_send(tmp_path):
    internal = '合成监控静默检查执行完毕。样本数 3，全部探测成功，未发现异常，无需对外通知。'
    text = '```json\n' + _body('internal_only', '', internal) + '\n```'
    case, prompts, channels, scheduler = _case(tmp_path, [text])
    reports = scheduler.tick(now=case.job['next_run_at'])
    history, errors = case.agent.scheduler_repository.history()
    assert not errors and history[0]['status'] == 'done' and history[0]['delivery_status'] == 'suppressed'
    assert len(prompts) == 1 and not channels.attempts and reports[0].wake_handled
    rows = case.store.messages.recent(history[0]['thread_id'], limit=0)
    assert any(row.metadata.get('internal_execution_result') == internal for row in rows)
    assert not any(row.content == text or row.content == internal for row in rows)


@pytest.mark.parametrize('wrap', ['说明\n{body}', '{body}\n后记', '```python\n{body}\n```',
    '```json\n{body}\n```\n```json\n{body}\n```'])
def test_ambiguous_or_non_json_wrapper_cannot_silently_complete_or_reexecute(tmp_path, wrap):
    case, prompts, channels, scheduler = _case(tmp_path, [wrap.format(body=_body('internal_only', ''))])
    with pytest.raises(RuntimeError, match='BACKGROUND_RECEIPT_CONTRACT_INVALID'):
        scheduler.tick(now=case.job['next_run_at'])
    history, errors = case.agent.scheduler_repository.history()
    assert not errors and history[0]['status'] == 'failed'
    assert len(prompts) == 1 and not channels.attempts
    assert scheduler.tick(now=case.job['next_run_at'] + 31) == [] and len(prompts) == 1


def test_explicit_public_json_only_reaches_original_group_and_preserves_source(tmp_path):
    public = '{"legitimate_json":true,"count":3}'
    case, prompts, channels, scheduler = _case(tmp_path, [_body(public=public)])
    reports = scheduler.tick(now=case.job['next_run_at'])
    assert len(prompts) == 1 and len(channels.attempts) == 1
    context, envelope, _ = channels.attempts[0]
    assert context.target == 'oc_group' and envelope.content == public
    history, errors = case.agent.scheduler_repository.history()
    assert not errors and history[0]['status'] == 'done' and history[0]['delivery_status'] == 'sent'
    rows = case.store.messages.recent(case.thread.thread_id, limit=0)
    saved = next(row for row in rows if row.content == public)
    source = saved.metadata['public_receipt']['execution_source']
    assert source['purpose'] == 'main' and source['thread_id'] == history[0]['thread_id']
    assert source['call_id'] and reports[0].wake_handled


def test_explicit_missing_public_reply_adds_only_one_expression_call(tmp_path):
    case, prompts, channels, scheduler = _case(tmp_path, [_body(public=''), '根据本次检查，有一项事项需要关注。'])
    scheduler.tick(now=case.job['next_run_at'])
    history, errors = case.agent.scheduler_repository.history()
    assert not errors and history[0]['status'] == 'done' and history[0]['delivery_status'] == 'sent'
    assert len(prompts) == 2 and len(channels.attempts) == 1
    assert channels.attempts[0][1].content == '根据本次检查，有一项事项需要关注。'
    assert 'internal_result_is_model_authored' in prompts[1]


@pytest.mark.parametrize('delivery', [{'mode': 'origin'}, {'mode': 'local'}, {'mode': 'none'}])
def test_legacy_or_local_delivery_has_no_completion_schema_or_extra_call(tmp_path, delivery):
    case, prompts, channels, scheduler = _case(tmp_path, ['{"normal_json":true}'], delivery=delivery)
    scheduler.tick(now=case.job['next_run_at'])
    assert len(prompts) == 1 and '[background-result-contract]' not in prompts[0]
    if delivery['mode'] == 'origin':
        assert channels.attempts[0][1].content == '{"normal_json":true}'
    else:
        assert not channels.attempts


# LLM: 重投沿原尝试账的截止时间，不能假设模型和事务在一秒内结束；仍验证截止前零副作用与到期后冻结信封。
# 函数用途: 用真实持久重试时间检查公开回执拒收后的退避和恢复，模型、运输仍仅为替身。
def test_explicit_final_failure_freezes_public_envelope_and_cold_retry_has_zero_model_call(tmp_path):
    case, prompts, channels, scheduler = _case(tmp_path, [_body(public='原运行的公开内容。')])
    channels.status = 'rejected'
    assert scheduler.tick(now=case.job['next_run_at']) == []
    pending = case.store.wakes.pending()[0]
    frozen = pending.metadata['owner_delivery']
    assert frozen['content'] == '原运行的公开内容。'
    assert frozen['message_metadata']['public_receipt']['public_source']['purpose'] == 'main'
    state, error = case.store.wakes.attempts.state_report(pending.wake_signal_id)
    assert error is None and state.redelivery_failures == 1
    retry_at = state.next_attempt_at
    assert retry_at > case.job['next_run_at']
    channels.status = 'sent'
    assert scheduler.tick(now=math.nextafter(retry_at, -math.inf)) == []
    assert len(prompts) == len(channels.attempts) == 1
    reports = scheduler.tick(now=retry_at)
    assert reports and reports[0].wake_handled and len(prompts) == 1
    assert [item[1].content for item in channels.attempts] == ['原运行的公开内容。', '原运行的公开内容。']


def test_nonempty_unmanaged_final_cannot_bypass_explicit_contract_via_provider_metadata(tmp_path):
    case, prompts, channels, scheduler = _case(tmp_path, ['{"unmanaged_classification":true}'])
    with pytest.raises(RuntimeError, match='BACKGROUND_RECEIPT_CONTRACT_INVALID'):
        scheduler.tick(now=case.job['next_run_at'])
    history, errors = case.agent.scheduler_repository.history()
    assert not errors and history[0]['status'] == 'failed', history
    assert history[0]['delivery_status'] != 'sent'
    assert len(prompts) == 1 and not channels.attempts
    assert scheduler.tick(now=case.job['next_run_at'] + 31) == [] and len(prompts) == 1


@pytest.mark.parametrize('fault', ['purpose', 'run', 'thread'])
def test_real_core_no_tools_ordinary_json_still_rejects_wrong_call_source(tmp_path, monkeypatch, fault):
    from dataclasses import replace

    from agent_py_agent.agent.agent_core.model import response_provenance as source

    case, prompts, channels, scheduler = _case(tmp_path, ['{"normal_json":true}'], delivery={'mode': 'origin'})
    stamp = source.stamp_response_provenance

    def corrupted_stamp(params, ledger, call_id, response):
        stamp(params, ledger, call_id, response)
        field = {'purpose': 'purpose', 'run': 'run_id', 'thread': 'thread_id'}[fault]
        response.response_provenance = replace(response.response_provenance, **{field: 'decision' if fault == 'purpose' else 'other'})

    monkeypatch.setattr(source, 'stamp_response_provenance', corrupted_stamp)
    code = 'MODEL_RESPONSE_PURPOSE_MISMATCH' if fault == 'purpose' else 'MODEL_RESPONSE_PROVENANCE_MISMATCH'
    with pytest.raises(RuntimeError, match=code):
        scheduler.tick(now=case.job['next_run_at'])
    history, errors = case.agent.scheduler_repository.history()
    assert not errors and history[0]['status'] == 'failed'
    assert not channels.attempts and len(prompts) == 1


def test_disabled_contract_retains_typed_scheduler_error_without_model_or_retry(tmp_path):
    from dataclasses import replace

    from agent_py_agent.agent.contracts.background_public_receipt import (
        BackgroundPublicReceiptError,
    )

    case, prompts, channels, scheduler = _case(tmp_path, [_body()])
    case.agent.config = replace(case.agent.config, background_public_receipt_contract_enabled=False)
    with pytest.raises(BackgroundPublicReceiptError) as caught:
        scheduler.tick(now=case.job['next_run_at'])
    assert caught.value.error_code == 'BACKGROUND_RECEIPT_CONTRACT_DISABLED'
    history, errors = case.agent.scheduler_repository.history()
    assert not errors and history[0]['status'] == 'failed'
    assert history[0]['error_code'] == 'BACKGROUND_RECEIPT_CONTRACT_DISABLED'
    assert not prompts and not channels.attempts
    assert scheduler.tick(now=case.job['next_run_at'] + 31) == []
    assert not prompts and not channels.attempts
