"""工具直发与必要通知共享原回复槽，不按正文相同推断。"""
import pytest

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.delivery import ChannelCapabilities
from agent_py_agent.agent.tooling.runtime_contracts import ProviderToolCapability
from agent_py_agent.tests.test_scheduler_user_action_notice import real_notice_case


# LLM: 真Agent/Registry/Executor/归档/调度仅替换模型和运输，不连接外部；原401及scope仍由真实宿主保存。
# 函数用途: 让一次工作先收到认证拒绝，再直接回原群，最终文本故意与工具正文不同。
def reply_slot_case(tmp_path, monkeypatch, *, notice=True, message="已经核验，请您选择后续动作。"):
    if not notice:
        from agent_py_agent.agent.scheduler.repository import SchedulerRepository
        update = SchedulerRepository.update_job

        def origin_delivery(self, job_id, **kwargs):
            if kwargs.get('patch', {}).get('delivery') == {'mode': 'local'}:
                kwargs['patch'] = {**kwargs['patch'], 'delivery': {'mode': 'origin'}}
            return update(self, job_id, **kwargs)

        monkeypatch.setattr(SchedulerRepository, 'update_job', origin_delivery)
    case = real_notice_case(tmp_path, monkeypatch, 'group', work='llm')
    case.agent.config.enable_tools = True
    case.backend.probe_tool_capability = lambda: ProviderToolCapability(
        provider='synthetic', endpoint='local://test', model='test', stream=False,
        native_supported=True, evidence='synthetic')
    case.sends, case.files = [], []

    class Adapter:
        supports_frozen_attachment_content = True

        def send_attachment_content(self, target, content, **kwargs):
            case.files.append((target, content))
            return True

        def send_message(self, target, message):
            case.sends.append((target, message))
            return True

    case.agent.channel_registry.register_adapter('feishu', Adapter(),
        capabilities=ChannelCapabilities(text=True, proactive=True, files=True, frozen_attachment_content=True))

    def generate(prompt, **kwargs):
        case.backend.prompts.append(prompt)
        step = len(case.backend.prompts)
        if notice and step == 1:
            return ModelResponse(text='', backend=case.backend.name, tool_use_blocks=[
                {'id': 'auth', 'name': 'web_fetch', 'input': case.arguments}])
        if step == (2 if notice else 1):
            return ModelResponse(text='', backend=case.backend.name, tool_use_blocks=[
                {'id': 'reply', 'name': 'send_message', 'input': {'message': message}}])
        return ModelResponse(text='本片的最终说明与工具正文不同，不能拿文字相等去重。', backend=case.backend.name)

    case.backend.generate = generate
    return case


def test_original_work_tool_reply_commits_notice_slot_once(tmp_path, monkeypatch):
    case = reply_slot_case(tmp_path, monkeypatch)
    case.scheduler.tick(now=case.now)
    history, errors = case.agent.scheduler_repository.history()
    assert not errors and history[0]['status'] == 'failed'
    assert len(case.calls) == 1
    assert len(case.sends) == 1
    assert case.sends[0][0] == 'oc_synthetic'
    assert len(case.channels.attempts) == 0
    rows = case.store.messages.recent(case.thread.thread_id, limit=0)
    notices = [row for row in rows if row.metadata.get('background_delivery_reason') == 'required_user_action']
    assert len(notices) == 1
    assert notices[0].content == '已经核验，请您选择后续动作。'
    assert history[0]['delivery_status'] == 'sent'
    assert case.store.tasks.load(case.signal.root_task_id).status == 'abandoned'
    case.scheduler.tick(now=case.now + 31)
    assert len(case.sends) == 1 and len(case.channels.attempts) == 0


@pytest.mark.parametrize('notice', [True, False], ids=['required', 'normal'])
@pytest.mark.parametrize('field', ['owner_id', 'owner_home', 'request_id', 'run_id', 'task_id',
    'thread_id', 'source_thread_id', 'entry_request_id', 'conversation_id', 'actor_id', 'channel', 'target'])
def test_other_reply_binding_never_consumes_current_notice(tmp_path, monkeypatch, field, notice):
    from agent_py_agent.agent.capability import channel_message_tool
    original = channel_message_tool._source_reply_binding

    def changed(*args):
        binding = original(*args)
        return {**binding, field: 'different-' + binding[field]}

    monkeypatch.setattr(channel_message_tool, '_source_reply_binding', changed)
    case = reply_slot_case(tmp_path, monkeypatch, notice=notice)
    case.scheduler.tick(now=case.now)
    assert len(case.sends) == len(case.channels.attempts) == 1
    assert len(case.calls) == int(notice)
    assert case.channels.attempts[0][0].target == 'oc_synthetic'


@pytest.mark.parametrize('notice', [True, False], ids=['required', 'normal'])
@pytest.mark.parametrize('change', ['failed', 'unknown', 'not_executed', 'envelope'])
def test_canonical_operation_failure_cannot_be_replaced_by_sent_flag(tmp_path, monkeypatch, change, notice):
    from dataclasses import replace

    from agent_py_agent.agent.runtime_db.managed_operation_store import ManagedOperationStore
    original = ManagedOperationStore.get_tool_operation

    def changed(self, **kwargs):
        record = original(self, **kwargs)
        if record is None or record.status != 'succeeded' or kwargs.get('tool_name') != 'send_message':
            return record
        if change in {'failed', 'unknown'}:
            return replace(record, status=change)
        result = dict(record.result)
        if change == 'not_executed':
            result['handler_executed'] = False
        else:
            result['result_envelope'] = {}
        return replace(record, result=result)

    monkeypatch.setattr(ManagedOperationStore, 'get_tool_operation', changed)
    case = reply_slot_case(tmp_path, monkeypatch, notice=notice)
    case.scheduler.tick(now=case.now)
    assert len(case.sends) == len(case.channels.attempts) == 1
    assert len(case.calls) == int(notice)


@pytest.mark.parametrize('notice', [True, False], ids=['required', 'normal'])
def test_original_flag_without_source_binding_does_not_claim_notice(tmp_path, monkeypatch, notice):
    monkeypatch.setattr('agent_py_agent.agent.capability.channel_message_tool._source_reply_binding', lambda *_: {})
    case = reply_slot_case(tmp_path, monkeypatch, notice=notice)
    case.scheduler.tick(now=case.now)
    assert len(case.sends) == len(case.channels.attempts) == 1


def test_normal_work_tool_reply_commits_source_slot_once(tmp_path, monkeypatch):
    case = reply_slot_case(tmp_path, monkeypatch, notice=False)
    reports = case.scheduler.tick(now=case.now)
    assert len(case.sends) == 1 and not case.channels.attempts and not case.calls
    assert reports[0].delivery_reason == 'scheduled_message_tool_delivery'
    messages = case.store.messages.recent(case.thread.thread_id, limit=0)
    assert len([row for row in messages if row.content == '已经核验，请您选择后续动作。']) == 1
    case.scheduler.tick(now=case.now + 31)
    assert len(case.sends) == 1 and not case.channels.attempts


@pytest.mark.parametrize('notice', [True, False], ids=['required', 'normal'])
def test_original_work_identity_cannot_be_replaced_by_other_attempt(tmp_path, monkeypatch, notice):
    from agent_py_agent.agent.conversation import background_execution
    original = background_execution.collect_background_execution_result

    def changed(*args, **kwargs):
        from dataclasses import replace
        result = original(*args, **kwargs)
        return replace(result, execution_identity={**result.execution_identity, 'attempt_id': 'other-attempt'})

    monkeypatch.setattr(background_execution, 'collect_background_execution_result', changed)
    # runtime imported the producer directly; both callsites still consume the same real pipeline.
    monkeypatch.setattr('agent_py_agent.agent.conversation.runtime.collect_background_execution_result', changed)
    case = reply_slot_case(tmp_path, monkeypatch, notice=notice)
    case.scheduler.tick(now=case.now)
    assert len(case.sends) == len(case.channels.attempts) == 1


@pytest.mark.parametrize('notice', [True, False], ids=['required', 'normal'])
def test_sent_tool_reply_canonical_failure_replays_without_model_or_resend(tmp_path, monkeypatch, notice):
    case = reply_slot_case(tmp_path, monkeypatch, notice=notice)
    original = case.store.messages.append_once
    failed = []

    def fail_once(params, **kwargs):
        if kwargs.get('dedupe_key', '').startswith('owner_delivery:') and not failed:
            failed.append(True)
            raise OSError('synthetic canonical append failure')
        return original(params, **kwargs)

    monkeypatch.setattr(case.store.messages, 'append_once', fail_once)
    with pytest.raises(OSError, match='synthetic canonical append failure'):
        case.scheduler.tick(now=case.now)
    pending = case.store.wakes.pending_one(case.signal.wake_signal_id)
    assert failed and pending is not None
    frozen = pending.metadata['owner_delivery']
    assert frozen['external_sent'] is True and frozen['receipt_id']
    counts = (len(case.backend.prompts), len(case.sends), len(case.channels.attempts), len(case.calls))
    state, error = case.store.wakes.attempts.state_report(case.signal.wake_signal_id)
    assert error is None and state is not None
    assert state.total_count == state.same_cause_count == 1 and state.next_attempt_at > case.now
    case.scheduler.tick(now=max(case.now + 31, state.next_attempt_at))
    assert (len(case.backend.prompts), len(case.sends), len(case.channels.attempts), len(case.calls)) == counts
    assert len(case.sends) == 1 and not case.channels.attempts
    assert case.store.wakes.pending_one(case.signal.wake_signal_id) is None
    rows = case.store.messages.recent(case.thread.thread_id, limit=0)
    assert sum(row.content == frozen['content'] for row in rows) == 1


@pytest.mark.parametrize('notice', [True, False], ids=['required', 'normal'])
def test_successful_operation_to_another_resolved_target_does_not_claim_source(tmp_path, monkeypatch, notice):
    from agent_py_agent.agent.capability import channel_message_target
    original = channel_message_target.resolve_message_target

    def other_target(*args, **kwargs):
        channel, _, root, error = original(*args, **kwargs)
        return channel, 'oc_other_authorized_fixture', root, error

    # 只替换宿主已授权目标解析的结果；实际运输/canonical成功且binding记录真实异目标。
    monkeypatch.setattr(channel_message_target, 'resolve_message_target', other_target)
    case = reply_slot_case(tmp_path, monkeypatch, notice=notice)
    case.scheduler.tick(now=case.now)
    assert len(case.sends) == len(case.channels.attempts) == 1
    assert case.sends[0][0] == 'oc_other_authorized_fixture'
    assert case.channels.attempts[0][0].target == 'oc_synthetic'


@pytest.mark.parametrize('notice', [True, False], ids=['required', 'normal'])
def test_consumer_rechecks_canonical_after_finalizer_projection(tmp_path, monkeypatch, notice):
    from dataclasses import replace

    from agent_py_agent.agent.conversation import runtime
    original = runtime.collect_background_execution_result

    def forged(*args, **kwargs):
        result = original(*args, **kwargs)
        assert result.message_tool_deliveries  # 原finalizer强校验确实已通过。
        return replace(result, message_tool_deliveries=tuple(
            {**item, 'content': '伪造的后续投影正文'} for item in result.message_tool_deliveries))

    monkeypatch.setattr(runtime, 'collect_background_execution_result', forged)
    case = reply_slot_case(tmp_path, monkeypatch, notice=notice)
    case.scheduler.tick(now=case.now)
    assert len(case.sends) == len(case.channels.attempts) == 1


@pytest.mark.parametrize('change', [
    {'schema_version': 'bad'}, {'task_id': 'other-task'}, {'reason': 'other-reason'}, {'external_sent': False},
])
def test_invalid_frozen_receipt_keeps_original_nonquota_closeout(change):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.runtime import _handle_nonquota_wake_error

    frozen = {'schema_version': 'wake-owner-delivery.v2', 'reason': 'scheduled_job_due',
        'task_id': 'task-1', 'content': '原冻结正文', 'external_sent': True, **change}
    signal = SimpleNamespace(wake_signal_id='wake-1', reason='scheduled_job_due', root_task_id='task-1',
        metadata={'owner_delivery': frozen})
    effects = []
    scheduler = SimpleNamespace(store=SimpleNamespace(wakes=SimpleNamespace(
        pending_one=lambda _: signal, mark_handled=lambda *a, **k: effects.append('handled'))),
        scheduler_service=SimpleNamespace(release=lambda *a, **k: effects.append('released'),
            finish=lambda *a, **k: effects.append('finished') or {'status': 'failed'}))
    _handle_nonquota_wake_error(scheduler, signal, lifecycle_reason='scheduled_job_due',
        claim=object(), error=OSError('synthetic canonical append failure'))
    assert effects == ['finished', 'handled']


@pytest.mark.parametrize('notice', [True, False], ids=['required', 'normal'])
def test_attachment_only_source_reply_preserves_complete_committed_envelope(tmp_path, monkeypatch, notice):
    from agent_py_agent.agent.artifacts.registry import ArtifactRegistration, register_artifact
    from agent_py_agent.agent.capability import channel_message_tool

    case = reply_slot_case(tmp_path, monkeypatch, notice=notice, message='')
    artifact = case.agent.home_paths.owner_home_dir / 'tasks' / 'attachment-fixture' / 'output' / 'result.bin'
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b'synthetic verified attachment')
    register_artifact(ArtifactRegistration(workspace_root=artifact.parent.parent, path=artifact,
        artifact_id='attachment-result', status='ready', source='test'))
    generate, success = case.backend.generate, channel_message_tool._success_result
    refs = ['audit://synthetic/candidate/1:0']

    def with_attachment(*args, **kwargs):
        result = generate(*args, **kwargs)
        for block in result.tool_use_blocks or ():
            if block['name'] == 'send_message':
                block['input']['attachments'] = [str(artifact)]
        return result

    def with_typed_refs(*args, **kwargs):
        # 合成原handler签发的已发送引用；canonical与archive仍由真实执行器保存并共核。
        return success(*args, **{**kwargs, 'evidence_refs': refs})

    case.backend.generate = with_attachment
    monkeypatch.setattr(channel_message_tool, '_success_result', with_typed_refs)
    reports = case.scheduler.tick(now=case.now)
    assert reports[0].delivery_status == 'sent'
    assert not case.channels.attempts and not case.sends and len(case.files) == 1
    rows = case.store.messages.recent(case.thread.thread_id, limit=0)
    mirrored = [row for row in rows if row.metadata.get('delivery_artifacts')]
    assert len(mirrored) == 1 and mirrored[0].content == ''
    assert mirrored[0].metadata['delivery_artifacts'][0]['path'] == str(artifact)
    assert mirrored[0].metadata['evidence_refs'] == refs
