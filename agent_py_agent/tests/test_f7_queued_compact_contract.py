# LLM: 只替换 urllib 最后 open 返回；真实 F6 冻结/媒体/辅助调用与 F2 物理队列、原 ledger/checkpoint 全链执行。
# 模块用途: 证明排队开启时摘要仍保留同一原生图片前缀，等待不虚增 HTTP 尝试或创建第二个 checkpoint。
import io
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.model.call_runtime import model_call_ledger, model_call_summary
from agent_py_agent.agent.backends import gateway_helpers, request_scope
from agent_py_agent.agent.conversation import compact, compact_request_budget
from agent_py_agent.agent.conversation.compact_checkpoint import committed_compact_checkpoint_chain
from agent_py_agent.agent.conversation.compact_provider_surface import (
    ConversationCompactModelSurface,
)
from agent_py_agent.agent.conversation.input_media import (
    import_input_media,
    input_media_blocks,
    input_media_root,
)
from agent_py_agent.agent.conversation.native_history import (
    CANONICAL_NATIVE_MESSAGES_METADATA_KEY,
    canonical_native_messages_envelope,
    provider_history_messages_from_rows,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests.test_f7_request_surface_contract import PNG, _image_bytes
from agent_py_agent.tests.test_provider_queue import wait_until


def test_enabled_queue_keeps_f6_native_prefix_and_counts_only_actual_summary_send(tmp_path, monkeypatch):
    agent = SimpleAgent(AgentConfig(model_backend='anthropic_compatible', model_name='synthetic-model',
        api_base='https://provider.invalid/v1', api_key='synthetic-no-network', stream_enabled=False,
        enable_tools=False, provider_max_concurrent_requests=1, model_context_window_tokens=200_000,
        model_input_modalities=['text', 'image'], my_agent_home=str(tmp_path / 'home'), prompt_files=[]), tmp_path)
    assert agent.home_paths.root.resolve() == (tmp_path / 'home').resolve()
    image_path = tmp_path / 'history.png'
    image_path.write_bytes(PNG)
    ref = import_input_media(image_path, input_media_root(agent))
    store = agent.conversation_store
    thread = store.threads.get_or_create({'canonical_user_id': 'synthetic-owner'})
    native = [{'role': 'user', 'content': [{'type': 'text', 'text': '旧图要求'}, *input_media_blocks([ref])]},
              {'role': 'assistant', 'content': [{'type': 'text', 'text': '旧图已确认'}]}]
    store.messages.append({'thread_id': thread.thread_id, 'role': 'user', 'content': '旧图要求',
        'metadata': {'conversation_request_id': 'old'}})
    store.messages.append({'thread_id': thread.thread_id, 'role': 'assistant', 'content': '旧图已确认',
        'metadata': {'conversation_request_id': 'old',
            CANONICAL_NATIVE_MESSAGES_METADATA_KEY: canonical_native_messages_envelope(native)}})
    original = store.messages.recent(thread.thread_id, limit=0)
    expected_prefix = provider_history_messages_from_rows(original)
    sent, frozen = [], []
    generate = compact_request_budget.generate_auxiliary_model_response

    def observe(request):
        frozen.append(request.messages)
        return generate(request)

    def open_request(request, **options):
        sent.append(json.loads(request.data))
        assert request_scope.provider_endpoint_counts(resource) == (1, 0)
        return io.BytesIO(json.dumps({'content': [{'type': 'text', 'text': '旧图要求与结论均已保留。'}],
            'stop_reason': 'end_turn', 'usage': {'input_tokens': 20, 'cache_read_input_tokens': 80,
                'output_tokens': 10}}).encode())

    monkeypatch.setattr(compact_request_budget, 'generate_auxiliary_model_response', observe)
    monkeypatch.setattr(gateway_helpers.urllib.request, 'build_opener',
        lambda *args: SimpleNamespace(open=open_request))
    resource = SimpleNamespace(api_base=agent.backend.api_base, provider_max_concurrent_requests=1)
    held = request_scope.acquire_provider_request(resource)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(compact.prepare_conversation_context, agent, store, thread,
                options=compact.ConversationCompactOptions(force=True,
                    model_surface=ConversationCompactModelSurface(context_scope='conversation')))
            try:
                wait_until(lambda: request_scope.provider_endpoint_counts(resource) == (1, 1))
                records = model_call_ledger(agent).records()
                assert len(records) == 1 and records[0].provider_attempt_count == 0 and not sent
            finally:
                held.release()
            result = future.result(timeout=3)
    finally:
        held.release()
    assert request_scope.provider_endpoint_counts(resource) == (0, 0)
    assert result.compacted and result.thread.compact_generation == 1
    assert len(sent) == len(frozen) == 1 and list(_image_bytes(sent[0])) == [PNG]
    assert tuple(frozen[0]) == expected_prefix
    assert store.messages.recent(thread.thread_id, limit=0) == original
    checkpoints = committed_compact_checkpoint_chain(agent, result.thread)
    assert len(checkpoints) == 1 and checkpoints[0]['media_refs'] == [ref['sha256']]
    record = model_call_ledger(agent).records()[0]
    assert record.metadata['purpose'] == 'conversation_compact_summary'
    summary = model_call_summary(agent, request_id=record.request_id, run_id=record.run_id)
    assert summary['provider_http_attempt_count'] == summary['physical_model_attempt_count'] == 1
    auxiliary = summary['purpose_breakdown']['auxiliary']
    assert auxiliary['provider_http_attempt_count'] == 1
    assert auxiliary['usage_breakdown']['provider']['cache_read_input_tokens'] == 80
    assert summary['purpose_breakdown']['main']['physical_model_attempt_count'] == 0
