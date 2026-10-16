"""原IM输入图片持久化与真实委派授权；只有路由模型/runner和供应商运输为替身。"""
from __future__ import annotations

import base64
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.host_message_context import (
    CONTEXT_KEY,
    HostMessageContext,
)
from agent_py_agent.agent.conversation.input_media import import_input_media_bytes, input_media_root
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic

_PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=='
)


# LLM: 只为真实临时agent建立已验证的原请求附件，dispatch/SessionTask/RuntimeDB均不替换；开关来自原能力配置。
# 函数用途: 复用既有后台装配fixture，在入站派发前放入合成图片，不创建平台连接或后台进程。
def media_case(tmp_path, monkeypatch, *, enabled=True, conversation_id="oc_a"):
    from agent_py_agent.agent.gateway_parts.im_task_routing import prepare_im_task_dispatch
    from agent_py_agent.tests.test_im_delegation_runtime import runtime_fixture
    from agent_py_agent.tests.test_subagent_input_media import _enable_input_media

    capability = tmp_path / "workspace" / "config" / "capability_config.yaml"
    capability.parent.mkdir(parents=True, exist_ok=True)
    capability.write_text(f"subagent_input_media_enabled: {'true' if enabled else 'false'}\n", encoding="utf-8")
    captured = {}

    def dispatch(context, conversation, prompt):
        _enable_input_media(context.agent, enabled)
        ref = import_input_media_bytes(_PNG, input_media_root(context.agent), name='synthetic.png')
        captured['ref'] = ref
        context.request['input_media'] = [ref]
        write_json_file_atomic(context.request_path, context.request)
        scope = SimpleNamespace(channel="feishu", channel_user_id="ou_a", channel_conversation_id=conversation_id)
        return prepare_im_task_dispatch(context, SimpleNamespace(thread_id=conversation.thread_id, scope=scope), prompt)

    monkeypatch.setattr('agent_py_agent.tests.test_im_delegation_runtime.prepare_im_task_dispatch', dispatch)
    case = runtime_fixture(tmp_path, monkeypatch, conversation_id=conversation_id)
    return (*case, captured['ref'])


@pytest.mark.parametrize("conversation_id", ["oc_a", "oc_a:thread:om_topic"])
def test_media_only_input_persists_once_in_session_task_and_root_keeps_exact_reference(tmp_path, monkeypatch, conversation_id):
    from agent_py_agent.agent.conversation.im_delegation import prepare_delegated_execution

    agent, params, task, _, _, ref = media_case(tmp_path, monkeypatch, conversation_id=conversation_id)
    record = task.metadata[CONTEXT_KEY]
    assert record['input_media'] == [ref] and record['references'] == []
    restored = HostMessageContext.from_record(record)
    assert restored.to_record() == record
    prepare_delegated_execution(agent, params)
    link = agent.conversation_store.tasks.load(task.task_id)
    assert link.host_message_context == {'source': 'session_task', 'task_id': task.task_id}
    assert len(agent.subagents.list_runs()) == 1
    child = agent.subagents.list_runs()[0]
    assert child.attributes['input_media'] == [ref]
    assert CONTEXT_KEY not in child.attributes


def test_disabled_media_delegation_fails_before_session_task_or_child_is_created(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.orchestration.input_media_refs import (
        SubagentInputMediaError,
    )
    from agent_py_agent.tests.test_im_delegation_runtime import SimpleAgent

    agents = []

    def capture(*args, **kwargs):
        agent = SimpleAgent(*args, **kwargs)
        agents.append(agent)
        return agent

    monkeypatch.setattr('agent_py_agent.tests.test_im_delegation_runtime.SimpleAgent', capture)
    with pytest.raises(SubagentInputMediaError) as error:
        media_case(tmp_path, monkeypatch, enabled=False)
    assert error.value.error_code == 'SUBAGENT_INPUT_MEDIA_DISABLED'
    assert not agents[0].conversation_store.session_tasks.list_report(limit=0)[0]
    assert not agents[0].subagents.list_runs()


def test_root_media_survives_request_gc_but_manual_child_does_not_inherit_unrequested_images(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.orchestration.create_policy import create_run_params
    from agent_py_agent.agent.conversation.host_message_context import media_for_run
    from agent_py_agent.agent.conversation.im_delegation import prepare_delegated_execution
    from agent_py_agent.tests.test_host_message_context import select_child

    agent, params, task, context, _, ref = media_case(tmp_path, monkeypatch)
    actor = agent._current_run_params.host_channel_actor
    prepare_delegated_execution(agent, params)
    context.request_path.unlink()
    from agent_py_agent.agent.conversation.store import ConversationStore
    agent.conversation_store = ConversationStore(agent.conversation_store.storage.root)
    assert media_for_run(agent, actor) == (ref,)
    child = agent.subagents.list_runs()[0]
    select_child(agent, child)
    assert media_for_run(agent, actor) == (ref,)
    raw = {'goal': '不请求原图的孩子'}
    manual = agent.subagents.create_run(params=create_run_params(agent, raw, raw['goal'], ['read_file']))
    select_child(agent, manual)
    assert not media_for_run(agent, actor)


def test_host_event_media_reaches_provider_and_remains_media_for_model_selection():
    from agent_py_agent.agent.agent_core.tool_request_projection import _ir_item_input_modalities
    from agent_py_agent.agent.backends.message_adapter import AnthropicMessageAdapter
    from agent_py_agent.agent.backends.tool_ir import RuntimeFactsTurn

    ref = {'path': '/synthetic/owner/media/input/' + 'a' * 64, 'sha256': 'a' * 64,
        'media_type': 'image/png', 'size_bytes': 10, 'name': 'synthetic.png'}
    event = RuntimeFactsTurn('原后台任务事实', source='session_task', media=(ref,))
    assert _ir_item_input_modalities(event) == ('image',)
    messages = AnthropicMessageAdapter().to_provider_messages([event])
    assert messages[0]['role'] == 'user'
    assert messages[0]['content'][-1]['source']['sha256'] == ref['sha256']


@pytest.mark.parametrize('fault', ['new_gateway', 'stale_attempt', 'wrong_owner', 'forged_attrs'])
def test_media_current_authority_and_new_request_isolation(tmp_path, monkeypatch, fault):
    from agent_py_agent.agent.conversation.host_message_context import media_for_run
    from agent_py_agent.agent.conversation.im_delegation import prepare_delegated_execution
    from agent_py_agent.tests.test_host_message_context import select_child

    agent, params, _, _, _, ref = media_case(tmp_path, monkeypatch)
    actor = agent._current_run_params.host_channel_actor
    prepare_delegated_execution(agent, params)
    child = agent.subagents.list_runs()[0]
    record = select_child(agent, child)
    if fault == 'new_gateway':
        current = agent._current_run_params
        current.source, current.request_id = 'gateway', 'independent-new-request'
        current.host_message_context = HostMessageContext.from_record({**agent.conversation_store.session_tasks.load(current.task_id).metadata[CONTEXT_KEY]})
    elif fault == 'stale_attempt':
        agent.subagents.runtime_db.create_attempt(record['agent_run_id'])
    elif fault == 'wrong_owner':
        actor = replace(actor, owner_id='other-owner')
    else:
        agent._current_run_params.task_attributes['input_media'] = []
        assert media_for_run(agent, actor) == (ref,)
        return
    from agent_py_agent.agent.runtime_db.managed_operation_store import AuthorityContextMissing
    with pytest.raises((ValueError, AuthorityContextMissing)):
        media_for_run(agent, actor)


# LLM: 复用真实agent快照及当前authority，仅模型发送留给调用方替身；不能用人工attrs为IM授权图片。
# 函数用途: 将已绑定的当前主/子身份装配到产品原生循环，检查实际开头IR与状态而不启动模型。
def native_loop(agent, *, attrs=None, trigger=None, protocol='native'):
    from agent_py_agent.agent.agent_core.runtime.loop_models import (
        RuntimeLoopParams,
        RuntimeToolLoopSeed,
    )
    from agent_py_agent.agent.agent_core.runtime.loop_support import _tool_loop_execute_params
    from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot

    current = agent._current_run_params
    params = RuntimeLoopParams(user_prompt='识别原图', root_user_prompt='识别原图', memories=[],
        runtime_injections=[], routed_context=None, resume_context_section='',
        request_id=current.request_id, run_id=current.run_id, task_id=current.task_id,
        attempt_id=current.attempt_id, task_attributes=attrs or {}, turn_trigger=trigger)
    return _tool_loop_execute_params(agent, RuntimeToolLoopSeed(params=params, memories=[],
        tool_catalog_section='', tool_recommendations_section='',
        tool_runtime_snapshot=agent.tools.runtime_snapshot(run_id=current.run_id),
        tool_protocol_snapshot=make_test_protocol_snapshot(run_id=current.run_id, source_protocol=protocol)))


@pytest.mark.parametrize('kind', ['session_task', 'lifecycle_wake'])
def test_background_native_event_and_three_provider_projections_get_exact_image_bytes(tmp_path, monkeypatch, kind):
    from agent_py_agent.agent.agent_core.runtime.turn_trigger import TurnTrigger
    from agent_py_agent.agent.backends.message_adapter import AnthropicMessageAdapter
    from agent_py_agent.agent.backends.openai_chat import _openai_user_messages
    from agent_py_agent.agent.backends.responses_wire import message_items
    from agent_py_agent.agent.backends.tool_ir import RuntimeFactsTurn, UserTurn
    from agent_py_agent.agent.conversation.im_delegation import prepare_delegated_execution
    from agent_py_agent.agent.conversation.input_media import provider_media_messages

    agent, params, _, _, _, ref = media_case(tmp_path, monkeypatch)
    assert agent.conversation_store.tasks.load(params.task_id) is None
    loop = native_loop(agent, trigger=TurnTrigger(kind=kind, reason=kind))
    prepare_delegated_execution(agent, params)
    first = loop.tool_ir_history[0]
    assert isinstance(first, RuntimeFactsTurn) and first.media == (ref,)
    assert not any(isinstance(item, UserTurn) for item in loop.tool_ir_history)
    assert loop.task_attributes['input_media'] == [ref]
    canonical = AnthropicMessageAdapter().to_provider_messages(loop.tool_ir_history)
    source = provider_media_messages(canonical)[0]['content'][1]['source']
    assert base64.b64decode(source['data']) == _PNG
    chat = _openai_user_messages(canonical[0]['content'])
    assert base64.b64decode(chat[0]['content'][1]['image_url']['url'].split(',')[1]) == _PNG
    response = message_items(canonical[0], 'synthetic-vision')
    image = next(item for item in response if isinstance(item['content'], list) and item['content'][0]['type'] == 'input_image')
    assert base64.b64decode(image['content'][0]['image_url'].split(',')[1]) == _PNG


def test_gateway_typed_context_and_inline_promotion_are_original_media_authority(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.backends.tool_ir import UserTurn
    from agent_py_agent.agent.conversation.host_message_context import (
        context_for_input,
        promotion_context_binding,
    )

    agent, _, _, _, _, ref = media_case(tmp_path, monkeypatch)
    actor = agent._current_run_params.host_channel_actor
    context = context_for_input(agent, actor, input_media=[ref])
    current = RunParams(source='gateway', request_id=actor.entry_request_id, run_id='direct-run',
        task_id='direct-task', attempt_id='direct-attempt', host_channel_actor=actor, host_message_context=context)
    agent._current_run_params = current
    binding = promotion_context_binding(agent, current, actor, actor.source_thread_id)
    assert binding.snapshot is context and not binding.session_task_id
    loop = native_loop(agent, attrs={'input_media': [{'path': '/forged'}]})
    assert isinstance(loop.tool_ir_history[0], UserTurn) and loop.tool_ir_history[0].media == (ref,)
    current.request_id = 'different-request'
    with pytest.raises(ValueError):
        native_loop(agent)


@pytest.mark.parametrize('declared,protocol,rejected', [(['text'], 'native', True),
    ([], 'native', False), (['text', 'image'], 'native', False), (['text', 'image'], 'text', True)])
def test_native_protocol_and_explicit_visual_modalities_are_separate_deterministic_gates(tmp_path, monkeypatch, declared, protocol, rejected):
    from agent_py_agent.agent.conversation.im_delegation import prepare_delegated_execution
    from agent_py_agent.agent.conversation.input_media import InputMediaError

    agent, params, _, _, _, _ = media_case(tmp_path, monkeypatch)
    prepare_delegated_execution(agent, params)
    agent.config.model_input_modalities = declared
    if rejected:
        with pytest.raises(InputMediaError):
            native_loop(agent, protocol=protocol)
    else:
        assert native_loop(agent, protocol=protocol).task_attributes['input_media']


def test_grandchild_only_gets_explicit_parent_subset_and_later_disabled_gate_rejects(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.hierarchy_tools import _hierarchy_child_spec
    from agent_py_agent.agent.agent_core.orchestration.input_media_refs import (
        SubagentInputMediaError,
    )
    from agent_py_agent.agent.conversation.host_message_context import media_for_run
    from agent_py_agent.agent.conversation.im_delegation import prepare_delegated_execution
    from agent_py_agent.agent.subagents.services.hierarchy.scheduler import HierarchyScheduleRequest
    from agent_py_agent.tests.test_host_message_context import select_child
    from agent_py_agent.tests.test_subagent_input_media import _enable_input_media

    agent, params, _, _, _, ref = media_case(tmp_path, monkeypatch)
    prepare_delegated_execution(agent, params)
    child = agent.subagents.list_runs()[0]
    select_child(agent, child)
    spec = _hierarchy_child_spec(agent, {'goal': '孙代理看图', 'input_media_refs': [ref['sha256']]}, tool_name='create_subagents')
    result = agent.subagents.hierarchy.schedule_child_runs(params=HierarchyScheduleRequest(parent_run_id=child.id,
        child_specs=[spec], apply=True, requested_by=child.id))
    grandchild = agent.subagents.load(result.created_run_ids[0])
    select_child(agent, grandchild)
    assert media_for_run(agent, agent._current_run_params.host_channel_actor) == (ref,)
    assert native_loop(agent).tool_ir_history[0].media == (ref,)
    _enable_input_media(agent, False)
    with pytest.raises(SubagentInputMediaError):
        native_loop(agent)


def test_compact_tool_retirement_does_not_erase_original_host_event_images():
    from agent_py_agent.agent.agent_core.tool_ir_history import summarized_runtime_fact_indexes
    from agent_py_agent.agent.backends.tool_ir import RuntimeFactsTurn, ToolResult

    event = RuntimeFactsTurn('原后台图', source='host.session_task', media=({'sha256': 'a' * 64},))
    history = [event, ToolResult(call_id='call', tool_name='read_file', status='succeeded'),
        RuntimeFactsTurn('更新状态', source=event.source)]
    assert summarized_runtime_fact_indexes(history, {1}) == set()


@pytest.mark.parametrize('declared', [['text', 'image'], ['text']])
def test_automatic_im_child_actual_runner_sends_original_image_or_fails_without_send(tmp_path, monkeypatch, declared):
    import json

    from agent_py_agent.agent.backends import http
    from agent_py_agent.agent.conversation.im_delegation import prepare_delegated_execution
    from agent_py_agent.agent.core import SimpleAgent

    def configured(config, root):
        return SimpleAgent(replace(config, model_backend='anthropic_compatible', model_name='synthetic-vision',
            api_base='https://provider.invalid/anthropic', api_key='synthetic-no-network', stream_enabled=False,
            enable_tools=False, model_input_modalities=declared, model_context_window_tokens=200000), root)

    monkeypatch.setattr('agent_py_agent.tests.test_im_delegation_runtime.SimpleAgent', configured)
    agent, params, _, _, _, ref = media_case(tmp_path, monkeypatch)
    prepare_delegated_execution(agent, params)
    child = agent.subagents.list_runs()[0]
    observed = []

    def send(request):
        observed.append(request.payload)
        return {'content': [{'type': 'text', 'text': '合成图识别完成'}], 'stop_reason': 'end_turn'}

    monkeypatch.setattr(http, 'post_json', send)
    result = agent.run_subagent(child.id, dry_run=False, probe=False)
    if declared == ['text']:
        assert not observed
        final = agent.subagents.load(child.id)
        assert final.status == 'FAILED'
        run = agent.subagents.runtime_db.agent_run_for_run_id(child.id)
        attempt = agent.subagents.runtime_db.get_attempt(run['current_attempt_id'])
        assert run['status'] == 'failed' and attempt['status'] == 'failed' and attempt['ended_at'] > 0
        return
    assert observed, str(result)
    images = [block for row in observed[0]['messages'] for block in row.get('content', [])
        if isinstance(block, dict) and block.get('type') == 'image']
    assert len(images) == 1 and base64.b64decode(images[0]['source']['data']) == _PNG
    assert 'local_file' not in json.dumps(observed[0]) and ref['path'] not in json.dumps(observed[0])


def test_gateway_constructor_creates_media_context_from_top_level_only(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.host_message_context import media_for_run
    from agent_py_agent.agent.gateway_parts.request_context import (
        GatewayAskRunContext,
        GatewayConversationContext,
    )
    from agent_py_agent.agent.gateway_parts.request_execution import (
        _gateway_run_params,
        _GatewayRunParamsRequest,
    )

    agent, _, _, _, source, ref = media_case(tmp_path, monkeypatch)
    conversation = GatewayConversationContext(thread_id=source.thread_id, scope=SimpleNamespace(
        channel='feishu', channel_user_id='ou_a', channel_conversation_id='oc_a'))
    request = {'id': 'direct-native-image', 'prompt': '原图识别', 'input_media': [ref]}
    context = GatewayAskRunContext(agent, request, tmp_path / 'direct-request.json',
        tmp_path / 'direct-response.json', request['id'], None)
    params = _gateway_run_params(_GatewayRunParamsRequest(request, context, conversation, request['prompt']))
    assert params.host_message_context.to_record()['input_media'] == [ref]
    agent._current_run_params = params
    assert media_for_run(agent, params.host_channel_actor) == (ref,)
    request.pop('input_media')
    request['metadata'] = {'input_media': [ref]}
    params = _gateway_run_params(_GatewayRunParamsRequest(request, context, conversation, request['prompt']))
    assert params.host_message_context is None and not (params.task_attributes or {}).get('input_media')


@pytest.mark.parametrize('fault', ['cancelled_session', 'stale_attempt', 'wrong_actor'])
def test_unpromoted_session_input_requires_exact_active_main_and_original_actor(tmp_path, monkeypatch, fault):
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskUpdate
    from agent_py_agent.agent.runtime_db.managed_operation_store import AuthorityContextMissing

    agent, params, task, _, _, _ = media_case(tmp_path, monkeypatch)
    assert agent.conversation_store.tasks.load(task.task_id) is None
    if fault == 'cancelled_session':
        agent.conversation_store.session_tasks.advance(task.task_id, SessionTaskUpdate(status='cancelled'))
    elif fault == 'stale_attempt':
        run = agent.subagents.runtime_db.main_agent_run_for_task(task.task_id)
        agent.subagents.runtime_db.create_attempt(run['agent_run_id'])
    else:
        agent._current_run_params.host_channel_actor = replace(agent._current_run_params.host_channel_actor, actor_id='ou_other')
    with pytest.raises((ValueError, AuthorityContextMissing)):
        native_loop(agent)
    assert not agent.subagents.list_runs() and agent.conversation_store.tasks.load(params.task_id) is None


def test_typed_media_snapshot_rejects_nested_fields_and_cross_owner_paths(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.host_message_context import context_for_input
    from agent_py_agent.agent.conversation.input_media import InputMediaError

    agent, _, task, _, _, ref = media_case(tmp_path, monkeypatch)
    raw = task.metadata[CONTEXT_KEY]
    for field, invalid in [('sha256', {'secret': 'synthetic'}), ('size_bytes', True), ('path', '../relative')]:
        broken = {**raw, 'input_media': [{**ref, field: invalid}]}
        with pytest.raises(ValueError):
            HostMessageContext.from_record(broken)
    with pytest.raises(InputMediaError):
        context_for_input(agent, agent._current_run_params.host_channel_actor,
            input_media=[{**ref, 'path': str(tmp_path / 'foreign-owner' / ref['sha256'])}])
